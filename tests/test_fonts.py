import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sai import fonts


class NerdFontPatternTests(unittest.TestCase):
    def test_matches_nerd_font_names(self):
        for name in (
            "CaskaydiaMono Nerd Font Mono",
            "CaskaydiaMono NFM",
            "JetBrainsMono NF",
            "JetBrainsMono NFP",
            "MesloLGS NF",
            "Symbols Nerd Font Mono (TrueType)",
            "FiraCode Nerd Font",
        ):
            self.assertTrue(fonts.NERD_FONT_PATTERN.search(name), name)

    def test_does_not_match_regular_fonts(self):
        for name in ("Cascadia Mono", "Consolas", "Fira Code", "Info Display", "NFS Mono", "Info"):
            self.assertFalse(fonts.NERD_FONT_PATTERN.search(name), name)


class FontFamilyTests(unittest.TestCase):
    def test_configured_family_is_the_win32_name(self):
        # Windows Terminal matches on the Win32 family name, not the long
        # typographic one; configuring the latter breaks font resolution.
        self.assertEqual(fonts.FONT_FAMILY, "CaskaydiaMono NFM")
        self.assertNotIn("Nerd Font", fonts.FONT_FAMILY)
        # The name SAI configures must itself read as a Nerd Font for detection.
        self.assertTrue(fonts.NERD_FONT_PATTERN.search(fonts.FONT_FAMILY))


class FaceExtractionTests(unittest.TestCase):
    def test_extracts_string_array_and_legacy_faces(self):
        text = (
            '{"profiles": {"defaults": {"font": {"face": "CaskaydiaMono Nerd Font Mono"}}},'
            ' "list": [{"fontFace": "Consolas"}, {"font": {"face": ["Cascadia Mono", "Symbols Nerd Font"]}}]}'
        )
        faces = fonts._faces_in_jsonc(text)
        self.assertIn("CaskaydiaMono Nerd Font Mono", faces)
        self.assertIn("Consolas", faces)
        self.assertIn("Symbols Nerd Font", faces)


class DetectionTests(unittest.TestCase):
    def setUp(self):
        fonts.clear_caches()
        self.addCleanup(fonts.clear_caches)

    def test_windows_terminal_trusts_configured_face_only(self):
        env = {"WT_SESSION": "x", "TERM_PROGRAM": ""}
        with patch.dict(os.environ, env):
            with patch.object(fonts, "windows_terminal_faces", return_value=["Cascadia Mono"]):
                self.assertFalse(fonts.nerd_font_available())
            fonts.clear_caches()
            with patch.object(fonts, "windows_terminal_faces", return_value=["CaskaydiaMono Nerd Font Mono"]):
                self.assertTrue(fonts.nerd_font_available())
            fonts.clear_caches()
            # Default font (no face configured) ships without Nerd Font
            # glyphs, even when a Nerd Font is installed system-wide.
            with patch.object(fonts, "windows_terminal_faces", return_value=[]):
                self.assertFalse(fonts.nerd_font_available())

    def test_vscode_uses_its_terminal_font_family(self):
        env = {"WT_SESSION": "", "TERM_PROGRAM": "vscode"}
        with patch.dict(os.environ, env):
            with patch.object(fonts, "vscode_terminal_faces", return_value=["'MesloLGS NF', monospace"]):
                self.assertTrue(fonts.nerd_font_available())

    def test_vscode_on_posix_falls_back_when_no_font_family_configured(self):
        env = {"WT_SESSION": "", "TERM_PROGRAM": "vscode"}
        with patch.dict(os.environ, env):
            with patch.object(fonts.os, "name", "posix"):
                with patch.object(fonts, "vscode_terminal_faces", return_value=[]):
                    with patch.object(fonts, "posix_font_names", return_value=["CaskaydiaMonoNerdFontMono-Regular"]):
                        self.assertTrue(fonts.nerd_font_available())

    def test_vscode_explicit_regular_font_does_not_use_posix_fallback(self):
        env = {"WT_SESSION": "", "TERM_PROGRAM": "vscode"}
        with patch.dict(os.environ, env):
            with patch.object(fonts.os, "name", "posix"):
                with patch.object(fonts, "vscode_terminal_faces", return_value=["Cascadia Mono"]):
                    with patch.object(fonts, "posix_font_names", return_value=["CaskaydiaMonoNerdFontMono-Regular"]):
                        self.assertFalse(fonts.nerd_font_available())

    def test_icons_env_override(self):
        with patch.dict(os.environ, {"SAI_ICONS": "off"}):
            self.assertFalse(fonts.icons_enabled())
        fonts.clear_caches()
        with patch.dict(os.environ, {"SAI_ICONS": "on"}):
            self.assertTrue(fonts.icons_enabled())
        fonts.clear_caches()
        with patch.dict(os.environ, {"SAI_ICONS": "auto"}):
            with patch.object(fonts, "nerd_font_available", return_value=False):
                self.assertFalse(fonts.icons_enabled())


class WindowsTerminalConfigTests(unittest.TestCase):
    def test_updates_default_font_with_backup(self):
        raw = (
            "{\n"
            '    // user comment\n'
            '    "profiles": {\n'
            '        "defaults": {},\n'
            '        "list": [],\n'
            "    },\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(raw, encoding="utf-8")
            with patch.object(fonts, "windows_terminal_settings_paths", return_value=[settings]):
                updated = fonts.configure_windows_terminal()
            self.assertEqual(updated, settings)
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(data["profiles"]["defaults"]["font"]["face"], fonts.FONT_FAMILY)
            self.assertTrue(Path(str(settings) + ".sai-backup").exists())

    def test_updates_settings_with_inline_jsonc_comments(self):
        raw = (
            "{\n"
            '    "copyFormatting": "none", // valid Windows Terminal JSONC\n'
            '    "profiles": {\n'
            '        "defaults": {}\n'
            "    }\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(raw, encoding="utf-8")
            with patch.object(fonts, "windows_terminal_settings_paths", return_value=[settings]):
                updated = fonts.configure_windows_terminal()
            self.assertEqual(updated, settings)
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(data["copyFormatting"], "none")
            self.assertEqual(data["profiles"]["defaults"]["font"]["face"], fonts.FONT_FAMILY)

    def test_refuses_legacy_profiles_list_schema(self):
        raw = '{"profiles": [{"fontFace": "Consolas"}]}'
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(raw, encoding="utf-8")
            with patch.object(fonts, "windows_terminal_settings_paths", return_value=[settings]):
                self.assertIsNone(fonts.configure_windows_terminal())
            self.assertEqual(settings.read_text(encoding="utf-8"), raw)


if __name__ == "__main__":
    unittest.main()
