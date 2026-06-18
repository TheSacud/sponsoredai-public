from __future__ import annotations

import ssl
import urllib.request

try:
    import certifi  # type: ignore
except Exception:  # noqa: BLE001 - source checkouts may not have optional deps installed yet
    certifi = None  # type: ignore[assignment]


_HTTPS_CONTEXT: ssl.SSLContext | None = None
_TRIED_CERTIFI = False


def urlopen(request, *, timeout: float):
    """Open a urllib request, preferring certifi's CA bundle when available.

    PyInstaller builds on macOS can lose access to the interpreter's usual CA
    discovery path. certifi gives the binary a deterministic trust store while
    source installs without certifi keep urllib's platform default behavior.
    """
    context = _https_context()
    if context is None:
        return urllib.request.urlopen(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def _https_context() -> ssl.SSLContext | None:
    global _HTTPS_CONTEXT, _TRIED_CERTIFI
    if _HTTPS_CONTEXT is not None or _TRIED_CERTIFI:
        return _HTTPS_CONTEXT
    _TRIED_CERTIFI = True
    if certifi is None:
        return None
    try:
        _HTTPS_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - keep networking best-effort
        _HTTPS_CONTEXT = None
    return _HTTPS_CONTEXT


def reset_for_tests() -> None:
    global _HTTPS_CONTEXT, _TRIED_CERTIFI
    _HTTPS_CONTEXT = None
    _TRIED_CERTIFI = False
