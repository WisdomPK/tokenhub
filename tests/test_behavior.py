# -*- coding: utf-8 -*-
"""行为回归：TH-07 切换线程化 / TH-08 手动刷新与倒计时 / 可见状态统一（手动模式）。

用 object.__new__ 绕过 TokenHubApp/FloatingWindow 的重初始化（Tk/配置/网络），
以 mock 注入依赖，只验证编排逻辑本身；不创建真实 Tk 窗口。
"""

import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from main import TokenHubApp  # noqa: E402
from floating_window import FloatingWindow  # noqa: E402
from config import ConfigWriteBlocked  # noqa: E402
from refresh_state import RefreshState  # noqa: E402


def _bare_app() -> TokenHubApp:
    """构造不执行 __init__ 的 TokenHubApp，注入最小依赖。"""
    app = object.__new__(TokenHubApp)
    app._result_queue = queue.Queue()
    app._refresh_event = threading.Event()
    app._switching_inflight = False
    app._switch_gen = 0
    app._countdown_due_at = {}
    app.refresh_state = RefreshState()
    app._last_fetch = time.time()
    app._current_key = None
    app._manual_visible = False
    app._visible = False
    app._leave_since = None
    # U02：sticky 提示单槽
    app._sticky_reason = None
    app._sticky_auto_text = None
    app._refresh_failed = False
    app._last_auto_check = 0.0
    return app


# ---------------------------------------------------------------------------
# TH-08a：手动刷新无条件触发
# ---------------------------------------------------------------------------

class ForceRefreshTests(unittest.TestCase):

    def test_maybe_refresh_skips_when_data_fresh(self):
        """老化判定仍生效：数据新时 _maybe_refresh_now 不置位。"""
        app = _bare_app()
        app._maybe_refresh_now()
        self.assertFalse(app._refresh_event.is_set())

    def test_force_refresh_sets_event_even_when_fresh(self):
        """强制刷新不受 15s 过期条件限制（新增/删除账号、倒计时归零共用）。"""
        app = _bare_app()
        app._force_refresh_now()
        self.assertTrue(app._refresh_event.is_set())

    def test_on_refresh_unconditional(self):
        """手动/托盘刷新（on_refresh）无条件置位刷新事件。"""
        app = _bare_app()
        app.on_refresh()
        self.assertTrue(app._refresh_event.is_set())


# ---------------------------------------------------------------------------
# TH-07：切换线程化（单在途 + 代际计数）
# ---------------------------------------------------------------------------

class SwitchThreadingTests(unittest.TestCase):

    def _app(self):
        app = _bare_app()
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app.cfg = mock.Mock()
        app.cfg.find_by_key.return_value = mock.Mock(label='备用')
        app.cfg.accounts = []
        app.store = mock.Mock()
        app.store.current_key.return_value = 'key-a'
        app.store.switch_to.return_value = (True, '直写文件', 'ok')
        app.ctrl = mock.Mock()
        app.ctrl.discover.return_value = None  # 强制走直写降级路径
        app._current_key = 'key-a'
        return app

    def test_switch_runs_off_main_thread_and_restores(self):
        app = self._app()
        main_thread_id = threading.get_ident()
        seen_threads = []

        real = app.store.switch_to

        def spy(key):
            seen_threads.append(threading.get_ident())
            return real(key)

        app.store.switch_to = spy

        app.on_switch('key-b')
        self.assertTrue(app._switching_inflight)
        self.assertEqual(app._switch_gen, 1)
        app.win.set_switching.assert_called_once_with('key-b')

        kind, payload = app._result_queue.get(timeout=5)
        self.assertEqual(kind, 'switch_result')
        gen, key, label, ok, message, status = payload
        self.assertEqual((gen, key, ok, message, status),
                         (1, 'key-b', True, '直写文件', 'ok'))
        self.assertTrue(seen_threads)  # 实际切换发生在后台线程
        self.assertNotEqual(seen_threads[0], main_thread_id)

        app._handle_switch_result(gen, key, label, ok, message, status)
        self.assertFalse(app._switching_inflight)
        app.win.set_switching.assert_called_with(None)
        app.win.set_toast.assert_called_once()  # 成功 toast

    def test_second_switch_rejected_while_inflight(self):
        """一次只允许一个切换在途：在途期间再次调用被忽略，不产生第二个结果。"""
        app = self._app()
        app.on_switch('key-b')
        app.on_switch('key-c')
        self.assertEqual(app._switch_gen, 1)
        self.assertTrue(app._switching_inflight)
        app._result_queue.get(timeout=5)
        self.assertTrue(app._result_queue.empty())

    def test_stale_generation_result_discarded(self):
        """过期代际结果被丢弃：不恢复 in-flight、不触碰 set_switching/toast。"""
        app = self._app()
        app._switching_inflight = True
        app._switch_gen = 2
        app._handle_switch_result(1, 'key-b', '备用', True, 'ok', 'ok')
        self.assertTrue(app._switching_inflight)
        app.win.set_switching.assert_not_called()
        app.win.set_toast.assert_not_called()

    def test_switch_failure_toasts_error_and_restores(self):
        """失败路径（unreachable）：原样 error + 恢复可操作状态（R2-02 不核对）。"""
        app = self._app()
        app.store.switch_to.return_value = (False, '切换失败（mock）', 'unreachable')
        app.on_switch('key-b')
        kind, payload = app._result_queue.get(timeout=5)
        self.assertEqual(kind, 'switch_result')
        app._handle_switch_result(*payload)
        self.assertFalse(app._switching_inflight)
        app.win.set_switching.assert_called_with(None)
        # R2-02：非 timeout_uncertain 一律原样 error，不附着"已核对"
        app.win.set_toast.assert_called_with('切换失败（mock）', 'error')
        # 错误类不接 follow_up（「请重启 opencode」仅跟随成功提示）
        self.assertNotIn('follow_up', app.win.set_toast.call_args[1])

    def test_switch_timeout_but_verified_effective(self):
        """R2-02：仅 timeout_uncertain 核对 auth.json 实际已生效 → 成功态提示（U09）。"""
        app = self._app()
        app.store.switch_to.return_value = (
            False, '控制接口超时，切换结果待确认', 'timeout_uncertain')
        app.store.current_key.return_value = 'key-b'  # 实际已切到目标账号
        app.on_switch('key-b')
        kind, payload = app._result_queue.get(timeout=5)
        app._handle_switch_result(*payload)
        app.win.set_toast.assert_called_with(
            '已切换至「备用」：已核对文件中的目标账号', 'success',
            follow_up=('请重启 opencode', 3000))


# ---------------------------------------------------------------------------
# 可见状态统一：托盘手动显示模式
# ---------------------------------------------------------------------------

class ManualVisibilityTests(unittest.TestCase):

    def _tracked_app(self):
        app = _bare_app()
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app.win.is_visible.return_value = False
        app.win.width = 620
        app.win.height = 70
        app.cfg = mock.Mock()
        app.cfg.settings = {
            'hotzone_offset': 48, 'hotzone_height': 10,
            'hotzone_width_ratio': 0.5, 'fallback_screen_edge': True,
        }
        app.tracker = mock.Mock()
        app.tracker.is_valid.return_value = True
        app.tracker.is_point_over_window.return_value = True   # 鼠标下方是 opencode 窗口
        app.tracker.is_opencode_running.return_value = False   # 进程未运行（回退可用）
        app.tracker.get_rect.return_value = mock.Mock(
            left=100, top=100, right=900, bottom=700, center_x=lambda: 500)
        app.tracker.get_cursor_pos.return_value = (500, 400)  # 不在热区、面板外
        app.tracker.in_hotzone.return_value = False
        app.tracker.bar_geometry.side_effect = (
            lambda rect, w, h, visible, *a, **k: (400, 100 if visible else -46))
        app._hwnd = 12345
        app._last_find = time.monotonic()
        return app

    def test_manual_show_hides_and_flags(self):
        app = _bare_app()
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app.set_manual_visible(True)
        self.assertTrue(app._manual_visible)
        app.win.set_visible.assert_called_once_with(True)
        app.set_manual_visible(False)
        self.assertFalse(app._manual_visible)
        app.win.set_visible.assert_called_with(False)

    def test_manual_mode_keeps_panel_shown_outside_hotzone(self):
        """手动模式：鼠标不在热区/面板外，tick 仍保持显示（不随悬停收起）。"""
        app = self._tracked_app()
        app._manual_visible = True
        app._update_position_and_visibility()
        self.assertTrue(app._visible)
        app.win.set_visible.assert_called_with(True)

    def test_no_manual_hover_logic_unchanged(self):
        """非手动模式：热区/面板外 → 隐藏（TH-09 判定兼容，不唤出）。"""
        app = self._tracked_app()
        app._manual_visible = False
        app._update_position_and_visibility()
        self.assertFalse(app._visible)
        app.win.set_visible.assert_not_called()


class ModalVisibilityTests(unittest.TestCase):
    """弹窗打开期间（is_modal_open）悬浮窗保持显示不收起。"""

    def _tracked_app(self, modal=False):
        app = _bare_app()
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = modal
        app.win.is_visible.return_value = False
        app.win.width = 620
        app.win.height = 70
        app.cfg = mock.Mock()
        app.cfg.settings = {
            'hotzone_offset': 48, 'hotzone_height': 10,
            'hotzone_width_ratio': 0.5, 'fallback_screen_edge': True,
        }
        app.tracker = mock.Mock()
        app.tracker.is_valid.return_value = True
        app.tracker.is_point_over_window.return_value = True   # 鼠标下方是 opencode 窗口
        app.tracker.is_opencode_running.return_value = False   # 进程未运行（回退可用）
        app.tracker.get_rect.return_value = mock.Mock(
            left=100, top=100, right=900, bottom=700, center_x=lambda: 500)
        app.tracker.get_cursor_pos.return_value = (500, 400)  # 不在热区、面板外
        app.tracker.in_hotzone.return_value = False
        app.tracker.bar_geometry.side_effect = (
            lambda rect, w, h, visible, *a, **k: (400, 100 if visible else -46))
        app._hwnd = 12345
        app._last_find = time.monotonic()
        return app

    def test_modal_open_keeps_panel_shown_away_from_mouse(self):
        """弹窗打开期间：鼠标远离热区/面板外仍保持显示（不触发收起）。"""
        app = self._tracked_app(modal=True)
        app._update_position_and_visibility()
        self.assertTrue(app._visible)
        app.win.set_visible.assert_called_with(True)

    def test_modal_closed_restores_hover_logic(self):
        """弹窗关闭：非手动、热区外 → 恢复隐藏（TH-09 判定不受影响）。"""
        app = self._tracked_app(modal=False)
        app._update_position_and_visibility()
        self.assertFalse(app._visible)
        app.win.set_visible.assert_not_called()

    def test_modal_close_then_hide_delay_recovers(self):
        """弹窗关闭后：先记离开时刻，超过 400ms 延迟再收起。"""
        app = self._tracked_app(modal=True)
        app._update_position_and_visibility()
        self.assertTrue(app._visible)  # 弹窗开着 → 保持
        app.win.is_modal_open.return_value = False  # 弹窗关闭
        app._update_position_and_visibility()  # 第一次：记 leave_since
        self.assertTrue(app._visible)          # 未到延迟，仍显示
        app._leave_since = time.monotonic() - 1  # 模拟超过 HIDE_DELAY_SECONDS
        app._update_position_and_visibility()
        self.assertFalse(app._visible)
        app.win.set_visible.assert_called_with(False)

    def test_modal_open_keeps_panel_in_fallback_mode(self):
        """无窗口回退（屏幕顶边）：弹窗打开期间同样保持显示。"""
        app = self._tracked_app(modal=True)
        app._hwnd = None
        app._last_find = time.monotonic()
        app.tracker.is_valid.return_value = False
        app.tracker.find_opencode_window.return_value = None
        app.root = mock.Mock()
        app.root.winfo_screenwidth.return_value = 1920
        app._update_position_and_visibility()
        self.assertTrue(app._visible)

    def test_modal_open_keeps_panel_when_no_window_no_fallback(self):
        """无窗口且不回退：弹窗打开期间保持显示（is_modal_open 直通）。"""
        app = self._tracked_app(modal=True)
        app._hwnd = None
        app._last_find = time.monotonic()
        app.tracker.is_valid.return_value = False
        app.tracker.find_opencode_window.return_value = None
        app.cfg.settings['fallback_screen_edge'] = False
        app._update_position_and_visibility()
        self.assertTrue(app._visible)


class MinimizedWindowTests(unittest.TestCase):
    """最小化场景：失效窗口立即视为无窗口；进程在运行时不落顶边回退；
    热区必须位于 opencode 窗口之上。"""

    def _app(self):
        app = _bare_app()
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app.win.is_visible.return_value = False
        app.win.width = 620
        app.win.height = 70
        app.cfg = mock.Mock()
        app.cfg.settings = {
            'hotzone_offset': 48, 'hotzone_height': 10,
            'hotzone_width_ratio': 0.5, 'fallback_screen_edge': True,
        }
        app.tracker = mock.Mock()
        app.tracker.is_valid.return_value = True
        app.tracker.is_point_over_window.return_value = True
        app.tracker.is_opencode_running.return_value = False
        app.tracker.get_rect.return_value = mock.Mock(
            left=100, top=100, right=900, bottom=700, center_x=lambda: 500)
        app.tracker.get_cursor_pos.return_value = (500, 400)  # 不在热区、面板外
        app.tracker.in_hotzone.return_value = False
        app.tracker.bar_geometry.side_effect = (
            lambda rect, w, h, visible, *a, **k: (400, 100 if visible else -46))
        app._hwnd = 12345
        app._last_find = time.monotonic()
        return app

    def test_minimized_window_hides_and_skips_fallback(self):
        """① 窗口失效（最小化）+ opencode 进程在运行 → 不显示、不走顶边回退。"""
        app = self._app()
        app.tracker.is_valid.return_value = False      # IsIconic → 失效
        app.tracker.is_opencode_running.return_value = True
        app.tracker.find_opencode_window.return_value = None
        app._fallback_screen_edge = mock.Mock()
        app._update_position_and_visibility()
        self.assertIsNone(app._hwnd)                   # 失效窗口立即视为无窗口
        self.assertFalse(app._visible)
        app._fallback_screen_edge.assert_not_called()  # 最小化不退化为顶边回退

    def test_not_running_falls_back_screen_edge(self):
        """② opencode 未运行 + fallback 开 → 顶边回退照常（顶边热区可唤出）。"""
        app = self._app()
        app._hwnd = None
        app.tracker.find_opencode_window.return_value = None
        app.tracker.is_opencode_running.return_value = False
        app.root = mock.Mock()
        app.root.winfo_screenwidth.return_value = 1920
        app.tracker.get_cursor_pos.return_value = (960, 5)  # 屏幕顶边中央热区
        app._update_position_and_visibility()
        app.win.update_position.assert_called_with(650, 0, -70)  # 回退路径已执行
        self.assertTrue(app._visible)

    def test_hotzone_requires_point_over_opencode(self):
        """③ 热区条内但鼠标下方不是 opencode 窗口 → 不唤出；是 → 唤出。"""
        app = self._app()
        app.tracker.in_hotzone.return_value = True
        app.tracker.is_point_over_window.return_value = False
        app._update_position_and_visibility()
        self.assertFalse(app._visible)                 # 被其他窗口遮挡 → 不响应
        app.tracker.is_point_over_window.return_value = True
        app._update_position_and_visibility()
        self.assertTrue(app._visible)                  # 鼠标下方是 opencode → 响应

    def test_visible_panel_stays_when_point_check_fails_but_inside(self):
        """④ 面板已可见：point 校验 False 但鼠标在面板矩形内 → inside 兜底保持。"""
        app = self._app()
        app._visible = True
        app.win.is_visible.return_value = True
        app.tracker.in_hotzone.return_value = False
        app.tracker.is_point_over_window.return_value = False
        app.tracker.get_cursor_pos.return_value = (450, 120)  # 面板矩形内
        app._update_position_and_visibility()
        self.assertTrue(app._visible)
        app.win.set_visible.assert_not_called()        # 状态无变化（未误收起）

    def test_manual_mode_keeps_panel_when_no_window_no_fallback(self):
        """手动显示/弹窗保持语义在无窗口分支保留（含仅最小化场景）。"""
        app = self._app()
        app._hwnd = None
        app.tracker.find_opencode_window.return_value = None
        app.tracker.is_opencode_running.return_value = True  # 仅最小化
        app._manual_visible = True
        app._update_position_and_visibility()
        self.assertTrue(app._visible)                  # 手动模式不被误收起
        app.win.set_visible.assert_called_with(True)


# ---------------------------------------------------------------------------
# TH-08c：倒计时归零 → 强制刷新（main 侧 ≥60s 去抖）
# ---------------------------------------------------------------------------

class CountdownDueMainTests(unittest.TestCase):

    def test_due_triggers_force_refresh_with_debounce(self):
        app = _bare_app()
        app.on_countdown_due('key-a')
        self.assertTrue(app._refresh_event.is_set())
        app._refresh_event.clear()
        app.on_countdown_due('key-a')  # 60s 内同账号 → 去抖
        self.assertFalse(app._refresh_event.is_set())
        app._countdown_due_at['key-a'] = time.time() - 61
        app.on_countdown_due('key-a')  # 超过 60s → 再次触发
        self.assertTrue(app._refresh_event.is_set())

    def test_due_debounce_is_per_account(self):
        app = _bare_app()
        app.on_countdown_due('key-a')
        app._refresh_event.clear()
        app.on_countdown_due('key-b')  # 不同账号不受 key-a 去抖影响
        self.assertTrue(app._refresh_event.is_set())


# ---------------------------------------------------------------------------
# TH-08c：FloatingWindow 侧归零检测（只报一次、每 tick 去重）
# ---------------------------------------------------------------------------

class _Metric:
    def __init__(self, resets_at):
        self.resets_at = resets_at
        self.percent = 10
        self.error = None


class _Usage:
    def __init__(self, rolling_resets_at):
        self.rolling = _Metric(rolling_resets_at)
        self.weekly = _Metric(None)      # 无 resets_at → '--'，不算归零
        self.monthly = _Metric(None)
        self.fetched_at = time.time()


class CountdownDueWindowTests(unittest.TestCase):

    def test_due_transition_fires_once_per_tick(self):
        win = object.__new__(FloatingWindow)
        win._cd_due = {}
        win._metric_blocks = [
            {'key': 'k1', 'metric': 'rolling', 'offline': False,
             'usage': _Usage('2000-01-01T00:00:00+00:00')},  # 已过期
            {'key': 'k1', 'metric': 'weekly', 'offline': False,
             'usage': _Usage('2999-01-01T00:00:00+00:00')},  # 未过期
        ]
        due = win._collect_due_keys()
        self.assertEqual(due, ['k1'])          # 仅滚动指标归零，每 tick 去重
        self.assertEqual(win._collect_due_keys(), [])  # 已上报 → 不再报

    def test_offline_block_never_due(self):
        win = object.__new__(FloatingWindow)
        win._cd_due = {}
        win._metric_blocks = [
            {'key': 'k1', 'metric': 'rolling', 'offline': True,
             'usage': _Usage('2000-01-01T00:00:00+00:00')},
        ]
        self.assertEqual(win._collect_due_keys(), [])


# ---------------------------------------------------------------------------
# R2-02：切换结果状态矩阵（仅 timeout_uncertain 核对；restore_failed 不升级）
# ---------------------------------------------------------------------------

class SwitchResultStatusTests(unittest.TestCase):

    def _app(self):
        app = _bare_app()
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app.cfg = mock.Mock()
        app.cfg.find_by_key.return_value = mock.Mock(label='主账号')
        app.cfg.accounts = []
        app.store = mock.Mock()
        app.store.current_key.return_value = 'key-a'
        app._current_key = 'key-a'
        return app

    def _finish(self, app, ok, message, status, key='key-b', label='备用'):
        app._switching_inflight = True
        app._switch_gen = 3
        app._handle_switch_result(3, key, label, ok, message, status)

    def test_ok_success_includes_switch_method(self):
        """U09：成功提示透传切换方式消息 + follow_up「请重启 opencode」（3 秒）。"""
        app = self._app()
        self._finish(app, True, '直写文件', 'ok')
        app.win.set_toast.assert_called_once_with(
            '已切换至「备用」：直写文件', 'success',
            follow_up=('请重启 opencode', 3000))

    def test_success_toast_never_claims_immediate_effect(self):
        """成功类提示不得包含"即时生效/立即"字样；重启提示由 follow_up 承担。"""
        app = self._app()
        self._finish(app, True, '切换请求已成功', 'ok')
        text, kind = app.win.set_toast.call_args[0]
        kwargs = app.win.set_toast.call_args[1]
        self.assertEqual(kind, 'success')
        self.assertNotIn('即时生效', text)
        self.assertNotIn('立即', text)
        self.assertEqual(kwargs.get('follow_up'), ('请重启 opencode', 3000))

    def test_timeout_uncertain_target_current_upgrades_to_success(self):
        """超时待确认 + 核对 current==target → 成功（唯一允许升级的失败态）。"""
        app = self._app()
        app.store.current_key.return_value = 'key-b'
        self._finish(app, False, '控制接口超时，切换结果待确认', 'timeout_uncertain')
        app.win.set_toast.assert_called_once_with(
            '已切换至「备用」：已核对文件中的目标账号', 'success',
            follow_up=('请重启 opencode', 3000))

    def test_timeout_uncertain_other_current_stays_error_with_check(self):
        app = self._app()
        self._finish(app, False, '控制接口超时，切换结果待确认', 'timeout_uncertain')
        app.win.set_toast.assert_called_once_with(
            '控制接口超时，切换结果待确认；已核对：当前账号仍为「主账号」',
            'error', sticky=True)  # U02：核对未生效 → sticky error
        self.assertEqual(app._sticky_reason, 'switch')

    def test_timeout_uncertain_no_current_passthrough(self):
        app = self._app()
        app.store.current_key.return_value = None
        self._finish(app, False, '控制接口超时，切换结果待确认', 'timeout_uncertain')
        app.win.set_toast.assert_called_once_with(
            '控制接口超时，切换结果待确认', 'error', sticky=True)
        self.assertEqual(app._sticky_reason, 'switch')

    def test_restore_failed_with_target_key_residue_never_upgrades(self):
        """R2-02 核心：恢复失败 + 目标密钥残留 → 保持 error，绝不成功升级；
        U02：sticky 保留原错误+备份信息。"""
        app = self._app()
        app.store.current_key.return_value = 'key-b'  # 恰是恢复失败的残留结果
        self._finish(app, False, '恢复失败，状态待确认；备份位于 <path>', 'restore_failed')
        args, kwargs = app.win.set_toast.call_args
        text, kind = args
        self.assertEqual(kind, 'error')
        self.assertNotIn('成功', text)
        self.assertNotIn('已核对生效', text)
        self.assertIn('恢复失败，状态待确认', text)
        self.assertTrue(kwargs.get('sticky'))  # U02
        self.assertEqual(app._sticky_reason, 'switch')

    def test_other_failures_passthrough_error_verbatim(self):
        """restored/unaffected/denied/unreachable → 原样 error，不附着"已核对"。"""
        cases = (
            ('restored', '读回校验失败，已恢复原账号'),
            ('unaffected', '写入失败，原账号未受影响'),
            ('denied', '控制接口拒绝（401）'),
            ('unreachable', '控制接口不可达'),
        )
        for status, message in cases:
            app = self._app()
            self._finish(app, False, message, status)
            app.win.set_toast.assert_called_once_with(message, 'error')


# ---------------------------------------------------------------------------
# R2-01：损坏配置保护（启动路径不写账号池 + 错误可见）
# ---------------------------------------------------------------------------

class ConfigLoadErrorTests(unittest.TestCase):

    def test_check_config_load_error_surfaces_and_returns_true(self):
        app = _bare_app()
        app.cfg = mock.Mock()
        app.cfg.load_error = '配置解析失败（mock），已保留原文件'
        app.cfg._path = r'C:\mock\config.json'
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        self.assertTrue(app._check_config_load_error())
        app.win.set_toast.assert_called_once()
        args, kwargs = app.win.set_toast.call_args
        text, kind = args
        self.assertEqual(kind, 'error')
        self.assertTrue(kwargs.get('sticky'))  # U02：需要用户处理 → sticky
        self.assertIn('配置无法读取，已保护原文件', text)
        self.assertIn(r'C:\mock\config.json', text)
        self.assertEqual(app._sticky_reason, 'config')

    def test_check_config_load_error_clean_returns_false(self):
        app = _bare_app()
        app.cfg = mock.Mock()
        app.cfg.load_error = None
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        self.assertFalse(app._check_config_load_error())
        app.win.set_toast.assert_not_called()

    def test_reload_current_state_skips_pool_write_when_load_error(self):
        """R2-01：load_error 非空时启动/轮询路径均不触发 ensure_current_in_pool。"""
        app = _bare_app()
        app.cfg = mock.Mock()
        app.cfg.load_error = '配置解析失败（mock）'
        app.cfg.find_by_key.return_value = None
        app.cfg.accounts = []
        app.store = mock.Mock()
        app.store.current_key.return_value = 'sk-current'
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app._current_key = None
        app._reload_current_state()
        app.cfg.ensure_current_in_pool.assert_not_called()
        self.assertEqual(app._current_key, 'sk-current')
        app.win.update_accounts.assert_called_once()

    def test_reload_current_state_pool_write_when_clean(self):
        """对照组：无 load_error 时照常自动加入账号池。"""
        app = _bare_app()
        app.cfg = mock.Mock()
        app.cfg.load_error = None
        app.cfg.find_by_key.return_value = None
        app.cfg.accounts = []
        app.store = mock.Mock()
        app.store.current_key.return_value = 'sk-current'
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app._current_key = None
        app._reload_current_state()
        app.cfg.ensure_current_in_pool.assert_called_once_with('sk-current')


# ---------------------------------------------------------------------------
# R2-05：托盘左键切换（单一状态来源）与可见态同步
# ---------------------------------------------------------------------------

class TrayToggleTests(unittest.TestCase):

    def test_on_tray_toggle_alternates_manual_state_and_syncs(self):
        app = _bare_app()
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app._tray = mock.Mock()
        app._on_tray_toggle()
        self.assertTrue(app._manual_visible)
        app._on_tray_toggle()
        self.assertFalse(app._manual_visible)
        app._tray.set_visible.assert_has_calls(
            [mock.call(True), mock.call(False)])

    def test_apply_visibility_syncs_tray(self):
        app = _bare_app()
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app._tray = mock.Mock()
        app._visible = False
        app._leave_since = None
        app._apply_visibility(True)
        app._tray.set_visible.assert_called_once_with(True)
        # 隐藏走 400ms 延迟：第一次只记 leave_since，不同步
        app._apply_visibility(False)
        app._tray.set_visible.assert_called_once_with(True)
        app._leave_since = time.monotonic() - 1  # 超过 HIDE_DELAY_SECONDS
        app._apply_visibility(False)
        app._tray.set_visible.assert_called_with(False)
        self.assertFalse(app._visible)

    def test_manual_show_syncs_tray(self):
        app = _bare_app()
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app._tray = mock.Mock()
        app.set_manual_visible(True)
        app._tray.set_visible.assert_called_once_with(True)
        app.set_manual_visible(False)
        app._tray.set_visible.assert_called_with(False)

    def test_tray_dispatch_toggle_prefers_on_toggle(self):
        """tray 侧：on_toggle 存在时左键不再依赖内部 _visible 字段。"""
        from tray import TrayIcon
        tray = object.__new__(TrayIcon)
        tray.on_toggle = mock.Mock()
        tray.on_show = mock.Mock()
        tray.on_hide = mock.Mock()
        tray._visible = True  # 旧字段错误状态也不影响新回调路径
        tray._dispatch('toggle')
        tray.on_toggle.assert_called_once()
        tray.on_hide.assert_not_called()
        tray.on_show.assert_not_called()


# ---------------------------------------------------------------------------
# R2-06：托盘初始化错误交付主线程（恰一次、队列不遗留）
# ---------------------------------------------------------------------------

class TrayInitErrorTests(unittest.TestCase):

    def _bare_tray(self):
        from tray import TrayIcon
        tray = object.__new__(TrayIcon)
        tray._running = False
        tray._command_queue = queue.Queue()
        tray._handle_init_error = mock.Mock()
        tray._dispatch = mock.Mock()
        return tray

    def test_poll_delivers_init_error_after_fail_init(self):
        """R2-06：_fail_init 置停止后，_poll 仍取走并交付已入队错误。"""
        tray = self._bare_tray()
        tray._command_queue.put(('init_error', '注册托盘窗口失败'))
        tray._poll()
        tray._handle_init_error.assert_called_once_with('注册托盘窗口失败')
        self.assertTrue(tray._command_queue.empty())  # 队列不遗留
        tray._dispatch.assert_not_called()           # 停止态不派发普通命令

    def test_init_error_delivered_exactly_once(self):
        """再次轮询不重复交付（无无限重试）。"""
        tray = self._bare_tray()
        tray._command_queue.put(('init_error', 'x'))
        tray._poll()
        tray._poll()
        tray._handle_init_error.assert_called_once_with('x')

    def test_on_error_main_callback_once(self):
        """main 侧 _on_tray_error：log + sticky toast 恰一次（U02）。"""
        app = _bare_app()
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app._on_tray_error('创建托盘窗口失败（CreateWindowExW 返回 NULL）')
        app.win.set_toast.assert_called_once()
        args, kwargs = app.win.set_toast.call_args
        text, kind = args
        self.assertEqual(kind, 'error')
        self.assertTrue(kwargs.get('sticky'))
        self.assertIn('托盘初始化失败', text)
        self.assertIn('创建托盘窗口失败', text)
        self.assertEqual(app._sticky_reason, 'tray')


# ---------------------------------------------------------------------------
# R3-03：账号增删回调返回值契约（失败 (False, msg)，成功 (True, "")）
# ---------------------------------------------------------------------------

class AccountCallbackTests(unittest.TestCase):

    def test_add_account_success_returns_true(self):
        app = _bare_app()
        app.cfg = mock.Mock()
        app.cfg.accounts = []
        app.cfg.find_by_key.return_value = None
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        result = app.on_add_account('', 'sk-demo')
        self.assertEqual(result, (True, ''))
        label_arg = app.cfg.upsert_account.call_args[0][0]
        self.assertEqual(label_arg, '账号1')  # 空标签 → 默认名
        self.assertTrue(app._refresh_event.is_set())  # 成功 → 强制刷新

    def test_add_account_blocked_returns_false_and_toast(self):
        """ConfigWriteBlocked（损坏保护）→ (False, msg) + 错误 toast。"""
        app = _bare_app()
        app.cfg = mock.Mock()
        app.cfg.accounts = []
        app.cfg.upsert_account.side_effect = ConfigWriteBlocked('配置损坏保护')
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        result = app.on_add_account('账号D', 'sk-demo')
        self.assertEqual(result[0], False)
        self.assertIn('配置写入被阻止', result[1])
        app.win.set_toast.assert_called_once()  # 错误 toast 保留
        self.assertFalse(app._refresh_event.is_set())  # 失败不触发刷新

    def test_add_account_generic_error_returns_false(self):
        app = _bare_app()
        app.cfg = mock.Mock()
        app.cfg.accounts = []
        app.cfg.upsert_account.side_effect = OSError('磁盘只读')
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        result = app.on_add_account('账号D', 'sk-demo')
        self.assertEqual(result[0], False)
        self.assertIn('磁盘只读', result[1])

    def test_delete_account_success_returns_true(self):
        app = _bare_app()
        app.cfg = mock.Mock()
        app.cfg.accounts = []
        app.cfg.find_by_key.return_value = None
        app.cfg.remove_account.return_value = True
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app._current_key = 'key-a'
        result = app.on_delete_account('key-b')
        self.assertEqual(result, (True, ''))

    def test_delete_current_account_returns_false(self):
        app = _bare_app()
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app._current_key = 'key-a'
        result = app.on_delete_account('key-a')
        self.assertEqual(result, (False, '不能删除当前使用的账号'))
        app.win.set_toast.assert_called_once_with('不能删除当前使用的账号', 'error')

    def test_delete_account_blocked_returns_false(self):
        app = _bare_app()
        app.cfg = mock.Mock()
        app.cfg.remove_account.side_effect = ConfigWriteBlocked('配置损坏保护')
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        app._current_key = 'key-a'
        result = app.on_delete_account('key-b')
        self.assertEqual(result[0], False)
        self.assertIn('配置写入被阻止', result[1])
        app.win.set_toast.assert_called_once()


# ---------------------------------------------------------------------------
# U02：sticky 提示编排（单槽优先级 config > switch > tray > auto）
# ---------------------------------------------------------------------------

class StickyToastTests(unittest.TestCase):

    def _app(self):
        app = _bare_app()
        app.cfg = mock.Mock()
        app.cfg.accounts = []
        app.cfg.find_by_key.return_value = None
        app.win = mock.Mock()
        app.win.is_modal_open.return_value = False
        return app

    def test_config_priority_blocks_auto(self):
        """手动类（config）占位后 auto 不覆盖（优先级 config > auto）。"""
        app = self._app()
        app._set_sticky('config', '配置无法读取，已保护原文件：C:/x', 'error')
        app._set_sticky('auto', '刷新失败，显示上次数据 · 点击重试', 'info')
        self.assertEqual(app._sticky_reason, 'config')
        app.win.set_toast.assert_called_once_with(
            '配置无法读取，已保护原文件：C:/x', 'error', sticky=True)

    def test_auto_replaces_none(self):
        app = self._app()
        app._set_sticky('auto', '数据较旧 · 点击刷新', 'info')
        self.assertEqual(app._sticky_reason, 'auto')
        app.win.set_toast.assert_called_once_with(
            '数据较旧 · 点击刷新', 'info', sticky=True)

    def test_auto_same_text_dedup(self):
        """auto 周期性重复 → 不重复设置。"""
        app = self._app()
        app._set_sticky('auto', '数据较旧 · 点击刷新', 'info')
        app._set_sticky('auto', '数据较旧 · 点击刷新', 'info')
        app.win.set_toast.assert_called_once()

    def test_refresh_success_clears_auto_sticky(self):
        """刷新成功且原因为 auto → clear_transient()。"""
        app = self._app()
        app._set_sticky('auto', '数据较旧 · 点击刷新', 'info')
        app._result_queue.put(('usage', (app.refresh_state.revision, {}, time.time())))
        app._poll_results()
        self.assertIsNone(app._sticky_reason)
        app.win.clear_transient.assert_called_once()

    def test_refresh_success_does_not_clear_manual_reason(self):
        """刷新成功不清除手动类 sticky（config）。"""
        app = self._app()
        app._set_sticky('config', '配置无法读取，已保护原文件：C:/x', 'error')
        app._result_queue.put(('usage', (app.refresh_state.revision, {}, time.time())))
        app._poll_results()
        self.assertEqual(app._sticky_reason, 'config')
        app.win.clear_transient.assert_not_called()

    def test_auto_sticky_refresh_failed(self):
        app = self._app()
        app._last_auto_check = 0.0
        app._refresh_failed = True
        app._last_fetch = time.time()
        app._update_auto_sticky()
        self.assertEqual(app._sticky_reason, 'auto')
        app.win.set_toast.assert_called_once_with(
            '刷新失败，显示上次数据 · 点击重试', 'info', sticky=True)

    def test_auto_sticky_stale_data(self):
        app = self._app()
        app._last_auto_check = 0.0
        app._refresh_failed = False
        app._last_fetch = time.time() - 121  # 超过 120s
        app._update_auto_sticky()
        self.assertEqual(app._sticky_reason, 'auto')
        app.win.set_toast.assert_called_once_with(
            '数据较旧 · 点击刷新', 'info', sticky=True)


if __name__ == '__main__':
    unittest.main()