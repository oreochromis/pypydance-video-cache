"""The approved PY artwork must reach both source and frozen Qt windows."""

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from dance_ui import widgets
    QT_READY = True
except ImportError:
    QT_READY = False


@unittest.skipUnless(QT_READY, "Install the Qt UI dependencies to check the application icon.")
class ApplicationIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.artwork = Path(__file__).resolve().parents[1] / "assets" / "app-icon.png"

    def test_packaged_artwork_is_the_exact_approved_transparent_png(self):
        self.assertEqual(
            hashlib.sha256(self.artwork.read_bytes()).hexdigest(),
            "bc3c9218c861c121da0a8053442ac5882a9e40fb8d4c3b35c570405fcc345193",
        )
        approved = QImage(str(self.artwork))
        self.assertEqual(approved.size(), QSize(512, 512))
        self.assertTrue(approved.hasAlphaChannel())
        actual = widgets.app_icon().pixmap(512, 512).toImage()
        self.assertEqual(actual, approved.convertToFormat(actual.format()))

    def test_icon_scales_for_windows_sizes_and_keeps_transparent_corners(self):
        icon = widgets.app_icon()
        for size in (16, 24, 32, 48, 64, 128, 256):
            with self.subTest(size=size):
                image = icon.pixmap(size, size).toImage()
                self.assertFalse(image.isNull())
                self.assertEqual(image.size(), QSize(size, size))
                self.assertTrue(image.hasAlphaChannel())
                self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
                self.assertGreater(image.pixelColor(size // 2, size // 2).alpha(), 0)

    def test_frozen_layout_uses_bundled_artwork_independently_of_working_directory(self):
        with tempfile.TemporaryDirectory(prefix="pypydance-icon-test-") as directory:
            root = Path(directory)
            bundle = root / "_internal"
            (bundle / "dance_ui").mkdir(parents=True)
            (bundle / "assets").mkdir()
            shutil.copyfile(self.artwork, bundle / "assets" / "app-icon.png")
            previous_directory = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(widgets, "__file__", str(bundle / "dance_ui" / "widgets.py")):
                    actual = widgets.app_icon().pixmap(64, 64).toImage()
            finally:
                os.chdir(previous_directory)
            self.assertEqual(actual, widgets.app_icon().pixmap(64, 64).toImage())

    def test_missing_or_corrupt_artwork_is_reported_instead_of_a_silent_fallback(self):
        with tempfile.TemporaryDirectory(prefix="pypydance-icon-test-") as directory:
            root = Path(directory)
            (root / "dance_ui").mkdir()
            (root / "assets").mkdir()
            with patch.object(widgets, "__file__", str(root / "dance_ui" / "widgets.py")):
                with self.assertRaisesRegex(FileNotFoundError, "app-icon.png"):
                    widgets.app_icon()
                (root / "assets" / "app-icon.png").write_bytes(b"broken artwork")
                with self.assertRaisesRegex(ValueError, "app-icon.png"):
                    widgets.app_icon()


if __name__ == "__main__":
    unittest.main()
