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


def replace_file(source: Path, target: Path) -> None:
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    shutil.copy2(source, target)


def replace_tree(source: Path, target: Path) -> None:
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    shutil.copytree(source, target)


def write_checksum(path: Path) -> None:
    checksum = sha256(path)
    checksum_path = path.with_name(f"{path.name}.sha256")
    checksum_path.write_text(f"{checksum}  {path.name}\n", encoding="utf-8")
    print(f"Checksum {checksum_path}")


def remove_if_exists(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Package a SAI release binary with a checksum.")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=["darwin", "linux", "win32"])
    parser.add_argument("--arch", required=True, choices=["x64", "arm64"])
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.source.is_dir():
        # onedir build: package the whole tree as a directory. The directory
        # itself must NOT carry an executable extension -- binary_name() suffixes
        # win32 with ".exe", which would name the directory "sai-win32-x64.exe/"
        # while every downstream step (CI smoke, npm staging, the release asset)
        # expects "sai-win32-x64/". The executable *inside* keeps its name.
        dir_name = f"sai-{args.platform}-{args.arch}"
        target = args.output_dir / dir_name
        # Clean stale artifacts for this target: the dir's own archive/checksum,
        # plus a single-file binary a previous onefile build would have left.
        remove_if_exists(target.with_name(f"{dir_name}.tar.gz"))
        remove_if_exists(target.with_name(f"{dir_name}.tar.gz.sha256"))
        remove_if_exists(args.output_dir / binary_name(args.platform, args.arch))
        remove_if_exists(args.output_dir / f"{binary_name(args.platform, args.arch)}.sha256")
        replace_tree(args.source, target)
        executable = target / ("sai.exe" if args.platform == "win32" else "sai")
        if not executable.is_file():
            raise SystemExit(f"Expected executable missing from packaged directory: {executable}")
        if args.platform != "win32":
            executable.chmod(executable.stat().st_mode | 0o755)
        archive_path = Path(
            shutil.make_archive(str(target), "gztar", root_dir=args.output_dir, base_dir=dir_name)
        )
        print(f"Packaged {target}")
        print(f"Archive {archive_path}")
        write_checksum(archive_path)
        return 0

    target = args.output_dir / binary_name(args.platform, args.arch)
    remove_if_exists(target.with_name(f"{target.name}.tar.gz"))
    remove_if_exists(target.with_name(f"{target.name}.tar.gz.sha256"))
    replace_file(args.source, target)
    if args.platform != "win32":
        target.chmod(target.stat().st_mode | 0o755)

    print(f"Packaged {target}")
    write_checksum(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
