"""Session-scoped Windows instance guard, released even after a process crash."""
import ctypes

from .steamvr_startup import MUTEX_NAME

WAKE_EVENT_NAME = MUTEX_NAME + ".Wake"
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF


class Instance:
    def __init__(self, kernel, handle, wake_event=None):
        self.kernel, self.handle, self.wake_event = kernel, handle, wake_event

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.wake_event is not None:
            self.kernel.CloseHandle(self.wake_event)
            self.wake_event = None


def claim_instance():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateEventW.restype = ctypes.c_void_p
    kernel.SetEvent.argtypes = [ctypes.c_void_p]
    kernel.SetEvent.restype = ctypes.c_bool
    kernel.ResetEvent.argtypes = [ctypes.c_void_p]
    kernel.ResetEvent.restype = ctypes.c_bool
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_bool
    handle = kernel.CreateMutexW(None, False, MUTEX_NAME)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    if error == 183:  # ERROR_ALREADY_EXISTS
        kernel.CloseHandle(handle)
        # 已有实例：唤醒它的窗口（可能正最小化在托盘里）
        wake = kernel.CreateEventW(None, True, False, WAKE_EVENT_NAME)
        if wake:
            kernel.SetEvent(wake)
            kernel.CloseHandle(wake)
        return None
    wake = kernel.CreateEventW(None, True, False, WAKE_EVENT_NAME)
    return Instance(kernel, handle, wake)


def wait_for_wake(instance, callback):
    """阻塞等待重复启动的唤醒信号；在后台守护线程里调用。回调会被抛到调用方线程。"""
    kernel, event = instance.kernel, instance.wake_event
    if not event:
        return
    while True:
        if kernel.WaitForSingleObject(event, _INFINITE) != _WAIT_OBJECT_0:
            return
        kernel.ResetEvent(event)
        try:
            callback()
        except Exception:
            pass
