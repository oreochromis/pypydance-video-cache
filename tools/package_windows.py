"""Create reviewable release assets from a tested native Windows onedir build."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

# This file is also invoked directly as ``python tools/package_windows.py``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dance_ui import __version__
from dance_ui.exe_icon import describe_embedded_icon
from dance_ui.windows_taskbar import APPLICATION_ID
from dance_ui.release_resources import (
    APP_ICON_ICO_RELATIVE_PATH, APP_ICON_PNG_RELATIVE_PATH,
    COOLDOWN_ARCHIVE_PATH, COOLDOWN_RELATIVE_PATH,
    describe_app_icons, describe_cooldown, validate_app_icon_bytes, validate_cooldown_bytes,
)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def verify_taskbar_report(report: object) -> None:
    """Require real native icon evidence, rather than only the Qt icon property."""
    if not isinstance(report, dict) or report.get("application_id") != APPLICATION_ID:
        raise ValueError("The native Windows taskbar application identity was not verified.")
    icons = report.get("native_icons")
    if (not isinstance(icons, list) or len(icons) < 2
            or not {"BIG", "SMALL"}.issubset(
                item.get("kind") for item in icons if isinstance(item, dict)
            ) or any(not isinstance(item, dict) or item.get("pixel_verified") is not True for item in icons)):
        raise ValueError("The native Windows taskbar BIG and SMALL icons were not pixel verified.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path("dist/VRChat跳舞房助手"))
    parser.add_argument("--reports", type=Path, default=Path("build-reports"))
    parser.add_argument("--output", type=Path, default=Path("release"))
    args = parser.parse_args()
    if sys.platform != "win32" or platform.architecture()[0] != "64bit":
        raise SystemExit("Release assets must be prepared with native 64-bit Windows Python.")

    bundle = args.bundle.resolve()
    if bundle.name != "VRChat跳舞房助手":
        raise SystemExit("The bundle root must be named VRChat跳舞房助手.")
    required = (
        "VRChat跳舞房助手.exe",
        "README.md",
        "LICENSE",
        "docs/DEVELOPMENT.md",
        "docs/WINDOWS_PREVIEW.md",
        "启动.bat",
        "打开演示.bat",
        "_internal/" + COOLDOWN_RELATIVE_PATH,
        "_internal/" + APP_ICON_PNG_RELATIVE_PATH,
        "_internal/" + APP_ICON_ICO_RELATIVE_PATH,
    )
    for relative in required:
        if not (bundle / relative).is_file():
            raise SystemExit(f"The Windows bundle is incomplete: missing {relative}")
    # 运行库 DLL 随构建机的 Python 版本变化（python312.dll / python314.dll ...），按通配符检查
    if not list((bundle / "_internal").glob("python3*.dll")):
        raise SystemExit("The Windows bundle is incomplete: missing _internal/python3XX.dll")
    if not (bundle / "THIRD_PARTY_LICENSES").is_dir():
        raise SystemExit("The Windows bundle is missing third-party license materials.")
    if any("presets" in tuple(part.casefold() for part in path.relative_to(bundle).parts)
           for path in bundle.rglob("*")):
        raise SystemExit("Stable release bundles must not include bundled preset resources.")
    video_info = describe_cooldown()
    if describe_cooldown(bundle / "_internal" / COOLDOWN_RELATIVE_PATH) != video_info:
        raise SystemExit("The bundled cooldown video does not match the source video.")
    icons_info = describe_app_icons()
    if describe_app_icons(bundle / "_internal") != icons_info:
        raise SystemExit("The bundled application icons differ from the approved source icons.")
    embedded_icon = describe_embedded_icon(
        (bundle / "VRChat跳舞房助手.exe").read_bytes(),
        (bundle / "_internal" / APP_ICON_ICO_RELATIVE_PATH).read_bytes(),
    )

    tests = read_json(args.reports / "unit-tests.json")
    smoke = read_json(args.reports / "frozen-smoke.json")
    verify_taskbar_report(smoke.get("windows_taskbar"))
    if (tests.get("status") != "passed" or tests.get("platform") != "win32"
            or tests.get("tests_run", 0) < 1 or tests.get("passed") != tests.get("tests_run")):
        raise SystemExit("The full isolated test suite has not passed.")
    if any(tests.get(key) for key in (
        "failures", "errors", "skipped", "expected_failures", "unexpected_successes",
    )):
        raise SystemExit("The full isolated test suite must pass without failures, errors, or skipped tests.")
    if (
        smoke.get("status") != "passed" or smoke.get("frozen") is not True
        or smoke.get("platform") != "win32" or smoke.get("qt_platform") != "windows"
        or smoke.get("application_version") != __version__ or smoke.get("theme_id") != "light"
        or smoke.get("bundled_cooldown_video") != video_info
        or smoke.get("bundled_application_icons") != icons_info
        or smoke.get("windows_executable_icon") != embedded_icon
        or not {
            "demo_mode", "builtin_light_theme", "pillow_generated_background",
            "embedded_svg_icons", "four_page_navigation_and_rendering",
            "openvr_library_loaded_without_initialization",
            "bundled_cooldown_video",
            "application_icon_resources", "application_window_icon", "windows_executable_icon",
            "windows_taskbar_identity_and_icon",
        }.issubset(smoke.get("checks", []))
    ):
        raise SystemExit("A passing frozen-demo smoke report using native Windows Qt is required.")

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    expected_commit = os.environ.get("GITHUB_SHA")
    if expected_commit and commit != expected_commit:
        raise SystemExit("The checkout commit differs from the GitHub Actions commit.")
    dependencies = (args.reports / "dependencies.txt").read_text(encoding="utf-8-sig").splitlines()
    build_info = {
        "application": "VRChatDanceAssistant",
        "application_version": __version__,
        "asset": "VRChatDanceAssistant-windows-x64.zip",
        "git_sha": commit,
        "git_ref": os.environ.get("GITHUB_REF"),
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "build_platform": platform.platform(),
        "python": sys.version,
        "architecture": platform.architecture()[0],
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "dependencies": dependencies,
        "isolated_tests": tests,
        "frozen_demo_smoke": smoke,
        "bundled_cooldown_video": video_info,
        "bundled_application_icons": icons_info,
        "windows_executable_icon": embedded_icon,
        "windows_taskbar": smoke["windows_taskbar"],
        "user_reported_validation": {
            "source": "Direct user feedback before the stable release",
            "environment": "Windows running VRChat",
            "result": "User reported that the development version operated normally in actual use.",
            "scope": "Prior development build; separate from this commit's automated checks.",
            "individual_features_not_confirmed": [
                "SteamVR automatic startup and the associated UAC interaction",
                "hosts restoration after service shutdown",
                "Every CDN/YouTube cache scenario and old library migration",
            ],
        },
        "not_verified": [
            "User-machine Explorer taskbar appearance, existing pinned items, and icon caches",
            "The newly bundled cooldown video's playback in real Windows VRChat",
            "SteamVR automatic startup and the associated UAC interaction",
            "hosts restoration after service shutdown",
            "Individual CDN/YouTube cache scenarios and existing library migration",
            "Windows display scaling configurations and stable-package manual regression",
        ],
        "distribution": "Extract and keep the complete VRChat跳舞房助手 folder, including _internal.",
    }
    build_info_text = json.dumps(build_info, ensure_ascii=False, indent=2) + "\n"
    (bundle / "docs/build-info.json").write_text(build_info_text, encoding="utf-8")

    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / build_info["asset"]
    files = sorted(path for path in bundle.rglob("*") if path.is_file())
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for path in files:
            if path.is_symlink():
                raise SystemExit(f"Unexpected symbolic link in the Windows bundle: {path}")
            zipped.write(path, arcname=(Path(bundle.name) / path.relative_to(bundle)).as_posix())
    with zipfile.ZipFile(archive) as zipped:
        damaged = zipped.testzip()
        if damaged:
            raise SystemExit(f"Archive integrity check failed: {damaged}")
        for relative in required:
            if f"{bundle.name}/{relative}" not in zipped.namelist():
                raise SystemExit(f"Archive is missing {relative}")
        if validate_cooldown_bytes(zipped.read(COOLDOWN_ARCHIVE_PATH)) != video_info:
            raise SystemExit("The archived cooldown video differs from the source video.")
        archive_prefix = bundle.name + "/_internal/"
        if validate_app_icon_bytes(
            zipped.read(archive_prefix + APP_ICON_PNG_RELATIVE_PATH),
            zipped.read(archive_prefix + APP_ICON_ICO_RELATIVE_PATH),
        ) != icons_info:
            raise SystemExit("The archived application icons differ from the approved source icons.")
        if describe_embedded_icon(
            zipped.read(bundle.name + "/VRChat跳舞房助手.exe"),
            zipped.read(archive_prefix + APP_ICON_ICO_RELATIVE_PATH),
        ) != embedded_icon:
            raise SystemExit("The archived executable icon differs from the tested executable.")
    with archive.open("rb") as archive_file:
        digest = hashlib.file_digest(archive_file, "sha256").hexdigest()
    (args.output / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    (args.output / "build-info.json").write_text(build_info_text, encoding="utf-8")
    for name in ("unit-tests.json", "unit-tests.log", "frozen-smoke.json", "dependencies.txt"):
        shutil.copy2(args.reports / name, args.output / name)
    print(f"Created {archive} ({archive.stat().st_size:,} bytes, {len(files)} files)")
    print(f"SHA256: {digest}")
    print(f"Commit: {commit}")
    print(f"Tests: {tests['tests_run']} run; {len(tests.get('skipped', []))} skipped")
    print("Packaged demo smoke: passed. Prior Windows + VRChat normal operation was user-reported;")
    print("SteamVR startup/UAC, hosts restoration, and other detailed manual checks remain pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
