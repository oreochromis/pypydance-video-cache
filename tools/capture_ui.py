"""Capture the real Qt interface using isolated demo data only.

Example: python tools/capture_ui.py --output docs/screenshots
Optional: --font-dir PATH loads local TTF/OTF fonts for a consistent CI preview.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QScrollArea

from dance_ui.app import MainWindow
from dance_ui.controller import CoreController
from dance_ui.theme import ThemeStore


def settle():
    loop = QEventLoop()
    QTimer.singleShot(220, loop.quit)
    loop.exec()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/screenshots"))
    parser.add_argument("--font-dir", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    if args.font_dir:
        for path in args.font_dir.iterdir():
            if path.suffix.lower() in (".ttf", ".otf"):
                QFontDatabase.addApplicationFont(str(path))
    available = set(QFontDatabase.families())
    family = next((name for name in ("Microsoft YaHei UI", "Noto Sans SC", "PingFang SC", "Segoe UI") if name in available), app.font().family())
    app.setFont(QFont(family, 10))
    with tempfile.TemporaryDirectory(prefix="vrchat-dance-capture-") as state:
        store = ThemeStore(state)
        controller = CoreController(demo=True)
        window = MainWindow(controller, store)
        window.resize(1100, 920)
        window.show()
        controller.publish_initial()

        def capture(name):
            settle()
            path = args.output / (name + ".png")
            if not window.grab().save(str(path)):
                raise OSError("Unable to save " + str(path))
            print(path)

        store.select("light")
        window.apply_appearance()
        for index, name in enumerate(("run-light", "storage", "settings", "appearance")):
            window.navigate(index)
            capture(name)
        window.navigate(1)
        scroll = window.stack.widget(1).findChild(QScrollArea)
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        capture("storage-maintenance")
        store.select("dark")
        window.apply_appearance()
        window.navigate(0)
        capture("run-dark")
        window.close()
        settle()
        controller.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
