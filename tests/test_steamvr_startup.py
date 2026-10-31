"""No SteamVR, Windows processes, registration, or elevation are used here."""
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
    """按 app_key 分别记录安装/自启状态（旧版清理只应影响旧版 key）"""

    def __init__(self):
        self._installed = set()
        self._enabled = set()
        self.calls = []
        self.fail_enable = False

    @property
    def installed(self):
        return startup.APP_KEY in self._installed

    @installed.setter
    def installed(self, value):
        (self._installed.add if value else self._installed.discard)(startup.APP_KEY)

    @property
    def enabled(self):
        return startup.APP_KEY in self._enabled

    @enabled.setter
    def enabled(self, value):
        (self._enabled.add if value else self._enabled.discard)(startup.APP_KEY)

    def install_legacy(self):
        self._installed.add(startup.LEGACY_APP_KEY)
        self._enabled.add(startup.LEGACY_APP_KEY)

    @property
    def legacy_installed(self):
        return startup.LEGACY_APP_KEY in self._installed

    def isApplicationInstalled(self, key):
        return key in self._installed

    def getApplicationAutoLaunch(self, key):
        return key in self._enabled

    def addApplicationManifest(self, path, temporary):
        self.calls.append(("add", path, temporary))
        self._installed.add(startup.APP_KEY)

    def removeApplicationManifest(self, path):
        self.calls.append(("remove", path))
        if "pypydance" in str(path).lower():
            self._installed.discard(startup.LEGACY_APP_KEY)
            self._enabled.discard(startup.LEGACY_APP_KEY)
        else:
            self._installed.discard(startup.APP_KEY)
            self._enabled.discard(startup.APP_KEY)

    def setApplicationAutoLaunch(self, key, enabled):
        self.calls.append(("set", enabled))
        if self.fail_enable and enabled:
            raise RuntimeError("registration refused")
        (self._enabled.add if enabled else self._enabled.discard)(key)


class SteamVRStartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = startup.SteamVRStartup(self.directory.name)
        self.api = FakeApplications()
        # 测试环境绝不能真碰 Windows 计划任务
        self.ensure_task = patch.object(startup, "ensure_scheduled_task").start()
        self.remove_task = patch.object(startup, "remove_scheduled_task").start()
        self.addCleanup(patch.stopall)

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

    def test_status_removes_legacy_registration_without_touching_own_state(self):
        # 旧版 PyPyDanceCache 的自启残留应被自动注销，且不影响本应用的注册状态
        startup._legacy_cleanup_done = False
        self.api.install_legacy()
        self.api.installed = True
        self.api.enabled = True
        logs = []
        with patch.object(startup.sys, "platform", "win32"), patch.object(startup, "applications", self.session), \
                patch.object(startup, "_legacy_manifest_paths", return_value=[Path("pypydance.vrmanifest")]), \
                patch.object(Path, "is_file", return_value=True):
            result = self.manager.status()
        startup._legacy_cleanup_done = False
        self.assertTrue(result["enabled"])  # 本应用状态不受影响
        self.assertFalse(self.api.legacy_installed)
        self.assertNotIn(startup.LEGACY_APP_KEY, self.api._enabled)

    def test_status_without_legacy_registration_makes_no_cleanup_calls(self):
        startup._legacy_cleanup_done = False
        with patch.object(startup.sys, "platform", "win32"), patch.object(startup, "applications", self.session), \
                patch.object(Path, "is_file", return_value=True):
            self.manager.status()
        startup._legacy_cleanup_done = False
        self.assertEqual([c for c in self.api.calls if c[0] == "remove"], [])

    def test_manifest_launches_scheduled_task_without_elevation_prompt(self):
        # manifest 只触发计划任务，不再嵌 exe 路径/powershell 脚本，也绝不弹 UAC
        manifest = startup.launch_manifest()
        app = manifest["applications"][0]
        self.assertTrue(app["is_dashboard_overlay"])
        self.assertEqual(app["launch_type"], "binary")
        self.assertTrue(app["binary_path_windows"].lower().endswith("schtasks.exe"))
        self.assertEqual(app["arguments"], f"/Run /TN {startup.TASK_NAME}")
        self.assertNotIn("RunAs", json.dumps(manifest, ensure_ascii=False))

    def test_task_xml_carries_unicode_paths_and_highest_run_level(self):
        xml = startup._task_xml("C:\\小余's files\\app.exe", ["--steamvr-startup", "--state-dir", "D:\\缓存目录"])
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", xml)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", xml)
        self.assertIn("小余's files", xml)
        self.assertIn("--steamvr-startup", xml)
        self.assertIn("缓存目录", xml)
        self.assertIn("IgnoreNew", xml)
        # 无触发器：只能被 schtasks /Run 拉起，不会在午夜等时刻意外启动
        self.assertNotIn("<Triggers>", xml)

    def test_task_creation_failure_aborts_before_registration(self):
        self.ensure_task.side_effect = RuntimeError("拒绝访问")
        with patch.object(startup.sys, "platform", "win32"), patch.object(startup, "applications", self.session), patch.object(Path, "is_file", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "保存失败"):
                self.manager.set_enabled(True)
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.manager.manifest_path.exists())
        self.assertFalse(self.api.installed)

    def test_failure_rolls_back_new_registration_and_manifest(self):
        self.api.fail_enable = True
        with patch.object(startup.sys, "platform", "win32"), patch.object(startup, "applications", self.session), patch.object(Path, "is_file", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "保存失败"):
                self.manager.set_enabled(True)
        self.assertFalse(self.api.installed)
        self.assertFalse(self.api.enabled)
        self.assertFalse(self.manager.manifest_path.exists())
        self.assertTrue(any(call[0] == "remove" for call in self.api.calls))
        # 注册没成功，刚建的计划任务也要回滚删除
        self.remove_task.assert_called()

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
        self.assertTrue(manifest["applications"][0]["binary_path_windows"].lower().endswith("schtasks.exe"))
        # 真正的启动目标（exe 路径 + 参数）在计划任务里
        executable, arguments = self.ensure_task.call_args[0][:2]
        self.assertEqual(Path(executable), Path(startup.sys.executable).resolve())
        self.assertNotIn("dance_cache_gui.py", " ".join(arguments))
        self.assertIn("--steamvr-startup", arguments)
        self.assertIn(str(Path(self.directory.name).resolve()), arguments)

    def test_openvr_session_does_not_initialize_headset_system_and_always_shuts_down(self):
        module = types.SimpleNamespace(VRApplication_Background=3, initInternal2=MagicMock(),
                                       IVRApplications=MagicMock(return_value=self.api), shutdownInternal=MagicMock())
        # 就绪探针走子进程，测试里 mock 成就绪
        with patch.dict("sys.modules", {"openvr": module}), patch("dance_ui.openvr_probe.probe_steamvr_ready", return_value=(True, None)):
            with self.assertRaisesRegex(RuntimeError, "先启动 SteamVR"):
                with startup.applications() as api:
                    self.assertIs(api, self.api)
                    raise RuntimeError("failure")
        module.initInternal2.assert_called_once_with(3, None)
        module.shutdownInternal.assert_called_once()

    def test_openvr_init_failure_does_not_claim_known_disabled_state(self):
        module = types.SimpleNamespace(VRApplication_Background=3, initInternal2=MagicMock(side_effect=RuntimeError("not running")), shutdownInternal=MagicMock())
        with patch.dict("sys.modules", {"openvr": module}), patch("dance_ui.openvr_probe.probe_steamvr_ready", return_value=(True, None)):
            with self.assertRaisesRegex(RuntimeError, "先启动 SteamVR"):
                with startup.applications():
                    self.fail("Failed initialization must not enter session")
        # init 失败且无 overlay 会话可复用时，必须 shutdownInternal 清掉进程内残留状态
        module.shutdownInternal.assert_called_once()

    def test_probe_not_ready_never_touches_main_process_init(self):
        # 探针报告 vrserver 未就绪时必须直接报错，绝不在主进程 init
        # （失败 init 会毒化进程内 vrclient 状态，之后永远重放同一错误）
        module = types.SimpleNamespace(VRApplication_Background=3, initInternal2=MagicMock(), shutdownInternal=MagicMock())
        with patch.dict("sys.modules", {"openvr": module}), patch("dance_ui.openvr_probe.probe_steamvr_ready", return_value=(False, "InitError_Init_HmdNotFound")):
            with self.assertRaisesRegex(RuntimeError, "InitError_Init_HmdNotFound"):
                with startup.applications():
                    self.fail("Probe not ready must not enter session")
        module.initInternal2.assert_not_called()


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
