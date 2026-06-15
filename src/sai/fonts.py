"""Nerd Font detection and installation for sponsor-card icons.

Nerd Font codepoints live in the Unicode private use area: on a terminal
without a patched font they render as empty boxes, which looks broken on a
paid placement. Icons are therefore enabled only with positive evidence that
the user's terminal will render them, with SAI_ICONS as an explicit override.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .ansi import UNICODE_OK

SPONSOR_ICON = "\uf0a1"  # nf-fa-bullhorn

# Nerd Fonts abbreviate the GDI/Win32 family name (the long "... Nerd Font Mono"
# overflows the classic name-table limit), so a patched face shows up as e.g.
# "CaskaydiaMono NFM" / "JetBrainsMono NF" / "... NFP" - match those too.
NERD_FONT_PATTERN = re.compile(r"nerd ?fonts?|(?:^|[\s\"'-])nf[mp]?(?:$|[\s\"'-])", re.IGNORECASE)

FONT_ZIP_URL = "https://github.com/ryanoasis/nerd-fonts/releases/latest/download/CascadiaMono.zip"
# Use the font's actual Win32 family name. The long typographic name
# ("CaskaydiaMono Nerd Font Mono") is NOT what GDI/Windows Terminal match on, so
# configuring it leaves the terminal unable to find the font ("cannot locate the
# following fonts"). The Mono variant registers under the abbreviated "NFM" name.
FONT_FAMILY = "CaskaydiaMono NFM"
FONT_MEMBER = re.compile(r"CaskaydiaMonoNerdFontMono-Regular\.ttf$", re.IGNORECASE)


@functools.lru_cache(maxsize=1)
def icons_enabled() -> bool:
    mode = os.environ.get("SAI_ICONS", "auto").strip().lower()
    if mode in {"0", "off", "false", "no"}:
        return False
    if mode in {"1", "on", "true", "yes"}:
        return True
    return UNICODE_OK and nerd_font_available()


@functools.lru_cache(maxsize=1)
def nerd_font_available() -> bool:
    if os.environ.get("WT_SESSION"):
        # The terminal is known: trust its configured font, not what is
        # merely installed. No face configured means the bundled Cascadia,
        # which ships without Nerd Font glyphs.
        return _any_nerd(windows_terminal_faces())
    if os.environ.get("TERM_PROGRAM", "").strip().lower() == "vscode":
        faces = vscode_terminal_faces()
        if faces:
            return _any_nerd(faces)
        if os.name != "nt":
            return _any_nerd(posix_font_names())
        return False
    if os.name == "nt":
        return _any_nerd(windows_terminal_faces() + windows_font_names())
    return _any_nerd(posix_font_names())


def _any_nerd(names: list[str]) -> bool:
    return any(NERD_FONT_PATTERN.search(name) for name in names)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def _faces_in_jsonc(text: str) -> list[str]:
    faces = re.findall(r'"(?:face|fontFace)"\s*:\s*"([^"]+)"', text)
    for group in re.findall(r'"(?:face|fontFace)"\s*:\s*\[([^\]]*)\]', text):
        faces.extend(re.findall(r'"([^"]+)"', group))
    return faces


def windows_terminal_settings_paths() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    base = Path(local)
    return [
        base / "Packages" / "Microsoft.WindowsTerminal_8wekyb3d8bbwe" / "LocalState" / "settings.json",
        base / "Packages" / "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe" / "LocalState" / "settings.json",
        base / "Microsoft" / "Windows Terminal" / "settings.json",
    ]


def windows_terminal_faces() -> list[str]:
    faces: list[str] = []
    for path in windows_terminal_settings_paths():
        faces.extend(_faces_in_jsonc(_read_text(path)))
    return faces


def vscode_settings_paths() -> list[Path]:
    products = ("Code", "Code - Insiders", "Cursor")
    paths: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.extend(Path(appdata) / product / "User" / "settings.json" for product in products)
    home = Path.home()
    paths.extend(home / ".config" / product / "User" / "settings.json" for product in products)
    paths.extend(
        home / "Library" / "Application Support" / product / "User" / "settings.json" for product in products
    )
    return paths


def vscode_terminal_faces() -> list[str]:
    faces: list[str] = []
    for path in vscode_settings_paths():
        match = re.search(r'"terminal\.integrated\.fontFamily"\s*:\s*"([^"]*)"', _read_text(path))
        if match:
            faces.append(match.group(1))
    return faces


def windows_font_names() -> list[str]:
    try:
        import winreg
    except ImportError:
        return []
    names: list[str] = []
    for hive, path in (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    ):
        try:
            with winreg.OpenKey(hive, path) as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    try:
                        names.append(winreg.EnumValue(key, index)[0])
                    except OSError:
                        break
        except OSError:
            continue
    return names


def posix_font_names() -> list[str]:
    home = Path.home()
    roots = [
        home / ".local" / "share" / "fonts",
        home / ".fonts",
        home / "Library" / "Fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path("/Library/Fonts"),
    ]
    names: list[str] = []
    for root in roots:
        try:
            names.extend(path.stem for path in root.rglob("*.ttf"))
            names.extend(path.stem for path in root.rglob("*.otf"))
        except OSError:
            continue
    return names


def install_font() -> Path:
    """Download the patched Cascadia and install it for the current user.

    No admin rights needed: per-user fonts live under the user profile on
    Windows and under ~/.local/share/fonts (or ~/Library/Fonts) elsewhere.
    """
    filename, ttf_bytes = _download_font()
    if os.name == "nt":
        return _install_windows(filename, ttf_bytes)
    return _install_posix(filename, ttf_bytes)


def _download_font() -> tuple[str, bytes]:
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "font.zip"
        with urllib.request.urlopen(FONT_ZIP_URL, timeout=120) as response, open(archive, "wb") as out:
            shutil.copyfileobj(response, out)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                if FONT_MEMBER.search(member):
                    return Path(member).name, bundle.read(member)
    raise RuntimeError(f"No font file matching {FONT_MEMBER.pattern} in {FONT_ZIP_URL}")


def _install_windows(filename: str, ttf_bytes: bytes) -> Path:
    import ctypes
    import winreg

    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is not set")
    fonts_dir = Path(local) / "Microsoft" / "Windows" / "Fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    target = fonts_dir / filename
    target.write_bytes(ttf_bytes)
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows NT\CurrentVersion\Fonts",
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, f"{FONT_FAMILY} (TrueType)", 0, winreg.REG_SZ, str(target))
    try:
        ctypes.windll.gdi32.AddFontResourceW(str(target))
        HWND_BROADCAST, WM_FONTCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001D, 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_FONTCHANGE, 0, 0, SMTO_ABORTIFHUNG, 1000, None
        )
    except OSError:
        pass  # running apps will pick the font up after restart
    return target


def _install_posix(filename: str, ttf_bytes: bytes) -> Path:
    import sys

    fonts_dir = Path.home() / ("Library/Fonts" if sys.platform == "darwin" else ".local/share/fonts")
    fonts_dir.mkdir(parents=True, exist_ok=True)
    target = fonts_dir / filename
    target.write_bytes(ttf_bytes)
    fc_cache = shutil.which("fc-cache")
    if fc_cache:
        subprocess.run([fc_cache, "-f", str(fonts_dir)], check=False, capture_output=True)
    return target


def configure_windows_terminal(face: str = FONT_FAMILY) -> Path | None:
    """Point Windows Terminal's default profile font at `face`.

    Returns the settings path that was updated, or None when no settings
    file could be edited safely (caller should print manual instructions).
    A backup is written next to the settings file before any change.
    """
    for path in windows_terminal_settings_paths():
        raw = _read_text(path)
        if not raw:
            continue
        data = _parse_jsonc(raw)
        if not isinstance(data, dict):
            return None
        profiles = data.get("profiles")
        if profiles is None:
            profiles = {}
            data["profiles"] = profiles
        if not isinstance(profiles, dict):
            return None  # legacy list schema: do not guess
        defaults = profiles.setdefault("defaults", {})
        if not isinstance(defaults, dict):
            return None
        font = defaults.setdefault("font", {})
        if not isinstance(font, dict):
            return None
        font["face"] = face
        backup = Path(str(path) + ".sai-backup")
        shutil.copy2(path, backup)
        path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
        return path
    return None


def _parse_jsonc(raw: str) -> object | None:
    for candidate in (raw, _strip_jsonc(raw)):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def _strip_jsonc(raw: str) -> str:
    out: list[str] = []
    i = 0
    in_string = False
    escape = False
    while i < len(raw):
        char = raw[i]
        nxt = raw[i + 1] if i + 1 < len(raw) else ""
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and nxt == "/":
            i += 2
            while i < len(raw) and raw[i] not in "\r\n":
                i += 1
            continue
        if char == "/" and nxt == "*":
            i += 2
            while i + 1 < len(raw) and raw[i : i + 2] != "*/":
                i += 1
            i = min(i + 2, len(raw))
            continue
        out.append(char)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def clear_caches() -> None:
    icons_enabled.cache_clear()
    nerd_font_available.cache_clear()
