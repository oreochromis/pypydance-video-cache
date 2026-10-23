"""Qt desktop application.  Run with --demo for an isolated visual preview."""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, Slot, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFileDialog, QHBoxLayout, QHeaderView,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QScrollArea,
    QStackedWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .appearance import AppearancePage
from .controller import CoreController
from .pages import Pages, button, row
from .theme import ThemeStore
from .windows_taskbar import set_windows_application_identity
from .widgets import Backdrop, TextHalo, app_icon, bytes_text, label, set_status, status_label, stylesheet


STATES = {
    "cached": ("已缓存", "success"), "downloading": ("下载中", "info"),
    "failed": ("下载失败", "error"), "cancelled": ("已取消", "muted"),
    "youtube": ("等待播放", "muted"), "external": ("外部源", "muted"),
    "unresolved": ("待解析", "muted"), "not_cached": ("未缓存", "muted"),
    "cooldown": ("放松视频", "warning"),
}
SOURCE_NAMES = {"cdn": "CDN", "youtube": "YouTube", "wannadance": "WannaDance", "external": "外部源"}


def _version_newer(latest, current):
    """语义化版本比较：latest 高于 current 返回 True。"""
    def parts(value):
        out = []
        for piece in str(value).lstrip("vV").split("."):
            digits = "".join(ch for ch in piece if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return out
    new, old = parts(latest), parts(current)
    length = max(len(new), len(old))
    new += [0] * (length - len(new))
    old += [0] * (length - len(old))
    return new > old


class MainWindow(QMainWindow, Pages):
    def __init__(self, controller: CoreController, store: ThemeStore):
        super().__init__()
        self.controller, self.store = controller, store
        self.controller.configure_steamvr(store.root_dir)
        from .cdn_favorites import CDNFavorites
        self.cdn_favorites = CDNFavorites(store.root_dir)
        self._syncing = False
        self._dirty = {"storage": False, "settings": False}
        self.apply_buttons, self.apply_hints = {}, {}
        self._runtime = {"running": False, "phase": "stopped", "warnings": []}
        self._cooldown = {"phase": "idle", "available": False}
        self._queue = []
        self._downloads = {}
        self._scan_results = []
        self._steamvr_saved = None
        self._steamvr_available = False
        self._steamvr_dirty = False
        self._steamvr_syncing = False
        self._steamvr_pending = None
        self._steamvr_busy = False
        self._maintenance = None
        self._can_close = False
        self._closing = False
        self._runtime_warning = ""
        self._update_manual = False
        self._update_prompted = False
        self._banner_generation = 0
        self._row_height = 48
        self._vr_overlay = None  # load_fields 会先于服务创建调用 _sync_vr_overlay
        self.setWindowTitle("VRChat 跳舞房助手" + (" · 界面演示" if controller.demo else ""))
        self.setWindowIcon(app_icon())
        self.resize(1100, 920)
        self.setMinimumSize(850, 680)
        if QApplication.platformName() not in ("offscreen", "minimal") and self.screen():
            available = self.screen().availableGeometry()
            self.resize(min(1100, max(850, available.width() - 60)), min(920, max(680, available.height() - 60)))
            # 显式居中：Windows 偶尔会按记忆中的位置把窗口摆到（已拔除的）副屏区域外
            geo = self.geometry()
            self.move(available.x() + max(0, (available.width() - geo.width()) // 2),
                      available.y() + max(0, (available.height() - geo.height()) // 2))

        self.backdrop = Backdrop()
        self.setCentralWidget(self.backdrop)
        outer = QVBoxLayout(self.backdrop)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.build_header(outer)
        self.banner = label("", wrap=True)
        self.banner.setContentsMargins(26, 10, 26, 10)
        self.banner.hide()
        outer.addWidget(self.banner)
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("QStackedWidget {background: transparent;}")
        self.stack.addWidget(self.build_run_page())
        self.stack.addWidget(self.build_storage_page())
        self.stack.addWidget(self.build_settings_page())
        self.render_favorites()
        self.appearance_page = AppearancePage(store)
        self.stack.addWidget(self.appearance_page)
        outer.addWidget(self.stack, 1)
        self.build_logs_and_footer(outer)
        self._appearance_timer = QTimer(self)
        self._appearance_timer.setSingleShot(True)
        self._appearance_timer.setInterval(65)
        self._appearance_timer.timeout.connect(self.apply_appearance)
        self.appearance_page.changed.connect(lambda: self._appearance_timer.start())
        self.controller.event.connect(self.on_event)
        self.controller.result.connect(self.on_result)
        self.controller.error.connect(self.on_error)
        self.controller.busy_changed.connect(self.on_busy)
        self._download_timer = QTimer(self)
        self._download_timer.setSingleShot(True)
        self._download_timer.setInterval(125)
        self._download_timer.timeout.connect(self.render_downloads)
        self.load_fields()
        self.apply_appearance()
        self.render_queue()
        self.render_downloads()
        QTimer.singleShot(0, self.refresh_steamvr_startup)
        # SteamVR 面板页（实验）：仅真机 Windows 启用；跟随配置热启停
        if os.name == "nt" and not controller.demo:
            from . import vr_overlay
            self._vr_overlay = vr_overlay.VROverlayService()
            self._vr_overlay.bridge.action_requested.connect(self._on_vr_action)
            QTimer.singleShot(0, self._sync_vr_overlay)

    def build_header(self, outer):
        header = QWidget()
        header.setObjectName("header")
        box = QHBoxLayout(header)
        box.setContentsMargins(16, 0, 22, 0)
        box.setSpacing(5)
        self.nav_group = QButtonGroup(self)
        self.nav_buttons = []
        for index, title in enumerate(("运行", "存储", "设置", "外观")):
            nav = button(title, lambda checked=False, i=index: self.navigate(i))
            nav.setProperty("nav", True)
            nav.setCheckable(True)
            nav.setAccessibleName(title + "页面")
            self.nav_group.addButton(nav, index)
            self.nav_buttons.append(nav)
            box.addWidget(nav)
        self.nav_buttons[0].setChecked(True)
        box.addStretch()
        self.service_status = label("●  已停止")
        box.addWidget(self.service_status)
        box.addSpacing(12)
        self.service_button = button("启动服务", self.toggle_service)
        box.addWidget(self.service_button)
        outer.addWidget(header)

    def build_logs_and_footer(self, outer):
        self.logs_panel = QWidget()
        layout = QVBoxLayout(self.logs_panel)
        layout.setContentsMargins(26, 12, 26, 12)
        heading, heading_box = row(label("运行日志"))
        heading_box.addStretch()
        heading_box.addWidget(button("复制", self.copy_logs, link=True))
        heading_box.addWidget(button("导出", self.export_logs, link=True))
        heading_box.addWidget(button("导出诊断", self.export_diagnostics, link=True))
        heading_box.addWidget(button("收起", self.toggle_logs, link=True))
        layout.addWidget(heading)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(2000)
        self.logs.setFixedHeight(145)
        self.logs.setAccessibleName("最近的运行日志")
        layout.addWidget(self.logs)
        outer.addWidget(self.logs_panel)
        footer = QWidget()
        footer.setObjectName("footer")
        footer_box = QHBoxLayout(footer)
        footer_box.setContentsMargins(26, 9, 26, 9)
        footer_box.addWidget(label("VRChat 跳舞房助手  ·  " + __version__, muted=True))
        self.update_label = button("", self.open_release_page, link=True)
        self.update_label.setToolTip("前往 GitHub Releases 下载新版本")
        self.update_label.hide()
        footer_box.addWidget(self.update_label)
        if self.controller.demo:
            footer_box.addWidget(status_label("演示数据 · 不启动服务", "warning"))
        footer_box.addStretch()
        self.logs_toggle = button("查看日志", self.toggle_logs, link=True)
        footer_box.addWidget(self.logs_toggle)
        outer.addWidget(footer)

    def navigate(self, index):
        self.stack.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)
        if index == 1:
            self.controller.refresh_stats()
        elif index == 3:
            self.appearance_page.refresh()

    def apply_appearance(self):
        theme, preferences = self.store.active, self.store.preferences
        self.backdrop.set_appearance(theme, self.store.background_path())
        self.setStyleSheet(stylesheet(theme, preferences))
        scale = preferences.get("scale", 100) / 100
        self._row_height = round({"comfortable": 52, "standard": 44, "compact": 36}[preferences["density"]] * scale)
        for widget in (self.queue_table, self.download_table, self.scan_table, self.favorites_table):
            widget.verticalHeader().setDefaultSectionSize(self._row_height)
        self.download_table.verticalHeader().setDefaultSectionSize(self._row_height + round(8 * scale))
        self.queue_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.queue_table.setColumnWidth(3, round(118 * scale))
        self.render_downloads()
        self.resize_tables()
        self.logs_panel.setVisible(preferences["logs_expanded"])
        self.logs_toggle.setText("收起日志" if preferences["logs_expanded"] else "查看日志")
        self.render_runtime()
        if theme["text_shadow_opacity"] > 0:
            self.song_title.setGraphicsEffect(TextHalo(
                theme["text_shadow_color"], theme["text_shadow_opacity"],
                theme["text_shadow_size"], theme["text_shadow_blur"], self.song_title,
            ))
        else:
            self.song_title.setGraphicsEffect(None)

    def download_event(self, kind, data):
        key = data.get("task_id") or data.get("filename")
        if not key:
            return
        previous = self._downloads.get(key, {})
        now = time.monotonic()
        task = dict(previous)
        task.update(data)
        if kind == "download_start":
            task.update(status="active", done=0, total=0, speed=0)
        elif kind == "progress":
            elapsed = now - previous.get("updated", now)
            if "demo_speed" in data:
                task["speed"] = data["demo_speed"]
            elif elapsed > .02:
                task["speed"] = max(0, (data.get("done", 0) - previous.get("done", 0)) / elapsed)
            task["status"] = "active"
        else:
            task["status"] = "cancelled" if data.get("cancelled") else "done" if data.get("ok") else "failed"
            task["speed"] = 0
            if task["status"] == "failed":
                self.append_log("[下载失败] " + str(task.get("filename", key)) + "：" + str(task.get("error") or "连接中断或文件未完整接收"))
        task["updated"] = now
        self._downloads[key] = task
        ended = [key for key, item in self._downloads.items() if item.get("status") != "active"]
        for key in ended[:-30]:
            self._downloads.pop(key, None)
        if kind == "progress":
            if not self._download_timer.isActive():
                self._download_timer.start()
        else:
            self.render_downloads()

    def clear_download_history(self):
        self._downloads = {key: value for key, value in self._downloads.items() if value.get("status") == "active"}
        self.render_downloads()

    def render_downloads(self):
        tasks = sorted(self._downloads.values(), key=lambda item: (item.get("status") != "active", -item.get("updated", 0)))
        self.download_table.setRowCount(len(tasks))
        active = sum(item.get("status") == "active" for item in tasks)
        self.download_count.setText(f"{active} 项进行中" if active else "暂无进行中的任务")
        self.download_empty.setVisible(not tasks)
        self.clear_history_button.setVisible(any(item.get("status") != "active" for item in tasks))
        self.download_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.download_table.setColumnWidth(1, max(220, round(240 * self.store.preferences["scale"] / 100)))
        for index, task in enumerate(tasks):
            name = next((song.get("name") for song in self._queue if (
                task.get("song_id") is not None and song.get("song_id") == task["song_id"]
            ) or (task.get("filename") and song.get("filename") == task["filename"])), None)
            self.download_table.setItem(index, 0, self.cell(name or task.get("filename"), task.get("filename", "")))
            total, done = max(0, task.get("total", 0) or 0), max(0, task.get("done", 0) or 0)
            progress_body = QWidget()
            progress_layout = QVBoxLayout(progress_body)
            progress_layout.setContentsMargins(8, 6, 8, 6)
            progress_layout.setSpacing(4)
            progress = QProgressBar()
            progress.setTextVisible(False)
            status = task.get("status", "active")
            if total:
                progress.setRange(0, 1000)
                progress.setValue(min(1000, round(done / total * 1000)))
                detail = f"{bytes_text(done)} / {bytes_text(total)}  ·  {min(100, round(done / total * 100))}%"
            elif status == "active":
                progress.setRange(0, 0)
                detail = "已接收 " + bytes_text(done) + " · 大小未知"
            else:
                progress.setRange(0, 1000)
                progress.setValue(1000 if status == "done" else 0)
                detail = "已接收 " + bytes_text(done)
            progress_layout.addWidget(progress)
            progress_layout.addWidget(label(detail, muted=True))
            self.download_table.setCellWidget(index, 1, progress_body)
            self.download_table.setItem(index, 2, self.cell(bytes_text(task.get("speed", 0)) + "/s" if status == "active" else "—"))
            title, color = {"active": ("下载中", "info"), "done": ("已完成", "success"),
                            "failed": ("失败", "error"), "cancelled": ("已取消", "muted")}[status]
            badge = self.badge_cell(title, color)
            badge.setToolTip(str(task.get("error") or title))
            self.download_table.setCellWidget(index, 3, badge)
        self.resize_tables()

    def render_runtime(self):
        running, phase = self._runtime.get("running", False), self._runtime.get("phase", "stopped")
        previous_warning = self._runtime_warning
        self._runtime_warning = "；".join(self._runtime.get("warnings") or [])
        title = {"starting": "正在启动", "stopping": "正在停止", "failed": "服务异常"}.get(phase)
        if not title:
            title = "部分功能可用" if running and self._runtime_warning else "运行中" if running else "已停止"
        if self.controller.demo:
            title = "演示 · " + title
        self.service_status.setText("●  " + title)
        warnings = self._runtime.get("warnings") or []
        self.service_status.setToolTip("当前问题：\n• " + "\n• ".join(warnings) if warnings else "")
        color = "#A97823" if phase in ("starting", "stopping") or self._runtime_warning else "#20824E" if running else "#8290A6"
        if running and self.store.active.get("base") == "dark" and not self._runtime_warning:
            color = "#7CDBA5"
        if phase == "failed":
            color = "#C64D5C"
        self.service_status.setStyleSheet("color:" + color + ";")
        self.service_button.setText("启动中…" if phase == "starting" else "停止中…" if phase == "stopping" else "停止服务" if running else "启动服务")
        if not running and self._runtime.get("hosts_restore_pending") and phase not in ("starting", "stopping"):
            self.service_button.setText("恢复系统设置")
        self.service_button.setEnabled(phase not in ("starting", "stopping"))
        if self._runtime_warning:
            self.notify(self._runtime_warning, persistent=True)
        elif self._runtime.get("error"):
            self.notify(str(self._runtime["error"]), error=True)
        elif previous_warning and self.banner.text() == previous_warning:
            self.banner.clear()
            self.banner.hide()
        self.render_cooldown()

    def render_cooldown(self):
        state = self._cooldown
        phase = state.get("phase", "idle")
        available = state.get("available", False)
        filename = self.controller.config().get("cooldown_file", "")
        self.cooldown_file_label.setText(filename or ("内置默认放松视频" if available else "内置默认放松视频不可用"))
        self.cooldown_file_label.setToolTip(filename)
        if phase == "armed":
            set_status(self.cooldown_badge, "等待替换", "warning")
            self.cooldown_hint.setText("已经就绪，将在下一次视频请求时使用放松视频。")
            self.cooldown_button.setText("取消等待")
        elif phase == "serving":
            set_status(self.cooldown_badge, "正在替换", "info")
            self.cooldown_hint.setText("正在提供本地放松视频。退出后，后续请求恢复原视频。")
            self.cooldown_button.setText("退出 CoolDown")
        else:
            set_status(self.cooldown_badge, "未启用")
            if available:
                hint = "休息一下，将下一次视频请求替换为本地放松视频。"
            elif filename:
                hint = "自选放松视频不存在或不可用，请在设置中重新选择，或改用内置视频并应用更改。"
            else:
                hint = "内置放松视频缺失或不可用，请重新完整解压程序，或在设置中选择本地 MP4。"
            self.cooldown_hint.setText(hint)
            self.cooldown_button.setText("启用 CoolDown" if available else "选择放松视频")
        self.cooldown_button.setEnabled((not available or self._runtime.get("running", False)) and not self.controller.busy("cooldown"))

    def render_stats(self, data):
        used, limit = float(data.get("total_mb", 0)), float(data.get("limit_mb", 0))
        self.cache_summary.setText(f"{bytes_text(used * 1024**2)}  /  {bytes_text(limit * 1024**2)}")
        self.cache_detail.setText(f"{data.get('count', 0)} 个视频文件 · 临时缓存用量")
        self.cache_usage.setValue(min(1000, round(used / limit * 1000)) if limit else 0)
        self.cache_usage.setAccessibleName(f"已用 {used:.0f} MB，上限 {limit:.0f} MB")
        lib_count, lib_mb = data.get("lib_count", 0), data.get("lib_mb", 0)
        wd_count, wd_mb = data.get("wd_count", 0), data.get("wd_mb", 0)
        cfg = self.controller.config()
        if cfg.get("library_dir"):
            self.library_summary.setText(f"{lib_count} 个视频 · {bytes_text(lib_mb * 1024**2)}")
        elif cfg.get("cdn_permanent") or cfg.get("youtube_permanent"):
            self.library_summary.setText("尚未设置曲库目录（永久保存暂缓生效）")
        else:
            self.library_summary.setText("尚未设置曲库目录")
        if cfg.get("wd_library_dir"):
            self.wd_library_summary.setText(f"{wd_count} 个视频 · {bytes_text(wd_mb * 1024**2)}")
        elif cfg.get("wd_permanent"):
            self.wd_library_summary.setText("尚未设置曲库目录（永久保存暂缓生效）")
        else:
            self.wd_library_summary.setText("尚未设置曲库目录")
    def resize_tables(self):
        for widget, limit in ((self.queue_table, 4), (self.download_table, 3), (self.scan_table, 4), (self.favorites_table, 6)):
            rows = min(limit, widget.rowCount())
            widget.setFixedHeight(rows * widget.verticalHeader().defaultSectionSize() + widget.horizontalHeader().sizeHint().height() + 4)
            widget.setVisible(widget.rowCount() > 0)

    def load_fields(self, section=None):
        config = self.controller.config()
        self._syncing = True
        try:
            if section in (None, "storage"):
                self.cache_path.setText(config["cache_dir"])
                self.cache_limit.setValue(config["max_cache_mb"])
                self.library_path.setText(config["library_dir"])
                self.wd_library_path.setText(config.get("wd_library_dir", ""))
                self.cdn_permanent.setChecked(config["cdn_permanent"])
                self.wd_permanent.setChecked(config.get("wd_permanent", False))
                self.youtube_permanent.setChecked(config["youtube_permanent"])
            if section in (None, "settings"):
                self.preload_count.setMaximum(max(10, config["preload_count"]))
                self.preload_count.setValue(config["preload_count"])
                self.preload_sequential.setChecked(config.get("preload_sequential", True))
                self.bundle_direct.setChecked(config.get("bundle_direct", True))
                self.youtube_enabled.setChecked(config["enable_youtube_cache"])
                self.cooldown_path.setText(config["cooldown_file"])
                self.preferred_ip.setText(config["preferred_cdn_ip"])
                self.auto_prefer_cdn.setChecked(config.get("auto_prefer_cdn", True))
                self.vr_overlay.setChecked(config.get("vr_overlay", False))
            for name in (section,) if section else ("storage", "settings"):
                self._dirty[name] = False
                self.apply_buttons[name].setEnabled(False)
                self.apply_hints[name].setText("已保存" + (" · 仅本次演示有效" if self.controller.demo else ""))
        finally:
            self._syncing = False
        self.render_cooldown()
        self._sync_vr_overlay()

    def _sync_vr_overlay(self):
        """按当前配置启停 SteamVR 面板页（应用设置后热生效）"""
        if self._vr_overlay is None:
            return
        if self.controller.config().get("vr_overlay"):
            self._vr_overlay.start()
        else:
            self._vr_overlay.stop()

    def _on_vr_action(self, action):
        if action == "toggle_service" and not self._closing:
            self.toggle_service()

    def mark_dirty(self, section):
        if self._syncing:
            return
        self._dirty[section] = True
        self.apply_buttons[section].setEnabled(not self.controller.busy("apply_" + section))
        self.apply_hints[section].setText("有未应用的更改")

    def apply_settings(self, section):
        if section == "storage":
            patch = {"max_cache_mb": self.cache_limit.value(), "library_dir": self.library_path.text().strip(),
                     "wd_library_dir": self.wd_library_path.text().strip(),
                     "cdn_permanent": self.cdn_permanent.isChecked(), "wd_permanent": self.wd_permanent.isChecked(),
                     "youtube_permanent": self.youtube_permanent.isChecked()}
        else:
            patch = {"preload_count": self.preload_count.value(), "preload_sequential": self.preload_sequential.isChecked(),
                     "enable_youtube_cache": self.youtube_enabled.isChecked(), "bundle_direct": self.bundle_direct.isChecked(),
                     "vr_overlay": self.vr_overlay.isChecked(),
                     "cooldown_file": self.cooldown_path.text().strip(), "preferred_cdn_ip": self.preferred_ip.text().strip(),
                     "auto_prefer_cdn": self.auto_prefer_cdn.isChecked()}
        self.controller.apply_config(patch, section)

    def render_steamvr_controls(self):
        editable = self._steamvr_available and self._steamvr_saved is not None and not self._steamvr_busy
        self.steamvr_startup.setEnabled(editable)
        self.steamvr_apply_button.setEnabled(editable and self._steamvr_dirty)
        self.steamvr_refresh_button.setEnabled(not self._steamvr_busy)

    def mark_steamvr_dirty(self):
        if self._steamvr_syncing or self._steamvr_saved is None:
            return
        self._steamvr_dirty = self.steamvr_startup.isChecked() != self._steamvr_saved
        self.steamvr_hint.setText("启动设置有未应用的更改。" if self._steamvr_dirty else (
            "已启用跟随 SteamVR 启动。" if self._steamvr_saved else "未启用跟随 SteamVR 启动。"))
        self.render_steamvr_controls()

    def refresh_steamvr_startup(self):
        if self._closing or self._can_close or self._steamvr_busy:
            return
        self._steamvr_pending = "read"
        self.controller.steamvr_status()

    def apply_steamvr_startup(self):
        if self._steamvr_busy or not self._steamvr_available or not self._steamvr_dirty:
            return
        self._steamvr_pending = "save"
        self.controller.set_steamvr_startup(self.steamvr_startup.isChecked())

    def show_steamvr_status(self, result):
        result = result or {}
        self._steamvr_available = bool(result.get("available"))
        known = self._steamvr_available and isinstance(result.get("enabled"), bool)
        preserve_draft = self._steamvr_pending != "save" and self._steamvr_dirty
        self._steamvr_saved = result["enabled"] if known else None
        self._steamvr_syncing = True
        try:
            if not preserve_draft:
                self.steamvr_startup.setCheckState(
                    Qt.CheckState.Checked if self._steamvr_saved is True else
                    Qt.CheckState.Unchecked if self._steamvr_saved is False else Qt.CheckState.PartiallyChecked)
                self.steamvr_startup.setTristate(not known)
                self._steamvr_dirty = False
            elif known:
                self._steamvr_dirty = self.steamvr_startup.isChecked() != self._steamvr_saved
        finally:
            self._steamvr_syncing = False
        message = str(result.get("message") or (
            "已启用跟随 SteamVR 启动。" if self._steamvr_saved is True else
            "未启用跟随 SteamVR 启动。" if self._steamvr_saved is False else "无法确认 SteamVR 启动状态。"))
        if self._steamvr_dirty:
            message += " 启动设置有未应用的更改。"
        self.steamvr_hint.setText(message)
        self.render_steamvr_controls()

    def browse_library(self):
        path = QFileDialog.getExistingDirectory(self, "选择永久曲库目录", self.library_path.text())
        if path:
            self.library_path.setText(path)

    def browse_wd_library(self):
        path = QFileDialog.getExistingDirectory(self, "选择 WannaDance 曲库目录", self.wd_library_path.text())
        if path:
            self.wd_library_path.setText(path)

    def open_wd_library(self):
        saved = self.controller.config().get("wd_library_dir", "")
        if not saved:
            self.notify("请先选择 WannaDance 曲库目录并应用更改。")
            return
        self.controller.open_directory(saved)

    def browse_cooldown(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择放松视频", self.cooldown_path.text(), "MP4 视频 (*.mp4)")
        if path:
            self.cooldown_path.setText(path)

    def open_cache(self):
        self.controller.open_directory(self.controller.config()["cache_dir"])

    def open_library(self):
        saved = self.controller.config()["library_dir"]
        if not saved:
            self.notify("请先选择曲库目录并应用更改。")
            return
        self.controller.open_directory(saved)

    def clear_cache(self):
        if QMessageBox.question(self, "清理临时缓存", "清理临时缓存目录中的 MP4 文件？\n正在使用的文件会保留，永久曲库不会清理。",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.controller.clear_cache()

    def toggle_service(self):
        if self._runtime.get("running") or self._runtime.get("hosts_restore_pending"):
            self.controller.stop()
        else:
            self.controller.start()

    def toggle_cooldown(self):
        if not self._cooldown.get("available"):
            self.navigate(2)
            self.cooldown_path.setFocus()
            return
        self.controller.cooldown()

    def start_maintenance(self, kind):
        if self._maintenance:
            return
        if self._dirty["storage"]:
            self.notify("请先应用存储设置，再开始曲库维护。")
            return
        self._maintenance = kind
        self.rebuild_button.setEnabled(False)
        self.verify_button.setEnabled(False)
        self.cancel_maintenance_button.setEnabled(True)
        self.maintenance_progress.setRange(0, 0)
        self.maintenance_progress.show()
        self.maintenance_status.setText("正在准备" + ("重建索引…" if kind == "rebuild" else "校验曲库…"))
        self.controller.maintenance(kind)

    def cancel_maintenance(self):
        self.cancel_maintenance_button.setEnabled(False)
        self.maintenance_status.setText("正在取消，请等待当前网络请求结束…")
        self.controller.cancel_maintenance()

    def finish_maintenance(self):
        self._maintenance = None
        self.rebuild_button.setEnabled(True)
        self.verify_button.setEnabled(True)
        self.cancel_maintenance_button.setEnabled(False)

    def start_scan(self, full):
        if full and QMessageBox.question(self, "扩大范围测速（抽样）", "将从更大范围的 CDN IP 中抽样连接并下载测试数据，不会穷举全部 IP，可能需要数分钟。现在开始？",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                         QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.quick_scan_button.setEnabled(False)
        self.full_scan_button.setEnabled(False)
        self.use_ip_button.hide()
        self.favorite_ip_button.hide()
        self._scan_results = []
        self.scan_table.setRowCount(0)
        self.scan_table.hide()
        self.scan_status.setText("正在扩大范围抽样测速，请稍候…" if full else "正在测试候选 CDN 地址…")
        self.controller.scan(full=full)

    def finish_scan(self, results):
        if results is not None:
            self._scan_results = list(results)
        self.quick_scan_button.setEnabled(True)
        self.full_scan_button.setEnabled(True)
        valid = [item for item in self._scan_results if not item.get("error") and item.get("mbps", 0) > 0]
        self._scan_results.sort(key=lambda item: item.get("mbps", 0) or 0, reverse=True)
        self.scan_table.setRowCount(len(self._scan_results))
        for index, item in enumerate(self._scan_results):
            self.scan_table.setItem(index, 0, self.cell(item.get("ip", "—")))
            self.scan_table.setItem(index, 1, self.cell(f"{item.get('mbps', 0):.2f} MB/s" if not item.get("error") else "—"))
            self.scan_table.setItem(index, 2, self.cell("可用" if item in valid else "未连通", item.get("error", "")))
        self.scan_status.setText(f"测速完成 · {len(valid)} 个可用地址。选择地址后，点击“使用选中的 IP”，再应用更改。" if valid else "没有测得可用地址。请检查网络，或保留自动选择。")
        self.use_ip_button.setVisible(bool(valid))
        self.favorite_ip_button.setVisible(bool(valid))
        self.resize_tables()
        if valid:
            self.scan_table.selectRow(0)

    def render_favorites(self):
        self.favorites_table.setRowCount(len(self.cdn_favorites.items))
        for index, ip in enumerate(self.cdn_favorites.items):
            self.favorites_table.setItem(index, 0, self.cell(ip))
        self.favorites_hint.setText(self.cdn_favorites.warning or (
            "收藏保存在本机，重启后保留；历史收藏不代表当前速度，建议定期测速。" if self.cdn_favorites.items
            else "暂无收藏。测速完成后选中地址，点击“收藏选中的 IP”。"))
        self.resize_tables()
        if self.cdn_favorites.items:
            self.favorites_table.selectRow(0)

    def favorite_selected_ip(self):
        index = self.scan_table.currentRow()
        if index < 0 or index >= len(self._scan_results):
            self.notify("请先选择一个测速结果。")
            return
        item = self._scan_results[index]
        if item.get("error") or (item.get("mbps") or 0) <= 0:
            self.notify("请选择测速成功的地址。")
            return
        self.save_favorites([*self.cdn_favorites.items, item["ip"]])

    def save_favorites(self, items):
        try:
            self.cdn_favorites.save(items)
        except (ValueError, OSError) as exc:
            self.notify(f"CDN 收藏保存失败：{exc}")
            return
        self.render_favorites()
        self.notify("CDN 收藏已保存到本机。")

    def use_favorite_ip(self):
        index = self.favorites_table.currentRow()
        if 0 <= index < len(self.cdn_favorites.items):
            self.preferred_ip.setText(self.cdn_favorites.items[index])
            self.notify("已填入收藏 IP，应用更改后生效。")
        else:
            self.notify("请先选择一个收藏地址。")

    def remove_favorite_ip(self):
        index = self.favorites_table.currentRow()
        if 0 <= index < len(self.cdn_favorites.items):
            self.save_favorites([ip for i, ip in enumerate(self.cdn_favorites.items) if i != index])
        else:
            self.notify("请先选择一个收藏地址。")

    def use_selected_ip(self):
        index = self.scan_table.currentRow()
        if index < 0 or index >= len(self._scan_results):
            self.notify("请先选择一个测速结果。")
            return
        item = self._scan_results[index]
        if item.get("error") or item.get("mbps", 0) <= 0:
            self.notify("该地址没有测得可用速度，请选择其他地址。")
            return
        self.preferred_ip.setText(item["ip"])
        self.notify("已填入优选 IP，应用更改后生效。")

    def toggle_logs(self):
        try:
            self.store.change_preferences({"logs_expanded": not self.store.preferences["logs_expanded"]})
            self.apply_appearance()
        except (ValueError, OSError) as exc:
            self.notify(str(exc), error=True)

    def copy_logs(self):
        QApplication.clipboard().setText(self.logs.toPlainText())
        self.notify("运行日志已复制。")

    def export_logs(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出运行日志", "vrchat-dance-log.txt", "文本文件 (*.txt)")
        if not path:
            return
        try:
            Path(path).write_text(self.logs.toPlainText(), encoding="utf-8")
        except OSError as exc:
            self.notify("导出失败：" + str(exc), error=True)
        else:
            self.notify("运行日志已导出。")

    def export_diagnostics(self):
        if self.controller.demo:
            self.notify("演示模式不采集本机诊断信息。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出诊断信息", "vrchat-dance-diagnostics.txt", "文本文件 (*.txt)")
        if not path:
            return
        self._diagnostics_path = path
        self.notify("正在收集诊断信息…")
        self.controller.collect_diagnostics()

    def _write_diagnostics(self, text):
        path = getattr(self, "_diagnostics_path", None)
        self._diagnostics_path = None
        if not path:
            return
        content = (f"VRChat 跳舞房助手 诊断信息\n版本: {__version__}\n\n" + text
                   + "\n===== 运行日志 =====\n" + self.logs.toPlainText() + "\n")
        try:
            Path(path).write_text(content, encoding="utf-8")
        except OSError as exc:
            self.notify("导出失败：" + str(exc), error=True)
        else:
            self.notify("诊断信息已导出。反馈问题时请附上该文件。")

    def open_release_page(self):
        import dance_cache
        QDesktopServices.openUrl(QUrl(dance_cache.UPDATE_CHECK_PAGE))

    def open_project_page(self):
        import dance_cache
        QDesktopServices.openUrl(QUrl(dance_cache.PROJECT_PAGE))

    def check_updates_clicked(self):
        if self.controller.demo:
            self.update_status.setText("演示模式不检查更新。")
            return
        if self.controller.busy("update_check"):
            return
        self._update_manual = True
        self.update_status.setText("正在检查更新…")
        self.update_check_button.setEnabled(False)
        self.controller.check_updates()

    def prompt_update(self, tag):
        box = QMessageBox(self)
        box.setWindowTitle("发现新版本")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"发现新版本 {tag}（当前 {__version__}）。")
        box.setInformativeText("是否前往 GitHub 下载更新？")
        update_button = box.addButton("前去更新", QMessageBox.ButtonRole.AcceptRole)
        later_button = box.addButton("下次再提醒", QMessageBox.ButtonRole.RejectRole)
        never_button = box.addButton("该版本不再提醒", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(update_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is update_button:
            self.open_release_page()
        elif clicked is never_button:
            try:
                self.store.change_preferences({"dismissed_update_version": tag})
            except Exception as exc:
                self.notify("保存偏好失败：" + str(exc), error=True)
            else:
                self.update_label.hide()
                self.append_log(f"已设置不再提醒版本 {tag}；该版本的更新信息仍可在 设置 → 更新 中查看。")

    def render_session(self, data):
        self.session_stats.setText(
            f"本次会话：本地命中 {data.get('hits', 0)} 首 · 回源 {data.get('misses', 0)} 首"
            f" · 节省流量 {bytes_text(data.get('saved_mb', 0) * 1024**2)}")

    def append_log(self, message):
        self.logs.appendPlainText(str(message))

    def notify(self, message, *, error=False, persistent=False):
        self._banner_generation += 1
        generation = self._banner_generation
        self.banner.setText(message)
        self.banner.setStyleSheet("background:rgba(205,78,94,24);color:#C64D5C;" if error else "")
        self.banner.setVisible(bool(message))
        if error:
            self.append_log("[界面] " + message)
        if not error and not persistent:
            QTimer.singleShot(6500, lambda: self.clear_notice(generation))

    def clear_notice(self, generation):
        if generation == self._banner_generation:
            self.banner.setText(self._runtime_warning)
            self.banner.setVisible(bool(self._runtime_warning))

    @staticmethod
    def cell(value, tooltip=""):
        item = QTableWidgetItem(str(value or "—"))
        item.setToolTip(str(tooltip or value or ""))
        return item

    @staticmethod
    def badge_cell(text, state):
        body, layout = row()
        layout.setContentsMargins(8, 0, 8, 0)
        layout.addWidget(status_label(text, state), 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch()
        return body

    def render_queue(self):
        if self._queue:
            current = self._queue[0]
            self.song_title.setText(current.get("name") or "未命名歌曲")
            source = SOURCE_NAMES.get(current.get("source"), "待识别")
            destination = {"library": "永久曲库", "cache": "临时缓存"}.get(current.get("storage"))
            details = ["点歌人：" + str(current.get("player") or "未知"), "来源：" + source]
            if current.get("song_id") is not None:
                details.append("ID " + str(current["song_id"]))
            if destination:
                details.append(destination)
            self.song_meta.setText("  ·  ".join(details))
            name, state = STATES.get(current.get("state_code"), (current.get("state", "待解析"), "muted"))
            set_status(self.song_state, name, state)
        else:
            self.song_title.setText("等待房间队列")
            self.song_meta.setText("进入 PyPyDance / WannaDance 房间后，这里会显示最近检测到的队列首曲。")
            set_status(self.song_state, "等待检测")
        upcoming = self._queue[1:]
        self.queue_table.setRowCount(len(upcoming))
        for index, song in enumerate(upcoming):
            self.queue_table.setItem(index, 0, self.cell(f"{index + 1:02}"))
            self.queue_table.setItem(index, 1, self.cell(song.get("name", "未命名歌曲")))
            self.queue_table.setItem(index, 2, self.cell(song.get("player", "未知")))
            name, state = STATES.get(song.get("state_code"), (song.get("state", "待解析"), "muted"))
            badge = self.badge_cell(name, state)
            badge.setToolTip(str(song.get("error") or name))
            self.queue_table.setCellWidget(index, 3, badge)
        self.queue_count.setText(f"{len(upcoming)} 首排队中" if upcoming else "尚无排队歌曲")
        self.queue_empty.setVisible(not upcoming)
        self.resize_tables()

    @Slot(str, dict)
    def on_event(self, kind, data):
        if self._can_close:
            return
        if kind == "log":
            self.append_log(data.get("msg", data.get("message", "")))
        elif kind == "warning":
            self.notify(str(data.get("message") or data.get("msg") or "启动时出现配置提示，请查看日志。"), error=True)
        elif kind == "state":
            self._runtime.update(data)
            self.render_runtime()
        elif kind == "config":
            # 后台自动修改（如自动优选 CDN 写入优选 IP）后同步界面；
            # 用户有未应用的编辑时只更新缓存，不覆盖输入框
            self.controller.reload_config()
            if not self._dirty["settings"]:
                config = self.controller.config()
                self._syncing = True
                try:
                    self.preferred_ip.setText(config["preferred_cdn_ip"])
                finally:
                    self._syncing = False
        elif kind == "queue":
            self._queue = data.get("items", [])
            self.render_queue()
        elif kind == "stats":
            self.render_stats(data)
        elif kind == "session":
            self.render_session(data)
        elif kind == "cooldown":
            self._cooldown.update(data)
            self.render_cooldown()
        elif kind in ("download_start", "progress", "download_done"):
            self.download_event(kind, data)
        elif kind in ("rebuild_progress", "verify_progress"):
            total = data.get("total", 0)
            done = data.get("done", 0) if kind.startswith("rebuild") else data.get("checked", 0)
            self.maintenance_progress.setRange(0, max(1, total))
            self.maintenance_progress.setValue(done)
            self.maintenance_progress.show()
            current = str(data.get("current") or "")
            self.maintenance_status.setText(f"{'重建索引' if kind.startswith('rebuild') else '校验曲库'}  {done} / {total}" + (" · " + current if current else ""))
        elif kind in ("rebuild_done", "verify_done"):
            self.finish_maintenance()
            if data.get("error"):
                self.maintenance_progress.hide()
                self.maintenance_status.setText("维护失败：" + str(data["error"]))
                self.notify(str(data["error"]), error=True)
                return
            total = data.get("total", 0)
            self.maintenance_progress.setRange(0, max(1, total))
            if not data.get("cancelled"):
                self.maintenance_progress.setValue(total)
            result = "已取消" if data.get("cancelled") else "维护完成"
            if kind == "rebuild_done":
                result += f" · 已关联 {data.get('done', 0)} / {total} · 失败 {data.get('failed', 0)}"
            else:
                result += f" · 已检查 {data.get('checked', total)} / {total} · 大小不符 {data.get('bad', 0)} · 跳过 {data.get('skipped', 0)} · 失败 {data.get('failed', 0)}"
            self.maintenance_status.setText(result)
            self.controller.refresh_stats()
        elif kind == "speedtest_result":
            self._scan_results.append(data)
            self.scan_status.setText(f"已测试 {len(self._scan_results)} 个地址，测速继续进行；扩大范围测速仅抽样，不穷举全部 IP。")
        elif kind == "speedtest_done":
            self.finish_scan(data.get("results"))

    @Slot(str, object)
    def on_result(self, operation, result):
        if operation == "steamvr_startup":
            self.show_steamvr_status(result)
        elif operation.startswith("apply_"):
            section = operation.removeprefix("apply_")
            self.load_fields(section)
            self.notify("设置已应用。" + ("请重新启动服务。" if (result or {}).get("restart_required") else ""))
            self.controller.refresh_stats()
        elif operation == "clear_cache":
            result = result or {}
            if result.get("demo"):
                self.notify("演示模式：没有清理任何文件。")
                return
            text = f"已清理 {result.get('removed_count', 0)} 个文件，释放 {bytes_text(result.get('removed_bytes', 0))}。"
            if result.get("skipped_busy"):
                text += f" 保留 {result['skipped_busy']} 个使用中的文件。"
            if result.get("failed_count"):
                text += f" {result['failed_count']} 个文件清理失败，详情见日志。"
                for error in result.get("errors", []):
                    self.append_log(str(error))
            self.notify(text, error=bool(result.get("failed_count")))
            self.controller.refresh_stats()
        elif operation == "update_check":
            manual = self._update_manual
            self._update_manual = False
            self.update_check_button.setEnabled(True)
            if result and _version_newer(result, __version__):
                dismissed = self.store.preferences.get("dismissed_update_version", "")
                if result != dismissed:
                    self.update_label.setText(f"发现新版本 {result} · 点击查看")
                    self.update_label.show()
                    self.append_log(f"发现新版本 {result}（当前 {__version__}），可在 GitHub Releases 下载。")
                    if not self._update_prompted:
                        self._update_prompted = True
                        self.prompt_update(result)
                if manual:
                    if result == dismissed:
                        self.update_status.setText(f"发现新版本 {result}（已对该版本关闭提醒，此处仍会显示）。")
                    else:
                        self.update_status.setText(f"发现新版本 {result}！点击页脚的更新提示前往下载。")
            elif manual:
                if result:
                    self.update_status.setText(f"已是最新版本（{__version__}）。")
                else:
                    self.update_status.setText("检查失败：无法访问 GitHub，请检查网络或稍后再试。")
        elif operation == "diagnostics":
            self._write_diagnostics(result)
        elif operation == "shutdown":
            self.controller.dispose()
            self._can_close = True
            QTimer.singleShot(0, self.close)

    @Slot(str, str)
    def on_error(self, operation, message):
        if operation == "update_check":
            if self._update_manual:
                self._update_manual = False
                self.update_status.setText("检查失败：" + message)
                self.update_check_button.setEnabled(True)
            return  # 自动检查失败（无网络等）静默忽略
        self.notify(message, error=True)
        if operation == "steamvr_startup":
            self.steamvr_hint.setText(("保存失败，更改仍保留：" if self._steamvr_pending == "save" else "读取失败，无法确认当前状态：") + message)
            self.render_steamvr_controls()
        elif operation.startswith("apply_"):
            self.apply_hints[operation.removeprefix("apply_")].setText("保存失败，更改仍保留")
        elif operation in ("rebuild", "verify"):
            self.finish_maintenance()
            self.maintenance_progress.hide()
            self.maintenance_status.setText(message)
        elif operation == "scan":
            self.quick_scan_button.setEnabled(True)
            self.full_scan_button.setEnabled(True)
            self.scan_status.setText(message)
        elif operation == "shutdown":
            self._closing = False
            self.controller._closing = False
            self.setEnabled(True)

    @Slot(str, bool)
    def on_busy(self, operation, busy):
        if operation == "steamvr_startup":
            self._steamvr_busy = busy
            if busy:
                self.steamvr_hint.setText("正在保存启动设置…" if self._steamvr_pending == "save" else "正在读取启动设置…")
            else:
                self._steamvr_pending = None
            self.render_steamvr_controls()
        elif operation.startswith("apply_"):
            section = operation.removeprefix("apply_")
            self.stack.widget(1 if section == "storage" else 2).setEnabled(not busy)
            self.apply_buttons[section].setEnabled(not busy and self._dirty[section])
            if busy:
                self.apply_hints[section].setText("正在保存…")
        elif operation == "clear_cache":
            self.clear_cache_button.setEnabled(not busy)
            self.clear_cache_button.setText("正在清理…" if busy else "清理临时缓存")
        elif operation in ("start", "stop"):
            if busy:
                self.service_button.setEnabled(False)
            else:
                self.render_runtime()
        elif operation == "cooldown":
            if busy:
                self.cooldown_button.setEnabled(False)
                self.cooldown_button.setText("正在处理…")
            else:
                self.render_cooldown()

    def closeEvent(self, event):
        if self._can_close:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        if any(self.controller.busy(operation) for operation in ("apply_storage", "apply_settings", "steamvr_startup")):
            self.notify("正在保存设置，请等待完成后再退出。")
            return
        if (any(self._dirty.values()) or self._steamvr_dirty) and QMessageBox.question(
            self, "尚有未应用的更改", "存储、设置或启动选项有未应用的更改。放弃这些更改并退出？\n外观设置已经自动保存。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._closing = True
        if self._vr_overlay is not None:
            self._vr_overlay.stop()
        self.notify("正在停止服务并退出…", persistent=True)
        self.setEnabled(False)
        self.controller.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(description="VRChat 跳舞房助手")
    parser.add_argument("--demo", action="store_true", help="使用隔离的演示数据，不启动服务或修改缓存")
    parser.add_argument("--no-autostart", action="store_true", help="启动界面但不自动启动缓存服务")
    parser.add_argument("--steamvr-startup", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", type=Path, help="指定外观数据目录；不会改变缓存配置路径")
    parser.add_argument("--smoke-test-report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.smoke_test_report is not None and not args.demo:
        parser.error("--smoke-test-report 必须与 --demo 一起使用。")
    # Establish a stable taskbar identity before QApplication creates any HWND.
    try:
        set_windows_application_identity()
    except OSError as exc:
        app = QApplication.instance() or QApplication(sys.argv[:1])
        QMessageBox.critical(None, "无法初始化 Windows 任务栏图标", str(exc))
        return 1
    if os.name == "nt" and not args.demo and not args.no_autostart:
        import dance_cache
        dance_cache.ensure_admin()
    instance = None
    if os.name == "nt" and not args.demo:
        from .single_instance import claim_instance
        try:
            instance = claim_instance()
        except OSError as exc:
            app = QApplication.instance() or QApplication(sys.argv[:1])
            QMessageBox.critical(None, "无法启动 VRChat 跳舞房助手", "无法检查程序是否已在运行。\n\n" + str(exc))
            return 1
        if instance is None:
            return 0
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setOrganizationName("Sharkspeare")
    app.setApplicationName("VRChatDanceAssistant")
    # 缩短浮窗弹出延迟（默认约 700ms，偏迟钝）
    try:
        from PySide6.QtWidgets import QStyleHints
        app.styleHints().setStyleHint(QStyleHints.StyleHint.SH_ToolTip_WakeUpDelay, 200)
    except Exception:
        pass
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    available = set(QFontDatabase.families())
    family = next((name for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "PingFang SC", "Segoe UI") if name in available), app.font().family())
    app.setFont(QFont(family, 10))
    temporary = tempfile.TemporaryDirectory(prefix="vrchat-dance-preview-") if args.demo and args.state_dir is None else None
    state_dir = args.state_dir or (Path(temporary.name) if temporary else Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)) / "appearance")
    controller = None
    try:
        app.setWindowIcon(app_icon())
        store = ThemeStore(state_dir)
        controller = CoreController(demo=args.demo)
        window = MainWindow(controller, store)
        window.show()
        for warning in store.warnings:
            window.notify(warning, error=True)
        QTimer.singleShot(0, controller.publish_initial)
        if args.smoke_test_report is not None:
            from .smoke import inspect_demo, save_report

            def smoke_check():
                code = save_report(args.smoke_test_report, inspect_demo(window))
                window._can_close = True
                window.close()
                app.exit(code)

            QTimer.singleShot(250, smoke_check)
        if not args.demo and not args.no_autostart and os.name == "nt":
            QTimer.singleShot(200, controller.start)
            QTimer.singleShot(3000, controller.check_updates)
        elif not args.demo and os.name != "nt":
            window.notify("缓存服务仅支持 Windows；当前环境可查看界面。使用 --demo 可以安全预览示例数据。", persistent=True)
        return app.exec()
    except (OSError, ValueError) as exc:
        if args.smoke_test_report is not None:
            from .smoke import save_report
            save_report(args.smoke_test_report, {
                "status": "failed", "frozen": bool(getattr(sys, "frozen", False)),
                "platform": sys.platform, "qt_platform": app.platformName(),
                "error": str(exc),
            })
            return 1
        QMessageBox.critical(None, "无法初始化 VRChat 跳舞房助手", "请检查配置文件及目录的读写权限。\n\n" + str(exc))
        return 1
    finally:
        if controller is not None:
            controller.dispose()
        if temporary:
            temporary.cleanup()
        if instance is not None:
            instance.close()
