"""日志配置模块。

统一初始化 ``stock_selector`` 的日志：同时输出到控制台与文件，
避免各模块自行 ``print`` 导致信息分散。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

LOGGER_NAME = "stock_selector"
_CONFIGURED = False

# 日志格式：时间 | 级别 | 模块:行号 | 内容
_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s:%(lineno)d | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str | Path] = None,
    console: bool = True,
    force: bool = False,
) -> logging.Logger:
    """初始化根日志器。

    :param level: 日志级别字符串，如 ``"DEBUG"`` / ``"INFO"``
    :param log_file: 日志文件绝对路径；为 None 时只输出到控制台
    :param console: 是否输出到控制台
    :param force: 是否强制重新配置（测试场景使用）
    :return: 配置好的 :class:`logging.Logger`
    """
    global _CONFIGURED
    logger = logging.getLogger(LOGGER_NAME)

    if _CONFIGURED and not force:
        return logger

    # 清理已有 handler，避免重复输出
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover
            pass

    logger.setLevel(_parse_level(level))
    logger.propagate = False

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    if console:
        stream = logging.StreamHandler(stream=sys.stdout)
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    if log_file:
        try:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            # 单文件最大 10MB，保留 5 个备份
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception as exc:  # pragma: no cover - 依赖文件系统
            logger.warning("日志文件初始化失败，仅使用控制台输出：%s", exc)

    _CONFIGURED = True
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取带命名空间的子日志器。

    :param name: 子模块名，例如 ``"tdx_reader"``；为空则返回根日志器
    """
    if not _CONFIGURED:
        # 未显式初始化时给出一个可用的默认配置
        setup_logging()
    if name:
        return logging.getLogger(f"{LOGGER_NAME}.{name}")
    return logging.getLogger(LOGGER_NAME)


def setup_from_config(force: bool = False) -> logging.Logger:
    """根据 ``config.yaml`` 初始化日志。"""
    # 延迟导入，避免循环依赖
    from .config import get_config

    cfg = get_config()
    log_file = cfg.get("logging.file")
    resolved = str(cfg.resolve_path(log_file)) if log_file else None
    return setup_logging(
        level=str(cfg.get("logging.level", "INFO")),
        log_file=resolved,
        console=bool(cfg.get("logging.console", True)),
        force=force,
    )


def _parse_level(level: str) -> int:
    """把字符串级别转为 logging 常量，非法值回退到 INFO。"""
    value = getattr(logging, str(level).upper(), None)
    return value if isinstance(value, int) else logging.INFO
