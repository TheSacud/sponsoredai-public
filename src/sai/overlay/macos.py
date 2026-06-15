"""macOS backend for the desktop overlay.

The Windows backend uses Win32 HWNDs, GDI and the notification area. This module
keeps the same public shape for macOS using AppKit for the non-activating banner
window and Quartz/AppKit for visibility checks. Imports are deliberately lazy so
the package remains importable on machines without PyObjC.
"""

from __future__ import annotations

import logging
import sys
import threading
import weakref
from typing import Any, Callable, Optional, Tuple

from ..sponsors import display_url
from . import assets, branding
from .geometry import Placement
from .win32 import Rect, SystemProbe

logger = logging.getLogger(__name__)

_OVERLAYS: dict[int, weakref.ReferenceType["MacOverlayWindow"]] = {}
_BANNER_VIEW_CLASS = None
_STATUS_TARGET_CLASS = None


def is_macos() -> bool:
    return sys.platform == "darwin"


def enable_dpi_awareness() -> None:
    """No-op parity with the Win32 backend."""


def _appkit():
    if not is_macos():
        raise RuntimeError("macOS overlay backend is only available on macOS")
    import AppKit  # type: ignore
    import Foundation  # type: ignore

    return AppKit, Foundation


def _ensure_app():
    AppKit, _Foundation = _appkit()
    app = AppKit.NSApplication.sharedApplication()
    policy = getattr(AppKit, "NSApplicationActivationPolicyAccessory", 1)
    app.setActivationPolicy_(policy)
    return app


def _screen_top_of(screens) -> float:
    if not screens:
        return 0.0
    return max(float(s.frame().origin.y + s.frame().size.height) for s in screens)


def _screen_top() -> float:
    AppKit, _Foundation = _appkit()
    return _screen_top_of(list(AppKit.NSScreen.screens() or []))


def _frame_to_top_left_rect(frame, top: Optional[float] = None) -> Rect:
    # ``top`` (the global top edge across all screens, used to flip AppKit's
    # bottom-left origin to our top-left coordinates) is the same for every frame,
    # so a caller resolving several frames in one pass computes it once and passes
    # it in instead of re-enumerating NSScreen.screens() per frame.
    if top is None:
        top = _screen_top()
    left = int(round(frame.origin.x))
    right = int(round(frame.origin.x + frame.size.width))
    y_top = int(round(top - (frame.origin.y + frame.size.height)))
    y_bottom = int(round(top - frame.origin.y))
    return Rect(left, y_top, right, y_bottom)


def _placement_to_frame(placement: Placement):
    AppKit, _Foundation = _appkit()
    top = _screen_top()
    y = top - placement.y - placement.height
    return AppKit.NSMakeRect(
        placement.x, y, placement.width, placement.height
    )


def _overlay(handle: int) -> Optional["MacOverlayWindow"]:
    ref = _OVERLAYS.get(int(handle))
    return ref() if ref is not None else None


def _screen_number(screen, fallback: int) -> int:
    try:
        number = screen.deviceDescription().objectForKey_("NSScreenNumber")
        return int(number)
    except Exception:  # noqa: BLE001 - display id is best-effort only
        return fallback


def _screen_rects() -> list[tuple[int, Rect, Any]]:
    AppKit, _Foundation = _appkit()
    screens = list(AppKit.NSScreen.screens() or [])
    # Compute the global top edge ONCE for the whole table; the previous code
    # recomputed it (a full NSScreen.screens() re-enumeration) inside every
    # _frame_to_top_left_rect call, making this O(N^2) in the screen count.
    top = _screen_top_of(screens)
    return [
        (_screen_number(screen, idx), _frame_to_top_left_rect(screen.frame(), top), screen)
        for idx, screen in enumerate(screens, start=1)
    ]


def _rect_center(rect: Rect) -> tuple[float, float]:
    return (rect.left + rect.width / 2.0, rect.top + rect.height / 2.0)


def _contains(rect: Rect, point: tuple[float, float]) -> bool:
    x, y = point
    return rect.left <= x < rect.right and rect.top <= y < rect.bottom


def _screen_for_rect(rect: Rect, screens=None):
    center = _rect_center(rect)
    if screens is None:
        screens = _screen_rects()
    for number, srect, screen in screens:
        if _contains(srect, center):
            return number, screen
    if screens:
        return screens[0][0], screens[0][2]
    return 0, None


def _make_banner_view_class():
    global _BANNER_VIEW_CLASS
    if _BANNER_VIEW_CLASS is not None:
        return _BANNER_VIEW_CLASS

    AppKit, _Foundation = _appkit()
    import objc  # type: ignore

    class SaiBannerView(AppKit.NSView):  # type: ignore[misc]
        def initWithOwner_(self, owner):
            self = objc.super(SaiBannerView, self).init()
            if self is None:
                return None
            self.owner = owner
            return self

        def isFlipped(self):
            return True

        def drawRect_(self, _dirty):
            self.owner._draw()

        def mouseUp_(self, event):
            self.owner._mouse_up(event)

    _BANNER_VIEW_CLASS = SaiBannerView
    return SaiBannerView


def _make_status_target_class():
    global _STATUS_TARGET_CLASS
    if _STATUS_TARGET_CLASS is not None:
        return _STATUS_TARGET_CLASS

    _AppKit, Foundation = _appkit()
    import objc  # type: ignore

    class SaiStatusTarget(Foundation.NSObject):  # type: ignore[misc]
        def initWithOwner_(self, owner):
            self = objc.super(SaiStatusTarget, self).init()
            if self is None:
                return None
            self.owner = owner
            return self

        def action_(self, sender):
            self.owner._invoke(sender.tag())

    _STATUS_TARGET_CLASS = SaiStatusTarget
    return SaiStatusTarget


class MacOSProbe:
    """Live SystemProbe for macOS.

    Handles are process ids for target apps and registered synthetic ids for the
    overlay panel. All queries fail closed for billing.
    """

    def __init__(self) -> None:
        if not is_macos():
            raise RuntimeError("MacOSProbe is only available on macOS")
        self._AppKit, self._Foundation = _appkit()
        import Quartz  # type: ignore

        self._Quartz = Quartz
        # Per-tick snapshots: a single VisibilityMonitor.sample() + driver
        # placement resolves the target window rect and screen up to ~3x within
        # one 5Hz tick. begin_tick() captures the two system-wide queries (the
        # CGWindowList enumeration and the NSScreen list) once so the whole tick
        # shares them; end_tick() drops them so off-tick callers (click handlers,
        # tests) fall back to live queries.
        self._tick_windows: Optional[list] = None
        self._tick_screens: Optional[list] = None
        self._tick_screen_top: Optional[float] = None
        # pid -> (NSRunningApplication, executable path). The path is immutable for
        # a single process's lifetime, but macOS recycles PIDs, so caching by pid
        # alone would keep returning a dead app's path once its pid is reused. We
        # keep the app object too: a terminated app reports isTerminated (a cheap
        # property read), so a recycled pid is detected and re-resolved instead of
        # placing/billing the overlay over the wrong window.
        self._pid_path_cache: dict[int, tuple[Any, str]] = {}

    def begin_tick(self) -> None:
        self._tick_windows = self._list_windows()
        AppKit = self._AppKit
        screens = list(AppKit.NSScreen.screens() or [])
        top = _screen_top_of(screens)
        self._tick_screen_top = top
        self._tick_screens = [
            (_screen_number(screen, idx), _frame_to_top_left_rect(screen.frame(), top), screen)
            for idx, screen in enumerate(screens, start=1)
        ]

    def end_tick(self) -> None:
        self._tick_windows = None
        self._tick_screens = None
        self._tick_screen_top = None

    def _screens(self) -> list:
        return self._tick_screens if self._tick_screens is not None else _screen_rects()

    def _list_windows(self) -> list:
        try:
            q = self._Quartz
            return list(
                q.CGWindowListCopyWindowInfo(
                    q.kCGWindowListOptionOnScreenOnly, q.kCGNullWindowID
                )
                or []
            )
        except Exception:  # noqa: BLE001 - fail closed
            return []

    def foreground_window(self) -> int:
        try:
            app = self._AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            return int(app.processIdentifier()) if app is not None else 0
        except Exception:  # noqa: BLE001 - fail closed
            return 0

    def process_image_path(self, hwnd: int) -> Optional[str]:
        if not hwnd:
            return None
        if _overlay(hwnd) is not None:
            return None
        pid = int(hwnd)
        cached = self._pid_path_cache.get(pid)
        if cached is not None:
            app, path = cached
            try:
                alive = not bool(app.isTerminated())
            except Exception:  # noqa: BLE001 - fail closed: re-resolve if liveness is unprovable
                alive = False
            if alive:
                return path
            # App gone (and its pid may have been recycled onto another process):
            # drop the stale entry and re-resolve below.
            self._pid_path_cache.pop(pid, None)
        try:
            app = self._AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is None:
                return None
            url = app.executableURL() or app.bundleURL()
            path = str(url.path()) if url is not None else None
        except Exception:  # noqa: BLE001 - fail closed
            return None
        if path:
            # Only cache a real path: caching None would suppress retries of a
            # transiently-unresolvable foreground app and break fail-closed.
            self._pid_path_cache[pid] = (app, path)
        return path

    def _bounds_for_pid(self, pid: int) -> Optional[dict]:
        windows = self._tick_windows
        if windows is None:
            windows = self._list_windows()
        q = self._Quartz
        for info in windows:
            try:
                if int(info.get(q.kCGWindowOwnerPID, 0)) != int(pid):
                    continue
                if int(info.get(q.kCGWindowLayer, 0)) != 0:
                    continue
                bounds = info.get(q.kCGWindowBounds) or {}
                if float(bounds.get("Width", 0)) <= 0 or float(bounds.get("Height", 0)) <= 0:
                    continue
                # Bridge only the small bounds dict, not the entire window
                # CFDictionary (owner name, title, memory, ... — all unused).
                return dict(bounds)
            except Exception:  # noqa: BLE001 - ignore one malformed row
                continue
        return None

    def _rect_from_bounds(self, bounds: dict) -> Optional[Rect]:
        try:
            left = int(round(float(bounds.get("X", 0))))
            top = int(round(float(bounds.get("Y", 0))))
            width = int(round(float(bounds.get("Width", 0))))
            height = int(round(float(bounds.get("Height", 0))))
            if width <= 0 or height <= 0:
                return None
            return Rect(left, top, left + width, top + height)
        except Exception:  # noqa: BLE001 - fail closed
            return None

    def window_rect(self, hwnd: int) -> Optional[Rect]:
        overlay = _overlay(hwnd)
        if overlay is not None:
            return overlay.rect
        if not hwnd:
            return None
        bounds = self._bounds_for_pid(int(hwnd))
        return self._rect_from_bounds(bounds) if bounds is not None else None

    def is_window_visible(self, hwnd: int) -> bool:
        overlay = _overlay(hwnd)
        if overlay is not None:
            return overlay.visible
        return self.window_rect(hwnd) is not None

    def is_minimized(self, hwnd: int) -> bool:
        overlay = _overlay(hwnd)
        if overlay is not None:
            return not overlay.visible
        return self.window_rect(hwnd) is None

    def is_cloaked(self, hwnd: int) -> bool:
        # CGWindowListOptionOnScreenOnly already excludes hidden/minimized/other
        # Space target windows. The overlay panel is explicitly ordered in/out.
        return not self.is_window_visible(hwnd)

    def monitor_of(self, hwnd: int) -> int:
        rect = self.window_rect(hwnd)
        if rect is None:
            return 0
        number, _screen = _screen_for_rect(rect, self._screens())
        return int(number)

    def monitor_work_area(self, hwnd: int) -> Optional[Rect]:
        rect = self.window_rect(hwnd)
        if rect is None:
            return None
        _number, screen = _screen_for_rect(rect, self._screens())
        if screen is None:
            return None
        try:
            return _frame_to_top_left_rect(screen.visibleFrame(), self._tick_screen_top)
        except Exception:  # noqa: BLE001 - fail closed
            return None

    def monitor_dpi(self, hwnd: int) -> int:
        # AppKit frames and our macOS surface are in logical points; keep the
        # existing DPI-scaled layout at its 96-DPI base size.
        return 96 if hwnd else 0

    def idle_seconds(self) -> float:
        try:
            q = self._Quartz
            return float(q.CGEventSourceSecondsSinceLastEventType(
                q.kCGEventSourceStateHIDSystemState,
                q.kCGAnyInputEventType,
            ))
        except Exception:  # noqa: BLE001 - fail closed
            return float("inf")


def default_probe() -> SystemProbe:
    if not is_macos():
        raise RuntimeError("The macOS system probe is only available on macOS")
    return MacOSProbe()


def autorelease_pool():
    """Context manager draining one autorelease pool per overlay loop iteration.

    The driver runs its own 5Hz loop rather than -[NSApplication run], and
    pump()'s nextEventMatchingMask: does not drain a pool, so the per-tick
    CGWindowList / NSString / NSColor / NSFont temporaries would accumulate for
    the whole (always-on) process lifetime. Wrapping each iteration bounds that
    growth. objc is imported lazily to keep the package importable without PyObjC.
    """
    import objc  # type: ignore

    return objc.autorelease_pool()


class MacTextSurface:
    CLOSE_GLYPH = "x"

    def __init__(
        self,
        *,
        background: Tuple[int, int, int] = (24, 24, 27),
        foreground: Tuple[int, int, int] = (228, 228, 231),
        dim: Tuple[int, int, int] = (148, 148, 156),
        accent: Tuple[int, int, int] = (125, 156, 255),
        success: Tuple[int, int, int] = (88, 196, 125),
        point_size: int = 11,
        padding: int = 14,
        rail_width: int = 3,
        close_size: int = 18,
        height: int = 42,
        logo_margin: int = 6,
        logo_gap: int = 10,
        progress_gap: int = 12,
        progress_width: int = 22,
        corner_radius: int = 10,
    ) -> None:
        self._AppKit, self._Foundation = _appkit()
        self._bg = background
        self._fg = foreground
        self._dim = dim
        self._accent = accent
        self._success = success
        self._point_size = point_size
        self._padding = padding
        self._rail_width = rail_width
        self._close_size = close_size
        self._height = height
        self._logo_margin = logo_margin
        self._logo_gap = logo_gap
        self._progress_gap = progress_gap
        self._progress_width = progress_width
        self._corner_radius = corner_radius
        self._reward_progress: Optional[Tuple[float, bool]] = None
        self._repaint: Optional[Callable[[], None]] = None
        # measure() (every tick) and draw() re-derive immutable CoreText objects
        # for the same text/colour/dpi; cache them so each is built once. Fonts and
        # colours are immutable for this surface's fixed theme, so no invalidation
        # is needed beyond dispose(); the Win32 surface caches its HFONT the same way.
        self._font_cache: dict[int, Any] = {}
        self._attrs_cache: dict[tuple, dict] = {}
        self._text_size_cache: dict[tuple[str, int], Tuple[int, int]] = {}
        self._logo_data: dict[str, Optional[bytes]] = {}
        self._logo_images: dict[str, Any] = {}
        self._fetching: set[str] = set()
        self._lock = threading.Lock()
        self._example_image = None
        self._example_tried = False

    def set_repaint(self, callback: Callable[[], None]) -> None:
        self._repaint = callback

    def set_reward_progress(self, progress: Optional[dict[str, Any]]) -> None:
        state = self._normalise_reward_progress(progress)
        if state == self._reward_progress:
            return
        self._reward_progress = state
        if self._repaint is not None:
            self._repaint()

    def _scaled(self, value: int, dpi: int) -> int:
        return max(1, round(value * dpi / 96))

    def _font(self, dpi: int):
        font = self._font_cache.get(dpi)
        if font is None:
            font = self._AppKit.NSFont.systemFontOfSize_(self._scaled(self._point_size, dpi))
            self._font_cache[dpi] = font
        return font

    def _color(self, rgb: Tuple[int, int, int], alpha: float = 1.0):
        r, g, b = rgb
        return self._AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
            r / 255.0, g / 255.0, b / 255.0, alpha
        )

    def _attrs(self, color: Tuple[int, int, int], dpi: int):
        key = (color, dpi)
        attrs = self._attrs_cache.get(key)
        if attrs is None:
            attrs = {
                self._AppKit.NSFontAttributeName: self._font(dpi),
                self._AppKit.NSForegroundColorAttributeName: self._color(color),
            }
            self._attrs_cache[key] = attrs
        return attrs

    def _text_size(self, text: str, dpi: int) -> Tuple[int, int]:
        key = (text, dpi)
        cached = self._text_size_cache.get(key)
        if cached is not None:
            return cached
        size = self._Foundation.NSString.stringWithString_(text).sizeWithAttributes_(
            self._attrs(self._fg, dpi)
        )
        result = (int(round(size.width)), int(round(size.height)))
        self._text_size_cache[key] = result
        return result

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

    def _segments(self, card) -> list[tuple[str, Tuple[int, int, int]]]:
        segments: list[tuple[str, Tuple[int, int, int]]] = [
            ("sponsored   ", self._dim),
            (card.sponsor, self._accent),
        ]
        if card.message:
            segments.append(("    " + card.message, self._fg))
        host = display_url(card.url) if card.url else ""
        if host:
            segments.append(("    " + host, self._accent))
        return segments

    def _logo_box(self, dpi: int) -> int:
        return max(1, self._scaled(self._height, dpi) - 2 * self._scaled(self._logo_margin, dpi))

    def _has_logo(self, card) -> bool:
        return bool(getattr(card, "brand_icon_url", None) or getattr(card, "is_example", False))

    def _left_offset(self, card, dpi: int) -> int:
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
        close = self._scaled(self._close_size, dpi)
        text = sum(self._text_size(seg, dpi)[0] for seg, _color in self._segments(card))
        progress = 0
        if self._reward_progress is not None:
            progress = self._scaled(self._progress_gap, dpi) + self._scaled(self._progress_width, dpi)
        return (
            self._left_offset(card, dpi) + text + pad + progress + close + pad,
            self._scaled(self._height, dpi),
        )

    def _data_to_image(self, data: bytes):
        nsdata = self._Foundation.NSData.dataWithBytes_length_(data, len(data))
        image = self._AppKit.NSImage.alloc().initWithData_(nsdata)
        return image if image is not None and image.isValid() else None

    def _example_logo(self):
        if not self._example_tried:
            self._example_tried = True
            self._example_image = self._data_to_image(assets.sai_mark_png())
        return self._example_image

    def _logo_image(self, card):
        url = getattr(card, "brand_icon_url", None)
        if url:
            if url in self._logo_images:
                return self._logo_images[url]
            data = self._ensure_logo_data(url)
            if data:
                image = self._data_to_image(data)
                self._logo_images[url] = image
                return image
            return None
        if getattr(card, "is_example", False):
            return self._example_logo()
        return None

    def _ensure_logo_data(self, url: str) -> Optional[bytes]:
        with self._lock:
            if url in self._logo_data:
                return self._logo_data[url]
            if url not in self._fetching:
                self._fetching.add(url)
                thread = threading.Thread(target=self._load_logo_worker, args=(url,), daemon=True)
                thread.start()
        return None

    def _load_logo_worker(self, url: str) -> None:
        data: Optional[bytes] = None
        try:
            data = branding.fetch_icon(url)
        finally:
            with self._lock:
                self._logo_data[url] = data
                self._fetching.discard(url)
        if self._repaint is not None:
            try:
                self._repaint()
            except Exception:  # noqa: BLE001
                pass

    def _draw_text(self, text: str, x: int, y: int, color: Tuple[int, int, int], dpi: int) -> int:
        ns = self._Foundation.NSString.stringWithString_(text)
        ns.drawAtPoint_withAttributes_(self._AppKit.NSMakePoint(x, y), self._attrs(color, dpi))
        return self._text_size(text, dpi)[0]

    def _draw_logo(self, image, height: int, dpi: int) -> None:
        if image is None:
            return
        box = self._logo_box(dpi)
        size = image.size()
        if size.width > 0 and size.height > 0:
            scale = min(box / float(size.width), box / float(size.height))
            dw, dh = max(1, int(size.width * scale)), max(1, int(size.height * scale))
        else:
            dw = dh = box
        x0 = self._scaled(8, dpi) + (box - dw) // 2
        y0 = max(0, (height - box) // 2) + (box - dh) // 2
        rect = self._AppKit.NSMakeRect(x0, y0, dw, dh)
        op = getattr(self._AppKit, "NSCompositingOperationSourceOver", 2)
        image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            rect, self._AppKit.NSZeroRect, op, 1.0, True, None
        )

    def _draw_reward_progress(self, width: int, height: int, dpi: int) -> None:
        if self._reward_progress is None:
            return
        progress, eligible = self._reward_progress
        slot = self._scaled(self._progress_width, dpi)
        gap = self._scaled(self._progress_gap, dpi)
        close_x0, _cy0, _cx1, _cy1 = self.close_box(width, height, dpi)
        size = min(slot, max(1, height - 2 * self._scaled(9, dpi)))
        x0 = close_x0 - gap - slot + max(0, (slot - size) // 2)
        y0 = max(0, (height - size) // 2)
        ring = max(2, size // 5)
        inner = max(1, size - 2 * ring)
        rect = self._AppKit.NSMakeRect(x0, y0, size, size)
        self._color(self._dim, 0.35).setFill()
        self._AppKit.NSBezierPath.bezierPathWithOvalInRect_(rect).fill()
        if progress > 0:
            center = self._AppKit.NSMakePoint(x0 + size / 2.0, y0 + size / 2.0)
            path = self._AppKit.NSBezierPath.bezierPath()
            path.moveToPoint_(center)
            path.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                center, size / 2.0, -90.0, -90.0 + 360.0 * progress, False
            )
            path.closePath()
            self._color(self._success if eligible else self._accent).setFill()
            path.fill()
        self._color(self._bg, 0.94).setFill()
        self._AppKit.NSBezierPath.bezierPathWithOvalInRect_(
            self._AppKit.NSMakeRect(x0 + ring, y0 + ring, inner, inner)
        ).fill()

    def draw(self, width: int, height: int, card, dpi: int) -> None:
        if card is None:
            return
        rect = self._AppKit.NSMakeRect(0, 0, width, height)
        radius = self._scaled(self._corner_radius, dpi)
        self._color(self._bg, 0.94).setFill()
        self._AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, radius, radius
        ).fill()

        logo = self._logo_image(card)
        if logo is not None:
            self._draw_logo(logo, height, dpi)
        else:
            self._color(self._accent).setFill()
            self._AppKit.NSBezierPath.bezierPathWithRect_(
                self._AppKit.NSMakeRect(0, 0, self._scaled(self._rail_width, dpi), height)
            ).fill()

        text_h = self._text_size("Ag", dpi)[1]
        y = max(0, (height - text_h) // 2)
        x = self._left_offset(card, dpi)
        for text, color in self._segments(card):
            x += self._draw_text(text, x, y, color, dpi)

        self._draw_reward_progress(width, height, dpi)
        x0, y0, x1, _y1 = self.close_box(width, height, dpi)
        cw, ch = self._text_size(self.CLOSE_GLYPH, dpi)
        self._draw_text(
            self.CLOSE_GLYPH,
            x0 + ((x1 - x0) - cw) // 2,
            max(y0, (height - ch) // 2),
            self._dim,
            dpi,
        )

    def dispose(self) -> None:
        with self._lock:
            self._logo_data.clear()
            self._logo_images.clear()
            self._fetching.clear()
        self._font_cache.clear()
        self._attrs_cache.clear()
        self._text_size_cache.clear()
        self._example_image = None


class MacOverlayWindow:
    DEFAULT_DPI = 96

    def __init__(
        self,
        surface: MacTextSurface,
        *,
        on_click: Optional[Callable[[], None]] = None,
        on_dismiss: Optional[Callable[[], None]] = None,
        alpha: int = 235,
        corner_radius: int = 10,
    ) -> None:
        if not is_macos():
            raise RuntimeError("MacOverlayWindow is only available on macOS")
        self._AppKit, self._Foundation = _appkit()
        self._app = _ensure_app()
        self._surface = surface
        self._on_click = on_click
        self._on_dismiss = on_dismiss
        self._alpha = alpha
        self._corner_radius = corner_radius
        self._card = None
        self._dpi = self.DEFAULT_DPI
        self._placement: Optional[Placement] = None
        self._shown = False
        self._handle = id(self)
        self._create()
        _OVERLAYS[self._handle] = weakref.ref(self)
        set_repaint = getattr(self._surface, "set_repaint", None)
        if callable(set_repaint):
            set_repaint(self._invalidate)

    def _create(self) -> None:
        style = self._AppKit.NSWindowStyleMaskBorderless
        style |= getattr(self._AppKit, "NSWindowStyleMaskNonactivatingPanel", 1 << 7)
        backing = self._AppKit.NSBackingStoreBuffered
        rect = self._AppKit.NSMakeRect(0, 0, 10, 10)
        self._panel = self._AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, backing, False
        )
        self._panel.setOpaque_(False)
        self._panel.setBackgroundColor_(self._AppKit.NSColor.clearColor())
        self._panel.setHasShadow_(True)
        self._panel.setHidesOnDeactivate_(False)
        level = getattr(self._AppKit, "NSStatusWindowLevel", 25)
        self._panel.setLevel_(level)
        behavior = 0
        for name in (
            "NSWindowCollectionBehaviorCanJoinAllSpaces",
            "NSWindowCollectionBehaviorFullScreenAuxiliary",
            "NSWindowCollectionBehaviorStationary",
        ):
            behavior |= int(getattr(self._AppKit, name, 0))
        if behavior:
            self._panel.setCollectionBehavior_(behavior)
        view_class = _make_banner_view_class()
        self._view = view_class.alloc().initWithOwner_(self)
        self._panel.setContentView_(self._view)

    @property
    def hwnd(self) -> int:
        return self._handle

    @property
    def dpi(self) -> int:
        return self._dpi

    @property
    def visible(self) -> bool:
        return bool(self._shown)

    @property
    def rect(self) -> Optional[Rect]:
        if self._placement is None:
            return None
        p = self._placement
        return Rect(p.x, p.y, p.x + p.width, p.y + p.height)

    def set_dpi(self, value: int) -> None:
        if value:
            self._dpi = int(value)

    def _invalidate(self) -> None:
        try:
            self._view.setNeedsDisplay_(True)
        except Exception:  # noqa: BLE001 - repaint hints must be best-effort
            pass

    def _draw(self) -> None:
        if self._card is None or self._placement is None:
            return
        try:
            self._surface.draw(
                self._placement.width, self._placement.height, self._card, self._dpi
            )
        except Exception:  # noqa: BLE001 - never let a draw callback escape
            logger.debug("macOS overlay paint failed", exc_info=True)

    def _mouse_up(self, event) -> None:
        if self._placement is None:
            return
        point = self._view.convertPoint_fromView_(event.locationInWindow(), None)
        x, y = int(round(point.x)), int(round(point.y))
        if self._on_dismiss is not None:
            x0, y0, x1, y1 = self._surface.close_box(
                self._placement.width, self._placement.height, self._dpi
            )
            if x0 <= x <= x1 and y0 <= y <= y1:
                self._on_dismiss()
                return
        if self._on_click is not None:
            self._on_click()

    def set_card(self, card) -> None:
        self._card = card
        self._invalidate()

    def move_to(self, placement: Placement) -> None:
        if placement == self._placement:
            return
        self._placement = placement
        self._panel.setFrame_display_(_placement_to_frame(placement), True)
        self._invalidate()

    def set_click_through(self, enabled: bool) -> None:
        self._panel.setIgnoresMouseEvents_(bool(enabled))

    def show(self) -> None:
        if self._shown:
            return
        self._panel.orderFrontRegardless()
        self._shown = True

    def hide(self) -> None:
        if not self._shown:
            return
        self._panel.orderOut_(None)
        self._shown = False

    def pump(self) -> None:
        mask = getattr(self._AppKit, "NSEventMaskAny", getattr(self._AppKit, "NSAnyEventMask", (1 << 64) - 1))
        mode = getattr(self._AppKit, "NSDefaultRunLoopMode", "kCFRunLoopDefaultMode")
        while True:
            event = self._app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                mask,
                self._Foundation.NSDate.dateWithTimeIntervalSinceNow_(0),
                mode,
                True,
            )
            if event is None:
                break
            self._app.sendEvent_(event)
        self._app.updateWindows()

    def close(self) -> None:
        try:
            self.hide()
            self._panel.close()
        finally:
            _OVERLAYS.pop(self._handle, None)
            dispose = getattr(self._surface, "dispose", None)
            if callable(dispose):
                dispose()


class MacStatusItem:
    def __init__(self, controller, *, tooltip: str = "SAI sponsor overlay") -> None:
        if not is_macos():
            raise RuntimeError("MacStatusItem is only available on macOS")
        self._AppKit, self._Foundation = _appkit()
        self._controller = controller
        self._tooltip = tooltip
        _ensure_app()
        target_class = _make_status_target_class()
        self._target = target_class.alloc().initWithOwner_(self)
        length = getattr(self._AppKit, "NSVariableStatusItemLength", -1)
        self._item = self._AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(length)
        button = self._item.button()
        if button is not None:
            button.setTitle_("SAI")
            button.setToolTip_(tooltip)
        self._rebuild_menu()

    def _rebuild_menu(self) -> None:
        menu = self._AppKit.NSMenu.alloc().initWithTitle_("SAI")
        on = getattr(self._AppKit, "NSControlStateValueOn", 1)
        off = getattr(self._AppKit, "NSControlStateValueOff", 0)
        for spec in self._controller.items():
            if spec.get("sep"):
                menu.addItem_(self._AppKit.NSMenuItem.separatorItem())
                continue
            item = self._AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                spec["label"], "action:", ""
            )
            item.setTarget_(self._target)
            item.setTag_(int(spec["id"]))
            item.setState_(on if spec.get("checked") else off)
            menu.addItem_(item)
        self._item.setMenu_(menu)
        self._menu = menu

    def _invoke(self, item_id: int) -> None:
        try:
            self._controller.invoke(int(item_id))
        finally:
            self._rebuild_menu()

    def close(self) -> None:
        try:
            self._AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self._item)
        except Exception:  # noqa: BLE001
            pass
