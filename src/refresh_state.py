# -*- coding: utf-8 -*-
"""刷新状态：请求代次、窗口最后成功值与结果接纳规则。

纯状态模块，不引用 Tk；只允许主线程调用 invalidate/apply。
后台刷新线程只投递 (revision, usages, attempted_at)，由主线程统一提交。
"""

from dataclasses import dataclass

from api import AccountUsage, UsageMetric, WINDOW_NAMES


@dataclass(frozen=True)
class WindowView:
    """单个账号窗口的展示状态（metric 为最后一次有效读数，可能是旧值）。"""

    metric: UsageMetric | None
    last_attempt_at: float | None
    last_success_at: float | None
    error: str | None
    stale: bool


@dataclass(frozen=True)
class AccountView:
    """账号级汇总：成功时间取有效窗口最新值，窗口过期状态独立保留。"""

    last_attempt_at: float | None
    last_success_at: float | None
    error: str | None
    stale: bool
    has_data: bool
    latest_ok: bool


def _window_names() -> tuple:
    return WINDOW_NAMES


def _is_valid(metric) -> bool:
    return (metric is not None
            and getattr(metric, "error", None) is None
            and getattr(metric, "percent", None) is not None)


class RefreshState:
    def __init__(self):
        self._revision = 0
        self._views: dict[str, dict[str, WindowView]] = {}

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def views(self) -> dict[str, dict[str, WindowView]]:
        return self._views

    def invalidate(self) -> int:
        """使当前及更早的在途批次失效，返回新一代 revision。"""
        self._revision += 1
        return self._revision

    def apply(self, revision: int, usages: dict[str, AccountUsage],
              attempted_at: float) -> bool:
        """接纳一批刷新结果；过期批次返回 False 且不改动状态。"""
        if revision != self._revision:
            return False
        views: dict[str, dict[str, WindowView]] = {}
        for key, usage in usages.items():
            previous = self._views.get(key, {})
            row: dict[str, WindowView] = {}
            for name in _window_names():
                metric = getattr(usage, name, None) if usage is not None else None
                old = previous.get(name)
                if _is_valid(metric):
                    row[name] = WindowView(metric, attempted_at, attempted_at,
                                           None, False)
                    continue
                error = getattr(metric, "error", None) or "数据不可用"
                if old is not None and old.metric is not None:
                    row[name] = WindowView(old.metric, attempted_at,
                                           old.last_success_at, error, True)
                else:
                    row[name] = WindowView(None, attempted_at,
                                           old.last_success_at if old else None,
                                           error, False)
            views[key] = row
        self._views = views
        return True

    def account(self, key: str) -> AccountView | None:
        row = self._views.get(key)
        if row is None:
            return None
        attempts = [v.last_attempt_at for v in row.values()
                    if v.last_attempt_at is not None]
        successes = [v.last_success_at for v in row.values()
                     if v.last_success_at is not None]
        errors = [v.error for v in row.values() if v.error]
        latest_ok = any(
            v.error is None and v.metric is not None
            and v.last_success_at == v.last_attempt_at
            for v in row.values())
        return AccountView(
            last_attempt_at=max(attempts) if attempts else None,
            last_success_at=max(successes) if successes else None,
            error=errors[0] if errors else None,
            stale=any(v.stale for v in row.values()),
            has_data=any(v.metric is not None for v in row.values()),
            latest_ok=latest_ok,
        )

    def window(self, key: str, name: str) -> WindowView | None:
        row = self._views.get(key)
        return row.get(name) if row is not None else None
