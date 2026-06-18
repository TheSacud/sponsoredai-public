"""Passive "a newer version is available" check for the CLI.

The CLI has no auto-update: a global ``npm install -g @sponsoredai/cli`` stays
pinned until the user re-runs it. This module provides the nudge -- a cached,
best-effort lookup of the latest published version that the wallet command and
the agent wrappers surface at safe seams.

Design notes:
- The source of truth is the npm registry's ``latest`` dist-tag for the launcher
  package, NOT the backend. The backend runs from source and its ``__version__``
  can run ahead of what is actually published, so it cannot reliably answer "are
  you behind"; the registry, which is exactly what an update resolves to, can.
- It never raises and never blocks for long: a day-fresh cache short-circuits the
  network, and any fetch error (offline, slow, malformed) degrades to "no nudge".
- It is silent in CI and opt-out-able with ``SAI_NO_UPDATE_CHECK``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from . import __version__
from .ansi import DIM, style
from .config import USER_AGENT, runtime_paths, write_json_atomic

logger = logging.getLogger(__name__)

# The package users install with ``npm install -g``; its ``latest`` dist-tag is
# what an update would land them on, so it is the right thing to compare against.
PACKAGE_NAME = "@sponsoredai/cli"
REGISTRY_LATEST_URL = f"https://registry.npmjs.org/{PACKAGE_NAME}/latest"
UPDATE_COMMAND = f"npm install -g {PACKAGE_NAME}"

# Cached under the SAI home so the VS Code status bar (which polls `sai wallet`)
# does not hit the network on every read. A passive nudge does not need to be
# fresher than a day.
CACHE_FILE = "update_check.json"
CACHE_TTL_SECONDS = 24 * 60 * 60
# Short, so a stale cache day's single fetch cannot noticeably delay `sai wallet`
# (the extension reads it with an 8s timeout).
FETCH_TIMEOUT_SECONDS = 1.5

# Mirror config.ci_environment, but env-injectable so tests (which themselves run
# in CI) can exercise the check with a clean environment.
_CI_KEYS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "TF_BUILD")

Fetcher = Callable[[], str]


@dataclass(frozen=True)
class UpdateInfo:
    current: str
    latest: str

    @property
    def available(self) -> bool:
        return _is_newer(self.latest, self.current)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def update_check_disabled(env: dict[str, str] | os._Environ[str] = os.environ) -> bool:
    """Whether to skip the check entirely: explicit opt-out or a CI run."""
    if _truthy(env.get("SAI_NO_UPDATE_CHECK")):
        return True
    return any(env.get(key) for key in _CI_KEYS)


def _parse_version(value: str) -> tuple[int, ...] | None:
    """Numeric ``major.minor.patch`` tuple, or None if not a plain release.

    A pre-release/build suffix (``1.2.3-rc1``, ``1.2.3+build``) is dropped before
    parsing: we only ever publish plain releases, so the comparison stays simple
    and a non-numeric or malformed string yields None (treated as "cannot tell").
    """
    core = value.strip().lstrip("v").split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    out: list[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        out.append(int(part))
    return tuple(out) or None


def _is_newer(candidate: str, baseline: str) -> bool:
    new = _parse_version(candidate)
    old = _parse_version(baseline)
    if new is None or old is None:
        return False
    return new > old


def _default_fetcher() -> str:
    request = urllib.request.Request(REGISTRY_LATEST_URL, method="GET")
    # The default urllib User-Agent is rejected by the edge bot filter; reuse the
    # CLI's so the registry (and any proxy) sees a consistent client.
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", "application/json")
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8")


def _cache_path():
    return runtime_paths().home / CACHE_FILE


def _read_cache(now: float) -> str | None:
    try:
        with _cache_path().open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    checked_at = data.get("checked_at")
    latest = data.get("latest_version")
    if not isinstance(checked_at, (int, float)) or not isinstance(latest, str):
        return None
    if now - float(checked_at) > CACHE_TTL_SECONDS:
        return None
    return latest


def _write_cache(latest: str, now: float) -> None:
    try:
        write_json_atomic(_cache_path(), {"checked_at": now, "latest_version": latest})
    except OSError:
        # The cache is an optimization; a write failure just means we re-check
        # sooner. Never let it break the command we were called from.
        pass


def check_for_update(
    *,
    fetcher: Fetcher | None = None,
    now: float | None = None,
    env: dict[str, str] | os._Environ[str] = os.environ,
) -> UpdateInfo | None:
    """Return :class:`UpdateInfo` when a newer published version exists, else None.

    Best-effort and non-throwing. A day-fresh cache avoids the network; otherwise
    one short HTTP GET to the npm registry refreshes it. ``fetcher``/``now``/``env``
    are injectable for tests.
    """
    if update_check_disabled(env):
        return None
    clock = time.time() if now is None else now
    latest = _read_cache(clock)
    if latest is None:
        fetch = fetcher or _default_fetcher
        try:
            payload: Any = json.loads(fetch())
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            logger.debug("update check fetch failed error=%s", type(exc).__name__)
            return None
        candidate = payload.get("version") if isinstance(payload, dict) else None
        if not isinstance(candidate, str) or _parse_version(candidate) is None:
            return None
        latest = candidate
        _write_cache(latest, clock)
    info = UpdateInfo(current=__version__, latest=latest)
    return info if info.available else None


def update_notice(info: UpdateInfo) -> str:
    """One-line, dim notice for a terminal."""
    arrow = "→" if _unicode_ok() else "->"
    return style(
        f"A new sai is available: {info.current} {arrow} {info.latest}. "
        f"Update with: {UPDATE_COMMAND}",
        DIM,
    )


def notify_terminal_update(
    stream=None,
    *,
    fetcher: Fetcher | None = None,
    now: float | None = None,
    env: dict[str, str] | os._Environ[str] = os.environ,
) -> UpdateInfo | None:
    """Print a passive update notice to a TTY ``stderr``, if one is available.

    Only safe to call at a session seam (a command finishing, or after an agent
    has exited and the terminal is back to normal flow) -- it writes a single
    trailing line, never mid-render, so it cannot clobber an agent's repainting
    viewport. No-ops when stderr is not a TTY (pipes, redirects, CI).
    """
    target = sys.stderr if stream is None else stream
    if not _stream_is_tty(target):
        return None
    info = check_for_update(fetcher=fetcher, now=now, env=env)
    if info is None:
        return None
    print(update_notice(info), file=target)
    return info


def _stream_is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _unicode_ok() -> bool:
    # Lazy import so the module loads even if ansi's unicode probe is heavy; the
    # arrow is cosmetic and the ASCII fallback is always correct.
    try:
        from .ansi import UNICODE_OK

        return bool(UNICODE_OK)
    except Exception:  # pragma: no cover - cosmetic fallback only
        return False
