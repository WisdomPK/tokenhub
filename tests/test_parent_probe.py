# -*- coding: utf-8 -*-
"""TH-01 回归测试：父进程存活探测必须：
1) 反复探测不会终止目标进程（Windows 上 os.kill 的陷阱）；
2) 目标进程正常退出后能识别为已退出；
3) 源码中不得再出现 os.kill。
"""
import inspect
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import main as main_mod  # noqa: E402


class ParentProbeTest(unittest.TestCase):
    def test_live_process_not_killed_by_probe(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            time.sleep(0.8)
            for _ in range(5):
                self.assertTrue(
                    main_mod.TokenHubApp._parent_alive(proc.pid),
                    "存活进程应被识别为存活")
                time.sleep(0.1)
            self.assertIsNone(
                proc.poll(), "连续探测不得终止目标进程（TH-01）")
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_dead_process_detected(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=10)
        time.sleep(0.3)
        self.assertFalse(
            main_mod.TokenHubApp._parent_alive(proc.pid),
            "已退出进程应被识别为不存在")

    def test_never_uses_os_kill(self):
        import ast
        import textwrap

        src = textwrap.dedent(inspect.getsource(main_mod.TokenHubApp._parent_alive))
        func = ast.parse(src).body[0]
        # 去掉 docstring 后再检查函数体（文档里"严禁 os.kill"的警告不构成调用）
        if (func.body and isinstance(func.body[0], ast.Expr)
                and isinstance(func.body[0].value, ast.Constant)):
            func.body = func.body[1:]
        body = ast.unparse(func)
        self.assertNotIn("os.kill", body, "TH-01：禁止 os.kill 存活探测")


if __name__ == "__main__":
    unittest.main()
