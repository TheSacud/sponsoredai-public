import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke_client_binary.py"


class ClientBinarySmokeTests(unittest.TestCase):
    def run_smoke(self, binary: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--binary", str(binary), "--timeout", "2"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def fake_binary(self, root: Path, mode: str) -> Path:
        app = root / "fake_sai.py"
        app.write_text(
            textwrap.dedent(
                f"""
                import sys

                mode = {mode!r}
                args = sys.argv[1:]

                if args == ["--version"]:
                    print("sai 9.9.9")
                    raise SystemExit(0)

                if args == ["backend", "--help"]:
                    if mode == "bad_backend_success":
                        raise SystemExit(0)
                    if mode == "bad_backend_marker":
                        print("Run or inspect the sponsor backend")
                        raise SystemExit(2)
                    print("sai: error: argument command_name: invalid choice: 'backend'")
                    raise SystemExit(2)

                if args == ["dev", "mock", "--help"]:
                    if mode == "bad_dev_mock_success":
                        raise SystemExit(0)
                    if mode == "bad_dev_mock_marker":
                        print("Run a local full-product mock lab")
                        raise SystemExit(2)
                    print("sai: error: argument command_name: invalid choice: 'dev'")
                    raise SystemExit(2)

                raise SystemExit(99)
                """
            ).lstrip(),
            encoding="utf-8",
        )

        if os.name == "nt":
            wrapper = root / "sai.cmd"
            wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{app}" %*\r\n', encoding="utf-8")
        else:
            wrapper = root / "sai"
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{app}" "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
        return wrapper

    def test_good_client_rejects_server_only_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_smoke(self.fake_binary(Path(tmp), "good"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("client binary smoke OK", result.stdout)

    def test_relative_binary_path_is_resolved_before_execution(self):
        # Keep the temp dir on ROOT's drive: os.path.relpath can't express a
        # path across drives on Windows (CI checks out on D: but %TEMP% is C:).
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            binary = self.fake_binary(Path(tmp), "good")
            relative = Path(os.path.relpath(binary, ROOT))
            result = self.run_smoke(relative)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("client binary smoke OK", result.stdout)

    def test_backend_help_success_fails_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_smoke(self.fake_binary(Path(tmp), "bad_backend_success"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backend --help succeeded", result.stderr)

    def test_backend_help_marker_fails_smoke_even_when_command_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_smoke(self.fake_binary(Path(tmp), "bad_backend_marker"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("server-only marker", result.stderr)

    def test_dev_mock_help_success_fails_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_smoke(self.fake_binary(Path(tmp), "bad_dev_mock_success"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dev mock --help succeeded", result.stderr)

    def test_dev_mock_help_marker_fails_smoke_even_when_command_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_smoke(self.fake_binary(Path(tmp), "bad_dev_mock_marker"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("server-only marker", result.stderr)


if __name__ == "__main__":
    unittest.main()
