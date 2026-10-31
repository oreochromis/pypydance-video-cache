"""SteamVR companion registration through the public OpenVR applications API.

The background connection never starts SteamVR. All calls are short lived;
SteamVR owns the autolaunch flag rather than a second, potentially stale config.

自动启动走「最高权限计划任务」：开启自启时（本程序已是管理员）注册一个无
触发器的计划任务，manifest 里让 SteamVR 拉起 schtasks /Run 触发它——
全程不弹 UAC（直接 runas 会在每次 SteamVR 启动时弹桌面确认框，VR 里看不见，
等于自启失效）。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from xml.sax.saxutils import escape as _xml_escape

APP_KEY = "sharkspeare.vrchatdanceassistant"
TASK_NAME = "VRChatDanceAssistant-SteamVR"
MUTEX_NAME = r"Local\VRChatDanceAssistant.UI"
_runtime_lock = threading.Lock()

# 旧版 PyPyDanceCache 的 SteamVR 应用 key；用户从旧版迁移过来后，旧注册会让
# SteamVR 每次启动都拉起旧程序（抢 hosts/端口，且用户以为新版自启失效）
LEGACY_APP_KEY = "xiaoyu12cpu.pypydancecache"
_legacy_cleanup_done = False  # 进程内只尝试一次


def _schtasks_path():
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe"


def launch_manifest():
    """SteamVR 只负责触发计划任务；任务本身以最高权限运行，无需 UAC。

    重复拉起由应用内单实例锁兜底（新实例唤醒已有窗口后退出）。"""
    schtasks = _schtasks_path()
    return {"source": "VRChat Dance Assistant", "applications": [{
        "app_key": APP_KEY,
        "launch_type": "binary",
        # OpenVR requires this property for SetApplicationAutoLaunch. This
        # companion has no scene or rendered dashboard and takes no VR focus.
        "is_dashboard_overlay": True,
        "binary_path_windows": str(schtasks),
        "arguments": f"/Run /TN {TASK_NAME}",
        "working_directory": str(schtasks.parent),
        "strings": {"en_us": {"name": "VRChat Dance Assistant", "description": "VRChat dance room video cache companion"}},
    }]}


def _task_xml(executable, arguments):
    """无触发器的任务：永不自动运行，只能被 schtasks /Run 手动拉起。

    HighestAvailable 让任务以本程序同等的提权身份运行；InteractiveToken
    使任务跟随当前登录用户，不需要保存密码。"""
    command = subprocess.list2cmdline([str(executable)])
    argument_line = subprocess.list2cmdline([str(a) for a in arguments])
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>Launch VRChat Dance Assistant when SteamVR starts (elevated, no UAC).</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>HighestAvailable</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>false</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{_xml_escape(command)}</Command>\n"
        f"      <Arguments>{_xml_escape(argument_line)}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _run_schtasks(*args):
    return subprocess.run(
        [str(_schtasks_path()), *args],
        capture_output=True, text=True, encoding="mbcs", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def ensure_scheduled_task(executable, arguments):
    """创建/覆盖自启计划任务（需管理员；本程序满足）。失败抛 RuntimeError。"""
    descriptor, temporary = tempfile.mkstemp(prefix=".vrchatdance-task-", suffix=".xml")
    try:
        # schtasks /xml 要求 UTF-16（Python 的 utf-16 编码带 BOM）
        with os.fdopen(descriptor, "w", encoding="utf-16") as handle:
            handle.write(_task_xml(executable, arguments))
        result = _run_schtasks("/Create", "/TN", TASK_NAME, "/XML", temporary, "/F")
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"注册计划任务失败：{detail or f'退出码 {result.returncode}'}")


def _scheduled_task_exists():
    try:
        return _run_schtasks("/Query", "/TN", TASK_NAME).returncode == 0
    except OSError:
        return False


def remove_scheduled_task():
    """删除自启计划任务；任务不存在或删除失败都静默（清理性质）。"""
    try:
        if _scheduled_task_exists():
            _run_schtasks("/Delete", "/TN", TASK_NAME, "/F")
    except OSError:
        pass


def _write_manifest(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".steamvr-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _legacy_manifest_paths():
    """旧版可能注册过的 manifest 路径：默认 state 目录 + SteamVR appconfig.json
    里实际登记的所有路径（覆盖旧版装在非默认位置的情况）。"""
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Xiaoyu12-CPU" / "PyPyDanceCache"
                          / "appearance" / "steamvr" / "pypydance.vrmanifest")
    try:
        vrpath = Path(local or "") / "openvr" / "openvrpaths.vrpath"
        registry = json.loads(vrpath.read_text(encoding="utf-8"))
        for config_dir in registry.get("config") or []:
            appconfig = Path(config_dir) / "appconfig.json"
            if not appconfig.is_file():
                continue
            data = json.loads(appconfig.read_text(encoding="utf-8"))
            for entry in data.get("manifest_paths") or []:
                candidates.append(Path(entry))
    except Exception:
        pass
    # 去重且只保留确实属于旧版的 manifest（按内容里的 app_key 判定，
    # 避免误删路径碰巧相似的其他应用）
    seen = set()
    result = []
    for path in candidates:
        try:
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            keys = [app.get("app_key") for app in data.get("applications", [])]
            if LEGACY_APP_KEY in keys:
                result.append(path)
        except (OSError, ValueError):
            # manifest 文件已丢失但注册可能残留：旧版文件名很特殊，按名兜底
            if path.name.lower() == "pypydance.vrmanifest":
                result.append(path)
    return result


def remove_legacy_registration(api, log=None):
    """注销旧版 PyPyDanceCache 的 SteamVR 自启动注册（幂等，每进程一次）。

    只处理内容确认为旧版 app_key 的 manifest；任何失败都静默——这属于
    迁移清理，绝不能影响本软件自身的状态读写。"""
    global _legacy_cleanup_done
    if _legacy_cleanup_done:
        return
    _legacy_cleanup_done = True
    try:
        if not api.isApplicationInstalled(LEGACY_APP_KEY):
            return
        api.setApplicationAutoLaunch(LEGACY_APP_KEY, False)
        for path in _legacy_manifest_paths():
            try:
                api.removeApplicationManifest(str(path))
            except Exception:
                pass
        message = "检测到旧版 PyPyDanceCache 的 SteamVR 自启动残留，已自动注销"
        if log:
            log(message)
        else:
            try:
                import dance_cache as core
                core.log(message)
            except Exception:
                pass
    except Exception:
        pass


@contextmanager
def applications():
    # Importing/loading the native library is deliberately Windows-only and
    # lazy: demo and macOS never load OpenVR or connect to a headset runtime.
    try:
        from .openvr_runtime import import_openvr
        openvr = import_openvr()
    except (ImportError, OSError) as exc:
        raise RuntimeError("缺少 OpenVR 运行依赖，请安装完整 Windows 程序包。") from exc
    with _runtime_lock:
        initialized = False
        # vrclient 会把进程内第一次 init 的失败状态静态缓存（shutdown 清不掉），
        # 一旦失败本进程所有后续 init 都只重放同一个错误——先用子进程探针确认
        # vrserver 在接待再 init。
        # 探针报错放在 try 外：消息已完整，不要被外层二次包装
        from .openvr_probe import probe_steamvr_ready
        ready, probe_error = probe_steamvr_ready()
        if not ready:
            from .openvr_runtime import describe_probe_error
            raise RuntimeError(
                f"无法访问 SteamVR 启动设置：{describe_probe_error(probe_error)}")
        try:
            try:
                # Background explicitly does not start SteamVR if it isn't running.
                # Avoid openvr.init(): it also asks for IVRSystem, which is unneeded.
                openvr.initInternal2(openvr.VRApplication_Background, None)
                initialized = True
            except Exception:
                try:
                    openvr.shutdownInternal()
                except Exception:
                    pass
                raise
            yield openvr.IVRApplications()
        except Exception as exc:
            # pyopenvr 的错误 str() 为空，用类名（即错误名）兜底
            detail = str(exc) or type(exc).__name__
            raise RuntimeError(f"无法访问 SteamVR 启动设置，请先启动 SteamVR 后重试：{detail}") from exc
        finally:
            if initialized:
                openvr.shutdownInternal()


class SteamVRStartup:
    def __init__(self, state_dir):
        self.manifest_path = Path(state_dir) / "steamvr" / "vrchatdance.vrmanifest"

    @staticmethod
    def _result(enabled):
        return {"enabled": bool(enabled), "available": True,
                "message": "已开启：SteamVR 下次启动时打开助手。" if enabled else "已关闭跟随 SteamVR 启动。"}

    def _launch_target(self):
        """自启时要启动的本程序（可执行文件、参数、工作目录）；程序缺失时返回 None。"""
        executable = Path(sys.executable).resolve()
        arguments = ["--steamvr-startup", "--state-dir", str(self.manifest_path.parent.parent.resolve())]
        if not getattr(sys, "frozen", False):
            script = Path(__file__).resolve().parents[1] / "dance_cache_gui.py"
            arguments.insert(0, str(script))
            directory = script.parent
            pythonw = executable.with_name("pythonw.exe")
            if pythonw.is_file():
                executable = pythonw
        else:
            directory = executable.parent
        if not executable.is_file():
            return None
        return executable, arguments, directory

    def _expected_manifest(self):
        """按当前程序位置生成的 manifest 内容；启动程序或 schtasks 缺失时返回 None。"""
        if self._launch_target() is None or not _schtasks_path().is_file():
            return None
        return json.dumps(launch_manifest(), ensure_ascii=False, indent=2).encode("utf-8")

    def status(self):
        if sys.platform != "win32":
            return {"enabled": False, "available": False, "message": "跟随 SteamVR 启动仅支持 Windows。"}
        with applications() as api:
            # 迁移清理：旧版自启残留会让 SteamVR 每次启动拉起旧程序
            remove_legacy_registration(api)
            enabled = api.isApplicationInstalled(APP_KEY) and api.getApplicationAutoLaunch(APP_KEY)
            result = self._result(enabled)
            if enabled:
                # 计划任务存的是 exe 绝对路径：程序搬家/换版本后任务会指向旧路径。
                # 每次开启状态检查都重写任务（约 100ms），保证路径始终最新；
                # 失败只记日志，不影响状态读取。
                target = self._launch_target()
                if target is not None:
                    try:
                        ensure_scheduled_task(target[0], target[1])
                    except Exception as exc:
                        try:
                            import dance_cache as core
                            core.log(f"SteamVR 自启动计划任务更新失败：{exc}")
                        except Exception:
                            pass
                # 程序搬家/改名/换版本后，SteamVR 仍按旧 manifest 的内容启动旧路径
                # （表现为自启失效或拉起旧版本）。磁盘上的 manifest 与本程序当前位置
                # 应生成的内容不一致时，自动重写并重新注册，不需要用户重开开关
                expected = self._expected_manifest()
                current = self.manifest_path.read_bytes() if self.manifest_path.exists() else None
                if expected is not None and current != expected:
                    _write_manifest(self.manifest_path, expected)
                    api.addApplicationManifest(str(self.manifest_path.resolve()), False)
                    api.setApplicationAutoLaunch(APP_KEY, True)
                    result["message"] += " 检测到程序位置变化，已自动更新启动路径。"
                    try:
                        import dance_cache as core
                        core.log("检测到程序位置变化，SteamVR 自启动路径已自动更新")
                    except Exception:
                        pass
            else:
                # 已关闭自启：清掉可能残留的计划任务（比如旧版本留下的）
                remove_scheduled_task()
            return result

    def set_enabled(self, enabled):
        if sys.platform != "win32":
            raise RuntimeError("跟随 SteamVR 启动仅支持 Windows。")
        if not isinstance(enabled, bool):
            raise ValueError("启动设置必须是布尔值。")
        with applications() as api:
            installed = api.isApplicationInstalled(APP_KEY)
            previous_enabled = bool(installed and api.getApplicationAutoLaunch(APP_KEY))
            if not enabled:
                if installed:
                    api.setApplicationAutoLaunch(APP_KEY, False)
                    if api.getApplicationAutoLaunch(APP_KEY):
                        raise RuntimeError("SteamVR 未确认关闭，请刷新状态后重试。")
                remove_scheduled_task()
                return self._result(False)

            # 先建计划任务：它是实际拉起本程序的执行体，建失败就整体中止，
            # 不注册 manifest（否则会留下一个永远拉不起程序的自启项）
            target = self._launch_target()
            content = self._expected_manifest()
            if target is None or content is None:
                raise RuntimeError("找不到启动程序或 schtasks.exe，未更改 SteamVR 启动设置。")
            try:
                ensure_scheduled_task(target[0], target[1])
            except Exception as exc:
                raise RuntimeError(f"SteamVR 启动设置保存失败：{exc}") from exc
            previous_manifest = self.manifest_path.read_bytes() if self.manifest_path.exists() else None
            registered = False
            try:
                _write_manifest(self.manifest_path, content)
                api.addApplicationManifest(str(self.manifest_path.resolve()), False)
                registered = True
                api.setApplicationAutoLaunch(APP_KEY, True)
                if not api.getApplicationAutoLaunch(APP_KEY):
                    raise RuntimeError("SteamVR 未确认开启，请重试。")
            except Exception as exc:
                # Restore the old manifest and runtime flag after partial failure.
                # Never write a local 'enabled' flag ahead of the runtime result.
                rollback_errors = []
                try:
                    if previous_manifest is None:
                        # An existing app key may have been registered from a
                        # different state directory. Keep this valid manifest
                        # after replacement; do not leave a dangling registry
                        # entry when restoring its old autolaunch flag.
                        if not installed or not registered:
                            self.manifest_path.unlink(missing_ok=True)
                    else:
                        _write_manifest(self.manifest_path, previous_manifest)
                except OSError as rollback:
                    rollback_errors.append(str(rollback))
                try:
                    if installed:
                        if registered and previous_manifest is not None:
                            api.addApplicationManifest(str(self.manifest_path.resolve()), False)
                        api.setApplicationAutoLaunch(APP_KEY, previous_enabled)
                    elif registered:
                        api.removeApplicationManifest(str(self.manifest_path.resolve()))
                except Exception as rollback:
                    rollback_errors.append(str(rollback))
                # 注册没成功，刚建的任务也不能留（避免残留任务静默拉起程序）
                if not (installed and previous_enabled):
                    try:
                        remove_scheduled_task()
                    except Exception as rollback:
                        rollback_errors.append(str(rollback))
                detail = "；恢复未完成，请刷新状态检查：" + "; ".join(rollback_errors) if rollback_errors else ""
                raise RuntimeError(f"SteamVR 启动设置保存失败：{exc}{detail}") from exc
            return self._result(True)
