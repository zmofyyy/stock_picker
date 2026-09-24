"""持续追踪核心模块。

职责：
    * 按指定交易日（或最新交易日）更新关注池中所有股票的状态；
    * 判断策略信号变化、风险状态变化、持仓状态变化并落库；
    * 生成提醒并通过 :class:`~app.tracker.notifier.Notifier` 推送；
    * 支持历史回放：给定历史日期区间，逐日重建当时的追踪状态。

关键约束：
    追踪只会使用「截止目标交易日及之前」的数据，绝不使用未来数据。
    这一点通过 ``reader.read_daily(code, end=date)`` 从数据源头保证。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..core.logging import get_logger
from ..data.basics import StockBasicResolver, attach_float_shares
from ..data.names import NameResolver
from ..data.tdx_reader import TdxDataReader, normalize_code
from ..strategies.base import BaseStrategy, SignalDetail
from ..strategies.indicators import sma
from ..strategies.registry import get_strategy
from .notifier import (
    Notification,
    Notifier,
    build_risk_notification,
    build_signal_notification,
    build_state_change_notification,
)
from .state_machine import (
    HOLDING_STATES,
    STATE_CANDIDATE,
    STATE_PAUSED,
    RiskAssessment,
    StateContext,
    StateMachine,
    evaluate_risk,
)
from .storage import TrackerStorage

logger = get_logger("tracker")


@dataclass
class UpdateResult:
    """一次追踪更新的结果。"""

    trade_date: Optional[str] = None
    trigger_type: str = "auto"
    processed: int = 0
    changed: int = 0
    skipped: int = 0
    new_signals: int = 0
    alerts: int = 0
    errors: List[Dict[str, str]] = field(default_factory=list)
    state_changes: List[Dict[str, Any]] = field(default_factory=list)
    signal_list: List[Dict[str, Any]] = field(default_factory=list)
    alert_list: List[Dict[str, Any]] = field(default_factory=list)
    elapsed: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return {
            "trade_date": self.trade_date,
            "trigger_type": self.trigger_type,
            "processed": self.processed,
            "changed": self.changed,
            "skipped": self.skipped,
            "new_signals": self.new_signals,
            "alerts": self.alerts,
            "errors": self.errors,
            "state_changes": self.state_changes,
            "signals": self.signal_list,
            "alert_list": self.alert_list,
            "elapsed": round(self.elapsed, 3),
        }


class StockTracker:
    """股票持续追踪器。

    :param reader: 通达信数据读取器
    :param storage: 追踪存储
    :param strategy: 默认策略实例或策略名
    :param config: ``config.yaml`` 的 ``tracker`` 段配置
    :param name_resolver: 名称解析器
    :param notifier: 提醒发送器；为 None 时按配置自动创建
    :param basics: 股票基础信息（流通股本）解析器；供需要流通盘的策略使用
    """

    def __init__(
        self,
        reader: TdxDataReader,
        storage: TrackerStorage,
        strategy: Optional[BaseStrategy | str] = None,
        config: Optional[Dict[str, Any]] = None,
        name_resolver: Optional[NameResolver] = None,
        notifier: Optional[Notifier] = None,
        basics: Optional[StockBasicResolver] = None,
    ) -> None:
        self.reader = reader
        self.storage = storage
        self.names = name_resolver or NameResolver()
        self.basics = basics
        self.cfg: Dict[str, Any] = dict(config or {})

        # 注意：命名为 state_names 以避免与 list_states() 等方法混淆
        self.state_names: List[str] = list(self.cfg.get("states") or [])
        self.state_machine = StateMachine(self.state_names)

        risk_cfg = self.cfg.get("risk") or {}
        self.risk_cfg = {
            "near_stop_ratio": float(risk_cfg.get("near_stop_ratio", 0.02)),
            "vol_spike_ratio": float(risk_cfg.get("vol_spike_ratio", 1.5)),
            "drop_pct": float(risk_cfg.get("drop_pct", 0.03)),
            "break_ma_window": int(risk_cfg.get("break_ma_window", 20)),
        }
        self.alert_states = set(self.cfg.get("alert_states") or [])

        self.default_strategy = strategy if strategy is not None else get_strategy(None)
        self._strategy_cache: Dict[str, BaseStrategy] = {}

        if notifier is not None:
            self.notifier = notifier
        else:
            self.notifier = Notifier(
                self.cfg.get("notify") or {},
                base_dir=None,
            )

    # ==================================================================
    # 公开接口
    # ==================================================================
    def update(
        self,
        codes: Optional[Sequence[str]] = None,
        date: Optional[str] = None,
        strategy: Optional[BaseStrategy | str] = None,
        params: Optional[Dict[str, Any]] = None,
        notify: bool = True,
        trigger_type: str = "auto",
        group: Optional[str] = None,
    ) -> UpdateResult:
        """更新关注池状态。

        :param codes: 指定股票代码；为空时使用整个关注池
        :param date: 目标交易日；为空时使用每只股票的最新可用数据日
        :param strategy: 覆盖默认策略
        :param params: 覆盖策略参数
        :param notify: 是否发送提醒
        :param trigger_type: ``auto``（定时/手动）或 ``replay``（回放）
        :param group: 只更新指定分组
        :return: :class:`UpdateResult`
        """
        t0 = time.time()
        result = UpdateResult(trigger_type=trigger_type)

        watch_items = self._resolve_watch_items(codes, group)
        if not watch_items:
            result.elapsed = time.time() - t0
            logger.warning("关注池为空，追踪更新未执行任何操作")
            return result

        target_ts = pd.Timestamp(str(date)) if date else None
        override_strategy = self._resolve_strategy(strategy, params)

        # 同一批次内共享提醒去重集合
        self._alert_keys = self._existing_alert_keys(date)
        resolved_dates: List[str] = []

        for item in watch_items:
            try:
                outcome = self._update_one(
                    item,
                    target_ts,
                    override_strategy,
                    trigger_type=trigger_type,
                    notify=notify,
                )
            except Exception as exc:  # 单只股票异常不影响整批
                logger.exception("更新 %s 失败：%s", item.get("code"), exc)
                result.errors.append({"code": item.get("code", ""), "error": str(exc)})
                continue

            result.processed += 1
            if outcome.get("trade_date"):
                resolved_dates.append(outcome["trade_date"])
            if outcome.get("changed"):
                result.changed += 1
                result.state_changes.append(outcome["history"])
            if outcome.get("signal") is not None:
                result.new_signals += 1 if outcome["signal"].get("is_new") else 0
                result.signal_list.append(outcome["signal"])
            if outcome.get("skipped"):
                result.skipped += 1
            for a in outcome.get("alerts", []):
                result.alert_list.append(a)

        result.alerts = len(result.alert_list)
        result.trade_date = (
            pd.Timestamp(str(date)).strftime("%Y-%m-%d")
            if date
            else (max(resolved_dates) if resolved_dates else None)
        )
        result.elapsed = time.time() - t0
        logger.info(
            "追踪更新完成：处理 %d 只，状态变更 %d，新信号 %d，提醒 %d，耗时 %.2fs",
            result.processed, result.changed, result.new_signals, result.alerts, result.elapsed,
        )
        return result

    # ------------------------------------------------------------------
    def rebuild(
        self,
        codes: Optional[Sequence[str]] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        strategy: Optional[BaseStrategy | str] = None,
        params: Optional[Dict[str, Any]] = None,
        notify: bool = False,
        reset: bool = True,
        group: Optional[str] = None,
    ) -> UpdateResult:
        """历史回放：从 ``start`` 到 ``end`` 逐日重建追踪状态。

        回放过程与实时更新使用完全相同的决策函数，因此结果可复现。
        回放产生的历史记录 ``trigger_type`` 标记为 ``replay``。

        :param codes: 股票代码；为空使用整个关注池
        :param start: 起始日期；为空时取数据起点
        :param end: 结束日期；为空时取最新
        :param reset: 是否先清空这些股票的状态与历史（保证幂等）
        :return: :class:`UpdateResult`（聚合结果）
        """
        t0 = time.time()
        watch_items = self._resolve_watch_items(codes, group)
        result = UpdateResult(trigger_type="replay")
        if not watch_items:
            result.elapsed = time.time() - t0
            return result

        override_strategy = self._resolve_strategy(strategy, params)
        self._alert_keys = set()
        start_ts = pd.Timestamp(str(start)) if start else None
        end_ts = pd.Timestamp(str(end)) if end else None
        trade_dates: List[str] = []

        for item in watch_items:
            code = item["code"]
            strat = self._strategy_for(item, override_strategy)
            try:
                df = self.reader.read_daily(
                    code, end=(end_ts.strftime("%Y-%m-%d") if end_ts is not None else None)
                )
            except Exception as exc:
                result.errors.append({"code": code, "error": str(exc)})
                continue
            df = self._prepare(code, df, strat)
            if df is None or len(df) < strat.required_bars():
                result.skipped += 1
                continue

            if reset:
                self.storage.delete_history(code=code)
                self.storage.delete_signals(code=code)
                # 复位到初始状态，保证每次回放从同一起点出发
                self.storage.upsert_state(
                    code,
                    state=self.state_machine.states[0],
                    prev_state=None,
                    state_changed_at=None,
                    state_reason="回放复位",
                    data_date=None,
                )

            mask = pd.Series(True, index=df.index)
            if start_ts is not None:
                mask &= df["datetime"] >= start_ts
            if end_ts is not None:
                mask &= df["datetime"] <= end_ts
            positions = [i for i in range(len(df)) if bool(mask.iloc[i])]
            if not positions:
                result.skipped += 1
                continue

            try:
                signals = strat.generate_signals(df)
            except Exception as exc:
                result.errors.append({"code": code, "error": f"信号计算失败：{exc}"})
                continue

            for idx in positions:
                try:
                    outcome = self._update_one_at(
                        item, strat, df, signals, idx,
                        trigger_type="replay", notify=notify, persist=True,
                    )
                except Exception as exc:  # pragma: no cover
                    logger.exception("回放 %s 第 %d 行失败：%s", code, idx, exc)
                    result.errors.append({"code": code, "error": str(exc)})
                    continue
                result.processed += 1
                if outcome.get("trade_date"):
                    trade_dates.append(outcome["trade_date"])
                if outcome.get("changed"):
                    result.changed += 1
                    result.state_changes.append(outcome["history"])
                if outcome.get("signal") is not None:
                    result.new_signals += 1 if outcome["signal"].get("is_new") else 0
                    result.signal_list.append(outcome["signal"])
                for a in outcome.get("alerts", []):
                    result.alert_list.append(a)

        result.alerts = len(result.alert_list)
        result.trade_date = (
            pd.Timestamp(str(end)).strftime("%Y-%m-%d")
            if end
            else (max(trade_dates) if trade_dates else None)
        )
        result.elapsed = time.time() - t0
        logger.info(
            "历史回放完成：处理 %d 条日次记录，状态变更 %d，耗时 %.2fs",
            result.processed, result.changed, result.elapsed,
        )
        return result

    # ------------------------------------------------------------------
    def update_range(
        self,
        start: str,
        end: str,
        codes: Optional[Sequence[str]] = None,
        strategy: Optional[BaseStrategy | str] = None,
        params: Optional[Dict[str, Any]] = None,
        notify: bool = False,
    ) -> UpdateResult:
        """区间逐日更新（等价于 ``rebuild`` 但不复位历史）。"""
        return self.rebuild(
            codes=codes, start=start, end=end, strategy=strategy,
            params=params, notify=notify, reset=False,
        )

    # ------------------------------------------------------------------
    def set_state(
        self,
        code: str,
        state: str,
        reason: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        """手动设置某只股票的状态。

        :param code: 股票代码
        :param state: 目标状态
        :param reason: 原因
        :param note: 备注
        """
        std = normalize_code(code)
        if not self.state_machine.is_valid_state(state):
            raise ValueError(
                f"非法状态「{state}」，可选：{', '.join(self.state_machine.states)}"
            )
        current = self.storage.get_state(std) or {}
        old = current.get("state") or ""
        if not self.state_machine.can_transition(old, state):
            logger.warning(
                "状态迁移 %s → %s 不在标准迁移表中，仍按手动操作执行", old, state
            )
        today = datetime.now().strftime("%Y-%m-%d")
        history = self.storage.add_history(
            code=std,
            name=current.get("name") or self.names.resolve(std),
            old_state=old,
            new_state=state,
            reason=reason or f"手动设置状态为「{state}」",
            trade_date=today,
            strategy=current.get("strategy", ""),
            trigger_type="manual",
            factors=current.get("factors") or {},
            price=current.get("price"),
            note=note,
        )
        self.storage.upsert_state(
            std,
            state=state,
            prev_state=old,
            state_changed_at=datetime.now(),
            state_reason=reason or "手动设置",
        )
        return history

    def pause(self, code: str, paused: bool = True) -> Optional[Dict[str, Any]]:
        """暂停 / 恢复某只股票的自动追踪。

        暂停时把状态置为「暂停」，并在 ``extra.state_before_pause`` 中记住
        暂停前的状态；恢复时还原该状态（缺失时回到「候选」），
        这样下一次追踪更新就能继续正常流转。
        """
        std = normalize_code(code)
        row = self.storage.get_state(std) or {}
        current = row.get("state") or ""
        extra = dict(row.get("extra") or {})

        item = self.storage.update_watch(std, enabled=not paused)
        if paused:
            if current and current != STATE_PAUSED:
                extra["state_before_pause"] = current
            self.storage.upsert_state(
                std,
                state=STATE_PAUSED,
                prev_state=current or STATE_CANDIDATE,
                state_changed_at=datetime.now(),
                state_reason="用户暂停追踪",
                extra=extra,
            )
        else:
            restore = extra.pop("state_before_pause", None) or STATE_CANDIDATE
            self.storage.upsert_state(
                std,
                state=restore,
                prev_state=STATE_PAUSED,
                state_changed_at=datetime.now(),
                state_reason="用户恢复追踪",
                extra=extra,
            )
        return item

    # ==================================================================
    # 查询
    # ==================================================================
    def state_of(self, code: str) -> Optional[Dict[str, Any]]:
        """获取某只股票的当前追踪状态。"""
        return self.storage.get_state(code)

    def list_states(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """列出当前状态。"""
        return self.storage.list_states(**kwargs)

    def history(
        self,
        code: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """查询状态变迁历史。"""
        return self.storage.list_history(code=code, start=start, end=end, limit=limit)

    def timeline(self, code: str, limit: int = 100) -> Dict[str, Any]:
        """返回某只股票的状态时间线（状态 + 信号合并按时间排序）。"""
        std = normalize_code(code)
        history = self.storage.list_history(code=std, limit=limit, order="asc")
        signals = self.storage.list_signals(code=std, limit=limit, order="asc")

        events: List[Dict[str, Any]] = []
        for h in history:
            events.append(
                {
                    "type": "state",
                    "date": h.get("trade_date"),
                    "title": f"{h.get('old_state')} → {h.get('new_state')}",
                    "detail": h.get("reason", ""),
                    "level": "warning" if h.get("new_state") in ("止损", "失效", "减仓") else "info",
                    "raw": h,
                }
            )
        for s in signals:
            events.append(
                {
                    "type": "signal",
                    "date": s.get("trade_date"),
                    "title": f"{s.get('signal_text')}信号",
                    "detail": s.get("reason", ""),
                    "level": "info" if s.get("signal", 0) > 0 else "warning",
                    "raw": s,
                }
            )
        events.sort(key=lambda e: (e.get("date") or "", 0 if e["type"] == "signal" else 1))
        return {
            "code": std,
            "name": (self.storage.get_state(std) or {}).get("name") or self.names.resolve(std),
            "events": events,
        }

    def dashboard(self, date: Optional[str] = None) -> Dict[str, Any]:
        """仪表盘数据聚合。

        :param date: 指定日期；默认为状态表中最新数据日期
        """
        states = self.storage.list_states()
        watch = self.storage.list_watch()
        if date:
            target = pd.Timestamp(date).strftime("%Y-%m-%d")
        else:
            dates = [s.get("data_date") for s in states if s.get("data_date")]
            target = max(dates) if dates else datetime.now().strftime("%Y-%m-%d")

        today_signals = self.storage.list_signals(start=target, end=target, only_new=True, limit=200)
        today_history = self.storage.list_history(start=target, end=target, limit=200)
        today_alerts = self.storage.list_alerts(start=target, end=target, limit=200)

        state_dist: Dict[str, int] = {}
        risk_dist: Dict[str, int] = {}
        for s in states:
            state_dist[s["state"]] = state_dist.get(s["state"], 0) + 1
            risk_dist[s.get("risk_level", "normal")] = risk_dist.get(s.get("risk_level", "normal"), 0) + 1

        holdings = [s for s in states if s.get("cost_price") and s.get("shares")]
        total_pnl = sum(float(s.get("unrealized_pnl") or 0) for s in holdings)

        return {
            "date": target,
            "watch_count": len(watch),
            "active_count": len([w for w in watch if w.get("enabled")]),
            "tracked_count": len(states),
            "state_distribution": state_dist,
            "risk_distribution": risk_dist,
            "today_signals": today_signals,
            "today_state_changes": today_history,
            "today_alerts": today_alerts,
            "holdings": holdings,
            "holding_count": len(holdings),
            "total_unrealized_pnl": round(total_pnl, 2),
            "storage": self.storage.stats(),
        }

    # ==================================================================
    # 内部实现
    # ==================================================================
    def _resolve_watch_items(
        self, codes: Optional[Sequence[str]], group: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """把 codes / group 解析成关注项列表。

        未在关注池中的代码会被临时构造为一个「隐含关注项」，方便一次性追踪。
        """
        if codes:
            items: List[Dict[str, Any]] = []
            for c in codes:
                try:
                    std = normalize_code(c)
                except ValueError:
                    logger.warning("忽略无法识别的代码：%s", c)
                    continue
                item = self.storage.get_watch(std)
                if item is None:
                    state = self.storage.get_state(std) or {}
                    item = {
                        "code": std,
                        "name": state.get("name") or self.names.resolve(std),
                        "group": group or "临时追踪",
                        "strategy": state.get("strategy") or "",
                        "strategy_params": {},
                        "cost_price": state.get("cost_price"),
                        "shares": state.get("shares"),
                        "target_price": None,
                        "stop_price": None,
                        "enabled": True,
                    }
                items.append(item)
            return items
        return self.storage.list_watch(group=group, enabled_only=False)

    def _resolve_strategy(
        self,
        strategy: Optional[BaseStrategy | str],
        params: Optional[Dict[str, Any]],
    ) -> Optional[BaseStrategy]:
        """把入参统一成策略实例。"""
        if isinstance(strategy, BaseStrategy):
            if params:
                strategy.initialize({**strategy.get_params(), **params})
            return strategy
        if strategy is None and not params:
            return None
        return get_strategy(strategy, params)

    def _strategy_for(
        self, item: Dict[str, Any], override: Optional[BaseStrategy]
    ) -> BaseStrategy:
        """确定某只股票使用的策略（带缓存）。"""
        if override is not None:
            return override
        name = item.get("strategy") or self.default_strategy.name
        params = item.get("strategy_params") or {}
        key = f"{name}|{sorted(params.items()) if params else ''}"
        if key in self._strategy_cache:
            return self._strategy_cache[key]
        try:
            strat = get_strategy(name, params or None)
        except KeyError:
            logger.warning("关注项 %s 绑定的策略 %s 不存在，回退到默认策略", item.get("code"), name)
            strat = self.default_strategy
        self._strategy_cache[key] = strat
        return strat

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _prepare(
        self, code: str, df: Optional[pd.DataFrame], strategy: BaseStrategy
    ) -> Optional[pd.DataFrame]:
        """按策略需要为 K 线补充基础信息列（流通股本）。"""
        if df is None or not getattr(strategy, "requires_basics", False):
            return df
        return attach_float_shares(df, code, self.basics)

    def _update_one(
        self,
        item: Dict[str, Any],
        target_ts: Optional[pd.Timestamp],
        override: Optional[BaseStrategy],
        trigger_type: str = "auto",
        notify: bool = True,
    ) -> Dict[str, Any]:
        """更新单只股票（实时模式：只评估目标日）。

        :return: ``{"changed","history","signal","alerts","trade_date","skipped"}``
        """
        code = item["code"]
        strat = self._strategy_for(item, override)

        df = self.reader.read_daily(
            code, end=(target_ts.strftime("%Y-%m-%d") if target_ts is not None else None)
        )
        df = self._prepare(code, df, strat)
        if df is None or len(df) < strat.required_bars():
            return _skipped_outcome()

        signals = strat.generate_signals(df)
        idx = len(df) - 1
        if target_ts is not None:
            matches = df.index[df["datetime"] <= target_ts]
            if len(matches) == 0:
                return _skipped_outcome()
            idx = int(matches[-1])

        return self._update_one_at(
            item, strat, df, signals, idx,
            trigger_type=trigger_type, notify=notify, persist=True,
        )

    def _update_one_at(
        self,
        item: Dict[str, Any],
        strat: BaseStrategy,
        df: pd.DataFrame,
        signals: pd.DataFrame,
        idx: int,
        trigger_type: str = "auto",
        notify: bool = True,
        persist: bool = True,
    ) -> Dict[str, Any]:
        """在给定数据与下标上执行一次状态判定与持久化。

        这是追踪的核心：与回放、实时更新共用同一路径。
        """
        code = item["code"]
        name = item.get("name") or self.names.resolve(code)
        bar = df.iloc[idx]
        trade_date = pd.Timestamp(bar["datetime"]).strftime("%Y-%m-%d")

        detail = strat.build_detail(code, df, signals, idx, name=name)

        # ---- 风险与持仓 ----
        cost_price = _to_float(item.get("cost_price"))
        shares = _to_float(item.get("shares"))
        stop_price = _to_float(item.get("stop_price"))
        target_price = _to_float(item.get("target_price"))

        current_state_row = self.storage.get_state(code) or {}
        current_state = current_state_row.get("state") or self.state_machine.states[0]
        has_position = bool(
            (cost_price and cost_price > 0 and shares and shares > 0)
            or current_state in HOLDING_STATES
        )

        ma_window = max(int(self.risk_cfg["break_ma_window"]), 2)
        ma_value = None
        vol_ma = None
        if idx + 1 >= ma_window:
            window = df.iloc[: idx + 1]
            ma_series = sma(window["close"].astype("float64"), ma_window)
            ma_value = _to_float(ma_series.iloc[-1])
            vol_series = sma(window["volume"].astype("float64"), 5)
            vol_ma = _to_float(vol_series.iloc[-1])

        if detail is not None:
            close_now = detail.price
            prev_close = detail.prev_close
            pct = detail.pct_change
        else:
            close_now = _to_float(bar["close"])
            pct = None
            prev_close = _to_float(df["close"].iloc[idx - 1]) if idx >= 1 else None

        row = {
            "close": close_now,
            "volume": _to_float(bar["volume"]),
            "pct_change": pct if pct is not None and pct == pct else None,
        }
        risk = evaluate_risk(
            row,
            cost_price=cost_price,
            stop_price=stop_price,
            target_price=target_price,
            ma_value=ma_value,
            volume_ma=vol_ma,
            near_stop_ratio=self.risk_cfg["near_stop_ratio"],
            vol_spike_ratio=self.risk_cfg["vol_spike_ratio"],
            drop_pct=self.risk_cfg["drop_pct"],
        )

        # 持仓浮动盈亏
        unrealized_pnl = None
        unrealized_pct = None
        hold_days = None
        if cost_price and cost_price > 0 and close_now:
            unrealized_pct = close_now / cost_price - 1.0
            if shares:
                unrealized_pnl = (close_now - cost_price) * shares
            first_state = current_state_row.get("state_changed_at")
            if first_state:
                try:
                    hold_days = max(
                        (pd.Timestamp(trade_date) - pd.Timestamp(first_state)).days, 0
                    )
                except Exception:  # pragma: no cover
                    hold_days = None

        # ---- 状态判定 ----
        ctx = StateContext(
            signal=detail.signal if detail else 0,
            is_new_signal=detail.is_new if detail else False,
            has_position=has_position,
            risk=risk,
            price=close_now,
            cost_price=cost_price,
            stop_price=stop_price,
            target_price=target_price,
            near_signal=self._is_near_signal(detail),
            enabled=bool(item.get("enabled", True)),
        )
        decision = self.state_machine.decide(current_state, ctx)

        # ---- 提醒 ----
        alerts: List[Notification] = []
        if detail is not None:
            if decision.changed:
                alerts.append(
                    build_state_change_notification(
                        code, name, decision.prev_state, decision.state, decision.reason,
                        trade_date, close_now, strat.name,
                    )
                )
            if detail.is_new:
                alerts.append(
                    build_signal_notification(
                        code, name, detail.signal, detail.reason_text,
                        trade_date, close_now, strat.name,
                    )
                )
            # 风险提醒：只在「该风险今日新增」时发送，
            # 避免同一个持续存在的风险（如连续跌破均线）每天重复刷屏。
            prev_risk_codes = set(
                (current_state_row.get("extra") or {}).get("risk_codes") or []
            )
            new_risk_codes = [c for c in (risk.codes or []) if c not in prev_risk_codes]
            if risk.level != "normal" and risk.flags and new_risk_codes:
                alerts.append(
                    build_risk_notification(
                        code, name, risk.flags, risk.level, trade_date, close_now, strat.name,
                    )
                )
            alerts.extend(self._extra_alerts(code, name, detail, trade_date))

        if notify and alerts:
            self._deliver(alerts, trade_date)

        # ---- 持久化 ----
        history_record: Optional[Dict[str, Any]] = None
        signal_record: Optional[Dict[str, Any]] = None
        alert_records: List[Dict[str, Any]] = []

        if persist:
            if decision.changed:
                history_record = self._persist_history(
                    item, code, name, decision, detail, risk, trade_date,
                    close_now, strat.name, trigger_type,
                )
            if detail is not None and detail.signal != 0:
                signal_record = self._persist_signal(
                    code, name, strat, detail, trade_date, trigger_type
                )
            for n in alerts:
                record = self.storage.add_alert(
                    code=n.code, name=n.name, level=n.level, category=n.category,
                    title=n.title, message=n.message, strategy=n.strategy,
                    trade_date=n.trade_date, trigger_value=n.trigger_value,
                )
                alert_records.append(record)

            factors = detail.factors if detail is not None else {}
            self.storage.upsert_state(
                code,
                name=name,
                group=item.get("group") or current_state_row.get("group") or "默认分组",
                state=decision.state,
                prev_state=decision.prev_state if decision.changed else current_state_row.get("prev_state"),
                state_changed_at=(
                    datetime.now() if decision.changed and trigger_type != "replay"
                    else (pd.Timestamp(trade_date) if decision.changed else current_state_row.get("state_changed_at"))
                ),
                state_reason=decision.reason,
                strategy=strat.name,
                signal=detail.signal if detail else 0,
                signal_text=detail.signal_text if detail else "无信号",
                signal_reason=detail.reason_text if detail else "",
                is_new_signal=bool(detail.is_new) if detail else False,
                data_date=trade_date,
                price=close_now,
                prev_close=prev_close,
                pct_change=pct,
                volume=_to_float(bar["volume"]),
                amount=_to_float(bar.get("amount")) if hasattr(bar, "get") else None,
                risk_level=risk.level,
                risk_flags=risk.flags,
                cost_price=cost_price,
                shares=int(shares) if shares else None,
                unrealized_pnl=unrealized_pnl,
                unrealized_pct=unrealized_pct,
                hold_days=hold_days,
                factors=factors,
                # 合并而非覆盖：保留 state_before_pause 等由用户操作写入的附加信息，
                # 同时记录风险标识用于「今日新增风险」判定。
                extra={
                    **dict(current_state_row.get("extra") or {}),
                    "risk_detail": risk.detail,
                    "risk_codes": list(risk.codes or []),
                },
            )

        return {
            "changed": decision.changed,
            "history": history_record,
            "signal": signal_record,
            "alerts": alert_records,
            "trade_date": trade_date,
            "skipped": False,
            "decision": decision.to_dict(),
            "detail": detail.to_dict() if detail else None,
        }

    # ------------------------------------------------------------------
    def _persist_history(
        self,
        item: Dict[str, Any],
        code: str,
        name: str,
        decision: Any,
        detail: Optional[SignalDetail],
        risk: RiskAssessment,
        trade_date: str,
        price: Optional[float],
        strategy: str,
        trigger_type: str,
    ) -> Dict[str, Any]:
        """写入状态变迁历史（先清理同一交易日同标的的旧记录，保证幂等）。"""
        existing = self.storage.list_history(code=code, start=trade_date, end=trade_date, limit=50)
        for row in existing:
            if row["new_state"] == decision.state and row["old_state"] == decision.prev_state:
                # 完全相同的事件已存在，直接复用，避免重复
                return row
        if existing:
            self.storage.delete_history(code=code, trade_date=trade_date)

        return self.storage.add_history(
            code=code,
            name=name,
            old_state=decision.prev_state,
            new_state=decision.state,
            reason=decision.reason,
            trade_date=trade_date,
            strategy=strategy,
            trigger_type=trigger_type,
            factors=(detail.factors if detail is not None else {}) | {"risk": risk.flags},
            price=price,
        )

    def _persist_signal(
        self,
        code: str,
        name: str,
        strat: BaseStrategy,
        detail: SignalDetail,
        trade_date: str,
        trigger_type: str,
    ) -> Dict[str, Any]:
        """写入信号记录（同一交易日同标的同策略只保留一条）。"""
        existing = self.storage.list_signals(code=code, start=trade_date, end=trade_date, limit=50)
        for row in existing:
            if row["strategy"] == strat.name:
                return row
        return self.storage.add_signal(
            code=code,
            name=name,
            strategy=strat.name,
            signal=detail.signal,
            signal_text=detail.signal_text,
            trade_date=trade_date,
            price=detail.price,
            pct_change=detail.pct_change if detail.pct_change == detail.pct_change else None,
            reason=detail.reason_text,
            is_new=detail.is_new,
            factors=detail.factors,
        )

    # ------------------------------------------------------------------
    def _extra_alerts(
        self,
        code: str,
        name: str,
        detail: SignalDetail,
        trade_date: str,
    ) -> List[Notification]:
        """根据策略因子生成附加提醒（RSI 超买超卖、放量突破、均线交叉）。"""
        out: List[Notification] = []
        factors = detail.factors or {}
        conditions = detail.conditions or {}

        rsi_value = factors.get("rsi")
        if rsi_value is not None:
            try:
                rv = float(rsi_value)
                if rv >= 70:
                    out.append(
                        Notification(
                            title="RSI 超买",
                            message=f"RSI={rv:.2f}，处于超买区间",
                            level="warning", category="rsi", code=code, name=name,
                            trade_date=trade_date, trigger_value=rv,
                            strategy=detail.strategy,
                        )
                    )
                elif rv <= 30:
                    out.append(
                        Notification(
                            title="RSI 超卖",
                            message=f"RSI={rv:.2f}，处于超卖区间",
                            level="info", category="rsi", code=code, name=name,
                            trade_date=trade_date, trigger_value=rv,
                            strategy=detail.strategy,
                        )
                    )
            except (TypeError, ValueError):  # pragma: no cover
                pass

        if conditions.get("break_cond") and conditions.get("volume_cond"):
            out.append(
                Notification(
                    title="放量突破",
                    message="收盘价突破区间高点且成交量显著放大",
                    level="info", category="volume_breakout", code=code, name=name,
                    trade_date=trade_date, trigger_value=detail.price,
                    strategy=detail.strategy,
                )
            )

        if conditions.get("buy_cond") and "ma_short" in factors:
            out.append(
                Notification(
                    title="均线金叉",
                    message=f"短均线 {_num(factors.get('ma_short'))} 上穿长均线 {_num(factors.get('ma_long'))}",
                    level="info", category="ma_cross", code=code, name=name,
                    trade_date=trade_date, trigger_value=detail.price,
                    strategy=detail.strategy,
                )
            )
        elif conditions.get("sell_cond") and "ma_short" in factors:
            out.append(
                Notification(
                    title="均线死叉",
                    message=f"短均线 {_num(factors.get('ma_short'))} 下穿长均线 {_num(factors.get('ma_long'))}",
                    level="warning", category="ma_cross", code=code, name=name,
                    trade_date=trade_date, trigger_value=detail.price,
                    strategy=detail.strategy,
                )
            )
        return out

    def _deliver(self, alerts: Sequence[Notification], trade_date: Optional[str]) -> None:
        """发送提醒，并做通道级去重。"""
        seen = getattr(self, "_alert_keys", set()) or set()
        for n in alerts:
            key = (n.code, n.trade_date or trade_date, n.category, n.title)
            if key in seen:
                continue
            seen.add(key)
            try:
                self.notifier.send(n)
            except Exception as exc:  # pragma: no cover
                logger.warning("提醒发送失败：%s", exc)
        self._alert_keys = seen

    def _existing_alert_keys(self, date: Optional[str]) -> set:
        """加载指定日期已存在的提醒去重键。"""
        try:
            rows = self.storage.list_alerts(start=date, end=date, limit=2000) if date else []
        except Exception:  # pragma: no cover
            rows = []
        return {(r.get("code"), r.get("trade_date"), r.get("category"), r.get("title")) for r in rows}

    @staticmethod
    def _is_near_signal(detail: Optional[SignalDetail]) -> bool:
        """判断是否「接近触发买入条件」。

        规则：出现买入信号但不是新信号（说明条件刚从临界状态恢复），
        或放量突破的部分条件已满足。
        """
        if detail is None:
            return False
        conditions = detail.conditions or {}
        partial = bool(conditions.get("break_cond")) ^ bool(conditions.get("volume_cond"))
        if partial:
            return True
        rsi_value = (detail.factors or {}).get("rsi")
        if rsi_value is not None:
            try:
                return 30 <= float(rsi_value) <= 40
            except (TypeError, ValueError):  # pragma: no cover
                return False
        return False


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def _skipped_outcome() -> Dict[str, Any]:
    """构造「跳过」结果（数据不足或目标日期无行情）。"""
    return {
        "changed": False,
        "history": None,
        "signal": None,
        "alerts": [],
        "trade_date": None,
        "skipped": True,
    }


def _to_float(value: Any) -> Optional[float]:
    """安全转 float，失败或 NaN 返回 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _num(value: Any, digits: int = 3) -> str:
    """数值格式化。"""
    f = _to_float(value)
    return "-" if f is None else f"{f:.{digits}f}"
