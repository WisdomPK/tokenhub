# -*- coding: utf-8 -*-
"""W6（F7）：刷新代次、窗口级成功时间与旧数据保留。

RefreshState 为纯状态（不引用 Tk）；UI 断言用 object.__new__ 注入颜色表。
"""

import queue
import sys
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from api import AccountUsage, UsageMetric  # noqa: E402
from floating_window import FloatingWindow  # noqa: E402
from main import TokenHubApp  # noqa: E402
from refresh_state import RefreshState, WindowView  # noqa: E402


def _metric(percent=None, error=None):
    return UsageMetric("ok" if error is None else "error", percent, None, error)


def _usage(key, rolling=None, weekly=None, monthly=None, account_error=None):
    return AccountUsage(
        key,
        rolling if rolling is not None else _metric(1, None),
        weekly if weekly is not None else _metric(2, None),
        monthly if monthly is not None else _metric(3, None),
        time.time(),
        account_error=account_error,
    )


def _errored_usage(key, error="网络错误", account_error=None):
    return _usage(key, _metric(None, error), _metric(None, error),
                  _metric(None, error), account_error=account_error)


class RefreshStateTests(unittest.TestCase):

    def test_all_failure_keeps_last_values_but_marks_stale(self):
        state = RefreshState()
        state.apply(state.revision, {"k": _usage("k")}, 100.0)
        self.assertTrue(state.apply(state.revision, {"k": _errored_usage("k")}, 200.0))
        for name in ("rolling", "weekly", "monthly"):
            view = state.window("k", name)
            self.assertIsNotNone(view.metric)
            self.assertEqual(view.metric.percent, {"rolling": 1.0, "weekly": 2.0,
                                                   "monthly": 3.0}[name])
            self.assertTrue(view.stale)
            self.assertEqual(view.error, "网络错误")
            self.assertEqual(view.last_attempt_at, 200.0)
            self.assertEqual(view.last_success_at, 100.0)
        account = state.account("k")
        self.assertFalse(account.latest_ok)
        self.assertTrue(account.has_data)
        self.assertTrue(account.stale)
        self.assertEqual(account.last_success_at, 100.0)

    def test_partial_window_failure_does_not_touch_other_windows(self):
        state = RefreshState()
        state.apply(state.revision, {"k": _usage("k")}, 100.0)
        partial = _usage(
            "k", _metric(None, "网络错误"), _metric(50), _metric(70))
        self.assertTrue(state.apply(state.revision, {"k": partial}, 200.0))
        rolling = state.window("k", "rolling")
        self.assertTrue(rolling.stale)
        self.assertEqual(rolling.metric.percent, 1.0)
        self.assertEqual(rolling.error, "网络错误")
        weekly = state.window("k", "weekly")
        self.assertFalse(weekly.stale)
        self.assertIsNone(weekly.error)
        self.assertEqual(weekly.metric.percent, 50.0)
        self.assertEqual(weekly.last_success_at, 200.0)
        account = state.account("k")
        self.assertTrue(account.latest_ok)  # 部分窗口失败不改写账号级成功
        self.assertEqual(account.last_success_at, 200.0)

    def test_account_level_failure_marks_all_windows_stale(self):
        state = RefreshState()
        state.apply(state.revision, {"k": _usage("k")}, 100.0)
        failed = _errored_usage("k", "密钥无效（401）", account_error="密钥无效（401）")
        self.assertTrue(state.apply(state.revision, {"k": failed}, 200.0))
        for name in ("rolling", "weekly", "monthly"):
            self.assertTrue(state.window("k", name).stale)
            self.assertEqual(state.window("k", name).error, "密钥无效（401）")
        account = state.account("k")
        self.assertFalse(account.latest_ok)
        self.assertEqual(account.error, "密钥无效（401）")

    def test_recovery_clears_error_and_stale(self):
        state = RefreshState()
        state.apply(state.revision, {"k": _usage("k")}, 100.0)
        state.apply(state.revision, {"k": _errored_usage("k")}, 200.0)
        self.assertTrue(state.apply(state.revision, {"k": _usage("k")}, 300.0))
        for name in ("rolling", "weekly", "monthly"):
            view = state.window("k", name)
            self.assertFalse(view.stale)
            self.assertIsNone(view.error)
            self.assertEqual(view.last_attempt_at, 300.0)
            self.assertEqual(view.last_success_at, 300.0)
        self.assertTrue(state.account("k").latest_ok)

    def test_no_old_value_is_unavailable(self):
        state = RefreshState()
        self.assertTrue(state.apply(state.revision, {"k": _errored_usage("k")}, 100.0))
        view = state.window("k", "rolling")
        self.assertIsNone(view.metric)
        self.assertFalse(view.stale)
        self.assertEqual(view.error, "网络错误")
        account = state.account("k")
        self.assertFalse(account.has_data)
        self.assertFalse(account.latest_ok)
        self.assertIsNone(account.last_success_at)

    def test_zero_percent_is_a_valid_reading(self):
        state = RefreshState()
        self.assertTrue(state.apply(
            state.revision,
            {"k": _usage("k", _metric(0), _metric(0), _metric(0))}, 100.0))
        self.assertFalse(state.window("k", "rolling").stale)
        self.assertTrue(state.account("k").latest_ok)

    def test_old_revision_rejected_after_invalidate(self):
        state = RefreshState()
        state.apply(0, {"k": _usage("k")}, 100.0)
        revision = state.invalidate()
        self.assertEqual(revision, 1)
        self.assertFalse(state.apply(0, {"k": _usage("k")}, 200.0))
        self.assertEqual(state.window("k", "rolling").last_attempt_at, 100.0)
        self.assertTrue(state.apply(1, {"k": _usage("k")}, 300.0))
        self.assertEqual(state.window("k", "rolling").last_attempt_at, 300.0)

    def test_deleted_account_old_batch_not_applied(self):
        state = RefreshState()
        state.apply(0, {"k1": _usage("k1"), "k2": _usage("k2")}, 100.0)
        state.invalidate()  # 删除 k1 时使在途批次失效
        self.assertFalse(state.apply(0, {"k1": _usage("k1"), "k2": _usage("k2")}, 200.0))
        self.assertIsNotNone(state.account("k1"))
        self.assertTrue(state.apply(1, {"k2": _usage("k2")}, 300.0))
        self.assertIsNone(state.account("k1"))  # 新批次不再包含已删除账号
        self.assertEqual(state.account("k2").last_attempt_at, 300.0)


class RefreshUiTests(unittest.TestCase):

    def _window(self):
        win = object.__new__(FloatingWindow)
        win._views = {}
        win.c = {"warn": "warn", "attention": "attention",
                 "fg": "fg", "offline": "offline"}
        return win

    def test_stale_display_shows_last_time_and_reason(self):
        win = self._window()
        view = WindowView(_metric(59.6), 200.0, 100.0, "网络错误", True)
        display = win._metric_display(_usage("k"), "rolling", False, view=view)
        percent, pct_text, cd_text, icon, err_state, stale = display
        self.assertEqual(percent, 59.6)
        self.assertEqual(pct_text, "60%")
        self.assertTrue(cd_text.startswith("上次 "))
        self.assertIn("连接失败", cd_text)
        self.assertTrue(stale)
        self.assertFalse(icon)
        self.assertEqual(win._metric_fg(percent, err_state, stale), "offline")

    def test_no_old_value_shows_unavailable(self):
        win = self._window()
        view = WindowView(None, 200.0, None, "网络错误", False)
        percent, pct_text, cd_text, _icon, _err, _stale = win._metric_display(
            _usage("k"), "rolling", False, view=view)
        self.assertIsNone(percent)
        self.assertEqual(pct_text, "不可用")
        self.assertIn("连接失败", cd_text)

    def test_normal_display_uses_normal_color(self):
        win = self._window()
        view = WindowView(_metric(59.6), 200.0, 200.0, None, False)
        percent, pct_text, _cd, _icon, err, stale = win._metric_display(
            _usage("k"), "rolling", False, view=view)
        self.assertEqual((percent, pct_text, err, stale), (59.6, "60%", False, False))
        self.assertEqual(win._metric_fg(percent, err, stale), "fg")

    def test_stale_countdown_never_reports_due(self):
        win = object.__new__(FloatingWindow)
        win._cd_due = {}
        win._views = {}
        blk = {
            "key": "k", "metric": "rolling", "offline": False,
            "stale": True,
            "usage": _usage("k", _metric(10), _metric(10), _metric(10)),
        }
        blk["usage"].rolling.resets_at = "2000-01-01T00:00:00+00:00"
        self.assertFalse(win._countdown_due(blk))

    def test_account_summary_exposes_stale_but_no_fake_success(self):
        state = RefreshState()
        state.apply(state.revision, {"k": _usage("k")}, 100.0)
        state.apply(state.revision, {"k": _errored_usage("k")}, 200.0)
        account = state.account("k")
        self.assertEqual(account.last_attempt_at, 200.0)
        self.assertEqual(account.last_success_at, 100.0)
        self.assertTrue(account.stale)


class MainRefreshTests(unittest.TestCase):

    def _app(self, accounts=()):
        app = object.__new__(TokenHubApp)
        app.refresh_state = RefreshState()
        app._result_queue = queue.Queue()
        app._refresh_event = threading.Event()
        app._last_attempt = 0.0
        app._refresh_failed = False
        app._current_key = accounts[0].key if accounts else None
        app._sticky_reason = None
        app._sticky_auto_text = None
        app._switch_pending_restart = False
        app._auth_external_changed = False
        app._switching_inflight = False
        app._last_auth_check = 0.0
        app.cfg = mock.Mock()
        app.cfg.accounts = list(accounts)
        app.cfg.find_by_key.side_effect = lambda key: next(
            (a for a in app.cfg.accounts if a.key == key), None)
        app.win = mock.Mock()
        return app

    def test_poll_applies_revision_and_updates_attempt_time(self):
        app = self._app()
        app.refresh_state.apply(0, {"k": _usage("k")}, 50.0)
        app._result_queue.put(("usage", (0, {"k": _usage("k")}, 123.0)))
        app._poll_results()
        self.assertEqual(app._last_attempt, 123.0)
        self.assertFalse(app._refresh_failed)
        app.win.update_accounts.assert_called_once()

    def test_stale_batch_not_applied(self):
        app = self._app()
        app.refresh_state.invalidate()
        app._result_queue.put(("usage", (0, {"k": _usage("k")}, 123.0)))
        app._poll_results()
        self.assertEqual(app._last_attempt, 0.0)
        self.assertIsNone(app.refresh_state.account("k"))
        app.win.update_accounts.assert_not_called()

    def test_refresh_captures_revision_before_keys(self):
        """R4：捕获 revision 后、生成 keys 前被 invalidate，旧批次必须被拒绝。"""
        app = self._app()
        app.api = mock.Mock()
        app.api.fetch_all.return_value = {"k": _usage("k")}

        def invalidate_then_keys():
            app.refresh_state.invalidate()
            return ["k"]

        app._all_keys = mock.Mock(side_effect=invalidate_then_keys)
        app._refresh_once()
        _kind, payload = app._result_queue.get_nowait()
        revision, usages, attempted_at = payload
        self.assertEqual(revision, 0)  # 旧代，而不是 invalidate 后的新代
        self.assertFalse(app.refresh_state.apply(revision, usages, attempted_at))

        # 下一次刷新捕获新代，可正常接纳
        app._all_keys = mock.Mock(return_value=["k"])
        app._refresh_once()
        _kind, payload = app._result_queue.get_nowait()
        revision, usages, attempted_at = payload
        self.assertEqual(revision, app.refresh_state.revision)
        self.assertTrue(app.refresh_state.apply(revision, usages, attempted_at))

    def test_empty_refresh_uses_revision_captured_before_keys(self):
        app = self._app()

        def invalidate_then_empty():
            app.refresh_state.invalidate()
            return []

        app._all_keys = mock.Mock(side_effect=invalidate_then_empty)
        app._refresh_once()
        _kind, payload = app._result_queue.get_nowait()
        revision, usages, attempted_at = payload
        self.assertEqual(revision, 0)
        self.assertEqual(usages, {})
        self.assertFalse(app.refresh_state.apply(revision, usages, attempted_at))

    def test_poll_auth_retries_same_mtime_after_read_error(self):
        """R1：读取失败后不得因 mtime 未变而永久短路；5s 后重试并恢复。"""
        app = self._app()
        app.store = mock.Mock()
        app.store.mtime.return_value = 123.0
        values = [None, "key-a"]

        def read_key():
            key = values.pop(0)
            app.store.last_error = (
                "认证文件读取失败（denied）" if key is None else None)
            return key

        app.store.current_key.side_effect = read_key
        app._last_mtime = 122.0
        app._last_auth_check = 0.0
        app._poll_auth()
        self.assertEqual(app._auth_read_error, "认证文件读取失败（denied）")
        self.assertEqual(app._last_mtime, 122.0)  # 失败不推进 mtime

        app._last_auth_check = 0.0  # 已过 AUTH_POLL_SECONDS，mtime 仍为 123
        app._poll_auth()
        self.assertEqual(app._current_key, "key-a")
        self.assertIsNone(app._auth_read_error)
        self.assertEqual(app._last_mtime, 123.0)
        self.assertIsNone(app._sticky_reason)

    def test_all_accounts_failed_sets_global_failure(self):
        app = self._app()
        app.refresh_state.invalidate()
        app._result_queue.put(("usage", (1, {"k": _errored_usage("k")}, 123.0)))
        app._poll_results()
        self.assertTrue(app._refresh_failed)

    def test_partial_failure_is_not_global_failure(self):
        app = self._app()
        app.refresh_state.invalidate()
        partial = _usage("k", _metric(None, "网络错误"), _metric(50), _metric(70))
        app._result_queue.put(("usage", (1, {"k": partial}, 123.0)))
        app._poll_results()
        self.assertFalse(app._refresh_failed)

    def test_no_accounts_is_not_refresh_failure(self):
        app = self._app()
        app._result_queue.put(("usage", (0, {}, 123.0)))
        app._poll_results()
        self.assertFalse(app._refresh_failed)
        self.assertEqual(app.refresh_state.views, {})
        self.assertEqual(app._identity_state_text(), "未配置")

    def test_empty_refresh_clears_sticky_failure(self):
        """删光账号后无 keys 也必须投递空结果，清除旧的刷新失败 sticky。"""
        app = self._app()
        app._refresh_failed = True
        app._sticky_reason = "auto"
        app._sticky_auto_text = "刷新失败，显示上次数据 · 点击重试"
        app._refresh_once()
        kind, payload = app._result_queue.get_nowait()
        self.assertEqual(kind, "usage")
        _revision, usages, _attempted = payload
        self.assertEqual(usages, {})
        app._result_queue.put((kind, payload))
        app._poll_results()
        self.assertFalse(app._refresh_failed)
        self.assertIsNone(app._sticky_reason)
        app.win.clear_transient.assert_called_once()
        self.assertEqual(app.refresh_state.views, {})
        self.assertEqual(app._identity_state_text(), "未配置")

    def test_delete_last_account_clears_failure_immediately(self):
        account = types.SimpleNamespace(key="old", label="旧账号")
        app = self._app()
        app.cfg.accounts = [account]
        app._refresh_failed = True
        app._sticky_reason = "auto"
        app._sticky_auto_text = "刷新失败，显示上次数据 · 点击重试"

        def remove(_key):
            app.cfg.accounts = []
            return True

        app.cfg.remove_account.side_effect = remove
        app._current_key = None
        result = app.on_delete_account("old")
        self.assertEqual(result, (True, ""))
        self.assertFalse(app._refresh_failed)
        self.assertIsNone(app._sticky_reason)
        self.assertTrue(app._refresh_event.is_set())

    def test_auth_read_error_sets_sticky_and_is_not_unconfigured(self):
        app = self._app()
        app.store = mock.Mock()
        app.store.current_key.return_value = None
        app.store.last_error = "auth.json 读取失败（PermissionError）"
        app._reload_current_state()
        self.assertEqual(app._identity_state_text(), "认证文件读取失败")
        self.assertEqual(app._sticky_reason, "auth")
        args, kwargs = app.win.set_toast.call_args
        self.assertIn("认证文件读取失败", args[0])
        self.assertTrue(kwargs.get("sticky"))
        app.cfg.ensure_current_in_pool.assert_not_called()

    def test_auth_read_error_clears_after_successful_read(self):
        account = types.SimpleNamespace(key="k", label="账号")
        app = self._app(accounts=(account,))
        app.store = mock.Mock()
        app.store.current_key.return_value = None
        app.store.last_error = "auth.json 读取失败（denied）"
        app._reload_current_state()
        self.assertEqual(app._sticky_reason, "auth")
        app.store.current_key.return_value = "k"
        app.store.last_error = None
        app._reload_current_state()
        self.assertIsNone(app._sticky_reason)
        app.win.clear_transient.assert_called_once()
        self.assertEqual(app._identity_state_text(), "已配置")

    def test_auth_read_error_does_not_replace_config_sticky(self):
        app = self._app()
        text = "配置无法读取，已保护原文件：C:/x"
        app._set_sticky("config", text, "error")
        app.store = mock.Mock()
        app.store.current_key.return_value = None
        app.store.last_error = "认证文件读取失败（denied）"
        app._reload_current_state()
        self.assertEqual(app._sticky_reason, "config")
        app.win.set_toast.assert_called_once_with(text, "error", sticky=True)
        self.assertEqual(app._identity_state_text(), "认证文件读取失败")

    def test_auth_read_error_does_not_replace_switch_sticky(self):
        app = self._app()
        text = "恢复失败，状态待确认；备份位于 C:/x"
        app._set_sticky("switch", text, "error")
        app.store = mock.Mock()
        app.store.current_key.return_value = None
        app.store.last_error = "认证文件读取失败（denied）"
        app._reload_current_state()
        self.assertEqual(app._sticky_reason, "switch")
        app.win.set_toast.assert_called_once_with(text, "error", sticky=True)

    def test_auth_recovery_does_not_clear_preserved_config_sticky(self):
        account = types.SimpleNamespace(key="k", label="账号")
        app = self._app(accounts=(account,))
        text = "配置无法读取，已保护原文件：C:/x"
        app._set_sticky("config", text, "error")
        app.store = mock.Mock()
        app.store.current_key.return_value = None
        app.store.last_error = "认证文件读取失败（denied）"
        app._reload_current_state()
        app.store.current_key.return_value = "k"
        app.store.last_error = None
        app._reload_current_state()
        self.assertEqual(app._sticky_reason, "config")
        app.win.clear_transient.assert_not_called()
        self.assertEqual(app._identity_state_text(), "已配置")

    def test_settings_current_key_helper_raises_on_auth_error(self):
        app = self._app()
        app.store = mock.Mock()
        app.store.current_key.return_value = None
        app.store.last_error = "denied"
        with self.assertRaises(OSError):
            app._settings_current_key()

    def test_poll_auth_surfaces_read_error(self):
        app = self._app()
        app.store = mock.Mock()
        app.store.mtime.return_value = 2.0
        app.store.current_key.return_value = None
        app.store.last_error = "认证文件读取失败（denied）"
        app._last_mtime = 1.0
        app._last_auth_check = 0.0
        app._poll_auth()
        self.assertEqual(app._sticky_reason, "auth")
        self.assertEqual(app._identity_state_text(), "认证文件读取失败")

    def test_update_ui_accounts_passes_views_and_keeps_old_backup(self):
        old = types.SimpleNamespace(key="old", label="旧账号")
        new = types.SimpleNamespace(key="new", label="新账号")
        app = self._app(accounts=(new, old))
        app._current_key = "new"
        app.refresh_state.apply(0, {"old": _usage("old"), "new": _usage("new")}, 100.0)
        app._update_ui_accounts()
        args, kwargs = app.win.update_accounts.call_args
        self.assertIs(args[0], new)
        self.assertEqual([a.key for a, _u in args[2]], ["old"])
        old_usage = args[2][0][1]
        self.assertEqual(old_usage.rolling.percent, 1.0)
        self.assertIn("old", kwargs["views"])


class SharedCountdownTests(unittest.TestCase):

    def test_floating_countdown_uses_shared_core(self):
        with mock.patch("floating_window.remaining_seconds", return_value=3600) as core:
            self.assertEqual(FloatingWindow._format_countdown("ignored"), "1H0M")
            core.assert_called_once()

    def test_utc_expired_and_invalid(self):
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            FloatingWindow._format_countdown("2026-01-01T04:59:00Z", now), "4H59M")
        self.assertEqual(
            FloatingWindow._format_countdown("2025-12-31T23:59:00Z", now), "即将重置")
        self.assertEqual(FloatingWindow._format_countdown("not-a-time", now), "--")
        self.assertEqual(FloatingWindow._format_countdown("", now), "--")

    def test_naive_time_treated_as_utc(self):
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            FloatingWindow._format_countdown("2026-01-01 04:59:00", now), "4H59M")


if __name__ == "__main__":
    unittest.main()
