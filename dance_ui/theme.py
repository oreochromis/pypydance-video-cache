"""Theme persistence without a GUI dependency.

ThemeStore deliberately owns only appearance data.  It never touches the cache
service's configuration, hosts file, or any source image selected by the user.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import struct
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Mapping


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_EDGE = 16_384
HISTORY_LIMIT = 50

COLOR_FIELDS = frozenset({
    "accent", "background_color", "overlay_color", "nav_background", "nav_text",
    "nav_active_background", "nav_active_text", "text_primary", "text_secondary",
    "card_background", "border_color", "text_shadow_color",
})
NUMBER_RANGES = {
    "background_opacity": (0.0, 1.0),
    "image_opacity": (0.0, 1.0),
    "image_blur": (0.0, 30.0),
    "overlay_opacity": (0.0, 1.0),
    "nav_opacity": (0.0, 1.0),
    "nav_active_opacity": (0.0, 1.0),
    "card_opacity": (0.0, 1.0),
    "border_opacity": (0.0, 1.0),
    "border_width": (0.0, 6.0),
    "radius": (0.0, 24.0),
    "text_shadow_opacity": (0.0, 1.0),
    "text_shadow_size": (0.0, 6.0),
    "text_shadow_blur": (0.0, 20.0),
}
IMAGE_FITS = ("cover", "contain", "stretch", "center", "tile")
THEME_FIELDS = COLOR_FIELDS | frozenset(NUMBER_RANGES) | {
    "background_image", "image_fit",
}
DEFAULT_PREFERENCES = {"scale": 100, "density": "standard", "logs_expanded": False, "dismissed_update_version": ""}

_COMMON = {
    "background_image": "", "background_opacity": 1.0,
    "image_fit": "cover", "image_opacity": 0.35, "image_blur": 0.0,
    "overlay_opacity": 0.0, "border_width": 1.0, "radius": 12.0,
    "text_shadow_color": "#000000", "text_shadow_opacity": 0.0,
    "text_shadow_size": 1.0, "text_shadow_blur": 4.0,
}
BUILTIN_THEMES = {
    "light": {
        **_COMMON, "id": "light", "name": "浅色", "builtin": True, "base": "light",
        "accent": "#5865D8", "background_color": "#F3F5F9", "overlay_color": "#F3F5F9",
        "nav_background": "#FFFFFF", "nav_opacity": 0.82, "nav_text": "#657189",
        "nav_active_background": "#5865D8", "nav_active_opacity": 0.13,
        "nav_active_text": "#414FB8", "text_primary": "#172133",
        "text_secondary": "#667289", "card_background": "#FFFFFF",
        "card_opacity": 0.94, "border_color": "#DDE2EB", "border_opacity": 0.8,
    },
    "dark": {
        **_COMMON, "id": "dark", "name": "深色", "builtin": True, "base": "dark",
        "accent": "#95A3FF", "background_color": "#0F1420", "overlay_color": "#0F1420",
        "nav_background": "#141A28", "nav_opacity": 0.9, "nav_text": "#ADB7CB",
        "nav_active_background": "#95A3FF", "nav_active_opacity": 0.18,
        "nav_active_text": "#D6DCFF", "text_primary": "#EDF1F8",
        "text_secondary": "#A6B1C7", "card_background": "#1A2130",
        "card_opacity": 0.96, "border_color": "#344052", "border_opacity": 0.65,
    },
}
THEME_GROUPS = {
    "background": (
        "background_color", "background_opacity", "background_image", "image_fit",
        "image_opacity", "image_blur", "overlay_color", "overlay_opacity",
    ),
    "colors": ("accent", "text_primary", "text_secondary"),
    "navigation": (
        "nav_background", "nav_opacity", "nav_text", "nav_active_background",
        "nav_active_opacity", "nav_active_text",
    ),
    "cards": ("card_background", "card_opacity", "border_color", "border_opacity", "border_width", "radius"),
    "text": ("text_shadow_color", "text_shadow_opacity", "text_shadow_size", "text_shadow_blur"),
}


class ThemeError(ValueError):
    """A user-facing invalid appearance value or image."""


def normalize_color(value: Any) -> str:
    """Accept RGB/RRGGBB hex input and return an uppercase #RRGGBB value."""
    if not isinstance(value, str):
        raise ThemeError("颜色需要填写 HEX 色值，例如 #5865D8。")
    result = value.strip().lstrip("#")
    if len(result) == 3:
        result = "".join(c * 2 for c in result)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", result):
        raise ThemeError("颜色需要填写 #RGB 或 #RRGGBB 格式的 HEX 色值。")
    return "#" + result.upper()


def _number(value: Any, key: str) -> float:
    if isinstance(value, bool):
        raise ThemeError("数值设置不能使用开关值。")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ThemeError("请输入有效的数值。") from None
    if not math.isfinite(result):
        raise ThemeError("数值必须是有限数字。")
    low, high = NUMBER_RANGES[key]
    return round(min(high, max(low, result)), 3)


def _image_reference(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ThemeError("背景图片路径无效。")
    result = value.replace("\\", "/")
    if not re.fullmatch(r"backgrounds/[A-Za-z0-9_-]+\.(?:png|jpg|jpeg|webp)", result, re.IGNORECASE):
        raise ThemeError("请使用“导入图片”选择背景，图片会保存在应用的外观目录。")
    return result


def _theme_value(key: str, value: Any) -> Any:
    if key in COLOR_FIELDS:
        return normalize_color(value)
    if key in NUMBER_RANGES:
        return _number(value, key)
    if key == "image_fit":
        if value not in IMAGE_FITS:
            raise ThemeError("图片适应方式无效。")
        return value
    if key == "background_image":
        return _image_reference(value)
    raise ThemeError("未识别的主题设置：" + str(key))


def _preference_value(key: str, value: Any) -> Any:
    if key == "scale":
        if isinstance(value, bool):
            raise ThemeError("界面缩放请选择 100%、110%、125% 或 150%。")
        try:
            scale = int(value)
        except (TypeError, ValueError, OverflowError):
            raise ThemeError("界面缩放请选择 100%、110%、125% 或 150%。") from None
        if scale not in (100, 110, 125, 150) or str(value).strip() not in (str(scale), str(float(scale))):
            raise ThemeError("界面缩放请选择 100%、110%、125% 或 150%。")
        return scale
    if key == "density":
        if value not in ("comfortable", "standard", "compact"):
            raise ThemeError("信息密度请选择舒适、标准或紧凑。")
        return value
    if key == "logs_expanded":
        if not isinstance(value, bool):
            raise ThemeError("日志默认展开必须是开关值。")
        return value
    if key == "dismissed_update_version":
        if not isinstance(value, str):
            raise ThemeError("不再提醒的版本号必须是文本。")
        return value.strip()
    raise ThemeError("未识别的阅读偏好：" + str(key))


def _image_info(data: bytes) -> tuple[str, int, int]:
    """Inspect PNG/JPEG/WebP containers without importing Qt or an image library."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        position, dimensions, saw_data, finished = 8, None, False, False
        while position + 12 <= len(data):
            length = struct.unpack_from(">I", data, position)[0]
            kind = data[position + 4:position + 8]
            end = position + 12 + length
            if end > len(data):
                raise ThemeError("PNG 图片不完整，请重新选择图片。")
            payload = data[position + 8:position + 8 + length]
            expected = struct.unpack_from(">I", data, position + 8 + length)[0]
            if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected:
                raise ThemeError("PNG 图片校验失败，请重新选择图片。")
            if dimensions is None:
                if kind != b"IHDR" or length != 13:
                    raise ThemeError("PNG 图片头无效。")
                dimensions = struct.unpack_from(">II", payload)
            if kind == b"IDAT":
                saw_data = saw_data or bool(length)
            if kind == b"IEND":
                finished = length == 0
                break
            position = end
        if dimensions and saw_data and finished:
            return ("png", *dimensions)
        raise ThemeError("PNG 图片不完整，请重新选择图片。")
    if data.startswith(b"\xff\xd8"):
        position = 2
        frame_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        dimensions = None
        while position < len(data):
            if data[position] != 0xFF:
                break
            while position < len(data) and data[position] == 0xFF:
                position += 1
            if position >= len(data):
                break
            marker = data[position]
            position += 1
            if marker in (0xD9, 0xDA):
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(data):
                break
            length = struct.unpack_from(">H", data, position)[0]
            if length < 2 or position + length > len(data):
                break
            if marker in frame_markers and length >= 8:
                height, width = struct.unpack_from(">HH", data, position + 3)
                dimensions = (width, height)
            position += length
        if dimensions and b"\xff\xda" in data and data.rstrip().endswith(b"\xff\xd9"):
            return ("jpg", *dimensions)
        raise ThemeError("JPEG 图片格式无效或文件不完整。")
    if len(data) >= 20 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        declared = struct.unpack_from("<I", data, 4)[0] + 8
        if declared != len(data):
            raise ThemeError("WEBP 图片不完整，请重新选择图片。")
        position, dimensions = 12, None
        while position + 8 <= len(data):
            kind = data[position:position + 4]
            length = struct.unpack_from("<I", data, position + 4)[0]
            start, end = position + 8, position + 8 + length
            if end > len(data):
                raise ThemeError("WEBP 图片不完整。")
            payload = data[start:end]
            if kind == b"VP8X" and length >= 10:
                dimensions = (1 + int.from_bytes(payload[4:7], "little"), 1 + int.from_bytes(payload[7:10], "little"))
            elif kind == b"VP8L" and length >= 5 and payload[0] == 0x2F:
                bits = int.from_bytes(payload[1:5], "little")
                dimensions = (1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF))
            elif kind == b"VP8 " and length >= 10 and payload[3:6] == b"\x9d\x01\x2a":
                width, height = struct.unpack_from("<HH", payload, 6)
                dimensions = (width & 0x3FFF, height & 0x3FFF)
            position = end + (length % 2)
        if dimensions:
            return ("webp", *dimensions)
        raise ThemeError("无法识别此 WEBP 图片。")
    raise ThemeError("仅支持有效的 PNG、JPG 和 WEBP 图片。")


class ThemeStore:
    """Editable themes, independent preferences, atomic writes, and session undo."""

    def __init__(self, root_dir: str | os.PathLike[str]):
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.path = self.root_dir / "appearance.json"
        self.background_dir = self.root_dir / "backgrounds"
        self.warnings: list[str] = []
        self._history: list[dict[str, Any]] = []
        self._editing = False
        self._edit_recorded = False
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.background_dir.mkdir(parents=True, exist_ok=True)
        self._provided_themes = copy.deepcopy(BUILTIN_THEMES)
        self._state = self._defaults()
        if self.path.exists():
            try:
                if self.path.stat().st_size > 2 * 1024 * 1024:
                    raise ThemeError("外观配置文件过大。")
                with self.path.open("r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                self._state = self._normalise_document(raw)
            except (ValueError, UnicodeError) as error:
                backup = self.path.with_name("appearance.corrupt-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6] + ".json")
                # Preserve the user's original before replacing an unreadable file.
                shutil.copyfile(self.path, backup)
                self.warnings.append("外观配置无法读取，已保留原文件并恢复初始主题。" + str(error))
                self._write(self._state)
        else:
            self._write(self._state)

    def _defaults(self) -> dict[str, Any]:
        return {
            "version": 1, "active_theme": "light",
            "themes": copy.deepcopy(list(self._provided_themes.values())),
            "preferences": dict(DEFAULT_PREFERENCES),
        }

    def _normalise_document(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict) or not isinstance(raw.get("themes"), list):
            raise ThemeError("外观配置结构无效。")
        if raw.get("version", 1) != 1:
            raise ThemeError("外观配置版本暂不支持。")
        state = self._defaults()
        loaded, ids = [], set()
        for item in raw["themes"]:
            if not isinstance(item, dict):
                continue
            theme_id = item.get("id")
            if not isinstance(theme_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", theme_id) or theme_id in ids:
                continue
            base = theme_id if theme_id in BUILTIN_THEMES else item.get("base", "light")
            if not isinstance(base, str) or base not in BUILTIN_THEMES:
                base = "light"
            theme = copy.deepcopy(self._provided_themes.get(theme_id, BUILTIN_THEMES[base]))
            theme.update(id=theme_id, builtin=theme_id in self._provided_themes, base=base)
            name = item.get("name", theme["name"])
            theme["name"] = name.strip()[:60] if isinstance(name, str) and name.strip() else "未命名主题"
            for key in THEME_FIELDS:
                if key in item:
                    try:
                        theme[key] = _theme_value(key, item[key])
                    except (ThemeError, TypeError):
                        self.warnings.append("已恢复无效的主题设置：" + key)
            loaded.append(theme)
            ids.add(theme_id)
        for index, (theme_id, theme) in enumerate(self._provided_themes.items()):
            if theme_id not in ids:
                loaded.insert(index, copy.deepcopy(theme))
        state["themes"] = loaded
        available = {theme["id"] for theme in loaded}
        if isinstance(raw.get("active_theme"), str) and raw["active_theme"] in available:
            state["active_theme"] = raw["active_theme"]
        preferences = raw.get("preferences", {})
        if isinstance(preferences, dict):
            for key in DEFAULT_PREFERENCES:
                if key in preferences:
                    try:
                        state["preferences"][key] = _preference_value(key, preferences[key])
                    except (ThemeError, TypeError):
                        self.warnings.append("已恢复无效的阅读偏好：" + key)
        return state

    @property
    def active_id(self) -> str:
        return self._state["active_theme"]

    @property
    def active(self) -> dict[str, Any]:
        return copy.deepcopy(self._find(self._state, self.active_id))

    @property
    def themes(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._state["themes"])

    @property
    def preferences(self) -> dict[str, Any]:
        return dict(self._state["preferences"])

    @property
    def can_undo(self) -> bool:
        return bool(self._history)

    @staticmethod
    def _find(state: Mapping[str, Any], theme_id: str) -> dict[str, Any]:
        for theme in state["themes"]:
            if theme["id"] == theme_id:
                return theme
        raise ThemeError("没有找到这个主题。")

    def _write(self, state: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".appearance-", suffix=".tmp", dir=self.root_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def save(self) -> None:
        self._write(self._state)

    def _commit(self, candidate: dict[str, Any]) -> bool:
        if candidate == self._state:
            return False
        self._write(candidate)
        if not self._editing or not self._edit_recorded:
            self._history.append(copy.deepcopy(self._state))
            del self._history[:-HISTORY_LIMIT]
            if self._editing:
                self._edit_recorded = True
        self._state = candidate
        return True

    def begin_edit(self) -> None:
        """Group a slider drag into one undo step while still saving every change."""
        if not self._editing:
            self._editing = True
            self._edit_recorded = False

    def end_edit(self) -> None:
        self._editing = False
        self._edit_recorded = False

    def change(self, values: Mapping[str, Any]) -> bool:
        if not isinstance(values, Mapping):
            raise ThemeError("主题更改需要使用键值对。")
        normalised = {key: _theme_value(key, value) for key, value in values.items()}
        candidate = copy.deepcopy(self._state)
        self._find(candidate, self.active_id).update(normalised)
        return self._commit(candidate)

    def change_preferences(self, values: Mapping[str, Any]) -> bool:
        if not isinstance(values, Mapping):
            raise ThemeError("阅读偏好需要使用键值对。")
        normalised = {key: _preference_value(key, value) for key, value in values.items()}
        candidate = copy.deepcopy(self._state)
        candidate["preferences"].update(normalised)
        return self._commit(candidate)

    def select(self, theme_id: str) -> bool:
        self._find(self._state, theme_id)
        self.end_edit()
        candidate = copy.deepcopy(self._state)
        candidate["active_theme"] = theme_id
        return self._commit(candidate)

    @staticmethod
    def _name(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ThemeError("请填写主题名称。")
        result = value.strip()
        if len(result) > 60 or any(ord(char) < 32 for char in result):
            raise ThemeError("主题名称最多 60 个字符，不能包含换行。")
        return result

    def _unique_name(self, name: str) -> str:
        used = {theme["name"].casefold() for theme in self._state["themes"]}
        if name.casefold() not in used:
            return name
        for number in range(2, len(used) + 3):
            proposal = name[:54] + " " + str(number)
            if proposal.casefold() not in used:
                return proposal
        return name[:50] + " " + uuid.uuid4().hex[:6]

    def create(self, name: str | None = None, copy_current: bool = False) -> str:
        self.end_edit()
        current = self.active
        theme = current if copy_current else copy.deepcopy(BUILTIN_THEMES[current["base"]])
        new_id = "theme_" + uuid.uuid4().hex[:16]
        proposed = name if name is not None else (current["name"] + " 副本" if copy_current else "新主题")
        # Automatically generated names may need shortening after a long rename.
        proposed = proposed[:60] if name is None else proposed
        theme.update(id=new_id, name=self._unique_name(self._name(proposed)), builtin=False)
        candidate = copy.deepcopy(self._state)
        candidate["themes"].append(theme)
        candidate["active_theme"] = new_id
        self._commit(candidate)
        return new_id

    def rename(self, theme_id: str, name: str) -> bool:
        new_name = self._name(name)
        self._find(self._state, theme_id)
        if any(theme["id"] != theme_id and theme["name"].casefold() == new_name.casefold() for theme in self._state["themes"]):
            raise ThemeError("已有同名主题，请使用另一个名称。")
        candidate = copy.deepcopy(self._state)
        self._find(candidate, theme_id)["name"] = new_name
        return self._commit(candidate)

    def delete(self, theme_id: str) -> bool:
        target = self._find(self._state, theme_id)
        if target["builtin"]:
            raise ThemeError("随程序提供的主题需要保留；可以编辑、复制或重置其设置。")
        self.end_edit()
        candidate = copy.deepcopy(self._state)
        candidate["themes"] = [theme for theme in candidate["themes"] if theme["id"] != theme_id]
        if candidate["active_theme"] == theme_id:
            candidate["active_theme"] = target["base"]
        # Copies can share an image, and undo can restore deleted themes.  Keep
        # managed images instead of deleting a file another theme still needs.
        return self._commit(candidate)

    def undo(self) -> bool:
        self.end_edit()
        if not self._history:
            return False
        candidate = copy.deepcopy(self._history[-1])
        self._write(candidate)
        self._state = candidate
        self._history.pop()
        return True

    def reset_current(self) -> bool:
        self.end_edit()
        active = self.active
        defaults = self._provided_themes.get(active["id"], BUILTIN_THEMES[active["base"]])
        return self.change({key: defaults[key] for key in THEME_FIELDS})

    def reset_group(self, group: str) -> bool:
        if group not in THEME_GROUPS:
            raise ThemeError("未识别的设置分组。")
        self.end_edit()
        active = self.active
        defaults = self._provided_themes.get(active["id"], BUILTIN_THEMES[active["base"]])
        return self.change({key: defaults[key] for key in THEME_GROUPS[group]})

    def reset_preferences(self) -> bool:
        self.end_edit()
        return self.change_preferences(DEFAULT_PREFERENCES)

    def background_path(self, theme: Mapping[str, Any] | None = None) -> Path | None:
        value = (theme if theme is not None else self.active).get("background_image")
        if not value:
            return None
        try:
            value = _image_reference(value)
            path = (self.root_dir / value).resolve()
            if path.parent != self.background_dir.resolve():
                return None
            if path.is_file():
                return path
            # Earlier preview versions could reference a packaged image that
            # was never copied into user data. Keep the theme/reference intact;
            # a missing image renders its base color until the user imports one.
            return None
        except (OSError, ThemeError, RuntimeError):
            return None

    def import_background(self, path: str | os.PathLike[str]) -> str:
        source = Path(path).expanduser()
        if not source.is_file():
            raise ThemeError("没有找到这张图片，请重新选择。")
        if source.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            raise ThemeError("仅支持 PNG、JPG 和 WEBP 图片。")
        if source.stat().st_size > MAX_IMAGE_BYTES:
            raise ThemeError("图片最大支持 20 MB，请先缩小图片。")
        with source.open("rb") as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ThemeError("图片最大支持 20 MB，请先缩小图片。")
        extension, width, height = _image_info(data)
        if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS or max(width, height) > MAX_IMAGE_EDGE:
            raise ThemeError("图片尺寸过大：最多 4000 万像素，单边不超过 16384 像素。")
        filename = hashlib.sha256(data).hexdigest() + "." + extension
        target = self.background_dir / filename
        if not target.is_file() or target.stat().st_size != len(data) or hashlib.sha256(target.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
            descriptor, temporary = tempfile.mkstemp(prefix=".background-", suffix=".tmp", dir=self.background_dir)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        relative = "backgrounds/" + filename
        self.change({"background_image": relative})
        return relative
