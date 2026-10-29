# -*- coding: utf-8 -*-
"""TokenHub v3 构建脚本：PyInstaller onefile 打包。

等价命令：
    PY -m PyInstaller --onefile --noconsole --name TokenHub \
        --clean --distpath dist --workpath build src\\main.py

用法（项目根）：PY build.py
产物：dist\\TokenHub.exe
"""

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone


def _source_commit() -> str | None:
    """当前源码提交；非 git 仓库/命令不可用时返回 None，不阻断构建。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def _source_dirty() -> bool | None:
    """工作区是否有未提交变更；非 git 仓库/命令不可用时返回 None，不阻断构建。"""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(result.stdout.strip())
    except Exception:
        return None


def _pyinstaller_version() -> str | None:
    """PyInstaller 版本；元数据不可用时返回 None，不阻断构建。"""
    try:
        return importlib.metadata.version("pyinstaller")
    except Exception:
        return None


def _sha256(path: str) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def write_build_manifest(exe: str) -> None:
    """记录源码提交、工作区脏标记、Python/PyInstaller 版本与 EXE 摘要（构建成功后调用）。"""
    manifest = {
        "source_commit": _source_commit(),
        "source_dirty": _source_dirty(),
        "python": sys.version.split()[0],
        "pyinstaller": _pyinstaller_version(),
        "exe_sha256": _sha256(exe),
        "built_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    path = os.path.join("dist", "build-manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"构建清单: {path}")


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
        write_build_manifest(exe)
    else:
        print("构建完成，但未找到 dist/TokenHub.exe")
        sys.exit(1)


if __name__ == "__main__":
    build()