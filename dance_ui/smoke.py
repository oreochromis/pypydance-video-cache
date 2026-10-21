"""Bounded demo-only checks for the executable produced by Windows CI."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import traceback

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import qVersion
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication
from PIL import Image, __version__ as pillow_version

from . import __version__
from .exe_icon import describe_embedded_icon
from .release_resources import (
    APP_ICON_ICO_RELATIVE_PATH, APP_ICON_PNG_RELATIVE_PATH,
    describe_app_icons, describe_cooldown,
)
from .theme import BUILTIN_THEMES, DEFAULT_PREFERENCES
from .windows_taskbar import describe_windows_taskbar


def _check_openvr_library(report: dict) -> None:
    """Check the frozen DLL without initializing or configuring SteamVR."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    # pyopenvr loads libopenvr_api_64.dll at import time. init() and the
    # applications API intentionally remain unused in this demo-only check.
    import openvr

    report["openvr_version"] = openvr.__version__
    report["checks"].append("openvr_library_loaded_without_initialization")


def inspect_demo(window) -> dict:
    """Exercise real Qt resources/rendering, without starting the cache core."""
    report = {
        "status": "failed", "frozen": bool(getattr(sys, "frozen", False)),
        "platform": sys.platform, "qt_platform": QApplication.platformName(),
        "application_version": __version__, "qt_version": qVersion(),
        "pyside_version": pyside_version, "pillow_version": pillow_version,
        "checks": [], "real_service_tested": False, "vrchat_tested": False,
        "steamvr_tested": False,
    }
    try:
        if not window.controller.demo:
            raise ValueError("Executable smoke checks require an isolated demo.")
        report["checks"].append("demo_mode")
        report["bundled_cooldown_video"] = describe_cooldown()
        report["checks"].append("bundled_cooldown_video")
        report["bundled_application_icons"] = describe_app_icons()
        report["checks"].append("application_icon_resources")
        root = Path(__file__).resolve().parents[1]
        approved = QIcon(QPixmap(str(root / APP_ICON_PNG_RELATIVE_PATH)))
        actual_icons = [window.windowIcon()]
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            actual_icons.append(QApplication.windowIcon())
        for icon in actual_icons:
            if icon.isNull() or approved.isNull():
                raise ValueError("Qt could not load the approved PY application icon.")
            for size in (16, 32, 64, 256):
                if icon.pixmap(size, size).toImage() != approved.pixmap(size, size).toImage():
                    raise ValueError("The application/window icon differs from the approved PY artwork.")
        report["checks"].append("application_window_icon")
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            report["windows_executable_icon"] = describe_embedded_icon(
                Path(sys.executable).read_bytes(), (root / APP_ICON_ICO_RELATIVE_PATH).read_bytes(),
            )
            report["checks"].append("windows_executable_icon")
            report["windows_taskbar"] = describe_windows_taskbar(window)
            report["checks"].append("windows_taskbar_identity_and_icon")
        _check_openvr_library(report)
        if window.store.warnings:
            raise ValueError("; ".join(window.store.warnings))
        if window.store.active != BUILTIN_THEMES["light"]:
            raise ValueError("Use a fresh state directory to check the default light theme.")
        if {theme["id"] for theme in window.store.themes} != {"light", "dark"}:
            raise ValueError("Fresh profiles should contain only the built-in light/dark themes.")
        if window.store.preferences != DEFAULT_PREFERENCES:
            raise ValueError("Fresh reading preferences differ from the built-in defaults.")
        report["theme_id"] = window.store.active_id
        report["theme_name"] = window.store.active["name"]
        report["checks"].append("builtin_light_theme")
        # Exercise Pillow -> Qt without shipping a large wallpaper or importing
        # into the user's managed backgrounds. Only the widget changes, and the
        # normal appearance is restored even if decoding/rendering fails.
        try:
            with tempfile.TemporaryDirectory(prefix="vrchat-dance-smoke-image-") as directory:
                image = Path(directory) / "render-check.png"
                Image.new("RGB", (8, 6), (40, 90, 180)).save(image)
                theme = dict(window.store.active, image_opacity=1.0, image_blur=0.0)
                window.backdrop.set_appearance(theme, image)
                QApplication.processEvents()
                window.grab()
                if window.backdrop.image is None:
                    raise ValueError("Pillow could not decode the generated background.")
                if window.backdrop._render is None or window.backdrop._render.isNull():
                    raise ValueError("The Pillow-to-Qt background rendering failed.")
                report["checks"].append("pillow_generated_background")
        finally:
            window.apply_appearance()
            QApplication.processEvents()
        for name in ("check-light", "check-dark", "up", "down"):
            if QPixmap(":/controls/" + name + ".svg").isNull():
                raise ValueError("Qt could not decode the embedded SVG: " + name)
        report["checks"].append("embedded_svg_icons")
        titles = [button.text() for button in window.nav_buttons]
        if titles != ["运行", "存储", "设置", "外观"] or window.stack.count() != 4:
            raise ValueError("The expected four pages were not created.")
        for index, button in enumerate(window.nav_buttons):
            button.click()
            QApplication.processEvents()
            if window.stack.currentIndex() != index or not window.stack.currentWidget().isVisible():
                raise ValueError("Navigation did not show page " + titles[index])
            if window.grab().isNull():
                raise ValueError("Qt could not render page " + titles[index])
        report["checks"].append("four_page_navigation_and_rendering")
        report["status"] = "passed"
    except Exception:
        report["error"] = traceback.format_exc()
    return report


def save_report(path: Path, report: dict) -> int:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        if sys.stderr is not None:
            print("Cannot write smoke report: " + str(error), file=sys.stderr)
        return 1
    return 0 if report.get("status") == "passed" else 1
