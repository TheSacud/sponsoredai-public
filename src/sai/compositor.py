"""Terminal compositor primitives for pinning a sponsor line on the bottom row.

A full-screen agent TUI (codex, claude) owns a bottom-anchored viewport that it
diff-repaints every frame, so any line written into that region is clobbered.
To keep a sponsor line visible we run the agent inside a PTY, report the terminal
height as H-1 (the agent confines itself to rows 1..H-1), and rewrite its
output stream so it can never reach physical row H — which we paint ourselves.

`StreamRewriter` is the stream-rewriting core (verified against the codex TUI
source); the helpers below build the escape sequences for the reserved region
and the ad row. The platform PTY plumbing lives in `runner.py`.
"""
from __future__ import annotations

from .ansi import ELLIPSIS, OSC8_LINK, RESET, SGR, truncate_visible, visible_length

ESC = 0x1B


class StreamRewriter:
    """Rewrite a child->terminal byte stream so physical row H stays ours.

    - DECSTBM clamp: ESC[r (and degenerate/over-bound ESC[t;br) -> ESC[1;{H-1}r,
      so the child can never re-arm row H for scrolling.
    - CUP/HVP clamp: absolute cursor moves whose row targets row H are pulled
      back to H-1 (CUP ignores scroll margins, so the reported height alone is
      not a hard guarantee).
    - Tracks alt-screen (?1049/?1047/?47), synchronized-update (?2026) and the
      child's DECSC/DECRC depth so the caller never injects a paint mid-batch.

    Buffers a trailing partial escape across chunk boundaries. Feed from a
    single thread.
    """

    def __init__(self) -> None:
        self._pending = b""
        self._bottom = 1          # reserved scroll-region bottom = H-1
        self.repaint_due = False  # child wiped/toggled row H -> re-assert the ad
        self.in_sync = False      # inside a DECSET 2026 synchronized update
        self.decsc_depth = 0      # child has an open DECSC (ESC7) save
        self.alt_active = False   # child is on the alternate screen buffer

    def set_region_bottom(self, bottom: int) -> None:
        self._bottom = max(1, bottom)

    def safe_to_paint(self) -> bool:
        return not self.in_sync and self.decsc_depth == 0

    def feed(self, chunk: bytes) -> bytes:
        data = self._pending + chunk
        self._pending = b""
        out = bytearray()
        i, n = 0, len(data)
        while i < n:
            if data[i] != ESC:
                j = data.find(b"\x1b", i)
                if j == -1:
                    out += data[i:]
                    break
                out += data[i:j]
                i = j
                continue
            if i + 1 >= n:
                self._pending = data[i:]
                break
            t = data[i + 1]
            if t == 0x5B:  # '[' -> CSI
                k = self._csi_final(data, i + 2)
                if k == -1:
                    self._pending = data[i:]
                    break
                out += self._handle_csi(data[i : k + 1])
                i = k + 1
            elif t == 0x5D:  # ']' -> OSC
                end = self._osc_end(data, i + 2)
                if end == -1:
                    self._pending = data[i:]
                    break
                out += data[i : end + 1]
                i = end + 1
            else:
                if t == 0x37:      # ESC 7  DECSC
                    self.decsc_depth += 1
                elif t == 0x38:    # ESC 8  DECRC
                    self.decsc_depth = max(0, self.decsc_depth - 1)
                out += data[i : i + 2]
                i += 2
        return bytes(out)

    @staticmethod
    def _csi_final(data: bytes, start: int) -> int:
        i, n = start, len(data)
        while i < n:
            if 0x40 <= data[i] <= 0x7E:
                return i
            i += 1
        return -1

    @staticmethod
    def _osc_end(data: bytes, start: int) -> int:
        i, n = start, len(data)
        while i < n:
            if data[i] == 0x07:  # BEL
                return i
            if data[i] == ESC:
                if i + 1 >= n:
                    return -1
                if data[i + 1] == 0x5C:  # ST = ESC \
                    return i + 1
            i += 1
        return -1

    def _handle_csi(self, seq: bytes) -> bytes:
        final = seq[-1:]
        params = seq[2:-1]

        if final == b"r":  # DECSTBM
            if params in (b"", b";"):
                return b"\x1b[1;%dr" % self._bottom
            try:
                parts = params.split(b";")
                top = int(parts[0]) if parts[0] else 1
                bot = int(parts[1]) if len(parts) > 1 and parts[1] else self._bottom
            except ValueError:
                return b"\x1b[1;%dr" % self._bottom
            if bot < 1 or bot < top:  # degenerate (e.g. ESC[1;0r) == full reset
                return b"\x1b[1;%dr" % self._bottom
            top = max(1, top)
            if bot > self._bottom:
                bot = self._bottom
            return b"\x1b[%d;%dr" % (top, bot)

        if final in (b"H", b"f"):  # CUP / HVP -> clamp row off the reserved row
            if params in (b"", b";"):
                return seq
            try:
                parts = params.split(b";")
                row = int(parts[0]) if parts[0] else 1
                col = int(parts[1]) if len(parts) > 1 and parts[1] else 1
            except ValueError:
                return seq
            if row > self._bottom:
                return b"\x1b[%d;%d%s" % (self._bottom, col, final)
            return seq

        if final == b"J":  # ED -> only 2 (screen) / 3 (scrollback) wipe row H
            if params in (b"2", b"3"):
                self.repaint_due = True
            return seq

        if final in (b"h", b"l"):
            on = final == b"h"
            if b"?2026" in params:
                self.in_sync = on
            if b"?1049" in params or b"?1047" in params or b"?47" in params:
                self.alt_active = on
                self.repaint_due = True
            return seq

        return seq


def reserve_region(bottom: int) -> bytes:
    """Set the scroll region to rows 1..bottom (== H-1), freeing physical row H."""
    return b"\x1b[1;%dr" % bottom


def release_region() -> bytes:
    """Restore the full-screen scroll region."""
    return b"\x1b[r"


def park_cursor(row: int) -> bytes:
    """Move the cursor to (row, 1) — used before spawn so the child's startup
    CPR anchors its viewport at or above the reserved row."""
    return b"\x1b[%d;1H" % row


def clear_row(row: int) -> bytes:
    return b"\x1b[%d;1H\x1b[2K" % row


def clamp_line(text: str, cols: int) -> bytes:
    """Fit a possibly ANSI-styled line into `cols` columns so it never wraps.

    Leaves the last column free (auto-wrap guard). Strips OSC 8 hyperlinks
    before slicing — a cut link leaves the terminal in a broken link state —
    and re-adds a reset if any style is left open. Returns UTF-8 bytes ready to
    hand to paint_row().
    """
    limit = max(1, cols - 1)
    if visible_length(text) > limit:
        text = OSC8_LINK.sub("", text)
        keep = limit - visible_length(ELLIPSIS)
        text = truncate_visible(text, keep) + ELLIPSIS if keep > 0 else ELLIPSIS[:limit]
    if SGR.search(text):
        text += RESET
    return text.encode("utf-8", "replace")


def paint_row(row: int, text: bytes) -> bytes:
    """Paint `text` on physical `row`, leaving the cursor where it was.

    Uses DECSC/DECRC — safe only when StreamRewriter.safe_to_paint() is true
    (no open child DECSC / sync batch), which the caller must enforce.
    """
    return b"".join(
        [
            b"\x1b7",                 # DECSC: save cursor + attrs
            b"\x1b[%d;1H" % row,      # CUP to the row (ignores margins, DECOM reset)
            b"\x1b[2K\x1b[0m",        # clear line, clean SGR
            text,
            b"\x1b[0m",
            b"\x1b8",                 # DECRC: restore cursor + attrs
        ]
    )
