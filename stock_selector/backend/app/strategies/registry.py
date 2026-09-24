"""策略注册表。

提供策略的注册、查询与实例化；新策略只需继承 :class:`BaseStrategy`
并调用 :func:`register` 即可被 CLI / Web UI 自动发现。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from ..core.config import get_config
from ..core.logging import get_logger
from .base import BaseStrategy
from .ma_cross import MaCrossStrategy
from .rsi import RsiStrategy
from .volume_breakout import VolumeBreakoutStrategy
from .volume_surge import VolumeSurgeStrategy

logger = get_logger("strategy_registry")

#: 策略注册表：{策略名: 策略类}
STRATEGIES: Dict[str, Type[BaseStrategy]] = {}


def register(cls: Type[BaseStrategy]) -> Type[BaseStrategy]:
    """注册策略类。

    :param cls: 继承自 :class:`BaseStrategy` 的策略类
    :return: 原类（可作为装饰器使用）
    """
    if not issubclass(cls, BaseStrategy):
        raise TypeError(f"{cls} 必须继承 BaseStrategy")
    if not cls.name or cls.name == "base":
        raise ValueError(f"{cls.__name__} 未设置有效的 name 属性")
    if cls.name in STRATEGIES and STRATEGIES[cls.name] is not cls:
        logger.warning("策略 %s 被重复注册，后者覆盖前者", cls.name)
    STRATEGIES[cls.name] = cls
    return cls


# 注册内置策略
register(MaCrossStrategy)
register(RsiStrategy)
register(VolumeBreakoutStrategy)
register(VolumeSurgeStrategy)


def get_strategy_class(name: str) -> Type[BaseStrategy]:
    """按名称获取策略类。

    :param name: 策略名
    :raises KeyError: 策略不存在
    """
    key = (name or "").strip()
    if key not in STRATEGIES:
        raise KeyError(
            f"未知策略：{name}，可选：{', '.join(sorted(STRATEGIES)) or '无'}"
        )
    return STRATEGIES[key]


def get_strategy(
    name: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    use_config: bool = True,
) -> BaseStrategy:
    """实例化策略。

    参数优先级：``params`` 显式传入 > ``config.yaml`` 中的 ``strategy.<name>`` > 类默认值。

    :param name: 策略名；为空时使用 ``strategy.default``
    :param params: 显式参数覆盖
    :param use_config: 是否合并配置文件中的默认参数
    :return: 策略实例
    """
    cfg = get_config()
    key = name or str(cfg.get("strategy.default", "ma_cross"))
    cls = get_strategy_class(key)

    merged: Dict[str, Any] = {}
    if use_config:
        cfg_params = cfg.get(f"strategy.{key}", {})
        if isinstance(cfg_params, dict):
            merged.update(cfg_params)
    if params:
        merged.update({k: v for k, v in params.items() if v is not None})

    return cls(merged)


def list_strategies() -> List[Dict[str, Any]]:
    """列出全部已注册策略的元信息（供 UI 渲染参数表单）。"""
    return [cls.describe() for cls in sorted(STRATEGIES.values(), key=lambda c: c.name)]


def strategy_names() -> List[str]:
    """返回全部策略名。"""
    return sorted(STRATEGIES.keys())


def strategy_names_requiring_basics() -> List[str]:
    """返回需要股票基础信息（流通股本）的策略名。"""
    return sorted(
        name for name, cls in STRATEGIES.items() if getattr(cls, "requires_basics", False)
    )
