"""Publish verified stable assets from an explicitly requested main-branch build.

The historical script name remains for workflow and tooling compatibility.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dance_ui import __version__
from dance_ui.exe_icon import describe_embedded_icon
from dance_ui.windows_taskbar import APPLICATION_ID
from dance_ui.release_resources import (
    APP_ICON_ICO_RELATIVE_PATH, APP_ICON_PNG_RELATIVE_PATH,
    COOLDOWN_ARCHIVE_PATH,
    describe_app_icons, describe_cooldown, validate_app_icon_bytes, validate_cooldown_bytes,
)


REPOSITORY = "oreochromis/pypydance-video-cache"
# 版本号与代码保持同步，升版本只需改 dance_ui/__init__.py 一处
TAG = f"v{__version__}"
TITLE = f"VRChat 跳舞房助手 {__version__}（Windows 正式版）"
PUBLISH_MARKER = "[publish-release]"
REQUIRED_SMOKE_CHECKS = {
    "demo_mode", "builtin_light_theme", "pillow_generated_background",
    "embedded_svg_icons", "four_page_navigation_and_rendering",
    "openvr_library_loaded_without_initialization",
    "bundled_cooldown_video",
    "application_icon_resources", "application_window_icon", "windows_executable_icon",
    "windows_taskbar_identity_and_icon",
}


def gh(*arguments: str) -> str:
    return subprocess.check_output(["gh", *arguments, "--repo", REPOSITORY], text=True)


def api(path: str) -> dict | list:
    # gh api uses --hostname rather than the release commands' --repo option.
    result = subprocess.check_output(["gh", "api", "repos/" + REPOSITORY + "/" + path], text=True)
    return json.loads(result)


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_taskbar_report(report: object) -> None:
    """Require both native HICON checks and the explicit process identity."""
    if not isinstance(report, dict) or report.get("application_id") != APPLICATION_ID:
        raise ValueError("The native Windows taskbar application identity was not verified.")
    icons = report.get("native_icons")
    if (not isinstance(icons, list) or len(icons) < 2
            or not {"BIG", "SMALL"}.issubset(
                item.get("kind") for item in icons if isinstance(item, dict)
            ) or any(not isinstance(item, dict) or item.get("pixel_verified") is not True for item in icons)):
        raise ValueError("The native Windows taskbar BIG and SMALL icons were not pixel verified.")


def verify_assets(assets: Path, expected_sha: str) -> tuple[dict, list[Path]]:
    archive = assets / "VRChatDanceAssistant-windows-x64.zip"
    checksum = assets / (archive.name + ".sha256")
    info_path = assets / "build-info.json"
    if digest(archive) != checksum.read_text(encoding="ascii").split()[0]:
        raise ValueError("The release ZIP does not match its checksum.")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if info["git_sha"] != expected_sha:
        raise ValueError("The artifact was not built from this workflow's exact commit.")
    if "v" + info.get("application_version", "") != TAG:
        raise ValueError("The release application version does not match the requested tag.")
    source_video = describe_cooldown()
    if info.get("bundled_cooldown_video") != source_video:
        raise ValueError("The release video metadata differs from the approved source video.")
    source_icons = describe_app_icons()
    if info.get("bundled_application_icons") != source_icons:
        raise ValueError("The release application icons differ from the approved source icons.")
    tests, smoke = info["isolated_tests"], info["frozen_demo_smoke"]
    verify_taskbar_report(info.get("windows_taskbar"))
    if smoke.get("windows_taskbar") != info["windows_taskbar"]:
        raise ValueError("The build and frozen smoke Windows taskbar reports differ.")
    if (tests["status"] != "passed" or tests["platform"] != "win32"
            or tests["tests_run"] < 1 or tests["passed"] != tests["tests_run"]
            or any(tests[key] for key in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"))):
        raise ValueError("A complete Windows test run without failures or skips is required.")
    if (smoke["status"] != "passed" or smoke["frozen"] is not True
            or smoke["platform"] != "win32" or smoke["qt_platform"] != "windows"
            or "v" + smoke["application_version"] != TAG
            or smoke.get("theme_id") != "light"
            or smoke.get("bundled_cooldown_video") != source_video
            or smoke.get("bundled_application_icons") != source_icons
            or not info.get("windows_executable_icon")
            or smoke.get("windows_executable_icon") != info["windows_executable_icon"]
            or not REQUIRED_SMOKE_CHECKS.issubset(smoke.get("checks", []))):
        raise ValueError("The frozen Windows executable has not passed the expected stable checks.")
    with ZipFile(archive) as zipped:
        if zipped.testzip() is not None:
            raise ValueError("The release ZIP is damaged.")
        members = zipped.namelist()
        if any("presets" in tuple(part.casefold() for part in Path(name.replace("\\", "/")).parts)
               for name in members):
            raise ValueError("Stable release assets must not include bundled preset resources.")
        for path in ("VRChatDanceAssistant.exe", "_internal/python312.dll", "docs/build-info.json"):
            if "VRChatDanceAssistant/" + path not in members:
                raise ValueError("The release ZIP is incomplete: " + path)
        if COOLDOWN_ARCHIVE_PATH not in members:
            raise ValueError("The release ZIP is missing the bundled cooldown video.")
        if validate_cooldown_bytes(zipped.read(COOLDOWN_ARCHIVE_PATH)) != source_video:
            raise ValueError("The release ZIP cooldown video differs from the approved source video.")
        paths = ["VRChatDanceAssistant/_internal/" + path for path in (APP_ICON_PNG_RELATIVE_PATH, APP_ICON_ICO_RELATIVE_PATH)]
        if not all(path in members for path in paths):
            raise ValueError("The release ZIP is missing application icon resources.")
        if validate_app_icon_bytes(*(zipped.read(path) for path in paths)) != source_icons:
            raise ValueError("The release ZIP application icons differ from the approved source icons.")
        if describe_embedded_icon(
            zipped.read("VRChatDanceAssistant/VRChatDanceAssistant.exe"), zipped.read(paths[1]),
        ) != info["windows_executable_icon"]:
            raise ValueError("The release executable icon differs from the tested Windows executable.")
        if json.loads(zipped.read("VRChatDanceAssistant/docs/build-info.json")) != info:
            raise ValueError("The bundled and attached build reports differ.")
    return info, [archive, checksum, info_path]


def verify_uploaded(release: dict, files: list[Path]) -> None:
    uploaded = {asset["name"]: asset for asset in release["assets"]}
    for file in files:
        asset = uploaded.get(file.name)
        if (asset is None or asset["state"] != "uploaded" or asset["size"] != file.stat().st_size
                or asset.get("digest") != "sha256:" + digest(file)):
            raise ValueError("The remote release asset is missing or does not match: " + file.name)


def tag_commit() -> str | None:
    refs = api("git/matching-refs/tags/" + TAG)
    exact = [ref for ref in refs if ref["ref"] == "refs/tags/" + TAG]
    if not exact:
        return None
    target = exact[0]["object"]
    for _ in range(4):
        if target["type"] == "commit":
            return target["sha"]
        if target["type"] != "tag":
            break
        target = api("git/tags/" + target["sha"])["object"]
    raise ValueError("The release tag does not resolve to a commit.")


def notes(info: dict, checksum: str) -> str:
    smoke, tests = info["frozen_demo_smoke"], info["isolated_tests"]
    run_url = f"https://github.com/{REPOSITORY}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    return f"""VRChat 跳舞房助手 3.0.1 Windows x64 正式版。

## 本次更新

- 修复程序可能被归入 Python 的任务栏标识；沿用已经确认的黑白对话框 PY 图标。
- 为程序设置固定的 Windows 应用身份，并同步原生窗口的大、小图标，避免任务栏将程序归入 Python。
- 保留七种尺寸的 exe 图标和透明 PNG；原有功能及默认 CoolDown 视频保持现有行为。
- Windows 包继续不附带“小余12主题”及其背景。

如果此前已把 Python 图标固定到任务栏，请先取消固定旧项，再从本版 `VRChatDanceAssistant.exe` 重新固定。程序不会自动修改你的固定项或清理系统图标缓存。

## 下载与启动

下载下方 **VRChatDanceAssistant-windows-x64.zip**，完整解压后运行 `VRChatDanceAssistant/VRChatDanceAssistant.exe`，无需安装 Python。必须保留 `_internal` 等全部文件，其中包含默认视频；不能只复制 exe。exe 文件名必须保持纯英文（SteamVR 客户端库无法用中文进程名写日志，VR 面板会报 NoLogPath）；文件夹放在中文路径不受影响。

正常入口按 UAC 提示启动并自动开启缓存服务。程序未签名，UAC 发布者显示为未知。完整步骤见包内 `docs/WINDOWS_PREVIEW.md`。

## 本次实际验证

- 原生 Windows x64 构建：{info['build_platform']}。
- Windows 完整隔离测试：**{tests['tests_run']} 项通过，失败 0、错误 0、跳过 0**。
- 实际打包 exe 以 Windows Qt 平台启动演示并自动退出；内置浅蓝主题、四页导航渲染、SVG 图标、Pillow 背景渲染和 OpenVR DLL 加载检查均通过。
- exe 演示实际读取随包 MP4，核对 ISO MP4 文件标记、文件大小和 SHA-256；打包及发布校验确认源码资源、ZIP 内视频与构建报告一致。此检查不代表已在 VRChat 中播放。
- exe 演示、打包及发布校验读取实际 PE 的 RT_GROUP_ICON / RT_ICON，确认七个尺寸的嵌入图像与批准 ICO 逐帧一致；同时核验 PNG / ICO 源文件、包内资源和 SHA-256。
- 原生 Windows 演示读取真实进程应用身份和窗口大、小 HICON，并在黑白背景上逐像素对比批准的 PY 图案，确认任务栏使用的身份及图标均正确。
- Qt / PySide6 {smoke['qt_version']}，Pillow {smoke['pillow_version']}。完整环境、视频及图标校验值见附件及包内 `docs/build-info.json`。
- [本次 Windows 构建与测试记录]({run_url})。

**待用户实机确认：**你本机任务栏（包含旧固定项、系统图标缓存）及不同显示缩放下的图标外观。自动化检查不读取或更改 Explorer 缓存及固定项。用户此前已反馈旧版本在 Windows + VRChat 中正常工作；默认视频实际播放、SteamVR 自动启动与 UAC、hosts 恢复及其他专项的验证范围继续见构建报告。

源码提交：`{info['git_sha']}`（`main`）。

ZIP SHA-256：

```text
{checksum}
```
"""


def verify_publication_request() -> None:
    """Require an explicit main push, even if this script is called separately."""
    if (os.environ.get("GITHUB_ACTIONS") != "true" or not os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_EVENT_NAME") != "push"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"):
        raise SystemExit("Publishing requires an explicitly requested main-branch push workflow.")
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    except (KeyError, OSError, ValueError) as error:
        raise SystemExit("The release request event cannot be verified.") from error
    if (PUBLISH_MARKER not in (event.get("head_commit") or {}).get("message", "")
            or event.get("after") != os.environ.get("GITHUB_SHA")):
        raise SystemExit("The exact main push must explicitly request " + PUBLISH_MARKER + ".")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=Path("release"))
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    repository = os.environ.get("GITHUB_REPOSITORY", REPOSITORY)
    if repository != REPOSITORY:
        raise SystemExit("This publisher is restricted to the authorized Fork.")
    expected_sha = os.environ.get("GITHUB_SHA") or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    info, files = verify_assets(args.assets, expected_sha)
    print(f"Verified Windows assets from {expected_sha}", flush=True)
    if args.check_only:
        return 0
    verify_publication_request()
    existing_tag = tag_commit()
    if existing_tag is not None and existing_tag != expected_sha:
        raise SystemExit("The release tag already points elsewhere; it will not be overwritten.")
    releases = api("releases?per_page=100")
    matching = [release for release in releases if release["tag_name"] == TAG]
    if len(matching) > 1:
        raise SystemExit("More than one matching release was returned.")
    if matching and not matching[0]["draft"]:
        verify_uploaded(matching[0], files)
        if existing_tag != expected_sha or matching[0]["prerelease"]:
            raise SystemExit("The published release tag or stable state differs from the build.")
        if api("releases/latest")["tag_name"] != TAG:
            raise SystemExit("The published stable release is not the latest release.")
        print("Already published: " + matching[0]["html_url"], flush=True)
        return 0
    notes_path = args.assets / "release-notes.md"
    notes_path.write_text(notes(info, digest(files[0])), encoding="utf-8")
    command = "edit" if matching else "create"
    gh("release", command, TAG, "--target", expected_sha, "--draft=true", "--prerelease=false",
       "--latest=false", "--title", TITLE, "--notes-file", str(notes_path))
    gh("release", "upload", TAG, *(str(file) for file in files), "--clobber")
    release = next(item for item in api("releases?per_page=100") if item["tag_name"] == TAG)
    verify_uploaded(release, files)
    ready_tag = tag_commit()
    if ready_tag is not None and ready_tag != expected_sha:
        raise SystemExit("The release tag changed during upload; the draft will not be published.")
    gh("release", "edit", TAG, "--draft=false", "--prerelease=false", "--latest=true")
    release = api("releases/" + str(release["id"]))
    verify_uploaded(release, files)
    if (release["draft"] or release["prerelease"] or tag_commit() != expected_sha
            or api("releases/latest")["tag_name"] != TAG):
        raise SystemExit("The published release state could not be confirmed.")
    print("Published: " + release["html_url"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
