"""What each platform adapter sends where, for the ``network`` command.

Built from the same :class:`~bot_farm_silence_tracker.sources.base.HostUse` declarations that
the shared HTTP client enforces as its host allowlist, so the report cannot drift
from what the code allows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from bot_farm_silence_tracker.sources.registry import SOURCE_REGISTRY, load_source_class


def platform_network(platform: str, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Describe one platform's network use: hosts, credentials, login and default rate.

    ``options`` are ``--source-option`` values that change the hosts (Bluesky's
    ``pds_url``). Raises ``ValueError`` for an option that names a host that cannot
    be allowed, and :class:`~bot_farm_silence_tracker.sources.registry.AdapterNotAvailable`
    for an unknown platform.
    """
    cls = load_source_class(platform)
    uses = cls.host_uses(options)
    credentials = [{"env": name, "required": True} for name in cls.required_credentials]
    credentials += [{"env": name, "required": False} for name in cls.optional_credentials]
    return {
        "platform": platform,
        "login": cls.login_requirement(),
        "credentials": credentials,
        "default_rate": {
            "requests_per_second": round(cls.default_rps, 4),
            "burst": cls.default_burst,
        },
        "allowed_hosts": sorted({u.host for u in uses}),
        "hosts": [
            {
                "host": u.host,
                "scheme": "https",
                "purpose": u.purpose,
                "login_only": u.login_only,
                "credentials": [
                    {"env": c.env, "how": c.how, "secret": c.secret} for c in u.credentials
                ],
                "tokens": list(u.tokens),
            }
            for u in uses
        ],
    }


def network_report(
    platforms: Sequence[str] | None = None, options: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """:func:`platform_network` for ``platforms`` (default: every registered platform)."""
    names = list(platforms) if platforms else sorted(SOURCE_REGISTRY)
    return [platform_network(name, options) for name in names]


def format_network(report: Sequence[Mapping[str, Any]]) -> str:
    """Render :func:`network_report` as plain text."""
    lines: list[str] = []
    for entry in report:
        if lines:
            lines.append("")
        login = {"none": "no login"}.get(entry["login"], f"login {entry['login']}")
        lines.append(f"{entry['platform']} ({login})")
        rate = entry["default_rate"]
        lines.append(
            f"  default rate: {rate['requests_per_second']:g} requests/s, burst {rate['burst']:g}"
        )
        if entry["credentials"]:
            names = ", ".join(
                f"{c['env']} ({'required' if c['required'] else 'optional'})"
                for c in entry["credentials"]
            )
            lines.append(f"  credentials (environment only): {names}")
        else:
            lines.append("  credentials: none")
        if not entry["hosts"]:
            lines.append("  hosts: none (no network access; every request is refused)")
            continue
        lines.append("  hosts (https only; every other host is refused):")
        for host in entry["hosts"]:
            when = " [only with a login]" if host["login_only"] else ""
            lines.append(f"    https://{host['host']}{when}")
            lines.append(f"      used for: {host['purpose']}")
            for cred in host["credentials"]:
                lines.append(f"      receives {cred['env']}: {cred['how']}")
            for token in host["tokens"]:
                lines.append(f"      receives {token}")
            if not host["credentials"] and not host["tokens"]:
                lines.append("      receives no credentials")
    return "\n".join(lines)
