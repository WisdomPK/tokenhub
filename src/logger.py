# -*- coding: utf-8 -*-
"""TokenHub v3 日志模块。

UTF-8；日志文件 next-to-exe（开发期=项目根），不可写回退 %LOCALAPPDATA%\\TokenHub\\tokenhub.log。
保留 V2 便捷函数名（调用点稳定），文案全部重新编写（UTF-8 正确编码）。
"""

import logging
import logging.handlers
import os
import sys

if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 开发期=项目根

LOG_FILE = os.path.join(_APP_DIR, "tokenhub.log")
_FALLBACK_LOG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "TokenHub"
)
_FALLBACK_LOG_FILE = os.path.join(_FALLBACK_LOG_DIR, "tokenhub.log")


def _resolve_log_file() -> str:
    try:
        with open(LOG_FILE, "a", encoding="utf-8"):
            pass
        return LOG_FILE
    except OSError:
        try:
            os.makedirs(_FALLBACK_LOG_DIR, exist_ok=True)
        except OSError:
            pass
        return _FALLBACK_LOG_FILE


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("TokenHub")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    try:
        # §7.4：轮转日志，单文件上限 1MB，保留 3 份历史（tokenhub.log.1/.2/.3）
        file_handler = logging.handlers.RotatingFileHandler(
            _resolve_log_file(), maxBytes=1_000_000, backupCount=3,
            encoding="utf-8", mode="a")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(file_handler)
    except OSError:
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(console)
    return logger


logger = setup_logger()


def log_startup():
    logger.info("=" * 60)
    logger.info("TokenHub v3 启动")
    logger.info("=" * 60)


def log_config_load(account_count=None):
    if account_count is None:
        logger.info("配置加载完成")
    else:
        logger.info(f"配置加载完成 - 账号数: {account_count}")


def log_config_save(account_count=None):
    if account_count is None:
        logger.info("配置已保存")
    else:
        logger.info(f"配置已保存 - 账号数: {account_count}")


def log_api_request(url: str):
    logger.debug(f"API 请求: {url[:120]}")


def log_api_response(status_code: int, data: dict | None = None):
    logger.info(f"API 响应: HTTP {status_code}")
    if isinstance(data, dict):
        usage = data.get("usage")
        if isinstance(usage, dict):
            for name in ("rolling", "weekly", "monthly"):
                item = usage.get(name)
                if isinstance(item, dict):
                    logger.info(f"  {name}: {item.get('percent')}% | {item.get('resetsAt')}")


def log_api_error(error: str):
    logger.error(f"API 请求失败: {error}")


def log_data_update(error_state: bool = False):
    if error_state:
        logger.warning("数据更新: 显示错误状态（离线）")
    else:
        logger.info("数据更新: 悬浮窗数据已刷新")


def log_refresh_triggered():
    logger.debug("定时刷新触发")


def log_single_instance_blocked():
    logger.warning("检测到另一 TokenHub 实例已在运行，当前实例退出")


def log_ui_action(action: str):
    logger.debug(f"UI 操作: {action}")


def log_exception(location: str, exception: Exception):
    """§7.3：有活动异常（当前在处理 except）→ logger.exception 记录堆栈；
    否则（业务错误、无 traceback）→ logger.error，不输出 NoneType: None。"""
    if sys.exc_info()[0] is not None:
        logger.exception(f"异常发生位置 [{location}]: {exception}")
    else:
        logger.error(f"异常发生位置 [{location}]: {exception}")