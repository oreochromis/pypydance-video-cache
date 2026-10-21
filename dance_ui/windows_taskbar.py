"""Windows application identity and read-only native window icon checks."""
from __future__ import annotations

import ctypes
import sys


APPLICATION_ID = "Sharkspeare.VRChatDanceAssistant"


def _windows_library(name):
    return ctypes.WinDLL(name, use_last_error=True)


def _bind(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


def _check_hresult(result, operation):
    if result < 0:
        raise OSError(f"{operation} failed (HRESULT 0x{result & 0xFFFFFFFF:08X}).")


def set_windows_application_identity():
    """Identify the app before QApplication or any other UI is created."""
    if sys.platform != "win32":
        return
    setter = _bind(
        _windows_library("shell32"), "SetCurrentProcessExplicitAppUserModelID",
        [ctypes.c_wchar_p], ctypes.c_int32,
    )
    _check_hresult(setter(APPLICATION_ID), "SetCurrentProcessExplicitAppUserModelID")


def _process_application_id():
    getter = _bind(
        _windows_library("shell32"), "GetCurrentProcessExplicitAppUserModelID",
        [ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int32,
    )
    release = _bind(
        _windows_library("ole32"), "CoTaskMemFree", [ctypes.c_void_p], None,
    )
    value = ctypes.c_void_p()
    try:
        _check_hresult(getter(ctypes.byref(value)), "GetCurrentProcessExplicitAppUserModelID")
        if not value.value:
            raise ValueError("Windows returned an empty application identity.")
        return ctypes.wstring_at(value.value)
    finally:
        if value.value:
            release(value)


class _IconInfo(ctypes.Structure):
    _fields_ = [
        ("is_icon", ctypes.c_int32), ("hotspot_x", ctypes.c_uint32),
        ("hotspot_y", ctypes.c_uint32), ("mask", ctypes.c_void_p),
        ("color", ctypes.c_void_p),
    ]


class _Bitmap(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int32), ("width", ctypes.c_int32),
        ("height", ctypes.c_int32), ("stride", ctypes.c_int32),
        ("planes", ctypes.c_uint16), ("bits_per_pixel", ctypes.c_uint16),
        ("bits", ctypes.c_void_p),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32), ("width", ctypes.c_int32),
        ("height", ctypes.c_int32), ("planes", ctypes.c_uint16),
        ("bits_per_pixel", ctypes.c_uint16), ("compression", ctypes.c_uint32),
        ("image_size", ctypes.c_uint32), ("x_resolution", ctypes.c_int32),
        ("y_resolution", ctypes.c_int32), ("colors_used", ctypes.c_uint32),
        ("important_colors", ctypes.c_uint32),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("header", _BitmapInfoHeader), ("colors", ctypes.c_uint32 * 1)]


class _NativeIcons:
    """Typed calls keep HWND/HICON/HDC values intact in a 64-bit executable."""

    def __init__(self):
        user = _windows_library("user32")
        gdi = _windows_library("gdi32")
        pointer = ctypes.c_void_p
        integer = ctypes.c_int32
        unsigned = ctypes.c_uint32
        self.send_message = _bind(
            user, "SendMessageW", [pointer, unsigned, ctypes.c_size_t, ctypes.c_ssize_t],
            ctypes.c_ssize_t,
        )
        class_function = "GetClassLongPtrW" if ctypes.sizeof(pointer) == 8 else "GetClassLongW"
        self.class_value = _bind(user, class_function, [pointer, integer], ctypes.c_size_t)
        self.icon_info = _bind(user, "GetIconInfo", [pointer, ctypes.POINTER(_IconInfo)], integer)
        self.bitmap_info = _bind(gdi, "GetObjectW", [pointer, integer, pointer], integer)
        self.create_dc = _bind(gdi, "CreateCompatibleDC", [pointer], pointer)
        self.create_dib = _bind(
            gdi, "CreateDIBSection",
            [pointer, ctypes.POINTER(_BitmapInfo), unsigned, ctypes.POINTER(pointer), pointer, unsigned],
            pointer,
        )
        self.select = _bind(gdi, "SelectObject", [pointer, pointer], pointer)
        self.draw = _bind(
            user, "DrawIconEx",
            [pointer, integer, integer, pointer, integer, integer, unsigned, pointer, unsigned],
            integer,
        )
        self.flush = _bind(gdi, "GdiFlush", [], integer)
        self.delete_object = _bind(gdi, "DeleteObject", [pointer], integer)
        self.delete_dc = _bind(gdi, "DeleteDC", [pointer], integer)

    def window_icon(self, hwnd, large):
        # WM_GETICON returns a borrowed HICON; ownership stays with Qt/Windows.
        icon = self.send_message(hwnd, 0x007F, 1 if large else 0, 0)
        if not icon:
            icon = self.class_value(hwnd, -14 if large else -34)
        if not icon:
            raise ValueError("The native Windows window icon is missing.")
        return icon

    def size(self, icon):
        info = _IconInfo()
        if not self.icon_info(icon, ctypes.byref(info)):
            raise OSError("GetIconInfo failed for the native window icon.")
        try:
            bitmap = _Bitmap()
            if not self.bitmap_info(info.color or info.mask, ctypes.sizeof(bitmap), ctypes.byref(bitmap)):
                raise OSError("GetObjectW failed for the native window icon.")
            width, height = bitmap.width, bitmap.height if info.color else bitmap.height // 2
            if not (0 < width <= 512 and 0 < height <= 512):
                raise ValueError(f"Unexpected native window icon size: {width}x{height}.")
            return width, height
        finally:
            # GetIconInfo creates these bitmap copies, unlike the borrowed HICON.
            if info.color:
                self.delete_object(info.color)
            if info.mask:
                self.delete_object(info.mask)

    def render_rgb(self, icon, width, height, background):
        dc = self.create_dc(None)
        if not dc:
            raise OSError("CreateCompatibleDC failed while checking the window icon.")
        bitmap = old = None
        try:
            info = _BitmapInfo()
            info.header.size = ctypes.sizeof(_BitmapInfoHeader)
            info.header.width, info.header.height = width, -height
            info.header.planes, info.header.bits_per_pixel = 1, 32
            info.header.image_size = width * height * 4
            pixels = ctypes.c_void_p()
            bitmap = self.create_dib(dc, ctypes.byref(info), 0, ctypes.byref(pixels), None, 0)
            if not bitmap or not pixels.value:
                raise OSError("CreateDIBSection failed while checking the window icon.")
            old = self.select(dc, bitmap)
            if not old or old == ctypes.c_void_p(-1).value:
                old = None
                raise OSError("SelectObject failed while checking the window icon.")
            fill = bytes((background, background, background, 255)) * (width * height)
            ctypes.memmove(pixels, fill, len(fill))
            if not self.draw(dc, 0, 0, icon, width, height, 0, None, 3):
                raise OSError("DrawIconEx failed for the native window icon.")
            if not self.flush():
                raise OSError("GdiFlush failed while checking the window icon.")
            raw = ctypes.string_at(pixels, len(fill))
            return bytes(channel for offset in range(0, len(raw), 4)
                         for channel in (raw[offset + 2], raw[offset + 1], raw[offset]))
        finally:
            if old:
                self.select(dc, old)
            if bitmap:
                self.delete_object(bitmap)
            self.delete_dc(dc)


def _approved_rgb(icon, width, height, background):
    from PySide6.QtGui import QColor, QImage, QPainter

    image = icon.pixmap(width, height).toImage()
    if image.isNull() or image.width() != width or image.height() != height:
        raise ValueError("The approved icon could not be rendered at the native icon size.")
    image.setDevicePixelRatio(1.0)
    canvas = QImage(width, height, QImage.Format.Format_RGB32)
    canvas.fill(QColor(background, background, background))
    painter = QPainter(canvas)
    try:
        painter.drawImage(0, 0, image)
    finally:
        painter.end()
    canvas = canvas.convertToFormat(QImage.Format.Format_RGB888)
    raw = bytes(canvas.constBits())
    return b"".join(raw[row * canvas.bytesPerLine():row * canvas.bytesPerLine() + width * 3]
                    for row in range(height))


def _verify_pixels(actual, approved, tolerance=2):
    if not actual or len(actual) != len(approved):
        raise ValueError("The native window icon has unexpected pixel data.")
    maximum = max(abs(left - right) for left, right in zip(actual, approved))
    if maximum > tolerance:
        raise ValueError(f"The native window icon differs from the approved PY artwork (delta {maximum}).")
    return maximum


def describe_windows_taskbar(window):
    """Inspect native identity/icons; this does not inspect Explorer's pinned items."""
    if sys.platform != "win32":
        raise OSError("Native Windows taskbar checks require Windows.")
    application_id = _process_application_id()
    if application_id != APPLICATION_ID:
        raise ValueError(f"Unexpected Windows application identity: {application_id!r}.")
    native = _NativeIcons()
    hwnd = int(window.winId())
    icons = []
    for kind, large in (("BIG", True), ("SMALL", False)):
        handle = native.window_icon(hwnd, large)
        width, height = native.size(handle)
        maximum = 0
        for background in (0, 255):
            maximum = max(maximum, _verify_pixels(
                native.render_rgb(handle, width, height, background),
                _approved_rgb(window.windowIcon(), width, height, background),
            ))
        icons.append({
            "kind": kind, "size": [width, height], "backgrounds": ["black", "white"],
            "maximum_channel_delta": maximum, "pixel_verified": True,
        })
    return {"application_id": application_id, "native_icons": icons}
