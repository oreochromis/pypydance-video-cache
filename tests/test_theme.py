"""Appearance persistence tests: no Qt import and no system/cache-service writes."""

import base64
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from dance_ui.theme import (
    ThemeStore, ThemeError, BUILTIN_THEMES, DEFAULT_PREFERENCES, MAX_IMAGE_BYTES,
)


def png(width=2, height=3):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    # Deliberately cap the payload so oversized-header tests stay lightweight.
    raw = (b"\x00" + b"\x28\x5a\xb4" * min(width, 100)) * min(height, 100)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# Valid 2x3 image files generated once with Pillow; tests have no Pillow dependency.
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAADAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDlKKKK+vPDP//Z"
)
WEBP = base64.b64decode("UklGRjwAAABXRUJQVlA4IDAAAADwAQCdASoCAAMAAUAmJaACdLoB+AAETAAA/vDXA/9Js8TZ4mz5JH/xZyMR2XgAAAA=")


class ThemeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Exercise loading an existing profile as well as fresh defaults.
        legacy = self.root / "appearance"
        legacy.mkdir()
        (legacy / "appearance.json").write_text(json.dumps({
            "version": 1, "active_theme": "light",
            "themes": list(BUILTIN_THEMES.values()),
            "preferences": DEFAULT_PREFERENCES,
        }), encoding="utf-8")
        self.store = ThemeStore(self.root / "appearance")

    def source(self, name="wallpaper.png", data=None):
        source = self.root / name
        source.write_bytes(png() if data is None else data)
        return source

    def test_builtin_edits_persist_independently(self):
        self.store.change({"accent": "abc", "radius": 7})
        self.store.select("dark")
        self.assertEqual(self.store.active["accent"], BUILTIN_THEMES["dark"]["accent"])
        self.store.change({"accent": "#123456"})
        reopened = ThemeStore(self.store.root_dir)
        self.assertEqual(reopened.active_id, "dark")
        self.assertEqual(reopened.active["accent"], "#123456")
        reopened.select("light")
        self.assertEqual(reopened.active["accent"], "#AABBCC")
        self.assertEqual(reopened.active["radius"], 7)

    def test_preferences_survive_theme_changes_and_resets(self):
        preferences = {"scale": 125, "density": "compact", "logs_expanded": True, "dismissed_update_version": ""}
        self.store.change_preferences(preferences)
        self.store.select("dark")
        self.store.change({"radius": 18})
        self.store.reset_current()
        self.assertEqual(self.store.preferences, preferences)
        self.assertEqual(ThemeStore(self.store.root_dir).preferences, preferences)
        self.store.reset_preferences()
        self.assertEqual(self.store.preferences, DEFAULT_PREFERENCES)

    def test_copy_rename_delete_and_undo_keep_original(self):
        self.store.change({"accent": "#123456"})
        original = self.store.active
        copied = self.store.create(copy_current=True)
        self.store.rename(copied, "我的主题")
        self.store.change({"accent": "#654321"})
        self.store.delete(copied)
        self.assertEqual(self.store.active_id, "light")
        self.assertEqual(self.store.active, original)
        self.assertTrue(self.store.undo())
        self.assertEqual(self.store.active_id, copied)
        self.assertEqual(self.store.active["name"], "我的主题")
        self.assertEqual(self.store.active["accent"], "#654321")
        self.assertEqual(ThemeStore(self.store.root_dir).active_id, copied)

    def test_builtin_delete_and_invalid_rename_do_not_write(self):
        before = self.store.path.read_bytes()
        for operation in (
            lambda: self.store.delete("light"),
            lambda: self.store.rename("light", ""),
            lambda: self.store.rename("light", "深色"),
            lambda: self.store.select("not-a-theme"),
        ):
            with self.assertRaises(ThemeError):
                operation()
            self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(self.store.can_undo)

    def test_normalises_ranges_and_rejects_invalid_transaction(self):
        self.store.change({"radius": 999, "image_blur": -1, "card_opacity": 5, "background_opacity": -3, "border_width": 12})
        self.assertEqual(self.store.active["radius"], 24)
        self.assertEqual(self.store.active["image_blur"], 0)
        self.assertEqual(self.store.active["card_opacity"], 1)
        self.assertEqual(self.store.active["background_opacity"], 0)
        self.assertEqual(self.store.active["border_width"], 6)
        before = self.store.active
        for values in (
            {"accent": "#123456", "radius": float("nan")},
            {"accent": "not-a-color"}, {"image_blur": float("inf")},
            {"unknown_field": 5}, {"image_fit": "invalid"},
        ):
            with self.assertRaises(ThemeError):
                self.store.change(values)
            self.assertEqual(self.store.active, before)
        for values in ({"scale": 120}, {"scale": True}, {"scale": 125.9}, {"density": "tiny"}, {"logs_expanded": "false"}):
            with self.assertRaises(ThemeError):
                self.store.change_preferences(values)

    def test_atomic_write_failure_keeps_memory_disk_and_undo_history(self):
        before = self.store.path.read_bytes()
        with patch("dance_ui.theme.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.change({"accent": "#123456"})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.active["accent"], BUILTIN_THEMES["light"]["accent"])
        self.assertFalse(self.store.can_undo)
        self.assertEqual(list(self.store.root_dir.glob(".appearance-*.tmp")), [])

    def test_slider_group_is_one_undo_step(self):
        self.store.change({"accent": "#123456"})
        before_drag = self.store.active
        self.store.begin_edit()
        for value in (0.2, 0.4, 0.8):
            self.store.change({"card_opacity": value})
        self.store.end_edit()
        self.assertEqual(ThemeStore(self.store.root_dir).active["card_opacity"], 0.8)
        self.store.undo()
        self.assertEqual(self.store.active, before_drag)
        self.store.undo()
        self.assertEqual(self.store.active["accent"], BUILTIN_THEMES["light"]["accent"])
        self.assertFalse(self.store.undo())

    def test_group_reset_does_not_reset_unrelated_fields(self):
        self.store.change({"accent": "#123456", "card_opacity": 0.3, "radius": 24})
        self.store.reset_group("cards")
        self.assertEqual(self.store.active["accent"], "#123456")
        self.assertEqual(self.store.active["card_opacity"], BUILTIN_THEMES["light"]["card_opacity"])
        self.assertEqual(self.store.active["radius"], BUILTIN_THEMES["light"]["radius"])
        self.store.undo()
        self.assertEqual(self.store.active["card_opacity"], 0.3)

    def test_returned_values_cannot_mutate_store(self):
        self.store.active["accent"] = "#000000"
        self.store.themes[0]["name"] = "modified"
        self.store.preferences["scale"] = 150
        self.assertEqual(self.store.active["accent"], BUILTIN_THEMES["light"]["accent"])
        self.assertEqual(self.store.themes[0]["name"], "浅色")
        self.assertEqual(self.store.preferences["scale"], 100)

    def test_imports_copy_png_jpeg_webp_without_changing_source(self):
        for name, data, extension in (("photo.png", png(), ".png"), ("photo.jpeg", JPEG, ".jpg"), ("photo.webp", WEBP, ".webp")):
            source = self.source(name, data)
            relative = self.store.import_background(source)
            imported = self.store.background_path()
            self.assertEqual(imported.parent, self.store.background_dir)
            self.assertEqual(imported.suffix, extension)
            self.assertEqual(imported.read_bytes(), data)
            self.assertEqual(source.read_bytes(), data)
            self.assertFalse(Path(relative).is_absolute())
            source.unlink()
            self.assertTrue(imported.is_file())

    def test_image_copy_is_shared_and_survives_theme_deletion(self):
        source = self.source()
        relative = self.store.import_background(source)
        copied = self.store.create(copy_current=True)
        self.assertEqual(self.store.active["background_image"], relative)
        self.store.delete(copied)
        image = self.store.background_path()
        self.assertTrue(image.is_file())
        self.assertEqual(image.read_bytes(), source.read_bytes())
        self.store.undo()
        self.assertTrue(self.store.background_path().is_file())
        self.assertEqual(self.store.import_background(source), relative)
        self.assertEqual(len(list(self.store.background_dir.glob("*.png"))), 1)

    def test_missing_image_and_external_paths_fall_back(self):
        self.store.import_background(self.source())
        self.store.background_path().unlink()
        self.assertIsNone(self.store.background_path())
        self.assertIsNone(ThemeStore(self.store.root_dir).background_path())
        for path in ("../outside.png", "/tmp/outside.png", "backgrounds/../outside.png"):
            with self.assertRaises(ThemeError):
                self.store.change({"background_image": path})

    def test_rejects_invalid_oversized_and_incomplete_images(self):
        invalid = self.source("invalid.png", b"not a PNG")
        truncated = self.source("truncated.png", png()[:-10])
        dimensions = self.source("huge.png", png(20000, 1))
        pixels = self.source("pixels.png", png(8000, 8000))
        oversized = self.root / "large.jpg"
        with oversized.open("wb") as handle:
            handle.truncate(MAX_IMAGE_BYTES + 1)
        for source in (invalid, truncated, dimensions, pixels, oversized):
            with self.assertRaises(ThemeError):
                self.store.import_background(source)
        self.assertEqual(self.store.active["background_image"], "")
        self.assertEqual(list(self.store.background_dir.iterdir()), [])

    def test_corrupt_document_is_preserved_before_recovery(self):
        broken = b'{"version": 1, invalid'
        self.store.path.write_bytes(broken)
        recovered = ThemeStore(self.store.root_dir)
        self.assertEqual(recovered.active, BUILTIN_THEMES["light"])
        backups = list(self.store.root_dir.glob("appearance.corrupt-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), broken)
        self.assertTrue(recovered.warnings)
        json.loads(self.store.path.read_text(encoding="utf-8"))

    def test_invalid_document_values_recover_without_global_side_effects(self):
        raw = json.loads(self.store.path.read_text(encoding="utf-8"))
        raw["themes"][0]["accent"] = "broken"
        raw["themes"][0]["radius"] = 1000
        raw["themes"].append({"id": "custom", "name": "custom", "base": [], "accent": "123456"})
        raw["active_theme"] = {}
        raw["preferences"] = {"scale": 999, "density": "compact", "logs_expanded": "false"}
        self.store.path.write_text(json.dumps(raw), encoding="utf-8")
        recovered = ThemeStore(self.store.root_dir)
        self.assertEqual(recovered.active["accent"], BUILTIN_THEMES["light"]["accent"])
        self.assertEqual(recovered.active["radius"], 24)
        self.assertEqual(recovered.preferences, {"scale": 100, "density": "compact", "logs_expanded": False, "dismissed_update_version": ""})
        self.assertEqual(recovered.active_id, "light")
        independent = ThemeStore(self.root / "another-store")
        self.assertEqual(independent.preferences, DEFAULT_PREFERENCES)
        self.assertEqual(independent.active, BUILTIN_THEMES["light"])


if __name__ == "__main__":
    unittest.main()
