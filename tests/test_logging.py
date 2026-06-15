import logging
import os
import tempfile
import unittest
from pathlib import Path

from sai.app_logging import configure_logging, current_log_path, reset_logging_for_tests, tail_log_lines


class LoggingTests(unittest.TestCase):
    ENV_KEYS = ("SAI_HOME", "SAI_LOG_FILE", "SAI_LOG_LEVEL", "SAI_LOG_MAX_BYTES", "SAI_LOG_BACKUPS")

    def setUp(self):
        self._previous_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        reset_logging_for_tests()

    def tearDown(self):
        reset_logging_for_tests()
        for key, value in self._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_log_path_uses_sai_home_without_creating_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SAI_HOME"] = tmp
            path = current_log_path()

            configured = configure_logging(service="test")

            self.assertEqual(path, Path(tmp) / "logs" / "sai.log")
            self.assertEqual(configured, path)
            self.assertFalse(path.exists())

    def test_exception_is_written_to_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SAI_HOME"] = tmp
            path = configure_logging(service="test")

            try:
                raise RuntimeError("test failure")
            except RuntimeError:
                logging.getLogger("sai.test").exception("Captured test exception")

            for handler in logging.getLogger("sai").handlers:
                handler.flush()

            text = path.read_text(encoding="utf-8")
            self.assertIn("Captured test exception", text)
            self.assertIn("RuntimeError: test failure", text)
            self.assertEqual(tail_log_lines(1), text.rstrip("\n").splitlines()[-1:])
            reset_logging_for_tests()


if __name__ == "__main__":
    unittest.main()
