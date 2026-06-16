"""The glue: drive a SponsorSession from the visibility predicate.

Each tick the driver reads the VisibilityMonitor, and:
  * while the target app is attended (foreground + user present), it keeps the
    banner positioned over the target and shown, rotating the creative on the
    SponsorSession's own cadence;
  * the moment attendance drops (focus lost / user away), it closes the billing
    window (``mark_cards_hidden``) and hides the banner;
  * on shutdown it settles, so any qualified-visible card is credited.

This reuses the exact SponsorSession lifecycle the terminal uses, so an overlay
impression is held to the same 5-second-visible bar. It is pure orchestration
over injected collaborators (monitor, window, session, probe, surface, clock),
so it is fully unit-testable headless.

Phase 1 honesty: run with an example/credit-0 SponsorSession (no placement
client), so nothing contacts the backend and no GUI surface ever attests
``terminal_interactive``. Phase 2 swaps in a real client once the backend grows
a ``desktop_overlay`` surface gated on ``attended_interactive``.
"""

from __future__ import annotations

import time
import logging
from contextlib import AbstractContextManager, nullcontext
from typing import Callable, Optional

from ..sponsors import sponsor_enabled
from .geometry import place_banner

# The overlay is persistent-while-attended, not gated on a wait, so we bypass the
# SponsorSession idle-before-first-card gate by reporting a large idle_for. The
# AFK rotation guard and the user-present check still pause an unattended screen.
PERSISTENT_IDLE_SECONDS = 10**9

# Idle below this means the user just interacted; resets the SponsorSession AFK
# rotation guard so the carousel keeps advancing while they actively work.
DEFAULT_INPUT_PRESENCE_SECONDS = 3.0

# Banner sits just below the target's title bar by default, so it never covers
# the app's bottom composer/input (observed overlapping Claude Desktop's input
# box when bottom-anchored).
DEFAULT_ANCHOR = "top"

TICK_SECONDS = 0.2
logger = logging.getLogger(__name__)


class SessionDriver:
    def __init__(
        self,
        *,
        monitor,
        window,
        session,
        probe,
        surface,
        config: dict,
        anchor: str = DEFAULT_ANCHOR,
        enabled: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
        input_presence_seconds: float = DEFAULT_INPUT_PRESENCE_SECONDS,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self._monitor = monitor
        self._window = window
        self._session = session
        self._probe = probe
        self._surface = surface
        self._config = config
        self._anchor = anchor
        self._enabled = enabled or (lambda: sponsor_enabled(config))
        self._clock = clock
        self._input_presence_seconds = input_presence_seconds
        self._tick_seconds = tick_seconds
        self._card = None
        self._displaying = False
        self._dismissed = False
        self._last_hidden_reason: str | None = None

    def dismiss(self) -> None:
        """User clicked the banner's close box: hide it for the rest of this
        session (a softer, in-GUI counterpart to the kill switch)."""
        self._dismissed = True
        logger.info("overlay dismissed")

    @property
    def current_card(self):
        """The creative currently shown (or last shown), so a click handler can
        open its sponsor link. None until the first card is fetched."""
        return self._card

    def tick(self):
        # Let the probe snapshot any expensive per-tick system queries once (the
        # macOS probe shares a single CGWindowList / NSScreen enumeration across
        # the ~3 window-rect/monitor lookups a tick makes). Optional: a fake or
        # the Win32 probe simply does not define these.
        begin = getattr(self._probe, "begin_tick", None)
        if begin is not None:
            begin()
        try:
            return self._tick()
        finally:
            end = getattr(self._probe, "end_tick", None)
            if end is not None:
                end()

    def _tick(self):
        now = self._clock()

        # Live kill-switch / disable check, every tick, so toggling it off pulls
        # the banner immediately (matches the terminal path).
        if self._dismissed:
            self._hide(now, "dismissed")
            return None
        if not self._enabled():
            self._hide(now, "disabled")
            return None

        state = self._monitor.sample()
        attended = state.target_foreground and state.user_present
        if not attended:
            self._hide(now, "target_not_foreground" if not state.target_foreground else "user_idle")
            return state

        # Treat recent input as presence so the carousel keeps rotating.
        if state.idle_seconds < self._input_presence_seconds:
            self._session.note_user_input()

        # Rotate/fetch a creative on the session's own cadence. A returned card
        # is a fresh placement (its billing clock just started); None means "keep
        # showing the current one" (or nothing yet).
        card = self._session.maybe_card(
            now, idle_for=PERSISTENT_IDLE_SECONDS, terminal_is_interactive=True
        )
        if card is not None:
            self._card = card
            self._window.set_card(card)
        self._update_reward_progress(now)

        if self._card is None:
            # Attended but no creative available (e.g. no placement / disabled).
            self._hide(now, "no_card")
            return state

        rect = self._probe.window_rect(state.target_hwnd)
        if rect is None:
            # Can't locate the target: never show at a stale position, and freeze
            # the billing window (fail closed) until it can be placed again.
            self._hide(now, "target_rect_missing")
            return state
        # Measure and paint at the TARGET monitor's DPI (the banner tracks the
        # target), so a mixed-DPI multi-monitor setup doesn't size it wrong.
        dpi = self._probe.monitor_dpi(state.target_hwnd) or self._window.dpi
        self._window.set_dpi(dpi)
        width, height = self._surface.measure(self._card, dpi)
        # Clamp to the monitor work area so the banner is always fully on screen,
        # even when the app's frame extends past the visible edge (DWM frame /
        # off-screen positioning). Fall back to the window rect.
        bounds = self._probe.monitor_work_area(state.target_hwnd) or rect
        self._window.move_to(
            place_banner(rect, width, height, anchor=self._anchor, bounds=bounds)
        )
        was_displaying = self._displaying
        self._window.show()

        # Integrity: if we were already displaying and the banner is no longer
        # verifiably on screen over the target (cloaked / wrong monitor), close
        # the billing window even though we stay attended. Guarded by
        # _displaying so the first show (when the window isn't visible yet in the
        # just-sampled state) doesn't immediately freeze the new card.
        if self._displaying and not (state.overlay_visible and state.same_monitor):
            logger.warning(
                "overlay integrity failed card=%s overlay_visible=%s same_monitor=%s",
                self._card_id(),
                state.overlay_visible,
                state.same_monitor,
            )
            self._session.mark_cards_hidden(now)
            self._set_reward_progress(None)
        self._displaying = True
        self._last_hidden_reason = None
        if not was_displaying:
            logger.info("overlay visible card=%s anchor=%s dpi=%s", self._card_id(), self._anchor, dpi)
        return state

    def _update_reward_progress(self, now: float) -> None:
        progress = None
        getter = getattr(self._session, "reward_progress", None)
        if callable(getter):
            progress = getter(now)
        self._set_reward_progress(progress)

    def _set_reward_progress(self, progress) -> None:
        setter = getattr(self._surface, "set_reward_progress", None)
        if callable(setter):
            setter(progress)

    def _card_id(self) -> str:
        if self._card is None:
            return "-"
        return str(getattr(self._card, "placement_id", None) or getattr(self._card, "id", "-") or "-")

    def _hide(self, now: float, reason: str) -> None:
        was_displaying = self._displaying
        self._session.mark_cards_hidden(now)
        self._set_reward_progress(None)
        self._window.hide()
        self._displaying = False
        if was_displaying or self._last_hidden_reason != reason:
            logger.info("overlay hidden reason=%s card=%s", reason, self._card_id())
        self._last_hidden_reason = reason

    def run(
        self,
        stop: Optional[Callable[[], bool]] = None,
        sleep: Callable[[float], None] = time.sleep,
        iteration: Optional[Callable[[], "AbstractContextManager"]] = None,
    ) -> float:
        # ``iteration`` wraps each loop pass in a context manager (the macOS
        # backend passes an autorelease-pool factory so per-tick ObjC temporaries
        # are drained each pass instead of accumulating for the process lifetime).
        iteration = iteration or nullcontext
        try:
            while stop is None or not stop():
                with iteration():
                    self.tick()
                    self._window.pump()
                sleep(self._tick_seconds)
        finally:
            return self.settle()

    def settle(self) -> float:
        now = self._clock()
        self._session.mark_cards_hidden(now)
        self._window.hide()
        self._displaying = False
        earned = self._session.settle()
        logger.info("overlay settled earned=%s", earned)
        return earned
