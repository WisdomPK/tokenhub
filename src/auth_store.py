# -*- coding: utf-8 -*-
"""TokenHub v3 auth.json 读取与安全切换。

路径解析（spec 3.2）：
  1. 环境变量 OPENCODE_AUTH_PATH（仅测试用）
  2. %XDG_DATA_HOME%\\opencode\\auth.json（若设置）
  3. ~/.local/share/opencode/auth.json  ← 本机实际
  4. ~/.opencode/auth.json
优先"存在且含 opencode-go 字段"的文件；都不存在时默认目标为第 1 项。

切换：备份（保留最近 5 份）+ 同目录临时文件 + os.replace 原子替换 + 读回校验 + 失败回滚。
仅改 opencode-go 字段，保留其他键。
"""

import glob
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from logger import log_exception

BACKUP_KEEP = 5
BACKUP_SUFFIX = ".tokenhub.bak-"


class AuthStore:
    def __init__(self, path: str | None = None):
        """path 显式指定时忽略环境变量（供测试/集成使用）。"""
        self._explicit_path = path
        self.last_error: str | None = None

    # ------------------------------------------------------------------ 路径
    def _candidates(self) -> list[str]:
        if self._explicit_path:
            return [self._explicit_path]
        candidates = []
        env_path = os.environ.get("OPENCODE_AUTH_PATH")
        if env_path:
            candidates.append(env_path)
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            candidates.append(os.path.join(xdg, "opencode", "auth.json"))
        home = str(Path.home())
        candidates.append(os.path.join(home, ".local", "share", "opencode", "auth.json"))
        candidates.append(os.path.join(home, ".opencode", "auth.json"))
        return candidates

    @staticmethod
    def _has_opencode_go(path: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return isinstance(data, dict) and "opencode-go" in data
        except (OSError, ValueError):
            return False

    def path(self) -> str:
        candidates = self._candidates()
        for c in candidates:
            if os.path.isfile(c) and self._has_opencode_go(c):
                return c
        for c in candidates:
            if os.path.isfile(c):
                return c
        return candidates[0]

    # ------------------------------------------------------------------ 读取
    def read_current_key(self) -> tuple[str | None, str | None]:
        """读取当前 key，同时返回诊断信息。

        (None, None) 表示文件/字段缺失（未配置）；
        (None, message) 表示读取/解析异常，调用方不得当作未配置。
        """
        error = None
        try:
            with open(self.path(), "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except FileNotFoundError:
            self.last_error = None
            return (None, None)
        except (OSError, ValueError) as e:
            error = f"认证文件读取失败（{e}）"
        else:
            if not isinstance(data, dict):
                error = "认证文件读取失败（顶层不是 JSON 对象）"
            else:
                entry = data.get("opencode-go")
                if entry is None:
                    self.last_error = None
                    return (None, None)
                if not isinstance(entry, dict):
                    error = "认证文件读取失败（opencode-go 字段格式错误）"
                    self.last_error = error
                    return (None, error)
                key = entry.get("key")
                if isinstance(key, str) and key:
                    self.last_error = None
                    return (key, None)
                self.last_error = None
                return (None, None)
        self.last_error = error
        return (None, error)

    def current_key(self) -> str | None:
        """兼容接口：异常时返回 None，但 last_error 保留诊断。"""
        key, _error = self.read_current_key()
        return key

    def mtime(self) -> float:
        try:
            return os.path.getmtime(self.path())
        except OSError:
            return 0.0

    # ------------------------------------------------------------------ 备份
    def backup(self) -> str | None:
        """备份当前 auth.json 为 auth.json.tokenhub.bak-<ts>，返回备份路径。"""
        path = self.path()
        if not os.path.exists(path):
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = f"{path}{BACKUP_SUFFIX}{stamp}"
        try:
            shutil.copy2(path, backup_path)
        except OSError as e:
            log_exception("auth_backup", e)
            return None
        self._prune_backups(path)
        return backup_path

    def _prune_backups(self, path: str) -> None:
        try:
            backups = sorted(glob.glob(f"{path}{BACKUP_SUFFIX}*"))
        except OSError as e:
            log_exception("auth_prune_backups", e)
            return
        for old in backups[:-BACKUP_KEEP]:
            try:
                os.remove(old)
            except OSError as e:
                log_exception(f"auth_prune_backup:{old}", e)

    # ------------------------------------------------------------------ 切换
    def switch_to(self, key: str) -> tuple[bool, str, str]:
        """原子切换 opencode-go 密钥。返回 (ok, message, status)。

        R2-02/R2-04 契约（冻结）：
        - status ∈ {"ok", "unaffected", "restored", "restore_failed"}；
        - 消息冻结：ok → "已写入认证文件"；unaffected → "写入失败，原账号未受影响"；
          restored → "读回校验失败，已恢复原账号"；
          restore_failed → "恢复失败，状态待确认；备份位于 <实际存在路径>"
          （备份不存在时不含"备份位于"）；
        - 恢复永不消耗备份（copyfile + 原子替换），备份仅由保留策略轮转清理。
        """
        path = self.path()
        existed = os.path.exists(path)
        if existed:
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("auth.json 顶层不是 JSON 对象")
            except (OSError, ValueError) as e:
                return (False, f"auth.json 解析失败，未写入（{e}）", "unaffected")
        else:
            data = {}
        backup_path = self.backup() if existed else None
        if existed and backup_path is None:
            return (False, "备份 auth.json 失败，未写入", "unaffected")
        data["opencode-go"] = {"type": "api", "key": key}
        parent = os.path.dirname(path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                return (False, f"认证文件目录不可创建，未写入（{e}）", "unaffected")
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except OSError:
            self._cleanup_tmp(tmp_path)
            # 原子替换失败 = 目标文件未被触碰，原账号未受影响（无需回滚）
            return (False, "写入失败，原账号未受影响", "unaffected")
        # 读回校验
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                check = json.load(f)
            if (check.get("opencode-go") or {}).get("key") != key:
                raise ValueError("读回内容与写入不一致")
        except (OSError, ValueError):
            restored, message = self._rollback(path, backup_path, existed)
            if restored:
                return (False, "读回校验失败，已恢复原账号", "restored")
            return (False, message, "restore_failed")
        return (True, "已写入认证文件", "ok")

    @staticmethod
    def _cleanup_tmp(tmp_path: str) -> None:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError as e:
            log_exception(f"auth_cleanup_tmp:{tmp_path}", e)

    @staticmethod
    def _rollback(path: str, backup_path: str | None, existed: bool) -> tuple[bool, str]:
        """尝试恢复到变更前状态。返回 (restored, message)。

        R3-01：仅在原文件本不存在（existed=False）时才允许删除新文件；
        原文件存在而备份缺失/不可用 → 保留现状并返回 restore_failed
        （不得删除既有文件、不得报"已恢复"）。
        R3-02：恢复成功判定 = 恢复后内容与备份一致（字节级比较），
        不依赖原文件是否含 opencode-go 字段；备份不可解析/恢复写入失败
        均明确报错（restore_failed）。
        R2-04：恢复永不消耗备份（copyfile + 同目录临时文件 + 原子替换）。
        """
        if backup_path and os.path.exists(backup_path):
            # 读取备份并验证可解析（不可解析 → 明确报错，不判成功）
            try:
                with open(backup_path, "rb") as f:
                    backup_bytes = f.read()
            except OSError:
                return AuthStore._restore_failed_msg(backup_path)
            try:
                json.loads(backup_bytes.decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                return AuthStore._restore_failed_msg(backup_path)
            tmp_path = f"{path}.restoretmp"
            try:
                shutil.copyfile(backup_path, tmp_path)
                os.replace(tmp_path, path)
            except OSError:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError as e:
                    log_exception(f"auth_rollback_cleanup:{tmp_path}", e)
                return AuthStore._restore_failed_msg(backup_path)
            # 核对：恢复后内容与备份一致（字节级）
            try:
                with open(path, "rb") as f:
                    now_bytes = f.read()
            except OSError:
                return AuthStore._restore_failed_msg(backup_path)
            if now_bytes == backup_bytes:
                return (True, "已恢复原账号")
            return AuthStore._restore_failed_msg(backup_path)
        # 备份缺失/不可用
        if not existed:
            # 原文件本不存在 → 删除新文件即恢复
            if not os.path.exists(path):
                return (True, "已恢复原账号（原文件本不存在）")
            try:
                os.remove(path)
                if not os.path.exists(path):
                    return (True, "已恢复原账号（新文件已删除）")
            except OSError:
                pass
            return (False, "恢复失败，状态待确认")
        # 原文件存在、备份缺失 → 保留现状，restore_failed（R3-01：不得删除）
        return AuthStore._restore_failed_msg(backup_path)

    @staticmethod
    def _restore_failed_msg(backup_path: str | None) -> tuple[bool, str]:
        """restore_failed 冻结消息；引用备份路径前核对存在性。"""
        if backup_path and os.path.exists(backup_path):
            return (False, f"恢复失败，状态待确认；备份位于 {backup_path}")
        return (False, "恢复失败，状态待确认")