"""VR 面板子进程：在独立进程里持有 OpenVR overlay 会话。

设计动机：vrclient 一旦初始化失败或 vrserver 中途死亡，进程内状态不可恢复
（失败状态被静态缓存、接口指针失效），ctypes 层的访问冲突还可能拖垮主程序
（hosts 劫持就没人恢复了）。面板会话因此跑在独立子进程里：崩溃只死子进程，
父进程立刻拉起全新子进程（干净的 vrclient），主程序与缓存服务完全不受影响。

与父进程通过 127.0.0.1 回环 TCP 交换 JSON 行（窗口化打包 exe 没有可用的
stdin/stdout，探针模块已实证）：

  父 -> 子: {"type":"state","data":{...}} / {"type":"notice","message":...} / {"type":"stop"}
  子 -> 父: {"type":"hello","token":...}（首行鉴权）
            {"type":"log","message":...} / {"type":"action","action":...}
            {"type":"visibility","visible":bool} / {"type":"init-error","error":...}

退出码：0 正常（收到 stop 或父进程断开）；2 SteamVR 未就绪（init 失败，
父进程稍后拉起新子进程重试）；1 其他异常；访问冲突等硬崩溃表现为非零退出码，
由父进程按崩溃分类处理。
"""
from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .vr_overlay import (PANEL_KEY, PANEL_NAME, TEXTURE_W, TEXTURE_H, WIDTH_METERS,
                         RENDER_INTERVAL, render_panel, hit_test, _as_pixel_buffer)

WORKER_ARG = "--vr-panel-worker"
ENV_PORT = "VR_PANEL_PORT"
ENV_TOKEN = "VR_PANEL_TOKEN"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INIT_ERROR = 2   # SteamVR 未就绪（init 失败）：本进程 vrclient 可能已被
                      # 失败状态毒化，绝不能在本进程重试，直接退出让父进程拉新进程

POLL_INTERVAL = 0.05


def worker_command():
    """子进程启动命令与环境：打包版用 exe 自身加隐藏参数；源码运行用 -m 模块。"""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_ARG], env
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    return [sys.executable, "-m", "dance_ui.vr_panel_worker"], env


class PanelSession:
    """子进程内的 overlay 会话循环。commands 是父进程命令队列
    （None 哨兵 = 父进程断开连接，等同于 stop）。"""

    def __init__(self, openvr, commands, send, gl_upload=True):
        self._openvr = openvr
        self._commands = commands
        self._send = send
        self._gl_upload = gl_upload
        self._state = None
        self._hover = None
        self._visible = True
        self._notice = None
        self._force_render = False

    def _log(self, message):
        self._send({"type": "log", "message": message})

    def _set_notice(self, text, duration=4.0):
        """面板上显示一条临时反馈（操作结果/失败原因），到期自动消失"""
        self._notice = (text, time.time() + duration)
        self._force_render = True

    def _current_notice(self):
        if not self._notice:
            return None
        text, until = self._notice
        if time.time() >= until:
            self._notice = None
            return None
        return text

    def _drain_commands(self):
        """处理积压的父进程命令；返回 True 表示应结束会话"""
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return False
            if cmd is None or cmd.get("type") == "stop":
                return True
            if cmd.get("type") == "state":
                self._state = cmd.get("data")
            elif cmd.get("type") == "notice":
                self._set_notice(cmd.get("message") or "")

    def run(self):
        openvr = self._openvr
        overlay = openvr.IVROverlay()
        handle = thumbnail = uploader = None
        try:
            handle, thumbnail = overlay.createDashboardOverlay(PANEL_KEY, PANEL_NAME)
            overlay.setOverlayWidthInMeters(handle, WIDTH_METERS)
            overlay.setOverlayInputMethod(handle, openvr.VROverlayInputMethod_Mouse)
            icon_path = Path(__file__).resolve().parents[1] / "assets" / "app-icon.png"
            if icon_path.is_file():
                overlay.setOverlayFromFile(thumbnail, str(icon_path))
            self._log("VR 面板已注册：打开 SteamVR 菜单即可看到「VRChat 跳舞房助手」页面")
            if self._gl_upload:
                try:
                    from .vr_overlay import _GLTextureUploader
                    uploader = _GLTextureUploader()
                except Exception as exc:
                    self._log(f"VR 面板 GPU 纹理通道不可用，回退兼容模式（画面更新时可能闪烁）: {exc}")
            event = openvr.VREvent_t()
            last_render = 0.0
            last_hover = None
            last_pixels = None
            while True:
                if self._drain_commands():
                    return
                self._poll_events(openvr, overlay, handle, event, y_origin_top=uploader is not None)
                now = time.time()
                # 悬停按钮变化或按钮动作后立即重绘一次；面板隐藏时跳过渲染省 CPU
                interactive = self._hover != last_hover or self._force_render
                if self._visible and self._state is not None \
                        and (now - last_render >= RENDER_INTERVAL or interactive):
                    last_render = now
                    last_hover = self._hover
                    self._force_render = False
                    state = dict(self._state)
                    state["hover"] = self._hover
                    state["message"] = self._current_notice()
                    pixels = render_panel(state)
                    # 像素没变就跳过上传，空闲时完全不打扰合成器
                    if pixels != last_pixels:
                        last_pixels = pixels
                        if uploader is not None:
                            uploader.upload(openvr, overlay, handle, pixels)
                        else:
                            overlay.setOverlayRaw(handle, _as_pixel_buffer(pixels),
                                                  TEXTURE_W, TEXTURE_H, 4)
                time.sleep(POLL_INTERVAL)
        finally:
            if uploader is not None:
                uploader.close()
            for target in (handle, thumbnail):
                if target is not None:
                    try:
                        overlay.destroyOverlay(target)
                    except Exception:
                        pass

    def _poll_events(self, openvr, overlay, handle, event, y_origin_top=False):
        while True:
            polled = overlay.pollNextOverlayEvent(handle, event)
            # pyopenvr 的 pollNextOverlayEvent 返回 (result, event) 元组而不是 bool，
            # 元组恒为真，直接 while 会无限空转
            has_event = polled[0] if isinstance(polled, tuple) else bool(polled)
            if not has_event:
                return
            etype = event.eventType
            mouse = getattr(event.data, "mouse", None)
            if etype == getattr(openvr, "VREvent_FocusLeave", 304):
                # 激光移出面板：清掉悬停高亮，否则会一直残留
                self._hover = None
            elif etype == getattr(openvr, "VREvent_OverlayShown", 500):
                self._visible = True
                self._force_render = True
                self._send({"type": "visibility", "visible": True})
            elif etype == getattr(openvr, "VREvent_OverlayHidden", 501):
                self._visible = False
                self._send({"type": "visibility", "visible": False})
            elif etype == getattr(openvr, "VREvent_MouseMove", 300) and mouse is not None:
                self._hover = hit_test(mouse.x, mouse.y, y_origin_top)[0]
            elif etype == getattr(openvr, "VREvent_MouseButtonDown", 301) and mouse is not None:
                if getattr(mouse, "button", None) != getattr(openvr, "VRMouseButton_Left", 1):
                    continue
                action, px, py = hit_test(mouse.x, mouse.y, y_origin_top)
                self._hover = action
                # 校准日志：原始坐标 -> 换算像素 -> 命中结果，用于核对坐标映射是否准确
                self._log(f"VR 点击: 原始 ({mouse.x:.4f}, {mouse.y:.4f}) "
                          f"cursor={getattr(mouse, 'cursorIndex', '?')} "
                          f"-> 像素 ({px:.0f}, {py:.0f}) -> 命中 {action or '空白'}")
                if action:
                    # 预检（放松视频是否可用等）在父进程做；这里先给即时反馈
                    self._set_notice("已发送操作指令")
                    self._force_render = True
                    self._send({"type": "action", "action": action})


def worker_main():
    """子进程入口：连接父进程回环端口，鉴权后运行面板会话。"""
    try:
        port = int(os.environ.get(ENV_PORT, "0"))
    except ValueError:
        port = 0
    token = os.environ.get(ENV_TOKEN, "")
    if not port or not token:
        return EXIT_ERROR
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    except OSError:
        return EXIT_ERROR
    commands = queue.Queue()
    try:
        wfile = sock.makefile("w", encoding="utf-8", newline="\n")
        send_lock = threading.Lock()

        def send(obj):
            with send_lock:
                wfile.write(json.dumps(obj, ensure_ascii=False) + "\n")
                wfile.flush()

        send({"type": "hello", "token": token})

        def reader():
            try:
                for line in sock.makefile("r", encoding="utf-8", newline="\n"):
                    try:
                        commands.put(json.loads(line))
                    except ValueError:
                        pass
            except OSError:
                pass
            commands.put(None)  # 父进程断开

        threading.Thread(target=reader, daemon=True, name="vr-panel-reader").start()

        # GL 纹理通道需要 Qt 离屏上下文：必须在主线程创建（Qt 线程约束；
        # 旧版在面板工作线程里创建，违反约束，是访问冲突崩溃的最大嫌疑）
        gl_ok = False
        try:
            from PySide6.QtGui import QGuiApplication
            qt_app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
            gl_ok = True
        except Exception:
            gl_ok = False

        try:
            from .openvr_runtime import import_openvr
            openvr = import_openvr()
        except (ImportError, OSError) as exc:
            send({"type": "log", "message": f"VR 面板不可用（缺少 OpenVR 依赖）: {exc}"})
            return EXIT_ERROR
        try:
            openvr.init(openvr.VRApplication_Overlay)
        except Exception as exc:
            try:
                openvr.shutdown()
            except Exception:
                pass
            # init 失败 = 本进程 vrclient 已被毒化，上报后退出，父进程拉新进程
            send({"type": "init-error", "error": type(exc).__name__})
            return EXIT_INIT_ERROR
        try:
            PanelSession(openvr, commands, send, gl_upload=gl_ok).run()
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            try:
                send({"type": "log", "message": f"VR 面板会话异常: {detail}"})
            except OSError:
                pass
            return EXIT_ERROR
        finally:
            try:
                openvr.shutdown()
            except Exception:
                pass
        return EXIT_OK
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(worker_main())
