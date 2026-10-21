"""Qt-safe bridge to the existing cache core; demo mode never starts services."""
from __future__ import annotations

import copy
import os
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot, QUrl
from PySide6.QtGui import QDesktopServices

import dance_cache as core


DEMO_QUEUE = [
    {"name": "恋爱循环", "player": "小林", "song_id": 3528, "filename": "demo-current.mp4", "source": "cdn", "state": "已缓存", "state_code": "cached", "storage": "library"},
    {"name": "极乐净土", "player": "Momo", "song_id": 1837, "filename": "demo-next.mp4", "source": "cdn", "state": "已缓存", "state_code": "cached", "storage": "cache"},
    {"name": "夜に駆ける", "player": "Akira", "song_id": 6421, "filename": "demo-download.mp4", "source": "cdn", "state": "下载中", "state_code": "downloading", "task_id": "demo-download", "storage": "cache"},
    {"name": "アイドル", "player": "Neko", "song_id": 5290, "filename": "demo-waiting.mp4", "source": "cdn", "state": "未缓存", "state_code": "not_cached", "storage": None},
    {"name": "YouTube 示例曲目", "player": "Sora", "song_id": 9720, "source": "youtube", "state": "等待播放", "state_code": "youtube", "storage": None},
]


class CoreController(QObject):
    event = Signal(str, dict)
    result = Signal(str, object)
    error = Signal(str, str)
    busy_changed = Signal(str, bool)
    _worker_finished = Signal(str)

    def __init__(self, *, demo=False, parent=None):
        super().__init__(parent)
        self.demo = demo
        self._workers: dict[str, threading.Thread] = {}
        self._closing = False
        self._worker_finished.connect(self._finished)
        self._demo_config = copy.deepcopy(core.DEFAULT_CONFIG)
        self._demo_config.update(
            cache_dir=r"C:\PyPyDance\cache",
            library_dir=r"D:\PyPyDance\Library",
            cdn_permanent=True,
        )
        self._demo_running = True
        self._demo_cooldown = "idle"
        self._demo_maintenance_kind = None
        self._demo_maintenance_timer = QTimer(self)
        self._demo_maintenance_timer.setSingleShot(True)
        self._demo_maintenance_timer.timeout.connect(self._finish_demo_maintenance)
        self._startup_events = []
        self._initializing = not demo
        self._maintenance_cancel = threading.Event()
        self._listener = self._receive
        if not demo:
            core.add_listener(self._listener)
            try:
                core.load_config()
                core.load_mapping()
                self._config_cache = core.get_config()
            except Exception:
                core.remove_listener(self._listener)
                raise
            finally:
                self._initializing = False

    def _receive(self, kind, data):
        snapshot = copy.deepcopy(data)
        if self._initializing:
            self._startup_events.append((kind, snapshot))
        else:
            self.event.emit(kind, snapshot)

    def config(self) -> dict:
        # UI reads never wait for a worker holding the core's configuration lock.
        return copy.deepcopy(self._demo_config if self.demo else self._config_cache)

    def busy(self, operation: str) -> bool:
        return operation in self._workers

    def _run(self, operation, function):
        if self._closing and operation != "shutdown":
            return
        if self.busy(operation):
            return
        self.busy_changed.emit(operation, True)

        def worker():
            try:
                value = function()
                self.result.emit(operation, value)
            except Exception as exc:
                self.error.emit(operation, str(exc) or type(exc).__name__)
            finally:
                self._worker_finished.emit(operation)

        thread = threading.Thread(target=worker, name=f"ui-{operation}", daemon=True)
        self._workers[operation] = thread
        thread.start()

    @Slot(str)
    def _finished(self, operation):
        self._workers.pop(operation, None)
        self.busy_changed.emit(operation, False)

    def publish_initial(self):
        if self.demo:
            self._publish_demo_state()
            self.event.emit("queue", {"items": copy.deepcopy(DEMO_QUEUE)})
            self.event.emit("stats", {"count": 28, "total_mb": 1260, "limit_mb": 2048,
                                    "lib_count": 486, "lib_mb": 37.8 * 1024, "lib_enabled": True})
            self.event.emit("download_start", {"filename": "demo-download.mp4", "task_id": "demo-download", "song_id": 6421, "source": "cdn", "storage": "cache"})
            self.event.emit("progress", {"filename": "demo-download.mp4", "task_id": "demo-download",
                                        "done": 96 * 1024**2, "total": 150 * 1024**2, "demo_speed": 8.2 * 1024**2})
            self.event.emit("session", {"hits": 12, "misses": 3, "saved_mb": 1540.0})
            self._publish_demo_cooldown()
            self.event.emit("log", {"msg": "界面演示模式：使用示例数据，不启动网络服务，也不修改系统或缓存文件。"})
            return
        for kind, data in self._startup_events:
            self.event.emit(kind, data)
        self._startup_events.clear()
        self.event.emit("state", core.get_runtime_state())
        self.event.emit("queue", {"items": core.get_queue_snapshot()})
        for task in core.get_downloads_snapshot():
            self.event.emit("download_start", task)
            self.event.emit("progress", task)
        self.event.emit("cooldown", core.get_cooldown_state())
        self.event.emit("session", core.get_session_stats())
        self.refresh_stats()

    def configure_steamvr(self, state_dir):
        from .steamvr_startup import SteamVRStartup
        self._steamvr_startup = SteamVRStartup(state_dir)
        self._demo_steamvr_enabled = False

    def steamvr_status(self):
        if self.demo:
            self.result.emit("steamvr_startup", {
                "enabled": self._demo_steamvr_enabled, "available": True,
                "message": "演示模式：启动选项仅本次演示有效，不注册 SteamVR。"})
        else:
            self._run("steamvr_startup", self._steamvr_startup.status)

    def set_steamvr_startup(self, enabled):
        if self.demo:
            self._demo_steamvr_enabled = bool(enabled)
            self.steamvr_status()
        else:
            self._run("steamvr_startup", lambda: self._steamvr_startup.set_enabled(enabled))

    def refresh_stats(self):
        if not self.demo:
            self._run("stats", core.emit_stats)

    def _publish_demo_state(self):
        self.event.emit("state", {"running": self._demo_running,
                                 "phase": "running" if self._demo_running else "stopped",
                                 "youtube_available": self._demo_running,
                                 "queue_watcher_available": self._demo_running,
                                 "warnings": [], "error": None})

    def start(self):
        if self.demo:
            self._demo_running = True
            self._publish_demo_state()
            return
        if os.name != "nt":
            self.error.emit("start", "缓存服务仅支持 Windows。当前环境可以查看界面；安全预览请使用 --demo。")
            return
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            self.error.emit("start", "缓存服务需要管理员权限。请关闭后右键程序，选择“以管理员身份运行”。")
            return
        if self.busy("stop") or self.busy("start"):
            return

        def start():
            if self._closing:
                return
            error = core.start_services()
            if error:
                raise RuntimeError(error)
        self._run("start", start)

    def stop(self):
        if self.demo:
            self._demo_running = False
            self._demo_cooldown = "idle"
            self._publish_demo_state()
            self._publish_demo_cooldown()
            self.event.emit("download_done", {"filename": "demo-download.mp4", "task_id": "demo-download",
                                              "ok": False, "cancelled": True, "error": None})
            return
        if not self.busy("stop"):
            self._run("stop", self._stop_checked)

    @staticmethod
    def _stop_checked():
        error = core.stop_services()
        if error:
            raise RuntimeError(str(error))
        state = core.get_runtime_state()
        if state.get("phase") == "failed" and state.get("error"):
            raise RuntimeError(state["error"])

    def apply_config(self, patch: dict, section: str):
        patch = dict(patch)
        operation = f"apply_{section}"
        if self.demo:
            # Validate only the numeric/IP fields relevant to a safe preview.
            try:
                if "max_cache_mb" in patch and int(patch["max_cache_mb"]) < 100:
                    raise ValueError("缓存上限至少为 100 MB。")
                if "preferred_cdn_ip" in patch and patch["preferred_cdn_ip"]:
                    import ipaddress
                    if not ipaddress.IPv4Address(patch["preferred_cdn_ip"]).is_global:
                        raise ValueError("优选 CDN IP 必须是有效的公网 IPv4 地址。")
                changed = [k for k, v in patch.items() if self._demo_config.get(k) != v]
                self._demo_config.update(patch)
                self.result.emit(operation, {"restart_required": False, "changed": changed})
                self._publish_demo_cooldown()
            except Exception as exc:
                self.error.emit(operation, str(exc))
            return
        def apply():
            result = core.update_config(patch)
            self._config_cache = core.get_config()
            self.event.emit("cooldown", core.get_cooldown_state())
            return result
        self._run(operation, apply)

    def clear_cache(self):
        if self.demo:
            self.result.emit("clear_cache", {"removed_count": 0, "removed_bytes": 0, "skipped_busy": 0,
                                           "failed_count": 0, "errors": [], "demo": True})
            return
        self._run("clear_cache", core.clear_cache)

    def maintenance(self, kind: str):
        if self.demo:
            self._demo_maintenance_kind = kind
            self.event.emit(f"{kind}_progress", {"done": 0, "failed": 0, "checked": 0, "total": 486, "bad": 0, "current": "示例曲库"})
            self._demo_maintenance_timer.start(500)
            return
        function = core.rebuild_mapping if kind == "rebuild" else core.verify_library
        request = threading.Event()
        self._maintenance_cancel = request

        def start():
            if request.is_set():
                self.event.emit(f"{kind}_done", {"cancelled": True, "total": 0})
                return
            if not function():
                raise RuntimeError("未能启动曲库维护。请确认服务正在运行、曲库目录可用，并等待上一项任务结束。")
            if request.is_set():
                core.cancel_library_download()
        self._run(kind, start)

    def cancel_maintenance(self):
        if self.demo:
            self._demo_maintenance_timer.stop()
            self._finish_demo_maintenance(cancelled=True)
            return
        self._maintenance_cancel.set()
        core.cancel_library_download()

    def _finish_demo_maintenance(self, cancelled=False):
        kind = self._demo_maintenance_kind
        self._demo_maintenance_kind = None
        if kind:
            self.event.emit(f"{kind}_done", {
                "done": 0 if cancelled else 486, "checked": 0 if cancelled else 486,
                "total": 486, "failed": 0, "bad": 0, "skipped": 0,
                "cancelled": cancelled, "demo": True,
            })

    def scan(self, *, full=False):
        if self.demo:
            self.event.emit("speedtest_done", {"results": [
                {"ip": "104.16.0.1", "mbps": 7.8, "error": None},
                {"ip": "104.17.0.1", "mbps": 5.2, "error": None},
            ], "demo": True})
            return
        self._run("scan", core.full_scan_ips if full else core.speed_test_ips)

    def check_updates(self):
        if self.demo:
            return
        self._run("update_check", core.fetch_latest_release_tag)

    def collect_diagnostics(self):
        if self.demo:
            return
        self._run("diagnostics", core.collect_diagnostics)

    def cooldown(self):
        if self.demo:
            if not self._demo_running:
                return
            if self._demo_cooldown == "idle" and not core.resolve_cooldown_file(self._demo_config.get("cooldown_file", "")):
                return
            self._demo_cooldown = "armed" if self._demo_cooldown == "idle" else "idle"
            self._publish_demo_cooldown()
            return
        def toggle():
            if not core.is_running():
                raise RuntimeError("请先启动缓存服务。")
            if core.get_cooldown_state()["phase"] != "idle":
                core.cooldown_disarm()
            elif not core.cooldown_arm():
                raise RuntimeError("请先在“设置”中选择可用的放松视频并应用更改。")
        self._run("cooldown", toggle)

    def _publish_demo_cooldown(self):
        self.event.emit("cooldown", {"phase": self._demo_cooldown,
                                    "available": core.resolve_cooldown_file(self._demo_config.get("cooldown_file", "")) is not None,
                                    "active": self._demo_cooldown != "idle", "serving_filename": None})

    def open_directory(self, path: str):
        if self.demo:
            self.error.emit("open_directory", "演示路径仅用于展示。正式模式下可以打开实际缓存或曲库目录。")
            return
        directory = Path(path)
        if not directory.is_dir():
            self.error.emit("open_directory", "该目录不存在，请检查已经保存的目录设置。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory.resolve()))):
            self.error.emit("open_directory", "无法打开目录，请在文件管理器中手动打开。")

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        if self.demo:
            self._demo_maintenance_timer.stop()
            self.result.emit("shutdown", None)
        else:
            pending_start = self._workers.get("start")

            def shutdown():
                self._stop_checked()
                if pending_start is not None:
                    # A start worker may have been scheduled just before close,
                    # without entering the core lifecycle lock yet.  Do not let
                    # that worker start a service after the window has exited.
                    pending_start.join()
                    self._stop_checked()
            self._run("shutdown", shutdown)

    def dispose(self):
        if not self.demo:
            core.remove_listener(self._listener)
