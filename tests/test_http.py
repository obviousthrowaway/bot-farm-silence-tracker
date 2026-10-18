from __future__ import annotations

import httpx
import pytest

from bot_farm_silence_tracker.sources.errors import BudgetExceeded, RateLimited, SourceError
from bot_farm_silence_tracker.sources.http import HttpClient, TokenBucket, parse_retry_after


def client(handler, **kw) -> tuple[HttpClient, list[float]]:  # type: ignore[no-untyped-def]
    sleeps: list[float] = []
    kw.setdefault("requests_per_second", 1000.0)
    kw.setdefault("burst", 1000.0)
    # No adapter installs an allowlist in these tests of the client itself.
    kw.setdefault("unrestricted", True)
    http = HttpClient(
        transport=httpx.MockTransport(handler), sleep=sleeps.append, clock=lambda: 0.0, **kw
    )
    return http, sleeps


def test_user_agent_is_sent() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, json={"ok": True})

    http, _ = client(handler)
    assert http.get_json("https://example.test/x") == {"ok": True}
    assert seen[0].startswith("bot-farm-silence-tracker/")


def test_retries_429_honoring_retry_after() -> None:
    calls = iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(calls)
        return httpx.Response(status, headers={"Retry-After": "2"} if status == 429 else {})

    http, sleeps = client(handler)
    assert http.get("https://example.test").status_code == 200
    assert 2.0 in sleeps
    assert http.requests_made == 2  # Retries count against the budget.


def test_backoff_on_5xx_then_success() -> None:
    calls = iter([503, 502, 200])
    http, sleeps = client(lambda r: httpx.Response(next(calls)), backoff_base=1.0)
    assert http.get("https://example.test").status_code == 200
    assert sleeps == [1.0, 2.0]


def test_gives_up_on_persistent_429_and_5xx() -> None:
    http, _ = client(lambda r: httpx.Response(429), max_retries=2)
    with pytest.raises(RateLimited):
        http.get("https://example.test")
    assert http.requests_made == 3
    http2, _ = client(lambda r: httpx.Response(500), max_retries=1)
    with pytest.raises(SourceError):
        http2.get("https://example.test")


def test_long_retry_after_raises_instead_of_blocking() -> None:
    http, sleeps = client(lambda r: httpx.Response(429, headers={"Retry-After": "1800"}))
    with pytest.raises(RateLimited) as info:
        http.get("https://example.test")
    assert info.value.retry_after == 1800
    assert sleeps == []


def test_client_errors_are_returned_not_retried() -> None:
    http, _ = client(lambda r: httpx.Response(404))
    assert http.get("https://example.test").status_code == 404
    assert http.requests_made == 1


def test_budget_guard() -> None:
    http, _ = client(lambda r: httpx.Response(200), max_requests=2)
    http.get("https://example.test")
    http.get("https://example.test")
    assert http.requests_remaining == 0
    with pytest.raises(BudgetExceeded):
        http.get("https://example.test")
    with pytest.raises(BudgetExceeded):
        http.spend()
    assert http.requests_made == 2


def test_transport_errors_retry_then_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    http, sleeps = client(handler, max_retries=2)
    with pytest.raises(SourceError):
        http.get("https://example.test")
    assert len(sleeps) == 2


def test_token_bucket_sleeps_when_empty() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    bucket = TokenBucket(rate=2.0, capacity=1.0, clock=lambda: now[0], sleep=sleep)
    bucket.acquire()  # Uses the initial token.
    bucket.acquire()  # Must wait 0.5s for the next token.
    assert sleeps == [pytest.approx(0.5)]


def test_parse_retry_after() -> None:
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
