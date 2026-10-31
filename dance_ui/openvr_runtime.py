"""导入 openvr，并把底层客户端 DLL 重定向到 SteamVR 运行时自带的版本。

pyopenvr 自带的 libopenvr_api_64.dll 版本是固定的（如 2.12.x）。SteamVR
更新后，旧客户端 DLL 与新 vrserver 之间 ABI 错位：init、创建面板能成功，
随后的接口调用直接 access violation，连接-崩溃循环甚至会把 vrserver 冲垮。
运行时目录里的 openvr_api.dll 与 vrserver 严格同版本，永远是最匹配的选择。
运行时路径从 %LOCALAPPDATA%\\openvr\\openvrpaths.vrpath 读取（OpenVR 标准
路径注册表），找不到时回退 pyopenvr 自带 DLL，行为与原来一致。
"""

import ctypes
import importlib
import json
import os
import sys
import threading

_lock = threading.Lock()


def _runtime_dll_path():
    if sys.platform != "win32":
        return None
    try:
        paths_file = os.path.join(os.environ.get("LOCALAPPDATA", ""), "openvr", "openvrpaths.vrpath")
        with open(paths_file, encoding="utf-8") as fh:
            data = json.load(fh)
        for entry in data.get("runtime") or []:
            candidate = os.path.join(entry, "bin", "win64", "openvr_api.dll")
            if os.path.isfile(candidate):
                return candidate
    except Exception:
        pass
    return None


def non_ascii_exe_name():
    """exe 文件名（不含扩展名）含非 ASCII 字符时返回完整 exe 路径，否则 None。

    vrclient 用进程名构造 vrclient_<进程名>.txt 日志文件，进程名经过的编码
    转换对中文字符可能失败，导致 init 必报 InitError_Init_NoLogPath——与
    安装目录是否中文无关（实测：纯英文名的 exe 在中文目录里正常工作；
    部分中文名如「中文助手」能侥幸通过转换，「跳舞房助手」则必败，
    所以只要文件名非 ASCII 就视为不可用）。源码运行的 python.exe 是纯
    英文名，不检查。"""
    if not getattr(sys, "frozen", False):
        return None
    executable = os.path.abspath(sys.executable)
    stem = os.path.splitext(os.path.basename(executable))[0]
    try:
        stem.encode("ascii")
    except UnicodeEncodeError:
        return executable
    return None


def describe_probe_error(name):
    """把探针/openvr 错误名翻译成给用户的中文提示；NoLogPath 且 exe 文件名
    含中文时给出针对性指引（这是该错误在本软件上的唯一已知成因）。"""
    if name == "InitError_Init_NoLogPath":
        location = non_ascii_exe_name()
        if location:
            return (f"程序的 exe 文件名包含中文（{location}），SteamVR 的客户端库无法用"
                    f"中文进程名写日志（错误 NoLogPath）。请把 exe 重命名为纯英文"
                    f"（例如 VRChatDanceAssistant.exe）后重新启动；文件夹放中文路径不受影响")
    return name or "未知错误"


def import_openvr(logger=None):
    """导入 openvr 模块；Windows 上优先使用 SteamVR 运行时的客户端 DLL。"""
    with _lock:
        cached = sys.modules.get("openvr")
        if cached is not None:
            return cached
        runtime_dll = _runtime_dll_path()
        if runtime_dll is None:
            return importlib.import_module("openvr")
        original = ctypes.cdll.LoadLibrary

        def redirected(name, *args, **kwargs):
            if "libopenvr_api" in str(name):
                name = runtime_dll
            return original(name, *args, **kwargs)

        ctypes.cdll.LoadLibrary = redirected
        try:
            module = importlib.import_module("openvr")
        finally:
            ctypes.cdll.LoadLibrary = original
        if logger:
            logger("OpenVR 客户端 DLL 已切换到 SteamVR 运行时版本（避免版本错位崩溃）")
        return module
