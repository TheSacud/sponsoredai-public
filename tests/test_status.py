import io
import unittest
from unittest.mock import patch

from sai.ansi import ELLIPSIS
from sai.status import StatusRenderer


def hyperlinked(display, target):
    return f"\x1b]8;;{target}\x1b\\{display}\x1b]8;;\x1b\\"


class StatusRendererTests(unittest.TestCase):
    def _renderer(self):
        renderer = StatusRenderer(enabled=True)
        renderer._stream = io.StringIO()
        return renderer

    def test_show_keeps_hyperlink_when_it_fits(self):
        renderer = self._renderer()
        text = "Sponsored: " + hyperlinked("acme.dev", "https://x/c/p/t")
        with patch.object(StatusRenderer, "_terminal_width", return_value=200):
            renderer.show(text)
        self.assertIn("\x1b]8;;https://x/c/p/t\x1b\\", renderer._stream.getvalue())

    def test_show_keeps_link_whose_target_is_long_but_text_fits(self):
        renderer = self._renderer()
        target = "https://sponsoredai.dev/c/" + "p" * 80 + "/" + "t" * 80
        text = "Sponsored: " + hyperlinked("acme.dev", target)
        with patch.object(StatusRenderer, "_terminal_width", return_value=60):
            renderer.show(text)
        # Only visible characters count against the width; the long target
        # inside the escape sequence must not trigger truncation.
        self.assertIn("\x1b]8;;" + target, renderer._stream.getvalue())

    def test_show_strips_hyperlinks_instead_of_cutting_mid_escape(self):
        renderer = self._renderer()
        text = "Sponsored: " + hyperlinked("a" * 80, "https://x/c/p/t") + " +0.012 AI credits"
        with patch.object(StatusRenderer, "_terminal_width", return_value=60):
            renderer.show(text)
        written = renderer._stream.getvalue()
        self.assertNotIn("\x1b]8;", written)
        kept = 59 - len(ELLIPSIS) - len("Sponsored: ")
        self.assertIn("Sponsored: " + "a" * kept + ELLIPSIS, written)

    def test_show_truncates_visible_chars_and_resets_styles(self):
        renderer = self._renderer()
        text = "\x1b[2mSponsored:\x1b[0m \x1b[1mAcme\x1b[0m " + "m" * 100
        with patch.object(StatusRenderer, "_terminal_width", return_value=60):
            renderer.show(text)
        written = renderer._stream.getvalue()
        self.assertIn("\x1b[2mSponsored:\x1b[0m \x1b[1mAcme\x1b[0m", written)
        self.assertIn(ELLIPSIS + "\x1b[0m\x1b8", written)


if __name__ == "__main__":
    unittest.main()
