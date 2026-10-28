"""Release safety checks using the real video, synthetic EXEs, and no GitHub operations."""

import copy
from contextlib import redirect_stdout
import io
import hashlib
import json
import os
from pathlib import Path
import runpy
import struct
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from tools import package_windows as packager
from tools import publish_windows_preview as publisher
from dance_ui import release_resources as resources
from dance_ui.exe_icon import describe_embedded_icon, ico_frames
from dance_ui.windows_taskbar import APPLICATION_ID


def synthetic_icon_executable(ico: bytes, second_group: bool = False) -> bytes:
    """Build a minimal PE resource section, never a runnable Windows program."""
    frames = ico_frames(ico)
    group = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
    for index, frame in enumerate(frames, 1):
        group.extend(struct.pack(
            "<BBBBHHIH", frame["width"] % 256, frame["height"] % 256, frame["colors"], 0,
            frame["planes"], frame["bits"], frame["size"], index,
        ))
    tree = {3: {index: {1033: frame["data"]} for index, frame in enumerate(frames, 1)},
            14: {1: {1033: bytes(group)}}}
    if second_group:
        tree[14][2] = {1033: bytes(group)}
    section = bytearray()

    def allocate(size):
        offset = len(section)
        section.extend(b"\0" * size)
        return offset

    def directory(children):
        offset = allocate(16 + 8 * len(children))
        struct.pack_into("<HH", section, offset + 12, 0, len(children))
        for index, (id_, child) in enumerate(sorted(children.items())):
            if isinstance(child, dict):
                target = directory(child) | 0x80000000
            else:
                target = allocate(16)
                allocate((-len(section)) % 4)
                payload = allocate(len(child))
                section[payload:payload + len(child)] = child
                struct.pack_into("<IIII", section, target, 0x1000 + payload, len(child), 0, 0)
            struct.pack_into("<II", section, offset + 16 + index * 8, id_, target)
        return offset

    directory(tree)
    pe = bytearray(0x200)
    pe[:2] = b"MZ"
    struct.pack_into("<I", pe, 0x3C, 0x80)
    pe[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", pe, 0x84, 0x8664, 1, 0, 0, 0, 0xF0, 0)
    struct.pack_into("<H", pe, 0x98, 0x20B)
    struct.pack_into("<I", pe, 0x98 + 108, 16)
    struct.pack_into("<II", pe, 0x98 + 128, 0x1000, len(section))
    pe[0x188:0x190] = b".rsrc\0\0\0"
    raw_size = ((len(section) + 511) // 512) * 512
    struct.pack_into("<IIII", pe, 0x190, len(section), 0x1000, raw_size, 0x200)
    pe.extend(section)
    pe.extend(b"\0" * (raw_size - len(section)))
    return bytes(pe)


class OpenVRPackagingTests(unittest.TestCase):
    def run_hook(self, package_dir, windows):
        """Exercise the hook without importing OpenVR or requiring PyInstaller."""
        hook_modules = {
            name: ModuleType(name)
            for name in ("PyInstaller", "PyInstaller.compat", "PyInstaller.utils", "PyInstaller.utils.hooks")
        }
        hook_modules["PyInstaller.compat"].is_win = windows
        package_paths = Mock(return_value=(str(package_dir.parent), str(package_dir)))
        hook_modules["PyInstaller.utils.hooks"].get_package_paths = package_paths
        hook = Path(__file__).resolve().parents[1] / "tools" / "pyinstaller_hooks" / "hook-openvr.py"
        with patch.dict(sys.modules, hook_modules):
            result = runpy.run_path(str(hook))
        return result, package_paths

    def test_hook_collects_only_the_windows_x64_library(self):
        with tempfile.TemporaryDirectory(prefix="pypydance-openvr-hook-") as directory:
            package_dir = Path(directory) / "openvr"
            package_dir.mkdir()
            for name in ("libopenvr_api_64.dll", "libopenvr_api_32.dll", "libopenvr_api_64.so", "libopenvr_api_32.dylib"):
                (package_dir / name).write_bytes(b"synthetic library")
            result, package_paths = self.run_hook(package_dir, windows=True)
            self.assertEqual(result["binaries"], [(str(package_dir / "libopenvr_api_64.dll"), "openvr")])
            package_paths.assert_called_once_with("openvr")

    def test_hook_rejects_missing_windows_library_and_ignores_other_platforms(self):
        with tempfile.TemporaryDirectory(prefix="pypydance-openvr-hook-") as directory:
            package_dir = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "Windows x64 OpenVR library is missing"):
                self.run_hook(package_dir, windows=True)
            result, package_paths = self.run_hook(package_dir, windows=False)
            self.assertEqual(result["binaries"], [])
            package_paths.assert_not_called()

    def test_frozen_windows_smoke_loads_openvr_without_initializing_steamvr(self):
        from dance_ui.smoke import _check_openvr_library

        openvr = SimpleNamespace(__version__="2.12.1401", init=Mock(), VRApplications=Mock())
        report = {"checks": []}
        with patch.object(sys, "platform", "win32"), patch.object(sys, "frozen", True, create=True), \
                patch.dict(sys.modules, {"openvr": openvr}):
            _check_openvr_library(report)
        self.assertEqual(report["openvr_version"], "2.12.1401")
        self.assertEqual(report["checks"], ["openvr_library_loaded_without_initialization"])
        openvr.init.assert_not_called()
        openvr.VRApplications.assert_not_called()

    def test_smoke_requires_openvr_only_in_frozen_windows(self):
        from dance_ui.smoke import _check_openvr_library

        with patch.dict(sys.modules, {"openvr": None}):
            for platform, frozen in (("darwin", False), ("darwin", True), ("win32", False)):
                with self.subTest(platform=platform, frozen=frozen), \
                        patch.object(sys, "platform", platform), patch.object(sys, "frozen", frozen, create=True):
                    report = {"checks": []}
                    _check_openvr_library(report)
                    self.assertEqual(report, {"checks": []})
            with patch.object(sys, "platform", "win32"), patch.object(sys, "frozen", True, create=True):
                with self.assertRaises(ModuleNotFoundError):
                    _check_openvr_library({"checks": []})


class ReleaseAssetTests(unittest.TestCase):
    SHA = "a" * 40

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pypydance-release-test-")
        self.addCleanup(temporary.cleanup)
        self.assets = Path(temporary.name)
        self.block_process = patch.object(
            publisher.subprocess, "check_output",
            side_effect=AssertionError("Release tests must not execute git, gh, or any subprocess."),
        )
        self.block_process.start()
        self.addCleanup(self.block_process.stop)
        self.cooldown_bytes = resources.bundled_cooldown_path().read_bytes()
        self.cooldown_info = resources.describe_cooldown()
        source_root = Path(__file__).resolve().parents[1]
        self.icon_png = (source_root / resources.APP_ICON_PNG_RELATIVE_PATH).read_bytes()
        self.icon_ico = (source_root / resources.APP_ICON_ICO_RELATIVE_PATH).read_bytes()
        self.icon_info = resources.describe_app_icons()
        self.exe_bytes = synthetic_icon_executable(self.icon_ico)
        self.embedded_icon = describe_embedded_icon(self.exe_bytes, self.icon_ico)
        self.taskbar_info = {
            "application_id": APPLICATION_ID,
            "native_icons": [
                {"kind": "BIG", "size": [32, 32], "pixel_verified": True},
                {"kind": "SMALL", "size": [16, 16], "pixel_verified": True},
            ],
        }
        self.info = {
            "git_sha": self.SHA,
            "application_version": publisher.TAG.removeprefix("v"),
            "build_platform": "Windows synthetic test fixture",
            "bundled_cooldown_video": dict(self.cooldown_info),
            "bundled_application_icons": copy.deepcopy(self.icon_info),
            "windows_executable_icon": dict(self.embedded_icon),
            "windows_taskbar": copy.deepcopy(self.taskbar_info),
            "isolated_tests": {
                "status": "passed", "platform": "win32", "tests_run": 3, "passed": 3,
                "failures": [], "errors": [], "skipped": [],
                "expected_failures": [], "unexpected_successes": [],
            },
            "frozen_demo_smoke": {
                "status": "passed", "frozen": True, "platform": "win32",
                "qt_platform": "windows", "application_version": publisher.TAG.removeprefix("v"),
                "theme_id": "light", "checks": sorted(publisher.REQUIRED_SMOKE_CHECKS),
                "qt_version": "6.synthetic", "pillow_version": "synthetic",
                "bundled_cooldown_video": dict(self.cooldown_info),
                "bundled_application_icons": copy.deepcopy(self.icon_info),
                "windows_executable_icon": dict(self.embedded_icon),
                "windows_taskbar": copy.deepcopy(self.taskbar_info),
            },
        }

    def write_assets(self, info=None, extra_files=None):
        info = copy.deepcopy(self.info if info is None else info)
        text = json.dumps(info, ensure_ascii=False)
        archive = self.assets / "VRChatDanceAssistant-windows-x64.zip"
        with ZipFile(archive, "w") as zipped:
            for name in ("_internal/python312.dll",):
                zipped.writestr("VRChat跳舞房助手/" + name, b"tiny synthetic fixture")
            files = {
                "VRChat跳舞房助手.exe": self.exe_bytes,
                "_internal/" + resources.COOLDOWN_RELATIVE_PATH: self.cooldown_bytes,
                "_internal/" + resources.APP_ICON_PNG_RELATIVE_PATH: self.icon_png,
                "_internal/" + resources.APP_ICON_ICO_RELATIVE_PATH: self.icon_ico,
            }
            files.update(extra_files or {})
            for name, contents in files.items():
                if contents is not None:
                    zipped.writestr("VRChat跳舞房助手/" + name, contents)
            zipped.writestr("VRChat跳舞房助手/docs/build-info.json", text)
        (self.assets / "build-info.json").write_text(text, encoding="utf-8")
        (self.assets / (archive.name + ".sha256")).write_text(
            publisher.digest(archive) + "  " + archive.name + "\n", encoding="ascii",
        )
        return info

    def publication_environment(self, **overrides):
        event = self.assets / "github-event.json"
        event.write_text(json.dumps({
            "after": self.SHA, "head_commit": {"message": "Release 3.0.1 [publish-release]"},
        }), encoding="utf-8")
        environment = {
            "GITHUB_REPOSITORY": publisher.REPOSITORY, "GITHUB_SHA": self.SHA,
            "GITHUB_ACTIONS": "true", "GH_TOKEN": "synthetic-test-token",
            "GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_PATH": str(event), "GITHUB_RUN_ID": "synthetic-run",
        }
        environment.update(overrides)
        return environment

    def remote_assets(self, files):
        return {
            "assets": [
                {"name": file.name, "state": "uploaded", "size": file.stat().st_size,
                 "digest": "sha256:" + publisher.digest(file)}
                for file in files
            ],
        }

    def test_accepts_complete_windows_artifact_and_matching_uploaded_assets(self):
        expected = self.write_assets()
        info, files = publisher.verify_assets(self.assets, self.SHA)
        self.assertEqual(info, expected)
        self.assertEqual(len(files), 3)
        self.assertGreater(files[0].stat().st_size, len(self.cooldown_bytes))
        publisher.verify_uploaded(self.remote_assets(files), files)

    def test_approved_resource_is_an_actual_readable_mp4_under_five_megabytes(self):
        self.assertEqual(self.cooldown_bytes[4:8], b"ftyp")
        self.assertGreater(len(self.cooldown_bytes), 1_000_000)
        self.assertLessEqual(len(self.cooldown_bytes), resources.COOLDOWN_MAX_BYTES)
        self.assertEqual(self.cooldown_info["size_bytes"], len(self.cooldown_bytes))
        self.assertEqual(self.cooldown_info["sha256"], hashlib.sha256(self.cooldown_bytes).hexdigest())
        self.assertEqual(resources.describe_cooldown(), self.cooldown_info)

    def test_resource_validator_rejects_empty_oversized_and_substituted_mp4(self):
        forged_mp4 = self.cooldown_bytes[:32] + b"\0" * (len(self.cooldown_bytes) - 32)
        for contents in (b"", b"fake MP4", b"\0" * (resources.COOLDOWN_MAX_BYTES + 1), forged_mp4):
            with self.subTest(size=len(contents)), self.assertRaises(ValueError):
                resources.validate_cooldown_bytes(contents)

    def test_approved_icon_resources_have_seven_windows_sizes_and_matching_pe_images(self):
        self.assertEqual(resources.validate_app_icon_bytes(self.icon_png, self.icon_ico), self.icon_info)
        self.assertEqual(self.embedded_icon["frame_count"], 7)
        self.assertEqual(self.embedded_icon["frame_sizes"], [[size, size] for size in resources.APP_ICON_SIZES])
        self.assertEqual(self.embedded_icon["ico_sha256"], self.icon_info["ico"]["sha256"])
        self.assertEqual(self.embedded_icon["executable_sha256"], hashlib.sha256(self.exe_bytes).hexdigest())

    def test_rejects_missing_substituted_or_corrupt_icons_despite_a_matching_zip_checksum(self):
        for path, data in (
            ("_internal/" + resources.APP_ICON_PNG_RELATIVE_PATH, None),
            ("_internal/" + resources.APP_ICON_PNG_RELATIVE_PATH, self.icon_png[:-1]),
            ("_internal/" + resources.APP_ICON_ICO_RELATIVE_PATH, b"stock icon"),
            ("VRChat跳舞房助手.exe", b"MZ" + b"\0" * 1024),
        ):
            with self.subTest(path=path):
                self.write_assets(extra_files={path: data})
                with self.assertRaisesRegex(ValueError, "icon|PE"):
                    publisher.verify_assets(self.assets, self.SHA)

    def test_rejects_replaced_embedded_image_even_with_original_ico_beside_exe(self):
        frame = ico_frames(self.icon_ico)[0]["data"]
        replaced = self.exe_bytes.replace(frame, b"\0" * len(frame), 1)
        with self.assertRaisesRegex(ValueError, "does not embed"):
            describe_embedded_icon(replaced, self.icon_ico)
        self.write_assets(extra_files={"VRChat跳舞房助手.exe": replaced})
        with self.assertRaisesRegex(ValueError, "does not embed"):
            publisher.verify_assets(self.assets, self.SHA)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            describe_embedded_icon(synthetic_icon_executable(self.icon_ico, second_group=True), self.icon_ico)

    def test_rejects_invalid_ico_and_truncated_or_out_of_bounds_pe(self):
        for ico in (b"", b"fake", self.icon_ico[:30], b"\0\0\x02\0\0\0"):
            with self.subTest(ico_size=len(ico)), self.assertRaises(ValueError):
                ico_frames(ico)
        invalid_rva = bytearray(self.exe_bytes)
        struct.pack_into("<I", invalid_rva, 0x98 + 128, 0xFFFF0000)
        for exe in (b"", self.exe_bytes[:100], self.exe_bytes[:0x202], bytes(invalid_rva)):
            with self.subTest(exe_size=len(exe)), self.assertRaises(ValueError):
                describe_embedded_icon(exe, self.icon_ico)

    def test_rejects_icon_metadata_that_differs_from_source_smoke_or_executable(self):
        for section in (None, "frozen_demo_smoke"):
            for field in ("bundled_application_icons", "windows_executable_icon"):
                with self.subTest(section=section, field=field):
                    info = copy.deepcopy(self.info)
                    target = info if section is None else info[section]
                    target[field] = {"forged": True}
                    self.write_assets(info)
                    with self.assertRaises(ValueError):
                        publisher.verify_assets(self.assets, self.SHA)

    def test_taskbar_report_requires_native_big_and_small_pixels_and_application_identity(self):
        invalid = (
            None,
            {},
            dict(self.taskbar_info, application_id="Python"),
            dict(self.taskbar_info, native_icons=[]),
            dict(self.taskbar_info, native_icons=self.taskbar_info["native_icons"][:1]),
            dict(self.taskbar_info, native_icons=[
                {"kind": "BIG", "pixel_verified": True},
                {"kind": "BIG", "pixel_verified": True},
            ]),
            dict(self.taskbar_info, native_icons=[
                {"kind": "BIG", "pixel_verified": True},
                {"kind": "SMALL", "pixel_verified": False},
            ]),
            dict(self.taskbar_info, native_icons=[
                {"kind": "BIG", "pixel_verified": "true"},
                {"kind": "SMALL", "pixel_verified": True},
            ]),
            dict(self.taskbar_info, native_icons=[None, {"kind": "SMALL", "pixel_verified": True}]),
        )
        for verifier in (packager.verify_taskbar_report, publisher.verify_taskbar_report):
            verifier(self.taskbar_info)
            for report in invalid:
                with self.subTest(verifier=verifier.__module__, report=report), \
                        self.assertRaisesRegex(ValueError, "taskbar"):
                    verifier(report)

    def test_rejects_missing_or_inconsistent_native_taskbar_evidence(self):
        for section in (None, "frozen_demo_smoke"):
            for report in (None, dict(self.taskbar_info, application_id="Python")):
                with self.subTest(section=section, report=report):
                    info = copy.deepcopy(self.info)
                    target = info if section is None else info[section]
                    target["windows_taskbar"] = report
                    self.write_assets(info)
                    with self.assertRaisesRegex(ValueError, "taskbar"):
                        publisher.verify_assets(self.assets, self.SHA)
        info = copy.deepcopy(self.info)
        info["frozen_demo_smoke"]["windows_taskbar"]["native_icons"][0]["pixel_verified"] = False
        self.write_assets(info)
        with self.assertRaisesRegex(ValueError, "taskbar"):
            publisher.verify_assets(self.assets, self.SHA)

    def test_rejects_missing_or_changed_video_despite_a_matching_archive_checksum(self):
        path = "_internal/" + resources.COOLDOWN_RELATIVE_PATH
        for contents in (None, b"", b"not an MP4", self.cooldown_bytes[:-1]):
            with self.subTest(size=None if contents is None else len(contents)):
                self.write_assets(extra_files={path: contents})
                with self.assertRaisesRegex(ValueError, "cooldown video"):
                    publisher.verify_assets(self.assets, self.SHA)

    def test_rejects_video_metadata_that_does_not_match_the_source_and_smoke(self):
        for section in (None, "frozen_demo_smoke"):
            with self.subTest(section=section):
                info = copy.deepcopy(self.info)
                target = info if section is None else info[section]
                target["bundled_cooldown_video"]["sha256"] = "0" * 64
                self.write_assets(info)
                with self.assertRaises(ValueError):
                    publisher.verify_assets(self.assets, self.SHA)

    def test_rejects_wrong_commit_incomplete_tests_and_non_native_smoke(self):
        cases = [
            ("git_sha", None, "b" * 40),
            ("application_version", None, "2.0.1"),
            ("isolated_tests", "skipped", [{"test": "GUI", "reason": "Qt missing"}]),
            ("isolated_tests", "expected_failures", ["GUI"]),
            ("isolated_tests", "unexpected_successes", ["GUI"]),
            ("isolated_tests", "platform", "darwin"),
            ("isolated_tests", "passed", 2),
            ("frozen_demo_smoke", "qt_platform", "offscreen"),
            ("frozen_demo_smoke", "frozen", False),
            ("frozen_demo_smoke", "platform", "darwin"),
            ("frozen_demo_smoke", "application_version", "2.0.0-preview.999"),
            ("frozen_demo_smoke", "theme_id", "custom-theme"),
            ("frozen_demo_smoke", "checks", ["demo_mode"]),
        ]
        for section, key, value in cases:
            with self.subTest(section=section, field=key):
                info = copy.deepcopy(self.info)
                if key is None:
                    info[section] = value
                else:
                    info[section][key] = value
                self.write_assets(info)
                with self.assertRaises(ValueError):
                    publisher.verify_assets(self.assets, self.SHA)

    def test_rejects_bundled_presets_even_with_a_valid_checksum(self):
        for name in (
            "_internal/dance_ui/presets/xiaoyu12/theme.json",
            "_internal/dance_ui/PRESETS/xiaoyu12/background.png",
            "_internal\\dance_ui\\presets\\xiaoyu12\\background.png",
        ):
            with self.subTest(name=name):
                self.write_assets(extra_files={name: b"excluded preset"})
                with self.assertRaisesRegex(ValueError, "must not include bundled preset"):
                    publisher.verify_assets(self.assets, self.SHA)

    def test_rejects_remote_assets_with_wrong_hash_size_state_or_missing_file(self):
        self.write_assets()
        _, files = publisher.verify_assets(self.assets, self.SHA)
        for key, value in (("digest", "sha256:" + "0" * 64), ("size", 0), ("state", "starter")):
            with self.subTest(field=key):
                release = self.remote_assets(files)
                release["assets"][0][key] = value
                with self.assertRaisesRegex(ValueError, "remote release asset"):
                    publisher.verify_uploaded(release, files)
        with self.assertRaisesRegex(ValueError, "remote release asset"):
            publisher.verify_uploaded({"assets": []}, files)

    def test_resolves_only_the_exact_tag_and_follows_annotated_tags(self):
        reference = "refs/tags/" + publisher.TAG
        nearby = {"ref": reference + "-other", "object": {"type": "commit", "sha": "b" * 40}}
        direct = {"ref": reference, "object": {"type": "commit", "sha": self.SHA}}
        with patch.object(publisher, "api", return_value=[nearby]) as api:
            self.assertIsNone(publisher.tag_commit())
            api.assert_called_once_with("git/matching-refs/tags/" + publisher.TAG)
        with patch.object(publisher, "api", return_value=[nearby, direct]):
            self.assertEqual(publisher.tag_commit(), self.SHA)
        annotated = {"ref": reference, "object": {"type": "tag", "sha": "c" * 40}}
        with patch.object(publisher, "api", side_effect=[
            [annotated], {"object": {"type": "commit", "sha": self.SHA}},
        ]) as api:
            self.assertEqual(publisher.tag_commit(), self.SHA)
            self.assertEqual(api.call_args_list[-1].args, ("git/tags/" + "c" * 40,))
        invalid = {"ref": reference, "object": {"type": "blob", "sha": "d" * 40}}
        with patch.object(publisher, "api", return_value=[invalid]):
            with self.assertRaisesRegex(ValueError, "does not resolve to a commit"):
                publisher.tag_commit()

    def test_rejects_an_unauthorized_repository_before_reading_or_publishing(self):
        with patch.object(sys, "argv", ["publish_windows_preview.py"]), patch.dict(
            os.environ, {"GITHUB_REPOSITORY": "someone-else/pypydance-video-cache"}, clear=True,
        ), patch.object(publisher, "verify_assets") as verify, patch.object(publisher, "gh") as gh:
            with self.assertRaisesRegex(SystemExit, "authorized Fork"):
                publisher.main()
            verify.assert_not_called()
            gh.assert_not_called()

    def test_refuses_to_replace_an_existing_release_tag_at_another_commit(self):
        self.write_assets()
        environment = self.publication_environment()
        arguments = ["publish_windows_preview.py", "--assets", str(self.assets)]
        with patch.object(sys, "argv", arguments), patch.dict(os.environ, environment, clear=True), \
                patch.object(publisher, "tag_commit", return_value="b" * 40), \
                patch.object(publisher, "api") as api, patch.object(publisher, "gh") as gh, \
                redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "will not be overwritten"):
                publisher.main()
            api.assert_not_called()
            gh.assert_not_called()

    def test_only_an_explicit_exact_main_push_can_publish(self):
        cases = (
            {"GITHUB_REF": "refs/heads/ui-redesign"},
            {"GITHUB_REF": "refs/tags/v2.0.0"},
            {"GITHUB_EVENT_NAME": "workflow_dispatch"},
            {"GITHUB_ACTIONS": "false"},
            {"GH_TOKEN": ""},
            {"GITHUB_SHA": "b" * 40},
            {"GITHUB_EVENT_PATH": str(self.assets / "missing-event.json")},
        )
        for override in cases:
            with self.subTest(override=override):
                environment = self.publication_environment(**override)
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(SystemExit):
                        publisher.verify_publication_request()
        for message in ("Release 2.0.0", "Release [publish-preview]"):
            environment = self.publication_environment()
            Path(environment["GITHUB_EVENT_PATH"]).write_text(json.dumps({
                "after": self.SHA, "head_commit": {"message": message},
            }), encoding="utf-8")
            with self.subTest(message=message), patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(SystemExit, "explicitly request"):
                    publisher.verify_publication_request()
        with patch.dict(os.environ, self.publication_environment(), clear=True):
            publisher.verify_publication_request()

    def test_publishes_verified_draft_as_a_stable_latest_release(self):
        self.write_assets()
        _, files = publisher.verify_assets(self.assets, self.SHA)
        uploaded = self.remote_assets(files) | {
            "id": 7, "tag_name": publisher.TAG, "html_url": "https://example.invalid/release",
        }
        draft = uploaded | {"draft": True, "prerelease": False}
        published = uploaded | {"draft": False, "prerelease": False}
        with patch.object(sys, "argv", ["publish_windows_preview.py", "--assets", str(self.assets)]), \
                patch.dict(os.environ, self.publication_environment(), clear=True), \
                patch.object(publisher, "tag_commit", side_effect=[None, None, self.SHA]), \
                patch.object(publisher, "api", side_effect=[[], [draft], published, published]), \
                patch.object(publisher, "gh") as gh, redirect_stdout(io.StringIO()):
            self.assertEqual(publisher.main(), 0)
        create, upload, publish = [call.args for call in gh.call_args_list]
        self.assertEqual(create[:3], ("release", "create", publisher.TAG))
        self.assertIn("--draft=true", create)
        self.assertIn("--prerelease=false", create)
        self.assertEqual(upload[:3], ("release", "upload", publisher.TAG))
        self.assertEqual(publish, (
            "release", "edit", publisher.TAG, "--draft=false", "--prerelease=false", "--latest=true",
        ))

    def test_does_not_publish_a_draft_if_uploaded_assets_differ(self):
        self.write_assets()
        draft = {"id": 7, "tag_name": publisher.TAG, "draft": True, "assets": []}
        with patch.object(sys, "argv", ["publish_windows_preview.py", "--assets", str(self.assets)]), \
                patch.dict(os.environ, self.publication_environment(), clear=True), \
                patch.object(publisher, "tag_commit", return_value=None), \
                patch.object(publisher, "api", side_effect=[[], [draft]]), \
                patch.object(publisher, "gh") as gh, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "remote release asset"):
                publisher.main()
        self.assertEqual(len(gh.call_args_list), 2)
        self.assertFalse(any("--draft=false" in call.args for call in gh.call_args_list))

    def test_keeps_a_draft_private_if_the_tag_changes_during_upload(self):
        self.write_assets()
        _, files = publisher.verify_assets(self.assets, self.SHA)
        draft = self.remote_assets(files) | {"tag_name": publisher.TAG, "draft": True}
        with patch.object(sys, "argv", ["publish_windows_preview.py", "--assets", str(self.assets)]), \
                patch.dict(os.environ, self.publication_environment(), clear=True), \
                patch.object(publisher, "tag_commit", side_effect=[None, "b" * 40]), \
                patch.object(publisher, "api", side_effect=[[], [draft]]), \
                patch.object(publisher, "gh") as gh, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "changed during upload"):
                publisher.main()
        self.assertEqual(len(gh.call_args_list), 2)
        self.assertFalse(any("--draft=false" in call.args for call in gh.call_args_list))

    def test_never_overwrites_an_existing_published_release(self):
        self.write_assets()
        _, files = publisher.verify_assets(self.assets, self.SHA)
        published = self.remote_assets(files) | {
            "tag_name": publisher.TAG, "draft": False, "prerelease": False,
            "html_url": "https://example.invalid/release",
        }
        with patch.object(sys, "argv", ["publish_windows_preview.py", "--assets", str(self.assets)]), \
                patch.dict(os.environ, self.publication_environment(), clear=True), \
                patch.object(publisher, "tag_commit", return_value=self.SHA), \
                patch.object(publisher, "api", side_effect=[[published], published]), \
                patch.object(publisher, "gh") as gh, redirect_stdout(io.StringIO()):
            self.assertEqual(publisher.main(), 0)
            gh.assert_not_called()

    def test_packager_excludes_presets_and_records_user_feedback_separately(self):
        bundle = self.assets / "VRChat跳舞房助手"
        for name in (
            "VRChat跳舞房助手.exe", "_internal/python312.dll", "README.md", "LICENSE",
            "docs/DEVELOPMENT.md", "docs/WINDOWS_PREVIEW.md", "启动.bat",
        ):
            target = bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("synthetic package fixture", encoding="utf-8")
        (bundle / "THIRD_PARTY_LICENSES").mkdir()
        (bundle / "_internal" / resources.COOLDOWN_RELATIVE_PATH).parent.mkdir(parents=True, exist_ok=True)
        (bundle / "_internal" / resources.COOLDOWN_RELATIVE_PATH).write_bytes(self.cooldown_bytes)
        (bundle / "_internal" / resources.APP_ICON_PNG_RELATIVE_PATH).write_bytes(self.icon_png)
        (bundle / "_internal" / resources.APP_ICON_ICO_RELATIVE_PATH).write_bytes(self.icon_ico)
        (bundle / "VRChat跳舞房助手.exe").write_bytes(self.exe_bytes)
        reports = self.assets / "reports"
        reports.mkdir()
        for name, value in (
            ("unit-tests.json", self.info["isolated_tests"]),
            ("frozen-smoke.json", self.info["frozen_demo_smoke"]),
        ):
            (reports / name).write_text(json.dumps(value), encoding="utf-8")
        for name in ("dependencies.txt", "unit-tests.log"):
            (reports / name).write_text("synthetic report", encoding="utf-8")
        output = self.assets / "packaged"
        arguments = ["package_windows.py", "--bundle", str(bundle), "--reports", str(reports),
                     "--output", str(output)]
        with patch.object(sys, "argv", arguments), patch.object(sys, "platform", "win32"), \
                patch.object(packager.platform, "architecture", return_value=("64bit", "WindowsPE")), \
                patch.object(packager.platform, "platform", return_value="Windows synthetic fixture"), \
                patch.object(packager.subprocess, "check_output", return_value=self.SHA + "\n"), \
                patch.dict(os.environ, {"GITHUB_SHA": self.SHA}, clear=True), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(packager.main(), 0)
            info, _ = publisher.verify_assets(output, self.SHA)
            self.assertEqual(info["user_reported_validation"]["environment"], "Windows running VRChat")
            self.assertEqual(info["frozen_demo_smoke"], self.info["frozen_demo_smoke"])
            self.assertEqual(info["bundled_cooldown_video"], self.cooldown_info)
            self.assertEqual(info["bundled_application_icons"], self.icon_info)
            self.assertEqual(info["windows_executable_icon"], self.embedded_icon)
            self.assertEqual(info["windows_taskbar"], self.taskbar_info)
            self.assertTrue(info["not_verified"])
            preset = bundle / "_internal/dance_ui/presets/xiaoyu12/background.png"
            preset.parent.mkdir(parents=True)
            preset.write_bytes(b"excluded large image fixture")
            with self.assertRaisesRegex(SystemExit, "must not include bundled preset"):
                packager.main()


if __name__ == "__main__":
    unittest.main()
