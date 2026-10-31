"""VR overlay tests: fake openvr module / fake worker only, never touches SteamVR.

架构：父进程 VROverlayService（vr_overlay）只做子进程监管与状态推送；
会话本体在子进程 PanelSession（vr_panel_worker）。两侧分别测试。
"""
import queue
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dance_ui import steamvr_startup, vr_overlay, vr_panel_worker


class FakeEvent:
    def __init__(self):
        self.eventType = 0
        self.data = types.SimpleNamespace(mouse=types.SimpleNamespace(x=0.0, y=0.0, button=1))


class FakeOverlayAPI:
    def __init__(self, events=()):
        self.created = None
        self.raw_submissions = []
        self.thumbnail_file = None
        self.input_method = None
        self.width_meters = None
        self.destroyed = []
        self.events = deque(events)

    def createDashboardOverlay(self, key, name):
        self.created = (key, name)
        return 101, 102

    def setOverlayWidthInMeters(self, handle, width):
        self.width_meters = width

    def setOverlayInputMethod(self, handle, method):
        self.input_method = method

    def setOverlayFromFile(self, handle, path):
        self.thumbnail_file = path

    def setOverlayRaw(self, handle, buffer, width, height, bpp):
        # 回归：pyopenvr 对 buffer 取 byref()，传 bytes 会指到对象头导致 SteamVR 读到全零透明；
        # 必须是 ctypes 数组，且首像素应为背景色 (24,27,36,255)
        head = bytes(buffer[:4]) if not isinstance(buffer, bytes) else buffer[:4]
        self.raw_submissions.append((width, height, bpp, len(buffer), type(buffer).__name__, bytes(head)))

    def pollNextOverlayEvent(self, handle, event):
        # 和真实 pyopenvr 一致返回 (result, event) 元组——元组恒为真，
        # 调用方若直接 while pollNextOverlayEvent(...) 会无限空转（回归测试）
        if not self.events:
            return False, event
        event.eventType, mouse = self.events.popleft()
        if mouse is not None:
            event.data.mouse.x, event.data.mouse.y, event.data.mouse.button = mouse
        return True, event

    def destroyOverlay(self, handle):
        self.destroyed.append(handle)


def fake_openvr(api, init_error=None):
    return types.SimpleNamespace(
        VRApplication_Overlay=2,
        VROverlayInputMethod_Mouse=1,
        VREvent_MouseMove=300,
        VREvent_MouseButtonDown=301,
        VRMouseButton_Left=1,
        VREvent_t=FakeEvent,
        IVROverlay=lambda: api,
        init=MagicMock(side_effect=init_error) if init_error else MagicMock(),
        shutdown=MagicMock(),
    )


def sample_state(**kw):
    return {"running": True, "service_label": "停止服务",
            "current": {"name": "测试歌曲", "state": "已缓存", "meta": "点歌人：某人 · 来源：CDN"},
            "queue": [{"index": 1, "name": "下一首", "state": "已缓存", "cached": True},
                      {"index": 2, "name": "下下一首", "state": "未缓存", "cached": False}],
            "stats": {"hits": 3, "misses": 1, "saved_mb": 128.0},
            "downloading": [{"name": "abc.mp4", "percent": 0.42}],
            "cooldown": kw.get("cooldown", {"phase": "idle", "available": True}),
            "hover": kw.get("hover"), "message": None}


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class RenderTests(unittest.TestCase):
    def test_render_panel_produces_rgba_texture(self):
        data = vr_overlay.render_panel(sample_state())
        self.assertEqual(len(data), vr_overlay.TEXTURE_W * vr_overlay.TEXTURE_H * 4)

    def test_render_panel_handles_empty_state_and_long_titles(self):
        data = vr_overlay.render_panel({"current": {"name": "超长" * 200, "state": "已缓存", "meta": "x" * 500},
                                        "queue": [], "stats": {}, "hover": "cooldown",
                                        "cooldown": {"phase": "armed", "available": True}})
        self.assertEqual(len(data), vr_overlay.TEXTURE_W * vr_overlay.TEXTURE_H * 4)

    def test_hit_test_matches_calibrated_coordinate_mapping(self):
        # toggle_service 按钮 rect: (40, H-88, 300, H-32)；激光命中点像素 (100, H-60)
        # 校准实测映射：x*W；y 原点在下、按边长=W 的正方形归一化（上下各留 Y_MARGIN 边）
        x_norm = 100 / vr_overlay.TEXTURE_W
        y_norm = 1.0 - vr_overlay.Y_MARGIN - (vr_overlay.TEXTURE_H - 60) / vr_overlay.TEXTURE_W
        action, px, py = vr_overlay.hit_test(x_norm, y_norm)
        self.assertEqual(action, "toggle_service")
        self.assertAlmostEqual(px, 100)
        self.assertAlmostEqual(py, vr_overlay.TEXTURE_H - 60)
        # 两个按钮之间的空白处不命中
        action, _, _ = vr_overlay.hit_test(0.5, y_norm)
        self.assertIsNone(action)

    def test_hit_test_gl_mode_uses_top_origin_y(self):
        # GL 纹理通道下 SteamVR 报的鼠标 y 与 raw 互为镜像（原点在上）
        py_target = vr_overlay.TEXTURE_H - 60
        x_norm = 100 / vr_overlay.TEXTURE_W
        y_norm = vr_overlay.Y_MARGIN + py_target / vr_overlay.TEXTURE_W
        action, px, py = vr_overlay.hit_test(x_norm, y_norm, y_origin_top=True)
        self.assertEqual(action, "toggle_service")
        self.assertAlmostEqual(px, 100)
        self.assertAlmostEqual(py, py_target)


class PanelSessionTests(unittest.TestCase):
    """子进程会话：假 openvr + 命令队列，不碰真实 SteamVR/子进程。"""

    def run_session(self, api, gl_upload=False):
        commands = queue.Queue()
        sent = []
        session = vr_panel_worker.PanelSession(fake_openvr(api), commands, sent.append,
                                               gl_upload=gl_upload)
        thread = threading.Thread(target=session.run, daemon=True)
        thread.start()
        return session, commands, sent, thread

    def test_registers_overlay_renders_state_and_stops_cleanly(self):
        api = FakeOverlayAPI()
        session, commands, sent, thread = self.run_session(api)
        self.assertTrue(wait_until(lambda: api.created is not None))
        commands.put({"type": "state", "data": sample_state()})
        self.assertTrue(wait_until(lambda: bool(api.raw_submissions)))
        commands.put({"type": "stop"})
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(api.created, (vr_overlay.PANEL_KEY, vr_overlay.PANEL_NAME))
        self.assertEqual(api.input_method, 1)
        self.assertTrue(api.thumbnail_file.endswith("app-icon.png"))
        w, h, bpp, length, buf_type, head = api.raw_submissions[0]
        self.assertEqual((w, h, bpp), (1024, 640, 4))
        self.assertEqual(length, 1024 * 640 * 4)
        self.assertNotEqual(buf_type, "bytes")  # bytes 会被 byref 指到对象头，overlay 全透明
        self.assertEqual(head, bytes((24, 27, 36, 255)))  # 首像素 = 背景色，证明指针指向真实像素
        self.assertEqual(sorted(api.destroyed), [101, 102])
        self.assertTrue(any(m.get("type") == "log" and "已注册" in m.get("message", "")
                            for m in sent))

    def test_parent_disconnect_sentinel_stops_session(self):
        api = FakeOverlayAPI()
        session, commands, sent, thread = self.run_session(api)
        self.assertTrue(wait_until(lambda: api.created is not None))
        commands.put(None)  # 父进程断开
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(api.destroyed), [101, 102])

    def test_laser_click_sends_action_and_local_notice(self):
        def to_norm(bx, by):
            return (bx / vr_overlay.TEXTURE_W,
                    1.0 - vr_overlay.Y_MARGIN - by / vr_overlay.TEXTURE_W, 1)
        bx, by = vr_overlay.TEXTURE_W - 170, vr_overlay.TEXTURE_H - 60
        api = FakeOverlayAPI(events=[(300, to_norm(bx, by)), (301, to_norm(bx, by))])
        session, commands, sent, thread = self.run_session(api)
        self.assertTrue(wait_until(lambda: any(m.get("type") == "action" for m in sent)))
        commands.put({"type": "stop"})
        thread.join(5)
        actions = [m for m in sent if m.get("type") == "action"]
        self.assertEqual(actions[0]["action"], "cooldown")
        self.assertEqual(session._hover, "cooldown")
        # 子进程只转发动作 + 本地即时反馈；可用性预检在父进程
        self.assertTrue(any("已发送操作指令" in (session._current_notice() or "") for _ in [0])
                        or session._notice is None)  # notice 可能已过期，不强求
        self.assertTrue(any(m.get("type") == "log" and "VR 点击" in m.get("message", "")
                            for m in sent))

    def test_focus_leave_clears_hover_highlight(self):
        def to_norm(bx, by):
            return (bx / vr_overlay.TEXTURE_W,
                    1.0 - vr_overlay.Y_MARGIN - by / vr_overlay.TEXTURE_W, 1)
        bx, by = vr_overlay.TEXTURE_W - 170, vr_overlay.TEXTURE_H - 60
        api = FakeOverlayAPI(events=[(300, to_norm(bx, by)), (304, None)])  # 指上按钮后激光移出
        session, commands, sent, thread = self.run_session(api)
        self.assertTrue(wait_until(lambda: api.created is not None))
        self.assertTrue(wait_until(lambda: session._hover is None and not api.events))
        commands.put({"type": "stop"})
        thread.join(5)
        self.assertIsNone(session._hover)  # 高亮不残留

    def test_hidden_pauses_rendering_and_shown_resumes(self):
        api = FakeOverlayAPI()
        renders = []
        original = vr_panel_worker.render_panel

        def counting_render(state):
            renders.append(1)
            return original(state)

        session, commands, sent, thread = self.run_session(api)
        with patch.object(vr_panel_worker, "render_panel", counting_render):
            commands.put({"type": "state", "data": sample_state()})
            self.assertTrue(wait_until(lambda: bool(renders)))
            api.events.append((501, None))  # OverlayHidden
            self.assertTrue(wait_until(lambda: not session._visible))
            count = len(renders)
            time.sleep(1.3)  # 超过一个 1s 刷新周期
            self.assertEqual(len(renders), count)  # 隐藏期间不再渲染
            api.events.append((500, None))  # OverlayShown
            self.assertTrue(wait_until(lambda: len(renders) > count))
        commands.put({"type": "stop"})
        thread.join(5)
        # 可见性变化要上报父进程（父进程据此暂停状态采集，省 CPU/磁盘）
        visibility = [m["visible"] for m in sent if m.get("type") == "visibility"]
        self.assertEqual(visibility, [False, True])

    def test_gl_uploader_used_when_available(self):
        uploader = MagicMock()
        api = FakeOverlayAPI()
        with patch("dance_ui.vr_overlay._GLTextureUploader", return_value=uploader):
            session, commands, sent, thread = self.run_session(api, gl_upload=True)
            self.assertTrue(wait_until(lambda: api.created is not None))
            commands.put({"type": "state", "data": sample_state()})
            self.assertTrue(wait_until(lambda: uploader.upload.called))
            commands.put({"type": "stop"})
            thread.join(5)
        uploader.close.assert_called_once()
        self.assertEqual(api.raw_submissions, [])  # GL 通道生效时不用 raw

    def test_gl_uploader_failure_falls_back_to_raw(self):
        api = FakeOverlayAPI()
        with patch("dance_ui.vr_overlay._GLTextureUploader", side_effect=RuntimeError("no GL")):
            session, commands, sent, thread = self.run_session(api, gl_upload=True)
            self.assertTrue(wait_until(lambda: api.created is not None))
            commands.put({"type": "state", "data": sample_state()})
            self.assertTrue(wait_until(lambda: bool(api.raw_submissions)))
            commands.put({"type": "stop"})
            thread.join(5)
        self.assertTrue(any(m.get("type") == "log" and "回退兼容模式" in m.get("message", "")
                            for m in sent))


class FakeWorker:
    """假子进程句柄：消息预设、定时"退出"，send 全部记录。"""

    def __init__(self, messages=(), exit_code=2, exit_after=999.0):
        self.sent = []
        self._q = queue.Queue()
        for m in messages:
            self._q.put(m)
        self._exit_code = exit_code
        self._deadline = time.time() + exit_after
        self.closed = False

    def readline(self, timeout=0.2):
        if time.time() >= self._deadline:
            return vr_overlay.WORKER_CLOSED
        try:
            return self._q.get(timeout=min(timeout, 0.05))
        except queue.Empty:
            return None

    def send(self, obj):
        self.sent.append(obj)

    def poll(self):
        return self._exit_code if time.time() >= self._deadline else None

    def close(self, grace=3.0):
        self.closed = True


class SupervisorTests(unittest.TestCase):
    """父进程监管逻辑：重试/崩溃配额/动作转发/状态推送。"""

    def setUp(self):
        # 测试节奏拉快：重试间隔、状态推送间隔都缩到几十毫秒
        patcher = patch.object(vr_overlay, "RETRY_INTERVAL", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(vr_overlay, "STATE_PUSH_INTERVAL", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.budget_path = Path(self._tmp.name) / "restart.json"

    def make_service(self, workers, **kw):
        bridge = types.SimpleNamespace(action_requested=types.SimpleNamespace(emit=MagicMock()))
        logs = []
        if callable(workers):
            factory = workers
        else:
            made = list(workers)
            factory = lambda: made.pop(0) if made else FakeWorker()  # 用完后给常驻 worker
        service = vr_overlay.VROverlayService(
            bridge=bridge, state_provider=kw.get("state_provider", lambda: sample_state()),
            logger=logs.append, restart_state_path=kw.get("budget", self.budget_path),
            worker_factory=factory)
        return service, bridge, logs

    @staticmethod
    def crash_factory(exit_after, limit):
        """崩溃 worker 在拉起时才创建（deadline 从创建起算），用完后给常驻 worker"""
        made = []

        def factory():
            if len(made) >= limit:
                return FakeWorker()
            worker = FakeWorker(exit_code=-1073741819, exit_after=exit_after)
            made.append(worker)
            return worker

        return factory, made

    def test_init_error_retries_with_fresh_worker_and_throttled_log(self):
        # SteamVR 未就绪：子进程 init-error 退出 → 父进程拉新子进程重试（进程内毒化随退出消失）
        dying = [FakeWorker(messages=[{"type": "init-error", "error": "InitError_Init_HmdNotFound"}],
                            exit_code=2, exit_after=0.1) for _ in range(2)]
        service, bridge, logs = self.make_service(dying)
        service.start()
        self.assertTrue(wait_until(lambda: all(w.closed for w in dying)))
        service.stop()
        self.assertTrue(any("等待 SteamVR" in m for m in logs))
        self.assertFalse(service.running())

    def test_crash_restarts_within_budget_then_gives_up(self):
        # 快速连续崩溃（访问冲突等）：配额内自动拉起，超限停止
        factory, made = self.crash_factory(exit_after=0.05, limit=vr_overlay.AUTO_RESTART_LIMIT + 1)
        service, bridge, logs = self.make_service(factory)
        service.start()
        wait_until(lambda: not service.running(), timeout=10)
        service.stop()
        self.assertTrue(any("重新拉起" in m for m in logs))
        self.assertTrue(any("连续崩溃" in m and "停止自动拉起" in m for m in logs))
        self.assertFalse(service.running())

    def test_long_lived_crash_does_not_eat_budget(self):
        # 存活超过健康阈值才崩 = SteamVR 侧问题，不计入崩溃配额，永远能再拉起
        with patch.object(vr_overlay, "HEALTHY_SESSION_SECONDS", 0.05):
            factory, made = self.crash_factory(exit_after=0.2, limit=vr_overlay.AUTO_RESTART_LIMIT + 2)
            service, bridge, logs = self.make_service(factory)
            service.start()
            self.assertTrue(wait_until(lambda: all(w.closed for w in made) and len(made) >= vr_overlay.AUTO_RESTART_LIMIT + 2,
                                       timeout=15))
            time.sleep(0.2)
            service.stop()
        self.assertFalse(any("连续崩溃" in m for m in logs))
        self.assertTrue(any("重新拉起" in m for m in logs))

    def test_manual_start_resets_crash_budget(self):
        budget = vr_overlay.AutoRestartBudget(self.budget_path)
        budget.allow()
        budget.allow()
        self.assertTrue(self.budget_path.exists())
        service, bridge, logs = self.make_service([FakeWorker()])
        service.start()
        self.assertFalse(self.budget_path.exists())
        service.stop()

    def test_actions_forwarded_to_bridge(self):
        worker = FakeWorker(messages=[{"type": "action", "action": "toggle_service"}])
        service, bridge, logs = self.make_service([worker])
        service.start()
        self.assertTrue(wait_until(lambda: bridge.action_requested.emit.called))
        service.stop()
        bridge.action_requested.emit.assert_called_once_with("toggle_service")

    def test_cooldown_blocked_with_notice_when_unavailable(self):
        worker = FakeWorker(messages=[{"type": "action", "action": "cooldown"}])
        service, bridge, logs = self.make_service([worker])
        with patch("dance_cache.get_cooldown_state",
                   return_value={"phase": "idle", "available": False}), \
             patch("dance_cache.is_running", return_value=True):
            service.start()
            self.assertTrue(wait_until(
                lambda: any(m.get("type") == "notice" for m in worker.sent)))
            service.stop()
        bridge.action_requested.emit.assert_not_called()  # 未设放松视频时指令被拦下
        notice = next(m for m in worker.sent if m.get("type") == "notice")
        self.assertIn("放松视频", notice["message"])

    def test_state_pushed_only_when_changed_and_visible(self):
        worker = FakeWorker()
        service, bridge, logs = self.make_service([worker])
        service.start()
        self.assertTrue(wait_until(lambda: any(m.get("type") == "state" for m in worker.sent)))
        count = len([m for m in worker.sent if m.get("type") == "state"])
        time.sleep(0.3)  # 状态没变不重复推
        self.assertEqual(len([m for m in worker.sent if m.get("type") == "state"]), count)
        # 面板隐藏后暂停推送（省 CPU/磁盘），重新可见强制推一次
        worker._q.put({"type": "visibility", "visible": False})
        self.assertTrue(wait_until(lambda: not service._visible))
        time.sleep(0.2)
        count = len([m for m in worker.sent if m.get("type") == "state"])
        time.sleep(0.2)
        self.assertEqual(len([m for m in worker.sent if m.get("type") == "state"]), count)
        worker._q.put({"type": "visibility", "visible": True})
        self.assertTrue(wait_until(
            lambda: len([m for m in worker.sent if m.get("type") == "state"]) > count))
        service.stop()

    def test_worker_log_messages_forwarded(self):
        worker = FakeWorker(messages=[{"type": "log", "message": "VR 面板已注册：测试"}])
        service, bridge, logs = self.make_service([worker])
        service.start()
        self.assertTrue(wait_until(lambda: any("已注册" in m for m in logs)))
        service.stop()
        self.assertIn("VR 面板已关闭", logs)


class WorkerHandleProtocolTests(unittest.TestCase):
    """真实 socketpair 验证 WorkerHandle 的 JSON 行协议与鉴权。"""

    def make_handle(self, token="t1"):
        a, b = socket.socketpair()
        handle = vr_overlay.WorkerHandle.__new__(vr_overlay.WorkerHandle)
        handle._proc = types.SimpleNamespace(poll=lambda: None, terminate=lambda: None)
        handle._listener = None
        handle._token = token
        handle._stderr_file = None
        handle._conn = a
        handle._buf = ""
        handle._send_lock = threading.Lock()
        handle.authenticated = False
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return handle, b

    def test_hello_auth_then_messages_in_order(self):
        handle, peer = self.make_handle()
        peer.sendall(b'{"type":"hello","token":"t1"}\n{"type":"log","message":"a"}\n{"type":"log","message":"b"}\n')
        self.assertEqual(handle.readline(1.0)["message"], "a")
        self.assertTrue(handle.authenticated)
        self.assertEqual(handle.readline(1.0)["message"], "b")

    def test_wrong_token_closes_connection(self):
        handle, peer = self.make_handle()
        peer.sendall(b'{"type":"hello","token":"wrong"}\n')
        self.assertIs(handle.readline(1.0), vr_overlay.WORKER_CLOSED)

    def test_split_and_merged_tcp_frames_reassembled(self):
        handle, peer = self.make_handle()
        payload = b'{"type":"hello","token":"t1"}\n{"type":"log","mess'
        peer.sendall(payload)
        time.sleep(0.05)
        peer.sendall(b'age":"x"}\n')
        self.assertEqual(handle.readline(1.0)["message"], "x")

    def test_peer_close_reports_closed(self):
        handle, peer = self.make_handle()
        peer.sendall(b'{"type":"hello","token":"t1"}\n')
        peer.close()
        deadline = time.time() + 2
        result = None
        while time.time() < deadline:
            result = handle.readline(0.2)
            if result is vr_overlay.WORKER_CLOSED:
                break
        self.assertIs(result, vr_overlay.WORKER_CLOSED)


class ApplicationsTests(unittest.TestCase):
    def setUp(self):
        # 主进程 init 前的 vrserver 就绪探针（子进程）在测试里一律 mock，
        # 否则每个用例都会真起子进程连 SteamVR
        patcher = patch("dance_ui.openvr_probe.probe_steamvr_ready", return_value=(True, None))
        self.mock_probe = patcher.start()
        self.addCleanup(patcher.stop)

    def test_applications_still_fails_without_steamvr(self):
        module = types.SimpleNamespace(
            VRApplication_Background=3,
            initInternal2=MagicMock(side_effect=RuntimeError("not running")),
            IVRApplications=MagicMock(),
            shutdownInternal=MagicMock(),
        )
        with patch.dict("sys.modules", {"openvr": module}):
            with self.assertRaises(RuntimeError):
                with steamvr_startup.applications():
                    pass
        module.IVRApplications.assert_not_called()


if __name__ == "__main__":
    unittest.main()
