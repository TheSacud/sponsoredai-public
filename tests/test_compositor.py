import unittest

from sai.ansi import RESET, visible_length
from sai.compositor import (
    StreamRewriter,
    clamp_line,
    clear_row,
    clear_screen,
    paint_row,
    park_cursor,
    release_region,
    reserve_region,
)


def rw(bottom=49):
    r = StreamRewriter()
    r.set_region_bottom(bottom)
    return r


class StreamRewriterTests(unittest.TestCase):
    def test_bare_decstbm_reset_is_clamped(self):
        self.assertEqual(rw().feed(b"\x1b[r"), b"\x1b[1;49r")

    def test_in_bounds_region_passes_through(self):
        self.assertEqual(rw().feed(b"\x1b[1;40r"), b"\x1b[1;40r")

    def test_over_bound_region_bottom_is_clamped(self):
        self.assertEqual(rw().feed(b"\x1b[1;200r"), b"\x1b[1;49r")

    def test_degenerate_region_is_treated_as_full_reset(self):
        # codex emits ESC[1;0r when its viewport sits at y=0 (e.g. after Ctrl+L)
        self.assertEqual(rw().feed(b"\x1b[1;0r"), b"\x1b[1;49r")
        self.assertEqual(rw().feed(b"\x1b[5;3r"), b"\x1b[1;49r")

    def test_split_escape_across_chunks_is_rewritten(self):
        r = rw()
        self.assertEqual(r.feed(b"abc\x1b["), b"abc")
        self.assertEqual(r.feed(b"r z"), b"\x1b[1;49r z")

    def test_cup_to_reserved_row_is_clamped(self):
        self.assertEqual(rw().feed(b"\x1b[50;5H"), b"\x1b[49;5H")
        self.assertEqual(rw().feed(b"\x1b[80;1H"), b"\x1b[49;1H")
        self.assertEqual(rw().feed(b"\x1b[50H"), b"\x1b[49;1H")  # col defaults to 1

    def test_cup_within_bounds_passes_through(self):
        self.assertEqual(rw().feed(b"\x1b[10;3H"), b"\x1b[10;3H")
        self.assertEqual(rw().feed(b"\x1b[49;1H"), b"\x1b[49;1H")
        self.assertEqual(rw().feed(b"\x1b[H"), b"\x1b[H")

    def test_hvp_row_is_clamped(self):
        self.assertEqual(rw().feed(b"\x1b[60;2f"), b"\x1b[49;2f")

    def test_ed_repaint_only_for_full_clears(self):
        r = rw(); r.feed(b"\x1b[2J"); self.assertTrue(r.repaint_due)
        r = rw(); r.feed(b"\x1b[3J"); self.assertTrue(r.repaint_due)
        r = rw(); r.feed(b"\x1b[0J"); self.assertFalse(r.repaint_due)
        r = rw(); r.feed(b"\x1b[J"); self.assertFalse(r.repaint_due)

    def test_alt_screen_tracking(self):
        r = rw()
        self.assertEqual(r.feed(b"\x1b[?1049h"), b"\x1b[?1049h")
        self.assertTrue(r.alt_active)
        self.assertTrue(r.repaint_due)
        r.feed(b"\x1b[?1049l")
        self.assertFalse(r.alt_active)
        self.assertTrue(rw().feed(b"\x1b[?47h") and rw().feed(b"\x1b[?47h"))  # legacy form parsed
        r2 = rw(); r2.feed(b"\x1b[?47h"); self.assertTrue(r2.alt_active)

    def test_sync_update_gates_safe_to_paint(self):
        r = rw()
        self.assertTrue(r.safe_to_paint())
        r.feed(b"\x1b[?2026h")
        self.assertTrue(r.in_sync)
        self.assertFalse(r.safe_to_paint())
        r.feed(b"\x1b[?2026l")
        self.assertTrue(r.safe_to_paint())

    def test_decsc_depth_gates_safe_to_paint(self):
        r = rw()
        self.assertEqual(r.feed(b"\x1b7"), b"\x1b7")
        self.assertEqual(r.decsc_depth, 1)
        self.assertFalse(r.safe_to_paint())
        r.feed(b"\x1b8")
        self.assertTrue(r.safe_to_paint())

    def test_osc_passes_through_and_buffers_across_chunks(self):
        self.assertEqual(rw().feed(b"\x1b]0;codex\x07x"), b"\x1b]0;codex\x07x")
        r = rw()
        self.assertEqual(r.feed(b"\x1b]0;cod"), b"")
        self.assertEqual(r.feed(b"ex\x07X"), b"\x1b]0;codex\x07X")

    def test_ctrl_l_hard_clear_sequence(self):
        r = rw()
        out = r.feed(b"\x1b[r\x1b[0m\x1b[H\x1b[2J\x1b[3J\x1b[H")
        self.assertEqual(out, b"\x1b[1;49r\x1b[0m\x1b[H\x1b[2J\x1b[3J\x1b[H")
        self.assertTrue(r.repaint_due)

    def test_plain_text_with_literal_r_and_h_untouched(self):
        self.assertEqual(rw().feed(b"foo r H bar"), b"foo r H bar")


class HelperTests(unittest.TestCase):
    def test_reserve_and_release_region(self):
        self.assertEqual(reserve_region(49), b"\x1b[1;49r")
        self.assertEqual(release_region(), b"\x1b[r")

    def test_park_and_clear(self):
        self.assertEqual(clear_screen(), b"\x1b[0m\x1b[2J\x1b[H")
        self.assertEqual(park_cursor(49), b"\x1b[49;1H")
        self.assertEqual(clear_row(50), b"\x1b[50;1H\x1b[2K")

    def test_paint_row_wraps_text_in_save_restore(self):
        seq = paint_row(50, b"AD")
        self.assertTrue(seq.startswith(b"\x1b7"))
        self.assertTrue(seq.endswith(b"\x1b8"))
        self.assertIn(b"\x1b[50;1H", seq)
        self.assertIn(b"AD", seq)


class ClampLineTests(unittest.TestCase):
    @staticmethod
    def vis(b: bytes) -> int:
        return visible_length(b.decode("utf-8"))

    def test_short_text_passes_through(self):
        self.assertEqual(clamp_line("hello", 80), b"hello")

    def test_long_plain_text_fits_within_width(self):
        out = clamp_line("x" * 200, 20)
        self.assertLessEqual(self.vis(out), 19)  # cols-1, never the last column

    def test_narrow_window_does_not_overflow(self):
        line = "Sponsored: Acme Corp - reach devs (acme.dev) +0.001 AI credits"
        for cols in (60, 40, 30, 20, 12):
            out = clamp_line(line, cols)
            self.assertLessEqual(self.vis(out), cols - 1, f"cols={cols}")

    def test_tiny_window_never_crashes_and_fits(self):
        for cols in (1, 2, 3, 4, 5):
            out = clamp_line("Sponsored: Acme blah blah blah", cols)
            self.assertLessEqual(self.vis(out), max(1, cols - 1), f"cols={cols}")

    def test_ansi_styled_counts_visible_only(self):
        styled = "\x1b[1;33mSponsored\x1b[0m: " + "y" * 100
        out = clamp_line(styled, 30)
        self.assertLessEqual(self.vis(out), 29)

    def test_open_style_is_reset(self):
        out = clamp_line("\x1b[31mred with no reset", 80)
        self.assertTrue(out.endswith(RESET.encode("utf-8")))

    def test_osc8_link_stripped_when_truncating(self):
        # A sliced OSC 8 hyperlink would break the terminal; it must be removed.
        link = "\x1b]8;;https://acme.dev/x\x1b\\acme.dev\x1b]8;;\x1b\\"
        line = "Sponsored: Acme - " + link + " more text " * 10
        out = clamp_line(line, 25)
        self.assertNotIn(b"\x1b]8;;", out)
        self.assertLessEqual(self.vis(out), 24)

    def test_real_card_footer_fits_narrow(self):
        from sai.sponsors import LOCAL_SPONSORS
        card = LOCAL_SPONSORS[0]
        for cols in (80, 40, 24, 14):
            out = clamp_line(card.footer(width=max(1, cols - 1)), cols)
            self.assertLessEqual(self.vis(out), cols - 1, f"cols={cols}")

    def test_progress_card_footer_fits_narrow(self):
        from sai.sponsors import SponsorCard
        card = SponsorCard(
            id="plc_1",
            sponsor="Acme",
            message="Ship faster agent workflows with hosted preview environments",
            url="https://acme.example/sai",
            credit_amount=0.012,
            placement_id="plc_1",
            campaign_id="cmp_1",
        )
        progress = {"visible_seconds": 2.0, "remaining_seconds": 3.0, "eligible": False}
        for cols in (80, 40, 24, 14):
            out = clamp_line(card.footer(width=max(1, cols - 1), progress=progress), cols)
            self.assertLessEqual(self.vis(out), cols - 1, f"cols={cols}")


if __name__ == "__main__":
    unittest.main()
