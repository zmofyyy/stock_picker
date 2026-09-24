"""追踪模块：关注池、状态机、存储、追踪器、通知、报告。"""

from .notifier import Notification, Notifier
from .report import Report, ReportBuilder, markdown_to_html
from .state_machine import (
    DEFAULT_STATES,
    HOLDING_STATES,
    RISK_DANGER,
    RISK_NORMAL,
    RISK_WARNING,
    Decision,
    RiskAssessment,
    StateContext,
    StateMachine,
    evaluate_risk,
)
from .storage import TrackerStorage
from .tracker import StockTracker, UpdateResult
from .watchlist import Watchlist

__all__ = [
    "TrackerStorage",
    "Watchlist",
    "StockTracker",
    "UpdateResult",
    "StateMachine",
    "StateContext",
    "RiskAssessment",
    "Decision",
    "DEFAULT_STATES",
    "HOLDING_STATES",
    "RISK_NORMAL",
    "RISK_WARNING",
    "RISK_DANGER",
    "evaluate_risk",
    "Notifier",
    "Notification",
    "ReportBuilder",
    "Report",
    "markdown_to_html",
]
