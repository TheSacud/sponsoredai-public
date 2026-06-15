"""System-tray icon: the overlay's always-available control and consent home.

A chromeless floating banner has nowhere to put the kill switch, frequency,
or the Terms/Privacy links a real-money ad surface needs -- and a persistent ad
with no visible owner reads as adware. The tray icon's right-click menu is that
home, wired to the existing config controls.

The menu LOGIC (TrayController) is pure and testable without Win32; the native
icon/menu (TrayIcon) is Windows-only and built lazily in ctypes.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List

from ..browser import open_url
from ..config import kill_switch_active, set_frequency, set_kill_switch
from .win32 import is_windows

logger = logging.getLogger(__name__)

ID_TOGGLE = 1
ID_TERMS = 20
ID_PRIVACY = 21
ID_QUIT = 99
# Frequency menu item id -> config value.
FREQ_ITEMS = [(10, "off"), (11, "low"), (12, "normal"), (13, "high")]
FREQ_BY_ID = dict(FREQ_ITEMS)


class TrayController:
    """Builds the menu spec and performs the chosen action, wired to the same
    controls as `sai config` so the tray and CLI stay consistent."""

    def __init__(
        self,
        config: dict,
        on_quit: Callable[[], None],
        opener: Callable[[str], None] = open_url,
    ) -> None:
        self._config = config
        self._on_quit = on_quit
        self._open = opener

    def items(self) -> List[Dict]:
        enabled = not kill_switch_active()
        freq = self._config.get("frequency", "normal")
        items: List[Dict] = [
            {"id": ID_TOGGLE, "label": "Show sponsor ads", "checked": enabled},
            {"sep": True},
        ]
        for fid, name in FREQ_ITEMS:
            items.append({"id": fid, "label": f"Frequency: {name}", "checked": freq == name})
        items += [
            {"sep": True},
            {"id": ID_TERMS, "label": "Terms…"},
            {"id": ID_PRIVACY, "label": "Privacy…"},
            {"sep": True},
            {"id": ID_QUIT, "label": "Quit SAI overlay"},
        ]
        return items

    def invoke(self, item_id: int) -> None:
        if item_id == ID_TOGGLE:
            # "Show sponsor ads" is checked when ads are enabled (no kill switch).
            # Toggling flips the kill switch; the overlay loop re-reads it live.
            currently_enabled = not kill_switch_active()
            set_kill_switch(currently_enabled, reason="sai overlay tray")
        elif item_id in FREQ_BY_ID:
            value = FREQ_BY_ID[item_id]
            set_frequency(value)
            # Mutate the live config dict the running session reads each tick, so
            # the change takes effect without a restart.
            self._config["frequency"] = value
            self._config["ads_enabled"] = value != "off"
        elif item_id == ID_TERMS:
            self._open(self._url("/terms"))
        elif item_id == ID_PRIVACY:
            self._open(self._url("/privacy"))
        elif item_id == ID_QUIT:
            self._on_quit()

    def _url(self, path: str) -> str:
        base = (self._config.get("backend_url") or "https://sponsoredai.dev").rstrip("/")
        return base + path


# Win32 constants.
WM_APP = 0x8000
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
NIM_ADD, NIM_DELETE = 0x00000000, 0x00000002
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x00000001, 0x00000002, 0x00000004
IDI_APPLICATION = 32512
MF_STRING, MF_CHECKED, MF_SEPARATOR = 0x0000, 0x0008, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
WS_POPUP = 0x80000000


class TrayIcon:
    CALLBACK = WM_APP + 1

    def __init__(self, controller: TrayController, *, tooltip: str = "SAI sponsor overlay") -> None:
        if not is_windows():
            raise RuntimeError("TrayIcon is only available on Windows")
        import ctypes
        import ctypes.wintypes as wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._controller = controller
        self._tooltip = tooltip
        self._hwnd = 0
        self._hicon = 0
        self._added = False
        self._class_name = f"SaiTray_{id(self)}"
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._build_types()
        self._setup_prototypes()
        self._create()

    def _build_types(self) -> None:
        ctypes, wintypes = self._ctypes, self._wintypes
        c_void_p, c_uint, c_int = ctypes.c_void_p, ctypes.c_uint, ctypes.c_int
        c_size_t, c_ssize_t, c_wchar = ctypes.c_size_t, ctypes.c_ssize_t, ctypes.c_wchar
        DWORD, LONG = wintypes.DWORD, wintypes.LONG

        self._WNDPROC = ctypes.WINFUNCTYPE(c_ssize_t, c_void_p, c_uint, c_size_t, c_ssize_t)

        class POINT(ctypes.Structure):
            _fields_ = [("x", LONG), ("y", LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", DWORD), ("yHotspot", DWORD),
                        ("hbmMask", c_void_p), ("hbmColor", c_void_p)]

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", DWORD), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", DWORD), ("hWnd", c_void_p), ("uID", c_uint), ("uFlags", c_uint),
                ("uCallbackMessage", c_uint), ("hIcon", c_void_p), ("szTip", c_wchar * 128),
                ("dwState", DWORD), ("dwStateMask", DWORD), ("szInfo", c_wchar * 256),
                ("uVersion", c_uint), ("szInfoTitle", c_wchar * 64), ("dwInfoFlags", DWORD),
                ("guidItem", GUID), ("hBalloonIcon", c_void_p),
            ]

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", c_uint), ("lpfnWndProc", self._WNDPROC),
                        ("cbClsExtra", c_int), ("cbWndExtra", c_int), ("hInstance", c_void_p),
                        ("hIcon", c_void_p), ("hCursor", c_void_p), ("hbrBackground", c_void_p),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        self._POINT, self._NID, self._WNDCLASS = POINT, NOTIFYICONDATAW, WNDCLASS
        self._RECT, self._ICONINFO = RECT, ICONINFO

    def _setup_prototypes(self) -> None:
        ctypes, wintypes = self._ctypes, self._wintypes
        c_void_p, c_uint, c_int = ctypes.c_void_p, ctypes.c_uint, ctypes.c_int
        c_size_t, c_ssize_t = ctypes.c_size_t, ctypes.c_ssize_t
        DWORD, BOOL = wintypes.DWORD, wintypes.BOOL
        u, k, s = self._user32, self._kernel32, self._shell32

        k.GetModuleHandleW.restype = c_void_p
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        u.RegisterClassW.restype = wintypes.ATOM
        u.RegisterClassW.argtypes = [ctypes.POINTER(self._WNDCLASS)]
        u.UnregisterClassW.argtypes = [wintypes.LPCWSTR, c_void_p]
        u.CreateWindowExW.restype = c_void_p
        u.CreateWindowExW.argtypes = [DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, DWORD,
                                      c_int, c_int, c_int, c_int, c_void_p, c_void_p, c_void_p, c_void_p]
        u.DestroyWindow.argtypes = [c_void_p]
        u.DefWindowProcW.restype = c_ssize_t
        u.DefWindowProcW.argtypes = [c_void_p, c_uint, c_size_t, c_ssize_t]
        u.LoadIconW.restype = c_void_p
        u.LoadIconW.argtypes = [c_void_p, c_void_p]
        u.SetForegroundWindow.argtypes = [c_void_p]
        u.CreatePopupMenu.restype = c_void_p
        u.AppendMenuW.argtypes = [c_void_p, c_uint, c_size_t, wintypes.LPCWSTR]
        u.TrackPopupMenu.restype = c_int
        u.TrackPopupMenu.argtypes = [c_void_p, c_uint, c_int, c_int, c_int, c_void_p, c_void_p]
        u.DestroyMenu.argtypes = [c_void_p]
        u.GetCursorPos.argtypes = [ctypes.POINTER(self._POINT)]
        u.PostMessageW.argtypes = [c_void_p, c_uint, c_size_t, c_ssize_t]
        s.Shell_NotifyIconW.restype = BOOL
        s.Shell_NotifyIconW.argtypes = [DWORD, ctypes.POINTER(self._NID)]

        # For drawing a branded icon at runtime (no bundled .ico, no deps).
        u.GetDC.restype = c_void_p
        u.GetDC.argtypes = [c_void_p]
        u.ReleaseDC.argtypes = [c_void_p, c_void_p]
        u.FillRect.argtypes = [c_void_p, ctypes.POINTER(self._RECT), c_void_p]
        u.DrawTextW.restype = c_int
        u.DrawTextW.argtypes = [c_void_p, wintypes.LPCWSTR, c_int, ctypes.POINTER(self._RECT), c_uint]
        u.CreateIconIndirect.restype = c_void_p
        u.CreateIconIndirect.argtypes = [ctypes.POINTER(self._ICONINFO)]
        u.GetSystemMetrics.restype = c_int
        u.GetSystemMetrics.argtypes = [c_int]
        u.DestroyIcon.argtypes = [c_void_p]
        g = self._gdi32
        g.CreateCompatibleDC.restype = c_void_p
        g.CreateCompatibleDC.argtypes = [c_void_p]
        g.CreateCompatibleBitmap.restype = c_void_p
        g.CreateCompatibleBitmap.argtypes = [c_void_p, c_int, c_int]
        g.CreateBitmap.restype = c_void_p
        g.CreateBitmap.argtypes = [c_int, c_int, c_uint, c_uint, c_void_p]
        g.SelectObject.restype = c_void_p
        g.SelectObject.argtypes = [c_void_p, c_void_p]
        g.DeleteObject.argtypes = [c_void_p]
        g.DeleteDC.argtypes = [c_void_p]
        g.CreateSolidBrush.restype = c_void_p
        g.CreateSolidBrush.argtypes = [wintypes.COLORREF]
        g.SetTextColor.argtypes = [c_void_p, wintypes.COLORREF]
        g.SetBkMode.argtypes = [c_void_p, c_int]
        g.CreateFontW.restype = c_void_p
        g.CreateFontW.argtypes = [c_int] * 13 + [wintypes.LPCWSTR]
        g.PatBlt.argtypes = [c_void_p, c_int, c_int, c_int, c_int, DWORD]

    def _make_icon(self) -> int:
        """Draw a small branded icon at runtime: an accent square (the banner's
        rail colour) with a white 'S'. Pure GDI -- no bundled .ico, no deps.
        Falls back to the system application icon on any failure."""
        ctypes = self._ctypes
        u, g = self._user32, self._gdi32
        SM_CXSMICON, SM_CYSMICON = 49, 50
        TRANSPARENT, DEFAULT_CHARSET, CLEARTYPE_QUALITY = 1, 1, 5
        DT_CENTER, DT_VCENTER, DT_SINGLELINE = 0x1, 0x4, 0x20
        BLACKNESS = 0x42
        accent = 125 | (156 << 8) | (255 << 16)  # COLORREF 0x00BBGGRR -> rgb(125,156,255)
        white = 0x00FFFFFF
        try:
            w = u.GetSystemMetrics(SM_CXSMICON) or 16
            h = u.GetSystemMetrics(SM_CYSMICON) or 16
            screen = u.GetDC(None)
            memdc = g.CreateCompatibleDC(screen)
            color = g.CreateCompatibleBitmap(screen, w, h)
            mask = g.CreateBitmap(w, h, 1, 1, None)
            old_bmp = g.SelectObject(memdc, color)
            rect = self._RECT(0, 0, w, h)
            brush = g.CreateSolidBrush(accent)
            u.FillRect(memdc, ctypes.byref(rect), brush)
            g.DeleteObject(brush)
            g.SetBkMode(memdc, TRANSPARENT)
            g.SetTextColor(memdc, white)
            font = g.CreateFontW(-(h - 2), 0, 0, 0, 700, 0, 0, 0,
                                 DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, "Segoe UI")
            old_font = g.SelectObject(memdc, font)
            u.DrawTextW(memdc, "S", -1, ctypes.byref(rect), DT_CENTER | DT_VCENTER | DT_SINGLELINE)
            g.SelectObject(memdc, old_font)
            g.DeleteObject(font)
            # Opaque mask (all zero) so the whole square shows the colour bitmap.
            old_mask = g.SelectObject(memdc, mask)
            g.PatBlt(memdc, 0, 0, w, h, BLACKNESS)
            g.SelectObject(memdc, old_mask)
            g.SelectObject(memdc, old_bmp)
            info = self._ICONINFO()
            info.fIcon = True
            info.hbmMask = mask
            info.hbmColor = color
            hicon = u.CreateIconIndirect(ctypes.byref(info))
            g.DeleteObject(color)
            g.DeleteObject(mask)
            g.DeleteDC(memdc)
            u.ReleaseDC(None, screen)
            if hicon:
                return int(hicon)
        except OSError:
            logger.debug("custom tray icon failed; using system icon", exc_info=True)
        return int(u.LoadIconW(None, IDI_APPLICATION) or 0)

    def _create(self) -> None:
        ctypes = self._ctypes
        hinstance = self._kernel32.GetModuleHandleW(None)
        wndclass = self._WNDCLASS()
        self._wndproc = self._WNDPROC(self._on_message)  # keep a strong ref
        wndclass.lpfnWndProc = self._wndproc
        wndclass.hInstance = hinstance
        wndclass.lpszClassName = self._class_name
        if not self._user32.RegisterClassW(ctypes.byref(wndclass)):
            raise OSError(f"RegisterClassW failed: {ctypes.get_last_error()}")
        self._wndclass = wndclass
        # A hidden message window to receive the tray callback; never shown.
        self._hwnd = int(self._user32.CreateWindowExW(
            0, self._class_name, "SAI", WS_POPUP, 0, 0, 0, 0, None, None, hinstance, None) or 0)
        if not self._hwnd:
            raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")

        nid = self._NID()
        nid.cbSize = ctypes.sizeof(self._NID)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = self.CALLBACK
        self._hicon = self._make_icon()
        nid.hIcon = self._hicon
        nid.szTip = self._tooltip
        self._nid = nid
        self._added = bool(self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        if not self._added:
            logger.warning("Shell_NotifyIcon NIM_ADD failed: %s", ctypes.get_last_error())

    @property
    def added(self) -> bool:
        return self._added

    def _on_message(self, hwnd, message, wparam, lparam):
        if message == self.CALLBACK:
            event = lparam & 0xFFFF
            if event in (WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu()
            return 0
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _show_menu(self) -> None:
        ctypes = self._ctypes
        # Required so the popup dismisses when the user clicks elsewhere.
        self._user32.SetForegroundWindow(self._hwnd)
        hmenu = self._user32.CreatePopupMenu()
        try:
            for item in self._controller.items():
                if item.get("sep"):
                    self._user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                else:
                    flags = MF_STRING | (MF_CHECKED if item.get("checked") else 0)
                    self._user32.AppendMenuW(hmenu, flags, item["id"], item["label"])
            point = self._POINT()
            self._user32.GetCursorPos(ctypes.byref(point))
            cmd = self._user32.TrackPopupMenu(
                hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, self._hwnd, None)
        finally:
            self._user32.DestroyMenu(hmenu)
        # Win32 quirk: post a null message so the menu closes cleanly.
        self._user32.PostMessageW(self._hwnd, 0, 0, 0)
        if cmd:
            try:
                self._controller.invoke(int(cmd))
            except Exception:  # noqa: BLE001 - a menu action must never crash the overlay loop
                logger.exception("tray action failed")

    def close(self) -> None:
        ctypes = self._ctypes
        if self._added:
            try:
                self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except OSError:
                pass
            self._added = False
        if self._hicon:
            try:
                self._user32.DestroyIcon(self._hicon)
            except OSError:
                pass
            self._hicon = 0
        if self._hwnd:
            self._user32.DestroyWindow(self._hwnd)
            self._hwnd = 0
        try:
            self._user32.UnregisterClassW(self._class_name, self._kernel32.GetModuleHandleW(None))
        except OSError:
            pass
