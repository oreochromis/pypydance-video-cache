"""SteamVR companion registration through the public OpenVR applications API.

The background connection never starts SteamVR. All calls are short lived;
SteamVR owns the autolaunch flag rather than a second, potentially stale config.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

APP_KEY = "sharkspeare.vrchatdanceassistant"
MUTEX_NAME = r"Local\VRChatDanceAssistant.UI"
_runtime_lock = threading.Lock()


def _quote_ps(value):
    return "'" + str(value).replace("'", "''") + "'"


def launch_manifest(executable, arguments, working_directory, powershell):
    """Use ShellExecute/runas: CreateProcess cannot launch our elevated exe.

    EncodedCommand carries Unicode paths without shell interpolation. Check the
    instance before asking for elevation so SteamVR restarting is unobtrusive.
    """
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "try { $m = [System.Threading.Mutex]::OpenExisting("
        + _quote_ps(MUTEX_NAME)
        + ", [System.Security.AccessControl.MutexRights]::Synchronize); "
        "$m.Dispose(); exit 0 } "
        "catch [System.Threading.WaitHandleCannotBeOpenedException] {} "
        "Start-Process -FilePath " + _quote_ps(executable)
        + " -ArgumentList " + _quote_ps(subprocess.list2cmdline(arguments))
        + " -WorkingDirectory " + _quote_ps(working_directory)
        + " -Verb RunAs"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return {"source": "VRChat Dance Assistant", "applications": [{
        "app_key": APP_KEY,
        "launch_type": "binary",
        # OpenVR requires this property for SetApplicationAutoLaunch. This
        # companion has no scene or rendered dashboard and takes no VR focus.
        "is_dashboard_overlay": True,
        "binary_path_windows": str(powershell),
        "arguments": "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand " + encoded,
        "working_directory": str(working_directory),
        "strings": {"en_us": {"name": "VRChat Dance Assistant", "description": "VRChat dance room video cache companion"}},
    }]}


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


@contextmanager
def applications():
    # Importing/loading the native library is deliberately Windows-only and
    # lazy: demo and macOS never load OpenVR or connect to a headset runtime.
    try:
        import openvr
    except (ImportError, OSError) as exc:
        raise RuntimeError("缺少 OpenVR 运行依赖，请安装完整 Windows 程序包。") from exc
    with _runtime_lock:
        initialized = False
        try:
            # Background explicitly does not start SteamVR if it isn't running.
            # Avoid openvr.init(): it also asks for IVRSystem, which is unneeded.
            openvr.initInternal2(openvr.VRApplication_Background, None)
            initialized = True
            yield openvr.IVRApplications()
        except Exception as exc:
            raise RuntimeError(f"无法访问 SteamVR 启动设置，请先启动 SteamVR 后重试：{exc}") from exc
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

    def status(self):
        if sys.platform != "win32":
            return {"enabled": False, "available": False, "message": "跟随 SteamVR 启动仅支持 Windows。"}
        with applications() as api:
            return self._result(api.isApplicationInstalled(APP_KEY) and api.getApplicationAutoLaunch(APP_KEY))

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
                return self._result(False)

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
            powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            if not executable.is_file() or not powershell.is_file():
                raise RuntimeError("找不到启动程序或 Windows PowerShell，未更改 SteamVR 启动设置。")
            previous_manifest = self.manifest_path.read_bytes() if self.manifest_path.exists() else None
            content = json.dumps(launch_manifest(executable, arguments, directory, powershell),
                                 ensure_ascii=False, indent=2).encode("utf-8")
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
                detail = "；恢复未完成，请刷新状态检查：" + "; ".join(rollback_errors) if rollback_errors else ""
                raise RuntimeError(f"SteamVR 启动设置保存失败：{exc}{detail}") from exc
            return self._result(True)
