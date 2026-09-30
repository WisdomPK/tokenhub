# -*- coding: utf-8 -*-
"""TokenHub v3 配置与账号池管理。

config.json 位于 exe 同目录（开发期=项目根）；不可写时回退 %LOCALAPPDATA%\\TokenHub\\。
密钥明文存储（与 opencode 自身一致）；UI 仅展示掩码（Config.mask_key）。
"""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Account:
    """账号池条目：显示标签 + 密钥。"""

    label: str
    key: str


class ConfigWriteBlocked(RuntimeError):
    """配置写入被保护：load_error 非空（文件损坏/结构错误）时禁止任何写盘。

    R2-01：损坏配置必须保留原样，恢复/重建只能是明确操作；
    所有保存入口（save/upsert_account/remove_account/ensure_current_in_pool/
    apply_settings）在 load_error 时抛出本异常，不得静默覆盖。
    """


class Config:
    """配置存取与账号池操作（签名按 spec 附录 A 冻结）。"""

    VERSION = 3
    DEFAULT_SETTINGS = {
        "refresh_interval": 60,       # 秒
        "hotzone_height": 16,         # px（用户反馈 10px 太薄，默认调大到 16）
        "hotzone_width_ratio": 0.5,   # 占窗口宽比例
        "hotzone_offset": 48,         # 热区/展开位置相对窗口顶边的下移量 px（避开会话标签行）
        "fallback_screen_edge": True,  # 无窗口时回退屏幕顶边
        "theme": "auto",              # auto/light/dark
    }
    _SETTING_TYPES = {
        "refresh_interval": int,
        "hotzone_height": int,
        "hotzone_width_ratio": float,
        "hotzone_offset": int,
        "fallback_screen_edge": bool,
        "theme": str,
    }
    _THEMES = ("auto", "light", "dark")

    def __init__(self, path: str | None = None):
        self.accounts: list[Account] = []
        self.settings: dict = dict(self.DEFAULT_SETTINGS)
        self.load_error: str | None = None
        self.CONFIG_FILE = path or self._resolve_path()
        self._path = self.CONFIG_FILE
        self.load()

    # ------------------------------------------------------------------ 路径
    @staticmethod
    def _app_dir() -> str:
        """exe 同目录；开发期为项目根（src 的上一级）。"""
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return str(Path(__file__).resolve().parent.parent)

    @staticmethod
    def _is_writable(directory: str) -> bool:
        try:
            probe = os.path.join(directory, ".tokenhub_write_test")
            with open(probe, "w", encoding="utf-8"):
                pass
            os.remove(probe)
            return True
        except OSError:
            return False

    def _resolve_path(self) -> str:
        app_dir = self._app_dir()
        if self._is_writable(app_dir):
            return os.path.join(app_dir, "config.json")
        fallback_dir = os.path.join(
            os.environ.get("LOCALAPPDATA", str(Path.home())), "TokenHub"
        )
        try:
            os.makedirs(fallback_dir, exist_ok=True)
        except OSError:
            fallback_dir = str(Path.home())
        return os.path.join(fallback_dir, "config.json")

    # ------------------------------------------------------------------ 存取
    def load(self) -> None:
        """加载配置。

        TH-04：区分文件不存在/读取失败/解析失败/结构错误；
        损坏或结构错误的文件【保留原样不覆盖】，问题经 load_error 显式报告，
        由明确的恢复操作（删除文件后重启 / 修复内容）重建，绝不静默覆盖。
        """
        self.accounts = []
        self.settings = dict(self.DEFAULT_SETTINGS)
        self.load_error = None
        if not os.path.exists(self._path):
            self.save()
            return
        try:
            # utf-8-sig：容忍外部工具（如 PowerShell）写入的 UTF-8 BOM
            with open(self._path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except OSError as e:
            self.load_error = f"配置读取失败（{e}），已保留原文件；如需重建请手动删除 {self._path} 后重启"
            return
        except ValueError as e:
            self.load_error = f"配置解析失败（{e}），已保留原文件；如需重建请手动删除 {self._path} 后重启"
            return
        if not isinstance(data, dict):
            self.load_error = (
                "配置顶层不是 JSON 对象，已保留原文件；请修复内容或手动删除后重启"
            )
            return
        raw_accounts = data.get("accounts", [])
        if not isinstance(raw_accounts, list):
            self.load_error = "配置 accounts 字段不是列表，已保留原文件；请修复内容后重启"
            return
        for item in raw_accounts:
            if isinstance(item, dict) and isinstance(item.get("key"), str) and item["key"]:
                label = item.get("label")
                self.accounts.append(Account(str(label) if label else "", item["key"]))
        self.settings = self._sanitize_settings(data.get("settings"))

    def save(self) -> None:
        """保存配置（同目录临时文件 + 原子替换）。

        R2-01：load_error 非空时拒绝写盘（抛 ConfigWriteBlocked），
        损坏文件保持原样；TH-04：保存失败时清理临时文件并抛出
        （调用方负责恢复内存状态，保证内存与磁盘一致）。
        """
        if self.load_error:
            raise ConfigWriteBlocked(
                f"配置文件损坏已保护（{self.CONFIG_FILE}）：{self.load_error}；请修复或删除该文件后重启"
            )
        data = {
            "version": self.VERSION,
            "accounts": [
                {"label": a.label, "key": a.key, "key_masked": self.mask_key(a.key)}
                for a in self.accounts
            ],
            "settings": self.settings,
        }
        tmp_path = self._path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except OSError:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _sanitize_settings(raw) -> dict:
        out = dict(Config.DEFAULT_SETTINGS)
        if not isinstance(raw, dict):
            return out
        for key, typ in Config._SETTING_TYPES.items():
            value = raw.get(key)
            if typ is bool:
                if isinstance(value, bool):
                    out[key] = value
            elif typ is int:
                if isinstance(value, bool):
                    continue
                try:
                    out[key] = int(value)
                except (TypeError, ValueError):
                    pass
            elif typ is float:
                if isinstance(value, bool):
                    continue
                try:
                    out[key] = float(value)
                except (TypeError, ValueError):
                    pass
            elif typ is str:
                if isinstance(value, str) and value in Config._THEMES:
                    out[key] = value
        return out

    # ------------------------------------------------------------------ 账号池
    def find_by_key(self, key: str) -> Account | None:
        for a in self.accounts:
            if a.key == key:
                return a
        return None

    def _check_writable(self) -> None:
        """R2-01：load_error 时拒绝账号池/设置写入（在变更前抛出）。"""
        if self.load_error:
            raise ConfigWriteBlocked(
                f"配置文件损坏已保护（{self.CONFIG_FILE}）：{self.load_error}；请修复或删除该文件后重启"
            )

    def upsert_account(self, label: str, key: str) -> Account:
        """新增或更新（同 key 更新标签）；变更即落盘。

        R2-03：改名分支先记录旧 label，保存失败恢复后 re-raise
        （浅复制 list 无效——账号对象本身已被修改）。
        TH-04：保存失败恢复变更前状态，保证内存与磁盘一致。
        """
        self._check_writable()
        existing = self.find_by_key(key)
        if existing is not None:
            if label and label.strip():
                old_label = existing.label
                existing.label = label.strip()
                try:
                    self.save()
                except Exception:
                    existing.label = old_label
                    raise
            return existing
        label = label.strip() if label and label.strip() else self._next_default_label()
        account = Account(label, key)
        before = list(self.accounts)
        self.accounts.append(account)
        try:
            self.save()
        except Exception:
            self.accounts = before
            raise
        return account

    def remove_account(self, key: str) -> bool:
        self._check_writable()
        before = list(self.accounts)
        for i, a in enumerate(self.accounts):
            if a.key == key:
                del self.accounts[i]
                try:
                    self.save()
                except Exception:
                    self.accounts = before
                    raise
                return True
        return False

    def ensure_current_in_pool(self, current_key: str | None) -> Account | None:
        """当前密钥不在池中则自动加入（默认标签 账号N）；返回池中对应对象。"""
        self._check_writable()
        if not current_key:
            return None
        existing = self.find_by_key(current_key)
        if existing is not None:
            return existing
        account = Account(self._next_default_label(), current_key)
        before = list(self.accounts)
        self.accounts.append(account)
        try:
            self.save()
        except Exception:
            self.accounts = before
            raise
        return account

    def apply_settings(self, values: dict) -> None:
        """合并校验并保存设置；失败恢复快照并 re-raise（内存与磁盘一致）。

        供设置窗口保存：values 为完整或部分设置项；经 _sanitize_settings
        校验（非法/缺省键回落到默认值）。
        """
        self._check_writable()
        snapshot = dict(self.settings)
        merged = dict(self.settings)
        if isinstance(values, dict):
            merged.update(values)
        self.settings = self._sanitize_settings(merged)
        try:
            self.save()
        except Exception:
            self.settings = snapshot
            raise

    def _next_default_label(self) -> str:
        """默认标签：账号N；N 从 len(accounts)+1 起递增，避免与现有标签冲突。"""
        n = max(1, len(self.accounts) + 1)
        while any(a.label == f"账号{n}" for a in self.accounts):
            n += 1
        return f"账号{n}"

    @staticmethod
    def mask_key(key: str) -> str:
        """密钥掩码（全项目唯一规则）：

        - `oc_sk_` 开头且长度 ≥16 → `key[:8] + '…' + key[-4:]`（如 `oc_sk_AB…wxYZ`）；
        - 其他且长度 ≥12 → `key[:6] + '…' + key[-4:]`（原行为，覆盖 `sk-` 前缀）；
        - 其余（含空串）→ `''`；长度 <12 → `'***'`。
        """
        if not key:
            return ""
        if key.startswith("oc_sk_") and len(key) >= 16:
            return key[:8] + "…" + key[-4:]
        if len(key) >= 12:
            return key[:6] + "…" + key[-4:]
        return "***"