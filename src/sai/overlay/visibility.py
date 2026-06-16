"""The honest-impression core: decide, each tick, whether the overlay banner is
genuinely on screen and the user is present.

An overlay impression must clear the same bar as the in-terminal card, so a
placement only counts while ALL of these hold (mirroring the terminal's
"visible for >=5s during an attended wait" rule):

  * the foreground window belongs to the target app (matched by full image
    path, never by window class or bare process name -- ``Chrome_WidgetWin_1``
    is shared by every Electron app and a dozen processes are named ``claude``);
  * the overlay window is shown, not minimized, and not cloaked onto another
    virtual desktop;
  * the overlay and the target are on the same monitor;
  * the user has touched input recently (the GUI analogue of the terminal's
    stdin-presence AFK guard).

This module is pure logic over a ``SystemProbe``; the driver maps ``live``
transitions onto ``SponsorSession.maybe_card``/``mark_cards_hidden``/``settle``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Union

from .win32 import SystemProbe


# How long since the last keypress/mouse move before we treat the user as away.
# Generous on purpose: a developer reading the agent's output is present even
# while not typing, exactly as a silent-but-watching terminal reader is.
DEFAULT_IDLE_THRESHOLD_SECONDS = 60.0

# A matcher decides whether a foreground process image path is the target app.
TargetMatcher = Callable[[str], bool]

# An overlay HWND source: a fixed handle, or a callable resolving it lazily
# (the window is created after the monitor, so the driver passes a getter).
OverlayHandle = Union[int, Callable[[], Optional[int]], None]


@dataclass(frozen=True)
class VisibilityState:
    """A single tick's reading. ``live`` is the only value the driver acts on;
    the components are kept for diagnostics, logging and tests."""

    target_hwnd: int
    target_path: Optional[str]
    target_foreground: bool
    overlay_visible: bool
    same_monitor: bool
    user_present: bool
    idle_seconds: float
    live: bool


def _normalise_windows(path: str) -> str:
    return path.replace("/", "\\").lower()


def image_path_matcher(pattern: str) -> TargetMatcher:
    """Match a foreground image path against a regex (case-insensitive, run over
    a backslash-normalised path)."""
    compiled = re.compile(pattern)

    def match(path: str) -> bool:
        return bool(path) and bool(compiled.search(_normalise_windows(path)))

    return match


def _normalise_posix(path: str) -> str:
    return path.replace("\\", "/").lower()


def posix_path_matcher(pattern: str) -> TargetMatcher:
    """Match a foreground image path against a regex over slash-normalised paths."""
    compiled = re.compile(pattern)

    def match(path: str) -> bool:
        return bool(path) and bool(compiled.search(_normalise_posix(path)))

    return match


def any_of(*matchers: TargetMatcher) -> TargetMatcher:
    def match(path: str) -> bool:
        return any(m(path) for m in matchers)

    return match


def claude_desktop_matcher() -> TargetMatcher:
    r"""Match the Claude Desktop GUI, NOT the Claude Code CLI.

    The GUI ships either as a Store (MSIX) build under
    ``...\WindowsApps\Claude_<version>\app\Claude.exe`` or as a Squirrel build
    under ``...\AnthropicClaude\app-<version>\Claude.exe``. The Claude Code CLI
    lives at ``...\AppData\Roaming\Claude\claude-code\<v>\claude.exe`` -- it
    contains ``\claude\`` but neither anchor below, so it is excluded.
    """
    return any_of(
        image_path_matcher(
            r"(\\windowsapps\\claude_[^\\]+|\\anthropicclaude)\\.*\\claude\.exe$"
        ),
        posix_path_matcher(
            r"/claude(?: desktop)?\.app/contents/macos/claude$"
        ),
    )


def codex_desktop_matcher() -> TargetMatcher:
    r"""Match the Codex desktop GUI, NOT the Codex CLI.

    The GUI is a Store (MSIX) Electron build whose window is owned by
    ``...\WindowsApps\OpenAI.Codex_<version>\app\Codex.exe``. The same package
    bundles the CLI engine at ``...\app\resources\codex.exe`` and there is a
    standalone CLI at ``...\AppData\Local\OpenAI\Codex\bin\codex.exe`` -- both
    run the terminal billing path, so the pattern requires ``\app\Codex.exe``
    directly (no ``resources``/``bin`` segment) to exclude them.
    """
    return any_of(
        image_path_matcher(r"\\windowsapps\\openai\.codex_[^\\]+\\app\\codex\.exe$"),
        posix_path_matcher(
            r"/(?:codex|openai codex)\.app/contents/macos/codex$"
        ),
    )


def mock_foreground_matcher() -> TargetMatcher:
    """Development-only matcher: accept whichever real app is foreground.

    This is intentionally broad so a local mock overlay can be visually tested
    without installing Claude Desktop or the Codex desktop app. The CLI forces
    this target into credit-0 mode, so it cannot create billable impressions.
    """

    def match(path: str) -> bool:
        return bool(path)

    return match


class VisibilityMonitor:
    def __init__(
        self,
        probe: SystemProbe,
        is_target: TargetMatcher,
        overlay_hwnd: OverlayHandle = None,
        *,
        idle_threshold_seconds: float = DEFAULT_IDLE_THRESHOLD_SECONDS,
    ) -> None:
        self._probe = probe
        self._is_target = is_target
        self._overlay_hwnd = overlay_hwnd
        self._idle_threshold = idle_threshold_seconds

    def set_overlay_hwnd(self, overlay_hwnd: OverlayHandle) -> None:
        """The window is created after the monitor, so the driver wires its HWND
        in once it exists."""
        self._overlay_hwnd = overlay_hwnd

    def _overlay(self) -> int:
        handle = self._overlay_hwnd
        if callable(handle):
            handle = handle()
        return int(handle) if handle else 0

    def sample(self) -> VisibilityState:
        foreground = self._probe.foreground_window()
        path = self._probe.process_image_path(foreground) if foreground else None
        target_foreground = bool(path) and self._is_target(path)

        overlay = self._overlay()
        overlay_visible = (
            bool(overlay)
            and self._probe.is_window_visible(overlay)
            and not self._probe.is_minimized(overlay)
            and not self._probe.is_cloaked(overlay)
        )

        # Only worth a monitor lookup when both windows are otherwise in play.
        same_monitor = False
        if target_foreground and overlay_visible:
            target_monitor = self._probe.monitor_of(foreground)
            same_monitor = bool(target_monitor) and target_monitor == self._probe.monitor_of(overlay)

        idle = self._probe.idle_seconds()
        user_present = idle < self._idle_threshold

        live = target_foreground and overlay_visible and same_monitor and user_present
        return VisibilityState(
            target_hwnd=foreground if target_foreground else 0,
            target_path=path,
            target_foreground=target_foreground,
            overlay_visible=overlay_visible,
            same_monitor=same_monitor,
            user_present=user_present,
            idle_seconds=idle,
            live=live,
        )
