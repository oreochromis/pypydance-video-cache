"""Validate the approved video and application icons shipped with the application."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .exe_icon import ico_frames


COOLDOWN_RELATIVE_PATH = "assets/default_cooldown.mp4"
COOLDOWN_ARCHIVE_PATH = "VRChat跳舞房助手/_internal/" + COOLDOWN_RELATIVE_PATH
COOLDOWN_MAX_BYTES = 5 * 1024 * 1024
# The approved user clip is bundled byte for byte, without recompression.
COOLDOWN_SIZE_BYTES = 5_073_920
COOLDOWN_SHA256 = "453650779b315339a1c68b29a4b486d9fe0c85b49b3c8e0a5af94ff770d7b135"
APP_ICON_PNG_RELATIVE_PATH = "assets/app-icon.png"
APP_ICON_ICO_RELATIVE_PATH = "assets/app-icon.ico"
APP_ICON_PNG_SHA256 = "dfec16921792f9680ee82e3d03b0cd3f9ea333f3557766ebb13b05ba107624d3"
APP_ICON_ICO_SHA256 = "77aec870bcfbadec4ad8c64e81601867214cdad9af96dca2a2cffd6439a9f861"
APP_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def validate_app_icon_bytes(png: bytes, ico: bytes) -> dict:
    """Identify the approved PNG and its seven-size Windows ICO conversion."""
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(png).hexdigest() != APP_ICON_PNG_SHA256:
        raise ValueError("The application PNG icon differs from the approved PY icon.")
    if hashlib.sha256(ico).hexdigest() != APP_ICON_ICO_SHA256:
        raise ValueError("The application ICO icon differs from the approved PY icon.")
    frames = ico_frames(ico)
    if [(frame["width"], frame["height"]) for frame in frames] != [(size, size) for size in APP_ICON_SIZES]:
        raise ValueError("The application ICO must contain all seven approved image sizes.")
    return {
        "png": {"relative_path": APP_ICON_PNG_RELATIVE_PATH, "size_bytes": len(png),
                "sha256": APP_ICON_PNG_SHA256},
        "ico": {"relative_path": APP_ICON_ICO_RELATIVE_PATH, "size_bytes": len(ico),
                "sha256": APP_ICON_ICO_SHA256},
        "ico_sizes": list(APP_ICON_SIZES),
    }


def describe_app_icons(root: Path | None = None) -> dict:
    root = root or Path(__file__).resolve().parents[1]
    return validate_app_icon_bytes(
        (root / APP_ICON_PNG_RELATIVE_PATH).read_bytes(),
        (root / APP_ICON_ICO_RELATIVE_PATH).read_bytes(),
    )


def bundled_cooldown_path() -> Path:
    """PyInstaller places package modules and assets under the same _internal root."""
    return Path(__file__).resolve().parents[1] / COOLDOWN_RELATIVE_PATH


def validate_cooldown_bytes(contents: bytes) -> dict:
    """Reject missing, substituted, oversized, or non-MP4 release resources."""
    if not 12 <= len(contents) <= COOLDOWN_MAX_BYTES:
        raise ValueError("The bundled cooldown video must be nonempty and at most 5 MiB (5,242,880 bytes).")
    box_size = int.from_bytes(contents[:4], "big")
    if contents[4:8] != b"ftyp" or not 12 <= box_size <= len(contents):
        raise ValueError("The bundled cooldown video has no valid ISO MP4 ftyp signature.")
    digest = hashlib.sha256(contents).hexdigest()
    if len(contents) != COOLDOWN_SIZE_BYTES or digest != COOLDOWN_SHA256:
        raise ValueError("The bundled cooldown video differs from the approved source resource.")
    return {
        "relative_path": COOLDOWN_RELATIVE_PATH,
        "size_bytes": len(contents),
        "sha256": digest,
    }


def describe_cooldown(path: Path | None = None) -> dict:
    return validate_cooldown_bytes((path or bundled_cooldown_path()).read_bytes())
