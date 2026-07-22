import importlib.util
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "preview_site.py"


def _load_preview():
    spec = importlib.util.spec_from_file_location("preview_site", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SitePreviewTests(unittest.TestCase):
    def setUp(self):
        self.preview = _load_preview()

    def write_site(self, root: Path) -> None:
        (root / "_internal").mkdir()
        (root / "index.html").write_text("<h1>Home</h1>", encoding="utf-8")
        (root / "market.html").write_text("<h1>Market</h1>", encoding="utf-8")
        (root / "trust.html").write_text("<h1>Trust</h1>", encoding="utf-8")
        (root / "_internal" / "internal-note.html").write_text("<h1>Internal</h1>", encoding="utf-8")
        (root / "sai.css").write_text("body { color: black; }", encoding="utf-8")

    def fetch(self, base_url: str, route: str) -> str:
        url = base_url.rstrip("/") + route
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.read().decode("utf-8")

    def test_serves_nginx_style_clean_routes_and_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            self.write_site(site)
            server = self.preview.make_server(site, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = self.preview.server_url(server)
                self.assertIn("Home", self.fetch(base_url, "/"))
                self.assertIn("Market", self.fetch(base_url, "/market"))
                self.assertIn("Trust", self.fetch(base_url, "/trust"))
                self.assertIn("color: black", self.fetch(base_url, "/sai.css"))
                self.assertIn("Home", self.fetch(base_url, "/unknown-route"))
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_homepage_command_chips_are_atomic_and_use_fresh_styles(self):
        homepage = (ROOT / "site-v3" / "index.html").read_text(encoding="utf-8")
        stylesheet = (ROOT / "site-v3" / "sai-core-v4.css").read_text(encoding="utf-8")

        self.assertIn('href="sai-core-v4.css?v=20260718-1"', homepage)
        self.assertIn(".step code{display:inline-block", stylesheet)
        self.assertIn(
            ".step code,.kill code,.doc-prose code,.cl-bubble code{white-space:nowrap}",
            stylesheet,
        )

    def test_path_traversal_does_not_escape_site_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            site = root / "site"
            site.mkdir()
            self.write_site(site)
            (root / "secret.txt").write_text("do not serve", encoding="utf-8")

            server = self.preview.make_server(site, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = self.fetch(self.preview.server_url(server), "/../secret.txt")
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

            self.assertNotIn("do not serve", body)
            self.assertIn("Home", body)

    def test_check_mode_fetches_requested_static_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            self.write_site(site)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--site-dir",
                    str(site),
                    "--check",
                    "--check-path",
                    "/",
                    "--check-path",
                    "/market",
                    "--check-path",
                    "/trust",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("site preview check OK (3 routes)", result.stdout)

    def test_check_mode_rejects_routes_without_static_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            self.write_site(site)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--site-dir",
                    str(site),
                    "--check",
                    "--check-path",
                    "/missing",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/missing does not map to a static file", result.stderr)


if __name__ == "__main__":
    unittest.main()
