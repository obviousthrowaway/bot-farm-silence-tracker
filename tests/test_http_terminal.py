"""Every attempt gets exactly one terminal callback; httpx errors map to SourceError;
reset-header waits and the total wait budget."""

from __future__ import annotations

import httpx
import pytest

from bot_farm_silence_tracker.sources.errors import BudgetExceeded, RateLimited, SourceError
from bot_farm_silence_tracker.sources.http import (
    DEFAULT_USER_AGENT,
    PROJECT_URL,
    Attempt,
    HttpClient,
    default_user_agent,
    reset_at_epoch,
)

from .test_http import client


def _recording(http: HttpClient, log: list[tuple[str, int]], prefix: str = "") -> None:
    http.configure(
        before_attempt=lambda a: log.append((prefix + "reserve", a.attempt)),
        on_response=lambda a, r: log.append((prefix + "response", a.attempt)),
        on_attempt_failed=lambda a, e: log.append((prefix + "failed", a.attempt)),
        on_attempt_not_sent=lambda a, e: log.append((prefix + "not_sent", a.attempt)),
    )


def _terminals(log: list[tuple[str, int]], attempt: int) -> list[str]:
    return [k for k, n in log if n == attempt and k != "reserve"]


def test_connect_errors_release_every_reservation() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    http, _ = client(down, max_retries=2)
    log: list[tuple[str, int]] = []
    _recording(http, log)
    with pytest.raises(SourceError, match="transport error"):
        http.get("https://api.example.test/x")
    assert log == [
        ("reserve", 0), ("not_sent", 0),
        ("reserve", 1), ("not_sent", 1),
        ("reserve", 2), ("not_sent", 2),
    ]  # fmt: skip


def test_failing_second_before_hook_releases_the_first_reservation() -> None:
    sent: list[str] = []

    def ok(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200)

    http, _ = client(ok)
    log: list[tuple[str, int]] = []
    _recording(http, log)
    per_call_not_sent: list[BaseException] = []

    def refuse(attempt: Attempt) -> None:
        raise BudgetExceeded("per-call cap")

    with pytest.raises(BudgetExceeded, match="per-call cap"):
        http.get(
            "https://api.example.test/x",
            before_attempt=refuse,
            on_attempt_not_sent=lambda a, e: per_call_not_sent.append(e),
        )
    assert log == [("reserve", 0), ("not_sent", 0)]  # Client-wide reservation released.
    assert per_call_not_sent == []  # Its own before hook never finished.
    assert sent == []


def test_failing_first_before_hook_gets_no_terminal_callback() -> None:
    http, _ = client(lambda r: httpx.Response(200))
    released: list[int] = []

    def refuse(attempt: Attempt) -> None:
        raise BudgetExceeded("client cap")

    http.configure(before_attempt=refuse, on_attempt_not_sent=lambda a, e: released.append(1))
    with pytest.raises(BudgetExceeded):
        http.get("https://api.example.test/x")
    assert released == []


def test_each_attempt_gets_exactly_one_terminal_callback() -> None:
    script = iter(["connect", "read", 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        step = next(script)
        if step == "connect":
            raise httpx.ConnectError("refused", request=request)
        if step == "read":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(step)  # type: ignore[arg-type]

    http, _ = client(handler, max_retries=5)
    log: list[tuple[str, int]] = []
    _recording(http, log)
    assert http.get("https://api.example.test/x").status_code == 200
    assert [_terminals(log, n) for n in range(4)] == [
        ["not_sent"],
        ["failed"],
        ["response"],
        ["response"],
    ]


def test_per_call_terminal_hooks_run_after_client_hooks() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    http, _ = client(down, max_retries=0)
    log: list[tuple[str, int]] = []
    _recording(http, log)
    with pytest.raises(SourceError):
        http.get(
            "https://api.example.test/x",
            before_attempt=lambda a: log.append(("call_reserve", a.attempt)),
            on_attempt_not_sent=lambda a, e: log.append(("call_not_sent", a.attempt)),
        )
    assert log == [("reserve", 0), ("call_reserve", 0), ("not_sent", 0), ("call_not_sent", 0)]


def test_a_raising_terminal_hook_does_not_skip_the_others() -> None:
    http, _ = client(lambda r: httpx.Response(200))
    seen: list[str] = []

    def broken(attempt: Attempt, response: httpx.Response) -> None:
        seen.append("client")
        raise RuntimeError("hook bug")

    http.configure(on_response=broken)
    with pytest.raises(RuntimeError, match="hook bug"):
        http.get("https://api.example.test/x", on_response=lambda a, r: seen.append("call"))
    assert seen == ["client", "call"]


@pytest.mark.parametrize(
    "error",
    [
        httpx.DecodingError("bad gzip"),
        httpx.TooManyRedirects("loop"),
    ],
)
def test_other_httpx_errors_become_source_errors_and_charge(error: httpx.HTTPError) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise error

    http, _ = client(handler, max_retries=3)
    log: list[tuple[str, int]] = []
    _recording(http, log)
    with pytest.raises(SourceError, match="HTTP error") as info:
        http.get_json("https://api.example.test/x?token=s3cret")
    assert "s3cret" not in str(info.value)
    assert log == [("reserve", 0), ("failed", 0)]  # Sent, so charged; not retried.
    assert len(calls) == 1


def test_undecodable_gzip_body_is_a_source_error() -> None:
    http, _ = client(
        lambda r: httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"notgzip")
    )
    log: list[tuple[str, int]] = []
    _recording(http, log)
    with pytest.raises(SourceError):
        http.get_json("https://api.example.test/x")
    assert log == [("reserve", 0), ("failed", 0)]


def test_invalid_url_is_not_sent_and_maps_to_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("bad url")

    http, _ = client(handler)
    log: list[tuple[str, int]] = []
    _recording(http, log)
    with pytest.raises(SourceError, match="invalid URL"):
        http.get("https://api.example.test/x")
    assert log == [("reserve", 0), ("not_sent", 0)]


def test_interrupt_mid_send_is_reported_as_failed_and_reraised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    http, _ = client(handler)
    errors: list[BaseException] = []
    http.configure(on_attempt_failed=lambda a, e: errors.append(e))
    with pytest.raises(KeyboardInterrupt):
        http.get("https://api.example.test/x")
    assert len(errors) == 1 and isinstance(errors[0], httpx.RequestError)


def test_reset_at_the_cap_plus_margin_is_waited_for() -> None:
    now = 1_000_000.0
    calls = iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(calls), headers={"x-rate-limit-reset": str(int(now + 900))})

    http, sleeps = client(handler)
    http.configure(reset_wait=reset_at_epoch("x-rate-limit-reset", clock=lambda: now))
    assert http.get("https://api.example.test/x").status_code == 200
    assert sleeps == [901.0]
    # Beyond the cap plus the margin it still refuses.
    late, _ = client(lambda r: httpx.Response(429, headers={"x-rate-limit-reset": str(now + 905)}))
    late.configure(reset_wait=reset_at_epoch("x-rate-limit-reset", clock=lambda: now))
    with pytest.raises(RateLimited, match="long wait"):
        late.get("https://api.example.test/x")


def test_reset_header_waits_do_not_count_toward_the_total_budget() -> None:
    now = 1_000_000.0
    calls = iter([429, 429, 429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(calls), headers={"x-rate-limit-reset": str(int(now + 600))})

    http, sleeps = client(handler, max_total_wait=10.0, max_reset_wait_total=3600.0)
    http.configure(reset_wait=reset_at_epoch("x-rate-limit-reset", clock=lambda: now))
    assert http.get("https://api.example.test/x").status_code == 200
    assert sleeps == [601.0, 601.0, 601.0]


def test_user_agent_names_the_repo_and_the_rules() -> None:
    assert default_user_agent(PROJECT_URL) == DEFAULT_USER_AGENT
    assert "no scraping" in DEFAULT_USER_AGENT
    published = default_user_agent("https://github.com/example-owner/bot-farm-silence-tracker")
    assert published.endswith(
        "no scraping; https://github.com/example-owner/bot-farm-silence-tracker)"
    )
