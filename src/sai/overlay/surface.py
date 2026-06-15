"""What fills the banner's client area.

The host window (``window.OverlayWindow``) owns the hard part -- a topmost,
target-following layered window -- and is content-agnostic: it delegates "fill
this rectangle" to a ``Surface``. ``TextSurface`` draws one GDI line: an optional
brand logo (or an accent rail), the sponsor credit, and a dismiss affordance.
When richer ads are wanted, a ``WebView2Surface`` hosting an Edge WebView2
control in the same HWND slots in behind the same protocol.

The brand logo is fetched asynchronously (off the paint loop) through the
SSRF-guarded ``branding`` module and decoded/drawn with GDI+. Import-safe on
POSIX: all Win32/GDI/GDI+ is loaded lazily inside ``TextSurface``.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, List, Optional, Tuple

from . import assets, branding
from ..sponsors import display_url
from .win32 import is_windows


class Surface:
    def measure(self, card, dpi: int) -> Tuple[int, int]:
        """Desired (width, height) in physical pixels at the given DPI."""
        raise NotImplementedError

    def paint(self, hdc: int, width: int, height: int, card, dpi: int) -> None:
        """Fill the client area (``width`` x ``height``) for ``card``."""
        raise NotImplementedError

    def close_box(self, width: int, height: int, dpi: int) -> Tuple[int, int, int, int]:
        """The (x0, y0, x1, y1) hit rect of the dismiss control, for click routing."""
        raise NotImplementedError


def _rgb(color: Tuple[int, int, int]) -> int:
    r, g, b = color
    return r | (g << 8) | (b << 16)


def _argb(color: Tuple[int, int, int], alpha: int = 255) -> int:
    r, g, b = color
    a = max(0, min(255, alpha))
    return (a << 24) | (r << 16) | (g << 8) | b


class TextSurface(Surface):
    """A single-line sponsor banner drawn with GDI (+ GDI+ for the brand logo).

    Colours are opaque; the window applies whole-window translucency and rounds
    the corners, so the surface deals only with content.
    """

    CLOSE_GLYPH = "×"  # multiplication sign: a clean, always-available "x"
    INTERP_HQ_BICUBIC = 7

    def __init__(
        self,
        *,
        background: Tuple[int, int, int] = (24, 24, 27),
        foreground: Tuple[int, int, int] = (228, 228, 231),
        dim: Tuple[int, int, int] = (148, 148, 156),
        accent: Tuple[int, int, int] = (125, 156, 255),
        success: Tuple[int, int, int] = (88, 196, 125),
        font_face: str = "Segoe UI",
        point_size: int = 11,
        padding: int = 14,
        rail_width: int = 3,
        close_size: int = 18,
        height: int = 42,
        logo_margin: int = 6,
        logo_gap: int = 10,
        progress_gap: int = 12,
        progress_width: int = 22,
    ) -> None:
        self._bg = background
        self._fg = foreground
        self._dim = dim
        self._accent = accent
        self._success = success
        self._font_face = font_face
        self._point_size = point_size
        self._padding = padding
        self._rail_width = rail_width
        self._close_size = close_size
        self._height = height
        self._logo_margin = logo_margin
        self._logo_gap = logo_gap
        self._progress_gap = progress_gap
        self._progress_width = progress_width
        self._fonts: dict[int, int] = {}  # dpi -> HFONT

        # Brand-logo state (shared across the paint thread and fetch workers).
        self._logos: dict[str, Optional[int]] = {}  # url -> GpImage handle, or None if it failed
        self._fetching: set = set()
        self._lock = threading.Lock()
        self._repaint: Optional[Callable[[], None]] = None
        self._reward_progress: Optional[Tuple[float, bool]] = None
        # The example/preview cards' logo is a bundled local asset (not a fetch).
        self._example_handle: Optional[int] = None
        self._example_logo_tried = False

        self._gdiplus = None
        self._gdip_token = None
        if is_windows():
            import ctypes

            self._ctypes = ctypes
            self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._setup_prototypes()
            self._setup_gdiplus()
        else:  # pragma: no cover - exercised only on Windows
            self._ctypes = None

    def set_repaint(self, callback: Callable[[], None]) -> None:
        """The window wires this so the surface can ask for a repaint once an
        asynchronously-fetched logo is ready."""
        self._repaint = callback

    def set_reward_progress(self, progress: Optional[dict[str, Any]]) -> None:
        state = self._normalise_reward_progress(progress)
        if state == self._reward_progress:
            return
        self._reward_progress = state
        if self._repaint is not None:
            self._repaint()

    # -- GDI plumbing -------------------------------------------------------

    def _setup_prototypes(self) -> None:
        ctypes = self._ctypes
        c_void_p, c_int, c_wchar_p = ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p
        import ctypes.wintypes as wintypes

        class SIZE(ctypes.Structure):
            _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        self._SIZE = SIZE
        self._RECT = RECT

        g, u = self._gdi32, self._user32
        g.CreateFontW.restype = c_void_p
        g.CreateFontW.argtypes = [c_int] * 13 + [c_wchar_p]
        g.CreateSolidBrush.restype = c_void_p
        g.CreateSolidBrush.argtypes = [wintypes.COLORREF]
        g.SelectObject.restype = c_void_p
        g.SelectObject.argtypes = [c_void_p, c_void_p]
        g.DeleteObject.argtypes = [c_void_p]
        g.SetBkMode.argtypes = [c_void_p, c_int]
        g.SetTextColor.argtypes = [c_void_p, wintypes.COLORREF]
        g.TextOutW.argtypes = [c_void_p, c_int, c_int, c_wchar_p, c_int]
        g.GetTextExtentPoint32W.argtypes = [c_void_p, c_wchar_p, c_int, ctypes.POINTER(SIZE)]
        u.FillRect.argtypes = [c_void_p, ctypes.POINTER(RECT), c_void_p]
        u.GetDC.restype = c_void_p
        u.GetDC.argtypes = [c_void_p]
        u.ReleaseDC.argtypes = [c_void_p, c_void_p]

    def _setup_gdiplus(self) -> None:
        # GDI+ exposes a flat C API (no COM vtables), so it is ctypes-friendly.
        ctypes = self._ctypes
        c_void_p = ctypes.c_void_p
        c_int, c_uint = ctypes.c_int, ctypes.c_uint
        c_float, c_wchar_p = ctypes.c_float, ctypes.c_wchar_p
        try:
            gp = ctypes.WinDLL("gdiplus", use_last_error=True)

            class GdiplusStartupInput(ctypes.Structure):
                _fields_ = [("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", c_void_p),
                            ("SuppressBackgroundThread", c_int), ("SuppressExternalCodecs", c_int)]

            gp.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(GdiplusStartupInput), c_void_p]
            gp.GdiplusStartup.restype = c_uint
            gp.GdiplusShutdown.argtypes = [ctypes.c_size_t]
            gp.GdipCreateBitmapFromFile.argtypes = [c_wchar_p, ctypes.POINTER(c_void_p)]
            gp.GdipCreateBitmapFromFile.restype = c_uint
            gp.GdipDisposeImage.argtypes = [c_void_p]
            gp.GdipGetImageWidth.argtypes = [c_void_p, ctypes.POINTER(c_uint)]
            gp.GdipGetImageHeight.argtypes = [c_void_p, ctypes.POINTER(c_uint)]
            gp.GdipCreateFromHDC.argtypes = [c_void_p, ctypes.POINTER(c_void_p)]
            gp.GdipCreateFromHDC.restype = c_uint
            gp.GdipDeleteGraphics.argtypes = [c_void_p]
            gp.GdipSetInterpolationMode.argtypes = [c_void_p, c_int]
            gp.GdipSetSmoothingMode.argtypes = [c_void_p, c_int]
            gp.GdipCreateSolidFill.argtypes = [ctypes.c_uint32, ctypes.POINTER(c_void_p)]
            gp.GdipCreateSolidFill.restype = c_uint
            gp.GdipDeleteBrush.argtypes = [c_void_p]
            gp.GdipFillEllipseI.argtypes = [c_void_p, c_void_p, c_int, c_int, c_int, c_int]
            gp.GdipFillEllipseI.restype = c_uint
            gp.GdipFillPieI.argtypes = [
                c_void_p, c_void_p, c_int, c_int, c_int, c_int, c_float, c_float
            ]
            gp.GdipFillPieI.restype = c_uint
            gp.GdipDrawImageRectI.argtypes = [c_void_p, c_void_p, c_int, c_int, c_int, c_int]
            gp.GdipDrawImageRectI.restype = c_uint

            token = ctypes.c_size_t(0)
            startup = GdiplusStartupInput(1, None, 0, 0)
            if gp.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None) != 0:
                return
            self._gdiplus = gp
            self._gdip_token = token
        except OSError:  # pragma: no cover - gdiplus missing is not expected
            self._gdiplus = None

    def _scaled(self, value: int, dpi: int) -> int:
        return max(1, round(value * dpi / 96))

    def _font(self, dpi: int) -> int:
        cached = self._fonts.get(dpi)
        if cached:
            return cached
        cheight = -max(1, round(self._point_size * dpi / 72))
        CLEARTYPE_QUALITY = 5
        DEFAULT_CHARSET = 1
        hfont = self._gdi32.CreateFontW(
            cheight, 0, 0, 0, 400, 0, 0, 0,
            DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, self._font_face,
        )
        self._fonts[dpi] = hfont
        return hfont

    def _text_extent(self, hdc: int, text: str) -> Tuple[int, int]:
        size = self._SIZE()
        self._gdi32.GetTextExtentPoint32W(hdc, text, len(text), self._ctypes.byref(size))
        return size.cx, size.cy

    def _normalise_reward_progress(
        self, progress: Optional[dict[str, Any]]
    ) -> Optional[Tuple[float, bool]]:
        if not progress:
            return None
        try:
            visible = max(0.0, float(progress.get("visible_seconds") or 0.0))
            remaining = max(0.0, float(progress.get("remaining_seconds") or 0.0))
            raw_progress = progress.get("progress")
            if raw_progress is None:
                total = visible + remaining
                amount = visible / total if total > 0 else 0.0
            else:
                amount = float(raw_progress)
        except (TypeError, ValueError):
            return None
        amount = min(1.0, max(0.0, amount))
        eligible = bool(progress.get("eligible")) or remaining <= 0.0 or amount >= 1.0
        return (1.0 if eligible else amount, eligible)

    # -- brand logo (async fetch + GDI+ decode) -----------------------------

    def _logo_box(self, dpi: int) -> int:
        return max(1, self._scaled(self._height, dpi) - 2 * self._scaled(self._logo_margin, dpi))

    def _has_logo(self, card) -> bool:
        if self._gdiplus is None:
            return False
        return bool(getattr(card, "brand_icon_url", None) or getattr(card, "is_example", False))

    def _example_logo(self) -> Optional[int]:
        # Example/preview cards (no backend placement) show the bundled SAI mark
        # from a local asset -- never a network fetch, and never a path the
        # backend can influence.
        if self._gdiplus is None:
            return None
        if not self._example_logo_tried:
            self._example_logo_tried = True
            self._example_handle = self._decode(assets.sai_mark_png())
        return self._example_handle

    def _logo_handle(self, card) -> Optional[int]:
        if self._gdiplus is None:
            return None
        url = getattr(card, "brand_icon_url", None)
        if url:
            return self._ensure_logo(url)  # real placements: HTTPS + SSRF-guarded fetch
        if getattr(card, "is_example", False):
            return self._example_logo()    # example cards: bundled local asset only
        return None

    def _ensure_logo(self, url: str) -> Optional[int]:
        """Return a ready GpImage handle for ``url``, or None (and kick off a
        one-shot background fetch the first time)."""
        with self._lock:
            if url in self._logos:
                return self._logos[url]
            if url in self._fetching:
                return None
            self._fetching.add(url)
        thread = threading.Thread(target=self._load_logo_worker, args=(url,), daemon=True)
        thread.start()
        return None

    def _load_logo_worker(self, url: str) -> None:
        handle: Optional[int] = None
        try:
            data = branding.fetch_icon(url)
            if data:
                handle = self._decode(data)
        finally:
            with self._lock:
                self._logos[url] = handle
                self._fetching.discard(url)
        if self._repaint is not None:
            try:
                self._repaint()
            except Exception:  # noqa: BLE001 - a repaint hint must never crash the worker
                pass

    def _decode(self, data: bytes) -> Optional[int]:
        import os
        import tempfile

        ctypes = self._ctypes
        path = None
        try:
            fd, path = tempfile.mkstemp(suffix=".img")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            image = ctypes.c_void_p()
            if self._gdiplus.GdipCreateBitmapFromFile(path, ctypes.byref(image)) != 0 or not image.value:
                return None
            return int(image.value)
        except OSError:
            return None
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _draw_logo(self, hdc: int, handle: int, height: int, dpi: int) -> None:
        ctypes = self._ctypes
        graphics = ctypes.c_void_p()
        if self._gdiplus.GdipCreateFromHDC(hdc, ctypes.byref(graphics)) != 0 or not graphics.value:
            return
        try:
            self._gdiplus.GdipSetInterpolationMode(graphics, self.INTERP_HQ_BICUBIC)
            box = self._logo_box(dpi)
            iw, ih = ctypes.c_uint(0), ctypes.c_uint(0)
            self._gdiplus.GdipGetImageWidth(handle, ctypes.byref(iw))
            self._gdiplus.GdipGetImageHeight(handle, ctypes.byref(ih))
            # Fit within the square box, preserving aspect ratio (no distortion).
            if iw.value and ih.value:
                scale = min(box / iw.value, box / ih.value)
                dw, dh = max(1, int(iw.value * scale)), max(1, int(ih.value * scale))
            else:
                dw = dh = box
            x0 = self._scaled(8, dpi)
            y0 = max(0, (height - box) // 2)
            dx = x0 + (box - dw) // 2
            dy = y0 + (box - dh) // 2
            self._gdiplus.GdipDrawImageRectI(graphics, handle, dx, dy, dw, dh)
        finally:
            self._gdiplus.GdipDeleteGraphics(graphics)

    # -- content ------------------------------------------------------------

    def _segments(self, card) -> List[Tuple[str, Tuple[int, int, int]]]:
        segments: List[Tuple[str, Tuple[int, int, int]]] = [
            ("sponsored   ", self._dim),
            (card.sponsor, self._accent),
        ]
        if card.message:
            segments.append(("    " + card.message, self._fg))
        host = display_url(card.url) if card.url else ""
        if host:
            segments.append(("    " + host, self._accent))
        return segments

    def _left_offset(self, card, dpi: int) -> int:
        # When the card carries a logo, reserve a square slot at the left (so the
        # layout is stable whether or not the logo has finished loading); else
        # reserve the thin accent rail.
        if self._has_logo(card):
            return self._scaled(8, dpi) + self._logo_box(dpi) + self._scaled(self._logo_gap, dpi)
        return self._scaled(self._rail_width, dpi) + self._scaled(self._padding, dpi)

    def close_box(self, width: int, height: int, dpi: int) -> Tuple[int, int, int, int]:
        pad = self._scaled(self._padding, dpi)
        size = self._scaled(self._close_size, dpi)
        x1 = width - pad
        x0 = x1 - size
        cy = height // 2
        return (x0, cy - size // 2, x1, cy + size // 2)

    def measure(self, card, dpi: int) -> Tuple[int, int]:
        pad = self._scaled(self._padding, dpi)
        size = self._scaled(self._close_size, dpi)
        hdc = self._user32.GetDC(None)
        old = self._gdi32.SelectObject(hdc, self._font(dpi))
        try:
            text = sum(self._text_extent(hdc, seg)[0] for seg, _ in self._segments(card))
        finally:
            self._gdi32.SelectObject(hdc, old)
            self._user32.ReleaseDC(None, hdc)
        progress = 0
        if self._reward_progress is not None:
            progress = self._scaled(self._progress_gap, dpi) + self._scaled(self._progress_width, dpi)
        width = self._left_offset(card, dpi) + text + pad + progress + size + pad
        return width, self._scaled(self._height, dpi)

    def _draw_reward_progress(self, hdc: int, width: int, height: int, dpi: int) -> None:
        if self._reward_progress is None or self._gdiplus is None:
            return
        progress, eligible = self._reward_progress
        slot = self._scaled(self._progress_width, dpi)
        gap = self._scaled(self._progress_gap, dpi)
        close_x0, _, _, _ = self.close_box(width, height, dpi)
        size = min(slot, max(1, height - 2 * self._scaled(9, dpi)))
        x0 = close_x0 - gap - slot + max(0, (slot - size) // 2)
        y0 = max(0, (height - size) // 2)
        ring = max(2, size // 5)
        inner = max(1, size - 2 * ring)

        ctypes = self._ctypes
        gp = self._gdiplus
        graphics = ctypes.c_void_p()
        if gp.GdipCreateFromHDC(hdc, ctypes.byref(graphics)) != 0 or not graphics.value:
            return

        base = ctypes.c_void_p()
        fill = ctypes.c_void_p()
        cutout = ctypes.c_void_p()
        try:
            # Anti-aliased edges make the small ring look continuous rather than
            # stepping harshly as the fill advances.
            gp.GdipSetSmoothingMode(graphics, 4)
            if gp.GdipCreateSolidFill(_argb(self._dim, 70), ctypes.byref(base)) != 0:
                return
            color = self._success if eligible else self._accent
            if gp.GdipCreateSolidFill(_argb(color), ctypes.byref(fill)) != 0:
                return
            if gp.GdipCreateSolidFill(_argb(self._bg), ctypes.byref(cutout)) != 0:
                return

            gp.GdipFillEllipseI(graphics, base, x0, y0, size, size)
            if progress > 0:
                gp.GdipFillPieI(
                    graphics, fill, x0, y0, size, size,
                    self._ctypes.c_float(-90.0),
                    self._ctypes.c_float(360.0 * progress),
                )
            gp.GdipFillEllipseI(
                graphics, cutout,
                x0 + ring, y0 + ring, inner, inner,
            )
        finally:
            for brush in (base, fill, cutout):
                if brush.value:
                    gp.GdipDeleteBrush(brush)
            gp.GdipDeleteGraphics(graphics)

    def paint(self, hdc: int, width: int, height: int, card, dpi: int) -> None:
        if card is None:
            return
        ctypes = self._ctypes
        TRANSPARENT = 1

        bg = self._gdi32.CreateSolidBrush(_rgb(self._bg))
        full = self._RECT(0, 0, width, height)
        self._user32.FillRect(hdc, ctypes.byref(full), bg)
        self._gdi32.DeleteObject(bg)

        # Left marker: the brand logo if it has loaded, otherwise the accent rail
        # (which also serves as a loading placeholder while the logo is fetched).
        logo = self._logo_handle(card)
        if logo:
            self._draw_logo(hdc, logo, height, dpi)
        else:
            rail = self._gdi32.CreateSolidBrush(_rgb(self._accent))
            rail_rect = self._RECT(0, 0, self._scaled(self._rail_width, dpi), height)
            self._user32.FillRect(hdc, ctypes.byref(rail_rect), rail)
            self._gdi32.DeleteObject(rail)

        self._gdi32.SetBkMode(hdc, TRANSPARENT)
        old = self._gdi32.SelectObject(hdc, self._font(dpi))
        try:
            _, text_h = self._text_extent(hdc, "Ag")
            y = max(0, (height - text_h) // 2)
            x = self._left_offset(card, dpi)
            for text, color in self._segments(card):
                self._gdi32.SetTextColor(hdc, _rgb(color))
                self._gdi32.TextOutW(hdc, x, y, text, len(text))
                x += self._text_extent(hdc, text)[0]

            self._draw_reward_progress(hdc, width, height, dpi)
            x0, _, x1, _ = self.close_box(width, height, dpi)
            gw, gh = self._text_extent(hdc, self.CLOSE_GLYPH)
            self._gdi32.SetTextColor(hdc, _rgb(self._dim))
            self._gdi32.TextOutW(
                hdc, x0 + ((x1 - x0) - gw) // 2, max(0, (height - gh) // 2),
                self.CLOSE_GLYPH, len(self.CLOSE_GLYPH),
            )
        finally:
            self._gdi32.SelectObject(hdc, old)

    def dispose(self) -> None:
        for hfont in self._fonts.values():
            if hfont:
                self._gdi32.DeleteObject(hfont)
        self._fonts.clear()
        if self._gdiplus is not None:
            with self._lock:
                handles = [h for h in self._logos.values() if h]
                self._logos.clear()
            if self._example_handle:
                handles.append(self._example_handle)
                self._example_handle = None
            for handle in handles:
                self._gdiplus.GdipDisposeImage(handle)
            if self._gdip_token is not None:
                self._gdiplus.GdiplusShutdown(self._gdip_token)
                self._gdip_token = None
