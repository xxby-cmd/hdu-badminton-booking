"""Windows notification-area icon. Native callbacks only enqueue Tk actions."""
import ctypes
from ctypes import wintypes
import os
import threading

WINDOW_CLASS = 'HDUVenueAssistantTrayWindow'
DASHBOARD = 'HDUVenueAssistantDashboard'
RESTORE_MESSAGE = 0x8002


class DashboardInstance:
    def __init__(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateMutexW(None, False, 'Local\\HDUVenueAssistantDashboard')
        self.acquired = bool(self.handle) and ctypes.get_last_error() != 183

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def restore_existing():
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    for _ in range(10):
        hwnd = user.FindWindowW(WINDOW_CLASS, DASHBOARD)
        if hwnd:
            return bool(user.PostMessageW(hwnd, RESTORE_MESSAGE, 0, 0))
        threading.Event().wait(.2)
    return False


class TrayIcon:
    def __init__(self, events, name=None):
        self.events = events
        self.name = name or 'HDUVenueAssistant-' + str(os.getpid())
        self.ready = threading.Event()
        self.hwnd = None
        self.available = False
        self.thread = threading.Thread(target=self.run, name='venue-tray', daemon=True)
        self.thread.start()
        self.ready.wait(2)

    def stop(self):
        if self.hwnd:
            self.user.PostMessageW(self.hwnd, 0x0010, 0, 0)
        if self.thread is not threading.current_thread():
            self.thread.join(2)

    def run(self):
        user = ctypes.WinDLL('user32', use_last_error=True)
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user = user
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                           ctypes.c_size_t, ctypes.c_ssize_t)

        class WindowClass(ctypes.Structure):
            _fields_ = [('style', wintypes.UINT), ('procedure', callback_type),
                        ('class_extra', ctypes.c_int), ('window_extra', ctypes.c_int),
                        ('instance', wintypes.HINSTANCE), ('icon', wintypes.HANDLE),
                        ('cursor', wintypes.HANDLE), ('background', wintypes.HANDLE),
                        ('menu', wintypes.LPCWSTR), ('class_name', wintypes.LPCWSTR)]

        class IconData(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('window', wintypes.HWND),
                        ('id', wintypes.UINT), ('flags', wintypes.UINT),
                        ('callback', wintypes.UINT), ('icon', wintypes.HANDLE),
                        ('tip', wintypes.WCHAR * 128), ('state', wintypes.DWORD),
                        ('state_mask', wintypes.DWORD), ('info', wintypes.WCHAR * 256),
                        ('version', wintypes.UINT), ('info_title', wintypes.WCHAR * 64),
                        ('info_flags', wintypes.DWORD), ('guid', ctypes.c_ubyte * 16),
                        ('balloon_icon', wintypes.HANDLE)]

        user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        user.RegisterClassW.restype = wintypes.WORD
        user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        user.CreateWindowExW.restype = wintypes.HWND
        user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        user.DefWindowProcW.restype = ctypes.c_ssize_t
        user.PostMessageW.argtypes = user.DefWindowProcW.argtypes
        user.DestroyWindow.argtypes = [wintypes.HWND]
        user.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user.LoadIconW.restype = wintypes.HANDLE
        user.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user.RegisterWindowMessageW.restype = wintypes.UINT
        user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user.DispatchMessageW.restype = ctypes.c_ssize_t
        user.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HINSTANCE
        shell.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(IconData)]
        shell.Shell_NotifyIconW.restype = wintypes.BOOL
        taskbar_created = user.RegisterWindowMessageW('TaskbarCreated')
        instance = kernel.GetModuleHandleW(None)
        data = IconData()

        def notify_add():
            self.available = bool(shell.Shell_NotifyIconW(0, ctypes.byref(data)))

        @callback_type
        def procedure(hwnd, message, wparam, lparam):
            if message == 0x8001:
                if lparam in (0x0202, 0x0203):
                    self.events.put(('tray_restore',))
                elif lparam == 0x0205:
                    self.menu(hwnd)
                return 0
            if message == RESTORE_MESSAGE:
                self.events.put(('tray_restore',))
                return 0
            if taskbar_created and message == taskbar_created:
                notify_add()
                if not self.available:
                    self.events.put(('tray_restore',))
                return 0
            if message == 0x0010:
                shell.Shell_NotifyIconW(2, ctypes.byref(data))
                self.available = False
                user.DestroyWindow(hwnd)
                return 0
            if message == 0x0002:
                user.PostQuitMessage(0)
                return 0
            return user.DefWindowProcW(hwnd, message, wparam, lparam)

        window_class = WindowClass()
        window_class.procedure = procedure
        window_class.instance = instance
        window_class.class_name = WINDOW_CLASS
        registered = user.RegisterClassW(ctypes.byref(window_class))
        try:
            if not registered:
                return
            self.hwnd = user.CreateWindowExW(0, WINDOW_CLASS, self.name, 0, 0, 0, 0, 0,
                                             None, None, instance, None)
            if not self.hwnd:
                return
            data.size = ctypes.sizeof(data)
            data.window = self.hwnd
            data.id = 1
            data.flags = 1 | 2 | 4
            data.callback = 0x8001
            data.icon = user.LoadIconW(None, ctypes.c_void_p(32512))
            data.tip = '场馆预约助手 · 点击打开，右键退出'
            notify_add()
            self.ready.set()
            message = wintypes.MSG()
            while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
        finally:
            self.ready.set()
            self.hwnd = None
            self.available = False
            if registered:
                user.UnregisterClassW(WINDOW_CLASS, instance)

    def menu(self, hwnd):
        user = self.user
        user.CreatePopupMenu.restype = wintypes.HMENU
        user.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
        user.TrackPopupMenu.restype = wintypes.UINT
        user.DestroyMenu.argtypes = [wintypes.HMENU]
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        menu = user.CreatePopupMenu()
        if not menu:
            return
        try:
            user.AppendMenuW(menu, 0, 1, '打开预约助手')
            user.AppendMenuW(menu, 0, 2, '退出界面')
            point = wintypes.POINT()
            user.GetCursorPos(ctypes.byref(point))
            user.SetForegroundWindow(hwnd)
            selected = user.TrackPopupMenu(menu, 0x182, point.x, point.y, 0, hwnd, None)
            if selected == 1:
                self.events.put(('tray_restore',))
            elif selected == 2:
                self.events.put(('tray_exit',))
            user.PostMessageW(hwnd, 0, 0, 0)
        finally:
            user.DestroyMenu(menu)
