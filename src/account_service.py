# -*- coding: utf-8 -*-
"""桌面账号切换服务：控制接口发现/健康检查/切换与 auth 文件直写。

结构化结果供 UI 翻译为提示；UI 不解析 message 做决策。
"""

from dataclasses import dataclass

from control_client import ControlClient
from logger import log_exception

OUTCOMES = frozenset({
    "ok", "unaffected", "restored", "restore_failed",
    "denied", "unreachable", "timeout_uncertain",
})


@dataclass(frozen=True)
class SwitchResult:
    ok: bool
    outcome: str
    method: str  # api / file / unknown
    message: str


class AccountService:
    def __init__(self, store, ctrl: ControlClient | None = None):
        self.store = store
        self.ctrl = ctrl if ctrl is not None else ControlClient()

    def switch(self, key: str) -> SwitchResult:
        """优先控制接口；不可用时直写 auth 文件。不抛异常。"""
        info = None
        use_control = False
        try:
            info = self.ctrl.discover()
            if info is not None:
                use_control = bool(self.ctrl.health(info))
        except Exception as e:
            # 探测异常不静默：记录后按控制接口不可用处理（直写仍带读回校验）
            log_exception("account_service_discover", e)
            use_control = False

        if use_control:
            try:
                ok, message, status = self.ctrl.switch(info, key)
            except Exception as e:
                log_exception("account_service_switch", e)
                return SwitchResult(False, "unreachable", "api",
                                    f"切换失败（{e}）")
            method = "api"
        else:
            try:
                ok, message, status = self.store.switch_to(key)
            except Exception as e:
                log_exception("account_service_file", e)
                return SwitchResult(False, "unreachable", "unknown",
                                    f"切换失败（{e}）")
            method = "file"
        if status == "timeout_uncertain":
            return self._verify_timeout(key, message, method)
        return SwitchResult(bool(ok), self._outcome(ok, status), method, message)

    def _verify_timeout(self, key: str, message: str, method: str) -> SwitchResult:
        """超时先核对目标文件；确认才升级成功，否则保持待确认且不再写。"""
        current = None
        try:
            current = self.store.current_key()
        except Exception as e:
            log_exception("account_service_verify", e)
        if current == key:
            return SwitchResult(True, "ok", method, "已核对文件中的目标账号")
        return SwitchResult(False, "timeout_uncertain", method,
                            message or "控制接口超时，切换结果待确认")

    @staticmethod
    def _outcome(ok, status: str) -> str:
        if status in OUTCOMES:
            return status
        return "ok" if ok else "unreachable"
