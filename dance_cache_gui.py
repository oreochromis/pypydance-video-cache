"""Desktop entry point for the PyPyDance cache assistant."""
from __future__ import annotations

import os
import sys


def main():
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
