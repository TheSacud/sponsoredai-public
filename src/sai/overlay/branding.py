"""Safely fetch a sponsor's brand icon for the overlay banner.

Fetching ``brand_icon_url`` from the user's machine is a new SSRF/privacy
surface the terminal never had, so it is locked down: HTTPS only, the host must
resolve to a public address (no loopback/private/link-local/reserved ranges),
redirects are refused, the response must be an image, and the body is size- and
time-capped. The validation is a pure function so it is testable without a
network.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from typing import Callable, Optional
from urllib.parse import urlparse

from ..config import USER_AGENT

ALLOWED_CONTENT_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/gif",
    "image/webp", "image/x-icon", "image/vnd.microsoft.icon",
}
DEFAULT_TIMEOUT = 2.0
DEFAULT_MAX_BYTES = 512 * 1024

# getaddrinfo-shaped resolver: (host, port) -> list of (family, type, proto, canon, sockaddr).
Resolver = Callable[[str, int], list]


def _default_resolve(host: str, port: int) -> list:
    return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)


def icon_url_rejection(url: str, *, resolve: Resolver = _default_resolve) -> Optional[str]:
    """Return a short reason string if the URL must NOT be fetched, else None.

    Rejects anything but HTTPS, and any host that resolves to a non-public
    address (the SSRF guard) -- so a sponsor cannot point the icon at the user's
    own network or cloud metadata endpoints.
    """
    if len(url) > 2048:
        return "too_long"
    try:
        parsed = urlparse(url)
    except ValueError:
        return "unparseable"
    if parsed.scheme != "https":
        return "not_https"
    if parsed.username or parsed.password:
        return "credentials"
    host = parsed.hostname
    if not host:
        return "no_host"
    try:
        infos = resolve(host, parsed.port or 443)
    except (OSError, socket.gaierror):
        return "dns_failed"
    if not infos:
        return "dns_empty"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            return "bad_address"
        # Require a globally-routable address. This is stronger than enumerating
        # private/loopback/etc. -- it also rejects CGNAT/shared (100.64.0.0/10),
        # IETF-protocol and benchmarking ranges -- and matches the backend's
        # _is_public_destination_host ingest check.
        if not ip.is_global:
            return "non_public_ip"
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Refuse redirects entirely: a validated public HTTPS URL must not be able to
    # bounce to an internal target after the SSRF check.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_icon(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    resolve: Resolver = _default_resolve,
) -> Optional[bytes]:
    """Fetch the icon bytes if the URL passes every guard, else None. Never
    raises; safe to call off the UI thread."""
    if not url or icon_url_rejection(url, resolve=resolve) is not None:
        return None
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type not in ALLOWED_CONTENT_TYPES:
                return None
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                return None
            return data or None
    except (OSError, urllib.error.URLError, ValueError):
        return None
