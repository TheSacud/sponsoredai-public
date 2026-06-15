import unittest
from types import SimpleNamespace
from unittest import mock

from sai.cli import build_parser, main
from sai.overlay.app import OVERLAY_TOOL, _open_sponsor, build_credit0_session, run_overlay


class BuildSessionTests(unittest.TestCase):
    def test_credit0_session_has_no_placement_client(self):
        # Even when the config could build a real backend client, Phase-1 forces
        # it off so a GUI overlay never contacts the backend or attests
        # terminal_interactive.
        config = {
            "backend_url": "https://sponsoredai.dev",
            "install_id": "ins_test",
            "frequency": "normal",
        }
        session = build_credit0_session(config)
        self.assertIsNone(session.placement_client)
        self.assertEqual(session.tool, OVERLAY_TOOL)

    def test_credit0_example_cards_credit_zero(self):
        # The fallback example cards (used when there is no client) credit 0.
        session = build_credit0_session({"frequency": "normal"})
        card = session._next_card(terminal_is_interactive=True)
        self.assertTrue(card.is_example)
        self.assertEqual(card.credit_amount, 0.0)


class RunOverlayGuardTests(unittest.TestCase):
    def test_unsupported_platform_refuses_cleanly(self):
        with (
            mock.patch("sai.overlay.app.is_windows", return_value=False),
            mock.patch("sai.overlay.app.is_macos", return_value=False),
        ):
            self.assertEqual(run_overlay(), 1)

    def test_macos_is_a_supported_overlay_platform(self):
        from sai.overlay.app import _platform_supported

        with (
            mock.patch("sai.overlay.app.is_windows", return_value=False),
            mock.patch("sai.overlay.app.is_macos", return_value=True),
        ):
            self.assertTrue(_platform_supported())

    def test_macos_backend_wires_tray_controller(self):
        class FakeSurface:
            pass

        class FakeWindow:
            hwnd = 123
            dpi = 96

            def __init__(self, _surface, *, on_click=None, on_dismiss=None):
                pass

            def close(self):
                pass

        class FakeTray:
            created = False

            def __init__(self, controller):
                self.controller = controller
                FakeTray.created = True

            def close(self):
                pass

        class FakeDriver:
            current_card = None

            def __init__(self, **_kwargs):
                pass

            def dismiss(self):
                pass

            def run(self, stop=None, **_kwargs):
                return 0

        fake_backend = {
            "TextSurface": FakeSurface,
            "OverlayWindow": FakeWindow,
            "TrayIcon": FakeTray,
            "default_probe": lambda: object(),
            "enable_dpi_awareness": lambda: None,
        }
        fake_lock = mock.Mock()
        fake_lock.acquire.return_value = False

        with (
            mock.patch("sai.overlay.app.is_windows", return_value=False),
            mock.patch("sai.overlay.app.is_macos", return_value=True),
            mock.patch("sai.overlay.app._backend", return_value=fake_backend),
            mock.patch("sai.overlay.app.ensure_config_saved", return_value={"frequency": "normal"}),
            mock.patch("sai.overlay.app.billing_authority_lock", return_value=fake_lock),
            mock.patch("sai.overlay.app.SessionDriver", FakeDriver),
        ):
            self.assertEqual(run_overlay("both", billable=False), 0)
            self.assertTrue(FakeTray.created)


class OpenSponsorTests(unittest.TestCase):
    @staticmethod
    def _driver(*, click_url=None, url=None):
        card = SimpleNamespace(click_url=click_url, url=url)
        return SimpleNamespace(current_card=card)

    def test_opens_https_sponsor_url(self):
        driver = self._driver(url="https://sponsoredai.dev/sponsor")
        with mock.patch("sai.browser.webbrowser.open") as opener:
            _open_sponsor(driver)
        opener.assert_called_once_with("https://sponsoredai.dev/sponsor")

    def test_prefers_tracked_click_url(self):
        driver = self._driver(click_url="https://sponsoredai.dev/c/p/t", url="https://example.com")
        with mock.patch("sai.browser.webbrowser.open") as opener:
            _open_sponsor(driver)
        opener.assert_called_once_with("https://sponsoredai.dev/c/p/t")

    def test_rejects_file_scheme_url(self):
        # A backend-supplied file:// URL would leak NetNTLM hashes over SMB on
        # Windows if launched; the overlay must refuse it on click.
        driver = self._driver(url="file://attacker-host/share")
        with mock.patch("sai.browser.webbrowser.open") as opener:
            _open_sponsor(driver)
        opener.assert_not_called()

    def test_rejects_custom_scheme_url(self):
        driver = self._driver(url="search-ms:query=foo")
        with mock.patch("sai.browser.webbrowser.open") as opener:
            _open_sponsor(driver)
        opener.assert_not_called()

    def test_no_card_is_a_noop(self):
        with mock.patch("sai.browser.webbrowser.open") as opener:
            _open_sponsor(SimpleNamespace(current_card=None))
        opener.assert_not_called()


class CliWiringTests(unittest.TestCase):
    def test_parser_exposes_overlay_command(self):
        args = build_parser().parse_args(["overlay"])
        self.assertEqual(args.command_name, "overlay")
        self.assertEqual(args.target, "claude")
        self.assertEqual(args.anchor, "top")  # default: clear of the composer

    def test_parser_accepts_explicit_anchor(self):
        args = build_parser().parse_args(["overlay", "--anchor", "bottom-right"])
        self.assertEqual(args.anchor, "bottom-right")

    def test_bill_is_on_by_default_with_preview_opt_out(self):
        self.assertTrue(build_parser().parse_args(["overlay"]).bill)
        self.assertTrue(build_parser().parse_args(["overlay", "--bill"]).bill)
        self.assertFalse(build_parser().parse_args(["overlay", "--no-bill"]).bill)

    def test_parser_accepts_positional_codex_and_both_targets(self):
        self.assertEqual(build_parser().parse_args(["overlay", "codex"]).target, "codex")
        self.assertEqual(build_parser().parse_args(["overlay", "both"]).target, "both")

    def test_parser_keeps_legacy_target_flag(self):
        self.assertEqual(build_parser().parse_args(["overlay", "--target", "codex"]).target_option, "codex")
        self.assertEqual(build_parser().parse_args(["overlay", "--target", "both"]).target_option, "both")

    def test_main_runs_overlay_billable_by_default(self):
        cases = [
            (["overlay"], {"target": "claude", "anchor": "top", "billable": True}),
            (["overlay", "codex"], {"target": "codex", "anchor": "top", "billable": True}),
            (["overlay", "both", "--no-bill"], {"target": "both", "anchor": "top", "billable": False}),
            (["overlay", "--target", "codex"], {"target": "codex", "anchor": "top", "billable": True}),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv), mock.patch("sai.overlay.app.run_overlay", return_value=0) as run:
                self.assertEqual(main(argv), 0)
                run.assert_called_once_with(**expected)

    def test_target_matcher_resolves_known_targets(self):
        from sai.overlay.app import _target_matcher

        self.assertIsNotNone(_target_matcher("claude"))
        self.assertIsNotNone(_target_matcher("codex"))
        self.assertIsNotNone(_target_matcher("both"))
        self.assertIsNone(_target_matcher("nonsense"))


if __name__ == "__main__":
    unittest.main()
