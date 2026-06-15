import unittest

from sai.overlay.geometry import DEFAULT_MARGIN, place_banner
from sai.overlay.win32 import Rect


# A 1000x800 target window sitting at (100, 100) on a 1920x1080 work area.
TARGET = Rect(100, 100, 1100, 900)
WORK_AREA = Rect(0, 0, 1920, 1080)


class PlaceBannerTests(unittest.TestCase):
    def test_bottom_centered_by_default(self):
        p = place_banner(TARGET, width=400, height=40)
        self.assertEqual(p.width, 400)
        self.assertEqual(p.height, 40)
        # Horizontally centered on the target.
        self.assertEqual(p.x, TARGET.left + (TARGET.width - 400) // 2)
        # Just inside the bottom edge.
        self.assertEqual(p.y, TARGET.bottom - 40 - DEFAULT_MARGIN)

    def test_top_anchor(self):
        p = place_banner(TARGET, 400, 40, anchor="top")
        self.assertEqual(p.y, TARGET.top + DEFAULT_MARGIN)

    def test_horizontal_left_and_right(self):
        left = place_banner(TARGET, 400, 40, anchor="bottom-left", margin=10)
        self.assertEqual(left.x, TARGET.left + 10)
        right = place_banner(TARGET, 400, 40, anchor="top-right", margin=10)
        self.assertEqual(right.x, TARGET.right - 400 - 10)
        self.assertEqual(right.y, TARGET.top + 10)

    def test_clamped_into_bounds_when_target_hangs_off_screen(self):
        # Target pushed against the right edge so a centered banner would spill
        # past the work area; it must be pulled back inside.
        target = Rect(1700, 100, 2100, 900)  # right edge beyond the 1920 work area
        p = place_banner(target, width=400, height=40, bounds=WORK_AREA)
        self.assertLessEqual(p.x + p.width, WORK_AREA.right)
        self.assertGreaterEqual(p.x, WORK_AREA.left)

    def test_clamped_vertically(self):
        # A target flush with the bottom edge: the banner would sit below the
        # work area, so it is clamped up.
        target = Rect(100, 900, 1100, 1080)
        p = place_banner(target, 400, 40, bounds=WORK_AREA)
        self.assertLessEqual(p.y + p.height, WORK_AREA.bottom)

    def test_banner_wider_than_bounds_keeps_left_edge(self):
        narrow = Rect(0, 0, 300, 200)
        p = place_banner(TARGET, width=600, height=40, bounds=narrow)
        # Degenerate clamp range must not invert; pin to the left/top edge.
        self.assertEqual(p.x, narrow.left)

    def test_margin_is_respected(self):
        p = place_banner(TARGET, 400, 40, anchor="bottom", margin=50)
        self.assertEqual(p.y, TARGET.bottom - 40 - 50)


if __name__ == "__main__":
    unittest.main()
