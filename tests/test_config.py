import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sai.config import DEFAULT_BACKEND_URL, load_config, runtime_paths, save_config, write_json_atomic


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

    @unittest.skipUnless(os.name == "posix", "POSIX file-permission semantics")
    def test_save_config_is_private_and_home_is_locked_down(self):
        # N-2: the config file (api key + install secret) must be 0600, the SAI
        # home 0700, and no world-readable temp file may linger.
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"SAI_HOME": tmp}, clear=False):
                save_config({"api_key": "sai_secret", "install_secret": "s3cr3t"})
                cfg = runtime_paths().config_file
                self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(cfg.parent.stat().st_mode), 0o700)
                leftovers = [p.name for p in cfg.parent.iterdir() if p.name.endswith(".tmp")]
                self.assertEqual(leftovers, [])

    def test_concurrent_writes_never_corrupt_config(self):
        # N-3: many threads writing at once (as the threaded gateway can) must not
        # collide on a shared temp path; the file must always parse afterward and
        # no temp file may be left behind.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            errors = []

            def writer(n):
                try:
                    for _ in range(15):
                        write_json_atomic(path, {"writer": n, "k": "v" * 50}, private=True)
                except Exception as exc:  # noqa: BLE001 - surface any race failure
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(n,)) for n in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(errors, [])
            with path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)  # must be valid JSON (no torn/interleaved write)
            self.assertIn("writer", loaded)
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
