"""SteamVR 仪表盘 overlay：在 SteamVR 菜单里显示助手页面。

架构：面板会话跑在独立子进程（vr_panel_worker）里，本模块是父进程监管层。
vrclient 的进程内状态损坏（失败 init 静态缓存、vrserver 死亡后接口指针失效）
和 ctypes 访问冲突都只影响子进程——父进程拉起全新子进程即恢复，主程序的
缓存服务与 hosts 劫持完全不受影响。

子进程 crash 重启有配额（AutoRestartBudget）：存活不足 HEALTHY_SESSION_SECONDS
的连续崩溃计次，超限停止自动拉起；存活够久后崩溃说明是 SteamVR 侧问题，不计次。
面板出现在 SteamVR 菜单（dashboard overlay + 缩略图）、显示当前歌曲与缓存状态、
接收控制器激光的鼠标事件（启停服务 + CoolDown 两个按钮）。
纹理优先用常驻 OpenGL 纹理原地更新（不闪烁，在子进程主线程创建，符合 Qt 线程约束），
GL 不可用时回退 setOverlayRaw。SteamVR 未运行时子进程报 init-error 退出，
父进程按 15 秒间隔拉起新子进程重试。"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
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
RETRY_INTERVAL = 15.0   # SteamVR 未运行时的重连间隔（秒）
AUTO_RESTART_LIMIT = 3      # 进程内 OpenVR 损坏时的自动重启次数上限……
AUTO_RESTART_WINDOW = 600.0  # ……在该时间窗（秒）内计次，防 SteamVR 反复崩溃导致无限重启循环
HEALTHY_SESSION_SECONDS = 120.0  # 会话存活超过该时长才算"健康"，清掉自动重启计次


class AutoRestartBudget:
    """自动重启配额（跨进程持久化）：时间窗内最多 LIMIT 次，超限改为提示手动重启。
    会话成功建立后 reset——配额只惩罚"连续起不来"，正常使用的历史不计。"""

    def __init__(self, path):
        self._path = path

    def _read(self):
        try:
            import json
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return [float(t) for t in data.get("restarts", [])]
        except Exception:
            return []

    def _write(self, restarts):
        try:
            import json
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"restarts": restarts}), encoding="utf-8")
        except OSError:
            pass

    def allow(self):
        now = time.time()
        restarts = [t for t in self._read() if now - t < AUTO_RESTART_WINDOW]
        if len(restarts) >= AUTO_RESTART_LIMIT:
            return False
        restarts.append(now)
        self._write(restarts)
        return True

    def reset(self):
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass


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
        "cooldown": (TEXTURE_W - 300, TEXTURE_H - 88, TEXTURE_W - 40, TEXTURE_H - 32),
    }


def render_panel(state):
    """把状态画成 RGBA 字节。state 键：
    running, current{name, meta}, queue[{index, name, state}], stats{hits, misses, saved_mb},
    downloading[0..1]{name, percent}, clicks, hover(悬停按钮 action id 或 None), message
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
    hover = state.get("hover")
    for action, rect in rects.items():
        x0, y0, x1, y1 = rect
        fill = (98, 116, 255, 255) if hover == action else (62, 68, 90, 255)
        draw.rounded_rectangle(rect, 12, fill=fill)
        if action == "toggle_service":
            label = state.get("service_label") or "启动/停止服务"
        else:
            cooldown = state.get("cooldown") or {}
            if not cooldown.get("available"):
                label = "CoolDown（未设放松视频）"
            elif cooldown.get("phase") == "idle":
                label = "启用 CoolDown"
            else:
                label = "取消 CoolDown"
        font = body_font
        lw = draw.textlength(label, font=font)
        draw.text(((x0 + x1 - lw) / 2, (y0 + y1) / 2 - 13), label, font=font, fill=(238, 240, 247, 255))

    return img.tobytes()


# overlay 鼠标坐标按边长=纹理宽的正方形归一化，纹理高度不足的部分上下居中留边
# （9 点校准实测，误差 < 6px）。raw 上传时 y 原点在下：py = (1 - Y_MARGIN - y) * W；
# GL 纹理上传时 SteamVR 报的 y 与 raw 互为镜像（原点在上）：py = (y - Y_MARGIN) * W
Y_MARGIN = (TEXTURE_W - TEXTURE_H) / 2 / TEXTURE_W


def hit_test(x_norm, y_norm, y_origin_top=False):
    """overlay 鼠标事件坐标换算成纹理像素后命中按钮动作 id。
    x 原点在左直接乘宽；y 方向取决于上传通道（见 Y_MARGIN 注释）"""
    px = x_norm * TEXTURE_W
    if y_origin_top:
        py = (y_norm - Y_MARGIN) * TEXTURE_W
    else:
        py = (1.0 - Y_MARGIN - y_norm) * TEXTURE_W
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


class _GLTextureUploader:
    """常驻 OpenGL 纹理上传通道：纹理对象只建一次，每次更新用 glTexSubImage2D
    原地改写内容再 setOverlayTexture 通知 SteamVR。纹理句柄不变，SteamVR 合成器
    不会采到"替换中"的空纹理，解决 setOverlayRaw 整纹理替换造成的更新闪烁。
    任何一步失败都抛异常，由调用方回退 setOverlayRaw 兼容模式。"""

    _GL_TEXTURE_2D = 0x0DE1
    _GL_RGBA = 0x1908
    _GL_UNSIGNED_BYTE = 0x1401
    _GL_TEXTURE_MIN_FILTER = 0x2801
    _GL_TEXTURE_MAG_FILTER = 0x2800
    _GL_LINEAR = 0x2601

    def __init__(self):
        from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QGuiApplication
        import ctypes
        if QGuiApplication.instance() is None:
            # 无 QGuiApplication 时创建 GL 上下文会直接段错误（C++ 层崩溃，
            # 异常接不住），必须提前用 Python 异常拒绝
            raise RuntimeError("QGuiApplication 不存在")
        self._ct = ctypes
        self._surface = QOffscreenSurface()
        self._surface.create()
        if not self._surface.isValid():
            raise RuntimeError("QOffscreenSurface 创建失败")
        self._ctx = QOpenGLContext()
        if not self._ctx.create():
            raise RuntimeError("QOpenGLContext 创建失败")
        if not self._ctx.makeCurrent(self._surface):
            raise RuntimeError("OpenGL 上下文 makeCurrent 失败")
        self._gl = ctypes.windll.opengl32
        tex = ctypes.c_uint(0)
        self._gl.glGenTextures(1, ctypes.byref(tex))
        self._tex = tex.value
        if not self._tex:
            raise RuntimeError("glGenTextures 返回 0")
        self._gl.glBindTexture(self._GL_TEXTURE_2D, self._tex)
        self._gl.glTexParameteri(self._GL_TEXTURE_2D, self._GL_TEXTURE_MIN_FILTER, self._GL_LINEAR)
        self._gl.glTexParameteri(self._GL_TEXTURE_2D, self._GL_TEXTURE_MAG_FILTER, self._GL_LINEAR)
        self._gl.glTexImage2D(self._GL_TEXTURE_2D, 0, self._GL_RGBA, TEXTURE_W, TEXTURE_H, 0,
                              self._GL_RGBA, self._GL_UNSIGNED_BYTE, None)

    def upload(self, openvr, overlay, handle, pixels):
        # SteamVR 按 OpenGL 约定（原点左下）采样纹理，Pillow 数据是顶向下，
        # 直接上传画面会上下颠倒；上传前垂直翻转抵消（注意不是 180° 旋转，
        # 旋转会额外引入左右镜像、点选位置也会跟着错）
        from PIL import Image
        pixels = Image.frombytes("RGBA", (TEXTURE_W, TEXTURE_H), pixels) \
                      .transpose(Image.FLIP_TOP_BOTTOM).tobytes()
        self._gl.glBindTexture(self._GL_TEXTURE_2D, self._tex)
        self._gl.glTexSubImage2D(self._GL_TEXTURE_2D, 0, 0, 0, TEXTURE_W, TEXTURE_H,
                                 self._GL_RGBA, self._GL_UNSIGNED_BYTE, pixels)
        self._gl.glFlush()
        texture = openvr.Texture_t()
        texture.handle = self._tex
        texture.eType = openvr.TextureType_OpenGL
        texture.eColorSpace = openvr.ColorSpace_Auto
        overlay.setOverlayTexture(handle, texture)

    def close(self):
        try:
            tex = self._ct.c_uint(self._tex)
            self._gl.glDeleteTextures(1, self._ct.byref(tex))
            self._ctx.doneCurrent()
            self._surface.destroy()
        except Exception:
            pass


def collect_state():
    """从缓存核心采集面板数据；核心不可用时给出占位状态"""
    import dance_cache as core

    state = {"running": False, "current": None, "queue": [], "stats": {},
             "downloading": [], "cooldown": {"phase": "idle", "available": False},
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
        state["cooldown"] = core.get_cooldown_state()
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


def _describe_error_name(name):
    """按 openvr 错误名（异常类名）生成等待提示。pyopenvr 抛出的错误类 str() 为空，
    错误名全在类名里（error_value 是数字码）。
    121 NoServerForBackgroundApp：vrserver 活着但没有头显提供服务（SteamVR 开着、
    头显/串流未连接的典型状态）；108 HmdNotFound：SteamVR 未运行。"""
    if name == "InitError_Init_NoServerForBackgroundApp":
        return ("VR 面板：SteamVR 已启动，但没有头显连接（Virtual Desktop 串流未连上时也是这个状态），"
                "SteamVR 此时不接待面板程序；头显连上后面板会自动出现，无需操作")
    if name == "InitError_Init_HmdNotFound":
        return ("VR 面板：等待 SteamVR 就绪（若 SteamVR 已打开，请确认头显已连接 / "
                "Virtual Desktop 串流已连上；就绪后面板自动出现）")
    if name == "InitError_Init_NoLogPath":
        from .openvr_runtime import describe_probe_error
        return "VR 面板：" + describe_probe_error(name)
    if name and name.startswith("Probe"):
        return "VR 面板：等待 SteamVR 就绪（连接检测未通过，继续等待）"
    # 未知错误也必须把名字带出来，否则无法诊断（3.9.3 的 NoLogPath 就被旧兜底吞了）
    return f"VR 面板：等待 SteamVR 启动（{name or '未知错误'}），启动后自动出现"


def _describe_init_error(exc):
    return _describe_error_name(type(exc).__name__)


class OverlayBridge(QObject):
    """面板线程 -> GUI 线程的动作信号（Qt 自动排队到主线程执行）"""

    action_requested = Signal(str)


WORKER_CLOSED = object()   # readline 哨兵：子进程连接断开
STATE_PUSH_INTERVAL = 1.0  # 状态变化检查/推送间隔（秒）


class WorkerHandle:
    """真实子进程句柄：拉起 exe/-m 模块，回环 TCP + JSON 行协议通信。

    stderr 重定向到状态目录的 vr-panel-worker.log（硬崩溃如访问冲突时 Python
    层收不到任何信息，stderr 是唯一线索）。连接带 token 鉴权，防本机其他
    进程误连。"""

    def __init__(self, proc, listener, token, stderr_file):
        self._proc = proc
        self._listener = listener
        self._token = token
        self._stderr_file = stderr_file
        self._conn = None
        self._buf = ""
        self._send_lock = threading.Lock()
        self.authenticated = False

    @classmethod
    def spawn(cls, log_dir=None):
        from . import vr_panel_worker
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
        except OSError:
            listener.close()
            raise
        port = listener.getsockname()[1]
        token = secrets.token_hex(16)
        command, env = vr_panel_worker.worker_command()
        env[vr_panel_worker.ENV_PORT] = str(port)
        env[vr_panel_worker.ENV_TOKEN] = token
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        stderr = subprocess.DEVNULL
        stderr_file = None
        if log_dir is not None:
            try:
                os.makedirs(log_dir, exist_ok=True)
                stderr_file = open(os.path.join(log_dir, "vr-panel-worker.log"), "wb")
                stderr = stderr_file
            except OSError:
                stderr_file = None
        try:
            proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=stderr,
                                    creationflags=creationflags)
        except Exception:
            listener.close()
            if stderr_file is not None:
                stderr_file.close()
            raise
        return cls(proc, listener, token, stderr_file)

    def poll(self):
        return self._proc.poll()

    def send(self, obj):
        conn = self._conn
        if conn is None:
            raise OSError("worker 尚未连接")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._send_lock:
            conn.sendall(data)

    def readline(self, timeout):
        """下一条消息；超时返回 None；连接断开/鉴权失败返回 WORKER_CLOSED。"""
        deadline = time.monotonic() + timeout
        while True:
            if "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") == "hello":
                    # 首行鉴权：token 不符的连接直接掐断
                    if msg.get("token") != self._token:
                        return WORKER_CLOSED
                    self.authenticated = True
                    continue
                return msg
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if self._conn is None:
                if self._proc.poll() is not None:
                    return WORKER_CLOSED   # 还没连上就退出了
                self._listener.settimeout(remaining)
                try:
                    self._conn, _ = self._listener.accept()
                except socket.timeout:
                    return None
                except OSError:
                    return WORKER_CLOSED
                continue
            self._conn.settimeout(remaining)
            try:
                data = self._conn.recv(65536)
            except socket.timeout:
                return None
            except OSError:
                return WORKER_CLOSED
            if not data:
                return WORKER_CLOSED
            self._buf += data.decode("utf-8", "replace")

    def close(self, grace=3.0):
        """先发 stop 让子进程干净退出（销毁 overlay、VR_Shutdown），超时强杀。"""
        try:
            self.send({"type": "stop"})
        except OSError:
            pass
        deadline = time.monotonic() + grace
        while self._proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
        for conn in (self._conn, self._listener):
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
        if self._stderr_file is not None:
            try:
                self._stderr_file.close()
            except OSError:
                pass


class VROverlayService:
    """VR 面板监管：按需拉起/回收面板子进程，线程内自处理重试与崩溃重启。

    会话本体在子进程（vr_panel_worker）：vrclient 的进程内状态损坏、ctypes
    访问冲突、GL 崩溃都只影响子进程，父进程拉新子进程即可恢复，主程序
    （缓存服务/hosts 劫持）完全不受影响。"""

    def __init__(self, bridge=None, state_provider=None, logger=None,
                 restart_state_path=None, worker_factory=None):
        self.bridge = bridge or OverlayBridge()
        self.state_provider = state_provider or collect_state
        self.logger = logger or self._default_logger
        self._restart_budget = AutoRestartBudget(restart_state_path) if restart_state_path else None
        self._worker_factory = worker_factory  # 测试注入；None 用真实子进程
        self._stop = threading.Event()
        self._thread = None
        self._notice = None   # (文本, 过期时间戳)，父进程侧推给面板的临时反馈
        self._visible = True   # 子进程 visibility 消息驱动；默认可见保证首轮推送状态

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
        # 手动开启/重开面板 = 用户明确意图，清掉崩溃计次
        if self._restart_budget is not None:
            self._restart_budget.reset()
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

    def _set_notice(self, text, duration=4.0):
        """面板上显示一条临时反馈（操作结果/失败原因），到期自动消失"""
        self._notice = (text, time.time() + duration)

    def _current_notice(self):
        if not self._notice:
            return None
        text, until = self._notice
        if time.time() >= until:
            self._notice = None
            return None
        return text

    def _cooldown_ready(self):
        """VR 端点 CoolDown 前预检，返回 (是否放行, 拦截原因)"""
        try:
            import dance_cache as core
            if not core.get_cooldown_state().get("available"):
                return False, "未设置放松视频，请在桌面端「设置」页选择"
            if not core.is_running():
                return False, "缓存服务未运行，请先启动服务"
        except Exception:
            pass   # 核心不可用时不拦截，交给桌面端报错
        return True, None

    # ---------------- 线程内部 ----------------

    def _spawn_worker(self):
        if self._worker_factory is not None:
            return self._worker_factory()
        log_dir = None
        if self._restart_budget is not None:
            log_dir = str(self._restart_budget._path.parent)
        return WorkerHandle.spawn(log_dir)

    def _run(self):
        last_reason = None
        while not self._stop.is_set():
            try:
                worker = self._spawn_worker()
            except Exception as exc:
                self.logger(f"VR 面板进程拉起失败: {exc}")
                self._stop.wait(RETRY_INTERVAL)
                continue
            started_at = time.time()
            outcome, detail = self._talk(worker)
            worker.close()
            if self._stop.is_set():
                break
            if outcome == "init-error":
                # SteamVR 未就绪：子进程 init 失败后已退出（其进程内的 vrclient
                # 毒化随之消失），父进程稍后拉起全新子进程重试
                reason = _describe_error_name(detail)
                if reason != last_reason:  # 原因变化才记日志，避免等待期间刷屏
                    self.logger(reason)
                    last_reason = reason
                self._stop.wait(RETRY_INTERVAL)
                continue
            # crash：子进程异常退出（含访问冲突等硬崩溃，Python 层根本收不到异常）
            last_reason = None
            if (self._restart_budget is not None
                    and time.time() - started_at >= HEALTHY_SESSION_SECONDS):
                # 存活够久才崩 = 正常运行后 SteamVR 侧出问题，不计入崩溃配额
                self._restart_budget.reset()
            if self._restart_budget is not None and not self._restart_budget.allow():
                self.logger("VR 面板进程连续崩溃，已停止自动拉起"
                            "（重开「SteamVR 面板页」开关或重启软件可重试）")
                return
            self.logger(f"VR 面板进程异常退出（代码 {detail}），正在重新拉起…")
            self._stop.wait(1.0)
        self.logger("VR 面板已关闭")

    def _talk(self, worker):
        """陪伴一个子进程的生命周期：转发消息、推状态。
        返回 ("stop"|"init-error"|"crash", detail)。"""
        last_push = 0.0
        last_payload = None
        init_error = None
        while not self._stop.is_set():
            msg = worker.readline(timeout=0.2)
            if msg is WORKER_CLOSED:
                code = worker.poll()
                if init_error is not None:
                    return "init-error", init_error
                return "crash", code
            if msg is not None:
                mtype = msg.get("type")
                if mtype == "log":
                    self.logger(msg.get("message") or "")
                elif mtype == "action":
                    self._handle_action(worker, msg.get("action"))
                elif mtype == "visibility":
                    self._visible = bool(msg.get("visible"))
                    last_payload = None  # 重新可见时强制推一次最新状态
                elif mtype == "init-error":
                    init_error = msg.get("error")
            now = time.time()
            if self._visible and now - last_push >= STATE_PUSH_INTERVAL:
                last_push = now
                try:
                    state = self.state_provider()
                except Exception:
                    state = {}
                state["message"] = self._current_notice()
                try:
                    payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
                except (TypeError, ValueError):
                    payload = None
                if payload is not None and payload != last_payload:
                    try:
                        worker.send({"type": "state", "data": state})
                        last_payload = payload
                    except OSError:
                        pass  # 连接断了，下一轮 readline 会报 WORKER_CLOSED
        return "stop", None

    def _handle_action(self, worker, action):
        if not action:
            return
        if action == "cooldown":
            ready, reason = self._cooldown_ready()
            if not ready:
                try:
                    worker.send({"type": "notice", "message": reason})
                except OSError:
                    pass
                return
        self.bridge.action_requested.emit(action)
