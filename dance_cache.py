#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyPyDance Video Cache → VRChat PyPyDance助手（核心模块 + 命令行模式）
原理：hosts 劫持 cdn.pypy.dance -> 本地 HTTP 服务(80 端口) -> 命中缓存直接返回 / 未命中边下边存
同时监听 VRChat 日志中的 [PyPyDanceQueue] 点歌队列，提前下载即将播放的视频。
曲库模式：设置 library_dir 后，视频按歌曲编号命名（如 3528.mp4）永久保存在曲库目录；
播放时本地有就直接加载，没有就下载后新增进曲库。
YouTube 源：劫持 api.pypy.dance 的 302 重定向（纯 HTTP，零证书），YouTube 源歌曲在播放时
从 VRChat 日志捕获解析后的直链自动录流入库；Unity 的 https 歌单/封面请求由 443 TCP 隧道透传。
需要管理员权限（修改 hosts、绑定 80/443 端口）。
图形界面见 dance_cache_gui.py。
"""

import ctypes
import copy
import http.client
import ipaddress
import json
import os
import random
import re
import select
import socket
import ssl
import sys
import threading
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if getattr(sys, "frozen", False):
    # PyInstaller 打包后 __file__ 指向临时解压目录，程序数据要放在 exe 旁边
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Bundled read-only resources live beside this module, including in PyInstaller
# _internal. Keep the selected default implicit so moving an installation works.
DEFAULT_COOLDOWN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "default_cooldown.mp4")
HOSTS_PATH = r"C:\Windows\System32\drivers\etc\hosts"
HOSTS_MARK = "# dance-cache"
CDN_HOSTS = ["cdn.pypy.dance", "806bb815.cdn.pypy.dance"]
API_HOST = "api.pypy.dance"
UDON_API_HOST = "api.udon.dance"    # WannaDance：302 跳转服务（不劫持，仅解析真实 IP 供预载用）
UDON_PLAY_HOST = "play.udon.dance"  # WannaDance：视频文件 CDN（劫持）
HIJACK_HOSTS = CDN_HOSTS + [API_HOST]  # api 仅在 YouTube 缓存模式启用时劫持
VRC_LOG_DIR = os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "LocalLow", "VRChat", "VRChat")
MAPPING_PATH = os.path.join(SCRIPT_DIR, "mapping.json")

QUEUE_RE = re.compile(r"^\[PyPyDanceQueue] (\[.*])")
WD_QUEUE_RE = re.compile(r"queue info serialized: (\[.*])")  # WannaDance 队列
FILENAME_RE = re.compile(r"^[\w.\-]+\.mp4$")
YT_FILENAME_RE = re.compile(r"^yt_(\d+)\.mp4$")
WD_FILE_RE = re.compile(r"^(\d+)-[0-9a-z]+\.mp4$", re.IGNORECASE)  # play.udon.dance 的文件名 5283-667d628852a98.mp4
WD_CACHE_RE = re.compile(r"^wd_(\d+)\.mp4$")  # WannaDance 本地缓存命名


def hijack_host_list(include_api):
    return CDN_HOSTS + [UDON_PLAY_HOST] + ([API_HOST] if include_api else [])

DEFAULT_CONFIG = {
    "cache_dir": "cache",
    "library_dir": "",
    "max_cache_mb": 2048,
    "preload_count": 3,
    "max_parallel": 2,
    "preferred_cdn_ip": "",
    "enable_youtube_cache": True,
    "youtube_permanent": False,
    "cdn_permanent": False,
    "cooldown_file": "",
}

_config = {}
_origin_ips = {}             # host -> [ip, ...]
_downloads_in_progress = {}  # 目标路径 -> thread
_downloads_lock = threading.RLock()
_download_meta = {}          # 规范化目标路径 -> GUI 可读任务元信息
_download_results = {}       # 最近一次下载结果（仅内存，供队列状态使用）
_config_lock = threading.RLock()
_lifecycle_lock = threading.RLock()
_hosts_lock = threading.RLock()
_runtime_lock = threading.RLock()
_library_task_lock = threading.Lock()
_cooldown_lock = threading.RLock()
_run_id = 0
_service_threads = []
_hosts_restore_pending = False
_runtime_state = {
    "running": False, "phase": "stopped", "youtube_available": False,
    "queue_watcher_available": False, "hosts_restore_pending": False,
    "warnings": [], "error": None,
}
_resolved_ids = {}           # songNum -> filename or None
_resolved_lock = threading.Lock()
_mapping = {}                # song_id -> filename（"" 表示非CDN源）
_reverse_mapping = {}        # filename -> song_id
_mapping_lock = threading.Lock()
_mapping_dirty = False
_stop_event = threading.Event()
_server = None
_watcher = None
_running = False
_library_stop = threading.Event()
_rate_limit_until = 0.0
_rate_limit_lock = threading.Lock()

RETRYABLE_STATUS = {429, 500, 502, 503, 520, 521, 522, 524}


class TransientUpstream(Exception):
    """源站临时错误（限流/5xx），可退避重试"""

    def __init__(self, status, retry_after=0.0):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}")


def note_rate_limited(retry_after):
    global _rate_limit_until
    with _rate_limit_lock:
        _rate_limit_until = max(_rate_limit_until, time.time() + retry_after)


def wait_rate_limit():
    with _rate_limit_lock:
        delay = _rate_limit_until - time.time()
    if delay > 0:
        log(f"触发服务器限流，全局等待 {delay:.0f} 秒...")
        _stop_event.wait(min(delay, 300))


# ---------------- 事件总线 ----------------
# 事件类型: log / queue / progress / download_start / download_done / stats / state
#          rebuild_progress / rebuild_done / verify_progress / verify_done

_listeners = []
_listeners_lock = threading.Lock()
_last_queue_items = []  # 最近一次点歌队列（用于下载完成后刷新队列缓存状态）
_queue_lock = threading.RLock()


def add_listener(fn):
    with _listeners_lock:
        if fn not in _listeners:
            _listeners.append(fn)


def remove_listener(fn):
    with _listeners_lock:
        if fn in _listeners:
            _listeners.remove(fn)


def emit(kind, **data):
    with _listeners_lock:
        listeners = list(_listeners)
    for fn in listeners:
        try:
            fn(kind, data)
        except Exception:
            pass
    if kind == "download_done":
        refresh_queue_state()


def log(msg):
    emit("log", msg=msg)


# ---------------- 配置 ----------------

def load_config():
    global _config
    path = os.path.join(SCRIPT_DIR, "config.json")
    missing = not os.path.exists(path)
    warnings = []
    loaded = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("配置文件必须是对象")
            # 历史配置可能共用目录。保留其身份，启动/清理拒绝执行，供设置页修正。
            cfg = _validated_config({**DEFAULT_CONFIG, **loaded}, allow_shared_storage=True)
        except Exception as e:
            warnings.append(f"config.json 参数无效，已恢复默认参数并保留可读取的保存目录: {e}")
            preserved = {}
            if isinstance(loaded, dict):
                for name in ("cache_dir", "library_dir"):
                    if isinstance(loaded.get(name), str) and loaded[name].strip():
                        preserved[name] = loaded[name]
            cfg = _validated_config({**DEFAULT_CONFIG, **preserved}, allow_shared_storage=True)
    else:
        cfg = _validated_config(dict(DEFAULT_CONFIG))
    os.makedirs(cfg["cache_dir"], exist_ok=True)
    lib = cfg.get("library_dir") or ""
    if lib:
        try:
            os.makedirs(lib, exist_ok=True)
        except Exception as e:
            warnings.append(f"曲库目录不可用，已停用永久保存: {e}")
            cfg["library_dir"] = ""
            cfg["youtube_permanent"] = False
            cfg["cdn_permanent"] = False
    with _config_lock:
        _config = cfg
    if _same_directory(cfg["cache_dir"], cfg.get("library_dir")):
        warnings.append("缓存目录与永久曲库相同，已禁用服务启动与缓存清理，请在设置中分开保存目录")
    for warning in warnings:
        log(warning)
        emit("warning", message=warning)
    if warnings:
        _set_runtime_state(warnings=warnings)
    if missing:
        save_config()


def _canonical_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _same_directory(left, right):
    if not left or not right:
        return False
    try:
        return os.path.samefile(left, right)
    except OSError:
        return _canonical_path(left) == _canonical_path(right)


def _validated_config(config, *, allow_shared_storage=False):
    cfg = copy.deepcopy(config)
    for name, minimum in (("preload_count", 0), ("max_cache_mb", 100), ("max_parallel", 1)):
        value = cfg.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} 必须是大于等于 {minimum} 的整数")
    for name in ("enable_youtube_cache", "youtube_permanent", "cdn_permanent"):
        if not isinstance(cfg.get(name), bool):
            raise ValueError(f"{name} 必须是布尔值")
    for name in ("cache_dir", "library_dir", "preferred_cdn_ip", "cooldown_file"):
        if not isinstance(cfg.get(name), str):
            raise ValueError(f"{name} 必须是文本")
        cfg[name] = cfg[name].strip()
    if not cfg["cache_dir"]:
        raise ValueError("缓存目录不能为空")
    for name in ("cache_dir", "library_dir", "cooldown_file"):
        if cfg[name]:
            cfg[name] = os.path.abspath(os.path.join(SCRIPT_DIR, os.path.expanduser(cfg[name])))
    if not allow_shared_storage and _same_directory(cfg["cache_dir"], cfg["library_dir"]):
        raise ValueError("缓存目录与永久曲库不能是同一个目录")
    ip = cfg["preferred_cdn_ip"]
    if ip:
        try:
            parsed = ipaddress.ip_address(ip)
        except ValueError as e:
            raise ValueError("优选 CDN IP 必须是有效的公网 IPv4 地址") from e
        if parsed.version != 4 or not parsed.is_global:
            raise ValueError("优选 CDN IP 必须是有效的公网 IPv4 地址")
    if (cfg["youtube_permanent"] or cfg["cdn_permanent"]) and not cfg["library_dir"]:
        raise ValueError("启用永久保存前，请先设置曲库目录")
    return cfg


def get_config():
    """返回配置副本。界面通过 update_config 更新，不直接修改全局状态。"""
    with _config_lock:
        return copy.deepcopy(_config or _validated_config(dict(DEFAULT_CONFIG)))


def _write_config(config):
    cfg = copy.deepcopy(config)
    try:
        cfg["cache_dir"] = os.path.relpath(cfg["cache_dir"], SCRIPT_DIR)
    except (ValueError, KeyError):
        pass
    os.makedirs(SCRIPT_DIR, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=SCRIPT_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, os.path.join(SCRIPT_DIR, "config.json"))
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def save_config():
    """兼容旧界面；新界面使用会报告失败的 update_config。"""
    try:
        with _config_lock:
            _write_config(_config)
        return True
    except Exception as e:
        log(f"config.json 保存失败: {e}")
        return False


def update_config(patch):
    """验证整份配置并原子保存；失败时不修改内存或原配置文件。"""
    global _config
    if not isinstance(patch, dict):
        raise ValueError("配置更新必须是对象")
    unknown = set(patch) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError("未知配置项: " + ", ".join(sorted(unknown)))
    with _lifecycle_lock, _config_lock, _downloads_lock, _cooldown_lock:
        previous = get_config()
        cfg = _validated_config({**previous, **copy.deepcopy(patch)})
        changed = [name for name in patch if previous.get(name) != cfg[name]]
        if not changed:
            return {"restart_required": False, "changed": []}
        paths_changed = bool({"cache_dir", "library_dir"} & set(changed))
        busy = bool(_downloads_in_progress) or rebuilding() or verifying()
        if paths_changed and (_running or busy):
            raise ValueError("请先停止服务并等待下载或曲库任务结束，再修改保存目录")
        if "enable_youtube_cache" in changed and (_running or busy):
            raise ValueError("请先停止服务并等待后台任务结束，再修改 YouTube 缓存开关")
        if "cooldown_file" in changed and cooldown_active():
            raise ValueError("请先取消 Cool Down，再更换放松视频")
        if cfg["cooldown_file"] and "cooldown_file" in changed:
            if not os.path.isfile(cfg["cooldown_file"]) or os.path.getsize(cfg["cooldown_file"]) == 0:
                raise ValueError("放松视频不存在或为空文件")
        for name in ("cache_dir", "library_dir"):
            if cfg[name]:
                os.makedirs(cfg[name], exist_ok=True)
        _write_config(cfg)
        _config = cfg
    emit("config", changed=changed, restart_required=False)
    emit_stats()
    refresh_queue_state()
    return {"restart_required": False, "changed": changed}


def get_runtime_state():
    with _runtime_lock:
        return copy.deepcopy(_runtime_state)


def _set_runtime_state(**changes):
    with _runtime_lock:
        _runtime_state.update(changes)
        snapshot = copy.deepcopy(_runtime_state)
    emit("state", **snapshot)


def cache_path(filename):
    return os.path.join(_config["cache_dir"], filename)


# ---------------- 编号映射（song_id <-> CDN文件名） ----------------

def load_mapping():
    global _mapping_dirty
    try:
        with open(MAPPING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        with _mapping_lock:
            for k, v in data.items():
                sid = int(k)
                _mapping[sid] = v
                if v:
                    _reverse_mapping[v] = sid
        _mapping_dirty = False
        log(f"已加载编号映射 {len(_mapping)} 条")
    except FileNotFoundError:
        pass
    except Exception as e:
        message = f"mapping.json 读取失败: {e}"
        log(message)
        emit("warning", message=message)


def mark_mapping(song_id, filename):
    global _mapping_dirty
    with _mapping_lock:
        if _mapping.get(song_id) == filename:
            return
        _mapping[song_id] = filename
        if filename:
            _reverse_mapping[filename] = song_id
        _mapping_dirty = True


def flush_mapping():
    global _mapping_dirty
    with _mapping_lock:
        if not _mapping_dirty:
            return
        data = dict(_mapping)
        _mapping_dirty = False
    try:
        with open(MAPPING_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        log(f"mapping.json 保存失败: {e}")


def _mapping_flush_loop():
    while not _stop_event.wait(3):
        flush_mapping()


# ---------------- 曲库 ----------------

def library_enabled():
    d = _config.get("library_dir") or ""
    return bool(d) and os.path.isdir(d)


def library_path(song_id):
    return os.path.join(_config["library_dir"], f"{song_id}.mp4")


def _nonempty_file(path):
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def in_library(song_id):
    if not library_enabled():
        return False
    p = library_path(song_id)
    return _nonempty_file(p)


def local_path_for(filename):
    """按文件名找本地文件：先缓存目录，再按映射查曲库；yt_{id}.mp4 是 YouTube 源歌曲的虚拟文件名"""
    m = YT_FILENAME_RE.match(filename)
    if m:
        return yt_local_path(int(m.group(1)))
    p = cache_path(filename)
    if _nonempty_file(p):
        return p
    with _mapping_lock:
        sid = _reverse_mapping.get(filename)
    if sid is not None and library_enabled():
        p = library_path(sid)
        if _nonempty_file(p):
            return p
    return None


def is_cached(filename):
    """本地可用（缓存目录或曲库）"""
    return local_path_for(filename) is not None


def target_path(filename, song_id=None):
    """新下载的保存位置：曲库模式且"其他视频永久保存"开启时按编号命名进曲库，
    否则进缓存目录按文件名命名"""
    if song_id is None:
        with _mapping_lock:
            song_id = _reverse_mapping.get(filename)
    if song_id is not None and library_enabled() and _config.get("cdn_permanent", False):
        return library_path(song_id)
    return cache_path(filename)


# ---------------- YouTube 源支持 ----------------

def song_route(song_num):
    """返回 (kind, payload)：('cdn', 文件名) / ('yt', YouTube地址) / (None, None) 未知"""
    with _mapping_lock:
        v = _mapping.get(song_num)
    if not v:
        return None, None
    if v.startswith("yt:"):
        return "yt", v[3:]
    return "cdn", v


_yt_online_logged = set()  # 已提示过"本地无文件走在线"的 YouTube 源歌曲


def yt_local_path(song_id):
    """YouTube 源歌曲的本地文件：曲库 {id}.mp4 / 旧版永久目录 yt/{id}.mp4 / 缓存 yt_{id}.mp4"""
    if in_library(song_id):
        return library_path(song_id)
    p = os.path.join(_config["cache_dir"], "yt", f"{song_id}.mp4")
    if _nonempty_file(p):
        return p
    p = cache_path(f"yt_{song_id}.mp4")
    if _nonempty_file(p):
        return p
    return None


def yt_target_path(song_id):
    """录流保存位置：曲库模式且"YouTube永久保存"开启 -> 曲库 {id}.mp4；否则进普通缓存（会被清理）"""
    if library_enabled() and _config.get("youtube_permanent", False):
        return library_path(song_id)
    return cache_path(f"yt_{song_id}.mp4")


# ---------------- WannaDance 源支持（api.udon.dance 302 -> play.udon.dance 文件） ----------------

def wd_cache_name(song_id):
    return f"wd_{song_id}.mp4"


def wd_library_path(song_id):
    return os.path.join(_config.get("library_dir") or "", wd_cache_name(song_id))


def wd_local_path(song_id):
    """WannaDance 本地文件：曲库 wd_{id}.mp4 优先，其次普通缓存目录"""
    if library_enabled():
        p = wd_library_path(song_id)
        if _nonempty_file(p):
            return p
    p = cache_path(wd_cache_name(song_id))
    if _nonempty_file(p):
        return p
    return None


def wd_target_path(song_id):
    """WannaDance 新下载保存位置：曲库模式且"其他视频永久保存"开启时进曲库，否则进普通缓存"""
    if library_enabled() and _config.get("cdn_permanent", False):
        return wd_library_path(song_id)
    return cache_path(wd_cache_name(song_id))


def learn_mapping_from_redirect(song_num, loc):
    """从 /video 的 302 Location 学习映射：CDN 记文件名，YouTube 记 yt:地址"""
    u = urllib.parse.urlparse(loc)
    hn = (u.hostname or "").lower()
    if hn in CDN_HOSTS:
        name = os.path.basename(u.path)
        if FILENAME_RE.match(name):
            mark_mapping(song_num, name)
            log(f"学习到映射 id={song_num} -> {name}")
    elif "youtube.com" in hn or "youtu.be" in hn:
        mark_mapping(song_num, "yt:" + loc)
        log(f"学习到 YouTube 源 id={song_num}: {loc}")


def api_request(path):
    """向真实 api.pypy.dance 发起 HTTP 请求（走真实 IP，绕过本地劫持）。
    返回 (status, {小写头名: 值}, body)"""
    ip = _api_ip()
    if ip:
        conn = _HTTPConnectionIP(ip, 80, timeout=30)
    else:
        conn = http.client.HTTPConnection(API_HOST, 80, timeout=30)
    try:
        conn.request("GET", path, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
        resp = conn.getresponse()
        body = resp.read()
        hdrs = {}
        for k, v in resp.getheaders():
            hdrs.setdefault(k.lower(), v)
        return resp.status, hdrs, body
    finally:
        conn.close()


def get_cache_stats():
    count = total = lib_count = lib_total = 0
    try:
        for name in os.listdir(_config["cache_dir"]):
            p = os.path.join(_config["cache_dir"], name)
            if name.endswith(".mp4") and os.path.isfile(p):
                count += 1
                total += os.path.getsize(p)
    except Exception:
        pass
    if library_enabled():
        try:
            for name in os.listdir(_config["library_dir"]):
                p = os.path.join(_config["library_dir"], name)
                if name.endswith(".mp4") and os.path.isfile(p):
                    lib_count += 1
                    lib_total += os.path.getsize(p)
        except Exception:
            pass
    return count, total, lib_count, lib_total


def emit_stats():
    count, total, lib_count, lib_total = get_cache_stats()
    emit("stats", count=count, total_mb=total / 1048576, limit_mb=int(_config["max_cache_mb"]),
         lib_count=lib_count, lib_mb=lib_total / 1048576, lib_enabled=library_enabled())


def clear_cache():
    """只清理临时缓存，保护在途目标和 .part，并报告实际结果。"""
    result = {"removed_count": 0, "removed_bytes": 0, "skipped_busy": 0,
              "failed_count": 0, "errors": []}
    with _downloads_lock:
        directory = _config["cache_dir"]
        if _same_directory(directory, _config.get("library_dir")):
            raise ValueError("缓存目录与永久曲库相同，已拒绝清理以保护曲库")
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            names = []
        for name in names:
            path = os.path.join(directory, name)
            if not name.endswith((".mp4", ".part")) or not os.path.isfile(path):
                continue
            target = path[:-5] if path.endswith(".part") else path
            if _canonical_path(target) in _downloads_in_progress:
                result["skipped_busy"] += 1
                continue
            try:
                size = os.path.getsize(path)
                os.remove(path)
                result["removed_count"] += 1
                result["removed_bytes"] += size
            except OSError as e:
                result["failed_count"] += 1
                result["errors"].append({"filename": name, "error": str(e)})
    log(f"缓存清理完成：删除 {result['removed_count']} 个，正在下载跳过 {result['skipped_busy']} 个，"
        f"失败 {result['failed_count']} 个（永久曲库保留）")
    emit_stats()
    refresh_queue_state()
    emit("cache_cleared", **result)
    return result


# ---------------- 管理员权限 ----------------

def ensure_admin():
    if os.environ.get("DANCE_CACHE_NO_ADMIN"):
        return True  # 测试用：跳过提权
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return True
    except Exception:
        pass
    log("需要管理员权限，正在请求提权...")
    if getattr(sys, "frozen", False):
        exe = sys.executable
        args = " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        exe = sys.executable
        args = " ".join(f'"{a}"' for a in sys.argv)
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, SCRIPT_DIR, 1)
    if ret <= 32:
        log(f"提权失败(错误码 {ret})，请右键以管理员身份运行。")
        sys.exit(1)
    sys.exit(0)


# ---------------- DNS 解析（劫持前取真实 IP） ----------------

def resolve_real_ips():
    _origin_ips.clear()
    for host in HIJACK_HOSTS + [UDON_API_HOST, UDON_PLAY_HOST]:
        ips = []
        try:
            _, _, ips = __import__("socket").gethostbyname_ex(host)
        except Exception:
            pass
        ips = [ip for ip in ips if _is_public_ip(ip)]
        if not ips:
            ips = [ip for ip in doh_lookup(host) if _is_public_ip(ip)]
        if ips:
            _origin_ips[host] = ips
            log(f"解析 {host} -> {', '.join(ips)}")
        else:
            log(f"警告: 无法解析 {host}，该域名回源将不可用")


DOH_PROVIDERS = (
    "https://dns.alidns.com/resolve",
    "https://dns.google/resolve",
    "https://cloudflare-dns.com/dns-query",
)


def _doh_query(doh, host):
    try:
        ctx = ssl.create_default_context()
        u = urllib.parse.urlparse(doh)
        conn = http.client.HTTPSConnection(u.hostname, 443, timeout=8, context=ctx)
        conn.request("GET", f"{u.path}?name={host}&type=A", headers={"Accept": "application/dns-json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8", "replace"))
        conn.close()
        return [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
    except Exception:
        return []


def doh_lookup(host):
    for doh in DOH_PROVIDERS:
        ips = _doh_query(doh, host)
        if ips:
            return ips
    return []


def _is_public_ip(ip):
    """是否公网 IP（过滤 hosts 劫持产生的 127.0.0.1 等非公网结果）"""
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def gather_candidate_ips(host, rounds=2):
    """系统 DNS + 多个 DoH 服务器多轮查询，合并去重候选公网 IP（不同递归出口常被分配到不同节点组）"""
    ips = []
    try:
        _, _, sys_ips = socket.gethostbyname_ex(host)
        ips.extend(sys_ips)
    except Exception:
        pass
    for _ in range(rounds):
        for doh in DOH_PROVIDERS:
            ips.extend(_doh_query(doh, host))
    seen = set()
    out = []
    for ip in ips:
        if ip not in seen and _is_public_ip(ip):
            seen.add(ip)
            out.append(ip)
    return out


# ---------------- hosts 管理 ----------------

def _hosts_error_reason(e):
    """根据身份和错误码给出 hosts 写入失败的具体原因"""
    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        admin = False
    if not admin:
        return "没有管理员权限，请以管理员身份运行"
    if getattr(e, "winerror", None) == 32:
        return "hosts 文件正被其他程序占用，请关闭加速器/安全软件后重试"
    return ("hosts 文件被锁定或设为只读（可能是安全软件/加速器的防护功能），"
            "请关闭相关防护后重试，或先启动本软件再开加速器")


def clean_stale_hosts():
    with _hosts_lock:
        try:
            with open(HOSTS_PATH, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            kept = [l for l in lines if HOSTS_MARK not in l]
            if len(kept) != len(lines):
                with open(HOSTS_PATH, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                log("已清理上次残留的 hosts 记录")
        except FileNotFoundError:
            pass
        except PermissionError as e:
            log(f"错误: hosts 修改失败（{_hosts_error_reason(e)}）")
            raise


def hijack_hosts(include_api=False):
    with _hosts_lock:
        clean_stale_hosts()
        with open(HOSTS_PATH, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        with open(HOSTS_PATH + ".dance-cache.bak", "w", encoding="utf-8") as f:
            f.write(content)
        hosts = hijack_host_list(include_api)
        with open(HOSTS_PATH, "a", encoding="utf-8") as f:
            if not content.endswith("\n"):
                f.write("\n")
            for host in hosts:
                f.write(f"127.0.0.1 {host} {HOSTS_MARK}\n")
    log("hosts 已劫持: " + ", ".join(hosts))


def restore_hosts():
    try:
        clean_stale_hosts()
        log("hosts 已恢复")
        return True
    except OSError as e:
        log(f"hosts 恢复失败: {e}")
        return False


def _hosts_watchdog_loop(include_api):
    """运行期间定期检查 hosts 劫持条目，被外部清除（如加速器恢复备份）时自动重写"""
    expected = hijack_host_list(include_api)
    while not _stop_event.wait(15):
        try:
            # 与最终恢复串行，防止停止期间在恢复后重新写回劫持条目。
            with _hosts_lock:
                if _stop_event.is_set():
                    return
                with open(HOSTS_PATH, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                marked = [l for l in lines if HOSTS_MARK in l]
                missing = [h for h in expected if not any(h in l for l in marked)]
                if not missing:
                    continue
                kept = [l for l in lines if HOSTS_MARK not in l]
                if kept and not kept[-1].endswith("\n"):
                    kept[-1] += "\n"
                for h in expected:
                    kept.append(f"127.0.0.1 {h} {HOSTS_MARK}\n")
                if _stop_event.is_set():
                    return
                with open(HOSTS_PATH, "w", encoding="utf-8") as f:
                    f.writelines(kept)
            log(f"检测到 hosts 条目被清除（可能是加速器恢复备份），已自动重新写入: {', '.join(missing)}")
        except PermissionError as e:
            log(f"hosts 自动重写失败（{_hosts_error_reason(e)}）")
        except Exception as e:
            log(f"hosts 看门狗异常: {e}")


# ---------------- 源站请求（绕过劫持，用真实 IP + Host 头） ----------------

def _parse_retry_after(value, default):
    if value and value.strip().isdigit():
        return float(value)
    return default


def ordered_ips(host):
    """cdn.pypy.dance 优先使用用户设置的优选 IP，失效时自动回落到默认解析结果"""
    ips = list(_origin_ips.get(host, []))
    pref = (_config.get("preferred_cdn_ip") or "").strip()
    if pref and _is_public_ip(pref) and host == "cdn.pypy.dance":
        ips = [pref] + [ip for ip in ips if ip != pref]
    return ips


def _open_upstream_once(host, path, extra_headers=None, redirects=3):
    """返回 (conn, resp)。手动跟随重定向，目标 host 必须已解析真实 IP。"""
    cur_host, cur_path = host, path
    for _ in range(redirects + 1):
        ips = ordered_ips(cur_host)
        if not ips:
            raise RuntimeError(f"没有 {cur_host} 的真实 IP")
        last_err = None
        last_transient = None
        redirected = False
        for ip in ips:
            conn = None
            try:
                conn = http.client.HTTPConnection(ip, 80, timeout=30)
                headers = {"Host": cur_host, "User-Agent": "stagefright/1.2 (Linux;Android 10)", "Accept-Encoding": "identity"}
                if extra_headers:
                    headers.update(extra_headers)
                conn.request("GET", cur_path, headers=headers)
                resp = conn.getresponse()
                if resp.status in RETRYABLE_STATUS:
                    ra = _parse_retry_after(resp.getheader("Retry-After"), 0.0)
                    resp.read()
                    conn.close()
                    raise TransientUpstream(resp.status, ra)
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.getheader("Location") or ""
                    resp.read()
                    conn.close()
                    u = urllib.parse.urlparse(loc)
                    cur_host = u.hostname
                    cur_path = (u.path or "/") + (("?" + u.query) if u.query else "")
                    if cur_host not in _origin_ips:
                        raise RuntimeError(f"重定向到未支持的域名: {cur_host}")
                    redirected = True
                    break
                return conn, resp
            except TransientUpstream as e:
                last_transient = e  # 该 IP 被限流/临时故障，先换下一个 IP 试试
                continue
            except RuntimeError:
                raise
            except Exception as e:
                last_err = e
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                continue
        if not redirected:
            if last_transient is not None:
                raise last_transient
            raise RuntimeError(f"连接 {cur_host} 失败: {last_err}")
    raise RuntimeError("重定向次数过多")


def open_upstream(host, path, extra_headers=None, redirects=3, retries=5):
    """带限流/临时错误退避重试的源站请求"""
    last_err = None
    for attempt in range(retries):
        wait_rate_limit()
        if _stop_event.is_set():
            raise RuntimeError("已停止")
        try:
            return _open_upstream_once(host, path, extra_headers, redirects)
        except TransientUpstream as e:
            last_err = e
            backoff = max(e.retry_after, min(15 * (attempt + 1), 120))
            if e.status == 429:
                note_rate_limited(backoff)
                log(f"源站限流(429)，{backoff:.0f} 秒后重试 ({attempt + 1}/{retries})")
            else:
                log(f"源站临时错误 HTTP {e.status}，{backoff:.0f} 秒后重试 ({attempt + 1}/{retries})")
                _stop_event.wait(backoff)
        except Exception as e:
            last_err = e
            log(f"源站请求异常: {e}，稍后重试 ({attempt + 1}/{retries})")
            _stop_event.wait(min(3 * (attempt + 1), 15))
    raise RuntimeError(f"重试 {retries} 次后仍失败: {last_err}")


class _HTTPConnectionIP(http.client.HTTPConnection):
    """连接真实 IP，但 Host 头仍用域名（api 被 hosts 劫持后必须绕开本地回环）"""

    def __init__(self, ip, port=80, **kw):
        self._ip = ip
        super().__init__(API_HOST, port, **kw)

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout, self.source_address)


class _HTTPSConnectionIP(http.client.HTTPSConnection):
    """连接真实 IP，TLS 握手/SNI 仍用域名，证书校验不受影响"""

    def __init__(self, ip, port=443, **kw):
        self._ip = ip
        super().__init__(API_HOST, port, **kw)

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _api_ip():
    ips = _origin_ips.get(API_HOST) or []
    return ips[0] if ips else None


_api_conns = threading.local()


def _api_conn():
    """线程本地复用的 API 连接。api.pypy.dance 支持 HTTP 直连，省去 TLS 握手；
    若 HTTP 失败则自动降级为 HTTPS。一律走真实 IP，避免劫持后回环到自己。"""
    conn = getattr(_api_conns, "conn", None)
    if conn is None:
        ip = _api_ip()
        if getattr(_api_conns, "use_https", False):
            ctx = ssl.create_default_context()
            if ip:
                conn = _HTTPSConnectionIP(ip, 443, timeout=15, context=ctx)
            else:
                conn = http.client.HTTPSConnection(API_HOST, 443, timeout=15, context=ctx)
        else:
            if ip:
                conn = _HTTPConnectionIP(ip, 80, timeout=15)
            else:
                conn = http.client.HTTPConnection(API_HOST, 80, timeout=15)
        _api_conns.conn = conn
    return conn


def resolve_video_filename(song_num):
    """api.pypy.dance/video?id=N -> 302 -> 提取 CDN 文件名；YouTube 源返回 None。
    429/网络错误会退避重试；失败时不写入映射（避免把限流误判成非CDN源）。
    旧版本标记的 ""（非CDN源）会重新解析一次，以便学习 YouTube 源地址。"""
    with _resolved_lock:
        if song_num in _resolved_ids:
            return _resolved_ids[song_num]
    with _mapping_lock:
        v = _mapping.get(song_num)
    if v:
        fn = None if v.startswith("yt:") else v
        with _resolved_lock:
            _resolved_ids[song_num] = fn
        return fn
    filename = None
    answered = False
    retries = 4
    for attempt in range(retries):
        wait_rate_limit()
        if _stop_event.is_set():
            return None
        try:
            conn = _api_conn()
            conn.request("GET", f"/video?id={song_num}", headers={"User-Agent": "Mozilla/5.0"})
            resp = conn.getresponse()
            status = resp.status
            loc = resp.getheader("Location") or ""
            ra = _parse_retry_after(resp.getheader("Retry-After"), 0.0)
            resp.read()
        except Exception as e:
            old_conn = getattr(_api_conns, "conn", None)
            _api_conns.conn = None  # 连接可能已失效，丢弃后重建
            if old_conn is not None and not getattr(_api_conns, "use_https", False):
                _api_conns.use_https = True  # HTTP 失败，降级为 HTTPS 重试
            log(f"解析视频地址失败 id={song_num}: {e} ({attempt + 1}/{retries})")
            _stop_event.wait(min(3 * (attempt + 1), 15))
            continue
        if status in RETRYABLE_STATUS:
            backoff = max(ra, min(20 * (attempt + 1), 120))
            note_rate_limited(backoff)
            log(f"API 限流 HTTP {status} id={song_num}，{backoff:.0f} 秒后重试 ({attempt + 1}/{retries})")
            continue
        answered = True
        if status in (301, 302, 303, 307, 308):
            u = urllib.parse.urlparse(loc)
            hn = (u.hostname or "").lower()
            if hn in CDN_HOSTS:
                name = os.path.basename(u.path)
                if FILENAME_RE.match(name):
                    filename = name
                mark_mapping(song_num, filename or "")
            elif "youtube.com" in hn or "youtu.be" in hn:
                mark_mapping(song_num, "yt:" + loc)
                log(f"歌曲 id={song_num} 为 YouTube 源: {loc}")
            else:
                mark_mapping(song_num, "")
        else:
            log(f"解析视频地址 id={song_num} 返回 HTTP {status}")
        break
    if answered:
        # 得到明确答复就缓存结果（含 None），本会话内不再重复探测同一首
        with _resolved_lock:
            _resolved_ids[song_num] = filename
    return filename


# ---------------- 下载 ----------------

_progress_last = {}


def emit_progress(filename, done, total, task_id=None):
    now = time.time()
    key = task_id or filename
    with _downloads_lock:
        meta = _download_meta.get(task_id)
        if meta is not None:
            meta.update(done=done, total=total)
        data = dict(meta or {"filename": filename, "task_id": task_id})
        should_emit = (total > 0 and done >= total) or now - _progress_last.get(key, 0) >= 0.5
        if should_emit:
            _progress_last[key] = now
    if should_emit:
        data.update(filename=filename, done=done, total=total)
        emit("progress", **data)


def _cancelled(run_id):
    return _stop_event.is_set() or run_id != _run_id


def _begin_download(target, filename, song_id=None, source="cdn"):
    key = _canonical_path(target)
    if song_id is None:
        with _mapping_lock:
            song_id = _reverse_mapping.get(filename)
    meta = {
        "task_id": key, "filename": filename, "song_id": song_id,
        "source": source, "target_path": target,
        "storage": "library" if _same_directory(os.path.dirname(target), _config.get("library_dir")) else "cache",
        "done": 0, "total": 0,
    }
    with _downloads_lock:
        if _stop_event.is_set() or key in _downloads_in_progress:
            return None
        _downloads_in_progress[key] = threading.current_thread()
        _download_meta[key] = dict(meta)
        _download_results.pop(key, None)
    emit("download_start", **meta)
    refresh_queue_state()
    return meta


def get_downloads_snapshot():
    with _downloads_lock:
        return copy.deepcopy(list(_download_meta.values()))


def _finish_download(meta, ok, cancelled, error, done, total):
    data = {**meta, "ok": bool(ok), "cancelled": bool(cancelled),
            "error": error, "done": done, "total": total}
    key = meta["task_id"]
    with _downloads_lock:
        _downloads_in_progress.pop(key, None)
        _download_meta.pop(key, None)
        _progress_last.pop(key, None)
        _download_results[key] = dict(data)
        while len(_download_results) > 200:
            _download_results.pop(next(iter(_download_results)))
    cleanup_cache()
    emit("download_done", **data)
    emit_stats()


def _commit_download(part, target, size):
    try:
        os.replace(part, target)
    except OSError:
        # Windows 播放器可能持有已有完整文件；不能把不存在的目标当作成功。
        if not os.path.isfile(target) or os.path.getsize(target) != size:
            raise
        log(f"写入 {os.path.basename(target)} 时文件被占用，使用已有同大小文件")
        os.remove(part)


def _remove_part(part):
    try:
        os.remove(part)
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"无法删除临时文件 {os.path.basename(part)}: {e}")


def touch(path):
    try:
        os.utime(path)
    except Exception:
        pass


def download_file(filename, song_id=None):
    """完整下载；只有正常读至 EOF 且长度匹配才发布成功。"""
    target = target_path(filename, song_id)
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return True
    run_id = _run_id
    meta = _begin_download(target, filename, song_id)
    if meta is None:
        return False
    part = target + ".part"
    ok = False
    error = None
    done = total = 0
    conn = None
    eof = False
    try:
        conn, resp = open_upstream("cdn.pypy.dance", "/" + filename)
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        total = int(resp.getheader("Content-Length") or 0)
        with open(part, "wb") as f:
            while not _cancelled(run_id):
                chunk = resp.read(256 * 1024)
                if not chunk:
                    eof = True
                    break
                f.write(chunk)
                done += len(chunk)
                emit_progress(filename, done, total, meta["task_id"])
        if not _cancelled(run_id) and eof and done > 0 and (total == 0 or done == total):
            _commit_download(part, target, done)
            ok = True
            log(f"缓存完成 {os.path.basename(target)} ({done / 1048576:.1f} MB)")
        elif not _cancelled(run_id):
            error = f"下载不完整 ({done}/{total})"
            log(f"{filename}: {error}")
    except Exception as e:
        error = str(e)
        log(f"下载 {filename} 出错: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        cancelled = not ok and _cancelled(run_id)
        if not ok:
            _remove_part(part)
        _finish_download(meta, ok, cancelled, None if cancelled else error, done, total)
    return ok


def start_background_download(filename, song_id=None):
    if not filename:
        return
    target = target_path(filename, song_id)
    if os.path.exists(target) and os.path.getsize(target) > 0:
        return
    with _downloads_lock:
        if _canonical_path(target) in _downloads_in_progress:
            return
    threading.Thread(target=download_file, args=(filename, song_id), daemon=True).start()


def download_wd(song_id):
    """WannaDance 完整下载：api.udon.dance/Api/Songs/play?id=N 302 到 play.udon.dance 文件。
    文件名本身带编号，无需映射表。"""
    target = wd_target_path(song_id)
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return True
    name = wd_cache_name(song_id)
    run_id = _run_id
    meta = _begin_download(target, name, None, "wannadance")
    if meta is None:
        return False
    part = target + ".part"
    ok = False
    error = None
    done = total = 0
    conn = None
    eof = False
    try:
        conn, resp = open_upstream(UDON_API_HOST, f"/Api/Songs/play?id={song_id}")
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        total = int(resp.getheader("Content-Length") or 0)
        with open(part, "wb") as f:
            while not _cancelled(run_id):
                chunk = resp.read(256 * 1024)
                if not chunk:
                    eof = True
                    break
                f.write(chunk)
                done += len(chunk)
                emit_progress(name, done, total, meta["task_id"])
        if not _cancelled(run_id) and eof and done > 0 and (total == 0 or done == total):
            _commit_download(part, target, done)
            ok = True
            log(f"缓存完成 {os.path.basename(target)} ({done / 1048576:.1f} MB)")
        elif not _cancelled(run_id):
            error = f"下载不完整 ({done}/{total})"
            log(f"{name}: {error}")
    except Exception as e:
        error = str(e)
        log(f"下载 {name} 出错: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        cancelled = not ok and _cancelled(run_id)
        if not ok:
            _remove_part(part)
        _finish_download(meta, ok, cancelled, None if cancelled else error, done, total)
    return ok


def start_wd_download(song_id):
    target = wd_target_path(song_id)
    if os.path.exists(target) and os.path.getsize(target) > 0:
        return
    with _downloads_lock:
        if _canonical_path(target) in _downloads_in_progress:
            return
    threading.Thread(target=download_wd, args=(song_id,), daemon=True).start()


# ---------------- YouTube 源录流（从 VRChat 日志捕获播放中的直链） ----------------
# VRChat 播放 YouTube 源时会用自己的 yt-dlp 解析出 googlevideo 直链并写进日志：
#   [Video Playback] URL 'http://api.pypy.dance/video?id=N' resolved to 'https://...googlevideo.com/...'
# 监听到就直接下载该直链保存（不碰 YouTube 解析，无机器人验证问题）

RESOLVED_RE = re.compile(
    r"URL 'http://api\.pypy\.dance/video\?id=(\d+)' resolved to '(https://[^']+)'")


def scan_resolved_youtube(lines):
    """扫描日志行，发现播放中的 YouTube 源直链就开始录流"""
    for line in lines:
        m = RESOLVED_RE.search(line)
        if not m:
            continue
        url = m.group(2)
        if "googlevideo.com" not in url:
            continue
        start_googlevideo_download(int(m.group(1)), url)


def download_googlevideo(song_id, url):
    """保存播放中捕获的直链；取消或不完整时不会冒充可用本地视频。"""
    target = yt_target_path(song_id)
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return True
    run_id = _run_id
    name = os.path.basename(target)
    meta = _begin_download(target, name, song_id, "youtube")
    if meta is None:
        return False
    part = target + ".part"
    ok = False
    error = None
    done = total = 0
    eof = False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        log(f"捕获到播放中的 YouTube 源 id={song_id}，开始录制...")
        with urllib.request.urlopen(req, timeout=30) as r:
            status = getattr(r, "status", 200)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            total = int(r.headers.get("Content-Length") or 0)
            with open(part, "wb") as f:
                while not _cancelled(run_id):
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        eof = True
                        break
                    f.write(chunk)
                    done += len(chunk)
                    emit_progress(name, done, total, meta["task_id"])
        if not _cancelled(run_id) and eof and done > 0 and (total == 0 or done == total):
            _commit_download(part, target, done)
            ok = True
            log(f"YouTube 源录制完成 {name} ({done / 1048576:.1f} MB)")
        elif not _cancelled(run_id):
            error = f"录制不完整 ({done}/{total})，下次播放时重试"
            log(f"YouTube 源 id={song_id}: {error}")
    except Exception as e:
        error = str(e)
        log(f"YouTube 源录制失败 id={song_id}: {e}")
    finally:
        cancelled = not ok and _cancelled(run_id)
        if not ok:
            _remove_part(part)
        _finish_download(meta, ok, cancelled, None if cancelled else error, done, total)
    return ok


def start_googlevideo_download(song_id, url):
    if yt_local_path(song_id):
        return
    target = yt_target_path(song_id)
    with _downloads_lock:
        if _canonical_path(target) in _downloads_in_progress:
            return
    threading.Thread(target=download_googlevideo, args=(song_id, url), daemon=True).start()


# ---------------- 缓存目录清理 (LRU，曲库目录不清理) ----------------

def cleanup_cache():
    # 与登记下载/手工清理共用锁，避免检查 busy 后又有新任务开始写入。
    with _downloads_lock:
        try:
            directory = _config["cache_dir"]
            if _same_directory(directory, _config.get("library_dir")):
                return
            limit = int(_config["max_cache_mb"]) * 1048576
            files = []
            total = 0
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                if name.endswith(".mp4") and os.path.isfile(path):
                    st = os.stat(path)
                    files.append((st.st_mtime, st.st_size, path))
                    total += st.st_size
            for _, size, path in sorted(files):
                if total <= limit:
                    break
                if _canonical_path(path) in _downloads_in_progress:
                    continue
                try:
                    os.remove(path)
                    total -= size
                    log(f"缓存超限，清理 {os.path.basename(path)}")
                except OSError:
                    pass
        except (OSError, KeyError, ValueError):
            pass


# ---------------- 曲库任务取消（重建映射/校验共用） ----------------

def cancel_library_download():
    _library_stop.set()


# ---------------- 映射重建 ----------------

_rebuild_thread = None


def _start_library_task(kind, worker):
    global _rebuild_thread, _verify_thread
    with _lifecycle_lock, _config_lock, _library_task_lock:
        if not library_enabled():
            log("请先设置有效的曲库目录")
            return False
        if rebuilding() or verifying():
            log("已有曲库任务正在进行，请等待完成或取消后再试")
            return False
        if _stop_event.is_set():
            log("服务已停止，请重新启动服务后再运行曲库任务")
            return False
        _library_stop.clear()
        thread = threading.Thread(target=worker, daemon=True)
        if kind == "rebuild":
            _rebuild_thread = thread
        else:
            _verify_thread = thread
        thread.start()
    return True


def rebuild_mapping():
    """只补全本地曲库缺少的映射；与校验任务互斥。"""
    return _start_library_task("rebuild", _rebuild_worker)


def rebuilding():
    return _rebuild_thread is not None and _rebuild_thread.is_alive()


# ---------------- 曲库完整性校验 ----------------

_verify_thread = None


def verify_library():
    """对比远端文件大小，仅报告不一致，不自动删除或重新下载。"""
    return _start_library_task("verify", _verify_worker)


def verifying():
    return _verify_thread is not None and _verify_thread.is_alive()


def _remote_size(filename):
    conn, resp = open_upstream("cdn.pypy.dance", "/" + filename,
                               extra_headers={"Range": "bytes=0-0"}, retries=2)
    try:
        if resp.status == 200:
            value = resp.getheader("Content-Length")
            return int(value) if value else None
        if resp.status != 206:
            return None
        match = re.search(r"/(\d+)\s*$", resp.getheader("Content-Range") or "")
        return int(match.group(1)) if match else None
    finally:
        # 服务器忽略 Range 时不要为校验而读取整个视频。
        conn.close()


def _verify_worker():
    checked = bad = failed = skipped = 0
    bad_ids = []
    total = 0
    error = None
    try:
        directory = _config["library_dir"]
        ids = sorted({int(name[:-4]) for name in os.listdir(directory)
                      if re.fullmatch(r"\d+\.mp4", name)})
        total = len(ids)
        log(f"开始校验曲库文件大小，共 {total} 个文件...")
        emit("verify_progress", checked=0, bad=0, failed=0, skipped=0, total=total, current="")
        lock = threading.Lock()

        def work(sid):
            nonlocal checked, bad, failed, skipped
            if _library_stop.is_set() or _stop_event.is_set():
                return
            filename = resolve_video_filename(sid)
            if _library_stop.is_set() or _stop_event.is_set():
                return
            problem = None
            is_youtube = song_route(sid)[0] == "yt"
            if filename:
                try:
                    local_size = os.path.getsize(os.path.join(directory, f"{sid}.mp4"))
                    remote_size = _remote_size(filename)
                    if remote_size is None:
                        problem = "failed"
                    elif local_size != remote_size:
                        problem = "bad"
                        log(f"校验不一致 {sid}.mp4: 本地 {local_size} / 远端 {remote_size}（仅报告，未修改文件）")
                except Exception:
                    problem = "failed"
            else:
                problem = "skipped" if is_youtube else "failed"
            with lock:
                checked += 1
                if problem == "bad":
                    bad += 1
                    bad_ids.append(sid)
                elif problem == "failed":
                    failed += 1
                elif problem == "skipped":
                    skipped += 1
                progress = dict(checked=checked, bad=bad, failed=failed, skipped=skipped,
                                total=total, current=str(sid))
            emit("verify_progress", **progress)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(work, ids))
        flush_mapping()
    except Exception as e:
        error = str(e)
        log(f"曲库校验出错: {e}")
    cancelled = _library_stop.is_set() or _stop_event.is_set()
    log(("曲库校验已取消。" if cancelled else "曲库校验完成。")
        + f"已检查 {checked}/{total}，不一致 {bad}，YouTube 跳过 {skipped}，失败 {failed}")
    emit("verify_done", total=total, checked=checked, bad=bad, skipped=skipped, failed=failed,
         bad_ids=sorted(bad_ids), cancelled=cancelled, error=error)
    emit_stats()


def _rebuild_worker():
    done = failed = total = 0
    error = None
    try:
        ids = sorted({int(name[:-4]) for name in os.listdir(_config["library_dir"])
                      if re.fullmatch(r"\d+\.mp4", name)})
        with _mapping_lock:
            todo = [sid for sid in ids if not _mapping.get(sid)]
        total = len(todo)
        log(f"曲库共 {len(ids)} 个视频，其中 {total} 个需要补全映射")
        emit("rebuild_progress", done=0, failed=0, total=total, current="")
        lock = threading.Lock()

        def work(sid):
            nonlocal done, failed
            if _library_stop.is_set() or _stop_event.is_set():
                return
            resolve_video_filename(sid)
            if _library_stop.is_set() or _stop_event.is_set():
                return
            known = song_route(sid)[0] in ("cdn", "yt")
            with lock:
                done += int(known)
                failed += int(not known)
                progress = dict(done=done, failed=failed, total=total, current=str(sid))
            emit("rebuild_progress", **progress)

        with ThreadPoolExecutor(max_workers=16) as executor:
            list(executor.map(work, todo))
        flush_mapping()
    except Exception as e:
        error = str(e)
        log(f"映射重建出错: {e}")
    cancelled = _library_stop.is_set() or _stop_event.is_set()
    log(("映射重建已取消。" if cancelled else "映射重建完成。") + f" 成功 {done}，失败 {failed}")
    emit("rebuild_done", done=done, failed=failed, total=total, cancelled=cancelled, error=error)
    refresh_queue_state()


# ---------------- CDN IP 测速 ----------------

SPEEDTEST_BYTES = 8 * 1048576


def _pick_test_file():
    with _mapping_lock:
        for v in _mapping.values():
            if v and not v.startswith("yt:"):
                return v
    return "KhlUAAndf98.mp4"


def _download_test(ip, filename, nbytes, timeout=20):
    """从指定 CF IP 下载 nbytes 测速，返回 (MB/s, None) 或 (None, 错误)"""
    try:
        conn = http.client.HTTPConnection(ip, 80, timeout=timeout)
        t0 = time.time()
        conn.request("GET", "/" + filename, headers={
            "Host": "cdn.pypy.dance",
            "Range": f"bytes=0-{nbytes - 1}",
            "Accept-Encoding": "identity",
        })
        resp = conn.getresponse()
        if resp.status not in (200, 206):
            err = f"HTTP {resp.status}"
            resp.read()
            conn.close()
            return None, err
        got = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            got += len(chunk)
        dt = max(time.time() - t0, 0.01)
        conn.close()
        return got / 1048576 / dt, None
    except Exception as e:
        return None, str(e)


def speed_test_ips():
    """快速扫描：合并系统 DNS 和多个 DoH 服务器解析到的候选节点，各下载 8MB 测速"""
    filename = _pick_test_file()
    candidates = gather_candidate_ips("cdn.pypy.dance")
    pref = (_config.get("preferred_cdn_ip") or "").strip()
    if pref and _is_public_ip(pref):
        candidates = [pref] + [ip for ip in candidates if ip != pref]
    candidates = candidates[:16]
    if not candidates:
        log("测速失败: 无法解析 CDN IP，请检查网络（如开着代理请关闭后重试）")
        emit("speedtest_done")
        return
    log(f"快速扫描开始（系统DNS+DoH合并共 {len(candidates)} 个节点，每个下载 {SPEEDTEST_BYTES // 1048576}MB 取样）...")
    results = []
    for ip in candidates:
        mbps, err = _download_test(ip, filename, SPEEDTEST_BYTES)
        results.append({"ip": ip, "mbps": mbps, "error": err})
        if mbps is not None:
            log(f"测速 {ip}: {mbps:.2f} MB/s")
        else:
            log(f"测速 {ip}: 失败 ({err})")
        emit("speedtest_result", ip=ip, mbps=mbps, error=err)
    emit("speedtest_done", results=results)


# Cloudflare 官方 IPv4 段（https://www.cloudflare.com/ips-v4）
CF_IP_RANGES = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/12",
    "172.64.0.0/13", "131.0.72.0/22",
]

FULLSCAN_SAMPLES = 300            # 从全段随机抽样探测的 IP 数
FULLSCAN_TOP = 10                 # 初筛后进入完整测速的数量
FULLSCAN_PROBE_BYTES = 64 * 1024  # 初筛每个 IP 的下载量


def _sample_cf_ips(n):
    """按段大小加权，从 Cloudflare 全部 IPv4 段随机抽 n 个不重复 IP"""
    nets = [ipaddress.ip_network(r) for r in CF_IP_RANGES]
    weights = [net.num_addresses for net in nets]
    ips = set()
    while len(ips) < n:
        net = random.choices(nets, weights=weights)[0]
        ip = net.network_address + random.randrange(net.num_addresses)
        ips.add(str(ipaddress.ip_address(ip)))
    return list(ips)


def full_scan_ips():
    """全部扫描：从 Cloudflare 全段随机抽样快速探测，筛出最快的一批再做 8MB 下载测速"""
    filename = _pick_test_file()
    samples = _sample_cf_ips(FULLSCAN_SAMPLES)
    log(f"全部扫描：从 Cloudflare 全段随机抽 {len(samples)} 个 IP 初筛（每个试下 {FULLSCAN_PROBE_BYTES // 1024}KB）...")
    probes = []
    done = 0
    with ThreadPoolExecutor(max_workers=50) as pool:
        futures = {pool.submit(_download_test, ip, filename, FULLSCAN_PROBE_BYTES, 4): ip for ip in samples}
        for fut in as_completed(futures):
            mbps, _ = fut.result()
            done += 1
            if mbps is not None:
                probes.append({"ip": futures[fut], "mbps": mbps})
            if done % 50 == 0 or done == len(samples):
                log(f"全部扫描初筛进度: {done}/{len(samples)}（可用 {len(probes)}）")
    if not probes:
        log("全部扫描失败: 抽样 IP 全部不可用，请检查网络（如开着代理请关闭后重试）")
        emit("speedtest_done")
        return
    top = sorted(probes, key=lambda r: -r["mbps"])[:FULLSCAN_TOP]
    log(f"初筛完成，对最快 {len(top)} 个节点做 {SPEEDTEST_BYTES // 1048576}MB 完整测速...")
    results = []
    for r in top:
        mbps, err = _download_test(r["ip"], filename, SPEEDTEST_BYTES)
        results.append({"ip": r["ip"], "mbps": mbps, "error": err})
        if mbps is not None:
            log(f"测速 {r['ip']}: {mbps:.2f} MB/s")
        else:
            log(f"测速 {r['ip']}: 失败 ({err})")
        emit("speedtest_result", ip=r["ip"], mbps=mbps, error=err)
    emit("speedtest_done", results=results)


# ---------------- 本地 HTTP 服务 ----------------

def parse_range(header, size):
    try:
        units, rng = header.split("=", 1)
        if units.strip() != "bytes":
            return None
        start_s, end_s = rng.split("-", 1)
        if start_s.strip() == "":
            length = int(end_s)
            if length <= 0:
                return None
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s.strip() else size - 1
        if start > end or start >= size:
            return None
        return start, min(end, size - 1)
    except Exception:
        return None


class DanceHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DanceCache/2.0"

    def log_message(self, fmt, *args):
        pass

    def _host(self):
        return self.headers.get("Host", "").split(":", 1)[0].strip().lower()

    def _filename(self):
        path = urllib.parse.urlparse(self.path).path
        name = os.path.basename(path)
        return name if FILENAME_RE.match(name) else None

    def do_HEAD(self):
        if self._host() == API_HOST:
            return self._handle_api(head_only=True)
        if self._host() == UDON_PLAY_HOST:
            return self._handle_udon(head_only=True)
        name = self._filename()
        if not name:
            self.send_error(404)
            return
        p = cooldown_check(name) or local_path_for(name)
        if p:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(os.path.getsize(p)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
        else:
            self._proxy(head_only=True)

    def do_GET(self):
        if self._host() == API_HOST:
            return self._handle_api()
        if self._host() == UDON_PLAY_HOST:
            return self._handle_udon()
        name = self._filename()
        if not name:
            self.send_error(404)
            return
        replacement = cooldown_check(name)
        p = replacement or local_path_for(name)
        if p:
            log(f"本地命中 {name} -> {os.path.basename(p)}")
            if not replacement:
                touch(p)
            self._serve_file(p)
        else:
            log(f"本地未命中 {name}，回源拉取")
            self._proxy()

    # ---------------- play.udon.dance（WannaDance）处理 ----------------

    def _handle_udon(self, head_only=False):
        """play.udon.dance/files/XXXX/{id}-{hash}.mp4?e=..&s=.. -> wd_{id}.mp4 本地缓存"""
        path = urllib.parse.urlparse(self.path).path
        m = WD_FILE_RE.match(os.path.basename(path))
        if not m:
            self.send_error(404)
            return
        sid = int(m.group(1))
        name = wd_cache_name(sid)
        if head_only:
            p = cooldown_check(name) or wd_local_path(sid)
            if p:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(os.path.getsize(p)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
            else:
                self._proxy(head_only=True, upstream_host=UDON_PLAY_HOST,
                            upstream_path=self.path, name=name, wd_id=sid)
            return
        replacement = cooldown_check(name)
        p = replacement or wd_local_path(sid)
        if p:
            log(f"本地命中 {name} (WannaDance) -> {os.path.basename(p)}")
            if not replacement:
                touch(p)
            self._serve_file(p)
        else:
            log(f"本地未命中 {name} (WannaDance)，回源拉取")
            self._proxy(upstream_host=UDON_PLAY_HOST, upstream_path=self.path,
                        name=name, wd_id=sid)

    # ---------------- api.pypy.dance 处理 ----------------

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_api(self, head_only=False):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/video":
            sid_s = (urllib.parse.parse_qs(u.query).get("id") or [""])[0]
            if sid_s.isdigit():
                return self._handle_video_redirect(int(sid_s), head_only)
        self._api_passthrough(head_only)

    def _handle_video_redirect(self, sid, head_only=False):
        """/video?id=N 三分支：CDN 源直接给文件名；YouTube 源已入库给本地地址；
        未知/未入库透传真实 API（顺手学习映射）"""
        kind, payload = song_route(sid)
        if kind == "cdn":
            return self._redirect(f"http://cdn.pypy.dance/{payload}")
        if kind == "yt":
            if yt_local_path(sid):
                log(f"API 重定向 id={sid} -> 本地 YouTube 缓存 yt_{sid}.mp4")
                return self._redirect(f"http://cdn.pypy.dance/yt_{sid}.mp4")
            # 未入库：本次走 YouTube 在线播放；VRChat 解析出直链后会被日志监听器捕获录制
            if sid not in _yt_online_logged:
                _yt_online_logged.add(sid)
                log(f"YouTube 源 id={sid} 未入库，本次在线播放，播放时自动录制入库")
            return self._redirect(payload)
        self._api_passthrough(head_only, sid)

    def _api_passthrough(self, head_only=False, sid=None):
        """原样透传到真实 api.pypy.dance；/video 的 302 响应顺手学习映射"""
        try:
            status, hdrs, body = api_request(self.path)
        except Exception as e:
            log(f"API 透传失败 {self.path}: {e}")
            try:
                self.send_error(502, f"upstream error: {e}")
            except Exception:
                pass
            return
        if sid is not None and status in (301, 302, 303, 307, 308):
            loc = hdrs.get("location", "")
            if loc:
                learn_mapping_from_redirect(sid, loc)
        try:
            self.send_response(status)
            skip = {"transfer-encoding", "connection", "keep-alive", "server", "date", "content-length"}
            for k, v in hdrs.items():
                if k not in skip:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _serve_file(self, path):
        size = os.path.getsize(path)
        range_header = self.headers.get("Range")
        rng = parse_range(range_header, size) if range_header else None
        try:
            if rng:
                start, end = rng
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                start, length = 0, size
                self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _proxy(self, head_only=False, upstream_host="cdn.pypy.dance", upstream_path=None, name=None, wd_id=None):
        if name is None:
            name = self._filename()
        if upstream_path is None:
            upstream_path = "/" + name
        run_id = _run_id
        range_header = self.headers.get("Range")
        extra = {"Range": range_header} if range_header else None
        conn = None
        meta = None
        part = None
        ok = False
        saved = total = 0
        error = None
        try:
            conn, resp = open_upstream(upstream_host, upstream_path, extra_headers=extra)
            if _cancelled(run_id):
                return
            self.send_response(resp.status)
            for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified"):
                value = resp.getheader(header)
                if value is not None:
                    self.send_header(header, value)
            if resp.getheader("Accept-Ranges") is None:
                self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if head_only:
                return

            # 错误正文与服务器未按完整请求返回的片段只透传，绝不登记为缓存。
            if range_header and resp.status in (200, 206):
                if wd_id is not None:
                    start_wd_download(wd_id)
                else:
                    start_background_download(name)
            elif not range_header and resp.status == 200:
                target = wd_target_path(wd_id) if wd_id is not None else target_path(name)
                total = int(resp.getheader("Content-Length") or 0)
                meta = _begin_download(target, name)
            if meta is None:
                while not _cancelled(run_id):
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                return

            part = target + ".part"
            client_alive = True
            eof = False
            with open(part, "wb") as f:
                while not _cancelled(run_id):
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        eof = True
                        break
                    f.write(chunk)
                    saved += len(chunk)
                    emit_progress(name, saved, total, meta["task_id"])
                    if client_alive:
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                            client_alive = False
            if not _cancelled(run_id) and eof and saved > 0 and (total == 0 or saved == total):
                _commit_download(part, target, saved)
                ok = True
                log(f"边下边存完成 {os.path.basename(target)} ({saved / 1048576:.1f} MB)")
            elif not _cancelled(run_id):
                error = f"下载不完整 ({saved}/{total})"
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
            error = str(e) or "连接已中断"
        except Exception as e:
            error = str(e)
            log(f"代理 {name} 出错: {e}")
            if conn is None:
                try:
                    self.send_error(502, "upstream unavailable")
                except Exception:
                    pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if meta is not None:
                cancelled = not ok and _cancelled(run_id)
                if not ok and part:
                    _remove_part(part)
                _finish_download(meta, ok, cancelled, None if cancelled else error, saved, total)


# ---------------- 443 TCP 隧道（api.pypy.dance 的 https 请求原样透传） ----------------

_tunnel_sock = None


def _tunnel_handle(client, ips):
    up = None
    run_id = _run_id
    try:
        for ip in ips:
            try:
                up = socket.create_connection((ip, 443), timeout=10)
                break
            except OSError:
                continue
        if up is None:
            return
        pair = [client, up]
        last_activity = time.monotonic()
        while not _cancelled(run_id):
            r, _, _ = select.select(pair, [], [], 1)
            if not r:
                if time.monotonic() - last_activity >= 120:
                    return
                continue
            if _cancelled(run_id):
                return
            last_activity = time.monotonic()
            for s in r:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    (up if s is client else client).sendall(data)
                except OSError:
                    return
    finally:
        try:
            client.close()
        except Exception:
            pass
        if up is not None:
            try:
                up.close()
            except Exception:
                pass


def _tunnel_loop(sock, ips):
    while not _stop_event.is_set():
        try:
            client, _ = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=_tunnel_handle, args=(client, ips), daemon=True).start()


# ---------------- VRChat 日志监听 ----------------

def find_newest_log():
    try:
        candidates = [
            os.path.join(VRC_LOG_DIR, n)
            for n in os.listdir(VRC_LOG_DIR)
            if n.startswith("output_log") and n.endswith(".txt")
        ]
    except FileNotFoundError:
        return None
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def extract_queue_items(lines):
    """从日志行中取最后一条队列 JSON（PyPyDance 或 WannaDance，后出现的生效）"""
    items = None
    for line in lines:
        content = line.split("  -  ", 1)[-1].strip()
        m = QUEUE_RE.match(content)
        if m:
            try:
                items = json.loads(m.group(1))
            except Exception:
                pass
            continue
        m = WD_QUEUE_RE.search(content)
        if m:
            try:
                raw = json.loads(m.group(1))
                items = [_normalize_wd_item(x) for x in raw if isinstance(x, dict)]
            except Exception:
                pass
    return items


def _normalize_wd_item(x):
    """WannaDance 队列项 -> 内部统一结构（wd=True 标记，songNum 即 WannaDance 歌曲编号）"""
    players = x.get("playerNames") or []
    return {
        "songNum": x.get("songId"),
        "videoName": x.get("title") or "?",
        "playerName": ", ".join(str(p) for p in players),
        "wd": True,
    }


def describe_queue(items):
    """纯本地快照；不为显示队列发起解析请求，也不把未缓存误报为下载中。"""
    with _downloads_lock:
        tasks = copy.deepcopy(list(_download_meta.values()))
        outcomes = copy.deepcopy(list(_download_results.values()))
    cooldown = get_cooldown_state()
    result = []
    for index, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        sn = it.get("songNum", 0)
        valid_id = isinstance(sn, int) and not isinstance(sn, bool) and sn > 0
        if it.get("wd"):
            # WannaDance：文件名即编号，无需映射表
            kind, payload = ("cdn", wd_cache_name(sn)) if valid_id else (None, None)
            filename = payload
            source = "wannadance" if valid_id else "external"
            local = wd_local_path(sn) if valid_id else None
        else:
            kind, payload = song_route(sn) if valid_id else (None, None)
            filename = payload if kind == "cdn" else (f"yt_{sn}.mp4" if kind == "yt" else None)
            source = {"cdn": "cdn", "yt": "youtube"}.get(kind, "unknown" if valid_id else "external")
            local = local_path_for(filename) if filename else None
            if local is None and valid_id:
                local = yt_local_path(sn)
        matches = lambda task: (valid_id and task.get("song_id") == sn) or (filename and task.get("filename") == filename)
        task = next((task for task in tasks if matches(task)), None)
        outcome = next((outcome for outcome in reversed(outcomes) if matches(outcome)), None)
        entry = {
            "name": str(it.get("videoName") or "?"), "player": str(it.get("playerName") or ""),
            "song_id": sn if valid_id else None, "filename": filename,
            "source": task["source"] if task else source, "position": index,
            "storage": ("library" if _same_directory(os.path.dirname(local), _config.get("library_dir")) else "cache") if local else None,
            "task_id": task["task_id"] if task else None,
            "error": None,
        }
        if filename and filename == cooldown["serving_filename"]:
            code, label = "cooldown", "Cool Down"
        elif local:
            code, label = "cached", "已缓存"
        elif task:
            code, label = "downloading", "下载中"
            entry["storage"] = task["storage"]
        elif outcome and not outcome["ok"]:
            code, label = ("cancelled", "已取消") if outcome["cancelled"] else ("failed", "失败")
            entry["error"] = outcome["error"]
        elif source == "youtube":
            code, label = "youtube", "等待播放"
        elif source == "external":
            code, label = "external", "外部源"
        elif source == "unknown":
            code, label = "unresolved", "待解析"
        else:
            code, label = "not_cached", "未缓存"
        entry.update(state_code=code, state=label)
        result.append(entry)
    return result


def get_queue_snapshot():
    with _queue_lock:
        items = copy.deepcopy(_last_queue_items)
    return describe_queue(items)


def refresh_queue_state():
    emit("queue", items=get_queue_snapshot())


def submit_preload(items):
    count = int(_config["preload_count"]) + 1
    for it in items[:count]:
        if not isinstance(it, dict) or _stop_event.is_set():
            continue
        sn = it.get("songNum", 0)
        if not (isinstance(sn, int) and not isinstance(sn, bool) and sn > 0):
            continue
        if it.get("wd"):
            # WannaDance：文件名即编号，直接走 api.udon.dance 302 下载
            if not wd_local_path(sn):
                start_wd_download(sn)
            continue
        if in_library(sn) or yt_local_path(sn):
            continue
        kind, payload = song_route(sn)
        if kind is None:
            resolve_video_filename(sn)
            kind, payload = song_route(sn)
        if kind == "cdn" and not is_cached(payload):
            start_background_download(payload, sn)
    refresh_queue_state()


# ---------------- Cool Down（切歌时把下一首替换为放松视频，一次性） ----------------

_cooldown_armed = False      # 待命状态
_cooldown_serving = None     # 正在用放松视频服务的文件名（该文件的后续 Range 请求都用放松视频）
_cooldown_exclude = None     # 待命时正在播放的歌曲文件名（不对它触发）


def resolve_cooldown_file(configured_path=""):
    """留空使用内置视频；显式选择失效时不替换成其他视频。"""
    path = (configured_path or "").strip() or DEFAULT_COOLDOWN_PATH
    try:
        return path if path and os.path.isfile(path) and os.path.getsize(path) > 0 else None
    except OSError:
        return None


def cooldown_file():
    """返回当前可用的放松视频；不把安装目录写入用户配置。"""
    return resolve_cooldown_file(_config.get("cooldown_file", ""))


def cooldown_available():
    return cooldown_file() is not None


def get_cooldown_state():
    with _cooldown_lock:
        phase = "armed" if _cooldown_armed else ("serving" if _cooldown_serving is not None else "idle")
        return {"phase": phase, "available": cooldown_available(), "active": phase != "idle",
                "serving_filename": _cooldown_serving}


def _emit_cooldown():
    emit("cooldown", **get_cooldown_state())
    refresh_queue_state()


def _current_song_filename():
    with _queue_lock:
        items = copy.deepcopy(_last_queue_items)
    if not items or not isinstance(items[0], dict):
        return None
    sn = items[0].get("songNum", 0)
    if not (isinstance(sn, int) and not isinstance(sn, bool) and sn > 0):
        return None
    kind, payload = song_route(sn)
    return payload if kind == "cdn" else (f"yt_{sn}.mp4" if kind == "yt" else None)


def cooldown_arm():
    global _cooldown_armed, _cooldown_serving, _cooldown_exclude
    with _cooldown_lock:
        if not _running or _stop_event.is_set() or not cooldown_available():
            return False
        _cooldown_exclude = _current_song_filename()
        _cooldown_armed = True
        _cooldown_serving = None
    log("Cool Down 已待命：下一首在本机替换为放松视频（可点击取消）")
    _emit_cooldown()
    return True


def cooldown_disarm(silent=False):
    global _cooldown_armed, _cooldown_serving, _cooldown_exclude
    with _cooldown_lock:
        _cooldown_armed = False
        _cooldown_serving = None
        _cooldown_exclude = None
    if not silent:
        log("Cool Down 已取消")
    _emit_cooldown()


def cooldown_active():
    return get_cooldown_state()["active"]


def cooldown_check(filename):
    """请求级一次性替换；下一个不同文件名请求解除，不伪造播放结束事件。"""
    global _cooldown_armed, _cooldown_serving, _cooldown_exclude
    changed = False
    message = None
    path = None
    with _cooldown_lock:
        if _cooldown_serving is not None:
            if filename == _cooldown_serving:
                path = cooldown_file()
                if path:
                    return path
                _cooldown_serving = None
                _cooldown_exclude = None
                changed = True
                message = "Cool Down 视频已不可用，已恢复正常请求"
            else:
                _cooldown_serving = None
                _cooldown_exclude = None
                changed = True
                message = "已切换到其他歌曲，Cool Down 本次替换结束"
        if _cooldown_armed and filename != _cooldown_exclude:
            path = cooldown_file()
            _cooldown_armed = False
            changed = True
            if path:
                _cooldown_serving = filename
                message = f"Cool Down 触发：{filename} -> 放松视频 {os.path.basename(path)}"
            else:
                _cooldown_serving = None
                _cooldown_exclude = None
                message = "Cool Down 视频不可用，已自动取消，请重新选择文件"
    if message:
        log(message)
    if changed:
        _emit_cooldown()
    return path


class LogWatcher(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.current_file = None
        self.offset = 0
        self.buf = ""

    def run(self):
        log("日志监听已启动，等待 VRChat 日志...")
        while not _stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                log(f"日志监听出错: {e}")
            _stop_event.wait(1.0)

    def _tick(self):
        newest = find_newest_log()
        if newest is None:
            return
        if newest != self.current_file:
            self.current_file = newest
            self.buf = ""
            log(f"监听日志文件: {os.path.basename(newest)}")
            with open(newest, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 2 * 1024 * 1024))
                tail = f.read()
            self.offset = size
            tail_lines = tail.splitlines()
            if get_runtime_state()["youtube_available"]:
                scan_resolved_youtube(tail_lines)
            items = extract_queue_items(tail_lines)
            if items is not None:
                self._on_queue(items)
            return
        size = os.path.getsize(newest)
        if size < self.offset:
            self.offset = 0
            self.buf = ""
        if size == self.offset:
            return
        with open(newest, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self.offset)
            data = f.read()
            self.offset = f.tell()
        self.buf += data
        if "\n" not in self.buf:
            return
        lines = self.buf.split("\n")
        self.buf = lines.pop()
        if get_runtime_state()["youtube_available"]:
            scan_resolved_youtube(lines)
        items = extract_queue_items(lines)
        if items is not None:
            self._on_queue(items)

    def _on_queue(self, items):
        global _last_queue_items
        with _queue_lock:
            _last_queue_items = copy.deepcopy(items or [])
        if not items:
            emit("queue", items=[])
            return
        emit("queue", items=describe_queue(items))
        submit_preload(items)


# ---------------- 服务启停 ----------------

class _StartCancelled(Exception):
    pass


def _spawn_service_thread(target, args=()):
    thread = threading.Thread(target=target, args=args, daemon=True)
    _service_threads.append(thread)
    thread.start()
    return thread


def start_services():
    """串行启停，返回 None 表示成功；兼容旧 CLI 的错误字符串。"""
    global _server, _watcher, _running, _tunnel_sock, _run_id, _hosts_restore_pending
    with _lifecycle_lock:
        if _running:
            return None
        if _hosts_restore_pending:
            error = "hosts 尚未成功恢复，请先重试停止服务后再启动"
            _set_runtime_state(phase="failed", running=False, hosts_restore_pending=True, error=error)
            return error
        if _same_directory(_config.get("cache_dir"), _config.get("library_dir")):
            error = "缓存目录与永久曲库相同，请先在设置中分开保存目录再启动服务"
            _set_runtime_state(phase="failed", running=False, error=error)
            return error
        with _downloads_lock:
            finishing = bool(_downloads_in_progress)
        if finishing or rebuilding() or verifying() or any(t.is_alive() for t in _service_threads):
            error = "仍有后台任务正在结束，请稍后再启动服务"
            _set_runtime_state(phase="failed", running=False, error=error)
            return error
        _service_threads.clear()
        _run_id += 1
        _stop_event.clear()
        _library_stop.clear()
        _set_runtime_state(phase="starting", running=False, youtube_available=False,
                           queue_watcher_available=False, hosts_restore_pending=False,
                           warnings=[], error=None)
        server_started = False
        hosts_touched = False
        warnings = []
        try:
            log("正在解析真实服务器 IP...")
            resolve_real_ips()
            if _stop_event.is_set():
                raise _StartCancelled()
            if not _origin_ips.get("cdn.pypy.dance"):
                raise RuntimeError("无法获取 cdn.pypy.dance 真实 IP，请检查网络后重试")
            try:
                _server = ThreadingHTTPServer(("127.0.0.1", 80), DanceHandler)
            except OSError as e:
                raise RuntimeError(f"无法绑定 80 端口 ({e})，请关闭占用该端口的程序后重试") from e
            _server.daemon_threads = True
            yt_mode = bool(_config.get("enable_youtube_cache"))
            if yt_mode and not _origin_ips.get(API_HOST):
                warnings.append("无法解析 api.pypy.dance，YouTube 源缓存不可用")
                yt_mode = False
            if yt_mode:
                try:
                    _tunnel_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    _tunnel_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    _tunnel_sock.bind(("127.0.0.1", 443))
                    _tunnel_sock.listen(64)
                    _tunnel_sock.settimeout(1)
                except OSError as e:
                    warnings.append(f"无法绑定 443 端口 ({e})，YouTube 源缓存不可用")
                    if _tunnel_sock is not None:
                        _tunnel_sock.close()
                    _tunnel_sock = None
                    yt_mode = False
            if _stop_event.is_set():
                raise _StartCancelled()
            hosts_touched = True
            # 即使 hosts 写入中途失败，也必须保留恢复责任，直到恢复真正成功。
            _hosts_restore_pending = True
            hijack_hosts(yt_mode)
            if _stop_event.is_set():
                raise _StartCancelled()
            _spawn_service_thread(_server.serve_forever)
            server_started = True
            _spawn_service_thread(_hosts_watchdog_loop, (yt_mode,))
            if _tunnel_sock is not None:
                _spawn_service_thread(_tunnel_loop, (_tunnel_sock, list(_origin_ips[API_HOST])))
            _spawn_service_thread(_mapping_flush_loop)
            watcher_available = os.path.isdir(VRC_LOG_DIR)
            _set_runtime_state(youtube_available=yt_mode, queue_watcher_available=watcher_available)
            if watcher_available:
                _watcher = LogWatcher()
                _service_threads.append(_watcher)
                _watcher.start()
            else:
                warnings.append("未找到 VRChat 日志目录，当前仅提供被动缓存")
            _running = True
            _set_runtime_state(phase="running", running=True, youtube_available=yt_mode,
                               queue_watcher_available=watcher_available, hosts_restore_pending=True,
                               warnings=warnings, error=None)
            for warning in warnings:
                log("提示: " + warning)
            emit_stats()
            log("本地缓存服务运行中：127.0.0.1:80")
            return None
        except Exception as e:
            cancelled = isinstance(e, _StartCancelled)
            _stop_event.set()
            if _server is not None:
                try:
                    if server_started:
                        _server.shutdown()
                    _server.server_close()
                except Exception:
                    pass
                _server = None
            if _tunnel_sock is not None:
                try:
                    _tunnel_sock.close()
                except Exception:
                    pass
                _tunnel_sock = None
            _watcher = None
            _running = False
            restored = restore_hosts() if hosts_touched else True
            _hosts_restore_pending = not restored
            error = "启动已取消" if cancelled else str(e)
            if not restored:
                error += "；hosts 恢复失败，请检查日志"
            _set_runtime_state(phase="stopped" if cancelled and restored else "failed", running=False,
                               youtube_available=False, queue_watcher_available=False,
                               hosts_restore_pending=_hosts_restore_pending,
                               warnings=warnings, error=None if cancelled and restored else error)
            return error


def stop_services():
    """停止并恢复 hosts；None 表示成功，错误字符串表示需留窗重试。"""
    global _server, _watcher, _running, _tunnel_sock, _run_id, _hosts_restore_pending
    # 在等待生命周期锁之前通知启动线程取消，防止窗口关闭后又启动成功。
    _stop_event.set()
    with _lifecycle_lock:
        needs_restore = _hosts_restore_pending or _running
        _set_runtime_state(phase="stopping", error=None)
        _run_id += 1
        cancel_library_download()
        cooldown_disarm(silent=True)
        if _server is not None:
            try:
                _server.shutdown()
                _server.server_close()
            except Exception as e:
                log(f"HTTP 服务关闭异常: {e}")
            _server = None
        if _tunnel_sock is not None:
            try:
                _tunnel_sock.close()
            except OSError:
                pass
            _tunnel_sock = None
        restored = restore_hosts() if needs_restore else True
        _hosts_restore_pending = not restored
        flush_mapping()
        _running = False
        _watcher = None
        deadline = time.monotonic() + 2
        for thread in list(_service_threads):
            if thread is not threading.current_thread() and thread.is_alive():
                thread.join(max(0, deadline - time.monotonic()))
        error = None if restored else "服务已停止，但 hosts 恢复失败，请检查日志并重试停止服务"
        _set_runtime_state(phase="stopped" if restored else "failed", running=False,
                           youtube_available=False, queue_watcher_available=False, warnings=[],
                           hosts_restore_pending=_hosts_restore_pending, error=error)
        log("服务已停止" if restored else "服务已停止，hosts 尚未成功恢复")
        refresh_queue_state()
        return error


def is_running():
    return _running


# ---------------- 命令行模式 ----------------

def _cli_listener(kind, data):
    ts = time.strftime("%H:%M:%S")
    if kind == "log":
        print(f"[{ts}] {data['msg']}", flush=True)
    elif kind == "queue":
        items = data["items"]
        if not items:
            return
        print(f"[{ts}] 当前队列:", flush=True)
        for it in items:
            print(f"[{ts}]   [{it['state']}] {it['name']}", flush=True)
    elif kind == "stats":
        line = f"[{ts}] 缓存目录: {data['count']} 个 | {data['total_mb']:.0f} MB / {data['limit_mb']} MB"
        if data.get("lib_enabled"):
            line += f" | 曲库: {data['lib_count']} 首 | {data['lib_mb']:.0f} MB"
        print(line, flush=True)


def main():
    add_listener(_cli_listener)
    log("=== VRChat PyPyDance助手 V1.2（命令行模式） ===")
    log("Bilibili @鲨鲨比亚Sharkspeare")
    if os.name != "nt":
        log("本工具仅支持 Windows")
        sys.exit(1)
    ensure_admin()
    load_config()
    load_mapping()
    err = start_services()
    if err:
        log("错误: " + err)
        sys.exit(1)
    log("关闭此窗口或按 Ctrl+C 退出，退出时自动恢复 hosts。")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_services()
        log("已退出")


if __name__ == "__main__":
    main()
