# -*- coding: utf-8 -*-
"""TokenHub v3 插件本地控制 API 客户端（探测 / 健康检查 / 切换）。

接口（spec 3.4）：GET /health、POST /switch，header X-TokenHub-Token。

TH-05 超时预算：切换链为 额度条 → 插件 → opencode 认证接口（内层 3s）+ 文件降级。
外层 SWITCH_TIMEOUT 必须大于内层处理预算（3s 内层 + 降级 + 响应时间），故取 8s；
health 为轻量探测，保持 3s。

R3-05 异常分类（冻结）：
- 超时（直接 TimeoutError / URLError 包装超时）与请求已发出后的响应读取中断
  （连接重置/中止/RemoteDisconnected 等，服务端可能已执行）→ timeout_uncertain；
- 连接建立前的拒绝（ConnectionRefused 等）→ unreachable；
- 401 → denied；
- HTTP 200 但响应含 uncertain:true 或 status=timeout_uncertain → timeout_uncertain
  （插件已确认无法写入/无法确认，保留其错误消息；不得降级为 denied）。
不得把不确定场景归为 unreachable（不自动重试、不虚假声称失败）。

U09 成功消息（冻结，仅用于对接 main 的 toast 组合）：
- method=="api" → "切换请求已成功"；method=="file" → "认证文件已更新"；
  缺失 → "已完成"。
任何异常返回 False / (False, message, status)，不抛异常。
"""

import http.client
import json
import os
import urllib.error
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE_URL = "http://127.0.0.1"
HEALTH_TIMEOUT = 3.0
SWITCH_TIMEOUT = 8.0  # 外层预算 > 插件内层 3s（含文件降级与响应时间）


@dataclass
class ControlInfo:
    port: int
    token: str


class ControlClient:
    def discover(self) -> ControlInfo | None:
        """从环境变量 TOKENHUB_CONTROL_PORT / TOKENHUB_CONTROL_TOKEN 读取。"""
        port = os.environ.get("TOKENHUB_CONTROL_PORT")
        token = os.environ.get("TOKENHUB_CONTROL_TOKEN")
        if not port or not token:
            return None
        try:
            return ControlInfo(int(port), token)
        except ValueError:
            return None

    def health(self, info: ControlInfo) -> bool:
        try:
            request = Request(
                f"{BASE_URL}:{info.port}/health",
                headers={"X-TokenHub-Token": info.token},
            )
            with urlopen(request, timeout=HEALTH_TIMEOUT) as resp:
                if resp.status != 200:
                    return False
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("ok"))
        except Exception:
            return False

    @staticmethod
    def _timeout_uncertain() -> tuple[bool, str, str]:
        """超时/响应中断：服务端可能已执行，结果待确认（不归为 unreachable）。"""
        return (False, "控制接口超时，切换结果待确认", "timeout_uncertain")

    @staticmethod
    def _unreachable() -> tuple[bool, str, str]:
        return (False, "控制接口不可达", "unreachable")

    def switch(self, info: ControlInfo, key: str) -> tuple[bool, str, str]:
        """POST /switch {key}。返回 (ok, message, status)。

        status ∈ {"ok", "denied", "unreachable", "timeout_uncertain"}。
        """
        try:
            body = json.dumps({"key": key}).encode("utf-8")
            request = Request(
                f"{BASE_URL}:{info.port}/switch",
                data=body,
                method="POST",
                headers={
                    "X-TokenHub-Token": info.token,
                    "Content-Type": "application/json",
                },
            )
            with urlopen(request, timeout=SWITCH_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if resp.status == 200 and data.get("ok"):
                    method = data.get("method") if isinstance(data, dict) else None
                    if method == "api":
                        how = "切换请求已成功"
                    elif method == "file":
                        how = "认证文件已更新"
                    else:
                        how = "已完成"
                    return (True, how, "ok")
                error = data.get("error") if isinstance(data, dict) else None
                if isinstance(data, dict) and (
                        data.get("uncertain") is True
                        or data.get("status") == "timeout_uncertain"):
                    # 插件已确认“超时且无法确认”：保留待确认语义，不能降级为 denied
                    return (False, error or "控制接口超时，切换结果待确认",
                            "timeout_uncertain")
                return (False, error or "控制接口拒绝", "denied")
        except HTTPError as e:
            if e.code == 401:
                return (False, "控制接口拒绝（401）", "denied")
            return (False, f"控制接口拒绝（HTTP {e.code}）", "denied")
        except TimeoutError:
            # 直接超时（socket.timeout 在 3.10+ 即 TimeoutError）
            return self._timeout_uncertain()
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, TimeoutError):
                # R3-05：URLError 包装的超时
                return self._timeout_uncertain()
            if isinstance(reason, ConnectionRefusedError):
                return self._unreachable()
            if isinstance(reason, ConnectionError):
                # 连接建立后重置/中止 → 服务端可能已执行
                return self._timeout_uncertain()
            return self._unreachable()
        except ConnectionRefusedError:
            return self._unreachable()
        except (ConnectionError, http.client.HTTPException):
            # 响应读取中断（RemoteDisconnected/IncompleteRead 等）→ 不确定
            return self._timeout_uncertain()
        except Exception:
            return self._unreachable()