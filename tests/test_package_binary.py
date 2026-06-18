import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "package_binary.py"


class PackageBinaryTests(unittest.TestCase):
    def run_script(self, *args: str) -> None:
        subprocess.run([sys.executable, str(SCRIPT), *args], check=True, cwd=ROOT)

    def test_packages_single_file_binary_with_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sai"
            source.write_text("#!/bin/sh\n", encoding="utf-8")
            out = root / "release"

            self.run_script("--source", str(source), "--output-dir", str(out), "--platform", "linux", "--arch", "x64")

            target = out / "sai-linux-x64"
            checksum = out / "sai-linux-x64.sha256"
            self.assertTrue(target.is_file())
            self.assertIn("sai-linux-x64", checksum.read_text(encoding="utf-8"))

    def test_packages_onedir_binary_with_release_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sai-onedir"
            internal = source / "_internal"
            internal.mkdir(parents=True)
            executable = source / "sai"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            (internal / "base_library.zip").write_bytes(b"zip")
            out = root / "release"

            self.run_script("--source", str(source), "--output-dir", str(out), "--platform", "darwin", "--arch", "arm64")

            target = out / "sai-darwin-arm64"
            archive = out / "sai-darwin-arm64.tar.gz"
            checksum = out / "sai-darwin-arm64.tar.gz.sha256"
            self.assertTrue((target / "sai").is_file())
            self.assertTrue((target / "_internal" / "base_library.zip").is_file())
            self.assertTrue(archive.is_file())
            self.assertIn("sai-darwin-arm64.tar.gz", checksum.read_text(encoding="utf-8"))
            with tarfile.open(archive, "r:gz") as tar:
                self.assertIn("sai-darwin-arm64/sai", tar.getnames())

    def test_packages_win32_onedir_directory_has_no_exe_suffix(self):
        # The onedir directory must be sai-win32-x64/ (no .exe); the executable
        # inside is sai.exe. Downstream CI smoke + npm staging expect this exact
        # directory name, so guard against re-suffixing the directory itself.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sai-onedir"
            internal = source / "_internal"
            internal.mkdir(parents=True)
            (source / "sai.exe").write_bytes(b"MZ")
            (internal / "base_library.zip").write_bytes(b"zip")
            out = root / "release"

            self.run_script("--source", str(source), "--output-dir", str(out), "--platform", "win32", "--arch", "x64")

            target = out / "sai-win32-x64"
            archive = out / "sai-win32-x64.tar.gz"
            checksum = out / "sai-win32-x64.tar.gz.sha256"
            self.assertTrue(target.is_dir())
            self.assertFalse((out / "sai-win32-x64.exe").exists())
            self.assertTrue((target / "sai.exe").is_file())
            self.assertTrue((target / "_internal" / "base_library.zip").is_file())
            self.assertTrue(archive.is_file())
            self.assertIn("sai-win32-x64.tar.gz", checksum.read_text(encoding="utf-8"))
            with tarfile.open(archive, "r:gz") as tar:
                self.assertIn("sai-win32-x64/sai.exe", tar.getnames())


if __name__ == "__main__":
    unittest.main()
