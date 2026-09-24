"""SQLAlchemy ORM 模型定义。

SQLite 默认作为状态存储，包含以下表：
    * ``watchlist``        关注池
    * ``tracking_state``   当前追踪状态（每只股票一行）
    * ``tracking_history`` 状态变迁历史
    * ``signals``          策略信号记录
    * ``notes``            备注
    * ``alerts``           提醒记录

所有表都支持按股票代码、日期、状态、策略查询。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now() -> datetime:
    """统一的时间获取函数（便于测试打桩）。"""
    return datetime.now()


class Base(DeclarativeBase):
    """ORM 基类。"""


# ----------------------------------------------------------------------
# 关注池
# ----------------------------------------------------------------------
class WatchItem(Base):
    """关注池条目。"""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="")
    group: Mapped[str] = mapped_column(String(64), default="默认分组", index=True)
    tags: Mapped[List[str]] = mapped_column(JSON, default=list)
    note: Mapped[str] = mapped_column(Text, default="")

    strategy: Mapped[str] = mapped_column(String(64), default="ma_cross")
    strategy_params: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)

    cost_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    shares: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    target_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name or self.code,
            "group": self.group,
            "tags": list(self.tags or []),
            "note": self.note or "",
            "strategy": self.strategy,
            "strategy_params": dict(self.strategy_params or {}),
            "cost_price": self.cost_price,
            "shares": self.shares,
            "target_price": self.target_price,
            "stop_price": self.stop_price,
            "enabled": bool(self.enabled),
            "source": self.source,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


# ----------------------------------------------------------------------
# 当前状态
# ----------------------------------------------------------------------
class TrackingState(Base):
    """股票当前追踪状态。"""

    __tablename__ = "tracking_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="")
    group: Mapped[str] = mapped_column(String(64), default="默认分组")

    state: Mapped[str] = mapped_column(String(32), default="候选", index=True)
    prev_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    state_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    state_reason: Mapped[str] = mapped_column(Text, default="")

    strategy: Mapped[str] = mapped_column(String(64), default="")
    signal: Mapped[int] = mapped_column(Integer, default=0)
    signal_text: Mapped[str] = mapped_column(String(16), default="无信号")
    signal_reason: Mapped[str] = mapped_column(Text, default="")
    is_new_signal: Mapped[bool] = mapped_column(Boolean, default=False)

    data_date: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    prev_close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pct_change: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    risk_level: Mapped[str] = mapped_column(String(16), default="normal")
    risk_flags: Mapped[List[str]] = mapped_column(JSON, default=list)

    cost_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    shares: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    unrealized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unrealized_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hold_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_drawdown: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    factors: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    extra: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    __table_args__ = (Index("ix_tracking_state_state_date", "state", "data_date"),)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "code": self.code,
            "name": self.name or self.code,
            "group": self.group,
            "state": self.state,
            "prev_state": self.prev_state,
            "state_changed_at": _iso(self.state_changed_at),
            "state_reason": self.state_reason or "",
            "strategy": self.strategy,
            "signal": self.signal,
            "signal_text": self.signal_text,
            "signal_reason": self.signal_reason or "",
            "is_new_signal": bool(self.is_new_signal),
            "data_date": self.data_date,
            "price": self.price,
            "prev_close": self.prev_close,
            "pct_change": self.pct_change,
            "volume": self.volume,
            "amount": self.amount,
            "risk_level": self.risk_level,
            "risk_flags": list(self.risk_flags or []),
            "cost_price": self.cost_price,
            "shares": self.shares,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pct": self.unrealized_pct,
            "hold_days": self.hold_days,
            "max_drawdown": self.max_drawdown,
            "factors": dict(self.factors or {}),
            "extra": dict(self.extra or {}),
            "updated_at": _iso(self.updated_at),
        }


# ----------------------------------------------------------------------
# 状态变迁历史
# ----------------------------------------------------------------------
class TrackingHistory(Base):
    """状态变迁历史记录。"""

    __tablename__ = "tracking_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="")
    old_state: Mapped[str] = mapped_column(String(32), default="")
    new_state: Mapped[str] = mapped_column(String(32), index=True, default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    trade_date: Mapped[Optional[str]] = mapped_column(String(16), index=True, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    strategy: Mapped[str] = mapped_column(String(64), default="")
    trigger_type: Mapped[str] = mapped_column(String(32), default="auto")  # auto/manual/replay
    factors: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name or self.code,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "changed_at": _iso(self.changed_at),
            "trade_date": self.trade_date,
            "reason": self.reason or "",
            "strategy": self.strategy,
            "trigger_type": self.trigger_type,
            "factors": dict(self.factors or {}),
            "price": self.price,
            "note": self.note or "",
        }


# ----------------------------------------------------------------------
# 信号记录
# ----------------------------------------------------------------------
class SignalRecord(Base):
    """策略信号记录。"""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="")
    strategy: Mapped[str] = mapped_column(String(64), index=True, default="")
    signal: Mapped[int] = mapped_column(Integer, default=0)
    signal_text: Mapped[str] = mapped_column(String(16), default="")
    trade_date: Mapped[str] = mapped_column(String(16), index=True, default="")
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pct_change: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    factors: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name or self.code,
            "strategy": self.strategy,
            "signal": self.signal,
            "signal_text": self.signal_text,
            "trade_date": self.trade_date,
            "price": self.price,
            "pct_change": self.pct_change,
            "reason": self.reason or "",
            "is_new": bool(self.is_new),
            "factors": dict(self.factors or {}),
            "created_at": _iso(self.created_at),
        }


# ----------------------------------------------------------------------
# 备注
# ----------------------------------------------------------------------
class Note(Base):
    """股票备注。"""

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(64), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "code": self.code,
            "content": self.content,
            "author": self.author,
            "created_at": _iso(self.created_at),
        }


# ----------------------------------------------------------------------
# 提醒
# ----------------------------------------------------------------------
class Alert(Base):
    """提醒记录。"""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), index=True, default="")
    name: Mapped[str] = mapped_column(String(64), default="")
    level: Mapped[str] = mapped_column(String(16), default="info", index=True)  # info/warning/danger
    category: Mapped[str] = mapped_column(String(32), default="state_change", index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    strategy: Mapped[str] = mapped_column(String(64), default="")
    trade_date: Mapped[Optional[str]] = mapped_column(String(16), index=True, nullable=True)
    trigger_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    delivered: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    acked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name or self.code,
            "level": self.level,
            "category": self.category,
            "title": self.title,
            "message": self.message,
            "strategy": self.strategy,
            "trade_date": self.trade_date,
            "trigger_value": self.trigger_value,
            "delivered": dict(self.delivered or {}),
            "acked": bool(self.acked),
            "created_at": _iso(self.created_at),
        }


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def _iso(value: Optional[datetime]) -> Optional[str]:
    """datetime → ISO 字符串。"""
    if value is None:
        return None
    try:
        return value.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # pragma: no cover
        return str(value)


def create_db_engine(url: str, echo: bool = False):
    """创建数据库引擎并确保目录存在。

    :param url: SQLAlchemy 数据库 URL
    :param echo: 是否打印 SQL
    """
    if url.startswith("sqlite:///"):
        db_path = url.replace("sqlite:///", "", 1)
        if db_path:
            from pathlib import Path

            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, echo=echo, future=True, connect_args=connect_args)


def create_session_factory(engine) -> sessionmaker:
    """创建 Session 工厂。"""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(engine) -> None:
    """创建全部数据表（幂等）。"""
    Base.metadata.create_all(engine)


ALL_TABLES = [
    "watchlist",
    "tracking_state",
    "tracking_history",
    "signals",
    "notes",
    "alerts",
]
