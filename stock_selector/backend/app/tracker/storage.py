"""追踪状态持久化层（SQLite / SQLAlchemy）。

对外暴露 :class:`TrackerStorage`，封装关注池、状态、历史、信号、备注、
提醒六张表的读写。所有写操作都在同一个 Session 中完成并提交，
读操作返回普通字典，避免 ORM 对象泄漏到业务层。
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd
from sqlalchemy import delete, func, select

from ..core.logging import get_logger
from ..data.tdx_reader import normalize_code
from ..models.orm import (
    Alert,
    Note,
    SignalRecord,
    TrackingHistory,
    TrackingState,
    WatchItem,
    create_db_engine,
    create_session_factory,
    init_db,
    now,
)

logger = get_logger("tracker.storage")


def _norm(code: Optional[str]) -> str:
    """安全地标准化代码，失败时返回原字符串。"""
    if not code:
        return ""
    try:
        return normalize_code(code)
    except ValueError:
        return str(code).strip().upper()


def _as_date(value: Any) -> Optional[str]:
    """把各种日期写法统一为 ``YYYY-MM-DD`` 字符串。"""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= 10:
            return text[:10]
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except Exception:  # pragma: no cover
        return None


#: ORM 中类型为 DateTime 的字段；写入前必须转换为标准库 datetime，
#: 否则 pandas.Timestamp / numpy.datetime64 会被 SQLite 方言拒绝。
_DATETIME_FIELDS = {
    "state_changed_at",
    "changed_at",
    "created_at",
    "updated_at",
    "delivered_at",
}


def _as_datetime(value: Any) -> Optional[datetime]:
    """把 pandas / numpy / 字符串等写法统一为标准库 ``datetime``。

    SQLAlchemy 的 SQLite ``DateTime`` 只接受 ``datetime`` / ``date``，
    而业务层经常传来 ``pd.Timestamp``，因此这里统一做一次转换。

    :param value: 任意可解析的时间
    :return: ``datetime``；无法解析时返回 None
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.to_pydatetime()
    if hasattr(value, "to_pydatetime"):  # numpy.datetime64 等
        try:
            return value.to_pydatetime()
        except Exception:  # pragma: no cover
            pass
    try:
        ts = pd.Timestamp(value)
    except Exception:  # pragma: no cover
        return None
    return None if pd.isna(ts) else ts.to_pydatetime()


def _coerce_field(key: str, value: Any) -> Any:
    """按字段名做类型归一（目前仅处理 DateTime 字段）。"""
    if key in _DATETIME_FIELDS:
        return _as_datetime(value)
    return value


class TrackerStorage:
    """追踪数据存储。

    :param url: 数据库 URL，例如 ``sqlite:///tracking.db``
    :param echo: 是否打印 SQL
    """

    def __init__(self, url: str = "sqlite:///tracking.db", echo: bool = False) -> None:
        self.url = url
        self.engine = create_db_engine(url, echo=echo)
        init_db(self.engine)
        self.Session = create_session_factory(self.engine)
        logger.info("追踪数据库已就绪：%s", url)

    # ------------------------------------------------------------------
    # 通用
    # ------------------------------------------------------------------
    def close(self) -> None:
        """释放数据库连接。"""
        try:
            self.engine.dispose()
        except Exception:  # pragma: no cover
            pass

    def stats(self) -> Dict[str, Any]:
        """返回各表记录数。"""
        result: Dict[str, Any] = {"url": self.url}
        with self.Session() as s:
            for name, model in (
                ("watchlist", WatchItem),
                ("tracking_state", TrackingState),
                ("tracking_history", TrackingHistory),
                ("signals", SignalRecord),
                ("notes", Note),
                ("alerts", Alert),
            ):
                try:
                    result[name] = int(s.scalar(select(func.count()).select_from(model)) or 0)
                except Exception:  # pragma: no cover
                    result[name] = 0
        return result

    def reset(self, tables: Optional[Sequence[str]] = None) -> Dict[str, int]:
        """清空指定表（默认全部）。

        :param tables: 表名列表
        :return: 各表删除行数
        """
        mapping = {
            "watchlist": WatchItem,
            "tracking_state": TrackingState,
            "tracking_history": TrackingHistory,
            "signals": SignalRecord,
            "notes": Note,
            "alerts": Alert,
        }
        targets = list(tables) if tables else list(mapping)
        deleted: Dict[str, int] = {}
        with self.Session() as s:
            for name in targets:
                model = mapping.get(name)
                if model is None:
                    continue
                count = int(s.scalar(select(func.count()).select_from(model)) or 0)
                s.execute(delete(model))
                deleted[name] = count
            s.commit()
        return deleted

    # ------------------------------------------------------------------
    # 关注池
    # ------------------------------------------------------------------
    def add_watch(
        self,
        code: str,
        name: str = "",
        group: str = "默认分组",
        tags: Optional[Iterable[str]] = None,
        note: str = "",
        strategy: str = "",
        strategy_params: Optional[Dict[str, Any]] = None,
        cost_price: Optional[float] = None,
        shares: Optional[int] = None,
        target_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        source: str = "manual",
        overwrite: bool = False,
        state: Optional[str] = None,
        strategy_name: str = "",
    ) -> Dict[str, Any]:
        """添加/更新关注池条目。

        :param code: 股票代码
        :param name: 股票名称
        :param group: 分组
        :param tags: 标签列表
        :param note: 备注
        :param strategy: 绑定策略名
        :param strategy_params: 策略参数
        :param cost_price: 成本价
        :param shares: 持仓股数
        :param target_price: 目标价
        :param stop_price: 止损价
        :param source: 来源（manual / screen / backtest / import）
        :param overwrite: 已存在时是否覆盖全部字段
        :param state: 初始状态
        :param strategy_name: 兼容别名（等同 strategy）
        :return: 关注项字典
        """
        std = _norm(code)
        if not std:
            raise ValueError("股票代码不能为空")
        strategy = strategy or strategy_name

        with self.Session() as s:
            item = s.scalar(select(WatchItem).where(WatchItem.code == std))
            if item is None:
                item = WatchItem(
                    code=std,
                    name=name or std,
                    group=group or "默认分组",
                    tags=list(tags or []),
                    note=note or "",
                    strategy=strategy or "",
                    strategy_params=dict(strategy_params or {}),
                    cost_price=cost_price,
                    shares=shares,
                    target_price=target_price,
                    stop_price=stop_price,
                    source=source,
                    enabled=True,
                )
                s.add(item)
            else:
                item.updated_at = now()
                if overwrite:
                    item.name = name or item.name or std
                    item.group = group or item.group
                    item.tags = list(tags if tags is not None else (item.tags or []))
                    item.note = note if note is not None else item.note
                    item.strategy = strategy or item.strategy
                    item.strategy_params = dict(
                        strategy_params if strategy_params is not None else (item.strategy_params or {})
                    )
                    item.cost_price = cost_price if cost_price is not None else item.cost_price
                    item.shares = shares if shares is not None else item.shares
                    item.target_price = target_price if target_price is not None else item.target_price
                    item.stop_price = stop_price if stop_price is not None else item.stop_price
                    item.source = source or item.source
                else:
                    if name:
                        item.name = name
                    if group:
                        item.group = group
                    if tags:
                        merged = list(dict.fromkeys(list(item.tags or []) + list(tags)))
                        item.tags = merged
                    if note:
                        item.note = note
                    if strategy:
                        item.strategy = strategy
                    if strategy_params:
                        item.strategy_params = {
                            **(item.strategy_params or {}),
                            **dict(strategy_params),
                        }
                    if cost_price is not None:
                        item.cost_price = cost_price
                    if shares is not None:
                        item.shares = shares
                    if target_price is not None:
                        item.target_price = target_price
                    if stop_price is not None:
                        item.stop_price = stop_price
            s.commit()
            s.refresh(item)
            data = item.to_dict()

        # 同步初始状态（不存在时创建）
        if state or self.get_state(std) is None:
            self.upsert_state(std, name=data["name"], group=data["group"], state=state or "候选")
        return data

    def remove_watch(self, code: str, keep_state: bool = False) -> bool:
        """删除关注项。

        :param code: 股票代码
        :param keep_state: 是否保留状态记录
        :return: 是否删除了记录
        """
        std = _norm(code)
        with self.Session() as s:
            item = s.scalar(select(WatchItem).where(WatchItem.code == std))
            if item is None:
                return False
            s.delete(item)
            if not keep_state:
                st = s.scalar(select(TrackingState).where(TrackingState.code == std))
                if st is not None:
                    s.delete(st)
            s.commit()
        return True

    def get_watch(self, code: str) -> Optional[Dict[str, Any]]:
        """获取单个关注项。"""
        std = _norm(code)
        with self.Session() as s:
            item = s.scalar(select(WatchItem).where(WatchItem.code == std))
            return item.to_dict() if item else None

    def update_watch(self, code: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """更新关注项字段。

        :param code: 股票代码
        :param fields: 需要更新的字段（忽略 None）
        """
        std = _norm(code)
        allowed = {
            "name", "group", "note", "strategy", "strategy_params", "cost_price",
            "shares", "target_price", "stop_price", "enabled", "tags", "source",
        }
        with self.Session() as s:
            item = s.scalar(select(WatchItem).where(WatchItem.code == std))
            if item is None:
                return None
            for key, value in fields.items():
                if key in allowed and value is not None:
                    setattr(item, key, value)
            item.updated_at = now()
            s.commit()
            s.refresh(item)
            return item.to_dict()

    def list_watch(
        self,
        group: Optional[str] = None,
        enabled_only: bool = False,
        keyword: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """列出关注池。

        :param group: 按分组过滤
        :param enabled_only: 只返回启用的条目
        :param keyword: 按代码或名称模糊搜索
        :param tags: 过滤包含任一标签的条目
        """
        with self.Session() as s:
            stmt = select(WatchItem).order_by(WatchItem.group, WatchItem.code)
            if group:
                stmt = stmt.where(WatchItem.group == group)
            if enabled_only:
                stmt = stmt.where(WatchItem.enabled.is_(True))
            items = [i.to_dict() for i in s.scalars(stmt).all()]

        if keyword:
            k = keyword.strip().upper()
            items = [
                i for i in items
                if k in i["code"].upper() or k in (i["name"] or "").upper()
            ]
        if tags:
            tagset = set(tags)
            items = [i for i in items if tagset & set(i.get("tags") or [])]
        return items

    def list_groups(self) -> List[Dict[str, Any]]:
        """返回分组及其条目数。"""
        with self.Session() as s:
            rows = s.execute(
                select(WatchItem.group, func.count()).group_by(WatchItem.group)
            ).all()
        return [{"group": g or "默认分组", "count": int(c)} for g, c in rows]

    def watch_codes(self, enabled_only: bool = True) -> List[str]:
        """返回关注池中的代码列表。"""
        return [i["code"] for i in self.list_watch(enabled_only=enabled_only)]

    # ------------------------------------------------------------------
    # 当前状态
    # ------------------------------------------------------------------
    def upsert_state(self, code: str, **fields: Any) -> Dict[str, Any]:
        """写入或更新某只股票的当前状态。

        :param code: 股票代码
        :param fields: 状态字段
        :return: 更新后的状态字典
        """
        std = _norm(code)
        with self.Session() as s:
            row = s.scalar(select(TrackingState).where(TrackingState.code == std))
            if row is None:
                row = TrackingState(code=std)
                s.add(row)
            for key, value in fields.items():
                if value is None:
                    continue
                if hasattr(row, key):
                    setattr(row, key, _coerce_field(key, value))
            row.updated_at = now()
            s.commit()
            s.refresh(row)
            return row.to_dict()

    def get_state(self, code: str) -> Optional[Dict[str, Any]]:
        """读取某只股票当前状态。"""
        std = _norm(code)
        with self.Session() as s:
            row = s.scalar(select(TrackingState).where(TrackingState.code == std))
            return row.to_dict() if row else None

    def list_states(
        self,
        states: Optional[Sequence[str]] = None,
        group: Optional[str] = None,
        risk_level: Optional[str] = None,
        keyword: Optional[str] = None,
        codes: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """按条件查询当前状态。"""
        with self.Session() as s:
            stmt = select(TrackingState).order_by(TrackingState.group, TrackingState.code)
            if states:
                stmt = stmt.where(TrackingState.state.in_(list(states)))
            if group:
                stmt = stmt.where(TrackingState.group == group)
            if risk_level:
                stmt = stmt.where(TrackingState.risk_level == risk_level)
            if codes:
                stmt = stmt.where(TrackingState.code.in_([_norm(c) for c in codes]))
            rows = [r.to_dict() for r in s.scalars(stmt).all()]

        if keyword:
            k = keyword.strip().upper()
            rows = [
                r for r in rows
                if k in r["code"].upper() or k in (r["name"] or "").upper()
            ]
        return rows

    def delete_state(self, code: str) -> bool:
        """删除某只股票的状态记录。"""
        std = _norm(code)
        with self.Session() as s:
            row = s.scalar(select(TrackingState).where(TrackingState.code == std))
            if row is None:
                return False
            s.delete(row)
            s.commit()
        return True

    # ------------------------------------------------------------------
    # 历史
    # ------------------------------------------------------------------
    def add_history(
        self,
        code: str,
        old_state: str,
        new_state: str,
        reason: str = "",
        trade_date: Optional[str] = None,
        name: str = "",
        strategy: str = "",
        trigger_type: str = "auto",
        factors: Optional[Dict[str, Any]] = None,
        price: Optional[float] = None,
        note: str = "",
        changed_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """写入一条状态变迁记录。"""
        std = _norm(code)
        with self.Session() as s:
            row = TrackingHistory(
                code=std,
                name=name or std,
                old_state=old_state or "",
                new_state=new_state or "",
                reason=reason or "",
                trade_date=_as_date(trade_date),
                strategy=strategy or "",
                trigger_type=trigger_type,
                factors=dict(factors or {}),
                price=price,
                note=note or "",
                changed_at=_as_datetime(changed_at) or now(),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.to_dict()

    def list_history(
        self,
        code: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        states: Optional[Sequence[str]] = None,
        strategy: Optional[str] = None,
        trigger_type: Optional[str] = None,
        limit: int = 500,
        order: str = "desc",
    ) -> List[Dict[str, Any]]:
        """查询状态变迁历史。

        :param code: 股票代码
        :param start: 起始交易日
        :param end: 结束交易日
        :param states: 过滤新状态
        :param strategy: 过滤策略
        :param trigger_type: auto / manual / replay
        :param limit: 最多返回条数
        :param order: ``desc`` 或 ``asc``
        """
        with self.Session() as s:
            stmt = select(TrackingHistory)
            if code:
                stmt = stmt.where(TrackingHistory.code == _norm(code))
            if start:
                stmt = stmt.where(TrackingHistory.trade_date >= _as_date(start))
            if end:
                stmt = stmt.where(TrackingHistory.trade_date <= _as_date(end))
            if states:
                stmt = stmt.where(TrackingHistory.new_state.in_(list(states)))
            if strategy:
                stmt = stmt.where(TrackingHistory.strategy == strategy)
            if trigger_type:
                stmt = stmt.where(TrackingHistory.trigger_type == trigger_type)
            stmt = stmt.order_by(
                TrackingHistory.trade_date.desc()
                if order == "desc"
                else TrackingHistory.trade_date.asc(),
                TrackingHistory.id.desc() if order == "desc" else TrackingHistory.id.asc(),
            ).limit(int(limit))
            return [r.to_dict() for r in s.scalars(stmt).all()]

    def delete_history(
        self,
        code: Optional[str] = None,
        trade_date: Optional[str] = None,
        trigger_type: Optional[str] = None,
    ) -> int:
        """删除历史记录（用于回放时重建，保证幂等）。

        :return: 删除条数
        """
        with self.Session() as s:
            stmt = delete(TrackingHistory)
            conditions = []
            if code:
                conditions.append(TrackingHistory.code == _norm(code))
            if trade_date:
                conditions.append(TrackingHistory.trade_date == _as_date(trade_date))
            if trigger_type:
                conditions.append(TrackingHistory.trigger_type == trigger_type)
            for cond in conditions:
                stmt = stmt.where(cond)
            result = s.execute(stmt)
            s.commit()
            return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # 信号
    # ------------------------------------------------------------------
    def add_signal(
        self,
        code: str,
        strategy: str,
        signal: int,
        signal_text: str = "",
        trade_date: Optional[str] = None,
        name: str = "",
        price: Optional[float] = None,
        pct_change: Optional[float] = None,
        reason: str = "",
        is_new: bool = False,
        factors: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """写入一条策略信号记录。"""
        std = _norm(code)
        with self.Session() as s:
            row = SignalRecord(
                code=std,
                name=name or std,
                strategy=strategy or "",
                signal=int(signal),
                signal_text=signal_text or "",
                trade_date=_as_date(trade_date) or "",
                price=price,
                pct_change=pct_change,
                reason=reason or "",
                is_new=bool(is_new),
                factors=dict(factors or {}),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.to_dict()

    def delete_signals(
        self,
        code: Optional[str] = None,
        trade_date: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> int:
        """删除信号记录。"""
        with self.Session() as s:
            stmt = delete(SignalRecord)
            if code:
                stmt = stmt.where(SignalRecord.code == _norm(code))
            if trade_date:
                stmt = stmt.where(SignalRecord.trade_date == _as_date(trade_date))
            if strategy:
                stmt = stmt.where(SignalRecord.strategy == strategy)
            result = s.execute(stmt)
            s.commit()
            return int(result.rowcount or 0)

    def list_signals(
        self,
        code: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        strategy: Optional[str] = None,
        signal: Optional[int] = None,
        only_new: bool = False,
        limit: int = 500,
        order: str = "desc",
    ) -> List[Dict[str, Any]]:
        """查询信号记录。"""
        with self.Session() as s:
            stmt = select(SignalRecord)
            if code:
                stmt = stmt.where(SignalRecord.code == _norm(code))
            if start:
                stmt = stmt.where(SignalRecord.trade_date >= _as_date(start))
            if end:
                stmt = stmt.where(SignalRecord.trade_date <= _as_date(end))
            if strategy:
                stmt = stmt.where(SignalRecord.strategy == strategy)
            if signal is not None:
                stmt = stmt.where(SignalRecord.signal == int(signal))
            if only_new:
                stmt = stmt.where(SignalRecord.is_new.is_(True))
            stmt = stmt.order_by(
                SignalRecord.trade_date.desc()
                if order == "desc"
                else SignalRecord.trade_date.asc(),
                SignalRecord.id.desc() if order == "desc" else SignalRecord.id.asc(),
            ).limit(int(limit))
            return [r.to_dict() for r in s.scalars(stmt).all()]

    # ------------------------------------------------------------------
    # 备注
    # ------------------------------------------------------------------
    def add_note(self, code: str, content: str, author: str = "user") -> Dict[str, Any]:
        """新增备注。"""
        std = _norm(code)
        with self.Session() as s:
            row = Note(code=std, content=content, author=author)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.to_dict()

    def list_notes(self, code: str, limit: int = 100) -> List[Dict[str, Any]]:
        """列出某只股票的备注。"""
        std = _norm(code)
        with self.Session() as s:
            stmt = (
                select(Note)
                .where(Note.code == std)
                .order_by(Note.created_at.desc())
                .limit(limit)
            )
            return [r.to_dict() for r in s.scalars(stmt).all()]

    def delete_note(self, note_id: int) -> bool:
        """删除备注。"""
        with self.Session() as s:
            row = s.get(Note, note_id)
            if row is None:
                return False
            s.delete(row)
            s.commit()
        return True

    # ------------------------------------------------------------------
    # 提醒
    # ------------------------------------------------------------------
    def add_alert(
        self,
        code: str = "",
        name: str = "",
        level: str = "info",
        category: str = "state_change",
        title: str = "",
        message: str = "",
        strategy: str = "",
        trade_date: Optional[str] = None,
        trigger_value: Optional[float] = None,
        delivered: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """写入一条提醒。"""
        with self.Session() as s:
            row = Alert(
                code=_norm(code) if code else "",
                name=name or (code or ""),
                level=level,
                category=category,
                title=title,
                message=message,
                strategy=strategy or "",
                trade_date=_as_date(trade_date),
                trigger_value=trigger_value,
                delivered=dict(delivered or {}),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.to_dict()

    def list_alerts(
        self,
        code: Optional[str] = None,
        level: Optional[str] = None,
        category: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        unacked_only: bool = False,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """查询提醒记录。"""
        with self.Session() as s:
            stmt = select(Alert)
            if code:
                stmt = stmt.where(Alert.code == _norm(code))
            if level:
                stmt = stmt.where(Alert.level == level)
            if category:
                stmt = stmt.where(Alert.category == category)
            if start:
                stmt = stmt.where(Alert.trade_date >= _as_date(start))
            if end:
                stmt = stmt.where(Alert.trade_date <= _as_date(end))
            if unacked_only:
                stmt = stmt.where(Alert.acked.is_(False))
            stmt = stmt.order_by(Alert.created_at.desc(), Alert.id.desc()).limit(limit)
            return [r.to_dict() for r in s.scalars(stmt).all()]

    def ack_alert(self, alert_id: int) -> bool:
        """标记提醒为已读。"""
        with self.Session() as s:
            row = s.get(Alert, alert_id)
            if row is None:
                return False
            row.acked = True
            s.commit()
        return True

    def update_alert_delivery(self, alert_id: int, channel: str, ok: bool, detail: str = "") -> None:
        """记录提醒的投递结果。"""
        with self.Session() as s:
            row = s.get(Alert, alert_id)
            if row is None:
                return
            delivered = dict(row.delivered or {})
            delivered[channel] = {"ok": bool(ok), "detail": detail, "at": now().isoformat()}
            row.delivered = delivered
            s.commit()

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export_csv(self, table: str, path: Path | str, **filters: Any) -> Path:
        """把指定表导出为 CSV。

        :param table: ``watchlist`` / ``tracking_state`` / ``tracking_history`` /
            ``signals`` / ``notes`` / ``alerts``
        :param path: 目标路径
        :return: 写入路径
        """
        loaders = {
            "watchlist": lambda: self.list_watch(),
            "tracking_state": lambda: self.list_states(**filters),
            "tracking_history": lambda: self.list_history(**filters),
            "signals": lambda: self.list_signals(**filters),
            "notes": lambda: self.list_notes(filters.get("code", "")) if filters.get("code") else [],
            "alerts": lambda: self.list_alerts(**filters),
        }
        if table not in loaders:
            raise ValueError(f"不支持的导出表：{table}")
        rows = loaders[table]()
        df = pd.DataFrame(rows)
        # JSON 列转成字符串，避免 CSV 里出现 dict 表示
        for col in df.columns:
            if df[col].apply(lambda v: isinstance(v, (dict, list))).any():
                df[col] = df[col].apply(
                    lambda v: "" if v is None else str(v)
                )
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(p, index=False, encoding="utf-8-sig")
        return p

    # ------------------------------------------------------------------
    def query_states_by_ids(self, ids: Sequence[int]) -> List[Dict[str, Any]]:
        """按主键批量查询状态（辅助方法）。"""
        with self.Session() as s:
            stmt = select(TrackingState).where(TrackingState.id.in_(list(ids)))
            return [r.to_dict() for r in s.scalars(stmt).all()]
