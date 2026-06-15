"""macOS overlay backend tests — performance-focused.

These exercise the real PyObjC-backed probe/surface, so they only run on macOS.
They verify the per-tick caching/snapshotting that keeps the 5Hz overlay loop off
the expensive Quartz/CoreText paths (one CGWindowList + NSScreen enumeration per
tick, memoized fonts/text metrics, pid->path cache).
"""

import unittest

from sai.overlay.macos import is_macos


@unittest.skipUnless(is_macos(), "macOS overlay backend requires macOS")
class MacProbeTickSnapshotTests(unittest.TestCase):
    def _probe(self):
        from sai.overlay.macos import MacOSProbe

        return MacOSProbe()

    def _row(self, pid, *, x=10, y=20, w=300, h=100, layer=0, extra=None):
        from Quartz import (  # type: ignore
            kCGWindowBounds,
            kCGWindowLayer,
            kCGWindowOwnerPID,
        )

        row = {
            kCGWindowOwnerPID: pid,
            kCGWindowLayer: layer,
            kCGWindowBounds: {"X": x, "Y": y, "Width": w, "Height": h},
        }
        if extra:
            row.update(extra)
        return row

    def test_begin_tick_snapshots_window_list_once_for_all_lookups(self):
        probe = self._probe()
        pid = 4242
        calls = {"n": 0}

        def fake_list():
            calls["n"] += 1
            return [self._row(pid)]

        probe._list_windows = fake_list

        probe.begin_tick()
        try:
            rect = probe.window_rect(pid)
            probe.monitor_of(pid)        # would re-enumerate without the snapshot
            probe.monitor_work_area(pid)  # ditto
        finally:
            probe.end_tick()

        self.assertEqual(calls["n"], 1)  # only begin_tick enumerated
        self.assertEqual((rect.left, rect.top, rect.width, rect.height), (10, 20, 300, 100))

    def test_end_tick_falls_back_to_live_enumeration(self):
        probe = self._probe()
        pid = 4243
        calls = {"n": 0}

        def fake_list():
            calls["n"] += 1
            return [self._row(pid)]

        probe._list_windows = fake_list

        probe.begin_tick()
        probe.window_rect(pid)
        probe.end_tick()
        probe.window_rect(pid)  # outside a tick -> live query again

        self.assertEqual(calls["n"], 2)

    def test_bounds_for_pid_bridges_only_the_bounds_dict(self):
        probe = self._probe()
        pid = 4244
        probe._tick_windows = [self._row(pid, extra={"kCGWindowOwnerName": "Other", "junk": object()})]
        bounds = probe._bounds_for_pid(pid)
        self.assertEqual(set(bounds), {"X", "Y", "Width", "Height"})

    def test_zero_area_and_non_layer0_windows_are_skipped(self):
        probe = self._probe()
        pid = 4245
        probe._tick_windows = [
            self._row(pid, layer=3),          # not the app layer
            self._row(pid, w=0, h=0),         # zero area
            self._row(pid, x=5, y=6, w=200, h=80),  # the real one
        ]
        rect = probe.window_rect(pid)
        self.assertEqual((rect.left, rect.top, rect.width, rect.height), (5, 6, 200, 80))

    def test_process_image_path_memoizes_per_pid(self):
        probe = self._probe()
        pid = 5151
        calls = {"n": 0}

        class FakeURL:
            def path(self):
                return "/Applications/Claude.app/Contents/MacOS/Claude"

        class FakeApp:
            def executableURL(self):
                return FakeURL()

            def bundleURL(self):
                return None

            def isTerminated(self):
                return False  # alive -> the cache hit must not re-resolve

        class FakeNSRunningApplication:
            @staticmethod
            def runningApplicationWithProcessIdentifier_(p):
                calls["n"] += 1
                return FakeApp()

        class FakeAppKit:
            NSRunningApplication = FakeNSRunningApplication

        probe._AppKit = FakeAppKit

        p1 = probe.process_image_path(pid)
        p2 = probe.process_image_path(pid)
        self.assertEqual(p1, "/Applications/Claude.app/Contents/MacOS/Claude")
        self.assertEqual(p2, p1)
        self.assertEqual(calls["n"], 1)  # resolved once, then cached (app still alive)

    def test_process_image_path_reresolves_after_pid_reuse(self):
        # macOS recycles PIDs: when the monitored app quits and its pid is handed
        # to an unrelated process, the cached path must NOT stay stale, or the
        # overlay would render/bill over the wrong window.
        probe = self._probe()
        pid = 5151

        class FakeURL:
            def __init__(self, path):
                self._path = path

            def path(self):
                return self._path

        class FakeApp:
            def __init__(self, path, terminated=False):
                self._path = path
                self.terminated = terminated

            def executableURL(self):
                return FakeURL(self._path)

            def bundleURL(self):
                return None

            def isTerminated(self):
                return self.terminated

        claude = FakeApp("/Applications/Claude.app/Contents/MacOS/Claude")
        state = {"current": claude}

        class FakeNSRunningApplication:
            @staticmethod
            def runningApplicationWithProcessIdentifier_(p):
                return state["current"]

        probe._AppKit = type("FakeAppKit", (), {"NSRunningApplication": FakeNSRunningApplication})

        self.assertEqual(
            probe.process_image_path(pid),
            "/Applications/Claude.app/Contents/MacOS/Claude",
        )

        # Claude quits (cached object now reports terminated); the OS recycles the
        # pid onto an unrelated app. The path must re-resolve, not stay stale.
        claude.terminated = True
        state["current"] = FakeApp("/Applications/Mail.app/Contents/MacOS/Mail")
        self.assertEqual(
            probe.process_image_path(pid),
            "/Applications/Mail.app/Contents/MacOS/Mail",
        )

    def test_unresolvable_path_is_not_cached(self):
        probe = self._probe()
        pid = 5152
        calls = {"n": 0}

        class FakeNSRunningApplication:
            @staticmethod
            def runningApplicationWithProcessIdentifier_(p):
                calls["n"] += 1
                return None  # not resolvable this time

        class FakeAppKit:
            NSRunningApplication = FakeNSRunningApplication

        probe._AppKit = FakeAppKit

        self.assertIsNone(probe.process_image_path(pid))
        self.assertIsNone(probe.process_image_path(pid))
        self.assertEqual(calls["n"], 2)  # retried — fail-closed, never cached None

    def test_live_probe_smoke_does_not_crash(self):
        # Exercise the real Quartz/AppKit paths end-to-end for one tick.
        probe = self._probe()
        probe.begin_tick()
        try:
            fg = probe.foreground_window()
            self.assertIsInstance(fg, int)
            self.assertIsInstance(probe.idle_seconds(), float)
            if fg:
                probe.process_image_path(fg)
                probe.window_rect(fg)
                self.assertIsInstance(probe.monitor_of(fg), int)
        finally:
            probe.end_tick()


@unittest.skipUnless(is_macos(), "macOS overlay backend requires macOS")
class MacTextSurfaceCacheTests(unittest.TestCase):
    def _card(self):
        from sai.sponsors import LOCAL_SPONSORS

        return LOCAL_SPONSORS[0]

    def test_measure_is_stable_and_caches_fonts_and_text_metrics(self):
        from sai.overlay.macos import MacTextSurface

        surface = MacTextSurface()
        card = self._card()

        first = surface.measure(card, 96)
        self.assertIn(96, surface._font_cache)
        font = surface._font_cache[96]
        size_entries = len(surface._text_size_cache)
        self.assertGreater(size_entries, 0)

        second = surface.measure(card, 96)
        self.assertEqual(first, second)
        self.assertIs(surface._font_cache[96], font)  # font reused, not rebuilt
        self.assertEqual(len(surface._text_size_cache), size_entries)  # no re-measure

    def test_attrs_cached_per_color_and_dpi(self):
        from sai.overlay.macos import MacTextSurface

        surface = MacTextSurface()
        a1 = surface._attrs(surface._fg, 96)
        a2 = surface._attrs(surface._fg, 96)
        self.assertIs(a1, a2)
        a3 = surface._attrs(surface._accent, 96)
        self.assertIsNot(a1, a3)

    def test_dispose_clears_caches(self):
        from sai.overlay.macos import MacTextSurface

        surface = MacTextSurface()
        surface.measure(self._card(), 96)
        surface.dispose()
        self.assertEqual(surface._font_cache, {})
        self.assertEqual(surface._attrs_cache, {})
        self.assertEqual(surface._text_size_cache, {})


@unittest.skipUnless(is_macos(), "macOS overlay backend requires macOS")
class MacBackendWiringTests(unittest.TestCase):
    def test_autorelease_pool_is_a_context_manager(self):
        from sai.overlay.macos import autorelease_pool

        with autorelease_pool():
            pass  # must not raise

    def test_backend_wires_iteration_context(self):
        from sai.overlay.app import _backend

        backend = _backend()
        self.assertIn("iteration_context", backend)
        with backend["iteration_context"]():
            pass


if __name__ == "__main__":
    unittest.main()
