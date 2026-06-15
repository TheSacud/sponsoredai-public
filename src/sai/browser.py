"""Open URLs in the user's browser, but only ones we trust.

A sponsor link, dashboard URL or pairing link is ultimately backend-supplied
text. Handing an arbitrary scheme straight to ``webbrowser.open`` dispatches it
through the OS URL handler, so a misbehaving or compromised backend (or a
malicious sponsor whose URL we relay unvalidated) could launch far more than a
web page on a single click: ``file://attacker-host/share`` leaks NetNTLM hashes
over SMB on Windows, and custom protocol handlers (``search-ms:``, ``ms-msdt:``,
...) are a known local-exec vector. Defence in depth: open ``http``/``https``
only and drop everything else.
"""

from __future__ import annotations

import logging
import webbrowser
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# The only schemes a backend-derived URL is ever allowed to launch. Matches the
# allow-list the backend itself enforces on placement/redirect URLs.
SAFE_URL_SCHEMES = frozenset({"http", "https"})


def is_safe_url(url: str | None) -> bool:
    """True only for an ``http``/``https`` URL safe to hand to the OS handler."""
    if not url:
        return False
    try:
        scheme = urlparse(url).scheme
    except ValueError:
        return False
    return scheme.lower() in SAFE_URL_SCHEMES


def open_url(url: str | None) -> bool:
    """Open ``url`` in the browser iff it is ``http``/``https``; else log+skip.

    Returns True if a browser open was attempted. Never raises: a disallowed
    scheme is refused and an OS-level open failure is swallowed, so callers in a
    UI/event loop can safely ignore the result.
    """
    if not is_safe_url(url):
        logger.warning("refusing to open non-http(s) url: %r", url)
        return False
    try:
        webbrowser.open(url)
    except OSError:
        logger.debug("failed to open url", exc_info=True)
        return False
    return True
