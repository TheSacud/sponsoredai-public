import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sai.overlay.driver import (
    PERSISTENT_IDLE_SECONDS,
    SessionDriver,
    _STATUS_BY_REASON,
    _STATUS_SHOWING,
)
from sai.overlay.geometry import place_banner
from sai.overlay.lock import InstanceLock, _process_alive
from sai.overlay.visibility import VisibilityState
from sai.overlay.win32 import Rect


def vstate(*, fg=True, present=True, overlay=False, same=False, idle=0.0, hwnd=123):
    return VisibilityState(
        target_hwnd=hwnd if fg else 0,
        target_path=r"C:\...\Claude.exe" if fg else None,
        target_foreground=fg,
        overlay_visible=overlay,
        same_monitor=same,
        user_present=present,
        idle_seconds=idle,
        live=fg and overlay and same and present,
    )


class FakeMonitor:
    def __init__(self, state):
        self.state = state
        self.samples = 0

    def sample(self):
        self.samples += 1
        return self.state


class FakeWindow:
    def __init__(self):
        self.dpi = 96
        self.dpi_set = []
        self.set_cards = []
        self.moves = []
        self.shows = 0
        self.hides = 0
        self.pumps = 0

    def set_dpi(self, value):
        self.dpi_set.append(value)
        self.dpi = value

    def set_card(self, card):
        self.set_cards.append(card)

    def move_to(self, placement):
        self.moves.append(placement)

    def show(self):
        self.shows += 1

    def hide(self):
        self.hides += 1

    def pump(self):
        self.pumps += 1


class FakeSession:
    def __init__(self, cards=None, settle_value=0.0):
        self._cards = list(cards or [])
        self.maybe_calls = []
        self.hidden_at = []
        self.note_inputs = 0
        self.settles = 0
        self._settle_value = settle_value
        self.progress = None
        self.progress_calls = []

    def maybe_card(self, now, idle_for=None, terminal_is_interactive=None):
        self.maybe_calls.append((now, idle_for, terminal_is_interactive))
        return self._cards.pop(0) if self._cards else None

    def mark_cards_hidden(self, now=None):
        self.hidden_at.append(now)

    def reward_progress(self, now=None):
        self.progress_calls.append(now)
        return self.progress

    def note_user_input(self):
        self.note_inputs += 1

    def settle(self, now=None):
        self.settles += 1
        return self._settle_value


class FakeProbe:
    def __init__(self, work_area=None, rect=Rect(100, 100, 1100, 900), dpi=96):
        self._work_area = work_area
        self._rect = rect
        self._dpi = dpi

    def window_rect(self, hwnd):
        return self._rect

    def monitor_work_area(self, hwnd):
        return self._work_area

    def monitor_dpi(self, hwnd):
        return self._dpi


class FakeSurface:
    def __init__(self):
        self.reward_progresses = []

    def measure(self, card, dpi):
        return (400, 44)

    def set_reward_progress(self, progress):
        self.reward_progresses.append(progress)


def build(monitor_state, *, cards=None, enabled=True, settle_value=0.0, work_area=None, rect=Rect(100, 100, 1100, 900), dpi=96, on_status=None):
    session = FakeSession(cards=cards, settle_value=settle_value)
    window = FakeWindow()
    monitor = FakeMonitor(monitor_state)
    driver = SessionDriver(
        monitor=monitor,
        window=window,
        session=session,
        probe=FakeProbe(work_area=work_area, rect=rect, dpi=dpi),
        surface=FakeSurface(),
        config={},
        enabled=(lambda: enabled),
        clock=lambda: 10.0,
        on_status=on_status,
    )
    return driver, monitor, window, session


class SessionDriverTests(unittest.TestCase):
    def test_attended_with_card_shows_positions_and_does_not_freeze_first_card(self):
        driver, monitor, window, session = build(vstate(idle=0.0), cards=["CARD"])
        driver.tick()
        self.assertEqual(session.maybe_calls[0][1], PERSISTENT_IDLE_SECONDS)
        self.assertTrue(session.maybe_calls[0][2])  # terminal_is_interactive
        self.assertEqual(window.set_cards, ["CARD"])
        self.assertEqual(len(window.moves), 1)
        self.assertEqual(window.shows, 1)
        self.assertEqual(session.note_inputs, 1)  # idle 0 < presence threshold
        # First display must NOT close the billing window for the just-shown card.
        self.assertEqual(session.hidden_at, [])

    def test_pushes_reward_progress_to_surface(self):
        driver, monitor, window, session = build(vstate(idle=30.0), cards=["CARD"])
        session.progress = {
            "visible_seconds": 2.0,
            "remaining_seconds": 3.0,
            "progress": 0.4,
            "eligible": False,
        }
        driver.tick()
        self.assertEqual(session.progress_calls, [10.0])
        self.assertEqual(driver._surface.reward_progresses[-1], session.progress)

    def test_hide_clears_reward_progress(self):
        driver, monitor, window, session = build(vstate(fg=False))
        driver._surface.set_reward_progress({"remaining_seconds": 3.0})
        driver.tick()
        self.assertIsNone(driver._surface.reward_progresses[-1])

    def test_banner_is_clamped_to_the_target_window(self):
        # No work area available -> fall back to clamping within the window rect.
        driver, monitor, window, session = build(vstate(), cards=["CARD"])
        driver.tick()
        target = Rect(100, 100, 1100, 900)  # FakeProbe's rect
        expected = place_banner(target, 400, 44, anchor="top", bounds=target)
        self.assertEqual(window.moves[0], expected)

    def test_banner_clamped_to_work_area_when_window_overflows_screen(self):
        work = Rect(0, 0, 500, 500)
        driver, monitor, window, session = build(vstate(), cards=["CARD"], work_area=work)
        driver.tick()
        target = Rect(100, 100, 1100, 900)
        expected = place_banner(target, 400, 44, anchor="top", bounds=work)
        self.assertEqual(window.moves[0], expected)

    def test_not_attended_hides_and_closes_billing(self):
        driver, monitor, window, session = build(vstate(fg=False))
        driver.tick()
        self.assertEqual(session.maybe_calls, [])
        self.assertEqual(window.hides, 1)
        self.assertEqual(session.hidden_at, [10.0])

    def test_disabled_short_circuits_before_sampling(self):
        driver, monitor, window, session = build(vstate(), cards=["CARD"], enabled=False)
        result = driver.tick()
        self.assertIsNone(result)
        self.assertEqual(monitor.samples, 0)
        self.assertEqual(session.maybe_calls, [])
        self.assertEqual(window.hides, 1)
        self.assertEqual(session.hidden_at, [10.0])

    def test_user_input_not_noted_when_idle_high(self):
        driver, monitor, window, session = build(vstate(idle=30.0), cards=["CARD"])
        driver.tick()
        self.assertEqual(session.note_inputs, 0)

    def test_integrity_closes_billing_when_displaying_but_not_visible(self):
        # Tick 1: attended, card shown, overlay not yet visible -> displaying set.
        driver, monitor, window, session = build(vstate(overlay=False, same=False), cards=["CARD"])
        session.progress = {"visible_seconds": 2.0, "remaining_seconds": 3.0, "eligible": False}
        driver.tick()
        self.assertEqual(session.hidden_at, [])
        # Tick 2: still attended, no new card, banner reported cloaked/off-monitor.
        monitor.state = vstate(overlay=False, same=False, idle=30.0)
        driver.tick()
        self.assertEqual(session.hidden_at, [10.0])  # integrity close fired
        self.assertIsNone(driver._surface.reward_progresses[-1])

    def test_no_integrity_close_while_genuinely_live(self):
        driver, monitor, window, session = build(vstate(overlay=True, same=True), cards=["CARD"])
        driver.tick()
        monitor.state = vstate(overlay=True, same=True, idle=30.0)
        driver.tick()
        self.assertEqual(session.hidden_at, [])  # stayed visible, never closed

    def test_measures_and_paints_at_target_monitor_dpi(self):
        driver, monitor, window, session = build(vstate(), cards=["CARD"], dpi=144)
        driver.tick()
        self.assertEqual(window.dpi_set[-1], 144)  # pushed the target monitor DPI

    def test_falls_back_to_window_dpi_when_target_dpi_unknown(self):
        driver, monitor, window, session = build(vstate(), cards=["CARD"], dpi=0)
        driver.tick()
        self.assertEqual(window.dpi_set[-1], 96)  # 0 -> fall back to last known

    def test_rect_failure_hides_instead_of_showing_at_stale_position(self):
        driver, monitor, window, session = build(vstate(), cards=["CARD"], rect=None)
        driver.tick()
        self.assertEqual(window.shows, 0)  # never shown at a stale/unknown position
        self.assertGreaterEqual(window.hides, 1)
        self.assertIn(10.0, session.hidden_at)  # billing window closed (fail closed)

    def test_attended_but_no_card_hides(self):
        driver, monitor, window, session = build(vstate(), cards=[])
        driver.tick()
        self.assertEqual(len(session.maybe_calls), 1)
        self.assertEqual(window.shows, 0)
        self.assertEqual(window.hides, 1)

    def test_dismiss_hides_and_stops_showing(self):
        driver, monitor, window, session = build(vstate(), cards=["CARD"])
        driver.tick()
        self.assertEqual(window.shows, 1)
        driver.dismiss()
        calls_before = len(session.maybe_calls)
        result = driver.tick()
        self.assertIsNone(result)  # short-circuits like the kill switch
        self.assertGreaterEqual(window.hides, 1)
        self.assertEqual(len(session.maybe_calls), calls_before)  # never fetches again

    def test_settle_marks_hidden_and_settles(self):
        driver, monitor, window, session = build(vstate(), settle_value=0.42)
        earned = driver.settle()
        self.assertEqual(earned, 0.42)
        self.assertEqual(session.settles, 1)
        self.assertIn(10.0, session.hidden_at)
        self.assertEqual(window.hides, 1)

    def test_run_loops_until_stop_then_settles(self):
        driver, monitor, window, session = build(vstate(), cards=["CARD"])
        ticks = {"n": 0}

        def stop():
            ticks["n"] += 1
            return ticks["n"] > 3  # allow 3 ticks

        earned = driver.run(stop=stop, sleep=lambda _s: None)
        self.assertEqual(window.pumps, 3)
        self.assertEqual(session.settles, 1)
        self.assertEqual(earned, 0.0)


class StatusSignalTests(unittest.TestCase):
    """The driver pushes a one-line status to the tray on each state change so a
    billable overlay showing nothing reads as alive, not broken."""

    def test_showing_a_card_reports_showing(self):
        seen = []
        driver, *_ = build(vstate(), cards=["CARD"], on_status=seen.append)
        driver.tick()
        self.assertEqual(seen, [_STATUS_SHOWING])

    def test_attended_but_no_card_reports_no_sponsor(self):
        seen = []
        driver, *_ = build(vstate(), cards=[], on_status=seen.append)
        driver.tick()
        self.assertEqual(seen, [_STATUS_BY_REASON["no_card"]])

    def test_target_not_foreground_reports_waiting(self):
        seen = []
        driver, *_ = build(vstate(fg=False), on_status=seen.append)
        driver.tick()
        self.assertEqual(seen, [_STATUS_BY_REASON["target_not_foreground"]])

    def test_unchanged_state_is_not_re_emitted_every_tick(self):
        seen = []
        driver, monitor, *_ = build(vstate(fg=False), on_status=seen.append)
        driver.tick()
        driver.tick()
        self.assertEqual(seen, [_STATUS_BY_REASON["target_not_foreground"]])

    def test_status_sink_exception_never_breaks_the_tick(self):
        def boom(_text):
            raise RuntimeError("sink down")

        driver, _monitor, window, _session = build(vstate(fg=False), on_status=boom)
        driver.tick()  # must not raise
        self.assertEqual(window.hides, 1)


class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "overlay.lock"

    def test_acquire_blocks_second_holder(self):
        a = InstanceLock(self.path)
        b = InstanceLock(self.path)
        self.assertTrue(a.acquire())
        self.assertTrue(self.path.exists())
        self.assertFalse(b.acquire())
        a.release()
        self.assertFalse(self.path.exists())
        self.assertTrue(b.acquire())
        b.release()

    def test_context_manager_releases(self):
        with InstanceLock(self.path) as lock:
            self.assertTrue(lock.held)
            self.assertTrue(self.path.exists())
        self.assertFalse(self.path.exists())

    def test_acquire_is_best_effort_on_unexpected_oserror(self):
        # A filesystem failure other than "already exists" must not raise out of
        # acquire() (which would abort the caller's run); it returns False.
        lock = InstanceLock(self.path)
        with mock.patch("sai.overlay.lock.os.open", side_effect=PermissionError("denied")):
            self.assertFalse(lock.acquire())
        self.assertFalse(lock.held)

    def test_stale_lock_with_dead_owner_is_reclaimed(self):
        # A leftover lock owned by a PID that is not alive must be stealable.
        self.path.write_text("999999999 0", encoding="utf-8")
        old = 1.0  # epoch -> very old mtime
        os.utime(self.path, (old, old))
        lock = InstanceLock(self.path)
        self.assertTrue(lock.acquire())
        self.assertEqual(lock._owner_pid(), os.getpid())
        lock.release()

    def test_windows_process_alive_requires_still_active_exit_code(self):
        class FakeKernel32:
            def OpenProcess(self, *_args):
                return 123

            def GetExitCodeProcess(self, _handle, out):
                out._obj.value = 0
                return 1

            def CloseHandle(self, _handle):
                return 1

        with mock.patch("sai.overlay.lock.os.name", "nt"), \
                mock.patch("ctypes.windll", create=True) as windll:
            windll.kernel32 = FakeKernel32()
            self.assertFalse(_process_alive(12345))

    def test_windows_process_alive_accepts_still_active_exit_code(self):
        class FakeKernel32:
            def OpenProcess(self, *_args):
                return 123

            def GetExitCodeProcess(self, _handle, out):
                out._obj.value = 259
                return 1

            def CloseHandle(self, _handle):
                return 1

        with mock.patch("sai.overlay.lock.os.name", "nt"), \
                mock.patch("ctypes.windll", create=True) as windll:
            windll.kernel32 = FakeKernel32()
            self.assertTrue(_process_alive(12345))


class ProbeTickHookTests(unittest.TestCase):
    """The driver brackets each tick with the probe's optional begin_tick/end_tick
    so a probe can snapshot expensive per-tick system queries once."""

    def _hook_probe(self, events):
        class HookProbe(FakeProbe):
            def begin_tick(self_inner):
                events.append("begin")

            def end_tick(self_inner):
                events.append("end")

        return HookProbe()

    def test_begin_and_end_bracket_an_early_return_tick(self):
        events = []
        driver = SessionDriver(
            monitor=FakeMonitor(vstate()),
            window=FakeWindow(),
            session=FakeSession(cards=["CARD"]),
            probe=self._hook_probe(events),
            surface=FakeSurface(),
            config={},
            enabled=lambda: False,  # short-circuits before sampling
            clock=lambda: 10.0,
        )
        driver.tick()
        self.assertEqual(events, ["begin", "end"])

    def test_end_runs_even_if_the_tick_body_raises(self):
        events = []

        class BoomMonitor:
            def sample(self):
                raise RuntimeError("boom")

        driver = SessionDriver(
            monitor=BoomMonitor(),
            window=FakeWindow(),
            session=FakeSession(),
            probe=self._hook_probe(events),
            surface=FakeSurface(),
            config={},
            enabled=lambda: True,
            clock=lambda: 10.0,
        )
        with self.assertRaises(RuntimeError):
            driver.tick()
        self.assertEqual(events, ["begin", "end"])

    def test_probe_without_hooks_still_ticks(self):
        # FakeProbe defines neither hook; the driver must tolerate that.
        driver, _monitor, window, _session = build(vstate(fg=False))
        driver.tick()
        self.assertEqual(window.hides, 1)


class RunIterationContextTests(unittest.TestCase):
    def test_run_wraps_each_pass_in_the_iteration_context(self):
        events = []

        class Ctx:
            def __enter__(self_inner):
                events.append("enter")
                return self_inner

            def __exit__(self_inner, *_a):
                events.append("exit")
                return False

        driver, _monitor, window, _session = build(vstate(fg=False))
        passes = {"n": 0}

        def stop():
            done = passes["n"] >= 2
            passes["n"] += 1
            return done

        driver.run(stop=stop, sleep=lambda _s: None, iteration=lambda: Ctx())
        self.assertEqual(events, ["enter", "exit", "enter", "exit"])
        self.assertEqual(window.pumps, 2)  # one pump per wrapped pass

    def test_run_without_iteration_context_is_unaffected(self):
        driver, _monitor, window, _session = build(vstate(fg=False))
        passes = {"n": 0}

        def stop():
            done = passes["n"] >= 1
            passes["n"] += 1
            return done

        driver.run(stop=stop, sleep=lambda _s: None)
        self.assertEqual(window.pumps, 1)


if __name__ == "__main__":
    unittest.main()
