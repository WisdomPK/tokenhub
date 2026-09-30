# -*- coding: utf-8 -*-
"""TokenHub v3 构建脚本：PyInstaller onefile 打包。

等价命令：
    PY -m PyInstaller --onefile --noconsole --name TokenHub \
        --clean --distpath dist --workpath build src\\main.py

用法（项目根）：PY build.py
产物：dist\\TokenHub.exe
"""

import os
import shutil
import subprocess
import sys


def clean():
    """清理上次构建产物（build/ 与 dist/）。"""
    for directory in ("build", "dist"):
        if os.path.exists(directory):
            shutil.rmtree(directory)
            print(f"已清理 {directory}/")


def build():
    """执行 PyInstaller onefile 构建。"""
    clean()
    command = [
        sys.executable,
        "-m", "PyInstaller",
        "--onefile",
        "--noconsole",
        "--name", "TokenHub",
        "--clean",
        "--distpath", "dist",
        "--workpath", "build",
        os.path.join("src", "main.py"),
    ]
    print("执行:", " ".join(command))
    subprocess.run(command, check=True)
    exe = os.path.join("dist", "TokenHub.exe")
    if os.path.exists(exe):
        size_mb = os.path.getsize(exe) / (1024 * 1024)
        print(f"构建完成: {exe} ({size_mb:.1f} MB)")
    else:
        print("构建完成，但未找到 dist/TokenHub.exe")
        sys.exit(1)


if __name__ == "__main__":
    build()