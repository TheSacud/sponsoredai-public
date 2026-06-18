#!/usr/bin/env python3
"""Set the CLI version everywhere it is hardcoded, from one command.

``python scripts/set_version.py 0.2.4`` rewrites:
  - src/sai/__init__.py          __version__   (the single source of truth)
  - npm/package.json             version + the optionalDependencies pins
  - npm/platform/*/package.json  version

pyproject.toml reads ``sai.__version__`` dynamically, so it needs no edit. The VS
Code extension is on its own version line and is intentionally left untouched.

Verify the result with ``python scripts/check_versions.py``.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INIT = REPO / "src" / "sai" / "__init__.py"
NPM_PKG = REPO / "npm" / "package.json"
NPM_PLATFORM = REPO / "npm" / "platform"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_INIT_RE = re.compile(r'^(__version__\s*=\s*)"[^"]*"', re.MULTILINE)


def _set_init(version: str) -> None:
    text = INIT.read_text(encoding="utf-8")
    new, count = _INIT_RE.subn(rf'\g<1>"{version}"', text)
    if count != 1:
        raise SystemExit(f"error: expected exactly one __version__ in {INIT}, found {count}")
    INIT.write_text(new, encoding="utf-8")


def _write_json(path: Path, data: dict) -> None:
    # Match the 2-space + trailing-newline format that stage-platform-package.js
    # already writes these files with, so set_version and the publish step agree.
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _set_npm(version: str) -> None:
    pkg = json.loads(NPM_PKG.read_text(encoding="utf-8"))
    pkg["version"] = version
    for name in list(pkg.get("optionalDependencies", {})):
        pkg["optionalDependencies"][name] = version
    _write_json(NPM_PKG, pkg)
    for plat in sorted(NPM_PLATFORM.glob("*/package.json")):
        data = json.loads(plat.read_text(encoding="utf-8"))
        data["version"] = version
        _write_json(plat, data)


def main() -> int:
    ap = argparse.ArgumentParser(description="Set the CLI version everywhere it is hardcoded.")
    ap.add_argument("version", help="New version, e.g. 0.2.4 (plain X.Y.Z)")
    args = ap.parse_args()
    version = args.version.lstrip("v").strip()
    if not VERSION_RE.match(version):
        raise SystemExit(f"error: '{args.version}' is not a plain X.Y.Z version")
    _set_init(version)
    _set_npm(version)
    print(
        f"CLI version set to {version} (src/sai/__init__.py + npm/). "
        "pyproject reads it dynamically; the VS Code extension is unchanged."
    )
    print("Verify with: python scripts/check_versions.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
