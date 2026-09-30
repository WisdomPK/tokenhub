# -*- coding: utf-8 -*-
"""TH-02 隔离验证：WNDCLASSW 的 lpfnWndProc 回调赋值不再抛
"expected WinFunctionType instance, got method"。

修复前：_run_tray_loop 把 Python 绑定方法直接赋给 WNDPROC_ 字段 → TypeError，
托盘线程中断。修复后：用 WNDPROC_ 包装并持有（self._wndproc_cb）。

本测试不创建真实托盘窗口（纯类型/赋值验证，可离线运行）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from tray import IDI_APPLICATION, WNDCLASSW, WNDPROC_, user32  # noqa: E402


class TrayInitTest(unittest.TestCase):
    def _proc(self, hwnd, msg, wparam, lparam):
        return 0

    def test_load_icon_accepts_integer_resource_id(self):
        """回归：LoadIconW 第二参数为 MAKEINTRESOURCE 整数资源 ID（32512）。
        argtypes 不得声明为 LPCWSTR —— 否则冻结 exe 中托盘初始化会报
        "argument 2: TypeError: 'int' object cannot be interpreted as
        ctypes.c_wchar_p"（2026-09-29 实机复现）。"""
        handle = user32.LoadIconW(0, IDI_APPLICATION)
        self.assertTrue(handle, "LoadIconW(0, IDI_APPLICATION) 应返回非空 HICON")

    def test_wrapped_callback_assignment_ok(self):
        """修复后：WNDPROC_ 包装实例可赋给 lpfnWndProc（ctypes 会再包装，
        断言可调用与返回值，不比较实例同一性）。"""
        wnd_class = WNDCLASSW()
        cb = WNDPROC_(self._proc)
        wnd_class.lpfnWndProc = cb  # 修复点：不再抛类型错误
        wnd_class.lpszClassName = 'TokenHubTrayV3'
        self.assertIsNotNone(wnd_class.lpfnWndProc)
        self.assertEqual(wnd_class.lpfnWndProc(0, 0, 0, 0), 0)

    def test_raw_bound_method_assignment_fails(self):
        """旧写法（绑定方法直赋）确实抛 TypeError——证明包装的必要性。"""
        wnd_class = WNDCLASSW()
        with self.assertRaises(TypeError):
            wnd_class.lpfnWndProc = self._proc

    def test_wrapped_callback_invocation_returns_value(self):
        """包装回调可经 ctypes 调用且返回值不截断。"""
        cb = WNDPROC_(self._proc)
        result = cb(0, 0, 0, 0)
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()