"""Where the banner sits relative to the target window.

Pure integer geometry over physical pixels (the probe's ``GetWindowRect`` is in
physical pixels under per-monitor DPI awareness), so it is fully unit-testable
without a display. The window module feeds in the live target rect each tick and
applies the result with ``SetWindowPos``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .win32 import Rect


# Default gap between the banner and the target window edge, in physical pixels
# at 96 DPI; the window module scales it by the target monitor's DPI.
DEFAULT_MARGIN = 16


@dataclass(frozen=True)
class Placement:
    x: int
    y: int
    width: int
    height: int


def _clamp(value: int, low: int, high: int) -> int:
    # When the allowed range is degenerate (banner wider than the bounds), keep
    # the low edge rather than inverting.
    if high < low:
        return low
    return min(max(value, low), high)


def place_banner(
    target: Rect,
    width: int,
    height: int,
    *,
    anchor: str = "bottom",
    margin: int = DEFAULT_MARGIN,
    bounds: Rect | None = None,
) -> Placement:
    """Position a ``width`` x ``height`` banner against ``target``.

    ``anchor`` is a vertical edge (``"top"``/``"bottom"``) optionally suffixed
    with a horizontal side (``"-left"``/``"-right"``); horizontal defaults to
    centered on the target. The result is clamped inside ``bounds`` (the monitor
    work area) so the banner never lands off-screen when the target is maximized
    against an edge or is narrower than the banner.
    """
    vertical, _, horizontal = anchor.partition("-")

    if horizontal == "left":
        x = target.left + margin
    elif horizontal == "right":
        x = target.right - width - margin
    else:
        x = target.left + (target.width - width) // 2

    if vertical == "top":
        y = target.top + margin
    else:
        y = target.bottom - height - margin

    if bounds is not None:
        x = _clamp(x, bounds.left, bounds.right - width)
        y = _clamp(y, bounds.top, bounds.bottom - height)

    return Placement(x=x, y=y, width=width, height=height)
