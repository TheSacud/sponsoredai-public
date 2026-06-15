import base64
import unittest

from sai.overlay.branding import icon_url_rejection
from sai.overlay.win32 import is_windows


def resolver_returning(ip):
    def resolve(host, port):
        return [(2, 1, 6, "", (ip, port))]
    return resolve


class IconUrlRejectionTests(unittest.TestCase):
    PUBLIC = staticmethod(resolver_returning("93.184.216.34"))  # example.com, public

    def test_accepts_public_https(self):
        self.assertIsNone(icon_url_rejection("https://cdn.example.com/logo.png", resolve=self.PUBLIC))

    def test_rejects_non_https(self):
        self.assertEqual(icon_url_rejection("http://cdn.example.com/logo.png", resolve=self.PUBLIC), "not_https")
        self.assertEqual(icon_url_rejection("ftp://x/logo.png", resolve=self.PUBLIC), "not_https")
        self.assertEqual(icon_url_rejection("file:///etc/passwd", resolve=self.PUBLIC), "not_https")

    def test_rejects_missing_host(self):
        self.assertEqual(icon_url_rejection("https:///logo.png", resolve=self.PUBLIC), "no_host")

    def test_rejects_non_global_addresses(self):
        # Anything not globally routable -- including CGNAT/shared (100.64/10),
        # IETF-protocol (192.0.0.0/24) and benchmarking (198.18/15) ranges that
        # are NOT flagged is_private -- must be rejected.
        for ip, host in [("127.0.0.1", "localhost"), ("10.0.0.5", "intra"),
                         ("192.168.1.1", "router"), ("169.254.169.254", "metadata"),
                         ("0.0.0.0", "zero"), ("100.64.0.1", "cgnat"),
                         ("192.0.0.1", "ietf"), ("198.18.0.1", "bench")]:
            self.assertEqual(
                icon_url_rejection(f"https://{host}/logo.png", resolve=resolver_returning(ip)),
                "non_public_ip",
                msg=ip,
            )

    def test_rejects_credentials(self):
        self.assertEqual(
            icon_url_rejection("https://user:pw@cdn.example.com/logo.png", resolve=self.PUBLIC),
            "credentials",
        )

    def test_rejects_overlong_url(self):
        long_url = "https://cdn.example.com/" + "a" * 3000 + ".png"
        self.assertEqual(icon_url_rejection(long_url, resolve=self.PUBLIC), "too_long")

    def test_rejects_when_dns_fails(self):
        def boom(host, port):
            raise OSError("no such host")
        self.assertEqual(icon_url_rejection("https://nope.example/logo.png", resolve=boom), "dns_failed")

    def test_rejects_any_private_among_resolved_addresses(self):
        # If a host resolves to BOTH public and private, reject (DNS rebinding-ish).
        def mixed(host, port):
            return [(2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", ("127.0.0.1", port))]
        self.assertEqual(icon_url_rejection("https://sneaky.example/logo.png", resolve=mixed), "non_public_ip")


class AssetsTests(unittest.TestCase):
    def test_sai_mark_decodes_to_png_bytes(self):
        from sai.overlay.assets import sai_mark_png

        data = sai_mark_png()
        self.assertGreater(len(data), 100)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")  # PNG signature


class TextSurfaceRewardProgressTests(unittest.TestCase):
    def test_reward_progress_normalises_fill_amount_then_ready(self):
        from sai.overlay.surface import TextSurface

        repaint_calls = []
        s = TextSurface()
        self.addCleanup(s.dispose)
        s.set_repaint(lambda: repaint_calls.append(True))

        s.set_reward_progress({"visible_seconds": 0.0, "remaining_seconds": 5.0, "eligible": False})
        self.assertEqual(s._reward_progress, (0.0, False))
        self.assertEqual(len(repaint_calls), 1)

        s.set_reward_progress({"visible_seconds": 2.0, "remaining_seconds": 3.0, "eligible": False})
        self.assertEqual(s._reward_progress, (0.4, False))
        self.assertEqual(len(repaint_calls), 2)

        s.set_reward_progress({"visible_seconds": 5.0, "remaining_seconds": 0.0, "eligible": True})
        self.assertEqual(s._reward_progress, (1.0, True))
        self.assertEqual(len(repaint_calls), 3)


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


@unittest.skipUnless(is_windows(), "GDI+ logo decoding requires Windows")
class TextSurfaceLogoTests(unittest.TestCase):
    def _surface(self):
        from sai.overlay.surface import TextSurface

        s = TextSurface()
        self.addCleanup(s.dispose)
        return s

    def test_gdiplus_available_and_decodes_png(self):
        s = self._surface()
        self.assertIsNotNone(s._gdiplus)
        self.assertTrue(s._decode(_PNG_1X1))

    def test_decode_rejects_non_image_bytes(self):
        s = self._surface()
        self.assertIsNone(s._decode(b"this is not an image"))

    def test_left_offset_reserves_a_logo_slot(self):
        from sai.sponsors import SponsorCard

        s = self._surface()
        with_logo = SponsorCard(id="a", sponsor="Acme", message="m", url="https://x/y",
                                credit_amount=0.0, placement_id="p", brand_icon_url="https://x/logo.png")
        # A real placement (placement_id set) with no icon and not example -> rail.
        without = SponsorCard(id="b", sponsor="Acme", message="m", url="https://x/y",
                              credit_amount=0.0, placement_id="p")
        self.assertGreater(s._left_offset(with_logo, 96), s._left_offset(without, 96))

    def test_example_card_uses_the_bundled_local_logo(self):
        from sai.sponsors import SponsorCard

        s = self._surface()
        example = SponsorCard(id="ex", sponsor="Demo", message="m", url="https://x/y", credit_amount=0.0)
        self.assertTrue(example.is_example)  # placement_id is None
        self.assertTrue(s._has_logo(example))
        self.assertTrue(s._logo_handle(example))  # decodes the bundled SAI mark, no network

    def test_real_card_without_icon_has_no_logo(self):
        from sai.sponsors import SponsorCard

        s = self._surface()
        real = SponsorCard(id="r", sponsor="Demo", message="m", url="https://x/y",
                           credit_amount=0.0, placement_id="plc")
        self.assertFalse(s._has_logo(real))
        self.assertIsNone(s._logo_handle(real))


if __name__ == "__main__":
    unittest.main()
