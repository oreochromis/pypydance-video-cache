"""Checks for startup identity and native-icon verification without a Windows host."""
import ctypes
import os
import unittest
from unittest.mock import Mock, patch

from dance_ui import windows_taskbar as taskbar

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class WindowsApplicationIdentityTests(unittest.TestCase):
    def test_other_platforms_do_not_load_windows_libraries(self):
        with patch.object(taskbar.sys, "platform", "darwin"), patch.object(taskbar, "_windows_library") as loader:
            self.assertIsNone(taskbar.set_windows_application_identity())
        loader.assert_not_called()

    def test_sets_stable_identity_with_typed_hresult_and_unicode_argument(self):
        shell = Mock()
        shell.SetCurrentProcessExplicitAppUserModelID.return_value = 0
        with patch.object(taskbar.sys, "platform", "win32"), patch.object(taskbar, "_windows_library", return_value=shell):
            taskbar.set_windows_application_identity()
        setter = shell.SetCurrentProcessExplicitAppUserModelID
        setter.assert_called_once_with("Sharkspeare.VRChatDanceAssistant")
        self.assertEqual(setter.argtypes, [ctypes.c_wchar_p])
        self.assertIs(setter.restype, ctypes.c_int32)

    def test_failed_identity_is_reported(self):
        shell = Mock()
        shell.SetCurrentProcessExplicitAppUserModelID.return_value = -2147467259
        with patch.object(taskbar.sys, "platform", "win32"), patch.object(taskbar, "_windows_library", return_value=shell):
            with self.assertRaisesRegex(OSError, "HRESULT 0x80004005"):
                taskbar.set_windows_application_identity()

    def test_native_check_rejects_other_platforms(self):
        with patch.object(taskbar.sys, "platform", "darwin"):
            with self.assertRaisesRegex(OSError, "require Windows"):
                taskbar.describe_windows_taskbar(Mock())

    def test_readback_releases_windows_allocated_identity(self):
        shell, ole = Mock(), Mock()
        value = ctypes.create_unicode_buffer(taskbar.APPLICATION_ID)

        def read_identity(pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(value, ctypes.c_void_p)
            return 0

        shell.GetCurrentProcessExplicitAppUserModelID.side_effect = read_identity
        with patch.object(taskbar, "_windows_library", side_effect=[shell, ole]):
            self.assertEqual(taskbar._process_application_id(), taskbar.APPLICATION_ID)
        self.assertEqual(ole.CoTaskMemFree.call_count, 1)
        self.assertEqual(ole.CoTaskMemFree.call_args.args[0].value, ctypes.addressof(value))


class NativeIconPixelTests(unittest.TestCase):
    def test_small_compositing_rounding_is_accepted(self):
        self.assertEqual(taskbar._verify_pixels(bytes((0, 80, 255)), bytes((2, 79, 254))), 2)

    def test_colored_python_or_another_artwork_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "differs from the approved PY artwork"):
            taskbar._verify_pixels(bytes((45, 98, 147)), bytes((0, 0, 0)))

    def test_missing_or_differently_sized_pixels_are_rejected(self):
        for actual, approved in ((b"", b""), (b"\x00", b"\x00\x00")):
            with self.subTest(actual=actual, approved=approved):
                with self.assertRaisesRegex(ValueError, "unexpected pixel data"):
                    taskbar._verify_pixels(actual, approved)

    def test_smoke_verifies_both_native_icon_sizes_on_both_backgrounds(self):
        native = Mock()
        native.window_icon.side_effect = [100, 101]
        native.size.side_effect = [(32, 32), (16, 16)]
        native.render_rgb.return_value = bytes((10, 20, 30))
        window = Mock()
        window.winId.return_value = 123
        with patch.object(taskbar.sys, "platform", "win32"), \
                patch.object(taskbar, "_process_application_id", return_value=taskbar.APPLICATION_ID), \
                patch.object(taskbar, "_NativeIcons", return_value=native), \
                patch.object(taskbar, "_approved_rgb", return_value=bytes((10, 20, 31))):
            report = taskbar.describe_windows_taskbar(window)
        self.assertEqual(report["application_id"], taskbar.APPLICATION_ID)
        self.assertEqual([item["size"] for item in report["native_icons"]], [[32, 32], [16, 16]])
        self.assertTrue(all(item["pixel_verified"] for item in report["native_icons"]))
        self.assertEqual(native.render_rgb.call_count, 4)
        self.assertEqual([call.args[-1] for call in native.render_rgb.call_args_list], [0, 255, 0, 255])

    def test_smoke_does_not_claim_success_for_a_different_application_id(self):
        with patch.object(taskbar.sys, "platform", "win32"), \
                patch.object(taskbar, "_process_application_id", return_value="Python"), \
                patch.object(taskbar, "_NativeIcons") as native:
            with self.assertRaisesRegex(ValueError, "Unexpected Windows application identity"):
                taskbar.describe_windows_taskbar(Mock())
        native.assert_not_called()

    def test_bitmap_copies_are_released_when_size_inspection_fails(self):
        native = taskbar._NativeIcons.__new__(taskbar._NativeIcons)

        def read_info(_icon, pointer):
            info = ctypes.cast(pointer, ctypes.POINTER(taskbar._IconInfo)).contents
            info.color, info.mask = 12, 13
            return 1

        native.icon_info = Mock(side_effect=read_info)
        native.bitmap_info = Mock(return_value=0)
        native.delete_object = Mock()
        with self.assertRaisesRegex(OSError, "GetObjectW failed"):
            native.size(11)
        self.assertEqual([call.args[0] for call in native.delete_object.call_args_list], [12, 13])

    def test_failed_native_render_restores_the_dc_and_releases_owned_resources(self):
        native = taskbar._NativeIcons.__new__(taskbar._NativeIcons)
        storage = ctypes.create_string_buffer(16)

        def create_dib(_dc, _info, _usage, pixels, _section, _offset):
            ctypes.cast(pixels, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(storage)
            return 12

        native.create_dc = Mock(return_value=10)
        native.create_dib = Mock(side_effect=create_dib)
        native.select = Mock(return_value=13)
        native.draw = Mock(return_value=0)
        native.delete_object = Mock()
        native.delete_dc = Mock()
        with self.assertRaisesRegex(OSError, "DrawIconEx failed"):
            native.render_rgb(11, 2, 2, 255)
        self.assertEqual([call.args for call in native.select.call_args_list], [(10, 12), (10, 13)])
        native.delete_object.assert_called_once_with(12)
        native.delete_dc.assert_called_once_with(10)


class ApprovedIconCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_transparent_artwork_is_composited_against_both_backgrounds(self):
        from PySide6.QtGui import QColor, QIcon, QImage, QPixmap
        image = QImage(2, 2, QImage.Format.Format_ARGB32)
        image.fill(QColor(255, 0, 0, 128))
        icon = QIcon(QPixmap.fromImage(image))
        black = taskbar._approved_rgb(icon, 2, 2, 0)
        white = taskbar._approved_rgb(icon, 2, 2, 255)
        self.assertEqual(black, bytes((128, 0, 0)) * 4)
        self.assertEqual(white, bytes((255, 127, 127)) * 4)


if __name__ == "__main__":
    unittest.main()
