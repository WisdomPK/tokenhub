# -*- coding: utf-8 -*-
"""W3（F4）：桌面端额度解析契约（共享 fixture）。

fixture 只读；Python 与 dsh 两端共用同一组合成输入。
"""

import json
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from api import (  # noqa: E402
    AccountUsage,
    ApiClient,
    UsageMetric,
    display_percent,
    format_countdown,
    remaining_seconds,
    usage_validation_error,
)
from dialogs import _default_validator  # noqa: E402
from floating_window import FloatingWindow  # noqa: E402
from settings_window import SettingsWindow  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "usage-contract.json"


def _fixture():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _usage_from_windows(windows):
    client = ApiClient()
    return AccountUsage(
        "sk-fixture",
        client._parse_metric(windows.get("rolling")),
        client._parse_metric(windows.get("weekly")),
        client._parse_metric(windows.get("monthly")),
        time.time(),
    )


class FakeResp:
    def __init__(self, body):
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class PercentContractTests(unittest.TestCase):

    def test_fixture_percent_cases(self):
        for case in _fixture()["percent"]:
            with self.subTest(case=case["id"]):
                metric = ApiClient._parse_metric({"percent": case["input"]})
                if case["valid"]:
                    self.assertIsInstance(metric.percent, float)
                    self.assertAlmostEqual(metric.percent, float(case["expected"]))
                else:
                    self.assertIsNone(metric.percent)

    def test_percent_zero_is_available_not_missing(self):
        metric = ApiClient._parse_metric({"percent": 0})
        self.assertEqual(metric.percent, 0.0)
        self.assertIsNone(metric.error)

    def test_over_100_keeps_raw_text_value(self):
        metric = ApiClient._parse_metric({"percent": 120})
        self.assertEqual(metric.percent, 120.0)
        self.assertEqual(display_percent(metric.percent), 120)

    def test_display_rounds_half_up_without_truncating_source(self):
        self.assertEqual(display_percent(59.6), 60)
        self.assertEqual(display_percent(59.4), 59)
        self.assertEqual(display_percent(0), 0)
        self.assertIsNone(display_percent(None))

    def test_floating_display_uses_rounded_value_and_raw_threshold(self):
        win = object.__new__(FloatingWindow)
        win._views = {}
        win.c = {"warn": "warn", "attention": "attention",
                 "fg": "fg", "offline": "offline"}
        usage = AccountUsage(
            "k", UsageMetric("ok", 59.6, None),
            UsageMetric("ok", 84.9, None), UsageMetric("ok", 120, None),
            time.time())
        _p, pct_text, _cd, _icon, _err, _stale = win._metric_display(
            usage, "rolling", False)
        self.assertEqual(pct_text, "60%")
        self.assertEqual(win._pct_color(59.6), "fg")
        self.assertEqual(win._pct_color(60), "attention")
        self.assertEqual(win._pct_color(85), "warn")
        self.assertEqual(win._pct_color(120), "warn")


class ResetsContractTests(unittest.TestCase):

    def test_resets_fixture(self):
        now = datetime(2026, 10, 29, 10, 0, 0, tzinfo=timezone.utc)
        for case in _fixture()["resetsAt"]:
            with self.subTest(case=case["id"]):
                seconds = remaining_seconds(case["input"], now)
                if case["valid"]:
                    self.assertEqual(seconds, 7200)
                else:
                    self.assertIsNone(seconds)

    def test_naive_time_is_utc_and_invalid_time_only_affects_countdown(self):
        now = datetime(2026, 10, 29, 10, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            remaining_seconds("2026-10-29T12:00:00Z", now),
            remaining_seconds("2026-10-29 12:00:00", now))
        self.assertEqual(format_countdown("2026-10-29 12:00:00Z", now), "2h0m")
        self.assertEqual(format_countdown("not-a-time", now), "")


class ResponseShapeTests(unittest.TestCase):

    def test_partial_windows_keep_valid_values(self):
        usage = _usage_from_windows(_fixture()["responses"]["partial"])
        self.assertEqual(usage.rolling.percent, 12.5)
        self.assertIsNone(usage.weekly.percent)
        self.assertEqual(usage.monthly.percent, 7.0)
        self.assertIsNone(usage_validation_error(usage))

    def test_all_missing_is_unavailable_but_response_is_not_malformed(self):
        usage = _usage_from_windows(_fixture()["responses"]["all-missing"])
        for name in ("rolling", "weekly", "monthly"):
            self.assertIsNone(getattr(usage, name).percent)
        self.assertIsNone(usage.account_error)
        self.assertEqual(usage_validation_error(usage), "接口数据不可用")

    def test_bad_time_does_not_discard_percent(self):
        usage = _usage_from_windows(_fixture()["responses"]["bad-time"])
        self.assertEqual(usage.rolling.percent, 12.5)
        self.assertEqual(usage.weekly.percent, 40.0)
        self.assertEqual(usage.monthly.percent, 70.0)
        self.assertIsNone(remaining_seconds(usage.rolling.resets_at))
        self.assertIsNone(remaining_seconds(usage.weekly.resets_at))
        self.assertIsNotNone(remaining_seconds(usage.monthly.resets_at))
        self.assertIsNone(usage_validation_error(usage))

    def test_resets_at_alias_supported(self):
        metric = ApiClient._parse_metric(
            {"percent": 1, "resets_at": "2026-11-29T12:00:00Z"})
        self.assertEqual(metric.resets_at, "2026-11-29T12:00:00Z")
        metric = ApiClient._parse_metric(
            {"percent": 1, "resetsAt": "2026-11-29T12:00:00Z"})
        self.assertEqual(metric.resets_at, "2026-11-29T12:00:00Z")

    def test_whole_response_format_error_is_account_level(self):
        client = ApiClient()
        with mock.patch("api.urlopen", return_value=FakeResp('{"usage": "bad"}')):
            usage = client.fetch_usage("sk-x")
        self.assertEqual(usage.account_error, "响应解析失败")
        self.assertEqual(usage_validation_error(usage), "响应解析失败")

    def test_single_window_unavailable_is_not_response_error(self):
        client = ApiClient()
        body = json.dumps({"usage": {"rolling": {"percent": 1}}})
        with mock.patch("api.urlopen", return_value=FakeResp(body)):
            usage = client.fetch_usage("sk-x")
        self.assertIsNone(usage.account_error)
        self.assertEqual(usage.rolling.percent, 1.0)
        self.assertIsNone(usage_validation_error(usage))

    def test_validation_requires_at_least_one_valid_window(self):
        self.assertEqual(usage_validation_error(None), "接口无返回")
        usage = _usage_from_windows(_fixture()["responses"]["all-missing"])
        self.assertEqual(usage_validation_error(usage), "接口数据不可用")


class ValidationPathTests(unittest.TestCase):

    def test_settings_rejects_http_200_without_valid_window(self):
        partial = _usage_from_windows(_fixture()["responses"]["partial"])
        missing = _usage_from_windows(_fixture()["responses"]["all-missing"])
        self.assertIsNone(SettingsWindow._usage_error(partial))
        self.assertEqual(SettingsWindow._usage_error(missing), "接口数据不可用")

    def test_dialog_validator_uses_same_contract(self):
        partial = _usage_from_windows(_fixture()["responses"]["partial"])
        missing = _usage_from_windows(_fixture()["responses"]["all-missing"])
        with mock.patch("api.ApiClient.fetch_usage", return_value=partial):
            self.assertIsNone(_default_validator("sk-x"))
        with mock.patch("api.ApiClient.fetch_usage", return_value=missing):
            self.assertEqual(_default_validator("sk-x"), "接口数据不可用")


if __name__ == "__main__":
    unittest.main()
