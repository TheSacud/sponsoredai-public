import os
import tempfile
import unittest
from unittest import mock

from sai.config import kill_switch_active, load_config
from sai.overlay.tray import (
    FREQ_BY_ID,
    ID_PRIVACY,
    ID_QUIT,
    ID_TERMS,
    ID_TOGGLE,
    TrayController,
)
from sai.overlay.macos import is_macos
from sai.overlay.win32 import is_windows


class TrayControllerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        prev = os.environ.get("SAI_HOME")
        os.environ["SAI_HOME"] = tmp.name  # keep kill-switch/config files in temp

        def restore():
            if prev is None:
                os.environ.pop("SAI_HOME", None)
            else:
                os.environ["SAI_HOME"] = prev

        self.addCleanup(restore)
        self.opened = []
        self.quit_called = []
        self.config = {"backend_url": "https://sponsoredai.dev", "frequency": "normal", "ads_enabled": True}
        self.ctl = TrayController(
            self.config,
            on_quit=lambda: self.quit_called.append(True),
            opener=self.opened.append,
        )

    def test_items_reflect_current_state(self):
        items = self.ctl.items()
        toggle = next(i for i in items if i.get("id") == ID_TOGGLE)
        self.assertTrue(toggle["checked"])  # ads enabled (no kill switch)
        normal = next(i for i in items if i.get("label") == "Frequency: normal")
        self.assertTrue(normal["checked"])

    def test_toggle_flips_the_kill_switch(self):
        self.assertFalse(kill_switch_active())
        self.ctl.invoke(ID_TOGGLE)
        self.assertTrue(kill_switch_active())  # ads turned off
        self.ctl.invoke(ID_TOGGLE)
        self.assertFalse(kill_switch_active())  # back on

    def test_frequency_updates_live_config_and_persists(self):
        high_id = next(k for k, v in FREQ_BY_ID.items() if v == "high")
        self.ctl.invoke(high_id)
        self.assertEqual(self.config["frequency"], "high")  # live dict updated
        self.assertTrue(self.config["ads_enabled"])
        self.assertEqual(load_config()["frequency"], "high")  # and persisted
        off_id = next(k for k, v in FREQ_BY_ID.items() if v == "off")
        self.ctl.invoke(off_id)
        self.assertFalse(self.config["ads_enabled"])

    def test_terms_and_privacy_open_backend_urls(self):
        self.ctl.invoke(ID_TERMS)
        self.ctl.invoke(ID_PRIVACY)
        self.assertEqual(
            self.opened,
            ["https://sponsoredai.dev/terms", "https://sponsoredai.dev/privacy"],
        )

    def test_quit_calls_the_callback(self):
        self.ctl.invoke(ID_QUIT)
        self.assertEqual(self.quit_called, [True])

    def test_default_opener_refuses_tampered_backend_url(self):
        # The default opener routes through sai.browser.open_url, so a config
        # whose backend_url carries a dangerous scheme can't launch it on click.
        ctl = TrayController(
            {"backend_url": "file://attacker-host/share"},
            on_quit=lambda: None,
        )
        with mock.patch("sai.browser.webbrowser.open") as opener:
            ctl.invoke(ID_TERMS)
            ctl.invoke(ID_PRIVACY)
        opener.assert_not_called()


@unittest.skipUnless(is_macos(), "MacStatusItem requires macOS")
class MacStatusItemSmokeTests(unittest.TestCase):
    """The macOS status item is the tray's counterpart and drives the same
    TrayController, so the open_url guard must hold on the real native menu too."""

    def _status_item(self, backend_url):
        from sai.overlay.macos import MacStatusItem

        controller = TrayController(
            {"backend_url": backend_url},
            on_quit=lambda: None,
        )
        item = MacStatusItem(controller)
        self.addCleanup(item.close)
        return item

    def test_invoke_routes_through_guard_and_refuses_file_scheme(self):
        item = self._status_item("file://attacker-host/share")
        with mock.patch("sai.browser.webbrowser.open") as opener:
            item._invoke(ID_TERMS)
            item._invoke(ID_PRIVACY)
        opener.assert_not_called()

    def test_invoke_opens_valid_https_backend_url(self):
        item = self._status_item("https://sponsoredai.dev")
        with mock.patch("sai.browser.webbrowser.open") as opener:
            item._invoke(ID_TERMS)
        opener.assert_called_once_with("https://sponsoredai.dev/terms")


@unittest.skipUnless(is_windows(), "TrayIcon requires Windows")
class TrayIconSmokeTests(unittest.TestCase):
    def test_create_and_close_adds_a_real_icon(self):
        from sai.overlay.tray import TrayIcon

        controller = TrayController({}, on_quit=lambda: None, opener=lambda _u: None)
        icon = TrayIcon(controller, tooltip="SAI test")
        try:
            self.assertTrue(icon.added)  # Shell_NotifyIcon NIM_ADD succeeded
        finally:
            icon.close()
        self.assertFalse(icon.added)


if __name__ == "__main__":
    unittest.main()
