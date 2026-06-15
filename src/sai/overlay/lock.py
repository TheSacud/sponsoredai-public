"""Single-authority lock so only one SponsorSession bills per machine-session.

The in-terminal compositor and the desktop overlay can both be live at once (a
user running ``sai codex`` in a terminal while Claude Desktop is open). Only one
should count impressions, or a single attended wait double-bills. Whoever holds
this lock is the billing authority; the other runs display-only.

Best-effort and cross-platform: an atomic O_EXCL create, with staleness recovery
so a crashed holder doesn't wedge the lock forever.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from ..config import runtime_paths

# One canonical lock for the whole machine-session, shared by the terminal
# compositor and the desktop overlay: a single user attends one surface at a
# time, so only the lock holder may bill -- the other shows credit-0.
BILLING_AUTHORITY_LOCK_FILE = "billing_authority.lock"


# A lock older than this with no live owner is considered abandoned and stolen.
STALE_AFTER_SECONDS = 6 * 60 * 60


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        # OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION). A handle means it lives.
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    return True


class InstanceLock:
    """Filesystem lock keyed on a path. ``acquire`` returns True if this process
    becomes the authority. Use as a context manager or call acquire/release."""

    def __init__(self, path) -> None:
        self._path = Path(path)
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def _owner_pid(self) -> Optional[int]:
        try:
            text = self._path.read_text(encoding="utf-8").strip()
            return int(text.split()[0]) if text else None
        except (OSError, ValueError, IndexError):
            return None

    def _is_stale(self) -> bool:
        pid = self._owner_pid()
        if pid is not None and _process_alive(pid) and pid != os.getpid():
            return False
        # Unreadable, dead owner, or our own leftover: also treat very old as stale.
        try:
            age = time.time() - self._path.stat().st_mtime
        except OSError:
            return True
        return pid is None or not _process_alive(pid) or age > STALE_AFTER_SECONDS

    def acquire(self) -> bool:
        # Best effort: ANY filesystem failure (denied permission, read-only home,
        # path too long, a transient AV lock, etc.) means we did not become the
        # authority -- return False rather than aborting the caller's run over a
        # lock file. Only a clean exclusive create returns True.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._is_stale():
                    return False
                # Reclaim an abandoned lock, then retry once.
                self._path.unlink()
                fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()} {int(time.time())}")
        except OSError:
            return False
        self._held = True
        return True

    def release(self) -> None:
        if not self._held:
            return
        if self._owner_pid() == os.getpid():
            try:
                self._path.unlink()
            except OSError:
                pass
        self._held = False

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def billing_authority_lock() -> InstanceLock:
    """The single machine-session billing-authority lock, shared by the terminal
    runner and the desktop overlay so only one surface bills at a time."""
    return InstanceLock(runtime_paths().home / BILLING_AUTHORITY_LOCK_FILE)
