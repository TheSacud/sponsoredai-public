"""ANSI escape-code helpers shared by the status line and sponsor cards."""

from __future__ import annotations

import os
import re

OSC8_LINK = re.compile("\x1b\\]8;;[^\x1b]*\x1b\\\\")
SGR = re.compile("\x1b\\[[0-9;]*m")

RESET = "\x1b[0m"
BOLD = "1"
DIM = "2"
UNDERLINE = "4"
GREEN = "32"
ACCENT = "38;5;179"  # warm amber — the sponsor brand accent (256-color)


def _console_unicode() -> bool:
    if os.name != "nt":
        return True
    import ctypes

    try:
        # The status stream writes raw UTF-8 bytes (WriteFile, not
        # WriteConsoleW), so non-ASCII glyphs only render correctly when the
        # console output codepage is UTF-8.
        return ctypes.windll.kernel32.GetConsoleOutputCP() == 65001
    except (AttributeError, OSError):
        return False


UNICODE_OK = _console_unicode()
ELLIPSIS = "…" if UNICODE_OK else "..."
DASH = "—" if UNICODE_OK else "-"
RAIL = "▌" if UNICODE_OK else "|"   # left accent bar marking the sponsored zone
MIDDOT = "·" if UNICODE_OK else "-"  # compact separator
ARROW = "↗" if UNICODE_OK else ""    # clickable-link hint (dropped without Unicode)


def styles_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return os.environ.get("SAI_NO_COLOR", "").lower() not in {"1", "true", "yes", "on"}


def style(text: str, *codes: str) -> str:
    if not codes or not styles_enabled():
        return text
    prefix = "".join(f"\x1b[{code}m" for code in codes)
    return f"{prefix}{text}{RESET}"


def visible_length(text: str) -> int:
    return len(SGR.sub("", OSC8_LINK.sub("", text)))


def truncate_visible(text: str, limit: int) -> str:
    """Cut to at most `limit` visible characters, keeping SGR sequences whole.

    OSC 8 hyperlinks must be stripped by the caller first: cutting one in
    half leaves the terminal with an unterminated link.
    """
    out: list[str] = []
    visible = 0
    i = 0
    while i < len(text) and visible < limit:
        match = SGR.match(text, i)
        if match:
            out.append(match.group())
            i = match.end()
            continue
        if text[i] == "\x1b":
            break
        out.append(text[i])
        visible += 1
        i += 1
    return "".join(out)
