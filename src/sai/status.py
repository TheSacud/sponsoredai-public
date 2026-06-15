from __future__ import annotations

import os
import sys
from typing import TextIO

from .ansi import ELLIPSIS, OSC8_LINK, RESET, SGR, truncate_visible, visible_length


class StatusRenderer:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._stream: TextIO | None = None
        self._visible = False

    def __enter__(self) -> "StatusRenderer":
        if not self.enabled:
            return self
        self._stream = self._open_terminal()
        if self._stream is None:
            self.enabled = False
        return self

    def __exit__(self, *_exc: object) -> None:
        self.clear()
        if self._stream and self._stream not in {sys.stderr, sys.stdout}:
            self._stream.close()

    @property
    def width(self) -> int:
        return max(40, self._terminal_width() - 1)

    def show(self, text: str) -> None:
        if not self.enabled or not self._stream:
            return
        safe_text = text.replace("\n", " ")
        limit = self.width
        if visible_length(safe_text) > limit:
            # Slicing through an OSC 8 hyperlink would leave the terminal with
            # an unterminated link, so drop the link wrappers before cutting.
            safe_text = OSC8_LINK.sub("", safe_text)
            safe_text = truncate_visible(safe_text, limit - len(ELLIPSIS)) + ELLIPSIS
        if SGR.search(safe_text):
            # A cut can drop the trailing reset; never let styles bleed into
            # whatever the terminal draws next on this row.
            safe_text += RESET
        self._stream.write(f"\x1b7\x1b[999;1H\x1b[2K{safe_text}\x1b8")
        self._stream.flush()
        self._visible = True

    def clear(self) -> None:
        if not self.enabled or not self._stream or not self._visible:
            return
        self._stream.write("\x1b7\x1b[999;1H\x1b[2K\x1b8")
        self._stream.flush()
        self._visible = False

    def print_receipt(self, text: str) -> None:
        if not self.enabled or not self._stream:
            return
        self.clear()
        self._stream.write(text.rstrip() + "\n")
        self._stream.flush()

    def note(self, text: str) -> None:
        # A standalone line printed above the status area, left in the scrollback.
        # Used where the pinned bottom-row overlay cannot survive: a full-screen
        # agent TUI on the Windows passthrough path repaints the bottom row and
        # erases anything show() drew there, so the card is surfaced inline once
        # instead of being fought for on row 999.
        if not self.enabled or not self._stream:
            return
        self.clear()
        self._stream.write(text.rstrip() + "\n")
        self._stream.flush()

    @staticmethod
    def _terminal_width() -> int:
        try:
            return os.get_terminal_size().columns
        except OSError:
            return 100

    @staticmethod
    def _open_terminal() -> TextIO | None:
        if os.name == "nt":
            try:
                stream: TextIO | None = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            except OSError:
                stream = sys.stderr if sys.stderr.isatty() else None
            if stream is not None and not _enable_windows_vt(stream):
                # Legacy conhost without VT support would print raw escapes.
                if stream is not sys.stderr:
                    stream.close()
                return None
            return stream
        try:
            return open("/dev/tty", "w", encoding="utf-8", errors="replace")
        except OSError:
            return sys.stderr if sys.stderr.isatty() else None


def _enable_windows_vt(stream: TextIO) -> bool:
    import ctypes
    import msvcrt

    enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    try:
        handle = msvcrt.get_osfhandle(stream.fileno())
        kernel32 = ctypes.windll.kernel32
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & enable_vt:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except OSError:
        return False

