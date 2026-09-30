# -*- coding: utf-8 -*-
"""TokenHub v3 用量接口客户端（官方 F1 接口）。

真实接口：GET https://opencode.ai/zen/go/v1/usage
请求头：Authorization: Bearer <key>；超时 15s；单账号失败不抛异常（写入 UsageMetric.error）。
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

API_URL = "https://opencode.ai/zen/go/v1/usage"
REQUEST_TIMEOUT = 15
MAX_WORKERS = 8


@dataclass
class UsageMetric:
    """单个窗口（5小时/每周/每月）的额度指标。"""

    status: str
    percent: int | None
    resets_at: str | None
    error: str | None = None


@dataclass
class AccountUsage:
    """一个账号的全部额度数据。"""

    key: str
    rolling: UsageMetric
    weekly: UsageMetric
    monthly: UsageMetric
    fetched_at: float


class ApiClient:
    def fetch_usage(self, key: str) -> AccountUsage:
        """同步拉取单个账号用量；任何失败写 UsageMetric.error，不抛异常。"""
        request = Request(
            API_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                # 网关屏蔽 Python-urllib 等通用库默认 UA（实测 403）；须标识自身客户端
                "User-Agent": "tokenhub/3.0.0",
            },
        )
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read()
        except HTTPError as e:
            if e.code == 401:
                message = "密钥无效（401）"
            elif e.code == 429:
                message = "请求过于频繁（429）"
            else:
                message = f"接口返回 HTTP {e.code}"
            return self._error_usage(key, message)
        except Exception:
            return self._error_usage(key, "网络错误")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._error_usage(key, "响应解析失败")
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return self._error_usage(key, "响应解析失败")
        return AccountUsage(
            key,
            self._parse_metric(usage.get("rolling")),
            self._parse_metric(usage.get("weekly")),
            self._parse_metric(usage.get("monthly")),
            time.time(),
        )

    def fetch_all(self, keys: list[str]) -> dict[str, AccountUsage]:
        """并发刷新全部账号；返回 {key: AccountUsage}。"""
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}
        results: dict[str, AccountUsage] = {}
        workers = min(MAX_WORKERS, len(keys))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self.fetch_usage, k): k for k in keys}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception:
                    results[key] = self._error_usage(key, "网络错误")
        return results

    @staticmethod
    def _parse_metric(item) -> UsageMetric:
        if not isinstance(item, dict):
            return UsageMetric("error", None, None, "数据缺失")
        try:
            percent = int(item.get("percent"))
        except (TypeError, ValueError):
            percent = None
        status = str(item.get("status") or "ok")
        resets_at = item.get("resetsAt") or item.get("resets_at")
        return UsageMetric(status, percent, resets_at if isinstance(resets_at, str) else None)

    @staticmethod
    def _error_usage(key: str, message: str) -> AccountUsage:
        now = time.time()
        metric = UsageMetric("error", None, None, message)
        return AccountUsage(key, metric, metric, metric, now)


def format_countdown(resets_at: str, now: datetime | None = None) -> str:
    """重置倒计时文案：<24h → "Xh Ym"；≥24h → "X天Yh"；已过期 → "即将重置"。

    resetsAt 按 UTC 解析（带 Z 后缀），与本地时区无关。
    """
    if not resets_at:
        return ""
    try:
        reset = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
        ref = now if now is not None else datetime.now(timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        diff = reset - ref
    except (ValueError, TypeError, OverflowError):
        return ""
    if diff.total_seconds() <= 0:
        return "即将重置"
    minutes = int(diff.total_seconds() // 60)
    hours = minutes // 60
    mins = minutes % 60
    if hours < 24:
        return f"{hours}h{mins}m"
    return f"{hours // 24}天{hours % 24}h"