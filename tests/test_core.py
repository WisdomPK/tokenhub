# -*- coding: utf-8 -*-
"""TokenHub v3 核心逻辑测试（Lane A）。

覆盖：config 账号池/掩码、api 解析/错误分类/倒计时、auth_store 路径/原子切换/回滚、
control_client env 发现/HTTP 往返/401/超时。

运行（项目根）：PY -m unittest discover -s tests -v
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from api import AccountUsage, ApiClient, UsageMetric, format_countdown  # noqa: E402
from auth_store import AuthStore  # noqa: E402
from config import Account, Config  # noqa: E402
from control_client import ControlClient, ControlInfo  # noqa: E402


# ---------------------------------------------------------------------------
# 本地 stub 控制服务器（control_client 测试用）
# ---------------------------------------------------------------------------
STUB = {"token": "tok123", "switch_delay": 0.0, "switch_http": 200,
        "switch_ok": True, "switch_uncertain": False}


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            if self.headers.get("X-TokenHub-Token") != STUB["token"]:
                self._send(401, {"ok": False})
                return
            self._send(200, {"ok": True, "version": "3.0.0"})
        else:
            self._send(404, {"ok": False})

    def do_POST(self):
        # 客户端超时断开时 handler 线程会抛连接错误 → 吞掉避免测试输出噪音
        try:
            self._handle_switch()
        except Exception:
            pass

    def _handle_switch(self):
        if self.path != "/switch":
            self._send(404, {"ok": False})
            return
        if self.headers.get("X-TokenHub-Token") != STUB["token"]:
            self._send(401, {"ok": False, "error": "令牌不符"})
            return
        if STUB["switch_delay"] > 0:
            time.sleep(STUB["switch_delay"])
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send(400, {"ok": False, "error": "bad json"})
            return
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not (key.startswith("sk-") or key.startswith("oc_sk_")):
            self._send(400, {"ok": False, "error": "bad key"})
            return
        if STUB["switch_uncertain"]:
            self._send(200, {
                "ok": False,
                "method": "api",
                "uncertain": True,
                "status": "timeout_uncertain",
                "error": "控制接口超时，无法确认切换结果；未再次写入认证文件",
            })
            return
        self._send(STUB["switch_http"], {
            "ok": STUB["switch_ok"],
            "method": "file",
            "error": None if STUB["switch_ok"] else "rejected",
        })


def _serve(server):
    try:
        server.serve_forever()
    except Exception:
        pass  # 关闭瞬间 select 可能报错 → 静默


def _start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=_serve, args=(server,), daemon=True)
    thread.start()
    return server, server.server_address[1]


def _stop_stub(server):
    try:
        server.shutdown()
    except Exception:
        pass
    server.server_close()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "config.json")

    def test_config_roundtrip(self):
        cfg = Config(path=self.path)
        cfg.accounts = [Account("账号B", "sk-B" * 8), Account("账号C", "sk-C" * 8)]
        cfg.settings["refresh_interval"] = 120
        cfg.settings["hotzone_height"] = 12
        cfg.settings["hotzone_width_ratio"] = 0.6
        cfg.settings["fallback_screen_edge"] = False
        cfg.settings["theme"] = "dark"
        cfg.save()

        cfg2 = Config(path=self.path)
        self.assertEqual(len(cfg2.accounts), 2)
        self.assertEqual(cfg2.accounts[0].label, "账号B")
        self.assertEqual(cfg2.accounts[0].key, "sk-B" * 8)
        self.assertEqual(cfg2.accounts[1].label, "账号C")
        self.assertEqual(cfg2.settings["refresh_interval"], 120)
        self.assertEqual(cfg2.settings["hotzone_height"], 12)
        self.assertEqual(cfg2.settings["hotzone_width_ratio"], 0.6)
        self.assertEqual(cfg2.settings["fallback_screen_edge"], False)
        self.assertEqual(cfg2.settings["theme"], "dark")

    def test_default_settings(self):
        cfg = Config(path=self.path)
        self.assertEqual(cfg.settings["refresh_interval"], 60)
        self.assertEqual(cfg.settings["hotzone_height"], 16)
        self.assertEqual(cfg.settings["hotzone_width_ratio"], 0.5)
        self.assertEqual(cfg.settings["fallback_screen_edge"], True)
        self.assertEqual(cfg.settings["theme"], "auto")

    def test_ensure_current_in_pool_new_and_existing(self):
        cfg = Config(path=self.path)
        added = cfg.ensure_current_in_pool("sk-new-key-1234abcd")
        self.assertIsNotNone(added)
        self.assertEqual(added.label, "账号1")
        self.assertEqual(added.key, "sk-new-key-1234abcd")
        # 已存在 → 返回原对象且不改名
        cfg.accounts[0].label = "主账号"
        again = cfg.ensure_current_in_pool("sk-new-key-1234abcd")
        self.assertIs(again, cfg.accounts[0])
        self.assertEqual(again.label, "主账号")
        # 第二个新 key → 账号2
        second = cfg.ensure_current_in_pool("sk-second-key-5678efgh")
        self.assertEqual(second.label, "账号2")
        # 无当前 key → 不添加
        self.assertIsNone(cfg.ensure_current_in_pool(None))

    def test_upsert_and_remove(self):
        cfg = Config(path=self.path)
        a = cfg.upsert_account("账号X", "sk-aaa-bbb-ccc")
        self.assertEqual(cfg.find_by_key("sk-aaa-bbb-ccc"), a)
        self.assertEqual(len(cfg.accounts), 1)
        # 同 key 更新标签，不重复添加
        a2 = cfg.upsert_account("账号Y", "sk-aaa-bbb-ccc")
        self.assertIs(a2, a)
        self.assertEqual(a2.label, "账号Y")
        self.assertEqual(len(cfg.accounts), 1)
        # remove
        self.assertTrue(cfg.remove_account("sk-aaa-bbb-ccc"))
        self.assertFalse(cfg.remove_account("sk-aaa-bbb-ccc"))
        self.assertIsNone(cfg.find_by_key("sk-aaa-bbb-ccc"))
        # 持久化
        cfg3 = Config(path=self.path)
        self.assertEqual(len(cfg3.accounts), 0)

    def test_upsert_empty_label_keeps_existing_name(self):
        cfg = Config(path=self.path)
        cfg.upsert_account("旧名", "sk-keep-name-0001")
        cfg.upsert_account("", "sk-keep-name-0001")
        self.assertEqual(cfg.find_by_key("sk-keep-name-0001").label, "旧名")

    def test_mask_key(self):
        # 统一规则：oc_sk_ 长 key（≥16）→ 前8+…+后4；sk- 长 key → 前6+…+后4；短 key → ***
        self.assertEqual(Config.mask_key("sk-AbCXXXXXXXXXXXXXXXXXXwxyz"), "sk-AbC…wxyz")
        self.assertEqual(Config.mask_key("oc_sk_ABCDEFGHIJKLMNOPQRSTUVwxYZ"), "oc_sk_AB…wxYZ")
        self.assertEqual(Config.mask_key("oc_sk_abcdefghij"), "oc_sk_ab…ghij")
        # oc_sk_ 前缀但长度 <16 → 走通用规则（≥12 → 前6+…+后4）
        self.assertEqual(Config.mask_key("oc_sk_abcdef"), "oc_sk_…cdef")
        self.assertEqual(Config.mask_key("shortkey"), "***")
        self.assertEqual(Config.mask_key(""), "")

    # ---- TH-04：损坏保留 / 结构校验 / 保存失败一致性 ----

    def test_config_corrupt_preserved(self):
        # 解析失败 → 保留原文件不覆盖，load_error 显式报告
        p = Path(self.path)
        p.write_text("{corrupt json!!!", encoding="utf-8")
        cfg = Config(path=self.path)
        self.assertIsNotNone(cfg.load_error)
        self.assertEqual(cfg.accounts, [])
        self.assertEqual(p.read_text(encoding="utf-8"), "{corrupt json!!!")

    def test_config_array_structure_error(self):
        # 合法 JSON 数组 [] → 结构错误，不崩、不覆盖
        p = Path(self.path)
        p.write_text("[]", encoding="utf-8")
        cfg = Config(path=self.path)
        self.assertIsNotNone(cfg.load_error)
        self.assertEqual(cfg.accounts, [])
        self.assertEqual(p.read_text(encoding="utf-8"), "[]")

    def test_config_bom_tolerated(self):
        # 外部工具（如 PowerShell）写入 UTF-8 BOM → 仍能正常加载（不触发保护）
        p = Path(self.path)
        payload = json.dumps({
            "version": 3,
            "accounts": [{"label": "账号B", "key": "sk-bom-key-1234"}],
            "settings": {
                "refresh_interval": 60, "hotzone_height": 16,
                "hotzone_width_ratio": 0.5, "hotzone_offset": 48,
                "fallback_screen_edge": True, "theme": "auto",
            },
        })
        p.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
        cfg = Config(path=self.path)
        self.assertIsNone(cfg.load_error)
        self.assertEqual(len(cfg.accounts), 1)
        self.assertEqual(cfg.accounts[0].label, "账号B")
        self.assertEqual(cfg.accounts[0].key, "sk-bom-key-1234")
        self.assertEqual(cfg.settings["hotzone_offset"], 48)

    def test_config_accounts_not_list(self):
        # accounts 字段类型错误 → 结构错误，保留原文件
        p = Path(self.path)
        p.write_text(json.dumps({"version": 3, "accounts": {"a": 1}}), encoding="utf-8")
        cfg = Config(path=self.path)
        self.assertIsNotNone(cfg.load_error)
        self.assertEqual(cfg.accounts, [])

    def test_config_save_failure_restores_memory(self):
        # 保存失败 → 内存回滚，磁盘不变（TH-04 一致性）
        cfg = Config(path=self.path)
        with mock.patch("config.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cfg.upsert_account("账号X", "sk-aaa-bbb-ccc")
        self.assertEqual(cfg.accounts, [])
        data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(data.get("accounts"), [])
        # remove 同样回滚
        cfg.accounts = [Account("账号1", "sk-1")]
        cfg.save()
        with mock.patch("config.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cfg.remove_account("sk-1")
        self.assertEqual(len(cfg.accounts), 1)
        self.assertEqual(cfg.accounts[0].key, "sk-1")

    # ---- R2-01：损坏配置写保护 ----

    def test_config_write_blocked_when_load_error(self):
        # load_error 时所有保存入口均抛 ConfigWriteBlocked，原文件字节不变
        p = Path(self.path)
        broken = b"{corrupt json!!!"
        p.write_bytes(broken)
        cfg = Config(path=self.path)
        self.assertIsNotNone(cfg.load_error)
        from config import ConfigWriteBlocked
        with self.assertRaises(ConfigWriteBlocked):
            cfg.save()
        with self.assertRaises(ConfigWriteBlocked):
            cfg.upsert_account("账号X", "sk-aaa-bbb-ccc")
        with self.assertRaises(ConfigWriteBlocked):
            cfg.remove_account("sk-aaa-bbb-ccc")
        with self.assertRaises(ConfigWriteBlocked):
            cfg.ensure_current_in_pool("sk-current-1234")
        with self.assertRaises(ConfigWriteBlocked):
            cfg.apply_settings({"refresh_interval": 30})
        self.assertEqual(p.read_bytes(), broken, "损坏文件字节必须保持不变")
        self.assertEqual(cfg.accounts, [])

    # ---- R2-03：改名保存失败恢复 label ----

    def test_config_rename_restores_label_on_save_failure(self):
        cfg = Config(path=self.path)
        acct = cfg.upsert_account("旧名", "sk-aaa-bbb-ccc")
        self.assertEqual(acct.label, "旧名")
        with mock.patch("config.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cfg.upsert_account("新名", "sk-aaa-bbb-ccc")
        # 内存 label 必须恢复（浅复制 list 无效，对象属性必须还原）
        self.assertEqual(acct.label, "旧名")
        self.assertEqual(cfg.accounts[0].label, "旧名")
        # 磁盘保持旧名
        data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(data["accounts"][0]["label"], "旧名")

    # ---- apply_settings：失败恢复快照 ----

    def test_config_apply_settings_failure_restores(self):
        cfg = Config(path=self.path)
        cfg.settings["refresh_interval"] = 120
        cfg.save()
        with mock.patch("config.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cfg.apply_settings({"refresh_interval": 999, "theme": "dark"})
        # 内存恢复快照
        self.assertEqual(cfg.settings["refresh_interval"], 120)
        self.assertEqual(cfg.settings["theme"], "auto")
        # 磁盘不变
        data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(data["settings"]["refresh_interval"], 120)

    def test_config_apply_settings_ok(self):
        cfg = Config(path=self.path)
        cfg.apply_settings({"refresh_interval": 90, "theme": "dark"})
        self.assertEqual(cfg.settings["refresh_interval"], 90)
        self.assertEqual(cfg.settings["theme"], "dark")
        data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(data["settings"]["refresh_interval"], 90)


# ---------------------------------------------------------------------------
# ApiClient
# ---------------------------------------------------------------------------
class ApiTests(unittest.TestCase):
    F1_JSON = json.dumps({
        "usage": {
            "rolling": {"status": "ok", "percent": 1, "resetsAt": "2026-09-30T03:00:00.000Z"},
            "weekly": {"status": "ok", "percent": 0, "resetsAt": "2026-10-04T03:00:00.000Z"},
            "monthly": {"status": "ok", "percent": 0, "resetsAt": "2026-10-31T03:00:00.000Z"},
        }
    })

    class FakeResp:
        def __init__(self, status, body):
            self.status = status
            self.body = body if isinstance(body, bytes) else body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body

    def _patch_urlopen(self, status=200, body=None, exc=None, recorder=None):
        def fake_urlopen(req, timeout=None):
            if recorder is not None:
                recorder.append(req)
            if exc is not None:
                raise exc
            return self.FakeResp(status, body)

        return mock.patch("api.urlopen", side_effect=fake_urlopen)

    def test_api_parse_ok(self):
        client = ApiClient()
        recorder = []
        with self._patch_urlopen(200, self.F1_JSON, recorder=recorder):
            usage = client.fetch_usage("sk-AbC-test-key-0001")
        self.assertEqual(usage.rolling.percent, 1)
        self.assertEqual(usage.weekly.percent, 0)
        self.assertEqual(usage.monthly.percent, 0)
        self.assertEqual(usage.rolling.resets_at, "2026-09-30T03:00:00.000Z")
        self.assertEqual(usage.rolling.status, "ok")
        self.assertIsNone(usage.rolling.error)
        self.assertIsNotNone(usage.fetched_at)
        # 请求头携带 Bearer
        self.assertEqual(len(recorder), 1)
        self.assertEqual(recorder[0].get_header("Authorization"), "Bearer sk-AbC-test-key-0001")

    def test_api_parse_errors(self):
        client = ApiClient()
        # 网络错误
        with self._patch_urlopen(exc=urllib.error.URLError("boom")):
            u = client.fetch_usage("sk-x")
        self.assertEqual(u.rolling.error, "网络错误")
        self.assertIsNone(u.rolling.percent)
        self.assertEqual(u.rolling.status, "error")
        # 401
        with self._patch_urlopen(exc=urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)):
            u = client.fetch_usage("sk-x")
        self.assertIn("401", u.rolling.error)
        # 429
        with self._patch_urlopen(exc=urllib.error.HTTPError("u", 429, "Too Many", {}, None)):
            u = client.fetch_usage("sk-x")
        self.assertIn("429", u.rolling.error)
        # 其他非200
        with self._patch_urlopen(exc=urllib.error.HTTPError("u", 500, "Err", {}, None)):
            u = client.fetch_usage("sk-x")
        self.assertIn("500", u.rolling.error)
        # 非法 JSON
        with self._patch_urlopen(200, "not-json{{{"):
            u = client.fetch_usage("sk-x")
        self.assertEqual(u.rolling.error, "响应解析失败")

    def test_api_fetch_all_concurrent(self):
        client = ApiClient()
        keys = [f"sk-key-{i:04d}" for i in range(5)]

        def fake(key):
            return AccountUsage(
                key,
                UsageMetric("ok", 1, None),
                UsageMetric("ok", 2, None),
                UsageMetric("ok", 3, None),
                time.time(),
            )

        with mock.patch.object(client, "fetch_usage", side_effect=fake):
            result = client.fetch_all(keys)
        self.assertEqual(set(result.keys()), set(keys))
        for k in keys:
            self.assertEqual(result[k].rolling.percent, 1)

    def test_countdown_format(self):
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(format_countdown("2026-01-01T04:59:00Z", now), "4h59m")
        self.assertEqual(format_countdown("2026-01-02T00:00:00Z", now), "1天0h")
        self.assertEqual(format_countdown("2025-12-31T23:59:00Z", now), "即将重置")
        self.assertEqual(format_countdown("", now), "")
        self.assertEqual(format_countdown("not-a-date", now), "")

    def test_countdown_uses_utc(self):
        now = datetime(2026, 1, 1, 3, 0, 0, tzinfo=timezone.utc)
        # 若按本地时区解析结果将随机器时区偏移变化；按 UTC 解析恒为 1天0h
        self.assertEqual(format_countdown("2026-01-02T03:00:00Z", now), "1天0h")

    def test_countdown_default_now(self):
        s = format_countdown("2030-01-01T00:00:00Z")
        self.assertIn("天", s)


# ---------------------------------------------------------------------------
# AuthStore
# ---------------------------------------------------------------------------
class AuthStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        (self.home / ".local/share/opencode").mkdir(parents=True, exist_ok=True)

    def _env(self):
        return mock.patch.dict(os.environ, {
            "USERPROFILE": str(self.home),
            "HOME": str(self.home),
            "XDG_DATA_HOME": "",
            "OPENCODE_AUTH_PATH": "",
        }, clear=False)

    def _write_auth(self, path, go_key="sk-current-key-0001"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "opencode-go": {"type": "api", "key": go_key},
            "openai": {"type": "api", "key": "sk-openai"},
        }), encoding="utf-8")

    def test_auth_path_priority(self):
        p = self.home / ".local/share/opencode/auth.json"
        self._write_auth(p)
        with self._env():
            store = AuthStore()
            self.assertEqual(store.path(), str(p))
            self.assertEqual(store.current_key(), "sk-current-key-0001")

    def test_auth_path_env_overrides(self):
        p = self.home / ".local/share/opencode/auth.json"
        self._write_auth(p)
        env_p = Path(self.tmp.name) / "env-auth.json"
        self._write_auth(env_p, go_key="sk-env-key-0002")
        with self._env() as e:
            e["OPENCODE_AUTH_PATH"] = str(env_p)
            store = AuthStore()
            self.assertEqual(store.path(), str(env_p))
            self.assertEqual(store.current_key(), "sk-env-key-0002")

    def test_auth_env_path_missing_is_only_target(self):
        default = self.home / ".local/share/opencode/auth.json"
        self._write_auth(default, go_key="sk-default-key-0001")
        default_bytes = default.read_bytes()
        env_p = Path(self.tmp.name) / "env-missing" / "auth.json"
        with self._env() as e:
            e["OPENCODE_AUTH_PATH"] = str(env_p)
            store = AuthStore()
            self.assertEqual(store.path(), str(env_p))
            self.assertIsNone(store.current_key())
            ok, msg, status = store.switch_to("sk-env-new-9999")
            self.assertTrue(ok, msg)
            self.assertEqual(status, "ok")
            self.assertTrue(env_p.exists())
            self.assertEqual(default.read_bytes(), default_bytes)
            self.assertEqual(store.current_key(), "sk-env-new-9999")

    def test_auth_env_path_corrupt_does_not_touch_default(self):
        default = self.home / ".local/share/opencode/auth.json"
        self._write_auth(default, go_key="sk-default-key-0001")
        default_bytes = default.read_bytes()
        env_p = Path(self.tmp.name) / "env-broken.json"
        env_p.write_text("{not-json", encoding="utf-8")
        env_bytes = env_p.read_bytes()
        with self._env() as e:
            e["OPENCODE_AUTH_PATH"] = str(env_p)
            store = AuthStore()
            self.assertEqual(store.path(), str(env_p))
            ok, msg, status = store.switch_to("sk-env-new-9999")
            self.assertFalse(ok)
            self.assertEqual(status, "unaffected")
            self.assertIn("解析失败", msg)
            self.assertEqual(env_p.read_bytes(), env_bytes)
            self.assertEqual(default.read_bytes(), default_bytes)

    def test_auth_path_xdg(self):
        xdg = Path(self.tmp.name) / "xdg"
        p = xdg / "opencode/auth.json"
        self._write_auth(p)
        with self._env() as e:
            e["XDG_DATA_HOME"] = str(xdg)
            store = AuthStore()
            self.assertEqual(store.path(), str(p))

    def test_auth_path_prefers_file_with_opencode_go(self):
        plain = self.home / ".local/share/opencode/auth.json"
        plain.parent.mkdir(parents=True, exist_ok=True)
        plain.write_text(json.dumps({"openai": {"type": "api", "key": "x"}}), encoding="utf-8")
        alt = self.home / ".opencode/auth.json"
        self._write_auth(alt, go_key="sk-alt-key-0003")
        with self._env():
            store = AuthStore()
            self.assertEqual(store.path(), str(alt))
            self.assertEqual(store.current_key(), "sk-alt-key-0003")

    def test_auth_path_default_when_missing(self):
        with self._env():
            store = AuthStore()
            self.assertEqual(store.path(), str(self.home / ".local/share/opencode/auth.json"))

    # ---- W8：显式路径是唯一目标，不回退/触碰默认路径 ----

    def test_explicit_missing_path_does_not_fall_back_to_default(self):
        default = self.home / ".local/share/opencode/auth.json"
        self._write_auth(default, go_key="sk-default-key-0001")
        default_bytes = default.read_bytes()
        explicit = Path(self.tmp.name) / "explicit" / "auth.json"
        with self._env():
            store = AuthStore(str(explicit))
            self.assertEqual(store.path(), str(explicit))
            self.assertIsNone(store.current_key())
            ok, msg, status = store.switch_to("sk-explicit-key-9999")
            self.assertTrue(ok, msg)
            self.assertEqual(status, "ok")
            self.assertTrue(explicit.exists())
            self.assertEqual(json.loads(explicit.read_text(encoding="utf-8"))
                             ["opencode-go"]["key"], "sk-explicit-key-9999")
            self.assertEqual(default.read_bytes(), default_bytes)
            self.assertEqual(store.current_key(), "sk-explicit-key-9999")

    def test_explicit_corrupt_path_does_not_touch_default(self):
        default = self.home / ".local/share/opencode/auth.json"
        self._write_auth(default, go_key="sk-default-key-0001")
        default_bytes = default.read_bytes()
        explicit = Path(self.tmp.name) / "broken-auth.json"
        explicit.write_text("{not-json", encoding="utf-8")
        explicit_bytes = explicit.read_bytes()
        with self._env():
            store = AuthStore(str(explicit))
            self.assertEqual(store.path(), str(explicit))
            ok, msg, status = store.switch_to("sk-explicit-key-9999")
            self.assertFalse(ok)
            self.assertEqual(status, "unaffected")
            self.assertIn("解析失败", msg)
            self.assertEqual(explicit.read_bytes(), explicit_bytes)
            self.assertEqual(default.read_bytes(), default_bytes)

    def test_switch_creates_missing_parent_directory(self):
        target = Path(self.tmp.name) / "nested" / "deep" / "auth.json"
        with self._env():
            store = AuthStore(str(target))
            ok, msg, status = store.switch_to("sk-new-key-9999abcd")
            self.assertTrue(ok, msg)
            self.assertEqual(status, "ok")
            self.assertTrue(target.exists())
            self.assertEqual(store.current_key(), "sk-new-key-9999abcd")

    def test_restore_message_matches_file_after_readback_failure(self):
        p = self.home / ".local/share/opencode/auth.json"
        self._write_auth(p, go_key="sk-old-key-0004")
        original = p.read_bytes()
        with self._env():
            store = AuthStore()
            real_replace = os.replace
            state = {"n": 0}

            def fake_replace(src, dst):
                state["n"] += 1
                real_replace(src, dst)
                if state["n"] == 1:
                    with open(dst, "w", encoding="utf-8") as f:
                        json.dump({"opencode-go": {"type": "api",
                                                   "key": "sk-TAMPERED"}}, f)

            with mock.patch("auth_store.os.replace", side_effect=fake_replace):
                ok, msg, status = store.switch_to("sk-new-key-9999abcd")
            self.assertFalse(ok)
            self.assertEqual(status, "restored")
            self.assertEqual(msg, "读回校验失败，已恢复原账号")
            self.assertEqual(p.read_bytes(), original)
            self.assertEqual(store.current_key(), "sk-old-key-0004")

    def test_auth_switch_atomic_and_readback(self):
        p = self.home / ".local/share/opencode/auth.json"
        self._write_auth(p, go_key="sk-old-key-0004")
        with self._env():
            store = AuthStore()
            ok, msg, status = store.switch_to("sk-new-key-9999abcd")
            self.assertTrue(ok, msg)
            self.assertEqual(status, "ok")
            self.assertEqual(msg, "已写入认证文件")
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(data["opencode-go"]["key"], "sk-new-key-9999abcd")
            self.assertEqual(data["opencode-go"]["type"], "api")
            self.assertEqual(store.current_key(), "sk-new-key-9999abcd")
            baks = list(p.parent.glob("auth.json.tokenhub.bak-*"))
            self.assertEqual(len(baks), 1)
            self.assertGreater(store.mtime(), 0)

    def test_auth_switch_write_failure_keeps_original(self):
        # TH-03/R2-02：原子替换失败 = 原文件未被触碰 → status=unaffected
        p = self.home / ".local/share/opencode/auth.json"
        self._write_auth(p, go_key="sk-old-key-0004")
        original = p.read_text(encoding="utf-8")
        with self._env():
            store = AuthStore()
            with mock.patch("auth_store.os.replace", side_effect=OSError("disk full")):
                ok, msg, status = store.switch_to("sk-new-key-9999abcd")
            self.assertFalse(ok)
            self.assertEqual(status, "unaffected")
            self.assertEqual(msg, "写入失败，原账号未受影响")
            self.assertEqual(p.read_text(encoding="utf-8"), original)
            self.assertEqual(store.current_key(), "sk-old-key-0004")

    def test_auth_switch_readback_failure_restores(self):
        # TH-03/R2-02：写入成功但读回校验失败 → 恢复备份并核对 → status=restored
        p = self.home / ".local/share/opencode/auth.json"
        self._write_auth(p, go_key="sk-old-key-0004")
        original = p.read_text(encoding="utf-8")
        with self._env():
            store = AuthStore()
            real_replace = os.replace
            state = {"n": 0}

            def fake_replace(src, dst):
                state["n"] += 1
                real_replace(src, dst)
                if state["n"] == 1:
                    # 第一次替换（写入）成功后篡改目标，令读回校验必然失败
                    with open(dst, "w", encoding="utf-8") as f:
                        json.dump({"opencode-go": {"type": "api", "key": "sk-TAMPERED"}}, f)

            with mock.patch("auth_store.os.replace", side_effect=fake_replace):
                ok, msg, status = store.switch_to("sk-new-key-9999abcd")
            self.assertFalse(ok)
            self.assertEqual(status, "restored")
            self.assertEqual(msg, "读回校验失败，已恢复原账号")
            self.assertEqual(p.read_text(encoding="utf-8"), original)
            self.assertEqual(store.current_key(), "sk-old-key-0004")
            # R2-04：恢复不消耗备份，备份仍存在
            baks = list(p.parent.glob("auth.json.tokenhub.bak-*"))
            self.assertEqual(len(baks), 1)

    def test_auth_switch_restore_failure_reports_state(self):
        # TH-03/R2-02：恢复也失败 → status=restore_failed，备份保留并给出备份位置
        p = self.home / ".local/share/opencode/auth.json"
        self._write_auth(p, go_key="sk-old-key-0004")
        with self._env():
            store = AuthStore()
            real_replace = os.replace
            state = {"n": 0}

            def fake_replace(src, dst):
                state["n"] += 1
                if state["n"] == 1:
                    real_replace(src, dst)
                    with open(dst, "w", encoding="utf-8") as f:
                        json.dump({"opencode-go": {"type": "api", "key": "sk-TAMPERED"}}, f)
                else:
                    raise OSError("restore failed")

            with mock.patch("auth_store.os.replace", side_effect=fake_replace):
                ok, msg, status = store.switch_to("sk-new-key-9999abcd")
            self.assertFalse(ok)
            self.assertEqual(status, "restore_failed")
            self.assertIn("待确认", msg)
            # 消息引用的备份路径必须真实存在（R2-04）
            self.assertIn("备份位于", msg)
            backup_ref = msg.split("备份位于 ", 1)[1].strip()
            self.assertTrue(os.path.exists(backup_ref))
            # 备份文件保留且内容为原账号
            baks = list(p.parent.glob("auth.json.tokenhub.bak-*"))
            self.assertEqual(len(baks), 1)
            bak_data = json.loads(baks[0].read_text(encoding="utf-8"))
            self.assertEqual(bak_data["opencode-go"]["key"], "sk-old-key-0004")
            # 状态确实已被改动（恢复失败），消息如实告知待确认
            self.assertEqual(store.current_key(), "sk-TAMPERED")

    def test_auth_switch_restore_write_ok_but_verify_fails(self):
        # R2-04：恢复替换成功但读回核对失败 → 备份仍存在且内容=原始、
        # 消息引用的备份路径有效（不消耗备份、不虚报路径）
        p = self.home / ".local/share/opencode/auth.json"
        self._write_auth(p, go_key="sk-old-key-0004")
        with self._env():
            store = AuthStore()
            real_replace = os.replace
            state = {"n": 0}

            def fake_replace(src, dst):
                state["n"] += 1
                real_replace(src, dst)
                # 每次替换后都篡改目标（含恢复时的替换）→ 恢复后核对必然失败
                with open(dst, "w", encoding="utf-8") as f:
                    json.dump({"opencode-go": {"type": "api", "key": "sk-TAMPERED"}}, f)

            with mock.patch("auth_store.os.replace", side_effect=fake_replace):
                ok, msg, status = store.switch_to("sk-new-key-9999abcd")
            self.assertFalse(ok)
            self.assertEqual(status, "restore_failed")
            self.assertIn("备份位于", msg)
            backup_ref = msg.split("备份位于 ", 1)[1].strip()
            self.assertTrue(os.path.exists(backup_ref), "消息引用的备份路径必须存在")
            baks = list(p.parent.glob("auth.json.tokenhub.bak-*"))
            self.assertEqual(len(baks), 1)
            bak_data = json.loads(baks[0].read_text(encoding="utf-8"))
            self.assertEqual(bak_data["opencode-go"]["key"], "sk-old-key-0004", "备份内容=原始")

    def test_auth_switch_preserves_other_keys(self):
        p = self.home / ".local/share/opencode/auth.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "openai": {"type": "api", "key": "sk-openai"},
            "deepseek": {"type": "api", "key": "sk-deepseek"},
            "opencode-go": {"type": "api", "key": "sk-old"},
        }), encoding="utf-8")
        with self._env():
            store = AuthStore()
            ok, _, status = store.switch_to("sk-new-key-9999abcd")
            self.assertTrue(ok)
            self.assertEqual(status, "ok")
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(data["openai"]["key"], "sk-openai")
            self.assertEqual(data["deepseek"]["key"], "sk-deepseek")
            self.assertEqual(data["opencode-go"]["key"], "sk-new-key-9999abcd")

    def test_auth_backup_retention_keeps_five(self):
        p = self.home / ".local/share/opencode/auth.json"
        self._write_auth(p)
        with self._env():
            store = AuthStore()
            for i in range(7):
                ok, _, status = store.switch_to(f"sk-key-{i:04d}")
                self.assertTrue(ok)
                self.assertEqual(status, "ok")
            baks = sorted(p.parent.glob("auth.json.tokenhub.bak-*"))
            self.assertEqual(len(baks), 5)

    def test_auth_backup_failure_logs_diagnostic(self):
        p = self.home / ".local/share/opencode/auth.json"
        self._write_auth(p)
        with self._env():
            store = AuthStore()
            with mock.patch("auth_store.shutil.copy2", side_effect=OSError("denied")), \
                    mock.patch("auth_store.log_exception") as log:
                self.assertIsNone(store.backup())
            log.assert_called_once()

    def test_auth_temp_cleanup_failure_logs_diagnostic(self):
        with mock.patch("auth_store.os.path.exists", return_value=True), \
                mock.patch("auth_store.os.remove", side_effect=OSError("denied")), \
                mock.patch("auth_store.log_exception") as log:
            AuthStore._cleanup_tmp("tokenhub.tmp")
        log.assert_called_once()

    def test_auth_current_key_permission_error_is_diagnostic(self):
        p = self.home / ".local/share/opencode/auth.json"
        self._write_auth(p)
        with self._env(), mock.patch(
                "builtins.open", side_effect=PermissionError("denied")):
            store = AuthStore()
            self.assertIsNone(store.current_key())
            self.assertIsNotNone(store.last_error)
            self.assertIn("认证文件读取失败", store.last_error)
        with self._env():
            store = AuthStore()
            self.assertEqual(store.current_key(), "sk-current-key-0001")
            self.assertIsNone(store.last_error)

    def test_auth_current_key_corrupt_file_is_diagnostic(self):
        p = self.home / ".local/share/opencode/auth.json"
        p.write_text("{not-json", encoding="utf-8")
        with self._env():
            store = AuthStore()
            self.assertIsNone(store.current_key())
            self.assertIsNotNone(store.last_error)
            self.assertIn("认证文件读取失败", store.last_error)

    def test_auth_current_key_missing_file_or_field_is_not_error(self):
        missing = Path(self.tmp.name) / "missing-auth.json"
        with self._env():
            store = AuthStore(str(missing))
            self.assertIsNone(store.current_key())
            self.assertIsNone(store.last_error)
        p = self.home / ".local/share/opencode/auth.json"
        p.write_text(json.dumps({"openai": {"type": "api", "key": "sk-x"}}),
                     encoding="utf-8")
        with self._env():
            store = AuthStore()
            self.assertIsNone(store.current_key())
            self.assertIsNone(store.last_error)

    def test_auth_current_key_malformed_field_is_diagnostic(self):
        p = self.home / ".local/share/opencode/auth.json"
        p.write_text(json.dumps({"opencode-go": "not-an-object"}), encoding="utf-8")
        with self._env():
            store = AuthStore()
            self.assertIsNone(store.current_key())
            self.assertIn("字段格式错误", store.last_error)

    def test_auth_current_key_missing_file(self):
        with self._env():
            store = AuthStore()
            self.assertIsNone(store.current_key())
            self.assertEqual(store.mtime(), 0.0)

    def test_auth_bom_tolerated(self):
        # 外部工具写入 UTF-8 BOM 的 auth.json → 路径识别/读取/切换均正常
        p = self.home / ".local/share/opencode/auth.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps({
            "opencode-go": {"type": "api", "key": "sk-bom-current-0001"},
            "openai": {"type": "api", "key": "sk-openai"},
        }).encode("utf-8"))
        with self._env():
            store = AuthStore()
            self.assertEqual(store.path(), str(p))  # _has_opencode_go 容忍 BOM
            self.assertEqual(store.current_key(), "sk-bom-current-0001")
            # 切换读取 BOM 文件同样正常
            ok, msg, status = store.switch_to("sk-bom-new-9999abcd")
            self.assertTrue(ok, msg)
            self.assertEqual(status, "ok")
            self.assertEqual(store.current_key(), "sk-bom-new-9999abcd")

    # ---- R3-01：备份缺失时不得删除既有文件 ----

    def test_auth_rollback_keeps_existing_file_when_backup_missing(self):
        # 原文件存在 + 备份缺失 → 保留目标文件、restore_failed、不得删除
        d = self.home / ".local/share/opencode"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "auth.json"
        p.write_text(json.dumps({"opencode-go": {"type": "api", "key": "sk-current"}}),
                     encoding="utf-8")
        missing_bak = str(p) + ".tokenhub.bak-gone"
        with self._env():
            store = AuthStore()
            restored, msg = store._rollback(str(p), missing_bak, existed=True)
            self.assertFalse(restored)
            self.assertIn("待确认", msg)
            self.assertTrue(os.path.exists(p), "既有文件不得被删除")
            self.assertEqual(
                json.loads(p.read_text(encoding="utf-8"))["opencode-go"]["key"], "sk-current")

    def test_auth_rollback_deletes_new_file_when_original_absent(self):
        # 原文件本不存在 → 删除新文件即恢复（restored）
        d = self.home / ".local/share/opencode"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "auth.json"
        p.write_text(json.dumps({"opencode-go": {"type": "api", "key": "sk-new"}}),
                     encoding="utf-8")
        with self._env():
            store = AuthStore()
            restored, msg = store._rollback(str(p), None, existed=False)
            self.assertTrue(restored, msg)
            self.assertFalse(os.path.exists(p))

    # ---- R3-02：恢复成功判定 = 内容与备份一致，不依赖 Go 字段 ----

    def test_auth_rollback_verify_by_content_scenarios(self):
        # 旧 Go 有 key / 无 Go 字段 / Go 空对象 / 仅其他提供商 → 均 restored
        cases = [
            {"opencode-go": {"type": "api", "key": "sk-old"}},
            {"openai": {"type": "api", "key": "sk-x"}},
            {"opencode-go": {}, "openai": {"type": "api", "key": "x"}},
            {"deepseek": {"type": "api", "key": "sk-d"}},
        ]
        with self._env():
            store = AuthStore()
            for i, content in enumerate(cases):
                d = self.home / ".local/share/opencode"
                p = d / f"auth-{i}.json"
                bak = d / f"auth-{i}.json.tokenhub.bak-1"
                p.write_text(json.dumps({"opencode-go": {"type": "api", "key": "sk-tampered"}}),
                             encoding="utf-8")
                bak.write_text(json.dumps(content), encoding="utf-8")
                restored, msg = store._rollback(str(p), str(bak), existed=True)
                self.assertTrue(restored, f"case {i}: {msg}")
                self.assertEqual(p.read_bytes(), bak.read_bytes(), f"case {i}: 内容=备份")
                self.assertTrue(bak.exists(), f"case {i}: 备份保留")

    def test_auth_rollback_unparseable_backup_fails(self):
        # 备份不可解析 → 明确报错（restore_failed），目标文件保持现状、备份保留
        d = self.home / ".local/share/opencode"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "auth.json"
        bak = d / "auth.json.tokenhub.bak-1"
        p.write_text(json.dumps({"opencode-go": {"type": "api", "key": "sk-tampered"}}),
                     encoding="utf-8")
        bak.write_bytes(b"{broken json")
        with self._env():
            store = AuthStore()
            restored, msg = store._rollback(str(p), str(bak), existed=True)
            self.assertFalse(restored)
            self.assertIn("待确认", msg)
            self.assertTrue(bak.exists(), "不可解析备份保留")
            self.assertEqual(
                json.loads(p.read_text(encoding="utf-8"))["opencode-go"]["key"], "sk-tampered")


# ---------------------------------------------------------------------------
# ControlClient
# ---------------------------------------------------------------------------
class ControlClientTests(unittest.TestCase):
    def setUp(self):
        STUB["token"] = "tok123"
        STUB["switch_delay"] = 0.0
        STUB["switch_http"] = 200
        STUB["switch_ok"] = True
        STUB["switch_uncertain"] = False

    def test_control_discover_from_env(self):
        with mock.patch.dict(os.environ, {
            "TOKENHUB_CONTROL_PORT": "8123",
            "TOKENHUB_CONTROL_TOKEN": "tok123",
        }, clear=False):
            info = ControlClient().discover()
        self.assertIsNotNone(info)
        self.assertEqual(info.port, 8123)
        self.assertEqual(info.token, "tok123")

    def test_control_discover_missing(self):
        with mock.patch.dict(os.environ, {
            "TOKENHUB_CONTROL_PORT": "",
            "TOKENHUB_CONTROL_TOKEN": "",
        }, clear=False):
            self.assertIsNone(ControlClient().discover())

    def test_control_health_ok(self):
        server, port = _start_stub()
        self.addCleanup(_stop_stub, server)
        with mock.patch.dict(os.environ, {
            "TOKENHUB_CONTROL_PORT": str(port),
            "TOKENHUB_CONTROL_TOKEN": "tok123",
        }, clear=False):
            client = ControlClient()
            info = client.discover()
            self.assertIsNotNone(info)
            self.assertTrue(client.health(info))

    def test_control_health_bad_token(self):
        server, port = _start_stub()
        self.addCleanup(_stop_stub, server)
        client = ControlClient()
        self.assertFalse(client.health(ControlInfo(port, "wrong-token")))

    def test_control_health_unreachable(self):
        client = ControlClient()
        self.assertFalse(client.health(ControlInfo(1, "tok")))

    def test_control_switch_roundtrip(self):
        server, port = _start_stub()
        self.addCleanup(_stop_stub, server)
        client = ControlClient()
        ok, msg, status = client.switch(ControlInfo(port, "tok123"), "sk-test-key-abcdef")
        self.assertTrue(ok)
        # U09：method=file → 冻结消息"认证文件已更新"，status=ok
        self.assertEqual(status, "ok")
        self.assertEqual(msg, "认证文件已更新")
        # 401 → denied
        ok, msg, status = client.switch(ControlInfo(port, "wrong-token"), "sk-test-key-abcdef")
        self.assertFalse(ok)
        self.assertEqual(status, "denied")
        self.assertEqual(msg, "控制接口拒绝（401）")
        # 服务端拒绝（200 + ok:false）→ denied
        STUB["switch_ok"] = False
        ok, msg, status = client.switch(ControlInfo(port, "tok123"), "sk-test-key-abcdef")
        self.assertFalse(ok)
        self.assertEqual(status, "denied")

    def test_control_switch_200_uncertain_keeps_timeout_status(self):
        """插件 200 + ok:false + uncertain/status 必须保持 timeout_uncertain。"""
        STUB["switch_uncertain"] = True
        server, port = _start_stub()
        self.addCleanup(_stop_stub, server)
        client = ControlClient()
        ok, msg, status = client.switch(ControlInfo(port, "tok123"), "sk-test-key-abcdef")
        self.assertFalse(ok)
        self.assertEqual(status, "timeout_uncertain")
        self.assertIn("无法确认切换结果", msg)

    def test_control_switch_accepts_oc_sk_key(self):
        # stub 校验器接受 oc_sk_ 前缀（与插件 /switch 一致）
        server, port = _start_stub()
        self.addCleanup(_stop_stub, server)
        client = ControlClient()
        ok, msg, status = client.switch(ControlInfo(port, "tok123"), "oc_sk_ABCDEFGHIJKLMNOPQRSTUVwxYZ")
        self.assertTrue(ok)
        self.assertEqual(status, "ok")
        # 无效前缀 → 拒绝
        ok, msg, status = client.switch(ControlInfo(port, "tok123"), "bad-key")
        self.assertFalse(ok)
        self.assertEqual(status, "denied")

    def test_control_switch_timeout(self):
        # TH-05：外层超时 8s（> 插件内层 3s）；超时 → timeout_uncertain 而非失败
        STUB["switch_delay"] = 10.0
        server, port = _start_stub()
        self.addCleanup(_stop_stub, server)
        client = ControlClient()
        t0 = time.monotonic()
        ok, msg, status = client.switch(ControlInfo(port, "tok123"), "sk-test-key-abcdef")
        elapsed = time.monotonic() - t0
        self.assertFalse(ok)
        self.assertLess(elapsed, 9.5)
        self.assertGreaterEqual(elapsed, 7.5)  # 确实等到了外层预算而非立即失败
        self.assertEqual(status, "timeout_uncertain")
        self.assertEqual(msg, "控制接口超时，切换结果待确认")

    def test_control_switch_unreachable(self):
        client = ControlClient()
        ok, msg, status = client.switch(ControlInfo(1, "tok"), "sk-test-key-abcdef")
        self.assertFalse(ok)
        self.assertEqual(status, "unreachable")
        self.assertEqual(msg, "控制接口不可达")

    def test_control_switch_timeout_classification(self):
        # R3-05 五类异常分类（mock urlopen 抛出）
        client = ControlClient()
        info = ControlInfo(8123, "tok")
        # 1) 直接超时
        with mock.patch("control_client.urlopen", side_effect=TimeoutError("timed out")):
            ok, msg, status = client.switch(info, "sk-test-key-abcdef")
        self.assertEqual(status, "timeout_uncertain")
        self.assertEqual(msg, "控制接口超时，切换结果待确认")
        # 2) URLError 包装超时
        with mock.patch("control_client.urlopen",
                        side_effect=urllib.error.URLError(TimeoutError("timed out"))):
            ok, msg, status = client.switch(info, "sk-test-key-abcdef")
        self.assertEqual(status, "timeout_uncertain")
        # 3) 连接拒绝（URLError 包装 + 裸）
        with mock.patch("control_client.urlopen",
                        side_effect=urllib.error.URLError(ConnectionRefusedError())):
            ok, msg, status = client.switch(info, "sk-test-key-abcdef")
        self.assertEqual(status, "unreachable")
        with mock.patch("control_client.urlopen", side_effect=ConnectionRefusedError()):
            ok, msg, status = client.switch(info, "sk-test-key-abcdef")
        self.assertEqual(status, "unreachable")
        # 4) 响应读取中断（连接建立后断开，服务端可能已执行）→ 待确认
        with mock.patch("control_client.urlopen", side_effect=ConnectionResetError("reset")):
            ok, msg, status = client.switch(info, "sk-test-key-abcdef")
        self.assertEqual(status, "timeout_uncertain")
        with mock.patch("control_client.urlopen", side_effect=http.client.RemoteDisconnected("gone")):
            ok, msg, status = client.switch(info, "sk-test-key-abcdef")
        self.assertEqual(status, "timeout_uncertain")
        with mock.patch("control_client.urlopen",
                        side_effect=urllib.error.URLError(ConnectionResetError("reset"))):
            ok, msg, status = client.switch(info, "sk-test-key-abcdef")
        self.assertEqual(status, "timeout_uncertain")
        # 5) 401 → denied
        with mock.patch("control_client.urlopen",
                        side_effect=urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)):
            ok, msg, status = client.switch(info, "sk-test-key-abcdef")
        self.assertEqual(status, "denied")
        self.assertEqual(msg, "控制接口拒绝（401）")
        # 其他非网络异常（如 ValueError）→ unreachable（保守兜底）
        with mock.patch("control_client.urlopen", side_effect=ValueError("weird")):
            ok, msg, status = client.switch(info, "sk-test-key-abcdef")
        self.assertEqual(status, "unreachable")


# ---------------------------------------------------------------------------
# Logger（§7.3：无活动异常时不得输出 NoneType: None）
# ---------------------------------------------------------------------------
class LoggerTests(unittest.TestCase):
    def setUp(self):
        import logging
        from logger import logger

        self._logger = logger
        self.records = []

        class _Recorder(logging.Handler):
            def __init__(self, records):
                super().__init__()
                self.records = records

            def emit(self, record):
                self.records.append(record)

        self._handler = _Recorder(self.records)
        self._logger.addHandler(self._handler)

    def tearDown(self):
        self._logger.removeHandler(self._handler)

    def test_log_exception_without_active_exception_uses_error(self):
        import logging
        from logger import log_exception

        log_exception("no-active-exc", RuntimeError("boom"))
        self.assertEqual(self.records[-1].levelno, logging.ERROR)
        message = self.records[-1].getMessage()
        self.assertNotIn("NoneType", message)
        self.assertIn("no-active-exc", message)  # 位置信息保留
        self.assertIsNone(self.records[-1].exc_info, "无活动异常时不应携带堆栈")

    def test_log_exception_inside_except_uses_exception(self):
        import logging
        from logger import log_exception

        try:
            raise ValueError("inner failure")
        except ValueError as e:
            log_exception("inside-except", e)
        record = self.records[-1]
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertIsNotNone(record.exc_info, "活动异常应携带堆栈信息")
        self.assertNotIn("NoneType", record.getMessage())


if __name__ == "__main__":
    unittest.main()