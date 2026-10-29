# -*- coding: utf-8 -*-
"""W7（F8）：桌面切换服务与“已配置/待重启”语义。"""

import queue
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from account_service import AccountService, SwitchResult  # noqa: E402
from api import AccountUsage, UsageMetric  # noqa: E402
from floating_window import FloatingWindow  # noqa: E402
from main import TokenHubApp  # noqa: E402
from refresh_state import RefreshState  # noqa: E402

TIMEOUT_MSG = "控制接口超时，切换结果待确认"


class FakeCtrl:
    def __init__(self, discover=True, healthy=True,
                 result=(True, "切换请求已成功", "ok")):
        self.info = object() if discover else None
        self.healthy = healthy
        self.result = result
        self.switch_calls = []

    def discover(self):
        return self.info

    def health(self, info):
        return self.healthy

    def switch(self, info, key):
        self.switch_calls.append((info, key))
        return self.result


class FakeStore:
    def __init__(self, current="key-a",
                 switch_result=(True, "已写入认证文件", "ok"),
                 current_error=None):
        self.current = current
        self.switch_result = switch_result
        self.current_error = current_error
        self.switch_calls = []

    def current_key(self):
        if self.current_error is not None:
            raise self.current_error
        return self.current

    def switch_to(self, key):
        self.switch_calls.append(key)
        self.current = key
        return self.switch_result


class AccountServiceTests(unittest.TestCase):

    def test_timeout_but_file_already_written_is_confirmed(self):
        store = FakeStore(current="key-b")
        ctrl = FakeCtrl(result=(False, TIMEOUT_MSG, "timeout_uncertain"))
        result = AccountService(store, ctrl).switch("key-b")
        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.method, "api")
        self.assertIn("已核对", result.message)
        self.assertEqual(store.switch_calls, [])  # 不盲目再写

    def test_timeout_unconfirmed_does_not_write_again(self):
        store = FakeStore(current="key-a")
        ctrl = FakeCtrl(result=(False, TIMEOUT_MSG, "timeout_uncertain"))
        result = AccountService(store, ctrl).switch("key-b")
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, "timeout_uncertain")
        self.assertEqual(result.method, "api")
        self.assertEqual(result.message, TIMEOUT_MSG)
        self.assertEqual(store.switch_calls, [])

    def test_timeout_with_unreadable_file_is_uncertain(self):
        store = FakeStore(current_error=OSError("read failed"))
        ctrl = FakeCtrl(result=(False, TIMEOUT_MSG, "timeout_uncertain"))
        result = AccountService(store, ctrl).switch("key-b")
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, "timeout_uncertain")

    def test_restore_failed_with_target_residue_never_upgrades(self):
        store = FakeStore(current="key-b")  # 目标 key 残留恰是失败结果
        ctrl = FakeCtrl(result=(False, "恢复失败，状态待确认", "restore_failed"))
        result = AccountService(store, ctrl).switch("key-b")
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, "restore_failed")
        self.assertNotIn("已核对", result.message)

    def test_denied_reports_structured_outcome(self):
        ctrl = FakeCtrl(result=(False, "控制接口拒绝（401）", "denied"))
        result = AccountService(FakeStore(), ctrl).switch("key-b")
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, "denied")
        self.assertEqual(result.method, "api")
        self.assertEqual(result.message, "控制接口拒绝（401）")

    def test_file_fallback_when_control_unavailable(self):
        store = FakeStore(current="key-a")
        ctrl = FakeCtrl(discover=False)
        result = AccountService(store, ctrl).switch("key-b")
        self.assertTrue(result.ok)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.method, "file")
        self.assertEqual(store.switch_calls, ["key-b"])

    def test_unhealthy_control_falls_back_to_file(self):
        store = FakeStore()
        result = AccountService(store, FakeCtrl(healthy=False)).switch("key-b")
        self.assertTrue(result.ok)
        self.assertEqual(result.method, "file")
        self.assertEqual(store.switch_calls, ["key-b"])

    def test_unexpected_store_error_is_not_silent(self):
        store = FakeStore()
        store.switch_to = mock.Mock(side_effect=OSError("磁盘只读"))
        result = AccountService(store, FakeCtrl(discover=False)).switch("key-b")
        self.assertFalse(result.ok)
        self.assertIn("磁盘只读", result.message)
        self.assertIn(result.outcome,
                      {"ok", "unaffected", "restored", "restore_failed",
                       "denied", "unreachable", "timeout_uncertain"})

    def test_switch_result_fields(self):
        result = SwitchResult(True, "ok", "file", "已写入认证文件")
        self.assertEqual((result.ok, result.outcome, result.method),
                         (True, "ok", "file"))


def _app():
    app = object.__new__(TokenHubApp)
    app.cfg = mock.Mock()
    app.cfg.load_error = None
    app.cfg.accounts = []
    app.cfg.find_by_key.return_value = None
    app.store = mock.Mock()
    app.store.current_key.return_value = "key-b"
    app.store.mtime.return_value = 10.0
    app.win = mock.Mock()
    app.refresh_state = RefreshState()
    app._refresh_event = threading.Event()
    app._result_queue = queue.Queue()
    app._switching_inflight = False
    app._switch_gen = 0
    app._current_key = "key-a"
    app._last_fetch = time.time()
    app._last_mtime = 1.0
    app._last_auth_check = 0.0
    app._sticky_reason = None
    app._sticky_auto_text = None
    app._switch_pending_restart = False
    app._auth_external_changed = False
    return app


class IdentityStateTests(unittest.TestCase):

    def test_default_state_is_configured(self):
        app = _app()
        self.assertEqual(app._identity_state_text(), "已配置")

    def test_success_switch_marks_pending_restart_and_persists(self):
        app = _app()
        app._switching_inflight = True
        app._switch_gen = 1
        result = SwitchResult(True, "ok", "api", "切换请求已成功")
        app._handle_switch_result(1, "key-b", "备用", result)
        self.assertTrue(app._switch_pending_restart)
        self.assertEqual(app._identity_state_text(), "已配置，待重启")
        app.win.set_toast.assert_called_once_with(
            "已切换至「备用」：切换请求已成功", "success",
            follow_up=("请重启 opencode", 3000))
        app.win.clear_transient()
        self.assertEqual(app._identity_state_text(), "已配置，待重启")
        self.assertFalse(hasattr(app, "effective_key"))

    def test_external_auth_change_marks_runtime_unconfirmed(self):
        app = _app()
        app._switch_pending_restart = True
        app._poll_auth()
        self.assertTrue(app._auth_external_changed)
        self.assertFalse(app._switch_pending_restart)
        self.assertEqual(app._identity_state_text(), "配置已变更，运行时账号未确认")
        self.assertEqual(app._current_key, "key-b")

    def test_success_switch_clears_external_change_flag(self):
        app = _app()
        app._auth_external_changed = True
        app._switching_inflight = True
        app._switch_gen = 1
        app._handle_switch_result(
            1, "key-b", "备用", SwitchResult(True, "ok", "file", "认证文件已更新"))
        self.assertFalse(app._auth_external_changed)
        self.assertTrue(app._switch_pending_restart)

    def test_timeout_other_current_keeps_sticky_error(self):
        app = _app()
        app.store.current_key.return_value = "key-a"
        app.cfg.find_by_key.return_value = mock.Mock(label="主账号")
        app._switching_inflight = True
        app._switch_gen = 1
        app._handle_switch_result(
            1, "key-b", "备用",
            SwitchResult(False, "timeout_uncertain", "api", TIMEOUT_MSG))
        args, kwargs = app.win.set_toast.call_args
        text, kind = args
        self.assertEqual(kind, "error")
        self.assertIn("已核对：当前账号仍为「主账号」", text)
        self.assertTrue(kwargs.get("sticky"))
        self.assertEqual(app._sticky_reason, "switch")

    def test_restore_failed_residue_keeps_sticky_error(self):
        app = _app()
        app._switching_inflight = True
        app._switch_gen = 1
        app._handle_switch_result(
            1, "key-b", "备用",
            SwitchResult(False, "restore_failed", "file",
                         "恢复失败，状态待确认；备份位于 <path>"))
        args, kwargs = app.win.set_toast.call_args
        text, kind = args
        self.assertEqual(kind, "error")
        self.assertNotIn("成功", text)
        self.assertTrue(kwargs.get("sticky"))

    def test_on_switch_uses_service_in_background_thread(self):
        app = _app()
        app.account_service = mock.Mock()
        app.account_service.switch.return_value = SwitchResult(
            True, "ok", "file", "已写入认证文件")
        app._switching_inflight = False
        main_thread = threading.get_ident()
        seen = []

        def switch(key):
            seen.append(threading.get_ident())
            return SwitchResult(True, "ok", "file", "已写入认证文件")

        app.account_service.switch.side_effect = switch
        app.on_switch("key-b")
        kind, payload = app._result_queue.get(timeout=5)
        self.assertEqual(kind, "switch_result")
        gen, key, label, result = payload
        self.assertEqual((gen, key, result), (1, "key-b", app.account_service.switch.return_value))
        self.assertTrue(seen)
        self.assertNotEqual(seen[0], main_thread)

    def test_floating_window_stores_identity_state(self):
        win = object.__new__(FloatingWindow)
        win.set_identity_state("已配置，待重启")
        self.assertEqual(win._identity_state, "已配置，待重启")

    def test_original_account_remains_visible_after_switch(self):
        old = types.SimpleNamespace(key="key-a", label="原账号")
        new = types.SimpleNamespace(key="key-b", label="新账号")
        app = _app()
        app.cfg.accounts = [old, new]
        app.cfg.find_by_key.side_effect = lambda key: {
            "key-a": old, "key-b": new}.get(key)
        app._current_key = "key-b"
        usage = AccountUsage(
            "key-a", UsageMetric("ok", 12.5, None),
            UsageMetric("ok", 20, None), UsageMetric("ok", 30, None), 100.0)
        app.refresh_state.apply(
            app.refresh_state.revision,
            {"key-a": usage, "key-b": usage}, 100.0)
        app._update_ui_accounts()
        args, kwargs = app.win.update_accounts.call_args
        self.assertIs(args[0], new)
        self.assertEqual([a.key for a, _u in args[2]], ["key-a"])
        self.assertEqual(args[2][0][1].rolling.percent, 12.5)
        self.assertIn("key-a", kwargs["views"])


if __name__ == "__main__":
    unittest.main()
