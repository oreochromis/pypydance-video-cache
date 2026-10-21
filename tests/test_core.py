"""Core regression tests: temporary files and fake HTTP only, no hosts or sockets."""
import copy
import importlib.util
import io
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock


class FakeResponse:
    def __init__(self, chunks=(), total=None, status=200):
        self.chunks = list(chunks)
        self.status = status
        self.headers = {} if total is None else {"Content-Length": str(total)}
        self.read_calls = 0
        self.closed = False

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, size=None):
        self.read_calls += 1
        value = self.chunks.pop(0) if self.chunks else b""
        if isinstance(value, Exception):
            raise value
        return value() if callable(value) else value

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeServer:
    def __init__(self):
        self.stopped = threading.Event()
        self.shutdown_calls = 0
        self.close_calls = 0
        self.daemon_threads = False

    def serve_forever(self):
        self.stopped.wait(5)

    def shutdown(self):
        self.shutdown_calls += 1
        self.stopped.set()

    def server_close(self):
        self.close_calls += 1
        self.stopped.set()


class CoreTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "isolated_dance_cache", Path(__file__).resolve().parents[1] / "dance_cache.py"
        )
        self.dc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.dc)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.library = self.root / "library"
        self.cache.mkdir()
        self.library.mkdir()
        self.dc.SCRIPT_DIR = str(self.root)
        self.dc.MAPPING_PATH = str(self.root / "mapping.json")
        self.dc.HOSTS_PATH = str(self.root / "hosts-never-used")
        self.dc.VRC_LOG_DIR = str(self.root / "missing-logs")
        self.builtin_cooldown = self.root / "bundled-resources" / "assets" / "default_cooldown.mp4"
        self.dc.DEFAULT_COOLDOWN_PATH = str(self.builtin_cooldown)
        self.dc._config = self.dc._validated_config({
            **self.dc.DEFAULT_CONFIG,
            "cache_dir": str(self.cache), "library_dir": str(self.library),
        })
        self.dc._write_config(self.dc._config)
        self.events = []
        self.listener = lambda kind, data: self.events.append((kind, copy.deepcopy(data)))
        self.dc.add_listener(self.listener)
        self.guards = ExitStack()
        # Any accidental real service/network call fails this test instead of touching the host.
        for name in ("resolve_real_ips", "resolve_video_filename", "open_upstream",
                     "hijack_hosts", "restore_hosts", "ThreadingHTTPServer"):
            self.guards.enter_context(mock.patch.object(
                self.dc, name, side_effect=AssertionError(f"Unexpected live operation: {name}")
            ))
        self.guards.enter_context(mock.patch.object(
            self.dc.urllib.request, "urlopen", side_effect=AssertionError("Unexpected URL request")
        ))

    def tearDown(self):
        self.dc._stop_event.set()
        self.dc._library_stop.set()
        threads = list(self.dc._service_threads)
        threads += [self.dc._rebuild_thread, self.dc._verify_thread]
        for thread in threads:
            if thread and thread is not threading.current_thread() and thread.is_alive():
                thread.join(2)
        self.guards.close()
        self.temp.cleanup()

    def events_of(self, kind):
        return [data for event, data in self.events if event == kind]

    def queue_song(self, sid=1, route="video.mp4"):
        if route is not None:
            self.dc.mark_mapping(sid, route)
        self.dc._last_queue_items = [{"songNum": sid, "videoName": "Song", "playerName": "Player"}]

    def proxy(self):
        handler = self.dc.DanceHandler.__new__(self.dc.DanceHandler)
        handler.path = "/video.mp4"
        handler.headers = {}
        handler.wfile = io.BytesIO()
        for name in ("send_response", "send_header", "end_headers", "send_error"):
            setattr(handler, name, mock.Mock())
        return handler

    @contextmanager
    def service_environment(self, api=False, bind_error=None):
        server = FakeServer()
        fake_socket = mock.Mock()
        if bind_error:
            fake_socket.bind.side_effect = OSError(bind_error)

        def resolve():
            self.dc._origin_ips = {"cdn.pypy.dance": ["1.1.1.1"]}
            if api:
                self.dc._origin_ips[self.dc.API_HOST] = ["1.0.0.1"]

        with ExitStack() as stack:
            factory = stack.enter_context(mock.patch.object(self.dc, "ThreadingHTTPServer", return_value=server))
            resolver = stack.enter_context(mock.patch.object(self.dc, "resolve_real_ips", side_effect=resolve))
            hijack = stack.enter_context(mock.patch.object(self.dc, "hijack_hosts"))
            restore = stack.enter_context(mock.patch.object(self.dc, "restore_hosts", return_value=True))
            stack.enter_context(mock.patch.object(self.dc.socket, "socket", return_value=fake_socket))
            stack.enter_context(mock.patch.object(self.dc, "_hosts_watchdog_loop", return_value=None))
            stack.enter_context(mock.patch.object(self.dc, "_mapping_flush_loop", return_value=None))
            stack.enter_context(mock.patch.object(self.dc, "_tunnel_loop", return_value=None))
            try:
                yield server, factory, resolver, hijack, restore
            finally:
                if self.dc.is_running():
                    self.dc.stop_services()

    def test_config_snapshot_is_detached(self):
        snapshot = self.dc.get_config()
        snapshot["preload_count"] = 99
        self.assertEqual(self.dc.get_config()["preload_count"], 3)

    def test_loading_legacy_shared_directory_preserves_library_and_blocks_cleanup(self):
        song = self.cache / "1.mp4"
        song.write_bytes(b"permanent video")
        config = {**self.dc.DEFAULT_CONFIG, "cache_dir": str(self.cache), "library_dir": str(self.cache)}
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.dc.load_config()
        self.assertEqual(self.dc.get_config()["library_dir"], str(self.cache))
        with self.assertRaises(ValueError):
            self.dc.clear_cache()
        self.assertIn("目录", self.dc.start_services())
        self.dc.resolve_real_ips.assert_not_called()
        self.dc.cleanup_cache()
        self.assertEqual(song.read_bytes(), b"permanent video")
        fixed = self.root / "new-cache"
        self.dc.update_config({"cache_dir": str(fixed)})
        self.assertEqual(self.dc.get_config()["library_dir"], str(self.cache))
        self.assertEqual(song.read_bytes(), b"permanent video")

    def test_invalid_stored_parameter_does_not_forget_storage_directories(self):
        config = {**self.dc.DEFAULT_CONFIG, "cache_dir": str(self.cache),
                  "library_dir": str(self.library), "preferred_cdn_ip": "invalid"}
        saved = json.dumps(config)
        (self.root / "config.json").write_text(saved, encoding="utf-8")
        self.dc.load_config()
        loaded = self.dc.get_config()
        self.assertEqual((loaded["cache_dir"], loaded["library_dir"]), (str(self.cache), str(self.library)))
        self.assertEqual(loaded["preferred_cdn_ip"], "")
        self.assertEqual((self.root / "config.json").read_text(encoding="utf-8"), saved)
        self.assertTrue(self.events_of("warning"))

    def test_invalid_config_patch_changes_neither_memory_nor_file(self):
        before = self.dc.get_config()
        saved = (self.root / "config.json").read_bytes()
        with self.assertRaises(ValueError):
            self.dc.update_config({"preload_count": 9, "preferred_cdn_ip": "999.1.2.3"})
        self.assertEqual(before, self.dc.get_config())
        self.assertEqual(saved, (self.root / "config.json").read_bytes())

    def test_atomic_save_failure_preserves_previous_configuration(self):
        before = self.dc.get_config()
        saved = (self.root / "config.json").read_bytes()
        with mock.patch.object(self.dc.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.dc.update_config({"preload_count": 7})
        self.assertEqual(before, self.dc.get_config())
        self.assertEqual(saved, (self.root / "config.json").read_bytes())
        self.assertEqual(list(self.root.glob("config-*.tmp")), [])

    def test_config_update_commits_only_after_validation(self):
        result = self.dc.update_config({"preload_count": 5})
        self.assertEqual(result, {"changed": ["preload_count"], "restart_required": False})
        saved = json.loads((self.root / "config.json").read_text())
        self.assertEqual(saved["preload_count"], 5)
        self.assertEqual(self.dc.get_config()["preload_count"], 5)

    def test_same_cache_and_library_directory_is_rejected_twice(self):
        permanent = self.cache / "123.mp4"
        permanent.write_bytes(b"preserve")
        with self.assertRaises(ValueError):
            self.dc.update_config({"library_dir": str(self.cache / ".")})
        self.dc._config["library_dir"] = str(self.cache)
        with self.assertRaises(ValueError):
            self.dc.clear_cache()
        self.assertEqual(permanent.read_bytes(), b"preserve")

    def test_busy_settings_require_stop_or_task_completion(self):
        key = self.dc._canonical_path(str(self.cache / "busy.mp4"))
        self.dc._downloads_in_progress[key] = threading.current_thread()
        with self.assertRaises(ValueError):
            self.dc.update_config({"cache_dir": str(self.root / "other")})
        self.dc._downloads_in_progress.clear()
        # 运行中但无在途任务：目录热改直接生效，无需停止服务
        self.dc._running = True
        result = self.dc.update_config({"cache_dir": str(self.root / "other")})
        self.assertEqual(result["changed"], ["cache_dir"])
        self.assertEqual(self.dc.get_config()["cache_dir"], str(self.root / "other"))
        with self.assertRaises(ValueError):
            self.dc.update_config({"enable_youtube_cache": False})
        self.dc._running = False

    def test_preferred_cdn_ip_applies_to_redirected_subdomain(self):
        self.dc._config["preferred_cdn_ip"] = "172.66.157.15"
        self.dc._origin_ips["cdn.pypy.dance"] = ["104.20.20.68"]
        self.dc._origin_ips["806bb815.cdn.pypy.dance"] = ["59.16.201.126"]
        self.assertEqual(self.dc.ordered_ips("cdn.pypy.dance")[0], "172.66.157.15")
        # 实际下载会被 301 到带哈希的子域名，优选 IP 必须同样生效
        self.assertEqual(self.dc.ordered_ips("806bb815.cdn.pypy.dance")[0], "172.66.157.15")
        # 未解析过的子域名也应返回优选 IP，而不是空列表导致无法下载
        self.assertEqual(self.dc.ordered_ips("abcdef12.cdn.pypy.dance"), ["172.66.157.15"])
        # 非 CDN 域名不受影响
        self.dc._origin_ips["api.pypy.dance"] = ["1.2.3.4"]
        self.assertEqual(self.dc.ordered_ips("api.pypy.dance"), ["1.2.3.4"])

    def test_clear_cache_protects_active_part_and_permanent_library(self):
        target = str(self.cache / "busy.mp4")
        (self.cache / "busy.mp4.part").write_bytes(b"partial")
        (self.cache / "old.mp4").write_bytes(b"old")
        (self.library / "123.mp4").write_bytes(b"permanent")
        meta = self.dc._begin_download(target, "busy.mp4")
        result = self.dc.clear_cache()
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(result["removed_bytes"], 3)
        self.assertEqual(result["skipped_busy"], 1)
        self.assertTrue((self.cache / "busy.mp4.part").exists())
        self.assertTrue((self.library / "123.mp4").exists())
        self.dc._finish_download(meta, False, True, None, 7, 0)

    def test_clear_cache_reports_delete_failure(self):
        locked = self.cache / "locked.mp4"
        locked.write_bytes(b"locked")
        with mock.patch.object(self.dc.os, "remove", side_effect=PermissionError("locked")):
            result = self.dc.clear_cache()
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["removed_count"], 0)
        self.assertEqual(result["errors"][0]["filename"], "locked.mp4")

    def test_queue_snapshot_does_not_resolve_or_invent_download(self):
        self.queue_song()
        row = self.dc.get_queue_snapshot()[0]
        self.assertEqual(row["state_code"], "not_cached")
        self.assertEqual(row["source"], "cdn")
        self.assertEqual(row["song_id"], 1)
        self.assertIsNone(row["task_id"])
        self.dc.resolve_video_filename.assert_not_called()

    def test_unknown_queue_is_a_pure_local_snapshot(self):
        self.queue_song(route=None)
        row = self.dc.get_queue_snapshot()[0]
        self.assertEqual(row["state_code"], "unresolved")
        self.dc.resolve_video_filename.assert_not_called()

    def test_actual_task_and_failure_are_visible_in_queue(self):
        self.queue_song()
        meta = self.dc._begin_download(str(self.cache / "video.mp4"), "video.mp4", 1)
        row = self.dc.get_queue_snapshot()[0]
        self.assertEqual(row["state_code"], "downloading")
        self.assertEqual(row["task_id"], meta["task_id"])
        self.dc._finish_download(meta, False, False, "network timeout", 0, 0)
        row = self.dc.get_queue_snapshot()[0]
        self.assertEqual(row["state_code"], "failed")
        self.assertEqual(row["error"], "network timeout")
        self.assertFalse(self.events_of("download_done")[-1]["ok"])

    def test_library_and_youtube_queue_states_remain_distinct(self):
        self.queue_song(route="yt:https://www.youtube.com/watch?v=test")
        self.assertEqual(self.dc.get_queue_snapshot()[0]["state_code"], "youtube")
        (self.library / "1.mp4").write_bytes(b"video")
        row = self.dc.get_queue_snapshot()[0]
        self.assertEqual(row["state_code"], "cached")
        self.assertEqual(row["source"], "youtube")
        self.assertEqual(row["storage"], "library")

    def test_learning_mapping_refreshes_queue_state(self):
        # 映射缺失时队列显示"待解析"；之后学习到映射（如播放时代理透传学到）
        # 必须主动刷新队列，否则本地已缓存的歌会一直卡在"待解析"
        self.queue_song(route=None)
        self.events.clear()
        (self.cache / "video.mp4").write_bytes(b"video")
        self.dc.mark_mapping(1, "video.mp4")
        queue_events = self.events_of("queue")
        self.assertTrue(queue_events)
        self.assertEqual(queue_events[-1]["items"][0]["state_code"], "cached")

    def test_listener_can_be_removed(self):
        self.dc.remove_listener(self.listener)
        self.dc.log("not delivered")
        self.assertEqual(self.events, [])

    def test_cdn_download_success_has_one_final_event(self):
        response = FakeResponse([b"abc", b"def"], total=6)
        connection = FakeConnection()
        with mock.patch.object(self.dc, "open_upstream", return_value=(connection, response)):
            self.assertTrue(self.dc.download_file("video.mp4", 1))
        self.assertEqual((self.cache / "video.mp4").read_bytes(), b"abcdef")
        done = self.events_of("download_done")
        self.assertEqual(len(done), 1)
        self.assertTrue(done[0]["ok"])
        self.assertFalse(done[0]["cancelled"])
        self.assertEqual(done[0]["done"], 6)
        self.assertEqual(self.dc.get_downloads_snapshot(), [])
        self.assertTrue(connection.closed)

    def test_cdn_http_error_is_never_saved(self):
        response = FakeResponse([b"error page"], status=404, total=10)
        with mock.patch.object(self.dc, "open_upstream", return_value=(FakeConnection(), response)):
            self.assertFalse(self.dc.download_file("video.mp4"))
        self.assertFalse((self.cache / "video.mp4").exists())
        self.assertEqual(len(self.events_of("download_done")), 1)
        self.assertIn("404", self.events_of("download_done")[0]["error"])

    def test_incomplete_download_and_read_error_always_finish(self):
        for response in (FakeResponse([b"abc"], total=10),
                         FakeResponse([b"abc", OSError("broken upstream")], total=10)):
            with self.subTest(response=response):
                self.events.clear()
                with mock.patch.object(self.dc, "open_upstream", return_value=(FakeConnection(), response)):
                    self.assertFalse(self.dc.download_file("video.mp4"))
                self.assertEqual(len(self.events_of("download_done")), 1)
                self.assertFalse(self.events_of("download_done")[0]["ok"])
                self.assertFalse((self.cache / "video.mp4").exists())
                self.assertFalse((self.cache / "video.mp4.part").exists())
                self.assertEqual(self.dc.get_downloads_snapshot(), [])

    def test_unknown_length_cancel_does_not_publish_partial_video(self):
        def interrupt():
            self.dc._stop_event.set()
            return b"partial"
        response = FakeResponse([interrupt])
        with mock.patch.object(self.dc, "open_upstream", return_value=(FakeConnection(), response)):
            self.assertFalse(self.dc.download_file("video.mp4"))
        self.assertFalse((self.cache / "video.mp4").exists())
        done = self.events_of("download_done")[-1]
        self.assertTrue(done["cancelled"])
        self.assertFalse(done["ok"])
        self.assertIsNone(done["error"])

    def test_youtube_unknown_length_cancel_is_not_success(self):
        def interrupt():
            self.dc._stop_event.set()
            return b"partial"
        with mock.patch.object(self.dc.urllib.request, "urlopen", return_value=FakeResponse([interrupt])):
            self.assertFalse(self.dc.download_googlevideo(1, "https://example.invalid/video"))
        self.assertFalse((self.cache / "yt_1.mp4").exists())
        done = self.events_of("download_done")[-1]
        self.assertTrue(done["cancelled"])
        self.assertEqual(done["source"], "youtube")
        self.assertEqual(done["song_id"], 1)

    def test_proxy_error_body_is_forwarded_without_caching(self):
        response = FakeResponse([b"not found"], total=9, status=404)
        handler = self.proxy()
        with mock.patch.object(self.dc, "open_upstream", return_value=(FakeConnection(), response)):
            handler._proxy()
        self.assertEqual(handler.wfile.getvalue(), b"not found")
        self.assertFalse((self.cache / "video.mp4").exists())
        self.assertEqual(self.events_of("download_start"), [])

    def test_proxy_read_failure_finishes_registered_task_once(self):
        response = FakeResponse([b"abc", OSError("read failure")], total=10)
        handler = self.proxy()
        with mock.patch.object(self.dc, "open_upstream", return_value=(FakeConnection(), response)):
            handler._proxy()
        done = self.events_of("download_done")
        self.assertEqual(len(done), 1)
        self.assertFalse(done[0]["ok"])
        self.assertIn("read failure", done[0]["error"])
        self.assertEqual(self.dc.get_downloads_snapshot(), [])
        self.assertFalse((self.cache / "video.mp4.part").exists())

    def test_proxy_full_response_succeeds_once(self):
        handler = self.proxy()
        with mock.patch.object(self.dc, "open_upstream", return_value=(FakeConnection(), FakeResponse([b"abc"], total=3))):
            handler._proxy()
        self.assertEqual((self.cache / "video.mp4").read_bytes(), b"abc")
        self.assertEqual(len(self.events_of("download_done")), 1)
        self.assertTrue(self.events_of("download_done")[0]["ok"])

    def test_size_verification_does_not_read_full_body_when_range_is_ignored(self):
        response = FakeResponse([AssertionError("must not read entire video")], total=123)
        connection = FakeConnection()
        with mock.patch.object(self.dc, "open_upstream", return_value=(connection, response)):
            self.assertEqual(self.dc._remote_size("video.mp4"), 123)
        self.assertEqual(response.read_calls, 0)
        self.assertTrue(connection.closed)

    def write_builtin_cooldown(self, content=b"bundled relax video"):
        self.builtin_cooldown.parent.mkdir(parents=True, exist_ok=True)
        self.builtin_cooldown.write_bytes(content)
        return str(self.builtin_cooldown)

    def test_new_and_legacy_empty_configs_use_builtin_without_saving_installation_path(self):
        expected = self.write_builtin_cooldown()
        config_path = self.root / "config.json"
        for initial in (None, {}, {"cooldown_file": ""}):
            with self.subTest(config=initial):
                if initial is None:
                    config_path.unlink(missing_ok=True)
                else:
                    config_path.write_text(json.dumps(initial), encoding="utf-8")
                self.dc.load_config()
                self.assertTrue(self.dc.get_cooldown_state()["available"])
                self.assertEqual(self.dc.cooldown_file(), expected)
                self.assertEqual(self.dc.get_config()["cooldown_file"], "")
                self.dc.save_config()
                self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["cooldown_file"], "")

    def test_cooldown_preserves_custom_choice_and_can_restore_builtin(self):
        builtin = self.write_builtin_cooldown()
        custom = self.root / "my-relax.mp4"
        custom.write_bytes(b"personal video")
        self.dc.update_config({"cooldown_file": str(custom)})
        self.dc.load_config()
        self.assertEqual(self.dc.cooldown_file(), str(custom))
        self.assertEqual(self.dc.get_config()["cooldown_file"], str(custom))
        self.dc.update_config({"cooldown_file": ""})
        self.assertEqual(self.dc.cooldown_file(), builtin)
        self.assertEqual(json.loads((self.root / "config.json").read_text(encoding="utf-8"))["cooldown_file"], "")

    def test_missing_custom_cooldown_is_not_silently_replaced_by_builtin(self):
        self.write_builtin_cooldown()
        missing = str(self.root / "deleted-personal-video.mp4")
        self.dc._config["cooldown_file"] = missing
        self.dc._write_config(self.dc._config)
        self.dc.load_config()
        self.assertEqual(self.dc.get_config()["cooldown_file"], missing)
        self.assertIsNone(self.dc.cooldown_file())
        self.dc._running = True
        self.assertFalse(self.dc.cooldown_arm())

    def test_missing_empty_or_unreadable_builtin_cooldown_cannot_be_armed(self):
        self.dc._running = True
        for content in (None, b""):
            with self.subTest(content=content):
                if content is not None:
                    self.write_builtin_cooldown(content)
                self.assertIsNone(self.dc.cooldown_file())
                self.assertFalse(self.dc.get_cooldown_state()["available"])
                self.assertFalse(self.dc.cooldown_arm())
        self.write_builtin_cooldown()
        with mock.patch.object(self.dc.os.path, "getsize", side_effect=PermissionError("denied")):
            self.assertIsNone(self.dc.cooldown_file())
            self.assertFalse(self.dc.cooldown_arm())

    def test_builtin_cooldown_serves_head_and_range_without_network_or_resource_writes(self):
        payload = b"bundled relaxation video content"
        self.write_builtin_cooldown(payload)
        self.queue_song()
        self.dc._running = True
        self.assertTrue(self.dc.cooldown_arm())
        self.assertIsNone(self.dc.cooldown_check("video.mp4"))
        head = self.proxy()
        head.path = "/next.mp4"
        head.do_HEAD()
        head.send_response.assert_called_once_with(200)
        self.assertIn(mock.call("Content-Length", str(len(payload))), head.send_header.call_args_list)
        self.assertEqual(head.wfile.getvalue(), b"")
        get = self.proxy()
        get.path = "/next.mp4"
        get.headers = {"Range": "bytes=3-10"}
        with mock.patch.object(self.dc, "touch") as touch:
            get.do_GET()
        touch.assert_not_called()
        get.send_response.assert_called_once_with(206)
        self.assertIn(mock.call("Content-Range", f"bytes 3-10/{len(payload)}"), get.send_header.call_args_list)
        self.assertEqual(get.wfile.getvalue(), payload[3:11])
        self.assertEqual(self.dc.get_cooldown_state()["phase"], "serving")
        self.assertIsNone(self.dc.cooldown_check("after.mp4"))
        self.assertEqual(self.dc.get_cooldown_state()["phase"], "idle")

    def test_cooldown_reports_armed_serving_and_idle(self):
        self.queue_song()
        clip = self.root / "relax.mp4"
        clip.write_bytes(b"relax")
        self.dc._config["cooldown_file"] = str(clip)
        self.dc._running = True
        self.assertTrue(self.dc.cooldown_arm())
        self.assertEqual(self.dc.get_cooldown_state()["phase"], "armed")
        self.assertIsNone(self.dc.cooldown_check("video.mp4"))
        self.assertEqual(self.dc.cooldown_check("next.mp4"), str(clip))
        self.assertEqual(self.dc.get_cooldown_state()["phase"], "serving")
        self.assertTrue(self.events_of("cooldown")[-1]["active"])
        self.assertEqual(self.dc.cooldown_check("next.mp4"), str(clip))
        self.assertIsNone(self.dc.cooldown_check("after.mp4"))
        self.assertEqual(self.dc.get_cooldown_state()["phase"], "idle")
        self.assertEqual([event["phase"] for event in self.events_of("cooldown")], ["armed", "serving", "idle"])
        self.dc._running = False

    def test_cooldown_cannot_change_file_while_active(self):
        clip = self.root / "relax.mp4"
        clip.write_bytes(b"relax")
        self.dc._cooldown_armed = True
        with self.assertRaises(ValueError):
            self.dc.update_config({"cooldown_file": str(clip)})

    def test_cooldown_cannot_arm_after_stop_has_been_requested(self):
        clip = self.root / "relax.mp4"
        clip.write_bytes(b"relax")
        self.dc._config["cooldown_file"] = str(clip)
        self.dc._running = True
        self.dc._stop_event.set()
        self.assertFalse(self.dc.cooldown_arm())
        self.assertEqual(self.dc.get_cooldown_state()["phase"], "idle")
        self.dc._running = False

    def test_library_tasks_are_exclusive_and_can_restart_after_cancel(self):
        entered = threading.Event()
        calls = []
        def worker():
            calls.append(1)
            entered.set()
            if len(calls) == 1:
                self.dc._library_stop.wait(2)
        with mock.patch.object(self.dc, "_rebuild_worker", side_effect=worker):
            self.assertTrue(self.dc.rebuild_mapping())
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.dc.verify_library())
            self.dc.cancel_library_download()
            self.dc._rebuild_thread.join(1)
            self.assertTrue(self.dc.rebuild_mapping())
            self.dc._rebuild_thread.join(1)
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.dc._library_stop.is_set())

    def test_verify_distinguishes_youtube_from_failed_resolution_and_reports_ids(self):
        for sid in (1, 2, 3):
            (self.library / f"{sid}.mp4").write_bytes(b"abc")
        self.dc.mark_mapping(1, "yt:https://www.youtube.com/watch?v=test")
        self.dc.mark_mapping(3, "three.mp4")
        with mock.patch.object(self.dc, "resolve_video_filename", side_effect=lambda sid: "three.mp4" if sid == 3 else None), \
             mock.patch.object(self.dc, "_remote_size", return_value=10):
            self.dc._verify_worker()
        done = self.events_of("verify_done")[-1]
        self.assertEqual((done["checked"], done["bad"], done["skipped"], done["failed"]), (3, 1, 1, 1))
        self.assertEqual(done["bad_ids"], [3])
        self.assertEqual((self.library / "3.mp4").read_bytes(), b"abc")

    def test_rebuild_counts_learned_youtube_mapping_as_success(self):
        (self.library / "1.mp4").write_bytes(b"abc")
        def resolve(sid):
            self.dc.mark_mapping(sid, "yt:https://www.youtube.com/watch?v=test")
            return None
        with mock.patch.object(self.dc, "resolve_video_filename", side_effect=resolve):
            self.dc._rebuild_worker()
        done = self.events_of("rebuild_done")[-1]
        self.assertEqual((done["done"], done["failed"]), (1, 0))
        self.assertFalse(done["cancelled"])

    def test_start_can_run_cdn_with_youtube_and_watcher_unavailable(self):
        with self.service_environment(api=False) as (server, factory, resolver, hijack, restore):
            self.assertIsNone(self.dc.start_services())
            state = self.dc.get_runtime_state()
            self.assertTrue(state["running"])
            self.assertFalse(state["youtube_available"])
            self.assertFalse(state["queue_watcher_available"])
            self.assertEqual(len(state["warnings"]), 3)
            hijack.assert_called_once_with(False)
            self.dc.stop_services()
            self.assertEqual(self.dc.get_runtime_state()["phase"], "stopped")
            self.assertEqual(server.close_calls, 1)
            restore.assert_called_once()

    def test_443_failure_is_a_capability_warning_not_total_failure(self):
        with self.service_environment(api=True, bind_error="port busy"):
            self.assertIsNone(self.dc.start_services())
            state = self.dc.get_runtime_state()
            self.assertTrue(state["running"])
            self.assertFalse(state["youtube_available"])
            self.assertTrue(any("443" in message for message in state["warnings"]))

    def test_http_bind_failure_does_not_modify_hosts(self):
        with self.service_environment() as (server, factory, resolver, hijack, restore):
            factory.side_effect = OSError("port busy")
            self.assertIn("80", self.dc.start_services())
            self.assertEqual(self.dc.get_runtime_state()["phase"], "failed")
            hijack.assert_not_called()
            restore.assert_not_called()
            self.assertIsNone(self.dc.stop_services())
            self.assertEqual(self.dc.get_runtime_state()["phase"], "stopped")
            self.assertIsNone(self.dc.get_runtime_state()["error"])
            restore.assert_not_called()

    def test_partial_hosts_failure_closes_unstarted_server_without_shutdown(self):
        with self.service_environment() as (server, factory, resolver, hijack, restore):
            hijack.side_effect = OSError("hosts write failed")
            self.assertIn("hosts", self.dc.start_services())
            self.assertEqual(server.shutdown_calls, 0)
            self.assertEqual(server.close_calls, 1)
            restore.assert_called_once()
            self.assertFalse(self.dc.is_running())

    def test_stop_failure_is_visible_in_runtime_state(self):
        with self.service_environment() as (server, factory, resolver, hijack, restore):
            self.assertIsNone(self.dc.start_services())
            restore.return_value = False
            self.assertIn("hosts", self.dc.stop_services())
            state = self.dc.get_runtime_state()
            self.assertFalse(state["running"])
            self.assertEqual(state["phase"], "failed")
            self.assertIn("hosts", state["error"])
            self.assertTrue(state["hosts_restore_pending"])
            self.assertIn("hosts", self.dc.start_services())
            self.assertEqual(factory.call_count, 1)
            restore.return_value = True
            self.assertIsNone(self.dc.stop_services())
            self.assertEqual(restore.call_count, 2)
            self.assertEqual(server.shutdown_calls, 1)
            state = self.dc.get_runtime_state()
            self.assertEqual(state["phase"], "stopped")
            self.assertFalse(state["hosts_restore_pending"])
            self.assertIsNone(state["error"])

    def test_partial_start_failure_with_failed_restore_can_retry_cleanup(self):
        with self.service_environment() as (server, factory, resolver, hijack, restore):
            hijack.side_effect = OSError("hosts write failed")
            restore.return_value = False
            self.assertIn("hosts", self.dc.start_services())
            self.assertFalse(self.dc.is_running())
            self.assertTrue(self.dc.get_runtime_state()["hosts_restore_pending"])
            restore.return_value = True
            self.assertIsNone(self.dc.stop_services())
            self.assertEqual(restore.call_count, 2)
            self.assertEqual(server.shutdown_calls, 0)
            self.assertFalse(self.dc.get_runtime_state()["hosts_restore_pending"])

    def test_hosts_watchdog_never_rewrites_after_stop_is_requested(self):
        hosts = self.root / "hosts-never-used"
        original = "127.0.0.1 localhost\n"
        hosts.write_text(original, encoding="utf-8")
        original_open = open

        @contextmanager
        def stop_during_read(path, mode="r", **kwargs):
            with original_open(path, mode, **kwargs) as handle:
                self.dc._stop_event.set()
                yield handle

        with mock.patch.object(self.dc._stop_event, "wait", return_value=False), \
             mock.patch("builtins.open", side_effect=stop_during_read):
            self.dc._hosts_watchdog_loop(False)
        self.assertEqual(hosts.read_text(encoding="utf-8"), original)

    def test_duplicate_start_requests_are_serialized(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        with self.service_environment() as (server, factory, resolver, hijack, restore):
            def resolve():
                entered.set()
                release.wait(2)
                self.dc._origin_ips["cdn.pypy.dance"] = ["1.1.1.1"]
            resolver.side_effect = resolve
            first = threading.Thread(target=lambda: results.append(self.dc.start_services()))
            second = threading.Thread(target=lambda: results.append(self.dc.start_services()))
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            release.set()
            first.join(2)
            second.join(2)
            self.assertEqual(results, [None, None])
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(hijack.call_count, 1)

    def test_stop_during_start_cancels_before_hosts_are_changed(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        with self.service_environment() as (server, factory, resolver, hijack, restore):
            def resolve():
                entered.set()
                release.wait(2)
                self.dc._origin_ips["cdn.pypy.dance"] = ["1.1.1.1"]
            resolver.side_effect = resolve
            starter = threading.Thread(target=lambda: results.append(self.dc.start_services()))
            stopper = threading.Thread(target=self.dc.stop_services)
            starter.start()
            self.assertTrue(entered.wait(1))
            stopper.start()
            self.assertTrue(self.dc._stop_event.wait(1))
            release.set()
            starter.join(2)
            stopper.join(2)
            self.assertEqual(results, ["启动已取消"])
            self.assertEqual(self.dc.get_runtime_state()["phase"], "stopped")
            hijack.assert_not_called()
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
