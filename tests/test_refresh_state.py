# -*- coding: utf-8 -*-
"""W6（F7）：刷新代次、窗口级成功时间与旧数据保留。

RefreshState 为纯状态（不引用 Tk）；UI 断言用 object.__new__ 注入颜色表。
"""

import queue
import sys
import time
import types
import unittest
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
        app._last_fetch = 0.0
        app._refresh_failed = False
        app._current_key = accounts[0].key if accounts else None
        app._sticky_reason = None
        app._sticky_auto_text = None
        app._switch_pending_restart = False
        app._auth_external_changed = False
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
        self.assertEqual(app._last_fetch, 123.0)
        self.assertFalse(app._refresh_failed)
        app.win.update_accounts.assert_called_once()

    def test_stale_batch_not_applied(self):
        app = self._app()
        app.refresh_state.invalidate()
        app._result_queue.put(("usage", (0, {"k": _usage("k")}, 123.0)))
        app._poll_results()
        self.assertEqual(app._last_fetch, 0.0)
        self.assertIsNone(app.refresh_state.account("k"))
        app.win.update_accounts.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
