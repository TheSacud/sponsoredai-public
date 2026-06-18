#!/usr/bin/env python3
"""Fail if the CLI version is inconsistent across the repo.

The single source of truth is ``src/sai/__init__.py`` ``__version__``. This asserts
that ``npm/package.json`` (its ``version`` and every ``optionalDependencies`` pin)
and every ``npm/platform/*/package.json`` match it, and that ``pyproject.toml`` takes
its version dynamically (so it cannot hold a stale literal).

The VS Code extension is on its own version line and is intentionally NOT checked
here. Used by ``tests.yml`` (PR-time drift guard), by the release workflow's
version guard, and by ``sync_public.py`` before an export.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def cli_version() -> str:
    text = (REPO / "src" / "sai" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("error: could not read __version__ from src/sai/__init__.py")
    return match.group(1)


def find_problems() -> list[str]:
    expected = cli_version()
    problems: list[str] = []

    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    if not re.search(r'^\s*dynamic\s*=\s*\[[^\]]*"version"', pyproject, re.MULTILINE):
        problems.append(
            "pyproject.toml does not declare version as dynamic "
            "(it must read sai.__version__, not hold a literal)"
        )
    if re.search(r'^\s*version\s*=\s*"', pyproject, re.MULTILINE):
        problems.append(
            "pyproject.toml still has a literal [project] version; "
            "remove it so the version cannot drift"
        )

    npm = json.loads((REPO / "npm" / "package.json").read_text(encoding="utf-8"))
    if npm.get("version") != expected:
        problems.append(f"npm/package.json version {npm.get('version')!r} != {expected!r}")
    for name, pin in (npm.get("optionalDependencies") or {}).items():
        if pin != expected:
            problems.append(f"npm/package.json optionalDependencies[{name}] {pin!r} != {expected!r}")

    for plat in sorted((REPO / "npm" / "platform").glob("*/package.json")):
        data = json.loads(plat.read_text(encoding="utf-8"))
        if data.get("version") != expected:
            rel = plat.relative_to(REPO).as_posix()
            problems.append(f"{rel} version {data.get('version')!r} != {expected!r}")

    return problems


def main() -> int:
    expected = cli_version()
    problems = find_problems()
    if problems:
        print("version check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  [x] {problem}", file=sys.stderr)
        return 1
    print(
        f"version check OK: CLI {expected} consistent across pyproject (dynamic), "
        "the npm launcher, and the platform packages."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
