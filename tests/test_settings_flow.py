# -*- coding: utf-8 -*-
"""W1（F1）：设置窗口保存通知与主界面配置刷新行为。

不创建真实 Tk 窗口：用 object.__new__ 注入控件/回调桩，只验证编排逻辑。
"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from main import TokenHubApp  # noqa: E402
from settings_window import SettingsWindow  # noqa: E402


def _settings_window(on_save=None):
    sw = object.__new__(SettingsWindow)
    sw.on_save = on_save
    sw.config = mock.Mock()
    sw.config.accounts = [mock.Mock(key="key-b", label="备用")]
    sw.config.mask_key.side_effect = lambda k: k
    sw.colors = {"ok": "#00ff00", "warn": "#ff0000"}
    sw.window = mock.Mock()
    sw.window.winfo_exists.return_value = True
    sw._feedback_job = None
    sw._inline_feedback = mock.Mock()
    sw._refresh_accounts = mock.Mock()
    sw._add_btn = mock.Mock()
    sw._key_entry = mock.Mock()
    sw._label_entry = mock.Mock()
    sw.get_current_key = lambda: "key-a"
    return sw


class NotifySavedTests(unittest.TestCase):

    def test_notify_saved_calls_callback_once(self):
        calls = []
        sw = _settings_window(on_save=lambda: calls.append(1))
        sw._notify_saved()
        self.assertEqual(calls, [1])

    def test_notify_saved_without_callback(self):
        sw = _settings_window(on_save=None)
        sw._notify_saved()  # 无回调正常返回，不递归


class AccountFlowTests(unittest.TestCase):

    def test_add_success_notifies_once_updates_list_and_clears_input(self):
        calls = []
        sw = _settings_window(on_save=lambda: calls.append(1))
        sw.config.upsert_account.return_value = mock.Mock(label="账号A")
        sw._on_add_done("账号A", "sk-new-key-0001", mock.Mock(), "")
        self.assertEqual(calls, [1])
        sw._refresh_accounts.assert_called_once()
        sw._key_entry.delete.assert_called_once_with(0, "end")
        sw._label_entry.delete.assert_called_once_with(0, "end")

    def test_add_failure_keeps_input_and_does_not_notify(self):
        calls = []
        sw = _settings_window(on_save=lambda: calls.append(1))
        sw.config.upsert_account.side_effect = OSError("磁盘只读")
        sw._on_add_done("账号A", "sk-new-key-0001", mock.Mock(), "")
        self.assertEqual(calls, [])
        sw._key_entry.delete.assert_not_called()
        sw._label_entry.delete.assert_not_called()
        sw._inline_feedback.assert_called_once()
        self.assertEqual(sw._inline_feedback.call_args.kwargs.get("ok"), False)

    def test_delete_success_notifies_once_and_updates_list(self):
        calls = []
        sw = _settings_window(on_save=lambda: calls.append(1))
        sw.config.remove_account.return_value = True
        with mock.patch("settings_window.messagebox.askyesno", return_value=True):
            sw._delete_account("key-b")
        self.assertEqual(calls, [1])
        sw._refresh_accounts.assert_called_once()

    def test_delete_failure_does_not_notify(self):
        calls = []
        sw = _settings_window(on_save=lambda: calls.append(1))
        sw.config.remove_account.return_value = False
        with mock.patch("settings_window.messagebox.askyesno", return_value=True):
            sw._delete_account("key-b")
        self.assertEqual(calls, [])

    def test_rename_success_notifies_once_and_updates_list(self):
        calls = []
        sw = _settings_window(on_save=lambda: calls.append(1))
        sw.config.upsert_account.return_value = mock.Mock(label="新名")
        dialog = mock.Mock()
        self.assertTrue(sw._apply_rename("key-b", "新名", dialog))
        self.assertEqual(calls, [1])
        sw.config.upsert_account.assert_called_once_with("新名", "key-b")
        dialog.destroy.assert_called_once()
        sw._refresh_accounts.assert_called_once()

    def test_rename_failure_does_not_notify_or_close(self):
        calls = []
        sw = _settings_window(on_save=lambda: calls.append(1))
        sw.config.upsert_account.side_effect = OSError("磁盘只读")
        dialog = mock.Mock()
        self.assertFalse(sw._apply_rename("key-b", "新名", dialog))
        self.assertEqual(calls, [])
        dialog.destroy.assert_not_called()


class SaveOptionsTests(unittest.TestCase):

    def _with_option_widgets(self, sw):
        sw._interval_spin = mock.Mock()
        sw._interval_spin.get.return_value = "60"
        sw._height_spin = mock.Mock()
        sw._height_spin.get.return_value = "16"
        sw._ratio_entry = mock.Mock()
        sw._ratio_entry.get.return_value = "0.5"
        sw._offset_spin = mock.Mock()
        sw._offset_spin.get.return_value = "48"
        sw._fallback_var = mock.Mock()
        sw._fallback_var.get.return_value = True
        sw._theme_combo = mock.Mock()
        sw._theme_combo.get.return_value = "auto"
        return sw

    def test_save_options_notifies_once_and_closes_window(self):
        calls = []
        sw = self._with_option_widgets(_settings_window(on_save=lambda: calls.append(1)))
        sw._cancel = mock.Mock()
        sw._save()
        self.assertEqual(calls, [1])
        sw.config.apply_settings.assert_called_once()
        sw._cancel.assert_called_once()

    def test_save_options_failure_keeps_window_and_does_not_notify(self):
        calls = []
        sw = self._with_option_widgets(_settings_window(on_save=lambda: calls.append(1)))
        sw.config.apply_settings.side_effect = OSError("磁盘只读")
        sw._cancel = mock.Mock()
        sw._save()
        self.assertEqual(calls, [])
        sw._cancel.assert_not_called()
        sw._inline_feedback.assert_called_once()
        self.assertEqual(sw._inline_feedback.call_args.kwargs.get("ok"), False)


class ConfigSavedMainTests(unittest.TestCase):

    def test_on_config_saved_updates_ui_then_forces_refresh(self):
        app = object.__new__(TokenHubApp)
        app.cfg = mock.Mock()
        app.cfg.settings = {"theme": "dark"}
        app.theme = mock.Mock()
        app.theme.get_colors.return_value = {"bg": "#ffffff"}
        app.win = mock.Mock()
        app._last_fetch = time.time()
        app._update_ui_accounts = mock.Mock()
        app._force_refresh_now = mock.Mock()
        app._on_config_saved()
        app.cfg.load.assert_called_once()
        app._update_ui_accounts.assert_called_once()
        app.theme.set_theme.assert_called_once_with("dark")
        app.win.apply_theme.assert_called_once_with({"bg": "#ffffff"})
        app._force_refresh_now.assert_called_once()


if __name__ == "__main__":
    unittest.main()
