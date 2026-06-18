import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(name):
    """Load a scripts/*.py helper as a module (the scripts dir is not a package)."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepoConsistencyTests(unittest.TestCase):
    def test_committed_tree_is_version_consistent(self):
        # A permanent invariant: __version__, pyproject (dynamic), and every npm
        # package.json must agree in the committed tree. Catches a hand-edit drift
        # in plain `pytest`, not only in the release workflow.
        result = subprocess.run([sys.executable, str(SCRIPTS / "check_versions.py")])
        self.assertEqual(result.returncode, 0)


class SetVersionTests(unittest.TestCase):
    def test_stamps_init_and_every_npm_package(self):
        sv = _load("set_version")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init = root / "src" / "sai" / "__init__.py"
            init.parent.mkdir(parents=True)
            init.write_text('__all__ = ["__version__"]\n__version__ = "0.2.3"\n', encoding="utf-8")
            npm = root / "npm" / "package.json"
            npm.parent.mkdir(parents=True)
            npm.write_text(
                json.dumps(
                    {
                        "name": "@sponsoredai/cli",
                        "version": "0.2.3",
                        "optionalDependencies": {"@sponsoredai/cli-win32-x64": "0.2.3"},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            plat = root / "npm" / "platform" / "win32-x64" / "package.json"
            plat.parent.mkdir(parents=True)
            plat.write_text(
                json.dumps({"name": "@sponsoredai/cli-win32-x64", "version": "0.2.3"}, indent=2) + "\n",
                encoding="utf-8",
            )

            with mock.patch.multiple(sv, INIT=init, NPM_PKG=npm, NPM_PLATFORM=root / "npm" / "platform"):
                sv._set_init("0.3.0")
                sv._set_npm("0.3.0")

            self.assertIn('__version__ = "0.3.0"', init.read_text(encoding="utf-8"))
            npm_data = json.loads(npm.read_text(encoding="utf-8"))
            self.assertEqual(npm_data["version"], "0.3.0")
            self.assertEqual(npm_data["optionalDependencies"]["@sponsoredai/cli-win32-x64"], "0.3.0")
            self.assertEqual(json.loads(plat.read_text(encoding="utf-8"))["version"], "0.3.0")

    def test_rejects_non_plain_versions(self):
        sv = _load("set_version")
        for bad in ["0.2", "1.2.3-rc1", "latest", "v"]:
            with self.assertRaises(SystemExit):
                with mock.patch.object(sys, "argv", ["set_version.py", bad]):
                    sv.main()


class CheckVersionsTests(unittest.TestCase):
    def _tree(self, root, *, init="0.3.0", npm="0.3.0", plat="0.3.0", dynamic=True):
        (root / "src" / "sai").mkdir(parents=True)
        (root / "src" / "sai" / "__init__.py").write_text(f'__version__ = "{init}"\n', encoding="utf-8")
        pyproject = '[project]\ndynamic = ["version"]\n' if dynamic else '[project]\nversion = "0.0.0"\n'
        (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
        npm_dir = root / "npm"
        npm_dir.mkdir()
        (npm_dir / "package.json").write_text(
            json.dumps({"version": npm, "optionalDependencies": {"@sponsoredai/cli-win32-x64": npm}}),
            encoding="utf-8",
        )
        platdir = npm_dir / "platform" / "win32-x64"
        platdir.mkdir(parents=True)
        (platdir / "package.json").write_text(json.dumps({"version": plat}), encoding="utf-8")

    def test_clean_tree_has_no_problems(self):
        cv = _load("check_versions")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._tree(root)
            with mock.patch.object(cv, "REPO", root):
                self.assertEqual(cv.find_problems(), [])

    def test_detects_npm_drift(self):
        cv = _load("check_versions")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._tree(root, npm="0.2.9")
            with mock.patch.object(cv, "REPO", root):
                problems = cv.find_problems()
        self.assertTrue(any("npm/package.json" in p for p in problems))

    def test_detects_static_pyproject_version(self):
        cv = _load("check_versions")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._tree(root, dynamic=False)
            with mock.patch.object(cv, "REPO", root):
                problems = cv.find_problems()
        self.assertTrue(any("pyproject" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
