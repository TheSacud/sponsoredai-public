"""The host window: a frameless, topmost, layered banner that floats over the
target app without ever taking focus, and follows the target as it moves.

This is the hard, durable part of the overlay. It is deliberately content-
agnostic -- it paints by delegating to a ``Surface`` -- so the one-line text
banner today and a richer WebView2-hosted ad later share the exact same window
behaviour (topmost, click-through toggle, no-activate, DPI-correct positioning).

Windows-only; everything ctypes is built lazily so the package imports on POSIX.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .geometry import Placement
from .surface import Surface
from .win32 import is_windows

logger = logging.getLogger(__name__)


# Window + extended styles.
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080  # keep it off the taskbar / Alt-Tab
WS_EX_TRANSPARENT = 0x00000020  # click-through

GWL_EXSTYLE = -20
LWA_ALPHA = 0x02

# Messages.
WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_LBUTTONUP = 0x0202
WM_DPICHANGED = 0x02E0

# ShowWindow / SetWindowPos.
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SRCCOPY = 0x00CC0020

PM_REMOVE = 0x0001
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
IDC_ARROW = 32512


def enable_dpi_awareness() -> None:
    """Opt the process into per-monitor DPI awareness so GetWindowRect and our
    placement are in true physical pixels. Best-effort across Windows versions.
    Call once, before any window is created."""
    if not is_windows():
        return
    import ctypes

    user32 = ctypes.WinDLL("user32")
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)-4
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


class OverlayWindow:
    DEFAULT_DPI = 96

    def __init__(
        self,
        surface: Surface,
        *,
        on_click: Optional[Callable[[], None]] = None,
        on_dismiss: Optional[Callable[[], None]] = None,
        alpha: int = 235,
        corner_radius: int = 10,
    ) -> None:
        if not is_windows():
            raise RuntimeError("OverlayWindow is only available on Windows")

        import ctypes
        import ctypes.wintypes as wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._surface = surface
        self._on_click = on_click
        self._on_dismiss = on_dismiss
        self._alpha = alpha
        self._corner_radius = corner_radius
        self._card = None
        self._dpi = self.DEFAULT_DPI
        self._hwnd = 0
        self._size = (0, 0)  # last applied client size, for click hit-testing
        self._placement: tuple[int, int, int, int] | None = None
        self._shown = False
        self._class_name = f"SaiOverlayBanner_{id(self)}"

        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._build_types()
        self._setup_prototypes()
        self._create()

    # -- type + prototype setup --------------------------------------------

    def _build_types(self) -> None:
        ctypes, wintypes = self._ctypes, self._wintypes
        c_void_p, c_int, c_uint = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint
        c_size_t, c_ssize_t = ctypes.c_size_t, ctypes.c_ssize_t

        # WPARAM is pointer-sized unsigned, LPARAM/LRESULT pointer-sized signed.
        self._WNDPROC = ctypes.WINFUNCTYPE(c_ssize_t, c_void_p, c_uint, c_size_t, c_ssize_t)

        LONG = wintypes.LONG

        class POINT(ctypes.Structure):
            _fields_ = [("x", LONG), ("y", LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", c_void_p), ("message", c_uint), ("wParam", c_size_t),
                        ("lParam", c_ssize_t), ("time", wintypes.DWORD), ("pt", POINT)]

        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [("hdc", c_void_p), ("fErase", wintypes.BOOL), ("rcPaint", RECT),
                        ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL),
                        ("rgbReserved", wintypes.BYTE * 32)]

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", c_uint), ("lpfnWndProc", self._WNDPROC),
                        ("cbClsExtra", c_int), ("cbWndExtra", c_int),
                        ("hInstance", c_void_p), ("hIcon", c_void_p),
                        ("hCursor", c_void_p), ("hbrBackground", c_void_p),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        self._POINT, self._RECT, self._MSG = POINT, RECT, MSG
        self._PAINTSTRUCT, self._WNDCLASS = PAINTSTRUCT, WNDCLASS

    def _setup_prototypes(self) -> None:
        ctypes, wintypes = self._ctypes, self._wintypes
        c_void_p, c_int, c_uint = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint
        c_size_t, c_ssize_t = ctypes.c_size_t, ctypes.c_ssize_t
        DWORD, BOOL = wintypes.DWORD, wintypes.BOOL
        u, k = self._user32, self._kernel32

        k.GetModuleHandleW.restype = c_void_p
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        u.RegisterClassW.restype = wintypes.ATOM
        u.RegisterClassW.argtypes = [ctypes.POINTER(self._WNDCLASS)]
        u.UnregisterClassW.argtypes = [wintypes.LPCWSTR, c_void_p]
        u.LoadCursorW.restype = c_void_p
        u.LoadCursorW.argtypes = [c_void_p, c_void_p]
        u.CreateWindowExW.restype = c_void_p
        u.CreateWindowExW.argtypes = [DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, DWORD,
                                      c_int, c_int, c_int, c_int, c_void_p, c_void_p,
                                      c_void_p, c_void_p]
        u.DestroyWindow.argtypes = [c_void_p]
        u.ShowWindow.argtypes = [c_void_p, c_int]
        u.SetLayeredWindowAttributes.argtypes = [c_void_p, wintypes.COLORREF, wintypes.BYTE, DWORD]
        u.SetWindowPos.argtypes = [c_void_p, c_void_p, c_int, c_int, c_int, c_int, c_uint]
        u.DefWindowProcW.restype = c_ssize_t
        u.DefWindowProcW.argtypes = [c_void_p, c_uint, c_size_t, c_ssize_t]
        u.BeginPaint.restype = c_void_p
        u.BeginPaint.argtypes = [c_void_p, ctypes.POINTER(self._PAINTSTRUCT)]
        u.EndPaint.argtypes = [c_void_p, ctypes.POINTER(self._PAINTSTRUCT)]
        u.GetClientRect.argtypes = [c_void_p, ctypes.POINTER(self._RECT)]
        u.InvalidateRect.argtypes = [c_void_p, c_void_p, BOOL]
        u.PeekMessageW.argtypes = [ctypes.POINTER(self._MSG), c_void_p, c_uint, c_uint, c_uint]
        u.TranslateMessage.argtypes = [ctypes.POINTER(self._MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(self._MSG)]
        u.PostQuitMessage.argtypes = [c_int]

        # The *Ptr variants are the 64-bit-safe way to read/write window longs.
        self._GetWindowLongPtr = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
        self._SetWindowLongPtr = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
        self._GetWindowLongPtr.restype = c_ssize_t
        self._GetWindowLongPtr.argtypes = [c_void_p, c_int]
        self._SetWindowLongPtr.restype = c_ssize_t
        self._SetWindowLongPtr.argtypes = [c_void_p, c_int, c_ssize_t]
        u.SetWindowRgn.restype = c_int
        u.SetWindowRgn.argtypes = [c_void_p, c_void_p, BOOL]
        self._gdi32.CreateCompatibleDC.restype = c_void_p
        self._gdi32.CreateCompatibleDC.argtypes = [c_void_p]
        self._gdi32.CreateCompatibleBitmap.restype = c_void_p
        self._gdi32.CreateCompatibleBitmap.argtypes = [c_void_p, c_int, c_int]
        self._gdi32.SelectObject.restype = c_void_p
        self._gdi32.SelectObject.argtypes = [c_void_p, c_void_p]
        self._gdi32.DeleteObject.argtypes = [c_void_p]
        self._gdi32.DeleteDC.argtypes = [c_void_p]
        self._gdi32.BitBlt.argtypes = [c_void_p, c_int, c_int, c_int, c_int, c_void_p, c_int, c_int, wintypes.DWORD]
        self._gdi32.CreateRoundRectRgn.restype = c_void_p
        self._gdi32.CreateRoundRectRgn.argtypes = [c_int, c_int, c_int, c_int, c_int, c_int]

        self._GetDpiForWindow = getattr(u, "GetDpiForWindow", None)
        if self._GetDpiForWindow is not None:
            self._GetDpiForWindow.restype = c_uint
            self._GetDpiForWindow.argtypes = [c_void_p]

    # -- lifecycle ----------------------------------------------------------

    def _create(self) -> None:
        ctypes = self._ctypes
        hinstance = self._kernel32.GetModuleHandleW(None)

        wndclass = self._WNDCLASS()
        wndclass.style = CS_HREDRAW | CS_VREDRAW
        # Bind a per-instance WndProc and keep a strong ref so it is not GC'd
        # while Windows still holds the pointer.
        self._wndproc = self._WNDPROC(self._on_message)
        wndclass.lpfnWndProc = self._wndproc
        wndclass.hInstance = hinstance
        wndclass.hCursor = self._user32.LoadCursorW(None, IDC_ARROW)
        wndclass.hbrBackground = None  # we paint the whole client area ourselves
        wndclass.lpszClassName = self._class_name
        if not self._user32.RegisterClassW(ctypes.byref(wndclass)):
            raise OSError(f"RegisterClassW failed: {ctypes.get_last_error()}")
        self._wndclass = wndclass  # keep alive

        exstyle = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        hwnd = self._user32.CreateWindowExW(
            exstyle, self._class_name, "SAI", WS_POPUP,
            0, 0, 10, 10, None, None, hinstance, None,
        )
        if not hwnd:
            raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
        self._hwnd = int(hwnd)
        self._user32.SetLayeredWindowAttributes(self._hwnd, 0, self._alpha, LWA_ALPHA)
        if self._GetDpiForWindow is not None:
            dpi = self._GetDpiForWindow(self._hwnd)
            if dpi:
                self._dpi = int(dpi)
        # Let the surface request a repaint (e.g. when an async brand logo loads).
        set_repaint = getattr(self._surface, "set_repaint", None)
        if callable(set_repaint):
            set_repaint(self._invalidate)

    def _invalidate(self) -> None:
        # Safe to call from any thread; marks the window for a WM_PAINT.
        # Do not erase first: progress updates repaint frequently and a background
        # erase between frames reads as flicker on layered windows.
        if self._hwnd:
            self._user32.InvalidateRect(self._hwnd, None, False)

    @property
    def hwnd(self) -> int:
        return self._hwnd

    @property
    def dpi(self) -> int:
        return self._dpi

    def set_dpi(self, value: int) -> None:
        """The driver tracks the target monitor's DPI and pushes it here, so the
        next measure/paint uses the right scale on mixed-DPI multi-monitor."""
        if value:
            self._dpi = int(value)

    def _on_message(self, hwnd, message, wparam, lparam):
        ctypes = self._ctypes
        if message == WM_PAINT:
            ps = self._PAINTSTRUCT()
            hdc = self._user32.BeginPaint(hwnd, ctypes.byref(ps))
            try:
                rect = self._RECT()
                self._user32.GetClientRect(hwnd, ctypes.byref(rect))
                width, height = rect.right - rect.left, rect.bottom - rect.top
                if self._card is not None and width > 0 and height > 0:
                    try:
                        self._paint_buffered(hdc, width, height)
                    except Exception:  # noqa: BLE001 - never let a Win32 callback exception escape
                        logger.debug("overlay paint failed", exc_info=True)
            finally:
                self._user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if message == WM_ERASEBKGND:
            # The surface always paints the full client area. Suppressing the
            # separate erase pass avoids visible flashes during the reward ring
            # animation.
            return 1
        if message == WM_LBUTTONUP:
            self._handle_click(lparam)
            return 0
        if message == WM_DPICHANGED:
            # The banner crossed to a monitor with a different DPI between ticks;
            # keep painting at the right scale (the high word of wParam is the
            # new DPI). The driver also re-pushes the target DPI each tick.
            new_dpi = (wparam >> 16) & 0xFFFF
            if new_dpi:
                self._dpi = new_dpi
            return 0
        if message == WM_DESTROY:
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _paint_buffered(self, hdc: int, width: int, height: int) -> None:
        memdc = self._gdi32.CreateCompatibleDC(hdc)
        if not memdc:
            self._surface.paint(hdc, width, height, self._card, self._dpi)
            return
        bitmap = self._gdi32.CreateCompatibleBitmap(hdc, width, height)
        if not bitmap:
            self._gdi32.DeleteDC(memdc)
            self._surface.paint(hdc, width, height, self._card, self._dpi)
            return
        old = self._gdi32.SelectObject(memdc, bitmap)
        try:
            self._surface.paint(memdc, width, height, self._card, self._dpi)
            self._gdi32.BitBlt(hdc, 0, 0, width, height, memdc, 0, 0, SRCCOPY)
        finally:
            self._gdi32.SelectObject(memdc, old)
            self._gdi32.DeleteObject(bitmap)
            self._gdi32.DeleteDC(memdc)

    def _handle_click(self, lparam) -> None:
        # Client coords are packed into lParam: low word = x, high word = y.
        x = lparam & 0xFFFF
        y = (lparam >> 16) & 0xFFFF
        if x >= 0x8000:
            x -= 0x10000
        if y >= 0x8000:
            y -= 0x10000
        width, height = self._size
        if width and height and self._on_dismiss is not None:
            try:
                x0, y0, x1, y1 = self._surface.close_box(width, height, self._dpi)
            except Exception:  # noqa: BLE001 - a surface without a close box just isn't dismissable here
                x0 = y0 = x1 = y1 = -1
            if x0 <= x <= x1 and y0 <= y <= y1:
                self._on_dismiss()
                return
        if self._on_click is not None:
            self._on_click()

    # -- public API ---------------------------------------------------------

    def set_card(self, card) -> None:
        self._card = card
        if self._hwnd:
            self._user32.InvalidateRect(self._hwnd, None, False)

    def move_to(self, placement: Placement) -> None:
        if not self._hwnd:
            return
        next_placement = (placement.x, placement.y, placement.width, placement.height)
        if next_placement == self._placement:
            return
        self._placement = next_placement
        # Re-assert HWND_TOPMOST on every move so the banner stays above the
        # target's own window even after the target raises itself.
        self._user32.SetWindowPos(
            self._hwnd, HWND_TOPMOST,
            placement.x, placement.y, placement.width, placement.height,
            SWP_NOACTIVATE,
        )
        size = (placement.width, placement.height)
        if size != self._size:
            self._size = size
            radius = max(1, round(self._corner_radius * self._dpi / 96))
            # +1 because CreateRoundRectRgn's right/bottom are exclusive.
            region = self._gdi32.CreateRoundRectRgn(
                0, 0, placement.width + 1, placement.height + 1, radius, radius
            )
            # SetWindowRgn takes ownership of the region; do not delete it.
            self._user32.SetWindowRgn(self._hwnd, region, True)

    def set_click_through(self, enabled: bool) -> None:
        if not self._hwnd:
            return
        exstyle = self._GetWindowLongPtr(self._hwnd, GWL_EXSTYLE)
        if enabled:
            exstyle |= WS_EX_TRANSPARENT
        else:
            exstyle &= ~WS_EX_TRANSPARENT
        self._SetWindowLongPtr(self._hwnd, GWL_EXSTYLE, exstyle)

    def show(self) -> None:
        if self._hwnd and not self._shown:
            self._user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            self._shown = True

    def hide(self) -> None:
        if self._hwnd and self._shown:
            self._user32.ShowWindow(self._hwnd, SW_HIDE)
            self._shown = False

    def pump(self) -> None:
        """Drain pending window messages without blocking. The overlay's own loop
        owns timing, so we never sit in a blocking GetMessage."""
        if not self._hwnd:
            return
        ctypes = self._ctypes
        msg = self._MSG()
        while self._user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))

    def close(self) -> None:
        if self._hwnd:
            self._user32.DestroyWindow(self._hwnd)
            self._hwnd = 0
        try:
            self._user32.UnregisterClassW(self._class_name, self._kernel32.GetModuleHandleW(None))
        except OSError:
            pass
        dispose = getattr(self._surface, "dispose", None)
        if callable(dispose):
            dispose()


def _smoke() -> None:  # pragma: no cover - manual/live verification only
    """Pop the banner over the foreground window for a few seconds and print a
    self-check of its on-screen state. Run with: python -m sai.overlay.window"""
    import time

    from ..sponsors import SponsorCard
    from .geometry import place_banner
    from .surface import TextSurface
    from .visibility import claude_desktop_matcher
    from .win32 import default_probe

    enable_dpi_awareness()
    probe = default_probe()
    is_claude = claude_desktop_matcher()
    surface = TextSurface()
    window = OverlayWindow(surface, on_click=lambda: print("[smoke] banner clicked"))
    card = SponsorCard(
        id="demo", sponsor="Your Brand",
        message="Ship faster agent workflows", url="https://sponsoredai.dev/sponsor",
        credit_amount=0.0,
    )
    window.set_card(card)
    window.set_click_through(False)

    shown_over_claude = False
    placement = None
    for _ in range(60):  # ~12s
        fg = probe.foreground_window()
        path = probe.process_image_path(fg) or ""
        rect = probe.window_rect(fg)
        if rect is not None:
            w, h = surface.measure(card, window.dpi)
            placement = place_banner(rect, w, h, anchor="bottom")
            window.move_to(placement)
            window.show()
            if is_claude(path):
                shown_over_claude = True
        window.pump()
        time.sleep(0.2)

    flags = window._GetWindowLongPtr(window.hwnd, GWL_EXSTYLE)
    print("hwnd            :", window.hwnd)
    print("dpi             :", window.dpi)
    print("placement       :", placement)
    print("readback rect   :", probe.window_rect(window.hwnd))
    print("is_visible      :", probe.is_window_visible(window.hwnd))
    print("TOPMOST set     :", bool(flags & WS_EX_TOPMOST))
    print("LAYERED set     :", bool(flags & WS_EX_LAYERED))
    print("NOACTIVATE set  :", bool(flags & WS_EX_NOACTIVATE))
    print("TOOLWINDOW set  :", bool(flags & WS_EX_TOOLWINDOW))
    print("ever over Claude:", shown_over_claude)
    window.close()


if __name__ == "__main__":  # pragma: no cover
    _smoke()
