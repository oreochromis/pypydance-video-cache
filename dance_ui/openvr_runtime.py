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
