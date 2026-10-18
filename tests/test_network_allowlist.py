"""The host allowlist, credential host binding and the ``network`` command.

No network: every request goes through ``httpx.MockTransport``, and the tests assert
that a refused request never reaches the transport.
"""

from __future__ import annotations

import base64
import contextlib
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from bot_farm_silence_tracker.cli import app
from bot_farm_silence_tracker.models import Account
from bot_farm_silence_tracker.network import network_report
from bot_farm_silence_tracker.sources import bluesky, reddit, reddit_archive, x
from bot_farm_silence_tracker.sources.base import Source, SourceConfig
from bot_farm_silence_tracker.sources.errors import DisallowedHostError
from bot_farm_silence_tracker.sources.http import Attempt, HttpClient
from bot_farm_silence_tracker.sources.registry import SOURCE_REGISTRY, load_source_class

from .helpers import CUTOFF, NOW

Handler = Callable[[httpx.Request], httpx.Response]

REDDIT_CREDS = {
    reddit.CLIENT_ID_ENV: "client-id-SECRET-1",
    reddit.CLIENT_SECRET_ENV: "client-secret-SECRET-2",
    reddit.USERNAME_ENV: "researcher-SECRET-3",
    reddit.PASSWORD_ENV: "password-SECRET-4",
}
X_CREDS = {x.BEARER_ENV: "bearer-SECRET-5"}
BSKY_CREDS = {bluesky.HANDLE_ENV: "handle-SECRET-6.example", bluesky.PASSWORD_ENV: "pw-SECRET-7"}

#: The hosts each platform must be allowed to contact, with its default options.
EXPECTED_HOSTS = {
    "reddit-archive": {"arctic-shift.photon-reddit.com"},
    "bluesky": {"public.api.bsky.app", "bsky.social"},
    "x": {"api.x.com"},
    "reddit": {"oauth.reddit.com", "www.reddit.com"},
    "fake": set(),
}

#: The real endpoints each adapter sends to, from its own constants.
REAL_URLS = {
    "reddit-archive": [reddit_archive.BASE_URL + "/api/time_series"],
    "bluesky": [bluesky.PUBLIC_APPVIEW + "/xrpc/" + bluesky.GET_PROFILE],
    "x": [x.API_BASE + "/users/1"],
    "reddit": [reddit.API_BASE + "/r/test/about"],
}
REAL_CREDENTIAL_URLS = {
    "bluesky": [bluesky.DEFAULT_PDS + "/xrpc/" + bluesky.CREATE_SESSION],
    "x": [x.API_BASE + "/users/1"],
    "reddit": [reddit.TOKEN_URL, reddit.API_BASE + "/r/test/about"],
}
CREDS = {"reddit": REDDIT_CREDS, "x": X_CREDS, "bluesky": BSKY_CREDS}


class Recorder:
    """A mock transport handler that records every request it receives."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.handler = handler or (lambda _r: httpx.Response(200, json={}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def make_http(recorder: Recorder, **kw: Any) -> HttpClient:
    return HttpClient(
        transport=httpx.MockTransport(recorder),
        sleep=lambda _s: None,
        clock=lambda: 0.0,
        requests_per_second=kw.pop("requests_per_second", 1000.0),
        burst=kw.pop("burst", 1000.0),
        max_retries=kw.pop("max_retries", 0),
        **kw,
    )


def make_source(
    platform: str,
    recorder: Recorder,
    *,
    credentials: dict[str, str] | None = None,
    **options: Any,
) -> Source:
    cls = load_source_class(platform)
    if platform == "x":
        options.setdefault("max_usd", "10")
    config = SourceConfig(
        http=make_http(recorder, requests_per_second=cls.default_rps, burst=cls.default_burst),
        credentials=CREDS.get(platform, {}) if credentials is None else credentials,
        now=NOW,
        cutoff=CUTOFF,
        options=options,
    )
    return cls(config)


def request_text(request: httpx.Request) -> str:
    """Everything a request carries, with HTTP Basic credentials decoded."""
    parts = [str(request.url)]
    for key, value in request.headers.items():
        parts.append(f"{key}: {value}")
        if value.lower().startswith("basic "):
            parts.append(base64.b64decode(value[6:]).decode())
    parts.append(request.content.decode(errors="replace"))
    return "\n".join(parts)


# -- every adapter's real endpoints pass --------------------------------------------------


@pytest.mark.parametrize("platform", sorted(SOURCE_REGISTRY))
def test_declared_hosts(platform: str) -> None:
    cls = load_source_class(platform)
    assert cls.allowed_hosts == EXPECTED_HOSTS[platform]


@pytest.mark.parametrize("platform", sorted(SOURCE_REGISTRY))
def test_source_installs_its_allowlist(platform: str) -> None:
    source = make_source(platform, Recorder())
    assert source.http.allowed_hosts == EXPECTED_HOSTS[platform]
    expected_credential_hosts = {
        u.host for u in type(source).network_hosts if u.receives_credentials
    }
    assert source.http.credential_hosts == expected_credential_hosts


@pytest.mark.parametrize("platform", sorted(REAL_URLS))
def test_real_endpoints_are_sent(platform: str) -> None:
    recorder = Recorder()
    source = make_source(platform, recorder)
    for url in REAL_URLS[platform]:
        source.http.request("GET", url, sends_credentials=False)
    for url in REAL_CREDENTIAL_URLS.get(platform, []):
        source.http.request(
            "GET", url, headers={"Authorization": "Bearer t"}, sends_credentials=True
        )
    sent = [str(r.url) for r in recorder.requests]
    assert sent == REAL_URLS[platform] + REAL_CREDENTIAL_URLS.get(platform, [])


# -- off-list hosts, http and redirects are refused ---------------------------------------


@pytest.mark.parametrize("platform", sorted(SOURCE_REGISTRY))
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/collect",
        "https://api.x.com.evil.example/",
        "https://sub.oauth.reddit.com/",
        "https://reddit.com/",
        "https://127.0.0.1/",
    ],
)
def test_off_list_host_is_refused_without_sending(platform: str, url: str) -> None:
    recorder = Recorder()
    source = make_source(platform, recorder)
    hooked: list[Attempt] = []
    with pytest.raises(DisallowedHostError, match="not on this platform's host allowlist"):
        source.http.request("GET", url, before_attempt=hooked.append, sends_credentials=False)
    assert recorder.requests == []
    assert hooked == []
    assert source.http.requests_made == 0


def test_fake_source_may_contact_nothing() -> None:
    recorder = Recorder()
    source = make_source("fake", recorder)
    for url in ("https://arctic-shift.photon-reddit.com/", "https://api.x.com/"):
        with pytest.raises(DisallowedHostError):
            source.http.get(url, sends_credentials=False)
    assert recorder.requests == []


@pytest.mark.parametrize("platform", sorted(REAL_URLS))
def test_plain_http_is_refused(platform: str) -> None:
    recorder = Recorder()
    source = make_source(platform, recorder)
    for url in REAL_URLS[platform]:
        insecure = "http://" + url.removeprefix("https://")
        with pytest.raises(DisallowedHostError, match="only https"):
            source.http.request("GET", insecure, sends_credentials=False)
    assert recorder.requests == []


def test_plain_http_is_refused_without_an_allowlist_too() -> None:
    recorder = Recorder()
    http = make_http(recorder)
    assert http.allowed_hosts is None
    with pytest.raises(DisallowedHostError, match="only https"):
        http.get("http://example.test/")
    with pytest.raises(DisallowedHostError, match="only https"):
        http.get("ftp://example.test/")
    assert recorder.requests == []


def test_insecure_http_flag_is_for_tests_only() -> None:
    recorder = Recorder()
    http = make_http(recorder, allow_insecure_http=True, allowed_hosts={"127.0.0.1"})
    http.get("http://127.0.0.1/ok", sends_credentials=False)
    with pytest.raises(DisallowedHostError):
        http.get("http://localhost/no", sends_credentials=False)
    assert [str(r.url) for r in recorder.requests] == ["http://127.0.0.1/ok"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_to_off_list_host_is_refused(status: int) -> None:
    recorder = Recorder(
        lambda _r: httpx.Response(status, headers={"Location": "https://evil.example/steal"})
    )
    source = make_source("reddit-archive", recorder)
    with pytest.raises(DisallowedHostError, match="redirected"):
        source.http.get(reddit_archive.BASE_URL + "/api/users/search", sends_credentials=False)
    assert [r.url.host for r in recorder.requests] == ["arctic-shift.photon-reddit.com"]


def test_redirect_to_http_is_refused() -> None:
    recorder = Recorder(
        lambda _r: httpx.Response(
            302, headers={"Location": "http://arctic-shift.photon-reddit.com/x"}
        )
    )
    source = make_source("reddit-archive", recorder)
    with pytest.raises(DisallowedHostError, match="only https"):
        source.http.get(reddit_archive.BASE_URL + "/api/users/search", sends_credentials=False)
    assert len(recorder.requests) == 1


def test_redirect_on_list_is_not_followed() -> None:
    recorder = Recorder(lambda _r: httpx.Response(302, headers={"Location": "/elsewhere"}))
    source = make_source("reddit-archive", recorder)
    response = source.http.get(
        reddit_archive.BASE_URL + "/api/users/search", sends_credentials=False
    )
    assert response.status_code == 302
    assert len(recorder.requests) == 1


def test_every_attempt_is_checked_retries_included() -> None:
    """A retry is checked again before it is sent (here the allowlist narrows mid-request)."""
    recorder = Recorder(lambda _r: httpx.Response(503))
    http = make_http(recorder, max_retries=3, allowed_hosts={"api.example.test"})

    def narrow(_attempt: Attempt, _response: httpx.Response) -> None:
        http.restrict_hosts(set())

    with pytest.raises(DisallowedHostError):
        http.get("https://api.example.test/x", on_response=narrow, sends_credentials=False)
    assert len(recorder.requests) == 1


def test_allowlist_can_only_narrow() -> None:
    http = make_http(Recorder(), allowed_hosts={"a.example"})
    http.restrict_hosts({"a.example", "b.example"})
    assert http.allowed_hosts == {"a.example"}
    with pytest.raises(ValueError, match="credential hosts"):
        http.restrict_hosts({"a.example"}, credential_hosts={"c.example"})


def test_host_comparison_ignores_case_only() -> None:
    recorder = Recorder()
    source = make_source("x", recorder)
    source.http.get(
        "https://API.X.COM/2/users/1", headers={"Authorization": "Bearer t"}, sends_credentials=True
    )
    with pytest.raises(DisallowedHostError):
        source.http.get("https://api.x.com.:443/2/users/1", sends_credentials=True)
    assert len(recorder.requests) == 1


# -- Bluesky pds_url -----------------------------------------------------------------------


def test_bluesky_logged_out_contacts_only_the_public_appview() -> None:
    source = make_source("bluesky", Recorder(), credentials={})
    assert source.http.allowed_hosts == {"public.api.bsky.app"}
    source = make_source("bluesky", Recorder(), credentials={}, pds_url="https://pds.example.org")
    assert source.http.allowed_hosts == {"public.api.bsky.app"}


def test_bluesky_pds_url_host_is_allowed_only_when_set() -> None:
    source = make_source("bluesky", Recorder())
    assert source.http.allowed_hosts == {"public.api.bsky.app", "bsky.social"}
    assert source.http.credential_hosts == {"bsky.social"}

    recorder = Recorder()
    source = make_source("bluesky", recorder, pds_url="https://pds.example.org/")
    assert source.http.allowed_hosts == {"public.api.bsky.app", "pds.example.org"}
    assert source.http.credential_hosts == {"pds.example.org"}
    with pytest.raises(DisallowedHostError):
        source.http.request(
            "POST", bluesky.DEFAULT_PDS + "/xrpc/" + bluesky.CREATE_SESSION, sends_credentials=True
        )
    assert recorder.requests == []


@pytest.mark.parametrize(
    "pds_url", ["http://pds.example.org", "pds.example.org", "https://pds.example.org/path"]
)
def test_bluesky_pds_url_must_be_an_https_origin(pds_url: str) -> None:
    with pytest.raises(ValueError, match="https:// origin"):
        make_source("bluesky", Recorder(), pds_url=pds_url)


# -- credentials go only to their hosts ---------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"headers": {"Authorization": "Bearer secret"}},
        {"headers": {"authorization": "Bearer secret"}},
        {"auth": ("id", "secret")},
        {"sends_credentials": True},
    ],
)
def test_credentials_refused_for_a_host_that_does_not_receive_them(kwargs: dict) -> None:
    recorder = Recorder()
    source = make_source("bluesky", recorder)
    with pytest.raises(DisallowedHostError, match="refused to send credentials"):
        source.http.request(
            "GET",
            bluesky.PUBLIC_APPVIEW + "/xrpc/" + bluesky.GET_PROFILE,
            **{"sends_credentials": False, **kwargs},
        )
    assert recorder.requests == []


def test_credentials_in_the_url_count_as_credentials() -> None:
    recorder = Recorder()
    source = make_source("reddit-archive", recorder)
    with pytest.raises(DisallowedHostError, match="refused to send credentials"):
        source.http.get(
            "https://user:pw@arctic-shift.photon-reddit.com/api", sends_credentials=False
        )
    assert recorder.requests == []


def test_client_wide_authorization_header_counts() -> None:
    recorder = Recorder()
    http = make_http(
        recorder,
        headers={"Authorization": "Bearer x"},
        allowed_hosts={"a.example", "b.example"},
        credential_hosts={"a.example"},
    )
    http.get("https://a.example/", sends_credentials=False)
    with pytest.raises(DisallowedHostError):
        http.get("https://b.example/", sends_credentials=False)
    assert len(recorder.requests) == 1


def _secrets_by_host(source: Source) -> dict[str, set[str]]:
    """Which hosts each secret may reach, from the adapter's declarations."""
    out: dict[str, set[str]] = {}
    for use in type(source).host_uses(source.config.options):
        for cred in use.credentials:
            out.setdefault(source.credentials[cred.env], set()).add(use.host)
    return out


def _assert_secrets_stay(recorder: Recorder, allowed: dict[str, set[str]]) -> None:
    assert recorder.requests
    for secret, hosts in allowed.items():
        for request in recorder.requests:
            if secret in request_text(request):
                assert request.url.host in hosts, (secret, request.url)
                assert request.url.scheme == "https"


def test_reddit_credentials_reach_only_their_hosts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == reddit.TOKEN_URL:
            return httpx.Response(200, json={"access_token": "ACCESS-SECRET-8", "expires_in": 3600})
        return httpx.Response(200, json={"data": {}})

    recorder = Recorder(handler)
    source = make_source("reddit", recorder)
    source._api_get("/r/test/about")  # type: ignore[attr-defined]
    hosts = [r.url.host for r in recorder.requests]
    assert hosts == ["www.reddit.com", "oauth.reddit.com"]
    allowed = _secrets_by_host(source)
    # The client secret and password never reach the API host, only the token endpoint.
    assert allowed[REDDIT_CREDS[reddit.CLIENT_SECRET_ENV]] == {"www.reddit.com"}
    assert allowed[REDDIT_CREDS[reddit.PASSWORD_ENV]] == {"www.reddit.com"}
    allowed["ACCESS-SECRET-8"] = {"oauth.reddit.com"}
    _assert_secrets_stay(recorder, allowed)
    assert REDDIT_CREDS[reddit.PASSWORD_ENV] in request_text(recorder.requests[0])
    assert "ACCESS-SECRET-8" in request_text(recorder.requests[1])


def test_x_bearer_reaches_only_the_api_host() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "1",
                    "username": "u1",
                    "created_at": "2020-01-02T03:04:05.000Z",
                    "public_metrics": {"tweet_count": 500},
                }
            },
        )

    recorder = Recorder(handler)
    source = make_source("x", recorder)
    with contextlib.suppress(Exception):  # Only the requests matter here.
        source.hydrate(Account("x", "1", "u1"))
    assert recorder.requests
    assert all(r.url.host == "api.x.com" for r in recorder.requests)
    _assert_secrets_stay(recorder, _secrets_by_host(source))


@pytest.mark.parametrize("pds_url", [None, "https://pds.example.org"])
def test_bluesky_login_refresh_and_tokens_reach_only_the_pds(pds_url: str | None) -> None:
    pds_host = "pds.example.org" if pds_url else "bsky.social"
    searches: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(bluesky.CREATE_SESSION):
            return httpx.Response(
                200, json={"accessJwt": "ACCESS-SECRET-9", "refreshJwt": "REFRESH-SECRET-10"}
            )
        if path.endswith("com.atproto.server.refreshSession"):
            return httpx.Response(
                200, json={"accessJwt": "ACCESS-SECRET-11", "refreshJwt": "REFRESH-SECRET-12"}
            )
        if path.endswith(bluesky.SEARCH_POSTS):
            searches.append(request)
            if len(searches) == 1:
                return httpx.Response(400, json={"error": "ExpiredToken", "message": "expired"})
            return httpx.Response(200, json={"posts": []})
        return httpx.Response(200, json={"did": "did:plc:a", "handle": "a.example"})

    recorder = Recorder(handler)
    options = {"pds_url": pds_url} if pds_url else {}
    source = make_source("bluesky", recorder, **options)
    source._call_authed(bluesky.SEARCH_POSTS, {"q": "x"}, phase="discovery")  # type: ignore[attr-defined]
    source._call(bluesky.GET_PROFILE, {"actor": "a.example"}, phase="hydrate")  # type: ignore[attr-defined]
    paths = [(r.url.host, r.url.path.rsplit("/", 1)[-1]) for r in recorder.requests]
    assert paths == [
        (pds_host, bluesky.CREATE_SESSION),
        (pds_host, bluesky.SEARCH_POSTS),
        (pds_host, "com.atproto.server.refreshSession"),
        (pds_host, bluesky.SEARCH_POSTS),
        ("public.api.bsky.app", bluesky.GET_PROFILE),
    ]
    allowed = _secrets_by_host(source)
    for token in ("ACCESS-SECRET-9", "REFRESH-SECRET-10", "ACCESS-SECRET-11"):
        allowed[token] = {pds_host}
    _assert_secrets_stay(recorder, allowed)
    assert "authorization" not in recorder.requests[-1].headers


# -- the network command --------------------------------------------------------------------


def test_network_report_matches_the_enforced_allowlist() -> None:
    report = network_report()
    assert [e["platform"] for e in report] == sorted(SOURCE_REGISTRY)
    for entry in report:
        cls = load_source_class(entry["platform"])
        assert set(entry["allowed_hosts"]) == cls.allowed_hosts
        assert entry["login"] == cls.login_requirement()
        assert entry["default_rate"]["burst"] == cls.default_burst
        for host in entry["hosts"]:
            assert host["scheme"] == "https"
    by_name = {e["platform"]: e for e in report}
    assert by_name["reddit"]["login"] == "required"
    assert by_name["bluesky"]["login"] == "optional"
    assert by_name["fake"]["login"] == "none"
    assert by_name["fake"]["hosts"] == []
    token_host = next(h for h in by_name["reddit"]["hosts"] if h["host"] == "www.reddit.com")
    assert {c["env"] for c in token_host["credentials"]} == set(REDDIT_CREDS)
    api_host = next(h for h in by_name["reddit"]["hosts"] if h["host"] == "oauth.reddit.com")
    assert {c["env"] for c in api_host["credentials"]} == {reddit.USERNAME_ENV}


def test_network_command_text(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {**REDDIT_CREDS, **X_CREDS, **BSKY_CREDS}.items():
        monkeypatch.setenv(name, value)
    result = CliRunner().invoke(app, ["network"])
    assert result.exit_code == 0, result.output
    for hosts in EXPECTED_HOSTS.values():
        for host in hosts:
            assert f"https://{host}" in result.output
    assert x.BEARER_ENV in result.output
    assert "[only with a login]" in result.output
    for value in {**REDDIT_CREDS, **X_CREDS, **BSKY_CREDS}.values():
        assert value not in result.output


def test_network_command_json_for_one_platform() -> None:
    result = CliRunner().invoke(
        app,
        [
            "network",
            "--platform",
            "bluesky",
            "--source-option",
            "pds_url=https://pds.example.org",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [e["platform"] for e in data] == ["bluesky"]
    assert data[0]["allowed_hosts"] == ["pds.example.org", "public.api.bsky.app"]
    pds = next(h for h in data[0]["hosts"] if h["host"] == "pds.example.org")
    assert pds["login_only"] is True
    assert {c["env"] for c in pds["credentials"]} == set(BSKY_CREDS)
    # Only the app password is a secret bound to the PDS; the handle is public.
    assert {c["env"]: c["secret"] for c in pds["credentials"]} == {
        bluesky.HANDLE_ENV: False,
        bluesky.PASSWORD_ENV: True,
    }


@pytest.mark.parametrize(
    "args",
    [
        ["--platform", "myspace"],
        ["--platform", "bluesky", "--source-option", "pds_url=http://pds.example.org"],
    ],
)
def test_network_command_errors(args: list[str]) -> None:
    result = CliRunner().invoke(app, ["network", *args])
    assert result.exit_code == 2


# -- a client without an allowlist refuses everything (unless unrestricted) ---------------


def test_a_client_without_an_allowlist_refuses_every_request() -> None:
    recorder = Recorder()
    http = make_http(recorder)
    assert http.allowed_hosts is None and not http.unrestricted
    with pytest.raises(DisallowedHostError, match="no host allowlist is installed"):
        http.get("https://api.example.test/x")
    assert recorder.requests == []


def test_unrestricted_is_an_explicit_opt_in_that_an_allowlist_ends() -> None:
    recorder = Recorder()
    http = make_http(recorder, unrestricted=True)
    assert http.unrestricted
    http.get("https://api.example.test/x")
    http.restrict_hosts({"api.example.test"})
    assert not http.unrestricted
    with pytest.raises(DisallowedHostError, match="not on this platform's host allowlist"):
        http.get("https://other.example.test/x", sends_credentials=False)
    assert [r.url.host for r in recorder.requests] == ["api.example.test"]


# -- only the default port --------------------------------------------------------------


@pytest.mark.parametrize("port", [8443, 80, 444, 1])
def test_a_non_default_port_is_refused(port: int) -> None:
    recorder = Recorder()
    source = make_source("x", recorder)
    with pytest.raises(DisallowedHostError, match="only the default port 443"):
        source.http.get(f"https://api.x.com:{port}/2/users/1", sends_credentials=True)
    unrestricted = make_http(recorder, unrestricted=True)
    with pytest.raises(DisallowedHostError, match="only the default port"):
        unrestricted.get(f"https://api.example.test:{port}/")
    assert recorder.requests == []


def test_the_explicit_default_port_is_the_same_origin() -> None:
    recorder = Recorder()
    source = make_source("x", recorder)
    source.http.get("https://api.x.com:443/2/users/1", sends_credentials=True)
    assert [r.url.host for r in recorder.requests] == ["api.x.com"]


@pytest.mark.parametrize(
    "pds_url",
    [
        "https://bsky.social:8443",
        "https://pds.example.org:1",
        "https://bsky.social#@evil.example",
        "https://bsky.social\\@evil.example",
        "https://evil.example\\@bsky.social",
        "https://bsky.social%2f@evil.example",
        "https://bsky.social?x",
        "https://evil.example;bsky.social",
        "https://bsky.social\t@evil.example",
        "https://bsky.social /@evil.example",
        "https://[::1]",
        "https://bsky.social.",
        "https://user:pw@bsky.social",
        "https://xn--bsky-social",
        "https://xn--bcher-kva.example",
    ],
)
def test_bluesky_pds_url_is_refused_unless_a_plain_https_origin(pds_url: str) -> None:
    with pytest.raises(ValueError, match="https:// origin"):
        bluesky.BlueskySource.host_uses({"pds_url": pds_url})
    with pytest.raises(ValueError, match="https:// origin"):
        make_source("bluesky", Recorder(), pds_url=pds_url)


def test_bluesky_pds_url_default_port_and_case_are_normalized() -> None:
    uses = bluesky.BlueskySource.host_uses({"pds_url": "https://PDS.Example.org:443/"})
    assert {u.host for u in uses} == {"public.api.bsky.app", "pds.example.org"}
    source = make_source("bluesky", Recorder(), pds_url="https://PDS.Example.org:443/")
    assert source.pds == "https://pds.example.org"  # type: ignore[attr-defined]


def test_option_errors_redact_a_user_name_and_password() -> None:
    with pytest.raises(ValueError) as caught:
        make_source("bluesky", Recorder(), pds_url="https://me:hunter2@pds.example.org:8443")
    assert "hunter2" not in str(caught.value) and "me:" not in str(caught.value)
    assert "***@pds.example.org" in str(caught.value)
    result = CliRunner().invoke(
        app,
        [
            "network",
            "--platform",
            "bluesky",
            "--source-option",
            "pds_url=http://u:hunter2@h.example",
        ],
    )
    assert result.exit_code == 2
    assert "hunter2" not in result.output


# -- sends_credentials is required, and credentials are detected too ---------------------


def test_sends_credentials_is_required_once_an_allowlist_is_installed() -> None:
    recorder = Recorder()
    source = make_source("reddit-archive", recorder)
    with pytest.raises(TypeError, match="sends_credentials"):
        source.http.get(reddit_archive.BASE_URL + "/api/users/search")
    assert recorder.requests == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"headers": {"Proxy-Authorization": "Basic eDp5"}},
        {"headers": {"Cookie": "session=abc"}},
        {"headers": {"X-Api-Key": "abc"}},
        {"headers": {"api-key": "abc"}},
        {"headers": {"X-Auth-Token": "abc"}},
        {"params": {"access_token": "abc"}},
        {"params": {"api_key": "abc"}},
        {"params": {"token": "abc"}},
        {"params": [("access_token", "abc")]},
        {"query": "?access_token=abc"},
        {"query": "?api_key=abc"},
        {"query": "?Token=abc"},
        {"json": {"identifier": "me", "password": "abc"}},
        {"data": {"client_secret": "abc"}},
        {"auth": ("id", "secret")},
    ],
)
def test_credentials_are_detected_even_when_not_declared(kwargs: dict) -> None:
    recorder = Recorder()
    source = make_source("bluesky", recorder)
    url = bluesky.PUBLIC_APPVIEW + "/xrpc/" + bluesky.GET_PROFILE + kwargs.pop("query", "")
    with pytest.raises(DisallowedHostError, match="refused to send credentials"):
        source.http.request("POST", url, sends_credentials=False, **kwargs)
    assert recorder.requests == []


def test_ordinary_requests_are_not_mistaken_for_credentials() -> None:
    recorder = Recorder()
    source = make_source("bluesky", recorder)
    source.http.get(
        bluesky.PUBLIC_APPVIEW + "/xrpc/" + bluesky.GET_PROFILE,
        params={"actor": "a.example", "cursor": "c", "limit": 5},
        headers={"User-Agent": "ua", "atproto-proxy": "did:web:x#bsky_appview"},
        sends_credentials=False,
    )
    assert len(recorder.requests) == 1


def test_client_wide_auth_counts_as_credentials_and_is_refused() -> None:
    """Client-wide auth is applied at send time, unseen by the binding check: refused."""
    recorder = Recorder()
    http = make_http(
        recorder, allowed_hosts={"a.example", "b.example"}, credential_hosts={"a.example"}
    )
    http._client.auth = ("id", "secret")
    assert http.detect_credentials("https://b.example/")
    with pytest.raises(TypeError, match="client-wide auth"):
        http.get("https://a.example/", sends_credentials=False)
    assert recorder.requests == []


@pytest.mark.parametrize(
    "auth",
    [httpx.BasicAuth("someone", "secret-value-1"), httpx.DigestAuth("someone", "x")],
)
def test_auth_other_than_a_tuple_is_refused(auth: httpx.Auth) -> None:
    recorder = Recorder()
    http = make_http(recorder, allowed_hosts={"a.example"}, credential_hosts={"a.example"})
    http.bind_credential_values({"secret-value-1": {"a.example"}})
    before: list[int] = []
    with pytest.raises(TypeError, match=r"auth only as a \(user, password\) tuple"):
        http.request(
            "GET",
            "https://a.example/",
            auth=auth,  # type: ignore[arg-type]
            sends_credentials=True,
            before_attempt=lambda a: before.append(a.attempt),
        )
    assert before == []
    assert recorder.requests == []


# -- per-credential enforcement ----------------------------------------------------------


@pytest.mark.parametrize(
    "how",
    [
        lambda v: {"headers": {"Authorization": f"bearer {v}"}},
        lambda v: {"headers": {"X-Note": v}},
        lambda v: {"params": {"q": v}},
        lambda v: {"json": {"note": v}},
        lambda v: {"data": {"note": v}},
        lambda v: {"auth": ("someone", v)},
    ],
)
@pytest.mark.parametrize("env", [reddit.CLIENT_SECRET_ENV, reddit.PASSWORD_ENV])
def test_a_credential_reaches_only_its_own_hosts(how: Callable[[str], dict], env: str) -> None:
    """oauth.reddit.com receives credentials (the token), but never the client secret."""
    recorder = Recorder()
    source = make_source("reddit", recorder)
    with pytest.raises(DisallowedHostError, match="refused to send a credential to oauth"):
        source.http.request(
            "POST",
            reddit.API_BASE + "/r/test/about",
            sends_credentials=True,
            **how(REDDIT_CREDS[env]),
        )
    assert recorder.requests == []


def test_an_encoded_credential_is_still_recognized() -> None:
    creds = {**REDDIT_CREDS, reddit.CLIENT_SECRET_ENV: "s3cr/t+value&x"}
    recorder = Recorder()
    source = make_source("reddit", recorder, credentials=creds)
    for kwargs in ({"params": {"q": "s3cr/t+value&x"}}, {"json": {"q": "s3cr/t+value&x"}}):
        with pytest.raises(DisallowedHostError, match="refused to send a credential"):
            source.http.request("POST", reddit.API_BASE + "/x", sends_credentials=True, **kwargs)
    assert recorder.requests == []


def test_bluesky_app_password_never_reaches_the_public_appview() -> None:
    recorder = Recorder()
    source = make_source("bluesky", recorder)
    with pytest.raises(DisallowedHostError, match="refused to send a credential"):
        source.http.get(
            bluesky.PUBLIC_APPVIEW + "/xrpc/" + bluesky.GET_PROFILE,
            params={"actor": BSKY_CREDS[bluesky.PASSWORD_ENV]},
            sends_credentials=False,
        )
    assert recorder.requests == []


def test_only_secret_credentials_are_bound() -> None:
    """Handles, user names and client ids are public: never bound, so never refused."""
    secrets = {
        "bluesky": {bluesky.PASSWORD_ENV},
        "reddit": {reddit.CLIENT_SECRET_ENV, reddit.PASSWORD_ENV},
        "x": {x.BEARER_ENV},
    }
    for platform, expected in secrets.items():
        source = make_source(platform, Recorder())
        bound = set(source.http._bound_values)
        assert bound == {CREDS[platform][env] for env in expected}, platform
        declared = {c.env: c.secret for u in type(source).network_hosts for c in u.credentials}
        assert {env for env, secret in declared.items() if secret} == expected


@pytest.mark.parametrize("actor", ["bigbob.bsky.social", "bob.bsky.social"])
def test_a_logged_in_bluesky_scan_may_look_up_the_researchers_handle(actor: str) -> None:
    """A scanned handle that contains (or is) the researcher's own handle is fine."""
    recorder = Recorder(lambda _r: httpx.Response(200, json={"did": "did:plc:a"}))
    creds = {bluesky.HANDLE_ENV: "bob.bsky.social", bluesky.PASSWORD_ENV: "abcd-efgh-ijkl-mnop"}
    source = make_source("bluesky", recorder, credentials=creds)
    source._call(bluesky.GET_PROFILE, {"actor": actor}, phase="hydrate")  # type: ignore[attr-defined]
    assert [r.url.params["actor"] for r in recorder.requests] == [actor]
    assert recorder.requests[0].url.host == "public.api.bsky.app"


def test_reddit_public_identifiers_may_reach_the_api_host() -> None:
    recorder = Recorder()
    source = make_source("reddit", recorder)
    for value in (REDDIT_CREDS[reddit.CLIENT_ID_ENV], REDDIT_CREDS[reddit.USERNAME_ENV]):
        source.http.get(
            reddit.API_BASE + f"/user/{value}/about",
            params={"q": value},
            sends_credentials=True,
        )
    assert len(recorder.requests) == 2


def test_a_short_password_does_not_block_an_unrelated_request() -> None:
    """A password of fewer than 8 characters is not bound: ``news`` cannot block /r/news."""
    creds = {**REDDIT_CREDS, reddit.PASSWORD_ENV: "news"}
    recorder = Recorder()
    source = make_source("reddit", recorder, credentials=creds)
    assert "news" not in source.http._bound_values
    source.http.get(
        reddit.API_BASE + "/r/news/about",
        headers={"Authorization": "bearer tok-abcdefgh"},
        sends_credentials=True,
    )
    assert len(recorder.requests) == 1


def test_an_eight_character_secret_is_matched_on_token_boundaries() -> None:
    creds = {**REDDIT_CREDS, reddit.PASSWORD_ENV: "newsroom"}
    recorder = Recorder()
    source = make_source("reddit", recorder, credentials=creds)
    for path in ("/r/newsroom/about", "/r/x/about?q=newsroom", "/r/x/about?q=a-newsroom_b"):
        with pytest.raises(DisallowedHostError, match="refused to send a credential to oauth"):
            source.http.get(reddit.API_BASE + path, sends_credentials=True)
    # Inside a longer word it is a different token, not the password.
    for path in ("/r/newsrooms/about", "/r/thenewsroom/about", "/r/x/about?q=newsroom2"):
        source.http.get(reddit.API_BASE + path, sends_credentials=True)
    assert len(recorder.requests) == 3


def test_a_secret_with_punctuation_at_its_edge_is_matched_anywhere() -> None:
    http = make_http(
        Recorder(),
        allowed_hosts={"a.example", "b.example"},
        credential_hosts={"a.example", "b.example"},
    )
    http.bind_credential_values({"-secret-value-": {"a.example"}})
    with pytest.raises(DisallowedHostError, match="refused to send a credential"):
        http.get("https://b.example/?q=x-secret-value-y", sends_credentials=True)


def test_a_json_credential_with_non_ascii_and_a_quote_is_recognized() -> None:
    """JSON-escaped both ways: ``\\u00e9`` and a literal ``é``, the quote escaped."""
    secret = 'pé"ss-word'
    recorder = Recorder()
    http = make_http(
        recorder,
        allowed_hosts={"a.example", "b.example"},
        credential_hosts={"a.example", "b.example"},
    )
    http.bind_credential_values({secret: {"a.example"}})
    ways = [
        {"json": {"p": secret}},  # As httpx encodes it.
        {"headers": {"X-Note": json.dumps({"p": secret})}},  # ensure_ascii=True.
    ]
    for kwargs in ways:
        with pytest.raises(DisallowedHostError, match="refused to send a credential"):
            http.request("POST", "https://b.example/", sends_credentials=True, **kwargs)
    assert recorder.requests == []
    http.request("POST", "https://a.example/", json={"p": secret}, sends_credentials=True)
    assert (
        recorder.requests[0].content
        == json.dumps({"p": secret}, ensure_ascii=False, separators=(",", ":")).encode()
    )


def test_credential_bindings_can_only_narrow() -> None:
    http = make_http(
        Recorder(),
        allowed_hosts={"a.example", "b.example"},
        credential_hosts={"a.example", "b.example"},
    )
    http.bind_credential_values({"value-1234": {"a.example", "b.example"}})
    http.bind_credential_values({"value-1234": {"a.example", "c.example"}})
    with pytest.raises(DisallowedHostError, match=r"declared only for a\.example"):
        http.get("https://b.example/?q=value-1234", sends_credentials=True)
    http.get("https://a.example/?q=value-1234", sends_credentials=True)


# -- the built request and every retry are checked ----------------------------------------


def test_the_built_request_is_checked_again() -> None:
    """A request whose final URL differs from the one given is checked as built."""
    recorder = Recorder()
    source = make_source("reddit-archive", recorder)
    real_build = source.http._client.build_request
    not_sent: list[int] = []

    def build(method: str, url: object, **kw: Any) -> httpx.Request:
        return real_build(method, "https://evil.example/collect", **kw)

    source.http._client.build_request = build  # type: ignore[method-assign]
    with pytest.raises(DisallowedHostError, match=r"evil\.example"):
        source.http.get(
            reddit_archive.BASE_URL + "/api/users/search",
            sends_credentials=False,
            on_attempt_not_sent=lambda a, _e: not_sent.append(a.attempt),
        )
    assert recorder.requests == []
    assert not_sent == [0]  # Refused after the hooks ran, so the reservation is released.
    assert source.http.requests_made == 0


def test_query_credentials_merged_at_build_time_are_checked() -> None:
    recorder = Recorder()
    source = make_source("bluesky", recorder)
    with pytest.raises(DisallowedHostError, match="refused to send credentials"):
        source.http.get(
            bluesky.PUBLIC_APPVIEW + "/xrpc/" + bluesky.GET_PROFILE,
            params="access_token=abc",  # type: ignore[arg-type]
            sends_credentials=False,
        )
    assert recorder.requests == []


def test_a_retry_is_refused_before_its_hooks_run() -> None:
    """Each attempt is checked first, before the pacer, the budget and the hooks."""
    recorder = Recorder(lambda _r: httpx.Response(503))
    http = make_http(recorder, max_retries=3, allowed_hosts={"api.example.test"})
    before: list[int] = []
    not_sent: list[int] = []

    def narrow(_attempt: Attempt, _response: httpx.Response) -> None:
        http.restrict_hosts(set())

    with pytest.raises(DisallowedHostError, match="not on this platform's host allowlist"):
        http.get(
            "https://api.example.test/x",
            sends_credentials=False,
            before_attempt=lambda a: before.append(a.attempt),
            on_response=narrow,
            on_attempt_not_sent=lambda a, _e: not_sent.append(a.attempt),
        )
    assert before == [0]
    assert not_sent == []
    assert http.requests_made == 1
    assert len(recorder.requests) == 1


def test_bluesky_create_session_to_a_non_credential_host_is_refused() -> None:
    recorder = Recorder()
    source = make_source("bluesky", recorder)
    source.pds = bluesky.PUBLIC_APPVIEW  # type: ignore[attr-defined]  # A bug that swaps the hosts.
    with pytest.raises(DisallowedHostError, match="refused to send credentials"):
        source._call_authed(bluesky.SEARCH_POSTS, {"q": "x"}, phase="discovery")  # type: ignore[attr-defined]
    assert recorder.requests == []


def test_bluesky_declares_sends_credentials_on_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(bluesky.CREATE_SESSION):
            return httpx.Response(200, json={"accessJwt": "a-1234", "refreshJwt": "r-1234"})
        if request.url.path.endswith(bluesky.SEARCH_POSTS):
            return httpx.Response(200, json={"posts": []})
        return httpx.Response(200, json={"did": "did:plc:a", "handle": "a.example"})

    source = make_source("bluesky", Recorder(handler))
    declared: list[tuple[str, bool]] = []
    real = source.http.request

    def spy(method: str, url: str, **kw: Any) -> httpx.Response:
        declared.append((url.rsplit("/", 1)[-1], kw["sends_credentials"]))
        return real(method, url, **kw)

    monkeypatch.setattr(source.http, "request", spy)
    source._call_authed(bluesky.SEARCH_POSTS, {"q": "x"}, phase="discovery")  # type: ignore[attr-defined]
    source._call(bluesky.GET_PROFILE, {"actor": "a.example"}, phase="hydrate")  # type: ignore[attr-defined]
    assert declared == [
        (bluesky.CREATE_SESSION, True),
        (bluesky.SEARCH_POSTS, True),
        (bluesky.GET_PROFILE, False),
    ]


# -- proxy and CA environment variables ----------------------------------------------------

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def test_proxy_and_ca_variables_are_ignored_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROXY_VARS:
        monkeypatch.setenv(name, "http://proxy.example:3128")
    # A CA file that does not exist would make httpx fail if it were read.
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent")
    http = HttpClient()
    assert http._client.trust_env is False
    assert http._client._mounts == {}  # No proxy transport.
    # Certificates are checked against certifi's bundle only.
    from bot_farm_silence_tracker.sources.http import certifi_ssl_context

    context = http._client._transport._pool._ssl_context  # type: ignore[attr-defined]
    assert context.verify_mode.name == "CERT_REQUIRED"
    assert context.cert_store_stats() == certifi_ssl_context().cert_store_stats()


def test_trust_env_honours_a_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    http = HttpClient(trust_env=True)
    assert http._client.trust_env is True
    proxies = [
        str(getattr(t, "_pool")._proxy_url)  # noqa: B009
        for t in http._client._mounts.values()
        if t is not None and hasattr(getattr(t, "_pool", None), "_proxy_url")
    ]
    assert any("proxy.example" in p for p in proxies), proxies


def _fake_scan_args(tmp_path: Any) -> list[str]:
    return [
        "scan", "--platform", "fake", "--n", "5", "--seed", "1",
        "--now", "2026-10-14T00:00:00Z", "--db", str(tmp_path / "f.db"),
        "--out", str(tmp_path / "f.csv"),
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("extra", "env", "expected"),
    [
        ([], {}, False),
        (["--trust-env"], {}, True),
        ([], {"BOT_FARM_SILENCE_TRACKER_TRUST_ENV": "1"}, True),
    ],
)
def test_cli_trust_env_is_opt_in(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
    env: dict[str, str],
    expected: bool,
) -> None:
    seen: list[bool] = []

    def make_client(**kwargs: Any) -> HttpClient:
        seen.append(kwargs["trust_env"])
        return HttpClient(**kwargs)

    monkeypatch.setattr("bot_farm_silence_tracker.cli.HttpClient", make_client)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    result = CliRunner().invoke(app, [*_fake_scan_args(tmp_path), *extra])
    assert result.exit_code == 0, result.output
    assert seen == [expected]
    assert ("--trust-env is on" in result.output) is expected


# -- options that decide where credentials go are never inherited ------------------------


def test_bluesky_declares_pds_url_as_a_credential_destination() -> None:
    assert bluesky.BlueskySource.credential_destination_options == {"pds_url"}
    assert bluesky.BlueskySource.credential_destinations({"pds_url": "https://p.example"}) == {
        "p.example"
    }


def test_resolve_resume_never_inherits_a_credential_destination() -> None:
    from bot_farm_silence_tracker.cli import dropped_credential_destinations, resolve_resume

    stored = {"source_options": {"pds_url": "https://evil.example", "page_size": "50"}}
    _values, options, conflicts = resolve_resume(
        1, stored, {}, {}, force=False, credential_destination_options={"pds_url"}
    )
    assert options == {"page_size": "50"}
    assert dropped_credential_destinations(stored, {}, {"pds_url"}) == {
        "pds_url": "https://evil.example"
    }
    # Passed again, the command line wins without a conflict (and without --force).
    _values, options, conflicts = resolve_resume(
        1,
        stored,
        {},
        {"pds_url": "https://bsky.social"},
        force=False,
        credential_destination_options={"pds_url"},
    )
    assert options["pds_url"] == "https://bsky.social" and conflicts == []
    assert dropped_credential_destinations(stored, {"pds_url": "x"}, {"pds_url"}) == {}


def test_reddit_access_token_is_bound_to_the_api_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == reddit.TOKEN_URL:
            return httpx.Response(
                200, json={"access_token": "ACCESS-SECRET-13", "expires_in": 3600}
            )
        return httpx.Response(200, json={"data": {}})

    recorder = Recorder(handler)
    source = make_source("reddit", recorder)
    source._api_get("/r/test/about")  # type: ignore[attr-defined]
    with pytest.raises(DisallowedHostError, match="refused to send a credential to www"):
        source.http.request(
            "POST", reddit.TOKEN_URL, data={"note": "ACCESS-SECRET-13"}, sends_credentials=True
        )
    assert [r.url.host for r in recorder.requests] == ["www.reddit.com", "oauth.reddit.com"]


def test_credential_params_are_refused_before_any_hook_runs() -> None:
    recorder = Recorder()
    source = make_source("bluesky", recorder)
    before: list[int] = []
    with pytest.raises(DisallowedHostError, match="refused to send credentials"):
        source.http.get(
            bluesky.PUBLIC_APPVIEW + "/xrpc/" + bluesky.GET_PROFILE,
            params={"access_token": "abc"},
            sends_credentials=False,
            before_attempt=lambda a: before.append(a.attempt),
        )
    assert before == []
    assert recorder.requests == []


@pytest.mark.parametrize("separator", [" ", "  ", "\t", " \t "])
def test_a_credential_in_a_hand_made_basic_header_is_recognized(separator: str) -> None:
    recorder = Recorder()
    source = make_source("reddit", recorder)
    pair = f"someone:{REDDIT_CREDS[reddit.CLIENT_SECRET_ENV]}".encode()
    header = "Basic" + separator + base64.b64encode(pair).decode()
    with pytest.raises(DisallowedHostError, match="refused to send a credential to oauth"):
        source.http.get(
            reddit.API_BASE + "/r/test/about",
            headers={"Authorization": header},
            sends_credentials=True,
        )
    assert recorder.requests == []
