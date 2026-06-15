"""Thin ctypes wrappers around the Win32 calls the overlay needs to decide when
its banner is genuinely on screen.

Everything Windows-specific is confined to ``Win32Probe`` and is built lazily in
its constructor, so importing this module on POSIX (for the test suite) never
touches ``ctypes.windll``/``wintypes``. Callers depend on the ``SystemProbe``
protocol, never on ``Win32Probe`` directly, which lets tests inject a fake.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


# PROCESS_QUERY_LIMITED_INFORMATION: the least-privileged right that still lets a
# non-elevated process read another process's full image path. PROCESS_QUERY_-
# INFORMATION would be denied against many targets; this one usually is not.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# DwmGetWindowAttribute index: non-zero means the window is cloaked (e.g. parked
# on another virtual desktop) and therefore not actually visible to the user.
DWMWA_CLOAKED = 14
# MonitorFromWindow flag: return NULL (0) when the window is not on any monitor,
# so an off-screen overlay compares unequal to the target's monitor.
MONITOR_DEFAULTTONULL = 0


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@runtime_checkable
class SystemProbe(Protocol):
    """The OS queries the visibility predicate is built from. Implemented for
    real by ``Win32Probe`` and faked in tests."""

    def foreground_window(self) -> int: ...
    def process_image_path(self, hwnd: int) -> Optional[str]: ...
    def window_rect(self, hwnd: int) -> Optional[Rect]: ...
    def is_window_visible(self, hwnd: int) -> bool: ...
    def is_minimized(self, hwnd: int) -> bool: ...
    def is_cloaked(self, hwnd: int) -> bool: ...
    def monitor_of(self, hwnd: int) -> int: ...
    def monitor_work_area(self, hwnd: int) -> Optional[Rect]: ...
    def monitor_dpi(self, hwnd: int) -> int: ...
    def idle_seconds(self) -> float: ...


def is_windows() -> bool:
    return os.name == "nt"


def _as_int(handle: object) -> int:
    """A c_void_p restype comes back as an int (or None for NULL); normalise both
    to a plain int so handles can be compared and stored uniformly."""
    return int(handle) if handle else 0


class Win32Probe:
    """Live ``SystemProbe`` backed by user32/kernel32/dwmapi.

    Every call is wrapped so a transient failure (an elevated target that denies
    OpenProcess, a window that closes mid-query, an unavailable DWM) FAILS CLOSED
    for billing -- it reports "not visible" / "user away" rather than the
    billable value -- instead of crashing the 5-times-a-second overlay loop. An
    unprovable honesty check must never keep a billing window open.
    """

    def __init__(self) -> None:
        if not is_windows():
            raise RuntimeError("Win32Probe is only available on Windows")

        import ctypes.wintypes as wintypes

        self._wintypes = wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            dwmapi: Optional[ctypes.WinDLL] = ctypes.WinDLL("dwmapi")
        except OSError:
            # dwmapi is present on every supported Windows, but degrade rather
            # than fail to construct if the DLL cannot be loaded.
            dwmapi = None

        c_void_p = ctypes.c_void_p
        POINTER = ctypes.POINTER
        DWORD, BOOL, UINT, LONG = wintypes.DWORD, wintypes.BOOL, wintypes.UINT, wintypes.LONG

        class RECT(ctypes.Structure):
            _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", UINT), ("dwTime", DWORD)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", DWORD)]

        self._RECT = RECT
        self._LASTINPUTINFO = LASTINPUTINFO
        self._MONITORINFO = MONITORINFO

        # Pin down argument/return types so 64-bit handles are never truncated to
        # 32-bit ints (the classic ctypes-on-Windows bug).
        user32.GetForegroundWindow.restype = c_void_p
        user32.GetForegroundWindow.argtypes = []
        user32.GetWindowThreadProcessId.argtypes = [c_void_p, POINTER(DWORD)]
        user32.GetWindowThreadProcessId.restype = DWORD
        user32.GetWindowRect.argtypes = [c_void_p, POINTER(RECT)]
        user32.GetWindowRect.restype = BOOL
        user32.IsWindowVisible.argtypes = [c_void_p]
        user32.IsWindowVisible.restype = BOOL
        user32.IsIconic.argtypes = [c_void_p]
        user32.IsIconic.restype = BOOL
        user32.MonitorFromWindow.argtypes = [c_void_p, DWORD]
        user32.MonitorFromWindow.restype = c_void_p
        user32.GetMonitorInfoW.argtypes = [c_void_p, POINTER(MONITORINFO)]
        user32.GetMonitorInfoW.restype = BOOL
        # GetDpiForWindow is Win10 1607+; absent on older Windows.
        self._GetDpiForWindow = getattr(user32, "GetDpiForWindow", None)
        if self._GetDpiForWindow is not None:
            self._GetDpiForWindow.restype = UINT
            self._GetDpiForWindow.argtypes = [c_void_p]
        user32.GetLastInputInfo.argtypes = [POINTER(LASTINPUTINFO)]
        user32.GetLastInputInfo.restype = BOOL

        kernel32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
        kernel32.OpenProcess.restype = c_void_p
        kernel32.QueryFullProcessImageNameW.argtypes = [c_void_p, DWORD, wintypes.LPWSTR, POINTER(DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = BOOL
        kernel32.CloseHandle.argtypes = [c_void_p]
        kernel32.CloseHandle.restype = BOOL
        kernel32.GetTickCount.argtypes = []
        kernel32.GetTickCount.restype = DWORD

        if dwmapi is not None:
            dwmapi.DwmGetWindowAttribute.argtypes = [c_void_p, DWORD, POINTER(DWORD), DWORD]
            dwmapi.DwmGetWindowAttribute.restype = LONG

        self._user32 = user32
        self._kernel32 = kernel32
        self._dwmapi = dwmapi
        # QueryFullProcessImageNameW is called every tick; reuse one buffer rather
        # than allocating 64 KB five times a second. Single-threaded by contract.
        self._PATH_CAP = 32768
        self._path_buf = ctypes.create_unicode_buffer(self._PATH_CAP)

    def foreground_window(self) -> int:
        try:
            return _as_int(self._user32.GetForegroundWindow())
        except OSError:
            return 0

    def process_image_path(self, hwnd: int) -> Optional[str]:
        if not hwnd:
            return None
        try:
            pid = self._wintypes.DWORD(0)
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return None
            handle = self._kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
            )
            if not handle:
                # Commonly an elevated target denying a non-elevated query.
                return None
            try:
                size = self._wintypes.DWORD(self._PATH_CAP)
                ok = self._kernel32.QueryFullProcessImageNameW(
                    handle, 0, self._path_buf, ctypes.byref(size)
                )
                if not ok:
                    return None
                return self._path_buf.value or None
            finally:
                self._kernel32.CloseHandle(handle)
        except OSError:
            return None

    def window_rect(self, hwnd: int) -> Optional[Rect]:
        if not hwnd:
            return None
        try:
            rect = self._RECT()
            if not self._user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
            return Rect(rect.left, rect.top, rect.right, rect.bottom)
        except OSError:
            return None

    def is_window_visible(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        try:
            return bool(self._user32.IsWindowVisible(hwnd))
        except OSError:
            return False

    def is_minimized(self, hwnd: int) -> bool:
        # Fail closed: an unprovable state counts as minimized (not visible).
        if not hwnd:
            return True
        try:
            return bool(self._user32.IsIconic(hwnd))
        except OSError:
            return True

    def is_cloaked(self, hwnd: int) -> bool:
        # Fail closed: if we cannot prove the window is NOT cloaked (dwmapi
        # missing, a non-S_OK result, or an error), treat it as cloaked so a
        # billing session never counts on an unverifiable visibility check.
        if not hwnd or self._dwmapi is None:
            return True
        try:
            value = self._wintypes.DWORD(0)
            hr = self._dwmapi.DwmGetWindowAttribute(
                hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
            )
            if hr != 0:  # not S_OK; the attribute could not be read -> fail closed
                return True
            return value.value != 0
        except OSError:
            return True

    def monitor_of(self, hwnd: int) -> int:
        if not hwnd:
            return 0
        try:
            return _as_int(self._user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONULL))
        except OSError:
            return 0

    def monitor_work_area(self, hwnd: int) -> Optional[Rect]:
        """The work area (screen minus taskbar) of the monitor the window is on.
        Used to clamp the banner on-screen even when the window's frame extends
        past the visible edge."""
        if not hwnd:
            return None
        MONITOR_DEFAULTTONEAREST = 2
        try:
            monitor = self._user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            if not monitor:
                return None
            info = self._MONITORINFO()
            info.cbSize = ctypes.sizeof(self._MONITORINFO)
            if not self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return None
            work = info.rcWork
            return Rect(work.left, work.top, work.right, work.bottom)
        except OSError:
            return None

    def monitor_dpi(self, hwnd: int) -> int:
        # DPI of the monitor the window is on. The banner tracks the target, so
        # this is the scale to measure/paint it at on mixed-DPI setups. 0 when
        # unavailable (caller falls back to the last known DPI).
        if not hwnd or self._GetDpiForWindow is None:
            return 0
        try:
            return int(self._GetDpiForWindow(hwnd)) or 0
        except OSError:
            return 0

    def idle_seconds(self) -> float:
        # Fail closed: if presence is unprovable, report infinite idle (== user
        # away) so a billing session never counts an unverifiable presence, and
        # so the AFK rotation guard is not reset on a failed read.
        try:
            info = self._LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(self._LASTINPUTINFO)
            if not self._user32.GetLastInputInfo(ctypes.byref(info)):
                return float("inf")
            tick = self._kernel32.GetTickCount()
            # Both are 32-bit DWORDs; masking the difference makes the ~49.7-day
            # GetTickCount wraparound a no-op instead of a huge negative idle.
            elapsed_ms = (tick - info.dwTime) & 0xFFFFFFFF
            return elapsed_ms / 1000.0
        except OSError:
            return float("inf")


def default_probe() -> SystemProbe:
    """The live probe on Windows. Raises elsewhere, since the overlay only runs
    on Windows; tests construct their own fake probe instead of calling this."""
    if not is_windows():
        raise RuntimeError("The Win32 system probe is only available on Windows")
    return Win32Probe()
