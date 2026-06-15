from __future__ import annotations

import argparse
import hashlib
import os
import shutil
from pathlib import Path


def binary_name(platform: str, arch: str) -> str:
    ext = ".exe" if platform == "win32" else ""
    return f"sai-{platform}-{arch}{ext}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Package a SAI release binary with a checksum.")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=["darwin", "linux", "win32"])
    parser.add_argument("--arch", required=True, choices=["x64", "arm64"])
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    target_name = binary_name(args.platform, args.arch)
    target = args.output_dir / target_name
    shutil.copy2(args.source, target)

    if args.platform != "win32":
        target.chmod(target.stat().st_mode | 0o755)

    checksum = sha256(target)
    checksum_path = target.with_name(f"{target.name}.sha256")
    checksum_path.write_text(f"{checksum}  {target.name}\n", encoding="utf-8")
    print(f"Packaged {target}")
    print(f"Checksum {checksum_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
