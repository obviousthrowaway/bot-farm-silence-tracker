"""The --version flag works on its own, without a subcommand."""

from __future__ import annotations

from typer.testing import CliRunner

from bot_farm_silence_tracker import __version__
from bot_farm_silence_tracker.cli import app


def test_version_flag_prints_version_and_exits_zero() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"bot-farm-silence-tracker {__version__}"


def _patch_head(monkeypatch, head) -> list[dict]:  # type: ignore[no-untyped-def]
    """Replace httpx.Client.head; return the client settings each call saw."""
    import httpx

    seen: list[dict] = []

    def fake_head(self: httpx.Client, url: object, **_: object) -> httpx.Response:
        seen.append(
            {
                "url": str(url),
                "follow_redirects": self.follow_redirects,
                "trust_env": self.trust_env,
            }
        )
        return head(str(url))

    monkeypatch.setattr(httpx.Client, "head", fake_head)
    return seen


def test_selftest_tls_is_hidden_and_reports_success(monkeypatch) -> None:
    import httpx

    seen = _patch_head(
        monkeypatch, lambda url: httpx.Response(200, request=httpx.Request("HEAD", url))
    )
    result = CliRunner().invoke(app, ["selftest-tls"])
    assert result.exit_code == 0, result.output
    assert "tls ok" in result.output
    assert "selftest-tls" not in CliRunner().invoke(app, ["--help"]).output
    # One HEAD to the fixed address, no redirects, no proxy or CA from the environment.
    assert seen == [
        {"url": "https://pypi.org/simple/", "follow_redirects": False, "trust_env": False}
    ]


def test_selftest_tls_fails_on_connection_error(monkeypatch) -> None:
    import httpx

    def fail(url: str) -> httpx.Response:
        raise httpx.ConnectError("certificate verify failed")

    _patch_head(monkeypatch, fail)
    result = CliRunner().invoke(app, ["selftest-tls"])
    assert result.exit_code == 1
    assert "tls ok" not in result.output


def test_selftest_tls_url_must_be_https_without_credentials(monkeypatch) -> None:
    import httpx

    seen = _patch_head(
        monkeypatch, lambda url: httpx.Response(200, request=httpx.Request("HEAD", url))
    )
    for url in ("http://pypi.org/", "ftp://pypi.org/", "https://user:hunter2@pypi.org/", "x"):
        result = CliRunner().invoke(app, ["selftest-tls", "--url", url])
        assert result.exit_code != 0, url
        assert "hunter2" not in result.output
    assert seen == []


def test_selftest_tls_does_not_follow_a_redirect(monkeypatch) -> None:
    import httpx

    seen = _patch_head(
        monkeypatch,
        lambda url: httpx.Response(
            302,
            headers={"Location": "https://elsewhere.example/"},
            request=httpx.Request("HEAD", url),
        ),
    )
    result = CliRunner().invoke(app, ["selftest-tls", "--url", "https://pypi.org/x"])
    assert result.exit_code == 0, result.output
    assert [call["url"] for call in seen] == ["https://pypi.org/x"]
