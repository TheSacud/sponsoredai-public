import os
import tempfile
import unittest
from unittest.mock import patch

from sai.config import DEFAULT_BACKEND_URL, load_config


class ConfigTests(unittest.TestCase):
    def test_new_config_defaults_to_public_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"SAI_HOME": tmp}, clear=False):
                config = load_config()

        self.assertEqual(config["backend_url"], DEFAULT_BACKEND_URL)
        self.assertEqual(config["frequency"], "normal")

    def test_default_backend_can_be_overridden_for_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"SAI_HOME": tmp, "SAI_DEFAULT_BACKEND_URL": "https://example.test"}
            with patch.dict(os.environ, env, clear=False):
                config = load_config()

        self.assertEqual(config["backend_url"], "https://example.test")


if __name__ == "__main__":
    unittest.main()
