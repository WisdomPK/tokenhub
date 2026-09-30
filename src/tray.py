"""TokenHub v3 系统托盘（纯 ctypes，无第三方依赖）。

从 V2 移植并修复：
- 回调改为线程安全：托盘线程只把命令放入队列，主线程经 root.after 轮询派发，
  不在托盘线程里直接调用 tk 回调；
- 补充 WNDCLASSW 结构定义、hInstance 传参、菜单分隔/命令完整化；
- 新增「立即刷新」菜单项与气球通知 notify()；
- TH-02 修复：lpfnWndProc 用 WNDPROC_ 包装并持有（self._wndproc_cb），
  补齐实际使用 Win32 API 的 restype/argtypes（64 位句柄不截断）；
  初始化失败经命令队列上报主线程（on_error 回调）+ 日志，不再静默退出。

用法（由 main.py 集成）：
    tray = TrayIcon(on_show=..., on_hide=..., on_settings=..., on_help=...,
                    on_refresh=..., on_exit=..., on_error=...)
    tray.start()
    tray.set_visible(win.is_visible())   # 左键切换显示/隐藏的依据
    tray.stop()                          # 退出时调用

    root 可不传：自动回退 tk._default_root（main 先创建隐藏 root 再建托盘）。
    回调全部经主线程轮询派发（root.after），不会在托盘线程里触碰 tk。
"""

import ctypes
import logging
import queue
import threading
import tkinter as tk
from ctypes import wintypes
from typing import Callable, Optional

_log = logging.getLogger("TokenHub")
if not _log.handlers:
    # 独立运行/测试兜底：无 TokenHub logger 配置时输出到 stderr（不建文件）
    _log.addHandler(logging.StreamHandler())

# ---- Windows API 常量 ----
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010

WM_USER = 0x0400
WM_TRAY_CALLBACK = WM_USER + 1
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

IDI_APPLICATION = 32512
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_RIGHTALIGN = 0x0008
TPM_BOTTOMALIGN = 0x0020

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

# ---- 64 位安全的函数签名（句柄/指针一律显式声明，防截断）----
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

user32.LoadIconW.restype = wintypes.HICON
# 第二参数是资源 ID（MAKEINTRESOURCE，如 IDI_APPLICATION=32512）或字符串名：
# 必须用 c_void_p 承载指针值；声明为 LPCWSTR 时传整数资源 ID 会抛
# "argument 2: TypeError: 'int' object cannot be interpreted as ctypes.c_wchar_p"
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]

user32.RegisterClassW.restype = wintypes.ATOM
# argtypes 在 WNDCLASSW 定义之后补充（见下）

user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]

user32.DefWindowProcW.restype = wintypes.LPARAM
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

user32.GetMessageW.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]

user32.TranslateMessage.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]

user32.DispatchMessageW.restype = wintypes.LPARAM
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

user32.DestroyWindow.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = [wintypes.HWND]

user32.PostQuitMessage.restype = None
user32.PostQuitMessage.argtypes = [ctypes.c_int]

user32.PostMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

user32.CreatePopupMenu.restype = wintypes.HMENU
user32.CreatePopupMenu.argtypes = []

user32.AppendMenuW.restype = wintypes.BOOL
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR]

user32.GetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]

user32.TrackPopupMenu.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.LPVOID,
]

user32.DestroyMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]

shell32.Shell_NotifyIconW.restype = wintypes.BOOL
# argtypes 在 NOTIFYICONDATAW 定义之后补充（见下）


WNDPROC_ = ctypes.WINFUNCTYPE(
    wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ('style', wintypes.UINT),
        ('lpfnWndProc', WNDPROC_),
        ('cbClsExtra', ctypes.c_int),
        ('cbWndExtra', ctypes.c_int),
        ('hInstance', wintypes.HINSTANCE),
        ('hIcon', wintypes.HICON),
        ('hCursor', wintypes.HANDLE),
        ('hbrBackground', wintypes.HBRUSH),
        ('lpszMenuName', wintypes.LPCWSTR),
        ('lpszClassName', wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('hWnd', wintypes.HWND),
        ('uID', wintypes.UINT),
        ('uFlags', wintypes.UINT),
        ('uCallbackMessage', wintypes.UINT),
        ('hIcon', wintypes.HICON),
        ('szTip', wintypes.WCHAR * 128),
        ('dwState', wintypes.DWORD),
        ('dwStateMask', wintypes.DWORD),
        ('szInfo', wintypes.WCHAR * 256),
        ('uTimeout', wintypes.UINT),
        ('szInfoTitle', wintypes.WCHAR * 64),
        ('dwInfoFlags', wintypes.DWORD),
    ]


# 结构体定义就绪后补齐具体类型的 argtypes（64 位安全）
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]


class TrayIcon:
    """系统托盘图标（ctypes 消息循环 + 主线程轮询派发）。"""

    MENU_SHOW = 1001
    MENU_HIDE = 1002
    MENU_SETTINGS = 1003
    MENU_HELP = 1004
    MENU_REFRESH = 1005
    MENU_EXIT = 1006

    POLL_MS = 60  # 主线程命令轮询间隔

    def __init__(self,
                 on_show: Callable, on_hide: Callable,
                 on_settings: Callable, on_help: Callable,
                 on_refresh: Callable, on_exit: Callable,
                 root=None, on_error: Optional[Callable] = None,
                 on_toggle: Optional[Callable] = None):
        if root is None:
            root = getattr(tk, '_default_root', None)
        if root is None:
            raise ValueError('TrayIcon 需要 root（或先创建 tk.Tk()）')
        self.root = root
        self.on_show = on_show
        self.on_hide = on_hide
        self.on_settings = on_settings
        self.on_help = on_help
        self.on_refresh = on_refresh
        self.on_exit = on_exit
        self.on_error = on_error  # 可选：初始化失败时主线程回调（未提供则仅日志）
        self.on_toggle = on_toggle  # R2-05：可选：左键切换回调（状态来源单一在主程序）

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._hwnd: Optional[int] = None
        self._icon_data: Optional[NOTIFYICONDATAW] = None
        self._wndproc_cb = None  # TH-02: 持有 WNDPROC_ 包装，防 GC/类型错误
        self._command_queue: 'queue.Queue[object]' = queue.Queue()
        self._visible = True

    # ---- 生命周期 ----

    def start(self) -> None:
        """启动托盘线程 + 主线程命令轮询（必须在主线程调用）。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_tray_loop, daemon=True)
        self._thread.start()
        self._poll_job = self.root.after(self.POLL_MS, self._poll)

    def stop(self) -> None:
        """停止托盘并清理。"""
        self._running = False
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        try:
            if getattr(self, '_poll_job', None):
                self.root.after_cancel(self._poll_job)
        except Exception:
            # §7.2：root 已销毁时 after_cancel 抛 TclError 属预期；仍留诊断
            _log.exception("取消托盘轮询失败（root 可能已销毁）")

    def set_visible(self, visible: bool) -> None:
        """仅同步内部可见态字段（R2-05：状态来源单一在主程序）。

        左键切换优先走 on_toggle 回调由主程序决策；本字段仅作为
        未提供 on_toggle 时的旧行为回退依据。
        """
        self._visible = visible

    def notify(self, title: str, text: str) -> None:
        """气泡通知（主线程调用；失败仅记录，不打断流程）。"""
        try:
            if self._icon_data is None:
                return
            data = self._icon_data
            data.uFlags |= NIF_INFO
            data.szInfo = text[:255]
            data.szInfoTitle = title[:63]
            data.dwInfoFlags = 1  # NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        except Exception:
            # §7.2：Win32 边界失败不改变功能语义，但至少留诊断
            _log.exception("气泡通知失败")

    # ---- 托盘线程 ----

    def _run_tray_loop(self) -> None:
        try:
            wnd_class = WNDCLASSW()
            # TH-02: 绑定方法不能直接赋给 WNDPROC_ 字段（类型错误）；
            # 用 WNDPROC_ 包装并持有引用，保证回调生命周期。
            self._wndproc_cb = WNDPROC_(self._wnd_proc)
            wnd_class.lpfnWndProc = self._wndproc_cb
            wnd_class.lpszClassName = 'TokenHubTrayV3'
            wnd_class.hInstance = kernel32.GetModuleHandleW(None)
            wnd_class.hIcon = user32.LoadIconW(0, IDI_APPLICATION)

            if not user32.RegisterClassW(ctypes.byref(wnd_class)):
                self._fail_init("RegisterClassW 失败（类可能已注册，继续尝试）")

            self._hwnd = user32.CreateWindowExW(
                0, 'TokenHubTrayV3', 'TokenHubTrayV3', 0,
                0, 0, 0, 0, 0, 0, wnd_class.hInstance, None)
            if not self._hwnd:
                self._fail_init("创建托盘窗口失败（CreateWindowExW 返回 NULL）")
                return

            self._create_tray_icon()

            msg = wintypes.MSG()
            while self._running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self._fail_init(f"托盘线程异常: {e}")
            return

        self._destroy_tray_icon()
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None

    def _fail_init(self, message: str) -> None:
        """初始化失败：记录日志并上报主线程（经命令队列）。"""
        _log.error("托盘初始化失败: %s", message)
        self._running = False
        self._command_queue.put(("init_error", message))

    def _wnd_proc(self, hwnd, msg, wParam, lParam):
        """托盘消息回调（托盘线程内执行；只入队，不直接调回调）。"""
        if msg == WM_TRAY_CALLBACK:
            mouse_msg = lParam
            if mouse_msg == WM_RBUTTONUP:
                self._show_menu(hwnd)
            elif mouse_msg == WM_LBUTTONUP:
                self._command_queue.put('toggle')
        elif msg == WM_COMMAND:
            mapping = {
                self.MENU_SHOW: 'show',
                self.MENU_HIDE: 'hide',
                self.MENU_SETTINGS: 'settings',
                self.MENU_HELP: 'help',
                self.MENU_REFRESH: 'refresh',
                self.MENU_EXIT: 'exit',
            }
            cmd = mapping.get(wParam)
            if cmd:
                self._command_queue.put(cmd)
        elif msg == WM_DESTROY:
            self._running = False
            user32.PostQuitMessage(0)
        return user32.DefWindowProcW(hwnd, msg, wParam, lParam)

    def _create_tray_icon(self) -> None:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY_CALLBACK
        data.hIcon = user32.LoadIconW(0, IDI_APPLICATION)
        data.szTip = 'TokenHub 额度条'
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
        self._icon_data = data

    def _destroy_tray_icon(self) -> None:
        if self._icon_data:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._icon_data))
            self._icon_data = None

    def _show_menu(self, hwnd) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, self.MENU_SHOW, '显示')
        user32.AppendMenuW(menu, 0, self.MENU_HIDE, '隐藏')
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, 0, self.MENU_SETTINGS, '设置')
        user32.AppendMenuW(menu, 0, self.MENU_HELP, '帮助')
        user32.AppendMenuW(menu, 0, self.MENU_REFRESH, '立即刷新')
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, 0, self.MENU_EXIT, '退出')

        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RIGHTALIGN | TPM_BOTTOMALIGN,
                              pt.x, pt.y, 0, hwnd, None)
        user32.DestroyMenu(menu)

    # ---- 主线程派发 ----

    def _poll(self) -> None:
        """主线程轮询：把托盘命令投递到主线程执行（root.after 调度）。

        R2-06：_fail_init 先置 _running=False 再入队 init_error——因此停止
        状态也要先把队列中已入队的最终错误取出交付一次（队列不遗留、
        不无限重试）；普通命令仅在运行中派发。
        """
        try:
            while True:
                item = self._command_queue.get_nowait()
                if isinstance(item, tuple) and item and item[0] == 'init_error':
                    self._handle_init_error(item[1] if len(item) > 1 else '未知错误')
                elif self._running:
                    self._dispatch(item)
        except queue.Empty:
            pass
        if not self._running:
            return  # 最终错误已交付；不再排程
        try:
            self._poll_job = self.root.after(self.POLL_MS, self._poll)
        except Exception:
            pass

    def _handle_init_error(self, message: str) -> None:
        """托盘初始化失败：主线程回调（若提供）+ 日志；停止轮询。"""
        _log.error("托盘不可用: %s", message)
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:
                # §7.2：主线程回调自身异常不改变交付语义，但必须留诊断
                _log.exception("托盘 on_error 回调异常")

    def _dispatch(self, cmd: str) -> None:
        if cmd == 'show':
            self.on_show()
        elif cmd == 'hide':
            self.on_hide()
        elif cmd == 'toggle':
            # R2-05：优先主程序状态回调（单一状态来源）；无则回退旧的内部字段判断
            if self.on_toggle is not None:
                self.on_toggle()
            elif self._visible:
                self.on_hide()
            else:
                self.on_show()
        elif cmd == 'settings':
            self.on_settings()
        elif cmd == 'help':
            self.on_help()
        elif cmd == 'refresh':
            self.on_refresh()
        elif cmd == 'exit':
            self.on_exit()