"""策略模块：基类、内置示例策略与注册表。"""

from .base import (
    SIGNAL_BUY,
    SIGNAL_HOLD,
    SIGNAL_SELL,
    SIGNAL_TEXT,
    BaseStrategy,
    SignalDetail,
)
from .ma_cross import MaCrossStrategy
from .registry import (
    STRATEGIES,
    get_strategy,
    get_strategy_class,
    list_strategies,
    register,
    strategy_names,
)
from .rsi import RsiStrategy
from .volume_breakout import VolumeBreakoutStrategy
from .volume_surge import VolumeSurgeStrategy

__all__ = [
    "BaseStrategy",
    "SignalDetail",
    "SIGNAL_BUY",
    "SIGNAL_SELL",
    "SIGNAL_HOLD",
    "SIGNAL_TEXT",
    "MaCrossStrategy",
    "RsiStrategy",
    "VolumeBreakoutStrategy",
    "VolumeSurgeStrategy",
    "STRATEGIES",
    "register",
    "get_strategy",
    "get_strategy_class",
    "list_strategies",
    "strategy_names",
]
