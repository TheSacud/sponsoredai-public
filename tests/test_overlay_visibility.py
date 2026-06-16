import types
import unittest

from sai.overlay.visibility import (
    DEFAULT_IDLE_THRESHOLD_SECONDS,
    VisibilityMonitor,
    any_of,
    claude_desktop_matcher,
    codex_desktop_matcher,
    image_path_matcher,
    mock_foreground_matcher,
)
from sai.overlay.win32 import is_windows


CLAUDE_STORE = (
    r"C:\Program Files\WindowsApps"
    r"\Claude_1.12603.1.0_x64__pzs8sxrjxfjjc\app\Claude.exe"
)
CLAUDE_SQUIRREL = r"C:\Users\Duarte\AppData\Local\AnthropicClaude\app-1.10628.0\Claude.exe"
CLAUDE_CODE_CLI = r"C:\Users\Duarte\AppData\Roaming\Claude\claude-code\2.1.170\claude.exe"
CODEX_CLI = r"C:\Users\Duarte\AppData\Local\OpenAI\Codex\bin\codex.exe"
CODEX_GUI = (
    r"C:\Program Files\WindowsApps"
    r"\OpenAI.Codex_26.609.4994.0_x64__2p2nqsd0c76g0\app\Codex.exe"
)
CODEX_CLI_BUNDLED = (
    r"C:\Program Files\WindowsApps"
    r"\OpenAI.Codex_26.609.4994.0_x64__2p2nqsd0c76g0\app\resources\codex.exe"
)
CLAUDE_MAC = "/Applications/Claude.app/Contents/MacOS/Claude"
CLAUDE_DESKTOP_MAC = "/Applications/Claude Desktop.app/Contents/MacOS/Claude"
CLAUDE_CODE_MAC = "/Users/duarte/.npm/_npx/abc/node_modules/.bin/claude"
CODEX_MAC = "/Applications/Codex.app/Contents/MacOS/Codex"
OPENAI_CODEX_MAC = "/Applications/OpenAI Codex.app/Contents/MacOS/Codex"
CODEX_CLI_MAC = "/opt/homebrew/bin/codex"


class FakeProbe:
    """In-memory SystemProbe: every reading is a settable attribute so a test can
    pose any window arrangement without touching Win32."""

    TARGET = 100
    OVERLAY = 200

    def __init__(self):
        self.fg = self.TARGET
        self.paths = {self.TARGET: CLAUDE_STORE}
        self.visible = {self.OVERLAY: True}
        self.minimized = {}
        self.cloaked = {}
        self.monitors = {self.TARGET: 1, self.OVERLAY: 1}
        self.idle = 0.0

    def foreground_window(self):
        return self.fg

    def process_image_path(self, hwnd):
        return self.paths.get(hwnd)

    def window_rect(self, hwnd):
        return None

    def is_window_visible(self, hwnd):
        return self.visible.get(hwnd, False)

    def is_minimized(self, hwnd):
        return self.minimized.get(hwnd, False)

    def is_cloaked(self, hwnd):
        return self.cloaked.get(hwnd, False)

    def monitor_of(self, hwnd):
        return self.monitors.get(hwnd, 0)

    def idle_seconds(self):
        return self.idle


class ClaudeDesktopMatcherTests(unittest.TestCase):
    def setUp(self):
        self.match = claude_desktop_matcher()

    def test_matches_store_build(self):
        self.assertTrue(self.match(CLAUDE_STORE))

    def test_matches_squirrel_build(self):
        self.assertTrue(self.match(CLAUDE_SQUIRREL))

    def test_matches_macos_app_bundle(self):
        self.assertTrue(self.match(CLAUDE_MAC))
        self.assertTrue(self.match(CLAUDE_DESKTOP_MAC))

    def test_rejects_claude_code_cli(self):
        # The CLI must never be mistaken for the desktop GUI, or the overlay would
        # fire over a terminal where the in-terminal compositor already bills.
        self.assertFalse(self.match(CLAUDE_CODE_CLI))
        self.assertFalse(self.match(CLAUDE_CODE_MAC))

    def test_rejects_codex(self):
        self.assertFalse(self.match(CODEX_CLI))

    def test_rejects_empty_and_unrelated(self):
        self.assertFalse(self.match(""))
        self.assertFalse(self.match(r"C:\Windows\explorer.exe"))

    def test_is_case_and_separator_insensitive(self):
        forward = CLAUDE_STORE.replace("\\", "/").upper()
        self.assertTrue(self.match(forward))


class CodexDesktopMatcherTests(unittest.TestCase):
    def setUp(self):
        self.match = codex_desktop_matcher()

    def test_matches_store_gui(self):
        self.assertTrue(self.match(CODEX_GUI))

    def test_matches_macos_app_bundle(self):
        self.assertTrue(self.match(CODEX_MAC))
        self.assertTrue(self.match(OPENAI_CODEX_MAC))

    def test_rejects_bundled_cli_engine(self):
        # ...\app\resources\codex.exe is the CLI engine, not the GUI window.
        self.assertFalse(self.match(CODEX_CLI_BUNDLED))

    def test_rejects_standalone_cli(self):
        self.assertFalse(self.match(CODEX_CLI))
        self.assertFalse(self.match(CODEX_CLI_MAC))

    def test_rejects_claude_and_unrelated(self):
        self.assertFalse(self.match(CLAUDE_STORE))
        self.assertFalse(self.match(""))

    def test_is_case_and_separator_insensitive(self):
        self.assertTrue(self.match(CODEX_GUI.replace("\\", "/").upper()))


class HelperMatcherTests(unittest.TestCase):
    def test_image_path_matcher_anchors_on_normalised_path(self):
        match = image_path_matcher(r"\\openai\\codex\\bin\\codex\.exe$")
        self.assertTrue(match(CODEX_CLI))
        self.assertFalse(match(CLAUDE_STORE))

    def test_any_of_unions_matchers(self):
        match = any_of(claude_desktop_matcher(), image_path_matcher(r"\\codex\.exe$"))
        self.assertTrue(match(CLAUDE_STORE))
        self.assertTrue(match(CODEX_CLI))
        self.assertFalse(match(CLAUDE_CODE_CLI))

    def test_mock_foreground_matcher_accepts_any_real_path(self):
        match = mock_foreground_matcher()
        self.assertTrue(match(CODEX_CLI))
        self.assertFalse(match(""))


class VisibilityMonitorTests(unittest.TestCase):
    def _monitor(self, probe):
        return VisibilityMonitor(
            probe,
            claude_desktop_matcher(),
            overlay_hwnd=FakeProbe.OVERLAY,
        )

    def test_live_when_everything_aligns(self):
        state = self._monitor(FakeProbe()).sample()
        self.assertTrue(state.live)
        self.assertTrue(state.target_foreground)
        self.assertTrue(state.overlay_visible)
        self.assertTrue(state.same_monitor)
        self.assertTrue(state.user_present)
        self.assertEqual(state.target_hwnd, FakeProbe.TARGET)

    def test_not_live_when_foreground_is_not_target(self):
        probe = FakeProbe()
        probe.paths[FakeProbe.TARGET] = r"C:\Windows\System32\notepad.exe"
        state = self._monitor(probe).sample()
        self.assertFalse(state.live)
        self.assertFalse(state.target_foreground)
        self.assertEqual(state.target_hwnd, 0)

    def test_not_live_when_foreground_is_claude_code_cli(self):
        probe = FakeProbe()
        probe.paths[FakeProbe.TARGET] = CLAUDE_CODE_CLI
        self.assertFalse(self._monitor(probe).sample().live)

    def test_not_live_without_an_overlay_window(self):
        probe = FakeProbe()
        monitor = VisibilityMonitor(probe, claude_desktop_matcher(), overlay_hwnd=None)
        state = monitor.sample()
        self.assertFalse(state.live)
        self.assertFalse(state.overlay_visible)

    def test_not_live_when_overlay_minimized(self):
        probe = FakeProbe()
        probe.minimized[FakeProbe.OVERLAY] = True
        self.assertFalse(self._monitor(probe).sample().live)

    def test_not_live_when_overlay_cloaked(self):
        probe = FakeProbe()
        probe.cloaked[FakeProbe.OVERLAY] = True
        self.assertFalse(self._monitor(probe).sample().live)

    def test_not_live_on_different_monitors(self):
        probe = FakeProbe()
        probe.monitors[FakeProbe.OVERLAY] = 2
        state = self._monitor(probe).sample()
        self.assertFalse(state.live)
        self.assertFalse(state.same_monitor)

    def test_not_live_when_target_monitor_unknown(self):
        # MonitorFromWindow returns 0 when the window is off every monitor; a
        # zero must never compare equal to a zero overlay monitor as "same".
        probe = FakeProbe()
        probe.monitors[FakeProbe.TARGET] = 0
        probe.monitors[FakeProbe.OVERLAY] = 0
        self.assertFalse(self._monitor(probe).sample().same_monitor)

    def test_user_present_threshold_boundary(self):
        probe = FakeProbe()
        probe.idle = DEFAULT_IDLE_THRESHOLD_SECONDS - 0.01
        self.assertTrue(self._monitor(probe).sample().user_present)
        probe.idle = DEFAULT_IDLE_THRESHOLD_SECONDS
        self.assertFalse(self._monitor(probe).sample().user_present)

    def test_not_live_when_user_idle(self):
        probe = FakeProbe()
        probe.idle = DEFAULT_IDLE_THRESHOLD_SECONDS + 5
        self.assertFalse(self._monitor(probe).sample().live)

    def test_not_present_when_idle_is_unprovable_infinite(self):
        # The probe reports inf when presence can't be proven (fail closed).
        probe = FakeProbe()
        probe.idle = float("inf")
        state = self._monitor(probe).sample()
        self.assertFalse(state.user_present)
        self.assertFalse(state.live)

    def test_set_overlay_hwnd_accepts_a_lazy_getter(self):
        probe = FakeProbe()
        monitor = VisibilityMonitor(probe, claude_desktop_matcher(), overlay_hwnd=None)
        self.assertFalse(monitor.sample().live)
        monitor.set_overlay_hwnd(lambda: FakeProbe.OVERLAY)
        self.assertTrue(monitor.sample().live)


@unittest.skipUnless(is_windows(), "Win32Probe requires Windows")
class Win32ProbeFailClosedTests(unittest.TestCase):
    """The real probe must fail CLOSED for billing when an OS query fails."""

    def _probe(self):
        from sai.overlay.win32 import default_probe

        return default_probe()

    def test_idle_seconds_is_infinite_when_getlastinputinfo_fails(self):
        probe = self._probe()
        probe._user32 = types.SimpleNamespace(GetLastInputInfo=lambda _ref: 0)
        self.assertEqual(probe.idle_seconds(), float("inf"))

    def test_is_cloaked_true_when_dwm_unavailable(self):
        probe = self._probe()
        probe._dwmapi = None
        self.assertTrue(probe.is_cloaked(1234))

    def test_is_minimized_true_on_probe_error(self):
        probe = self._probe()

        def boom(_hwnd):
            raise OSError("simulated failure")

        probe._user32 = types.SimpleNamespace(IsIconic=boom)
        self.assertTrue(probe.is_minimized(1234))


if __name__ == "__main__":
    unittest.main()
