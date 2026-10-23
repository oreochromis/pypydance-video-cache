"""VR overlay skeleton tests: fake openvr module only, never touches SteamVR."""
import sys
import threading
import time
import types
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dance_ui import steamvr_startup, vr_overlay


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
            "clicks": kw.get("clicks", 0), "hover": kw.get("hover"), "message": None}


class RenderTests(unittest.TestCase):
    def test_render_panel_produces_rgba_texture(self):
        data = vr_overlay.render_panel(sample_state())
        self.assertEqual(len(data), vr_overlay.TEXTURE_W * vr_overlay.TEXTURE_H * 4)

    def test_render_panel_handles_empty_state_and_long_titles(self):
        data = vr_overlay.render_panel({"current": {"name": "超长" * 200, "state": "已缓存", "meta": "x" * 500},
                                        "queue": [], "stats": {}, "clicks": 0, "hover": "test_click"})
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


class ServiceTests(unittest.TestCase):
    def make_service(self, api, **init_kw):
        bridge = types.SimpleNamespace(action_requested=types.SimpleNamespace(emit=MagicMock()))
        logs = []
        service = vr_overlay.VROverlayService(
            bridge=bridge, state_provider=lambda **kw: sample_state(**kw), logger=logs.append,
            gl_upload=init_kw.get("gl_upload", False))  # 测试默认走 setOverlayRaw，GL 路径单独 mock
        return service, bridge, logs

    def test_session_registers_dashboard_overlay_and_stops_cleanly(self):
        api = FakeOverlayAPI()
        module = fake_openvr(api)
        service, bridge, logs = self.make_service(api)
        with patch.dict("sys.modules", {"openvr": module}):
            service.start()
            deadline = time.time() + 5
            while not api.raw_submissions and time.time() < deadline:
                time.sleep(0.02)
            service.stop()
        self.assertEqual(api.created, (vr_overlay.PANEL_KEY, vr_overlay.PANEL_NAME))
        self.assertEqual(api.input_method, 1)
        self.assertTrue(api.thumbnail_file.endswith("app-icon.png"))
        w, h, bpp, length, buf_type, head = api.raw_submissions[0]
        self.assertEqual((w, h, bpp), (1024, 640, 4))
        self.assertEqual(length, 1024 * 640 * 4)
        self.assertNotEqual(buf_type, "bytes")  # bytes 会被 byref 指到对象头，overlay 全透明
        self.assertEqual(head, bytes((24, 27, 36, 255)))  # 首像素 = 背景色，证明指针指向真实像素
        self.assertEqual(sorted(api.destroyed), [101, 102])
        module.shutdown.assert_called()
        self.assertFalse(service.running())

    def test_laser_events_update_hover_and_fire_actions(self):
        # 激光移到测试按钮中心（像素 (TEXTURE_W-170, H-60)）并按下左键
        def to_norm(bx, by):
            return (bx / vr_overlay.TEXTURE_W,
                    1.0 - vr_overlay.Y_MARGIN - by / vr_overlay.TEXTURE_W, 1)
        bx, by = vr_overlay.TEXTURE_W - 170, vr_overlay.TEXTURE_H - 60
        move = (300, to_norm(bx, by))
        click = (301, to_norm(bx, by))
        api = FakeOverlayAPI(events=[move, click])
        service, bridge, logs = self.make_service(api)
        with patch.dict("sys.modules", {"openvr": fake_openvr(api)}):
            service.start()
            deadline = time.time() + 5
            while service._clicks == 0 and time.time() < deadline:
                time.sleep(0.02)
            service.stop()
        self.assertEqual(service._clicks, 1)
        self.assertEqual(service._hover, "test_click")
        self.assertTrue(any("激光点击测试" in message for message in logs))

    def test_toggle_service_button_emits_bridge_action(self):
        x0, y0, x1, y1 = vr_overlay._button_rects()["toggle_service"]
        px, py = (x0 + x1) / 2, (y0 + y1) / 2
        click = (301, (px / vr_overlay.TEXTURE_W,
                       1.0 - vr_overlay.Y_MARGIN - py / vr_overlay.TEXTURE_W, 1))
        api = FakeOverlayAPI(events=[click])
        service, bridge, logs = self.make_service(api)
        with patch.dict("sys.modules", {"openvr": fake_openvr(api)}):
            service.start()
            deadline = time.time() + 5
            while not bridge.action_requested.emit.called and time.time() < deadline:
                time.sleep(0.02)
            service.stop()
        bridge.action_requested.emit.assert_called_once_with("toggle_service")

    def test_gl_uploader_used_when_available(self):
        api = FakeOverlayAPI()
        service, bridge, logs = self.make_service(api, gl_upload=True)
        uploader = MagicMock()
        with patch.object(vr_overlay, "_GLTextureUploader", return_value=uploader):
            with patch.dict("sys.modules", {"openvr": fake_openvr(api)}):
                service.start()
                deadline = time.time() + 5
                while not uploader.upload.called and time.time() < deadline:
                    time.sleep(0.02)
                service.stop()
        uploader.upload.assert_called()
        self.assertEqual(api.raw_submissions, [])  # GL 通道生效时不走 setOverlayRaw
        uploader.close.assert_called_once()

    def test_gl_uploader_failure_falls_back_to_raw(self):
        api = FakeOverlayAPI()
        service, bridge, logs = self.make_service(api, gl_upload=True)
        with patch.object(vr_overlay, "_GLTextureUploader", side_effect=RuntimeError("no GL")):
            with patch.dict("sys.modules", {"openvr": fake_openvr(api)}):
                service.start()
                deadline = time.time() + 5
                while not api.raw_submissions and time.time() < deadline:
                    time.sleep(0.02)
                service.stop()
        self.assertTrue(api.raw_submissions)  # 回退到 setOverlayRaw
        self.assertTrue(any("回退兼容模式" in message for message in logs))

    def test_steamvr_not_running_retries_quietly_and_stops_fast(self):
        api = FakeOverlayAPI()
        module = fake_openvr(api, init_error=RuntimeError("SteamVR 未运行"))
        service, bridge, logs = self.make_service(api)
        started = time.time()
        with patch.dict("sys.modules", {"openvr": module}):
            service.start()
            time.sleep(0.3)
            service.stop()
        self.assertLess(time.time() - started, 5)  # 重试等待可被 stop 立即打断
        self.assertTrue(any("等待 SteamVR" in message for message in logs))
        self.assertIsNone(api.created)

    def test_session_failure_recovers_to_retry_loop(self):
        api = FakeOverlayAPI()

        class FailingOverlay(FakeOverlayAPI):
            def setOverlayRaw(self, *args):
                raise RuntimeError("SteamVR 已退出")

        failing = FailingOverlay()
        inits = []

        def init(app_type):
            inits.append(app_type)
            if len(inits) > 1:
                raise RuntimeError("not running")

        module = fake_openvr(failing)
        module.init = init
        service, bridge, logs = self.make_service(failing)
        with patch.dict("sys.modules", {"openvr": module}):
            service.start()
            deadline = time.time() + 5
            while len(inits) < 2 and time.time() < deadline:
                time.sleep(0.02)
            service.stop()
        self.assertGreaterEqual(len(inits), 2)  # 断连后回到重试循环
        self.assertTrue(any("连接中断" in message for message in logs))


class ApplicationsFallbackTests(unittest.TestCase):
    def test_applications_reuses_active_overlay_session(self):
        api = object()
        module = types.SimpleNamespace(
            VRApplication_Background=3,
            initInternal2=MagicMock(side_effect=RuntimeError("already initialized")),
            IVRApplications=MagicMock(return_value=api),
            shutdownInternal=MagicMock(),
        )
        vr_overlay._set_session_active(True)
        try:
            with patch.dict("sys.modules", {"openvr": module}):
                with steamvr_startup.applications() as got:
                    self.assertIs(got, api)
        finally:
            vr_overlay._set_session_active(False)
        module.shutdownInternal.assert_not_called()

    def test_applications_still_fails_without_overlay_session(self):
        module = types.SimpleNamespace(
            VRApplication_Background=3,
            initInternal2=MagicMock(side_effect=RuntimeError("not running")),
            IVRApplications=MagicMock(),
            shutdownInternal=MagicMock(),
        )
        vr_overlay._set_session_active(False)
        with patch.dict("sys.modules", {"openvr": module}):
            with self.assertRaises(RuntimeError):
                with steamvr_startup.applications():
                    pass
        module.IVRApplications.assert_not_called()


if __name__ == "__main__":
    unittest.main()
