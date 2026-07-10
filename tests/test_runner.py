import subprocess
import sys
import unittest
from unittest import mock

from sai.runner import (
    CommandRunner,
    POSIX_INTERRUPT_GRACE_SECONDS,
    WINDOWS_INTERRUPT_GRACE_SECONDS,
    WINDOWS_KILL_WAIT_SECONDS,
    _settle_ready_sponsor,
    normalize_exit_code,
    terminate_process,
    resolve_command,
)


class RunnerTests(unittest.TestCase):
    def test_resolve_command_preserves_absolute_executable(self):
        resolved = resolve_command([sys.executable, "--version"])
        self.assertEqual(resolved[0], sys.executable)
        self.assertEqual(resolved[1:], ["--version"])

    def test_resolve_command_keeps_missing_command_for_popen_error(self):
        resolved = resolve_command(["definitely-not-a-real-sai-command"])
        self.assertEqual(resolved, ["definitely-not-a-real-sai-command"])

    def test_short_run_defers_sponsor_import(self):
        runner = CommandRunner({"frequency": "normal"})

        with mock.patch.dict(sys.modules, {"sai.sponsors": None}):
            receipt = runner.run([sys.executable, "-c", "pass"])

        self.assertEqual(receipt.exit_code, 0)

    def test_settled_receipt_converts_interrupt_during_sponsor_settle_to_130(self):
        class InterruptingSession:
            id = "sess_test"
            qualified_waits = 1

            def settle(self):
                raise KeyboardInterrupt

        wallet = mock.Mock()
        wallet.balance.return_value = 2.5
        runner = CommandRunner({"frequency": "normal"}, wallet=wallet)

        with mock.patch("sai.runner.time.monotonic", return_value=12.0):
            receipt = runner._settled_receipt(10.0, InterruptingSession(), 0)

        self.assertEqual(receipt.exit_code, 130)
        self.assertEqual(receipt.duration_seconds, 2.0)
        self.assertEqual(receipt.credits_earned, 0.0)
        self.assertEqual(receipt.qualified_waits, 1)
        self.assertEqual(receipt.balance, 2.5)
        self.assertEqual(receipt.session_id, "sess_test")

    def test_settled_receipt_preserves_prior_eager_earnings_on_final_interrupt(self):
        class InterruptingSession:
            id = "sess_test"
            qualified_waits = 1
            earned = 0.25

            def settle(self):
                raise KeyboardInterrupt

        wallet = mock.Mock()
        wallet.balance.return_value = 2.5
        runner = CommandRunner({"frequency": "normal"}, wallet=wallet)

        with mock.patch("sai.runner.time.monotonic", return_value=12.0):
            receipt = runner._settled_receipt(10.0, InterruptingSession(), 0)

        self.assertEqual(receipt.exit_code, 130)
        self.assertEqual(receipt.credits_earned, 0.25)
        self.assertEqual(receipt.qualified_waits, 1)

    def test_settle_ready_sponsor_settles_when_progress_is_eligible(self):
        session = mock.Mock()
        session.reward_progress.return_value = {
            "visible_seconds": 5.2,
            "remaining_seconds": 0.0,
            "progress": 1.0,
            "eligible": True,
        }

        _settle_ready_sponsor(session, 12.5)

        session.reward_progress.assert_called_once_with(12.5)
        session.settle.assert_called_once_with(12.5)

    def test_settle_ready_sponsor_ignores_ineligible_progress(self):
        session = mock.Mock()
        session.reward_progress.return_value = {
            "visible_seconds": 2.0,
            "remaining_seconds": 3.0,
            "progress": 0.4,
            "eligible": False,
        }

        _settle_ready_sponsor(session, 12.5)

        session.reward_progress.assert_called_once_with(12.5)
        session.settle.assert_not_called()

    def _stub_status(self):
        status = mock.Mock()
        status.width = 80
        status.__enter__ = mock.Mock(return_value=status)
        status.__exit__ = mock.Mock(return_value=False)
        return status

    def test_passthrough_surfaces_fullscreen_tui_sponsor_inline(self):
        card = mock.Mock()
        card.footer.return_value = "Sponsored: Acme - hi"
        session = mock.Mock(id="sess_x", qualified_waits=0)
        session.maybe_card.return_value = card
        session.settle.return_value = 0.0
        status = self._stub_status()

        wallet = mock.Mock()
        wallet.balance.return_value = 0.0
        runner = CommandRunner({"frequency": "high"}, wallet=wallet)
        runner._sponsor_session = mock.Mock(return_value=session)

        with mock.patch("sai.runner.interactive_terminal", return_value=True), \
                mock.patch("sai.runner.StatusRenderer", return_value=status), \
                mock.patch("sai.runner.resolve_command", side_effect=list):
            runner._run_passthrough([sys.executable, "-c", "pass"], tool="claude")

        # The card is printed inline (survives a TUI repaint), never overlaid.
        status.note.assert_called_once_with("Sponsored: Acme - hi")
        status.show.assert_not_called()
        card.footer.assert_called_once_with(width=80)

    def test_inline_banner_is_printed_before_the_agent_spawns(self):
        # The whole point of the inline path: print the sponsor before the TUI
        # paints, or its first repaint wipes the one-shot line.
        order = []
        card = mock.Mock(sponsor="Acme")
        card.footer.return_value = "SPONSOR LINE"
        session = mock.Mock(id="s", qualified_waits=0)
        session.maybe_card.return_value = card
        session.settle.return_value = 0.0
        status = self._stub_status()
        status.note.side_effect = lambda *a, **k: order.append("note")
        proc = mock.Mock()
        proc.poll.return_value = 0
        proc.returncode = 0

        def popen(*_a, **_k):
            order.append("popen")
            return proc

        wallet = mock.Mock()
        wallet.balance.return_value = 0.0
        runner = CommandRunner({"frequency": "high"}, wallet=wallet)
        runner._sponsor_session = mock.Mock(return_value=session)

        with mock.patch("sai.runner.interactive_terminal", return_value=True), \
                mock.patch("sai.runner.StatusRenderer", return_value=status), \
                mock.patch("sai.runner.resolve_command", side_effect=list), \
                mock.patch("sai.runner.subprocess.Popen", side_effect=popen):
            runner._run_passthrough(["claude"], tool="claude")

        self.assertEqual(order, ["note", "popen"])

    def test_windows_codex_passthrough_does_not_print_inline_sponsor(self):
        status = self._stub_status()
        wallet = mock.Mock()
        wallet.balance.return_value = 0.0
        runner = CommandRunner({"frequency": "high"}, wallet=wallet)
        runner._sponsor_session = mock.Mock()
        proc = mock.Mock()
        proc.poll.return_value = 0
        proc.returncode = 0

        with mock.patch("sai.runner._is_windows", return_value=True), \
                mock.patch("sai.runner.interactive_terminal", return_value=True), \
                mock.patch("sai.runner.StatusRenderer", return_value=status), \
                mock.patch("sai.runner.resolve_command", side_effect=list), \
                mock.patch("sai.runner.subprocess.Popen", return_value=proc):
            receipt = runner._run_passthrough(["codex"], tool="codex")

        status.note.assert_not_called()
        status.show.assert_not_called()
        runner._sponsor_session.assert_not_called()
        self.assertEqual(receipt.exit_code, 0)
        self.assertEqual(receipt.qualified_waits, 0)

    def test_passthrough_does_not_inline_for_non_tui_tool(self):
        status = self._stub_status()
        wallet = mock.Mock()
        wallet.balance.return_value = 0.0
        runner = CommandRunner({"frequency": "high"}, wallet=wallet)
        runner._sponsor_session = mock.Mock()

        with mock.patch("sai.runner.interactive_terminal", return_value=True), \
                mock.patch("sai.runner.StatusRenderer", return_value=status), \
                mock.patch("sai.runner.resolve_command", side_effect=list):
            runner._run_passthrough([sys.executable, "-c", "pass"], tool="run")

        status.note.assert_not_called()
        runner._sponsor_session.assert_not_called()

    def test_run_routes_windows_claude_to_conpty_compositor(self):
        runner = CommandRunner({"frequency": "normal"})
        runner._run_windows_pty = mock.Mock(return_value="WPTY")
        runner._run_passthrough = mock.Mock(return_value="PASS")
        with mock.patch("sai.runner._is_windows", return_value=True), \
                mock.patch("sai.runner.interactive_terminal", return_value=True), \
                mock.patch("sai.runner._winpty_available", return_value=True):
            result = runner.run(["claude"], tool="claude")
        self.assertEqual(result, "WPTY")
        runner._run_windows_pty.assert_called_once()
        runner._run_passthrough.assert_not_called()

    def test_run_keeps_codex_passthrough_on_windows(self):
        runner = CommandRunner({"frequency": "normal"})
        runner._run_windows_pty = mock.Mock(return_value="WPTY")
        runner._run_passthrough = mock.Mock(return_value="PASS")
        with mock.patch("sai.runner._is_windows", return_value=True), \
                mock.patch("sai.runner.interactive_terminal", return_value=True), \
                mock.patch("sai.runner._winpty_available", return_value=True):
            result = runner.run(["codex"], tool="codex")
        self.assertEqual(result, "PASS")
        runner._run_windows_pty.assert_not_called()
        runner._run_passthrough.assert_called_once()

    def test_run_keeps_passthrough_for_non_tui_tool_on_windows(self):
        runner = CommandRunner({"frequency": "normal"})
        runner._run_windows_pty = mock.Mock()
        runner._run_passthrough = mock.Mock(return_value="PASS")
        with mock.patch("sai.runner._is_windows", return_value=True), \
                mock.patch("sai.runner.interactive_terminal", return_value=True), \
                mock.patch("sai.runner._winpty_available", return_value=True):
            result = runner.run([sys.executable, "-c", "pass"], tool="run")
        self.assertEqual(result, "PASS")
        runner._run_windows_pty.assert_not_called()

    def test_run_falls_back_to_passthrough_when_winpty_missing(self):
        runner = CommandRunner({"frequency": "normal"})
        runner._run_windows_pty = mock.Mock()
        runner._run_passthrough = mock.Mock(return_value="PASS")
        with mock.patch("sai.runner._is_windows", return_value=True), \
                mock.patch("sai.runner.interactive_terminal", return_value=True), \
                mock.patch("sai.runner._winpty_available", return_value=False):
            result = runner.run(["codex"], tool="codex")
        self.assertEqual(result, "PASS")
        runner._run_windows_pty.assert_not_called()

    def test_normalize_exit_code_maps_signals_to_shell_convention(self):
        self.assertEqual(normalize_exit_code(-2), 130)
        self.assertEqual(normalize_exit_code(7), 7)

    def test_terminate_process_uses_short_posix_grace_period(self):
        process = mock.Mock()
        process.pid = 123
        process.poll.return_value = None
        process.wait.return_value = 130

        with mock.patch("sai.runner.os.name", "posix"), mock.patch("sai.runner.os.killpg", create=True) as killpg:
            terminate_process(process)

        killpg.assert_called_once()
        process.wait.assert_called_once_with(timeout=POSIX_INTERRUPT_GRACE_SECONDS)
        process.kill.assert_not_called()

    def test_terminate_process_uses_short_windows_grace_period(self):
        process = mock.Mock()
        process.pid = 123
        process.poll.return_value = None
        process.wait.return_value = 130

        with mock.patch("sai.runner.os.name", "nt"):
            terminate_process(process)

        process.wait.assert_called_once_with(timeout=WINDOWS_INTERRUPT_GRACE_SECONDS)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_terminate_process_kills_windows_tree_after_timeout(self):
        process = mock.Mock()
        process.pid = 123
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("sai", WINDOWS_INTERRUPT_GRACE_SECONDS), 130]

        with mock.patch("sai.runner.os.name", "nt"), \
                mock.patch("sai.runner._kill_windows_process_tree", return_value=True) as kill_tree:
            terminate_process(process)

        kill_tree.assert_called_once_with(123)
        self.assertEqual(
            process.wait.call_args_list,
            [
                mock.call(timeout=WINDOWS_INTERRUPT_GRACE_SECONDS),
                mock.call(timeout=WINDOWS_KILL_WAIT_SECONDS),
            ],
        )
        process.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
