"""Fresh defaults and compatibility with themes saved by earlier previews."""

import copy
import hashlib
import json
import runpy
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from dance_ui import theme as theme_module
from dance_ui.theme import BUILTIN_THEMES, DEFAULT_PREFERENCES, ThemeError, ThemeStore


def small_png():
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 3, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x28\x5a\xb4" * 2) * 3))
            + chunk(b"IEND", b""))


class ThemeUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        # The preview persisted full snapshots, including the packaged image's
        # relative reference. Reproduce that document without the old media.
        self.previous = copy.deepcopy(BUILTIN_THEMES["light"])
        self.previous.update(
            id="theme_de512bd38e8f4604", name="小余12主题", builtin=True,
            background_image="backgrounds/fb33dc6ea90ed2124b8ec8af2a55d17a13c714e03d6460ffd3a37b4bfa088786.png",
            background_opacity=0.0, image_opacity=0.63, accent="#B5A5E0",
            background_color="#FFFFFF", nav_opacity=0.3,
            nav_active_opacity=0.16, text_primary="#39346D", card_opacity=0.66,
        )

    def legacy_document(self, *, include_preview=False):
        themes = copy.deepcopy(list(BUILTIN_THEMES.values()))
        themes[0].update(accent="#123456", radius=8)
        custom = copy.deepcopy(BUILTIN_THEMES["dark"])
        custom.update(id="legacy_custom", name="已有自定义", builtin=False, accent="#654321")
        themes.append(custom)
        if include_preview:
            themes.append(copy.deepcopy(self.previous))
        return {
            "version": 1, "active_theme": custom["id"], "themes": themes,
            "preferences": {"scale": 125, "density": "compact", "logs_expanded": True, "dismissed_update_version": ""},
        }

    def write_document(self, root, document):
        root.mkdir(parents=True, exist_ok=True)
        path = root / "appearance.json"
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def test_fresh_profile_has_only_light_dark_and_no_background_after_reopen(self):
        store = ThemeStore(self.root / "fresh")
        self.assertEqual(store.active, BUILTIN_THEMES["light"])
        self.assertEqual(store.preferences, DEFAULT_PREFERENCES)
        self.assertEqual(store.themes, list(BUILTIN_THEMES.values()))
        self.assertIsNone(store.background_path())
        self.assertEqual(list(store.background_dir.iterdir()), [])
        reopened = ThemeStore(store.root_dir)
        self.assertEqual(reopened.active, store.active)
        self.assertEqual(reopened.preferences, store.preferences)
        self.assertEqual(reopened.themes, store.themes)
        self.assertFalse(reopened.can_undo)
        self.assertEqual(reopened.warnings, [])

    def test_existing_theme_values_selection_preferences_and_file_are_preserved(self):
        raw = self.legacy_document()
        path = self.write_document(self.root / "existing", raw)
        original_bytes = path.read_bytes()
        store = ThemeStore(path.parent)
        self.assertEqual(store.active_id, raw["active_theme"])
        self.assertEqual(store.themes, raw["themes"])
        self.assertEqual(store.preferences, raw["preferences"])
        self.assertEqual(path.read_bytes(), original_bytes)
        store.save()
        reopened = ThemeStore(path.parent)
        self.assertEqual(reopened.active, store.active)
        self.assertEqual(reopened.themes, store.themes)
        self.assertEqual(reopened.preferences, raw["preferences"])

    def test_sparse_documents_restore_builtin_defaults_without_inserting_preview(self):
        cases = (
            {"version": 1, "themes": []},
            {"themes": [], "active_theme": "missing", "preferences": {}},
            {"version": 1, "themes": [], "active_theme": {}, "preferences": []},
        )
        for index, raw in enumerate(cases):
            with self.subTest(case=index):
                path = self.write_document(self.root / str(index), raw)
                original = path.read_bytes()
                store = ThemeStore(path.parent)
                self.assertEqual(store.active, BUILTIN_THEMES["light"])
                self.assertEqual(store.themes, list(BUILTIN_THEMES.values()))
                self.assertEqual(store.preferences, DEFAULT_PREFERENCES)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(store.warnings, [])

    def test_previous_snapshot_keeps_colors_selection_preferences_without_packaged_image(self):
        raw = self.legacy_document(include_preview=True)
        raw["active_theme"] = self.previous["id"]
        path = self.write_document(self.root / "previous", raw)
        original = path.read_bytes()
        store = ThemeStore(path.parent)
        self.assertEqual(store.active, dict(self.previous, builtin=False))
        self.assertEqual(store.preferences, raw["preferences"])
        self.assertIsNone(store.background_path())
        self.assertEqual(store.warnings, [])
        self.assertEqual(path.read_bytes(), original)
        # Repeated loads must not warn, reset the selection or discard the
        # missing reference, so a later import can recover the user's wallpaper.
        for _ in range(3):
            reopened = ThemeStore(path.parent)
            self.assertEqual(reopened.active, store.active)
            self.assertEqual(reopened.warnings, [])
            self.assertIsNone(reopened.background_path())
        store.change({"accent": "#102030"})
        reopened = ThemeStore(path.parent)
        self.assertEqual(reopened.active["accent"], "#102030")
        self.assertEqual(reopened.active["background_image"], self.previous["background_image"])
        self.assertEqual(reopened.active["card_opacity"], 0.66)

    def test_previous_snapshot_can_be_deleted_and_undone_as_a_personal_theme(self):
        raw = self.legacy_document(include_preview=True)
        raw["active_theme"] = self.previous["id"]
        path = self.write_document(self.root / "delete", raw)
        store = ThemeStore(path.parent)
        store.delete(self.previous["id"])
        self.assertEqual(store.active_id, "light")
        self.assertNotIn(self.previous["id"], {theme["id"] for theme in store.themes})
        self.assertTrue(store.undo())
        self.assertEqual(store.active, dict(self.previous, builtin=False))
        self.assertEqual(ThemeStore(path.parent).active, store.active)
        with self.assertRaises(ThemeError):
            store.delete("light")

    def test_reset_of_previous_snapshot_uses_base_but_undo_restores_its_colors(self):
        raw = self.legacy_document(include_preview=True)
        raw["active_theme"] = self.previous["id"]
        path = self.write_document(self.root / "reset", raw)
        store = ThemeStore(path.parent)
        previous = store.active
        store.reset_current()
        self.assertEqual(store.active, dict(BUILTIN_THEMES["light"],
                                          id=previous["id"], name=previous["name"], builtin=False))
        self.assertEqual(store.preferences, raw["preferences"])
        self.assertTrue(store.undo())
        self.assertEqual(store.active, previous)

    def test_previously_imported_background_stays_available_after_upgrade(self):
        data = small_png()
        reference = "backgrounds/" + hashlib.sha256(data).hexdigest() + ".png"
        raw = self.legacy_document(include_preview=True)
        raw["themes"][-1]["background_image"] = reference
        raw["active_theme"] = self.previous["id"]
        path = self.write_document(self.root / "imported", raw)
        image = path.parent / reference
        image.parent.mkdir()
        image.write_bytes(data)
        store = ThemeStore(path.parent)
        self.assertEqual(store.background_path(), image)
        self.assertEqual(store.active["accent"], self.previous["accent"])
        self.assertEqual(image.read_bytes(), data)
        image.unlink()
        self.assertIsNone(store.background_path())
        self.assertEqual(ThemeStore(path.parent).warnings, [])

    def test_personal_background_import_remains_managed_and_survives_reopen(self):
        source = self.root / "my-background.png"
        source.write_bytes(small_png())
        store = ThemeStore(self.root / "user-image")
        theme_id = store.create("我的主题")
        reference = store.import_background(source)
        image = store.root_dir / reference
        self.assertEqual(store.background_path(), image)
        self.assertEqual(image.read_bytes(), source.read_bytes())
        source.unlink()
        reopened = ThemeStore(store.root_dir)
        self.assertEqual(reopened.active_id, theme_id)
        self.assertEqual(reopened.background_path(), image)

    def test_failed_user_save_keeps_previous_snapshot_and_document_intact(self):
        raw = self.legacy_document(include_preview=True)
        raw["active_theme"] = self.previous["id"]
        path = self.write_document(self.root / "failed-save", raw)
        store = ThemeStore(path.parent)
        original = store.path.read_bytes()
        with patch("dance_ui.theme.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.change({"accent": "#102030"})
        self.assertEqual(store.active, dict(self.previous, builtin=False))
        self.assertEqual(store.path.read_bytes(), original)
        self.assertFalse(store.can_undo)
        self.assertEqual(list(store.root_dir.glob(".appearance-*.tmp")), [])

    def test_frozen_style_module_needs_no_packaged_media_and_writes_only_user_data(self):
        internal = self.root / "VRChat跳舞房助手" / "_internal"
        package = internal / "dance_ui"
        package.mkdir(parents=True)
        module_path = package / "theme.py"
        shutil.copyfile(Path(theme_module.__file__), module_path)
        before = module_path.read_bytes()
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(internal), create=True):
            namespace = runpy.run_path(str(module_path))
            store = namespace["ThemeStore"](self.root / "windows-user-data")
            self.assertEqual(store.active, BUILTIN_THEMES["light"])
            store.change({"accent": "#ABCDEF"})
            reopened = namespace["ThemeStore"](store.root_dir)
            self.assertEqual(reopened.active["accent"], "#ABCDEF")
            self.assertIsNone(reopened.background_path())
            self.assertEqual(reopened.warnings, [])
        self.assertEqual(list(package.iterdir()), [module_path])
        self.assertEqual(module_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
