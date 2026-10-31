"""Desktop entry point for the PyPyDance cache assistant."""
from __future__ import annotations

import os
import sys


def main():
    # 打包 exe 的 OpenVR 探针/VR 面板子进程入口：必须在一切初始化（单实例锁、
    # 管理员检测、GUI）之前拦截，子进程只做自己的事，完成后退出
    from dance_ui.openvr_probe import PROBE_ARG, probe_main
    if PROBE_ARG in sys.argv[1:]:
        return probe_main()
    from dance_ui.vr_panel_worker import WORKER_ARG, worker_main
    if WORKER_ARG in sys.argv[1:]:
        return worker_main()
    try:
        from dance_ui.app import main as run
    except ModuleNotFoundError as exc:
        if exc.name not in ("PySide6", "PIL"):
            raise
        message = "缺少界面运行依赖。请在项目目录执行：\npython -m pip install -r requirements.txt"
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "VRChat 跳舞房助手", 0x10)
        return 1
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
