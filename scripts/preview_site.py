#!/usr/bin/env python3
"""Serve the static SAI site locally for review.

This is intentionally a tiny stdlib-only helper. It serves ``site-v3`` with the
same clean-route behavior the nginx config uses for public static pages:
``/market`` maps to ``market.html``, and unknown routes fall back to
``index.html``.
"""

from __future__ import annotations

import argparse
import mimetypes
import posixpath
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
DEFAULT_SITE_DIR = REPO / "site-v3"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8798
DEFAULT_CHECK_PATHS = ("/", "/market", "/trust", "/privacy", "/terms")
# Clean-route aliases for pages living outside the site root (none right now;
# /demo and /promo were retired with the v4 redesign).
ALIASES: dict[str, Path] = {}


@dataclass(frozen=True)
class ResolvedPath:
    path: Path
    used_fallback: bool


class SitePreviewServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        site_dir: Path,
        verbose: bool = False,
    ) -> None:
        super().__init__(server_address, request_handler_class)
        self.site_dir = site_dir.resolve()
        self.verbose = verbose


class SitePreviewHandler(BaseHTTPRequestHandler):
    server: SitePreviewServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
        self._send_static(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler method name
        self._send_static(head_only=True)

    def log_message(self, fmt: str, *args: object) -> None:
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _send_static(self, *, head_only: bool) -> None:
        resolved = resolve_site_path(self.server.site_dir, self.path)
        if resolved is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            data = resolved.path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = mimetypes.guess_type(str(resolved.path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)


def resolve_site_path(
    site_dir: Path,
    request_target: str,
    *,
    allow_fallback: bool = True,
) -> ResolvedPath | None:
    site_dir = site_dir.resolve()
    path = urllib.parse.urlsplit(request_target).path or "/"
    path = urllib.parse.unquote(path)
    if not path.startswith("/"):
        path = "/" + path

    normalized = posixpath.normpath(path)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if path.endswith("/") and not normalized.endswith("/"):
        normalized += "/"

    direct_candidates: list[Path] = []
    alias = ALIASES.get(normalized.rstrip("/"))
    if alias is not None:
        direct_candidates.append(alias)
    elif normalized == "/" or normalized.endswith("/"):
        direct_candidates.append(Path(normalized.lstrip("/")) / "index.html")
    else:
        rel = Path(normalized.lstrip("/"))
        direct_candidates.extend((rel, rel.with_name(rel.name + ".html")))

    for candidate in direct_candidates:
        path = _contained_file(site_dir, candidate)
        if path is not None:
            return ResolvedPath(path=path, used_fallback=False)

    if allow_fallback:
        fallback = _contained_file(site_dir, Path("index.html"))
        if fallback is not None:
            return ResolvedPath(path=fallback, used_fallback=True)
    return None


def _contained_file(site_dir: Path, relative_path: Path) -> Path | None:
    candidate = (site_dir / relative_path).resolve()
    try:
        candidate.relative_to(site_dir)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def make_server(
    site_dir: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    verbose: bool = False,
) -> SitePreviewServer:
    site_dir = site_dir.expanduser()
    if not site_dir.is_dir():
        raise ValueError(f"site directory does not exist: {site_dir}")
    return SitePreviewServer((host, port), SitePreviewHandler, site_dir=site_dir, verbose=verbose)


def server_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[:2]
    display_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else str(host)
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}/"


def check_preview(server: SitePreviewServer, paths: list[str], *, timeout: float) -> list[str]:
    errors: list[str] = []
    base_url = server_url(server)
    thread = threading.Thread(target=server.serve_forever, name="site-preview-check", daemon=True)
    thread.start()
    try:
        for path in paths:
            route = path if path.startswith("/") else f"/{path}"
            direct = resolve_site_path(server.site_dir, route, allow_fallback=False)
            if direct is None:
                errors.append(f"{route} does not map to a static file")
                continue
            url = urllib.parse.urljoin(base_url, route.lstrip("/"))
            try:
                with urllib.request.urlopen(url, timeout=timeout) as response:
                    if response.status != HTTPStatus.OK:
                        errors.append(f"{route} returned HTTP {response.status}")
                    elif not response.read(1):
                        errors.append(f"{route} returned an empty response")
            except Exception as exc:  # noqa: BLE001 - smoke should report all failures
                errors.append(f"{route} failed: {type(exc).__name__}")
    finally:
        server.shutdown()
        thread.join(timeout=timeout)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview the static SAI site-v3 pages")
    parser.add_argument("--site-dir", type=Path, default=DEFAULT_SITE_DIR, help="Static site directory")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Port to bind (default: 8798, or 0 with --check)")
    parser.add_argument("--open", action="store_true", help="Open the preview URL in a browser")
    parser.add_argument("--check", action="store_true", help="Start the server, fetch key routes, and exit")
    parser.add_argument(
        "--check-path",
        action="append",
        dest="check_paths",
        help="Route to fetch in --check mode; may be repeated",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-request timeout for --check")
    parser.add_argument("--verbose", action="store_true", help="Log HTTP requests")
    args = parser.parse_args(argv)

    port = args.port if args.port is not None else (0 if args.check else DEFAULT_PORT)
    try:
        server = make_server(args.site_dir, host=args.host, port=port, verbose=args.verbose)
    except (OSError, ValueError) as exc:
        print(f"site preview failed: {exc}", file=sys.stderr)
        return 1

    url = server_url(server)
    if args.check:
        paths = args.check_paths or list(DEFAULT_CHECK_PATHS)
        try:
            errors = check_preview(server, paths, timeout=max(0.1, float(args.timeout)))
        finally:
            server.server_close()
        if errors:
            print("site preview check FAILED", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        print(f"site preview check OK ({len(paths)} routes)")
        return 0

    print(f"site preview listening at {url}")
    print("serving site-v3; press Ctrl+C to stop")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nsite preview stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
