# -*- coding: utf-8 -*-
"""TokenHub v3 主程序：状态机主循环 / 刷新调度 / 切换编排 / 单实例 / 父进程看护。

约定：本模块 import lane B（window_tracker）与 lane D（theme/floating_window/
settings_window/help_window/tray）的模块；这些模块未就绪时允许 import 失败，
集成后复测（py_compile 只做语法检查，不解析 import）。

状态机（每 100ms tick，plan Task A 硬性规格）：
  1. 窗口查找（失效窗口立即视为无窗口；重找节流 1s；最小化不匹配）
  2. 有窗口：bar_geometry 计算位置 → update_position；热区（须位于 opencode
     窗口上方，WindowFromPoint 校验）/面板内判定 → should_show
  3. 无窗口且 fallback_screen_edge 且 opencode 进程未运行：屏幕顶边回退模式
     （yv=0，yh=-height）；仅最小化时不回退
  4. 无窗口其余情况：隐藏（手动显示/弹窗打开期间保持）
"""

import os
import queue
import sys
import threading
import time

import tkinter as tk

from account_service import AccountService, SwitchResult
from api import AccountUsage, ApiClient, UsageMetric, WINDOW_NAMES
from auth_store import AuthStore
from config import Account, Config, ConfigWriteBlocked
from control_client import ControlClient
from floating_window import FloatingWindow
from help_window import HelpWindow
from logger import (
    log_api_error,
    log_config_load,
    log_data_update,
    log_exception,
    log_refresh_triggered,
    log_single_instance_blocked,
    log_startup,
    log_ui_action,
)
from refresh_state import RefreshState
from settings_window import SettingsWindow
from theme import ThemeManager
from tray import TrayIcon
from window_tracker import WindowTracker, set_dpi_awareness

VERSION = "3.0.0"
MUTEX_NAME = "Global\\TokenHub_SingleInstance_v3"
ERROR_ALREADY_EXISTS = 183

AUTH_POLL_SECONDS = 5.0        # auth.json mtime 监视间隔
PARENT_POLL_SECONDS = 5.0      # 父进程看护间隔
HIDE_DELAY_SECONDS = 0.4       # 离开 400ms 后收起
STALE_REFRESH_SECONDS = 15.0   # 数据超过 15s 视为旧 → 展开时即时刷新
FALLBACK_HOTZONE_WIDTH = 720   # 屏幕顶边回退热区宽度
STICKY_STALE_SECONDS = 120.0   # U02：数据年龄 >120s → auto sticky「数据较旧」
STICKY_AUTO_CHECK_SECONDS = 5.0  # U02：auto sticky 判定节流


class TokenHubApp:
    TICK_MS = 100
    # U02：sticky 优先级 config > switch > tray > auto（高优先级占位时低优先级不替换）
    STICKY_PRIORITY = {"config": 3, "switch": 2, "tray": 1, "auto": 0}

    def __init__(self):
        # 核心模块（按 plan Task A 规格）
        self.cfg = Config()
        self.store = AuthStore()
        self.api = ApiClient()
        self.ctrl = ControlClient()
        self.account_service = AccountService(self.store, self.ctrl)
        self.tracker = WindowTracker()
        self.theme = ThemeManager(choice=self.cfg.settings.get("theme", "auto"))
        colors = self.theme.get_colors()

        # 隐藏 root + 悬浮窗（lane D 契约，附录 A）
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = FloatingWindow(
            self.root, colors,
            on_switch=self.on_switch,
            on_settings=self.on_settings,
            on_help=self.on_help,
            on_exit=self.on_exit,
            on_refresh=self.on_refresh,
            on_add_account=self.on_add_account,
            on_delete_account=self.on_delete_account,
            on_countdown_due=self.on_countdown_due,
            on_status_click=self.on_refresh,  # U02：sticky 状态行点击 → 刷新/重试
        )

        # 调度状态
        self._result_queue: queue.Queue = queue.Queue()
        self._refresh_event = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self._stopping = False
        self._mutex = None

        # 窗口/可见状态
        self._hwnd = None
        self._last_find = 0.0
        self._visible = False
        self._leave_since: float | None = None

        # 数据状态
        self.refresh_state = RefreshState()
        self._last_fetch = 0.0
        self._current_key: str | None = None
        self._last_mtime: float | None = None
        self._last_auth_check = 0.0
        self._last_parent_check = 0.0
        self._tray = None

        # TH-07：切换线程化状态
        self._switching_inflight = False   # 一次只允许一个切换在途
        self._switch_gen = 0               # 代际计数：丢弃过期切换结果
        # 运行时生效语义：本进程切换成功 → 待重启；外部 auth 变化 → 未确认
        self._switch_pending_restart = False
        self._auth_external_changed = False
        # 可见状态统一：托盘手动显示/隐藏（手动显示后不随鼠标离开收起）
        self._manual_visible = False
        # TH-08c：倒计时归零去抖（key → 上次触发强制刷新时刻）
        self._countdown_due_at: dict = {}
        # U02：sticky 提示单槽（优先级 config > switch > tray > auto）
        self._sticky_reason: str | None = None
        self._sticky_auto_text: str | None = None
        self._refresh_failed = False          # 最近一次刷新是否失败（auto sticky 依据）
        self._last_auto_check = 0.0           # auto sticky 判定节流

        log_config_load(len(self.cfg.accounts))
        if not self._check_config_load_error():
            self._reload_current_state()
        self._last_mtime = self.store.mtime()
        self._maybe_refresh_now()

    def _check_config_load_error(self) -> bool:
        """R2-01/U02：启动时配置加载错误检查——有错则 sticky 提示并跳过账号池写入。

        配置损坏时 accounts 池为空，若继续 ensure_current_in_pool 会触发保存、
        覆盖刚声明保留的损坏文件；必须由用户明确修复/删除后重启。
        sticky error 持续可见（U02：需要用户处理的状态不随时间淡出）。
        """
        if not self.cfg.load_error:
            return False
        log_exception("config", RuntimeError(self.cfg.load_error))
        path = getattr(self.cfg, "_path", "配置文件")
        self._set_sticky("config", f"配置无法读取，已保护原文件：{path}", "error")
        return True

    # ------------------------------------------------------------- 单实例
    def acquire_single_instance(self) -> bool:
        import ctypes

        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
            if not handle or kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                if handle:
                    kernel32.CloseHandle(handle)
                return False
            self._mutex = handle
            return True
        except Exception as e:
            log_exception("mutex", e)
            return True  # 互斥锁获取失败不阻塞运行（保守）

    # ------------------------------------------------------------- 运行
    def run(self):
        log_startup()
        self._start_refresh_thread()
        self._setup_tray()
        self.root.after(self.TICK_MS, self._tick)
        try:
            self.root.mainloop()
        finally:
            self._shutdown()

    def _shutdown(self):
        self._stopping = True
        for label, closer in (
            ("tray.stop", lambda: self._tray.stop() if self._tray else None),
            ("win.destroy", self.win.destroy),
            ("root.destroy", self.root.destroy),
        ):
            try:
                closer()
            except Exception as e:
                log_exception(label, e)

    def _quit(self):
        self._stopping = True
        self.root.after(0, self.root.quit)

    # ------------------------------------------------------------- tick 主循环
    def _tick(self):
        if self._stopping:
            return
        try:
            self._poll_results()
            self._poll_auth()
            self._poll_parent()
            self._update_position_and_visibility()
            self._update_auto_sticky()  # U02：刷新失败/数据过旧的 sticky 提示
        except Exception as e:
            log_exception("tick", e)
        if not self._stopping:
            self.root.after(self.TICK_MS, self._tick)

    def _update_position_and_visibility(self):
        hwnd = self._track_target()
        if hwnd is None:
            if (self.cfg.settings.get("fallback_screen_edge", True)
                    and not self.tracker.is_opencode_running()):
                # 仅当 opencode 进程未运行才顶边回退；仅最小化（进程在运行）不回退
                self._fallback_screen_edge()
            else:
                # 无窗口：隐藏；手动显示/弹窗打开期间保持
                self._apply_visibility(
                    self._manual_visible or self.win.is_modal_open())
            return
        rect = self.tracker.get_rect(hwnd)
        if rect is None:
            self._apply_visibility(self._manual_visible)
            return
        mx, my = self.tracker.get_cursor_pos() or (0, 0)
        offset = int(self.cfg.settings.get("hotzone_offset", 48))
        x, yv = self.tracker.bar_geometry(
            rect, self.win.width, self.win.height, True, offset=offset)
        # 隐藏位 = 所在显示器工作区上缘上方（work_area.top - bar_h，
        # 由 window_tracker.bar_geometry 计算）
        _, yh = self.tracker.bar_geometry(rect, self.win.width, self.win.height, False)
        self.win.update_position(x, yv, yh)
        # 可见状态统一：热区 / 面板内 / 手动显示（托盘）/ 弹窗打开期间保持显示
        self._apply_visibility(
            self._hotzone_active(rect, hwnd, mx, my, offset)
            or self._panel_contains(mx, my, x, yv)
            or self._manual_visible or self.win.is_modal_open())

    def _track_target(self):
        """当前跟踪的 opencode 窗口句柄；失效立即重找（节流 1s），无则 None。"""
        hwnd = self._hwnd
        if hwnd is not None and not self.tracker.is_valid(hwnd):
            # 缓存窗口失效（含最小化）：立即视为无窗口，不再用失效窗口走正常分支
            self._hwnd = None
            hwnd = None
        if hwnd is None:
            now = time.monotonic()
            if now - self._last_find >= 1.0:  # 失效重找，节流 1s
                self._last_find = now
                self._hwnd = self.tracker.find_opencode_window()
                hwnd = self._hwnd
        if hwnd is None:
            return None
        return hwnd if self.tracker.is_valid(hwnd) else None

    def _hotzone_active(self, rect, hwnd, mx: int, my: int, offset: int) -> bool:
        """热区命中判定：鼠标下方必须确实是该窗口（其他应用/最小化残留坐标不唤出）。"""
        return self.tracker.in_hotzone(
            rect,
            int(self.cfg.settings["hotzone_height"]),
            float(self.cfg.settings["hotzone_width_ratio"]),
            mx, my,
            offset=offset,
        ) and self.tracker.is_point_over_window(hwnd, mx, my)

    def _panel_contains(self, mx: int, my: int, x: int, top: int) -> bool:
        """鼠标是否落在已展开的面板范围内（TH-09：隐藏时面板区域不参与唤出）。"""
        return (self.win.is_visible()
                and x <= mx <= x + self.win.width
                and top <= my <= top + self.win.height)

    def _fallback_screen_edge(self):
        """无窗口回退：屏幕顶边 10px、居中 720px 热区；面板 yv=0、yh=-height。"""
        sw = self.root.winfo_screenwidth()
        x = max(0, (sw - self.win.width) // 2)
        self.win.update_position(x, 0, -self.win.height)
        mx, my = self.tracker.get_cursor_pos() or (0, 0)
        half = min(FALLBACK_HOTZONE_WIDTH, sw) // 2
        in_hot = (
            (sw // 2 - half) <= mx <= (sw // 2 + half)
            and 0 <= my < int(self.cfg.settings["hotzone_height"])
        )
        # 弹窗打开期间同样保持显示（屏幕顶边回退模式）
        self._apply_visibility(
            in_hot or self._panel_contains(mx, my, x, 0)
            or self._manual_visible or self.win.is_modal_open())

    def _apply_visibility(self, should_show: bool):
        now = time.monotonic()
        if should_show:
            self._leave_since = None
            if not self._visible:
                self._enter_visible()
        elif self._visible:
            if self._leave_since is None:
                self._leave_since = now
            elif now - self._leave_since >= HIDE_DELAY_SECONDS:
                self._exit_visible()

    def _enter_visible(self) -> None:
        """面板转为可见：状态、窗口、托盘同步，数据旧则即时刷新。"""
        self._visible = True
        self.win.set_visible(True)
        self._sync_tray_visible(True)  # R2-05：状态同步到托盘（左键切换依据）
        self._maybe_refresh_now()  # 展开前数据旧 → 即时刷新

    def _exit_visible(self) -> None:
        """面板转为隐藏：状态、窗口、托盘同步（离开计时清零）。"""
        self._visible = False
        self._leave_since = None
        self.win.set_visible(False)
        self._sync_tray_visible(False)  # R2-05

    def set_manual_visible(self, visible: bool):
        """托盘手动显示/隐藏：手动显示后不随鼠标离开收起；隐藏后悬停逻辑照旧生效。"""
        self._manual_visible = visible
        self._leave_since = None
        if visible:
            self._enter_visible()
        else:
            self._exit_visible()

    def _sync_tray_visible(self, visible: bool) -> None:
        """R2-05：托盘左键切换依据 = 主程序实际可见态（托盘创建后同步，失败静默）。"""
        if getattr(self, "_tray", None) is not None:
            try:
                self._tray.set_visible(visible)
            except Exception as e:
                log_exception("tray_sync", e)

    # ------------------------------------------------------------- 刷新调度
    def _all_keys(self) -> list[str]:
        keys = [a.key for a in self.cfg.accounts]
        if self._current_key and self._current_key not in keys:
            keys.append(self._current_key)
        return keys

    def _start_refresh_thread(self):
        def loop():
            while not self._stopping:
                try:
                    self._refresh_once()
                except Exception as e:
                    log_exception("refresh", e)
                interval = max(1, int(self.cfg.settings.get("refresh_interval", 60)))
                self._refresh_event.wait(interval)
                self._refresh_event.clear()

        self._refresh_thread = threading.Thread(target=loop, daemon=True, name="tokenhub-refresh")
        self._refresh_thread.start()

    def _refresh_once(self):
        keys = self._all_keys()
        if not keys:
            return
        log_refresh_triggered()
        revision = self.refresh_state.revision
        try:
            usages = self.api.fetch_all(keys)
        except Exception as e:
            # fetch_all 按原约定不抛异常；此处仅防御装配/线程池级故障，错误仍可见
            log_api_error(str(e))
            attempted_at = time.time()
            message = str(e) or "刷新失败"
            metric = UsageMetric("error", None, None, message)
            usages = {
                key: AccountUsage(key, metric, metric, metric, attempted_at,
                                  account_error=message)
                for key in keys
            }
        else:
            attempted_at = time.time()
        # 后台线程只投递结果；成功时间/旧值保留由主线程 apply 统一提交
        self._result_queue.put(("usage", (revision, usages, attempted_at)))

    def _maybe_refresh_now(self):
        """数据超过 15s 旧 → 唤醒刷新线程立即刷新。"""
        if time.time() - self._last_fetch >= STALE_REFRESH_SECONDS:
            self._refresh_event.set()

    def _force_refresh_now(self):
        """无条件唤醒刷新线程立即 fetch_all。

        新增/删除账号后使用：新 key 不在上次刷新结果缓存中，仅靠
        _maybe_refresh_now 的过期判定可能不触发，必须强制发起一次全量拉取。
        threading.Event 跨线程安全，网络请求仍发生在刷新线程，不在 UI 线程同步发。
        """
        self._refresh_event.set()

    def _poll_results(self):
        while True:
            try:
                kind, payload = self._result_queue.get_nowait()
            except queue.Empty:
                return
            if kind == "usage":
                revision, usages, attempted_at = payload
                if not self.refresh_state.apply(revision, usages, attempted_at):
                    continue  # 过期批次：不覆盖新身份/新状态
                self._last_fetch = attempted_at
                self._refresh_failed = self._all_accounts_failed(usages)
                log_data_update(False)
                self._update_ui_accounts()
                if not self._refresh_failed and self._sticky_reason == "auto":
                    self._clear_sticky()  # U02：刷新成功且原因为 auto → 清除
            elif kind == "switch_result":
                self._handle_switch_result(*payload)

    def _all_accounts_failed(self, usages) -> bool:
        """全量失败才置全局失败；部分窗口/账号失败由各窗口 stale 标识。"""
        summaries = [self.refresh_state.account(key) for key in usages]
        summaries = [s for s in summaries if s is not None]
        return bool(summaries) and all(not s.latest_ok for s in summaries)

    # ------------------------------------------------------------- U02 sticky 编排
    def _set_sticky(self, reason: str, text: str, kind: str = "error") -> None:
        """U02：单槽 sticky 提示（优先级 config > switch > tray > auto）。

        高优先级已占用时低优先级不替换（手动类不被 auto 覆盖）；
        auto 类同文本周期性去重（不重复设置）。
        """
        priority = self.STICKY_PRIORITY
        cur = self._sticky_reason
        if cur is not None and priority.get(reason, 0) < priority.get(cur, 0):
            return
        if reason == "auto" and self._sticky_auto_text == text:
            return
        self._sticky_reason = reason
        self._sticky_auto_text = text if reason == "auto" else None
        self.win.set_toast(text, kind, sticky=True)

    def _clear_sticky(self) -> None:
        """U02：仅清除 auto 类 sticky（手动类由用户处理或下次事件替换）。"""
        if self._sticky_reason == "auto":
            self._sticky_reason = None
            self._sticky_auto_text = None
            try:
                self.win.clear_transient()
            except Exception as e:
                log_exception("clear_sticky", e)

    def _update_auto_sticky(self) -> None:
        """U02-auto：刷新失败或数据年龄 >120s → sticky info（点击重试/刷新）；5s 节流。"""
        now = time.time()
        if now - self._last_auto_check < STICKY_AUTO_CHECK_SECONDS:
            return
        self._last_auto_check = now
        if self._refresh_failed:
            self._set_sticky("auto", "刷新失败，显示上次数据 · 点击重试", "info")
        elif now - self._last_fetch > STICKY_STALE_SECONDS:
            self._set_sticky("auto", "数据较旧 · 点击刷新", "info")

    # ------------------------------------------------------------- auth.json 监视
    def _poll_auth(self):
        now = time.time()
        if now - self._last_auth_check < AUTH_POLL_SECONDS:
            return
        self._last_auth_check = now
        mtime = self.store.mtime()
        if mtime == self._last_mtime:
            return
        self._last_mtime = mtime
        if not self._switching_inflight:
            # 外部写入 auth 文件：无法推断运行时实际账号，只能保守标注未确认
            self._auth_external_changed = True
            self._switch_pending_restart = False
        self._reload_current_state()

    def _reload_current_state(self):
        key = self.store.current_key()
        if key != self._current_key:
            self._current_key = key
            # 身份变化使在途刷新批次失效，并立即安排新账号的数据刷新
            self.refresh_state.invalidate()
            if not self.cfg.load_error:
                # R2-01：配置损坏保护——不自动写入账号池（避免覆盖损坏文件）
                self.cfg.ensure_current_in_pool(key)
            self._force_refresh_now()
        self._update_ui_accounts()

    def _update_ui_accounts(self):
        key = self._current_key
        account = self.cfg.find_by_key(key) if key else None
        current_usage = self._usage_from_views(key) if key else None
        backups = [(a, self._usage_from_views(a.key))
                   for a in self.cfg.accounts if a.key != key]
        self.win.set_identity_state(self._identity_state_text())
        self.win.update_accounts(account, current_usage, backups,
                                 views=self.refresh_state.views)

    def _identity_state_text(self) -> str:
        """区分“文件已配置”和“运行时已确认生效”；无宿主证据时不伪造。"""
        if self._auth_external_changed:
            return "配置已变更，运行时账号未确认"
        if self._switch_pending_restart:
            return "已配置，待重启"
        if not self.cfg.accounts and not self._current_key:
            return "未配置"
        return "已配置"

    def _usage_from_views(self, key: str) -> AccountUsage | None:
        """把窗口级视图合成为 FloatingWindow 消费的 AccountUsage（旧值也保留）。"""
        row = self.refresh_state.views.get(key)
        if row is None:
            return None
        metrics = []
        attempts = []
        for name in WINDOW_NAMES:
            view = row.get(name)
            if view is None:
                metrics.append(UsageMetric("error", None, None, "数据不可用"))
                continue
            if view.last_attempt_at is not None:
                attempts.append(view.last_attempt_at)
            if view.metric is not None:
                metrics.append(view.metric)
            else:
                metrics.append(UsageMetric("error", None, None, view.error))
        return AccountUsage(key, metrics[0], metrics[1], metrics[2],
                            max(attempts) if attempts else time.time())

    # ------------------------------------------------------------- 父进程看护
    def _poll_parent(self):
        raw = os.environ.get("TOKENHUB_PARENT_PID")
        if not raw:
            return
        now = time.time()
        if now - self._last_parent_check < PARENT_POLL_SECONDS:
            return
        self._last_parent_check = now
        try:
            pid = int(raw)
        except ValueError:
            return
        if not self._parent_alive(pid):
            log_exception("parent", RuntimeError(f"父进程 {pid} 已退出，TokenHub 自行退出"))
            self._quit()

    @staticmethod
    def _parent_alive(pid: int) -> bool:
        """Windows 进程存活探测（修复审查报告 TH-01）。

        严禁 os.kill(pid, 0)：Python 官方文档明确，Windows 上除 CTRL_C/
        CTRL_BREAK 外的信号值都会经 TerminateProcess 直接终止目标进程——
        探测父进程会反过来杀死父进程。改用 Win32 句柄查询：
        OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess。
        声明完整 restype/argtypes 防 64 位句柄截断；句柄必释放；
        权限类错误保守视为存活（不误退）。
        """
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_INVALID_PARAMETER = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        try:
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if handle:
                try:
                    code = wintypes.DWORD()
                    if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                        return code.value == STILL_ACTIVE
                    return True  # 查询失败：保守视为存活（不误退）
                finally:
                    kernel32.CloseHandle(handle)
            if ctypes.get_last_error() == ERROR_INVALID_PARAMETER:
                return False  # 进程不存在
            return True  # 拒绝访问(5)等 → 保守视为存活
        except Exception:
            return True  # 探测本身失败：不误判父进程已退出

    # ------------------------------------------------------------- 动作
    def on_switch(self, key: str):
        """切换入口（TH-07）：主线程立即显示「切换中」，网络/文件操作移入后台线程。

        结果经 _result_queue 回主线程处理；单在途互斥 + 代际计数丢弃过期结果，
        慢响应不再阻塞 Tk 主循环（窗口可正常重绘/移动）。
        """
        if self._switching_inflight:
            return  # 一次只允许一个切换在途（UI 已禁用按钮，此处防御）
        account = self.cfg.find_by_key(key)
        label = account.label if account else Config.mask_key(key)
        self._switching_inflight = True
        self._switch_gen += 1
        gen = self._switch_gen
        self.win.set_switching(key)  # 立即禁用全部切换按钮并显示「切换中…」

        def work():
            try:
                result = self.account_service.switch(key)
            except Exception as e:  # 服务契约不抛异常；此处仅防御装配故障
                log_exception("switch", e)
                result = SwitchResult(False, "unreachable", "unknown",
                                      f"切换失败（{e}）")
            self._result_queue.put(("switch_result", (gen, key, label, result)))

        threading.Thread(target=work, daemon=True, name="tokenhub-switch").start()

    def _handle_switch_result(self, gen: int, key: str, label: str,
                              result: SwitchResult):
        """主线程处理切换结果：过期代际直接丢弃；完成/失败均恢复可操作状态。

        决策依据为 SwitchResult.outcome（不解析 message）；只区分展示方式。
        超时核对/恢复失败残留判定已在 AccountService 内完成，UI 不二次升级。
        """
        if gen != self._switch_gen:
            return  # 过期结果（更早一次切换产生）：不触碰当前状态
        self._switching_inflight = False
        self.win.set_switching(None)  # 恢复全部切换按钮
        if result.ok:
            # 本进程切换成功 = 配置已写入，但运行时仍需重启 opencode 才确认生效
            self._switch_pending_restart = True
            self._auth_external_changed = False
            self.win.set_toast(
                f"已切换至「{label}」：{result.message}", "success",
                follow_up=("请重启 opencode", 3000))
        elif result.outcome == "timeout_uncertain":
            current = None
            try:
                current = self.store.current_key()
            except Exception as e:
                log_exception("switch_verify", e)
            if current:
                account = self.cfg.find_by_key(current)
                cur_label = account.label if account else Config.mask_key(current)
                self._set_sticky(
                    "switch",
                    f"{result.message}；已核对：当前账号仍为「{cur_label}」", "error")
            else:
                self._set_sticky("switch", result.message, "error")
        elif result.outcome == "restore_failed":
            # U02：恢复失败保留原错误+备份信息，持续可见
            self._set_sticky("switch", result.message, "error")
        else:
            # restored/unaffected/denied/unreachable/未知：原样 error（普通 toast）
            self.win.set_toast(result.message, "error")
        self._reload_current_state()  # 立即重读当前账号刷新 UI
        self._last_mtime = self.store.mtime()
        self._maybe_refresh_now()

    def on_refresh(self):
        log_ui_action("refresh")
        self._force_refresh_now()  # TH-08：手动/托盘刷新无条件安排，不受 15s 过期条件限制

    def on_countdown_due(self, key: str):
        """倒计时归零回调（TH-08c）：触发一次强制刷新；同账号 ≥60s 去抖。"""
        now = time.time()
        last = self._countdown_due_at.get(key)
        if last is not None and now - last < 60:
            return
        self._countdown_due_at[key] = now
        self._force_refresh_now()

    def on_add_account(self, label: str, key: str) -> tuple[bool, str]:
        """新增账号回调（R3-03 契约）：返回 (ok, 错误消息)。

        UI 已完成真实校验；空标签补默认名；写入配置（自动落盘）。
        保存失败（含 ConfigWriteBlocked）→ (False, msg) 并保留错误 toast，
        弹窗据此保持打开、保留输入；成功 → (True, "")。
        空标签直接交给 Config.upsert_account 生成唯一默认名。
        """
        try:
            self.cfg.upsert_account(label, key)  # 变更即落盘
        except ConfigWriteBlocked as e:
            # R2-01：写入被阻止（损坏保护等）——明确提示，不静默
            self.win.set_toast(f"新增失败：配置写入被阻止（{e}）", "error")
            return (False, f"配置写入被阻止（{e}）")
        except Exception as e:
            log_exception("add_account", e)
            self.win.set_toast(f"新增失败（{e}）", "error")
            return (False, str(e) or "写入配置失败")
        self.refresh_state.invalidate()
        self._update_ui_accounts()
        self._force_refresh_now()
        return (True, "")

    def on_delete_account(self, key: str) -> tuple[bool, str]:
        """删除备用账号回调（R3-03 契约）：返回 (ok, 错误消息)。

        当前账号被 UI 拦截，此处兜底忽略；删除失败返回 (False, msg)，
        UI 保留行；成功 → (True, "")。
        """
        if key == self._current_key:
            msg = "不能删除当前使用的账号"
            self.win.set_toast(msg, "error")
            return (False, msg)
        try:
            self.cfg.remove_account(key)  # 变更即落盘；删除的是备用，不影响 ensure_current_in_pool
        except ConfigWriteBlocked as e:
            # R2-01：写入被阻止（损坏保护等）——明确提示，不静默
            self.win.set_toast(f"删除失败：配置写入被阻止（{e}）", "error")
            return (False, f"配置写入被阻止（{e}）")
        except Exception as e:
            log_exception("remove_account", e)
            return (False, str(e) or "删除失败")
        self.refresh_state.invalidate()
        self._update_ui_accounts()
        self._force_refresh_now()
        return (True, "")

    def on_settings(self):
        log_ui_action("settings")
        try:
            SettingsWindow(
                self.root,
                self.cfg,
                self.theme.get_colors(),
                api=self.api,
                on_save=self._on_config_saved,
                get_current_key=self.store.current_key,
            )
        except Exception as e:
            log_exception("settings", e)

    def on_help(self):
        log_ui_action("help")
        try:
            HelpWindow(self.root, self.theme.get_colors())
        except Exception as e:
            log_exception("help", e)

    def on_exit(self):
        log_ui_action("exit")
        self._quit()

    def _on_config_saved(self):
        self.cfg.load()  # 设置窗口可能已修改配置 → 重新读取
        try:  # 主题热切换
            self.theme.set_theme(self.cfg.settings.get("theme", "auto"))
            self.win.apply_theme(self.theme.get_colors())
        except Exception as e:
            log_exception("theme", e)
        # 账号列表立即反映增删/改名，再强制刷新数据（不等 15s 节流/下轮轮询）
        self.refresh_state.invalidate()
        self._update_ui_accounts()
        self._force_refresh_now()

    def _setup_tray(self):
        try:
            self._tray = TrayIcon(
                root=self.root,
                on_show=lambda: self.set_manual_visible(True),
                on_hide=lambda: self.set_manual_visible(False),
                on_toggle=self._on_tray_toggle,
                on_settings=self.on_settings,
                on_help=self.on_help,
                on_refresh=self.on_refresh,
                on_exit=self.on_exit,
                on_error=self._on_tray_error,
            )
            self._tray.start()
            self._sync_tray_visible(self._visible)  # R2-05：初始可见态同步
        except Exception as e:
            log_exception("tray", e)

    def _on_tray_toggle(self):
        """托盘左键：在手动显示/隐藏之间交替（R2-05，状态来源单一在主程序）。"""
        self.set_manual_visible(not self._manual_visible)

    def _on_tray_error(self, msg: str):
        """托盘初始化失败回调（R2-06/U02）：log + sticky error（持续可见）。"""
        log_exception("tray", RuntimeError(msg))
        self._set_sticky("tray", f"托盘初始化失败：{msg}", "error")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def self_test() -> int:
    """无 GUI 自检：初始化核心模块 + 调用一次真实用量接口，仅打印掩码 key。"""
    print(f"TokenHub v{VERSION} self-test")
    cfg = Config()
    store = AuthStore()
    api = ApiClient()
    key = store.current_key()
    if not key:
        print("未找到 opencode-go 密钥（auth.json 缺失或无 opencode-go 字段）")
        return 1
    usage = api.fetch_usage(key)
    print(f"账号: {Config.mask_key(key)}")
    rows = [("5小时", usage.rolling), ("本周", usage.weekly), ("本月", usage.monthly)]
    ok_count = 0
    for name, metric in rows:
        if metric.percent is not None:
            print(f"{name}: {metric.percent}% | 重置: {metric.resets_at}")
            ok_count += 1
        else:
            print(f"{name}: 离线（{metric.error}）")
    if ok_count:
        print("自检通过")
        return 0
    print("自检失败")
    return 1


def main(argv=None) -> int:
    # 必须先于任何 Tk 窗口创建：PerMonitorV2 DPI 感知
    #（缺此调用 → 进程 DPI 无感知，窗口被系统按 125% 位图拉伸 → 文字发虚）
    set_dpi_awareness()
    args = sys.argv[1:] if argv is None else list(argv)
    if "--version" in args:
        print(VERSION)
        return 0
    if "--self-test" in args:
        return self_test()
    app = TokenHubApp()
    if not app.acquire_single_instance():
        log_single_instance_blocked()
        return 0
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())