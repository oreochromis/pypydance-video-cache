"""Desktop interface for the VRChat dance room video cache service."""

__version__ = "3.8.1"

# 预载 Qt6Svg.dll：Windows 加载 qsvg 图片插件时不会按 PySide6 目录解析其依赖，
# 不显式 import 会导致 SVG 图标解码静默失败
try:
    from PySide6 import QtSvg as _QtSvg  # noqa: F401
except ImportError:
    pass
