#!/usr/bin/env python3
"""Smoke a packaged SAI client binary for release-only invariants.

The public client binary must not expose source-only backend or mock-lab
commands. This is a behavior guard for the PyInstaller excludes in the release
workflow: if those modules are accidentally bundled, argparse help will expose
the commands even when no service is started.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 10.0

SERVER_ONLY_CHECKS = (
    (
        ("backend", "--help"),
        (
            "Run or inspect the sponsor backend",
            "Start the local sponsor backend",
            "SAI backend listening",
        ),
    ),
    (
        ("dev", "mock", "--help"),
        (
            "Development-only mock surfaces",
            "Run a local full-product mock lab",
            "SAI mock lab",
        ),
    ),
)


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    output: str


def _run(binary: Path, args: tuple[str, ...], timeout: float) -> CommandResult:
    try:
        completed = subprocess.run(
            [str(binary), *args],
            cwd=binary.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{_format_args(args)} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise RuntimeError(f"{_format_args(args)} could not start: {type(exc).__name__}") from exc
    return CommandResult(args=args, returncode=completed.returncode, output=completed.stdout or "")


def _format_args(args: tuple[str, ...]) -> str:
    return "sai " + " ".join(args)


def smoke_client_binary(binary: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[str]:
    errors: list[str] = []
    binary = binary.expanduser()
    if not binary.exists():
        return [f"binary does not exist: {binary}"]
    if not binary.is_file():
        return [f"binary path is not a file: {binary}"]
    binary = binary.resolve()

    try:
        version = _run(binary, ("--version",), timeout)
    except RuntimeError as exc:
        return [str(exc)]
    if version.returncode != 0:
        errors.append(f"{_format_args(version.args)} exited {version.returncode}, expected 0")

    for args, forbidden_markers in SERVER_ONLY_CHECKS:
        try:
            result = _run(binary, args, timeout)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        if result.returncode == 0:
            errors.append(f"{_format_args(args)} succeeded; server-only command is exposed")
        for marker in forbidden_markers:
            if marker in result.output:
                errors.append(f"{_format_args(args)} exposed server-only marker: {marker!r}")
                break

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke a packaged SAI client binary")
    parser.add_argument("--binary", required=True, type=Path, help="Path to the packaged sai executable")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    errors = smoke_client_binary(args.binary, timeout=args.timeout)
    if errors:
        print("client binary smoke FAILED", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("client binary smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
