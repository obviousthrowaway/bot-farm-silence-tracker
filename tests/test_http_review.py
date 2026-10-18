"""HTTP client review fixes: status mapping, wait caps, not-sent errors, URL redaction."""

from __future__ import annotations

import httpx
import pytest

from bot_farm_silence_tracker.sources.errors import RateLimited, SourceAuthError, SourceError
from bot_farm_silence_tracker.sources.http import (
    DEFAULT_MAX_TOTAL_WAIT,
    NOT_SENT_ERRORS,
    HttpClient,
    redact_url,
)

from .test_http import client

SECRET_URL = "https://api.example.test/2/users?token=s3cret&q=x#frag"


@pytest.mark.parametrize("status", [401, 403])
def test_get_json_maps_auth_statuses(status: int) -> None:
    http, _ = client(lambda r: httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(SourceAuthError, match=str(status)) as info:
        http.get_json(SECRET_URL)
    assert "s3cret" not in str(info.value)


@pytest.mark.parametrize("status", [400, 404, 410, 422])
def test_get_json_maps_other_4xx_to_source_error(status: int) -> None:
    http, _ = client(lambda r: httpx.Response(status, json={}))
    with pytest.raises(SourceError) as info:
        http.get_json(SECRET_URL)
    assert not isinstance(info.value, SourceAuthError)
    assert f"HTTP {status}" in str(info.value) and "s3cret" not in str(info.value)


def test_get_json_maps_redirects_and_bad_bodies() -> None:
    redirect, _ = client(lambda r: httpx.Response(302, headers={"Location": "https://x.test/"}))
    with pytest.raises(SourceError, match="redirect"):
        redirect.get_json("https://example.test/a")
    bad_json, _ = client(lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(SourceError, match="invalid JSON"):
        bad_json.get_json("https://example.test/a")
    bad_bytes, _ = client(
        lambda r: httpx.Response(
            200, content=b"\xff\xfe\xfa", headers={"Content-Type": "application/json"}
        )
    )
    with pytest.raises(SourceError):
        bad_bytes.get_json("https://example.test/a")
    # Never a raw httpx error.
    for http in (redirect, bad_json, bad_bytes):
        try:
            http.get_json("https://example.test/a")
        except SourceError:
            pass
        except httpx.HTTPError as exc:  # pragma: no cover - the bug being guarded
            pytest.fail(f"raw httpx error escaped: {exc!r}")


def test_5xx_retry_after_over_cap_raises_rate_limited() -> None:
    http, sleeps = client(
        lambda r: httpx.Response(503, headers={"Retry-After": "3600"}), max_retry_wait=300.0
    )
    with pytest.raises(RateLimited) as info:
        http.get(SECRET_URL)
    assert info.value.retry_after == 3600
    assert sleeps == [] and http.requests_made == 1
    assert "api.example.test/2/users" in str(info.value) and "s3cret" not in str(info.value)


def test_5xx_retry_after_under_cap_is_honored() -> None:
    calls = iter([503, 200])
    http, sleeps = client(
        lambda r: httpx.Response(next(calls), headers={"Retry-After": "7"}), max_retry_wait=300.0
    )
    assert http.get("https://example.test").status_code == 200
    assert sleeps == [7.0]


def test_total_wait_budget_per_request() -> None:
    assert DEFAULT_MAX_TOTAL_WAIT == 1800.0
    assert HttpClient().max_total_wait == 1800.0
    http, sleeps = client(
        lambda r: httpx.Response(429, headers={"Retry-After": "400"}), max_retries=10
    )
    with pytest.raises(RateLimited, match="wait budget") as info:
        http.get(SECRET_URL)
    assert sleeps == [400.0] * 4  # A fifth wait would pass 1800s.
    assert "s3cret" not in str(info.value)

    # The budget is per request: a new request starts from zero.
    calls = iter([429, 200, 429, 200])
    ok, ok_sleeps = client(
        lambda r: httpx.Response(next(calls), headers={"Retry-After": "500"}),
    )
    assert ok.get("https://example.test").status_code == 200
    assert ok.get("https://example.test").status_code == 200
    assert ok_sleeps == [500.0, 500.0]


def test_total_wait_budget_covers_backoff_and_is_configurable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    http, sleeps = client(boom, max_retries=10, backoff_base=4.0, max_total_wait=10.0)
    with pytest.raises(RateLimited):
        http.get("https://example.test")
    assert sleeps == [4.0]  # 4 + 8 > 10.


def test_local_protocol_error_is_not_sent() -> None:
    assert httpx.LocalProtocolError in NOT_SENT_ERRORS
    failed: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.LocalProtocolError("bad header")

    http, _ = client(handler, max_retries=0)
    http.configure(on_attempt_failed=lambda attempt, exc: failed.append(exc))
    with pytest.raises(SourceError):
        http.get("https://example.test")
    assert failed == []  # Not charged: nothing reached the server.


def test_rate_limited_messages_include_redacted_url() -> None:
    http, _ = client(lambda r: httpx.Response(429), max_retries=0)
    with pytest.raises(RateLimited) as info:
        http.get(SECRET_URL)
    text = str(info.value)
    assert "https://api.example.test/2/users" in text
    assert "token" not in text and "frag" not in text
    assert redact_url(SECRET_URL) == "https://api.example.test/2/users"
    assert "x=1" not in redact_url("not a url?x=1")
