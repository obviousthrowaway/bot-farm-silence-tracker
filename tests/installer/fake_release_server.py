"""A stand-in for github.com that serves one fake release, for testing the installers.

It answers the three kinds of request the installers make:

* ``/<repo>/releases/latest`` redirects to ``/<repo>/releases/tag/<tag>``, or to
  ``/<repo>/releases`` when started without ``--tag`` (no release yet), as GitHub does.
* ``/<repo>/releases/download/<tag>/<name>`` serves the files given with ``--file``,
  plus a ``SHA256SUMS.txt`` computed from them.
* With ``--latest-mode`` set to ``503``, ``429`` or ``200-html``, ``/<repo>/releases/latest``
  answers with that error status or with a plain 200 HTML page instead of a redirect, to
  test what the installers do when the lookup of the latest release does not work.
* ``/<name>`` serves the files given with ``--script`` (such as ``install.ps1``) as text.

Point the installers at it with ``BOT_FARM_SILENCE_TRACKER_GITHUB_URL=http://127.0.0.1:PORT``
and ``BOT_FARM_SILENCE_TRACKER_INSECURE_TEST=1``. Example::

    python tests/installer/fake_release_server.py --port 8765 --tag v0.0.1 \\
        --file bot-farm-silence-tracker-linux-x64=/path/to/program
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
from pathlib import Path

DEFAULT_REPO = "obviousthrowaway/bot-farm-silence-tracker"


def _pair(text: str) -> tuple[str, Path]:
    name, sep, path = text.partition("=")
    if not sep or not name or "/" in name:
        raise argparse.ArgumentTypeError(f"expected NAME=PATH, got {text!r}")
    return name, Path(path)


def build_handler(
    repo: str,
    tag: str | None,
    files: dict[str, bytes],
    scripts: dict[str, bytes],
    latest_mode: str = "redirect",
) -> type[http.server.BaseHTTPRequestHandler]:
    release = dict(files)
    if files and "SHA256SUMS.txt" not in release:
        lines = [f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()]
        release["SHA256SUMS.txt"] = "".join(lines).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            prefix = f"/{repo}/releases"
            if path == f"{prefix}/latest" and latest_mode == "503":
                self._send(503, b"Service Unavailable", {"Retry-After": "1"})
            elif path == f"{prefix}/latest" and latest_mode == "429":
                self._send(429, b"Too Many Requests", {"Retry-After": "1"})
            elif path == f"{prefix}/latest" and latest_mode == "200-html":
                self._send(200, b"<html>Sign in to continue</html>", {"Content-Type": "text/html"})
            elif path == f"{prefix}/latest":
                target = f"{prefix}/tag/{tag}" if tag else prefix
                host = self.headers.get("Host", "127.0.0.1")
                self._send(302, headers={"Location": f"http://{host}{target}"})
            elif path in (prefix, f"{prefix}/tag/{tag}"):
                self._send(200, b"release page", {"Content-Type": "text/html"})
            elif tag and path.startswith(f"{prefix}/download/{tag}/"):
                name = path.rsplit("/", 1)[1]
                if name in release:
                    self._send(200, release[name], {"Content-Type": "application/octet-stream"})
                else:
                    self._send(404, b"Not Found")
            elif path.lstrip("/") in scripts:
                body = scripts[path.lstrip("/")]
                self._send(200, body, {"Content-Type": "text/plain; charset=utf-8"})
            else:
                self._send(404, b"Not Found")

        do_HEAD = do_GET

        def log_message(self, format: str, *args: object) -> None:
            print(f"fake-release: {self.command} {self.path}", flush=True)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--tag", help="release tag; leave out to act as if there is no release")
    parser.add_argument(
        "--latest-mode",
        choices=["redirect", "503", "429", "200-html"],
        default="redirect",
        help="how /releases/latest answers (default: redirect, as GitHub does)",
    )
    parser.add_argument("--file", type=_pair, action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--script", type=Path, action="append", default=[], metavar="PATH")
    args = parser.parse_args()
    files = {name: path.read_bytes() for name, path in args.file}
    scripts = {path.name: path.read_bytes() for path in args.script}
    handler = build_handler(args.repo, args.tag, files, scripts, args.latest_mode)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"fake-release: serving on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
