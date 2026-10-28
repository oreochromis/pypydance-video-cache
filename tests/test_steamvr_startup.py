"""No SteamVR, Windows processes, registration, or elevation are used here."""
import base64
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from dance_ui import steamvr_startup as startup
from dance_ui import single_instance


class FakeApplications:
    def __init__(self):
        self.installed = False
        self.enabled = False
        self.calls = []
        self.fail_enable = False

    def isApplicationInstalled(self, key):
        return self.installed

    def getApplicationAutoLaunch(self, key):
        return self.enabled

    def addApplicationManifest(self, path, temporary):
        self.calls.append(("add", path, temporary))
        self.installed = True

    def removeApplicationManifest(self, path):
        self.calls.append(("remove", path))
        self.installed = False
        self.enabled = False

    def setApplicationAutoLaunch(self, key, enabled):
        self.calls.append(("set", enabled))
        if self.fail_enable and enabled:
            raise RuntimeError("registration refused")
        self.enabled = enabled


class SteamVRStartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = startup.SteamVRStartup(self.directory.name)
        self.api = FakeApplications()

    @contextmanager
    def session(self):
        yield self.api

    def test_non_windows_status_never_loads_openvr(self):
        with patch.object(startup.sys, "platform", "darwin"), patch.object(startup, "applications") as session:
            self.assertFalse(self.manager.status()["available"])
            with self.assertRaisesRegex(RuntimeError, "Windows"):
                self.manager.set_enabled(True)
            session.assert_not_called()

    def test_enabling_and_disabling_read_back_runtime_state(self):
        with patch.object(startup.sys, "platform", "win32"), patch.object(startup, "applications", self.session), patch.object(Path, "is_file", return_value=True):
            self.assertFalse(self.manager.status()["enabled"])
            self.assertFalse(self.manager.manifest_path.exists())
            self.assertTrue(self.manager.set_enabled(True)["enabled"])
            self.assertTrue(self.manager.status()["enabled"])
            self.assertFalse(self.manager.set_enabled(False)["enabled"])
            self.assertFalse(self.manager.status()["enabled"])
        self.assertIn(("set", True), self.api.calls)
        self.assertIn(("set", False), self.api.calls)
        self.assertFalse(self.api.calls[0][2], "Manifest must persist across SteamVR restarts")

    def test_manifest_handles_spaces_unicode_quotes_and_elevation(self):
        manifest = startup.launch_manifest("C:\\小余's files\\app.exe", ["--steamvr-startup"], "C:\\小余's files", "C:\\Windows\\powershell.exe")
        app = manifest["applications"][0]
        script = base64.b64decode(app["arguments"].split()[-1]).decode("utf-16le")
        self.assertTrue(app["is_dashboard_overlay"])
        self.assertEqual(app["launch_type"], "binary")
        self.assertIn("小余''s files", script)
        self.assertIn("-Verb RunAs", script)
        self.assertIn("OpenExisting", script)
        self.assertIn("--steamvr-startup", script)
        self.assertNotIn("ExecutionPolicy", app["arguments"])

    def test_failure_rolls_back_new_registration_and_manifest(self):
        self.api.fail_enable = True
        with patch.object(startup.sys, "platform", "win32"), patch.object(startup, "applications", self.session), patch.object(Path, "is_file", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "保存失败"):
                self.manager.set_enabled(True)
        self.assertFalse(self.api.installed)
        self.assertFalse(self.api.enabled)
        self.assertFalse(self.manager.manifest_path.exists())
        self.assertTrue(any(call[0] == "remove" for call in self.api.calls))

    def test_failure_restores_existing_manifest_and_disabled_flag(self):
        self.manager.manifest_path.parent.mkdir(parents=True)
        self.manager.manifest_path.write_bytes(b"old manifest")
        self.api.installed = True
        self.api.fail_enable = True
        with patch.object(startup.sys, "platform", "win32"), patch.object(startup, "applications", self.session), patch.object(Path, "is_file", return_value=True):
            with self.assertRaises(RuntimeError):
                self.manager.set_enabled(True)
        self.assertEqual(self.manager.manifest_path.read_bytes(), b"old manifest")
        self.assertTrue(self.api.installed)
        self.assertFalse(self.api.enabled)

    def test_existing_foreign_registration_keeps_valid_manifest_on_failed_update(self):
        self.api.installed = True
        self.api.fail_enable = True
        with patch.object(startup.sys, "platform", "win32"), patch.object(startup, "applications", self.session), patch.object(Path, "is_file", return_value=True):
            with self.assertRaises(RuntimeError):
                self.manager.set_enabled(True)
        self.assertTrue(self.manager.manifest_path.is_file())
        self.assertEqual(json.loads(self.manager.manifest_path.read_text(encoding="utf-8"))["applications"][0]["app_key"], startup.APP_KEY)
        self.assertFalse(self.api.enabled)

    def test_failure_to_read_back_enable_is_not_reported_as_success(self):
        self.api.getApplicationAutoLaunch = lambda _: False
        with patch.object(startup.sys, "platform", "win32"), patch.object(startup, "applications", self.session), patch.object(Path, "is_file", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "未确认开启"):
                self.manager.set_enabled(True)
        self.assertFalse(self.api.installed)

    def test_missing_launch_files_does_not_change_registration(self):
        with patch.object(startup.sys, "platform", "win32"), patch.object(startup, "applications", self.session), patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "找不到"):
                self.manager.set_enabled(True)
        self.assertEqual(self.api.calls, [])

    def test_frozen_launch_targets_executable_and_startup_flag(self):
        with patch.object(startup.sys, "platform", "win32"), patch.object(startup.sys, "frozen", True, create=True), patch.object(startup, "applications", self.session), patch.object(Path, "is_file", return_value=True):
            self.manager.set_enabled(True)
        manifest = json.loads(self.manager.manifest_path.read_text(encoding="utf-8"))
        script = base64.b64decode(manifest["applications"][0]["arguments"].split()[-1]).decode("utf-16le")
        self.assertIn(str(Path(startup.sys.executable).resolve()), script)
        self.assertNotIn("dance_cache_gui.py", script)
        self.assertIn("--state-dir", script)
        self.assertIn(str(Path(self.directory.name).resolve()), script)

    def test_openvr_session_does_not_initialize_headset_system_and_always_shuts_down(self):
        module = types.SimpleNamespace(VRApplication_Background=3, initInternal2=MagicMock(),
                                       IVRApplications=MagicMock(return_value=self.api), shutdownInternal=MagicMock())
        with patch.dict("sys.modules", {"openvr": module}):
            with self.assertRaisesRegex(RuntimeError, "先启动 SteamVR"):
                with startup.applications() as api:
                    self.assertIs(api, self.api)
                    raise RuntimeError("failure")
        module.initInternal2.assert_called_once_with(3, None)
        module.shutdownInternal.assert_called_once()

    def test_openvr_init_failure_does_not_claim_known_disabled_state(self):
        module = types.SimpleNamespace(VRApplication_Background=3, initInternal2=MagicMock(side_effect=RuntimeError("not running")), shutdownInternal=MagicMock())
        with patch.dict("sys.modules", {"openvr": module}):
            with self.assertRaisesRegex(RuntimeError, "先启动 SteamVR"):
                with startup.applications():
                    self.fail("Failed initialization must not enter session")
        # init 失败且无 overlay 会话可复用时，必须 shutdownInternal 清掉进程内残留状态
        module.shutdownInternal.assert_called_once()


class SingleInstanceTests(unittest.TestCase):
    def test_first_instance_holds_handle_until_close(self):
        kernel = MagicMock()
        kernel.CreateMutexW.return_value = 123
        kernel.CreateEventW.return_value = 456
        with patch.object(single_instance.ctypes, "WinDLL", return_value=kernel, create=True), patch.object(single_instance.ctypes, "get_last_error", return_value=0, create=True):
            lease = single_instance.claim_instance()
        kernel.CloseHandle.assert_not_called()
        lease.close()
        lease.close()
        self.assertEqual(kernel.CloseHandle.call_count, 2)
        kernel.CloseHandle.assert_any_call(123)
        kernel.CloseHandle.assert_any_call(456)

    def test_duplicate_closes_temporary_handle(self):
        kernel = MagicMock()
        kernel.CreateMutexW.return_value = 123
        kernel.CreateEventW.return_value = 456
        with patch.object(single_instance.ctypes, "WinDLL", return_value=kernel, create=True), patch.object(single_instance.ctypes, "get_last_error", return_value=183, create=True):
            self.assertIsNone(single_instance.claim_instance())
        kernel.SetEvent.assert_called_once_with(456)
        self.assertEqual(kernel.CloseHandle.call_count, 2)
        kernel.CloseHandle.assert_any_call(123)
        kernel.CloseHandle.assert_any_call(456)
