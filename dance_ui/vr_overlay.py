"""SteamVR 仪表盘 overlay（实验性）：在 SteamVR 菜单里显示助手页面。

骨架目标：出现在 SteamVR 菜单（dashboard overlay + 缩略图）、显示当前歌曲与缓存状态、
接收控制器激光的鼠标事件（一个真实按钮 + 一个测试按钮验证命中）。
纹理用 setOverlayRaw 直接提交 RGBA 字节，不依赖 OpenGL 上下文。
SteamVR 未运行时按 15 秒间隔静默重试连接；运行中 SteamVR 退出会自动回到重试。"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Signal

from .steamvr_startup import APP_KEY
from . import __version__

PANEL_KEY = APP_KEY + ".panel"
PANEL_NAME = "VRChat 跳舞房助手"
TEXTURE_W, TEXTURE_H = 1024, 640
WIDTH_METERS = 1.5
RENDER_INTERVAL = 1.0   # 状态纹理刷新间隔（秒）
CURSOR_RENDER_INTERVAL = 1 / 30   # 光标移动触发的重绘限频（30fps）
RETRY_INTERVAL = 15.0   # SteamVR 未运行时的重连间隔（秒）

_session_lock = threading.Lock()
_session_active = False   # 本进程持有长生命周期的 OpenVR overlay 会话


def active_applications():
    """overlay 会话存活时返回 IVRApplications 接口，供启动设置复用同一会话；
    无会话时返回 None（调用方走自己的短连接）。"""
    with _session_lock:
        if not _session_active:
            return None
    try:
        import openvr
        return openvr.IVRApplications()
    except Exception:
        return None


def _set_session_active(value):
    global _session_active
    with _session_lock:
        _session_active = value


_font_cache = {}


def _font(size, bold=False):
    """Windows 中文字体；找不到时退回 PIL 默认字体（中文会变成方块，仅影响极端环境）"""
    key = (size, bold)
    if key not in _font_cache:
        from PIL import ImageFont
        for name in ("msyhbd.ttc" if bold else "msyh.ttc", "msyh.ttc", "simhei.ttf"):
            try:
                _font_cache[key] = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        else:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def _clip(draw, text, font, max_width):
    """按像素宽度截断超长文本"""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…" if text else ""


# 按钮区域（像素坐标）：动作 id -> rect
def _button_rects():
    return {
        "toggle_service": (40, TEXTURE_H - 88, 300, TEXTURE_H - 32),
        "test_click": (TEXTURE_W - 300, TEXTURE_H - 88, TEXTURE_W - 40, TEXTURE_H - 32),
    }


def render_panel(state):
    """把状态画成 RGBA 字节。state 键：
    running, current{name, meta}, queue[{index, name, state}], stats{hits, misses, saved_mb},
    downloading[0..1]{name, percent}, clicks, cursor(x, y 或 None), message
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (TEXTURE_W, TEXTURE_H), (24, 27, 36, 255))
    draw = ImageDraw.Draw(img)
    title_font = _font(34, bold=True)
    head_font = _font(24, bold=True)
    body_font = _font(22)
    small_font = _font(19)

    # 标题栏
    draw.text((40, 24), f"{PANEL_NAME}  v{__version__}", font=title_font, fill=(235, 238, 245, 255))
    dot = (88, 199, 120, 255) if state.get("running") else (140, 145, 158, 255)
    draw.ellipse((TEXTURE_W - 76, 36, TEXTURE_W - 48, 64), fill=dot)
    draw.text((TEXTURE_W - 210, 34), "运行中" if state.get("running") else "未运行",
              font=small_font, fill=dot)
    draw.line((40, 84, TEXTURE_W - 40, 84), fill=(58, 63, 80, 255), width=2)

    y = 104
    # 当前歌曲
    draw.text((40, y), "当前歌曲", font=head_font, fill=(150, 158, 178, 255))
    current = state.get("current")
    if current:
        draw.text((40, y + 36), _clip(draw, current.get("name") or "（无标题）", body_font, TEXTURE_W - 360),
                  font=body_font, fill=(238, 240, 247, 255))
        badge = current.get("state") or ""
        bw = draw.textlength(badge, font=small_font) + 24
        draw.rounded_rectangle((TEXTURE_W - 40 - bw, y + 34, TEXTURE_W - 40, y + 70), 10,
                               fill=(46, 84, 62, 255))
        draw.text((TEXTURE_W - 40 - bw + 12, y + 38), badge, font=small_font, fill=(126, 222, 160, 255))
        draw.text((40, y + 68), _clip(draw, current.get("meta") or "", small_font, TEXTURE_W - 80),
                  font=small_font, fill=(150, 158, 178, 255))
    else:
        draw.text((40, y + 36), "等待房间队列", font=body_font, fill=(120, 126, 145, 255))
    y += 116

    # 接下来
    queue = state.get("queue") or []
    draw.text((40, y), f"接下来（{len(queue)} 首排队中）" if queue else "接下来（尚无排队歌曲）",
              font=head_font, fill=(150, 158, 178, 255))
    y += 38
    for row in queue[:4]:
        draw.text((40, y), f"{row.get('index', ''):>2}", font=small_font, fill=(120, 126, 145, 255))
        draw.text((92, y), _clip(draw, row.get("name") or "", small_font, TEXTURE_W - 320),
                  font=small_font, fill=(222, 226, 236, 255))
        tag = row.get("state") or ""
        tw = draw.textlength(tag, font=small_font) + 20
        tone = (46, 84, 62, 255) if row.get("cached") else (68, 72, 92, 255)
        draw.rounded_rectangle((TEXTURE_W - 40 - tw, y - 4, TEXTURE_W - 40, y + 26), 8, fill=tone)
        draw.text((TEXTURE_W - 40 - tw + 10, y - 2), tag, font=small_font,
                  fill=(126, 222, 160, 255) if row.get("cached") else (170, 175, 195, 255))
        y += 36

    # 下载进度 / 统计
    downloading = (state.get("downloading") or [None])[0]
    stats = state.get("stats") or {}
    y = TEXTURE_H - 210
    if downloading:
        name = _clip(draw, downloading.get("name") or "", small_font, 560)
        percent = max(0.0, min(1.0, float(downloading.get("percent") or 0)))
        draw.text((40, y), f"正在缓存  {name}", font=small_font, fill=(150, 158, 178, 255))
        draw.rounded_rectangle((40, y + 30, TEXTURE_W - 40, y + 48), 9, fill=(52, 56, 72, 255))
        if percent > 0:
            draw.rounded_rectangle((40, y + 30, 40 + (TEXTURE_W - 80) * percent, y + 48), 9,
                                   fill=(90, 108, 255, 255))
        draw.text((TEXTURE_W - 130, y), f"{percent * 100:.0f}%", font=small_font, fill=(170, 175, 195, 255))
    draw.text((40, y + 62),
              f"本次会话：本地命中 {stats.get('hits', 0)} 首 · 回源 {stats.get('misses', 0)} 首 · "
              f"节省流量 {stats.get('saved_mb', 0):.0f} MB",
              font=small_font, fill=(150, 158, 178, 255))
    message = state.get("message")
    if message:
        draw.text((40, y + 92), _clip(draw, message, small_font, TEXTURE_W - 80),
                  font=small_font, fill=(216, 178, 92, 255))

    # 按钮
    rects = _button_rects()
    cursor = state.get("cursor")
    hover = None
    if cursor:
        px, py = cursor
        for action, (x0, y0, x1, y1) in rects.items():
            if x0 <= px <= x1 and y0 <= py <= y1:
                hover = action
    for action, rect in rects.items():
        x0, y0, x1, y1 = rect
        fill = (98, 116, 255, 255) if hover == action else (62, 68, 90, 255)
        draw.rounded_rectangle(rect, 12, fill=fill)
        if action == "toggle_service":
            label = state.get("service_label") or "启动/停止服务"
        else:
            label = f"激光点击测试（{state.get('clicks', 0)} 次）"
        font = body_font
        lw = draw.textlength(label, font=font)
        draw.text(((x0 + x1 - lw) / 2, (y0 + y1) / 2 - 13), label, font=font, fill=(238, 240, 247, 255))

    # 激光光标
    if cursor:
        px, py = cursor
        draw.ellipse((px - 9, py - 9, px + 9, py + 9), fill=(255, 96, 96, 230),
                     outline=(255, 255, 255, 255), width=2)
    return img.tobytes()


def hit_test(x_norm, y_norm):
    """overlay 鼠标事件坐标（0-1，y 原点在上，与纹理像素方向一致）换算成像素后命中按钮动作 id"""
    px = x_norm * TEXTURE_W
    py = y_norm * TEXTURE_H
    for action, (x0, y0, x1, y1) in _button_rects().items():
        if x0 <= px <= x1 and y0 <= py <= y1:
            return action, px, py
    return None, px, py


def _as_pixel_buffer(data):
    """pyopenvr 的 setOverlayRaw 对 buffer 参数取 byref()，直接传 bytes 会把指针
    指到 Python 对象头而不是像素数据（SteamVR 读到全零 = 全透明）。
    必须先复制进 ctypes 数组，byref 才能指向真实像素。"""
    import ctypes
    return (ctypes.c_char * len(data)).from_buffer_copy(data)


def collect_state(clicks=0, cursor=None):
    """从缓存核心采集面板数据；核心不可用时给出占位状态"""
    import dance_cache as core

    state = {"running": False, "current": None, "queue": [], "stats": {},
             "downloading": [], "clicks": clicks, "cursor": cursor,
             "service_label": "启动服务", "message": None}
    try:
        runtime = core.get_runtime_state()
    except Exception:
        runtime = {}
    state["running"] = bool(runtime.get("running"))
    state["service_label"] = "停止服务" if state["running"] else "启动服务"
    try:
        rows = core.get_queue_snapshot()
    except Exception:
        rows = []
    if rows:
        first = rows[0]
        state["current"] = {
            "name": first.get("name"),
            "state": first.get("state"),
            "meta": f"点歌人：{first.get('player') or '?'} · 来源：{first.get('source') or '?'}",
        }
        state["queue"] = [
            {"index": i + 1, "name": r.get("name"), "state": r.get("state"),
             "cached": r.get("state_code") == "cached"}
            for i, r in enumerate(rows[1:6])
        ]
    try:
        state["stats"] = core.get_session_stats()
    except Exception:
        pass
    try:
        active = [d for d in core.get_downloads_snapshot() if (d.get("total") or 0) > 0]
        state["downloading"] = [
            {"name": d.get("filename"), "percent": d["done"] / d["total"]} for d in active[:1]
        ]
    except Exception:
        pass
    return state


class OverlayBridge(QObject):
    """overlay 线程 -> GUI 线程的动作信号（Qt 自动排队到主线程执行）"""

    action_requested = Signal(str)


class VROverlayService:
    """SteamVR 面板页生命周期管理。start/stop 幂等，线程内自处理 SteamVR 断连重试。"""

    def __init__(self, bridge=None, state_provider=None, logger=None):
        self.bridge = bridge or OverlayBridge()
        self.state_provider = state_provider or collect_state
        self.logger = logger or self._default_logger
        self._stop = threading.Event()
        self._thread = None
        self._clicks = 0
        self._cursor = None

    @staticmethod
    def _default_logger(message):
        try:
            import dance_cache as core
            core.log(message)
        except Exception:
            pass

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="vr-overlay")
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(10)

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    # ---------------- 线程内部 ----------------

    def _run(self):
        retry_logged = False
        while not self._stop.is_set():
            try:
                import openvr
            except (ImportError, OSError) as exc:
                self.logger(f"VR 面板不可用（缺少 OpenVR 依赖）: {exc}")
                return
            try:
                openvr.init(openvr.VRApplication_Overlay)
            except Exception:
                if not retry_logged:
                    self.logger("VR 面板：等待 SteamVR 启动（启动后自动出现，无需操作）")
                    retry_logged = True
                self._stop.wait(RETRY_INTERVAL)
                continue
            try:
                self._session(openvr)
            except Exception as exc:
                if not self._stop.is_set():
                    self.logger(f"VR 面板连接中断: {exc}，等待 SteamVR 后自动重连")
                    retry_logged = False
            finally:
                _set_session_active(False)
                try:
                    openvr.shutdown()
                except Exception:
                    pass
        self.logger("VR 面板已关闭")

    def _session(self, openvr):
        overlay = openvr.IVROverlay()
        handle = thumbnail = None
        try:
            handle, thumbnail = overlay.createDashboardOverlay(PANEL_KEY, PANEL_NAME)
            overlay.setOverlayWidthInMeters(handle, WIDTH_METERS)
            overlay.setOverlayInputMethod(handle, openvr.VROverlayInputMethod_Mouse)
            from pathlib import Path
            icon_path = Path(__file__).resolve().parents[1] / "assets" / "app-icon.png"
            if icon_path.is_file():
                overlay.setOverlayFromFile(thumbnail, str(icon_path))
            _set_session_active(True)
            self.logger("VR 面板已注册：打开 SteamVR 菜单即可看到「VRChat 跳舞房助手」页面")
            event = openvr.VREvent_t()
            last_render = 0.0
            last_cursor = None
            while not self._stop.is_set():
                self._poll_events(openvr, overlay, handle, event)
                now = time.time()
                # 状态 1 秒一刷；光标移动时即时重绘（限 30fps），否则激光指针拖影严重
                cursor_changed = self._cursor != last_cursor
                if (now - last_render >= RENDER_INTERVAL
                        or (cursor_changed and now - last_render >= CURSOR_RENDER_INTERVAL)):
                    last_render = now
                    last_cursor = self._cursor
                    state = self.state_provider(clicks=self._clicks, cursor=self._cursor)
                    pixels = render_panel(state)
                    overlay.setOverlayRaw(handle, _as_pixel_buffer(pixels), TEXTURE_W, TEXTURE_H, 4)
                self._stop.wait(0.02)
            # 停止前清掉光标再渲染一次不必要；退出时销毁 overlay
        finally:
            if handle is not None:
                try:
                    overlay.destroyOverlay(handle)
                except Exception:
                    pass
            if thumbnail is not None:
                try:
                    overlay.destroyOverlay(thumbnail)
                except Exception:
                    pass

    def _poll_events(self, openvr, overlay, handle, event):
        while True:
            polled = overlay.pollNextOverlayEvent(handle, event)
            # pyopenvr 的 pollNextOverlayEvent 返回 (result, event) 元组而不是 bool，
            # 元组恒为真，直接 while 会无限空转，渲染永远轮不到（面板全透明的根因）
            has_event = polled[0] if isinstance(polled, tuple) else bool(polled)
            if not has_event:
                return
            etype = event.eventType
            mouse = getattr(event.data, "mouse", None)
            if etype == getattr(openvr, "VREvent_MouseMove", 300) and mouse is not None:
                self._cursor = (mouse.x * TEXTURE_W, mouse.y * TEXTURE_H)
            elif etype == getattr(openvr, "VREvent_MouseButtonDown", 301) and mouse is not None:
                if getattr(mouse, "button", None) != getattr(openvr, "VRMouseButton_Left", 1):
                    continue
                action, px, py = hit_test(mouse.x, mouse.y)
                self._cursor = (px, py)
                if action == "test_click":
                    self._clicks += 1
                    self.logger(f"VR 面板激光点击测试成功（第 {self._clicks} 次）")
                elif action:
                    self.logger(f"VR 面板触发操作: {action}")
                    self.bridge.action_requested.emit(action)
