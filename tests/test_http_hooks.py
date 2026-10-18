"""Adapter hooks on the shared HTTP client: reset headers, classifier, per-attempt hooks."""

from __future__ import annotations

import logging

import httpx
import pytest

from bot_farm_silence_tracker.sources.errors import BudgetExceeded, RateLimited, SourceError
from bot_farm_silence_tracker.sources.http import (
    DEFAULT_MAX_RETRY_WAIT,
    Attempt,
    HttpClient,
    reset_at_epoch,
    reset_in_seconds,
)

from .test_http import client


def test_reset_in_seconds_hook_used_without_retry_after() -> None:
    calls = iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(calls), headers={"X-Ratelimit-Reset": "42"})

    http, sleeps = client(handler)
    http.configure(reset_wait=reset_in_seconds("X-Ratelimit-Reset"))
    assert http.get("https://example.test").status_code == 200
    assert sleeps == [42.0]


def test_reset_at_epoch_hook_and_retry_after_wins() -> None:
    hook = reset_at_epoch("x-rate-limit-reset", margin=1.0, clock=lambda: 1_000.0)
    assert hook(httpx.Response(429, headers={"x-rate-limit-reset": "1060"})) == 61.0
    assert hook(httpx.Response(429, headers={"x-rate-limit-reset": "10"})) == 0.0  # Past.
    assert hook(httpx.Response(429)) is None
    assert hook(httpx.Response(429, headers={"x-rate-limit-reset": "soon"})) is None

    calls = iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Retry-After": "3", "x-rate-limit-reset": "1900"}
        return httpx.Response(next(calls), headers=headers)

    http, sleeps = client(handler)
    http.configure(reset_wait=hook)
    assert http.get("https://example.test").status_code == 200
    assert sleeps == [3.0]  # Retry-After takes precedence over the reset hook.


def test_reset_wait_beyond_cap_raises_and_cap_is_configurable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"X-Ratelimit-Reset": "600"})

    http, sleeps = client(handler, max_retry_wait=300.0)
    http.configure(reset_wait=reset_in_seconds("X-Ratelimit-Reset"))
    with pytest.raises(RateLimited) as info:
        http.get("https://example.test")
    assert info.value.retry_after == 600
    assert sleeps == []

    calls = iter([429, 200])
    http2, sleeps2 = client(
        lambda r: httpx.Response(next(calls), headers={"X-Ratelimit-Reset": "600"})
    )
    http2.configure(reset_wait=reset_in_seconds("X-Ratelimit-Reset"))
    assert http2.max_retry_wait == DEFAULT_MAX_RETRY_WAIT == 900.0
    assert http2.get("https://example.test").status_code == 200
    assert sleeps2 == [600.0]


def test_reset_hook_returning_none_falls_back_to_backoff() -> None:
    calls = iter([429, 200])
    http, sleeps = client(lambda r: httpx.Response(next(calls)), backoff_base=2.0)
    http.configure(reset_wait=lambda response: None)
    assert http.get("https://example.test").status_code == 200
    assert sleeps == [2.0]


def test_long_waits_are_logged_at_info(caplog: pytest.LogCaptureFixture) -> None:
    calls = iter([429, 429, 200])
    waits = iter(["4", "30"])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(calls)
        return httpx.Response(status, headers={"Retry-After": next(waits)} if status == 429 else {})

    http, sleeps = client(handler)
    with caplog.at_level(logging.INFO, logger="bot_farm_silence_tracker.sources.http"):
        http.get("https://example.test")
    assert sleeps == [4.0, 30.0]
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(messages) == 1 and "30s" in messages[0]  # Only the wait over 5s is logged.


@pytest.mark.parametrize("status", [402, 429])
def test_classifier_maps_usage_cap_to_budget_exceeded(status: int) -> None:
    def classify(response: httpx.Response) -> SourceError | None:
        if "UsageCapExceeded" in response.text:
            return BudgetExceeded("usage cap")
        return None

    http, sleeps = client(lambda r: httpx.Response(status, json={"title": "UsageCapExceeded"}))
    http.configure(classify_error=classify)
    with pytest.raises(BudgetExceeded):
        http.get("https://example.test")
    assert sleeps == [] and http.requests_made == 1

    # A body the classifier does not recognize falls through to the default handling.
    other, _ = client(lambda r: httpx.Response(status, json={"title": "Too Many Requests"}))
    other.configure(classify_error=classify)
    if status == 429:
        with pytest.raises(RateLimited):
            other.get("https://example.test")
    else:
        assert other.get("https://example.test").status_code == 402


def test_before_attempt_runs_for_every_attempt_client_hook_first() -> None:
    calls = iter([503, 429, 200])
    seen: list[tuple[str, int]] = []
    http, _ = client(lambda r: httpx.Response(next(calls)))
    http.configure(before_attempt=lambda a: seen.append(("client", a.attempt)))
    response = http.get(
        "https://example.test", before_attempt=lambda a: seen.append(("call", a.attempt))
    )
    assert response.status_code == 200
    assert seen == [
        ("client", 0),
        ("call", 0),
        ("client", 1),
        ("call", 1),
        ("client", 2),
        ("call", 2),
    ]


def test_before_attempt_can_refuse_a_retry() -> None:
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        return httpx.Response(503)

    budget = {"left": 2}

    def guard(attempt: Attempt) -> None:
        if budget["left"] <= 0:
            raise BudgetExceeded("usd cap")
        budget["left"] -= 1

    http, _ = client(handler)
    with pytest.raises(BudgetExceeded):
        http.get("https://example.test", before_attempt=guard)
    assert len(sent) == 2  # The third attempt, a retry, was refused before sending.
    assert http.requests_made == 2  # A refused attempt is not counted.


def test_request_budget_is_checked_before_the_attempt_hook() -> None:
    charged: list[int] = []
    http, _ = client(lambda r: httpx.Response(200), max_requests=1)
    http.get("https://example.test", before_attempt=lambda a: charged.append(a.attempt))
    with pytest.raises(BudgetExceeded):
        http.get("https://example.test", before_attempt=lambda a: charged.append(a.attempt))
    assert charged == [0]


def test_on_attempt_failed_only_for_errors_after_sending() -> None:
    failures: list[tuple[int, str]] = []

    def record(attempt: Attempt, error: httpx.TransportError) -> None:
        failures.append((attempt.attempt, type(error).__name__))

    def read_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    http, _ = client(read_timeout, max_retries=1)
    http.configure(on_attempt_failed=record)
    with pytest.raises(SourceError):
        http.get("https://example.test")
    assert failures == [(0, "ReadTimeout"), (1, "ReadTimeout")]

    failures.clear()

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http2, _ = client(refused, max_retries=1)
    with pytest.raises(SourceError):
        http2.get("https://example.test", on_attempt_failed=record)
    assert failures == []  # Never reached the server, so there is nothing to charge.


def test_on_response_sees_retried_responses() -> None:
    calls = iter([503, 200])
    seen: list[int] = []
    http, _ = client(lambda r: httpx.Response(next(calls)))
    http.get("https://example.test", on_response=lambda a, r: seen.append(r.status_code))
    assert seen == [503, 200]


def test_set_rate_replaces_the_single_pacer() -> None:
    http = HttpClient(requests_per_second=1.0, burst=1.0, unrestricted=True)
    assert http.rate == (1.0, 1.0)
    http.set_rate(100 / 60, burst=10)
    assert http.rate == (pytest.approx(100 / 60), 10.0)
    http.set_rate(2.0)
    assert http.rate == (2.0, 10.0)
