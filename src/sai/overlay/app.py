"""Assemble and run the desktop ad overlay: the ``sai overlay`` entry point.

Wires the probe, visibility monitor, banner window, credit-0 SponsorSession and
driver together, takes the single-authority lock, and runs the loop until the
user stops it (Ctrl+C), settling on the way out.
"""

from __future__ import annotations

import logging
import signal
import sys
from typing import Optional

from ..browser import open_url
from ..config import ensure_config_saved
from ..metrics import OVERLAY_SURFACE
from ..sponsors import SponsorSession
from .driver import DEFAULT_ANCHOR, SessionDriver
from .lock import billing_authority_lock
from .visibility import (
    VisibilityMonitor,
    any_of,
    claude_desktop_matcher,
    codex_desktop_matcher,
    mock_foreground_matcher,
)
from .macos import is_macos
from .win32 import is_windows

logger = logging.getLogger(__name__)

# Phase-1 surface label. Kept distinct from the terminal's "cli_agent_wait" so a
# future backend can recognise overlay impressions; harmless today because the
# credit-0 session never sends an event.
OVERLAY_TOOL = "desktop_overlay"


def build_credit0_session(config: dict, tool: str = OVERLAY_TOOL) -> SponsorSession:
    """A SponsorSession that can never bill: no placement client means example
    cards only (credit 0) and no backend contact. Used for explicit previews or
    when another local surface already holds billing authority."""
    session = SponsorSession(tool=tool, config=config)
    session.placement_client = None
    return session


def build_billable_session(config: dict) -> SponsorSession:
    """Phase-2: a real, backend-billed session on the desktop_overlay surface.

    Earns credits only where the backend honours that surface (an attested,
    on-screen, user-present banner held >=5s). Against a backend that does not
    yet recognise it, next_placement is simply declined, so this can never bill
    dishonestly -- it degrades to showing nothing rather than to false credit.
    """
    return SponsorSession(tool=OVERLAY_TOOL, config=config, surface=OVERLAY_SURFACE)


def _open_sponsor(driver: SessionDriver) -> None:
    card = driver.current_card
    if card is None:
        return
    # The URL is backend-supplied; only ever launch http/https so a dangerous
    # scheme (file://, custom protocol handler) can't be triggered on a click.
    open_url(card.click_url or card.url)


# Supported overlay targets: a human label + the foreground-image matcher that
# decides when the banner is shown. The billing contract is identical for every
# desktop GUI (the desktop_overlay surface), so adding a target is just a matcher.
_TARGETS = {
    "claude": ("Claude Desktop", claude_desktop_matcher),
    "codex": ("the Codex app", codex_desktop_matcher),
    "both": ("Claude Desktop + the Codex app",
             lambda: any_of(claude_desktop_matcher(), codex_desktop_matcher())),
    "mock": ("the foreground app (mock)", mock_foreground_matcher),
}


def _target_matcher(target: str):
    spec = _TARGETS.get(target)
    return spec[1]() if spec else None


def _target_label(target: str) -> str:
    spec = _TARGETS.get(target)
    return spec[0] if spec else target


def run_overlay(target: str = "claude", anchor: str = DEFAULT_ANCHOR, billable: bool = True) -> int:
    if not _platform_supported():
        print("sai overlay is only available on Windows and macOS.", file=sys.stderr)
        return 1

    matcher = _target_matcher(target)
    if matcher is None:
        print(f"sai overlay: unsupported target '{target}'.", file=sys.stderr)
        return 2

    backend = _backend()
    config = ensure_config_saved()
    backend["enable_dpi_awareness"]()

    probe = backend["default_probe"]()
    surface = backend["TextSurface"]()
    holder: dict = {}
    window = backend["OverlayWindow"](
        surface,
        on_click=lambda: _open_sponsor(holder["driver"]),
        on_dismiss=lambda: holder["driver"].dismiss(),
    )
    monitor = VisibilityMonitor(probe, matcher, overlay_hwnd=lambda: window.hwnd)

    # One billing authority per machine-session, shared with the terminal
    # compositor. Only contend for it when we actually intend to bill, so a
    # credit-0 preview never demotes a legitimately-earning terminal. If another
    # surface already holds it, fall back to credit-0 display.
    lock = billing_authority_lock()
    bill = billable and lock.acquire()
    if bill:
        logger.info("billing authority acquired surface=overlay target=%s", target)
    if billable and not bill:
        logger.info("another SAI billing authority is active; overlay runs credit-0")

    session = build_billable_session(config) if bill else build_credit0_session(config)

    stop = {"requested": False}

    def _request_stop(*_args) -> None:
        stop["requested"] = True

    _install_stop_handlers(_request_stop)

    # The tray icon is the overlay's control/consent home (kill switch,
    # frequency, Terms/Privacy, Quit). Its messages are pumped on the same
    # thread by window.pump(). Never let a tray failure stop the overlay.
    from .tray import TrayController

    controller = TrayController(config, on_quit=_request_stop)
    tray = None
    try:
        tray = backend["TrayIcon"](controller)
    except (OSError, RuntimeError):
        logger.warning("tray icon unavailable; overlay runs without it", exc_info=True)

    def _publish_status(text: str) -> None:
        # Surface what the overlay is doing on the tray (tooltip + a menu line),
        # so a billable overlay with no placement reads as alive, not broken --
        # without ever showing an example card when there is no real demand.
        controller.set_status(text)
        if tray is not None:
            try:
                tray.set_tooltip(text)
            except Exception:  # noqa: BLE001 - a status refresh must not stop the overlay
                logger.debug("tray status update failed", exc_info=True)

    driver = SessionDriver(
        monitor=monitor, window=window, session=session,
        probe=probe, surface=surface, config=config, anchor=anchor,
        on_status=_publish_status,
    )
    holder["driver"] = driver

    mode = "billing" if bill else "credit-0 preview"
    logger.info("overlay starting target=%s mode=%s anchor=%s", target, mode, anchor)
    print(f"SAI overlay running for {_target_label(target)} ({mode}). Press Ctrl+C to stop.")
    earned = 0.0
    try:
        earned = driver.run(stop=lambda: stop["requested"], iteration=backend.get("iteration_context"))
    finally:
        if tray is not None:
            tray.close()
        window.close()
        lock.release()
        logger.info("overlay stopped target=%s mode=%s earned=%s", target, mode, earned)
    return 0


def _platform_supported() -> bool:
    return is_windows() or is_macos()


def _backend() -> dict:
    if is_windows():
        # Lazy: these import ctypes/Win32 and only exist meaningfully on Windows.
        from .surface import TextSurface
        from .tray import TrayIcon
        from .window import OverlayWindow, enable_dpi_awareness
        from .win32 import default_probe

        return {
            "TextSurface": TextSurface,
            "OverlayWindow": OverlayWindow,
            "TrayIcon": TrayIcon,
            "default_probe": default_probe,
            "enable_dpi_awareness": enable_dpi_awareness,
        }
    if is_macos():
        # Lazy: these import PyObjC/AppKit/Quartz only on macOS.
        from .macos import (
            MacOverlayWindow,
            MacStatusItem,
            MacTextSurface,
            autorelease_pool,
            default_probe,
            enable_dpi_awareness,
        )

        return {
            "TextSurface": MacTextSurface,
            "OverlayWindow": MacOverlayWindow,
            "TrayIcon": MacStatusItem,
            "default_probe": default_probe,
            "enable_dpi_awareness": enable_dpi_awareness,
            # Drain one autorelease pool per overlay loop iteration (macOS only).
            "iteration_context": autorelease_pool,
        }
    raise RuntimeError("unsupported overlay platform")


def _default_probe():
    return _backend()["default_probe"]()


def _install_stop_handlers(handler) -> None:
    for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # Not in the main thread, or unsupported on this platform.
            pass
