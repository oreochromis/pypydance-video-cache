"""OpenVR 就绪探针：在子进程里做一次真实 init，确认 vrserver 正在接待。

vrclient.dll 会把进程内第一次 init 的失败状态静态缓存下来（实测：连读取
path registry 用的环境变量都被缓存），VR_ShutdownInternal 也清不掉。主进程
一旦在 SteamVR 未就绪时 init 失败过一次，之后无论怎么重试都只会重放同一个
错误（典型表现为 SteamVR 明明在跑，init 却永远 NoLogPath/HmdNotFound）。

所以主进程绝不在"可能失败"的情况下 init：先起子进程探针（失败了无所谓，
进程退出状态即随之消失），探针确认 vrserver 就绪后主进程才做真正的 init。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

PROBE_ARG = "--openvr-probe"
RESULT_ENV = "OPENVR_PROBE_RESULT"
PROBE_TIMEOUT = 30.0


def _probe_command():
    """打包版用 exe 自身加隐藏参数；源码运行用 -m 模块方式（需补 PYTHONPATH）。"""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        return [sys.executable, PROBE_ARG], env
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    return [sys.executable, "-m", "dance_ui.openvr_probe"], env


def probe_steamvr_ready(timeout=PROBE_TIMEOUT):
    """返回 (ready, error_name)。探针子进程 init 成功 -> (True, None)；
    否则 error_name 是 openvr 错误类名（如 InitError_Init_HmdNotFound）或
    ProbeTimeout / ProbeSpawnFailed / ProbeNoResult。"""
    descriptor, result_path = tempfile.mkstemp(prefix=".openvr-probe-")
    os.close(descriptor)
    command, env = _probe_command()
    env[RESULT_ENV] = result_path
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    outcome = None
    try:
        try:
            subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout, creationflags=creationflags)
        except subprocess.TimeoutExpired:
            return False, "ProbeTimeout"
        except OSError:
            return False, "ProbeSpawnFailed"
        try:
            with open(result_path, encoding="utf-8") as handle:
                outcome = handle.read().strip()
        except OSError:
            pass
    finally:
        try:
            os.unlink(result_path)
        except OSError:
            pass
    if not outcome:
        return False, "ProbeNoResult"
    if outcome == "OK":
        return True, None
    return False, outcome


def probe_main():
    """探针子进程入口。Background init（不会拉起未运行的 SteamVR），
    结果写 RESULT_ENV 指向的文件（窗口化打包 exe 没有可用 stdout）。"""
    result_path = os.environ.get(RESULT_ENV)
    outcome = "ProbeError"
    try:
        from .openvr_runtime import import_openvr
        openvr = import_openvr()
        try:
            openvr.initInternal2(openvr.VRApplication_Background, None)
            outcome = "OK"
        except Exception as exc:
            # pyopenvr 错误 str() 为空，错误名在类名里
            outcome = type(exc).__name__
        try:
            openvr.shutdownInternal()
        except Exception:
            pass
    except Exception as exc:
        outcome = f"ProbeError:{type(exc).__name__}"
    if result_path:
        try:
            with open(result_path, "w", encoding="utf-8") as handle:
                handle.write(outcome)
        except OSError:
            pass
    elif sys.stdout is not None:
        print(outcome, flush=True)
    return 0 if outcome == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(probe_main())
