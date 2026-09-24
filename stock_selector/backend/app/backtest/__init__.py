"""回测模块：撮合、绩效、引擎。"""

from .broker import Broker, Order, Position, Trade, build_broker
from .engine import BacktestEngine, BacktestResult, export_trades_csv
from .performance import (
    METRIC_LABELS,
    PerformanceResult,
    compute_metrics,
    drawdown_curve,
    max_drawdown_info,
)

__all__ = [
    "Broker",
    "Order",
    "Position",
    "Trade",
    "build_broker",
    "BacktestEngine",
    "BacktestResult",
    "export_trades_csv",
    "PerformanceResult",
    "compute_metrics",
    "drawdown_curve",
    "max_drawdown_info",
    "METRIC_LABELS",
]
