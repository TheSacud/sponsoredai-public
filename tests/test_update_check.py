import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from sai import update_check
from sai.update_check import (
    CACHE_TTL_SECONDS,
    UpdateInfo,
    _is_newer,
    _parse_version,
    check_for_update,
    notify_terminal_update,
    update_notice,
)


class _CountingFetcher:
    """A registry fetcher stub that records how often it was called."""

    def __init__(self, version):
        self.version = version
        self.count = 0

    def __call__(self):
        self.count += 1
        return json.dumps({"version": self.version})


class _TtyStream(io.StringIO):
    def isatty(self):
        return True


class VersionParsingTests(unittest.TestCase):
    def test_parses_plain_releases(self):
        self.assertEqual(_parse_version("0.2.3"), (0, 2, 3))
        self.assertEqual(_parse_version("v1.4"), (1, 4))

    def test_drops_prerelease_and_build_suffix(self):
        self.assertEqual(_parse_version("1.2.3-rc1"), (1, 2, 3))
        self.assertEqual(_parse_version("1.2.3+build9"), (1, 2, 3))

    def test_rejects_non_numeric(self):
        self.assertIsNone(_parse_version("1.2.x"))
        self.assertIsNone(_parse_version("garbage"))
        self.assertIsNone(_parse_version(""))

    def test_is_newer(self):
        self.assertTrue(_is_newer("0.2.4", "0.2.3"))
        self.assertTrue(_is_newer("0.3.0", "0.2.9"))
        self.assertFalse(_is_newer("0.2.3", "0.2.3"))
        self.assertFalse(_is_newer("0.2.2", "0.2.3"))
        # A shorter tuple is not a real bump (0.2 == 0.2.0 here, not greater).
        self.assertFalse(_is_newer("0.2", "0.2.0"))
        self.assertFalse(_is_newer("bad", "0.2.3"))


class CheckForUpdateTests(unittest.TestCase):
    def _home(self):
        return mock.patch.dict(os.environ, {"SAI_HOME": self._tmp}, clear=False)

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = self._tmpdir.name
        self.addCleanup(self._tmpdir.cleanup)
        # Pin the installed version so the comparison is deterministic regardless
        # of the real __version__.
        patcher = mock.patch.object(update_check, "__version__", "0.2.3")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_newer_version_is_reported(self):
        fetch = _CountingFetcher("0.2.4")
        with self._home():
            info = check_for_update(fetcher=fetch, now=1000.0, env={})
        self.assertIsNotNone(info)
        self.assertEqual(info.current, "0.2.3")
        self.assertEqual(info.latest, "0.2.4")
        self.assertEqual(fetch.count, 1)

    def test_same_version_is_no_nudge(self):
        fetch = _CountingFetcher("0.2.3")
        with self._home():
            info = check_for_update(fetcher=fetch, now=1000.0, env={})
        self.assertIsNone(info)

    def test_older_published_version_is_no_nudge(self):
        # The source/backend can run ahead of npm; never nag to "update" downward.
        fetch = _CountingFetcher("0.2.1")
        with self._home():
            info = check_for_update(fetcher=fetch, now=1000.0, env={})
        self.assertIsNone(info)

    def test_fresh_cache_skips_the_second_fetch(self):
        fetch = _CountingFetcher("0.2.4")
        with self._home():
            first = check_for_update(fetcher=fetch, now=1000.0, env={})
            second = check_for_update(fetcher=fetch, now=1000.0 + 60, env={})
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(fetch.count, 1)

    def test_stale_cache_refetches(self):
        fetch = _CountingFetcher("0.2.4")
        with self._home():
            check_for_update(fetcher=fetch, now=1000.0, env={})
            check_for_update(fetcher=fetch, now=1000.0 + CACHE_TTL_SECONDS + 1, env={})
        self.assertEqual(fetch.count, 2)

    def test_opt_out_skips_the_check_entirely(self):
        fetch = _CountingFetcher("0.2.4")
        with self._home():
            info = check_for_update(fetcher=fetch, env={"SAI_NO_UPDATE_CHECK": "1"})
        self.assertIsNone(info)
        self.assertEqual(fetch.count, 0)

    def test_ci_environment_skips_the_check(self):
        fetch = _CountingFetcher("0.2.4")
        with self._home():
            info = check_for_update(fetcher=fetch, env={"GITHUB_ACTIONS": "true"})
        self.assertIsNone(info)
        self.assertEqual(fetch.count, 0)

    def test_fetch_error_is_silent(self):
        def boom():
            raise OSError("offline")

        with self._home():
            self.assertIsNone(check_for_update(fetcher=boom, now=1000.0, env={}))

    def test_malformed_payload_is_silent(self):
        with self._home():
            self.assertIsNone(check_for_update(fetcher=lambda: "{not json", now=1000.0, env={}))
            self.assertIsNone(
                check_for_update(fetcher=lambda: json.dumps({"no": "version"}), now=1000.0, env={})
            )
            self.assertIsNone(
                check_for_update(fetcher=lambda: json.dumps({"version": "not-a-version"}), now=1000.0, env={})
            )


class NoticeTests(unittest.TestCase):
    def test_update_notice_mentions_versions_and_command(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            text = update_notice(UpdateInfo(current="0.2.3", latest="0.2.4"))
        self.assertIn("0.2.3", text)
        self.assertIn("0.2.4", text)
        self.assertIn("npm install -g @sponsoredai/cli", text)


class NotifyTerminalTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        env = mock.patch.dict(
            os.environ, {"SAI_HOME": self._tmpdir.name, "NO_COLOR": "1"}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        ver = mock.patch.object(update_check, "__version__", "0.2.3")
        ver.start()
        self.addCleanup(ver.stop)

    def test_non_tty_prints_nothing_and_skips_fetch(self):
        fetch = _CountingFetcher("0.2.4")
        stream = io.StringIO()  # isatty() is False
        info = notify_terminal_update(stream, fetcher=fetch, now=1000.0, env={})
        self.assertIsNone(info)
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(fetch.count, 0)

    def test_tty_prints_the_notice(self):
        fetch = _CountingFetcher("0.2.4")
        stream = _TtyStream()
        info = notify_terminal_update(stream, fetcher=fetch, now=1000.0, env={})
        self.assertIsNotNone(info)
        out = stream.getvalue()
        self.assertIn("0.2.4", out)
        self.assertIn("npm install -g @sponsoredai/cli", out)


class WalletPayloadTests(unittest.TestCase):
    def test_wallet_json_carries_an_update_block(self):
        from sai.cli import main

        env = {"SAI_HOME": tempfile.mkdtemp(), "SAI_NO_UPDATE_CHECK": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = main(["wallet", "--json", "--no-sync"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertIn("update", payload)
        # Opt-out is set, so the check is skipped and nothing is "available".
        self.assertEqual(payload["update"]["available"], False)
        self.assertIsNone(payload["update"]["latest"])
        self.assertIn("current", payload["update"])


if __name__ == "__main__":
    unittest.main()
