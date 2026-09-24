"""追踪器集成测试。

覆盖：
    * 关注池 → 状态更新 → 状态历史 → 信号记录 的完整链路
    * 同一天重复更新的幂等性
    * 历史回放与逐日更新的一致性（关键需求）
    * 不使用未来数据（按日期回放时只读截止当日的数据）
    * 风险驱动的状态流转（止损 / 止盈 / 减仓）
    * 手动设置状态、暂停恢复
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pandas as pd
import pytest

from backend.app.data.tdx_reader import TdxDataReader
from backend.app.tracker.notifier import Notifier
from backend.app.tracker.tracker import StockTracker

CODES = ["600000.SH", "000001.SZ", "300750.SZ", "830000.BJ"]


# ----------------------------------------------------------------------
@pytest.fixture()
def tracker(services) -> StockTracker:
    """构造追踪器（关闭通知）。"""
    services.tracker.notifier = Notifier({"console": False, "csv": False})
    services.tracker._tracker = None
    return services.tracker.tracker


@pytest.fixture()
def trading_dates(services) -> List[str]:
    """取第一只股票的交易日列表。"""
    df = services.data.reader.read_daily("600000.SH")
    return [d.strftime("%Y-%m-%d") for d in df["datetime"]]


def _add_watch(services, codes=CODES, strategy="ma_cross", **extra) -> None:
    """把测试股票加入关注池。"""
    for code in codes:
        services.tracker.add_watch(
            code=code, name="", group="测试", strategy=strategy, **extra
        )


# ----------------------------------------------------------------------
# 基础链路
# ----------------------------------------------------------------------
def test_update_creates_states(services, tracker, trading_dates) -> None:
    """更新后应生成状态记录与信号记录。"""
    _add_watch(services)
    target = trading_dates[150]
    result = tracker.update(codes=CODES, date=target, notify=False)

    assert result.processed == 4
    assert result.trade_date == target
    assert not result.errors

    for code in CODES:
        state = tracker.state_of(code)
        assert state is not None
        assert state["data_date"] == target
        assert state["state"] in tracker.state_names
        assert state["strategy"] == "ma_cross"

    history = services.tracker.storage.list_history()
    assert len(history) == len(result.state_changes)


def test_update_uses_only_past_data(services, tracker, trading_dates) -> None:
    """按日期更新时只使用截止当日的数据（严禁未来函数）。"""
    _add_watch(services, codes=["600000.SH"])
    target = trading_dates[120]
    tracker.update(codes=["600000.SH"], date=target, notify=False)

    state = tracker.state_of("600000.SH")
    assert state["data_date"] == target

    df = services.data.reader.read_daily("600000.SH", end=target)
    expected_close = float(df["close"].iloc[-1])
    assert state["price"] == pytest.approx(expected_close, rel=1e-6)

    # 与直接用截止数据做策略评估的结果一致
    strat = services.tracker.tracker.default_strategy
    detail = strat.evaluate("600000.SH", df)
    assert state["signal"] == detail.signal


def test_update_is_idempotent(services, tracker, trading_dates) -> None:
    """同一天重复更新不会产生重复历史。"""
    _add_watch(services, codes=["600000.SH"])
    target = trading_dates[130]

    first = tracker.update(codes=["600000.SH"], date=target, notify=False)
    history_after_first = services.tracker.storage.list_history(code="600000.SH")
    state_after_first = tracker.state_of("600000.SH")

    second = tracker.update(codes=["600000.SH"], date=target, notify=False)
    history_after_second = services.tracker.storage.list_history(code="600000.SH")
    state_after_second = tracker.state_of("600000.SH")

    assert len(history_after_first) == len(history_after_second)
    assert state_after_first["state"] == state_after_second["state"]
    assert first.trade_date == second.trade_date


def test_signal_records_written(services, tracker, trading_dates) -> None:
    """策略信号应写入 signals 表。"""
    _add_watch(services, codes=CODES)
    for d in trading_dates[60:200]:
        tracker.update(codes=CODES, date=d, notify=False)

    signals = services.tracker.storage.list_signals(limit=500)
    assert len(signals) > 0
    for s in signals:
        assert s["signal"] in (-1, 0, 1)
        assert s["signal"] != 0
        assert s["trade_date"]


def test_alerts_generated(services, tracker, trading_dates) -> None:
    """状态变更应产生提醒记录。"""
    _add_watch(services)
    for d in trading_dates[60:160]:
        tracker.update(codes=CODES, date=d, notify=False)

    alerts = services.tracker.storage.list_alerts(limit=500)
    assert len(alerts) > 0
    categories = {a["category"] for a in alerts}
    assert "state_change" in categories or "buy_signal" in categories
    assert all(a["trade_date"] for a in alerts)


# ----------------------------------------------------------------------
# 回放一致性
# ----------------------------------------------------------------------
def test_replay_matches_incremental_update(services, tracker, trading_dates) -> None:
    """逐日实时更新 vs 一次性回放：历史记录必须完全一致。"""
    start, end = trading_dates[80], trading_dates[140]
    _add_watch(services, codes=CODES)

    # ---- 路径 A：逐日更新（模拟每天收盘后跑一次）----
    for d in trading_dates[80:141]:
        tracker.update(codes=CODES, date=d, notify=False)

    live = [
        (h["code"], h["trade_date"], h["old_state"], h["new_state"])
        for h in services.tracker.storage.list_history(limit=5000, order="asc")
    ]
    assert live, "逐日更新未产生任何状态变更，测试数据不足"

    # ---- 路径 B：清空后一次性回放 ----
    services.tracker.storage.delete_history()
    services.tracker.storage.delete_signals()
    for code in CODES:
        services.tracker.storage.upsert_state(code, state="候选", prev_state=None,
                                              state_changed_at=None, data_date=None)

    result = tracker.rebuild(codes=CODES, start=start, end=end, reset=True, notify=False)
    replay = [
        (h["code"], h["trade_date"], h["old_state"], h["new_state"])
        for h in services.tracker.storage.list_history(limit=5000, order="asc")
    ]

    assert replay == live
    assert result.changed == len(replay)


def test_replay_is_repeatable(services, tracker, trading_dates) -> None:
    """回放两次结果一致（幂等）。"""
    _add_watch(services, codes=CODES)
    start, end = trading_dates[70], trading_dates[130]

    tracker.rebuild(codes=CODES, start=start, end=end, reset=True, notify=False)
    first = services.tracker.storage.list_history(limit=5000, order="asc")

    tracker.rebuild(codes=CODES, start=start, end=end, reset=True, notify=False)
    second = services.tracker.storage.list_history(limit=5000, order="asc")

    assert [h["new_state"] for h in first] == [h["new_state"] for h in second]
    assert [h["trade_date"] for h in first] == [h["trade_date"] for h in second]


def test_replay_does_not_read_future_data(services, tracker, trading_dates) -> None:
    """回放某段区间时，区间内的状态只依赖区间内及之前的数据。"""
    _add_watch(services, codes=["600000.SH"])
    end = trading_dates[120]
    tracker.rebuild(codes=["600000.SH"], start=trading_dates[60], end=end, reset=True, notify=False)
    state = tracker.state_of("600000.SH")
    assert state["data_date"] == end


# ----------------------------------------------------------------------
# 风险管理
# ----------------------------------------------------------------------
def test_stop_loss_triggers(services, tracker, trading_dates) -> None:
    """跌破止损价应触发「止损」状态。"""
    code = "600000.SH"
    target = trading_dates[120]
    df = services.data.reader.read_daily(code, end=target)
    close = float(df["close"].iloc[-1])

    services.tracker.add_watch(
        code=code, group="测试", strategy="ma_cross",
        cost_price=round(close * 1.2, 2),     # 成本高于现价
        shares=1000,
        stop_price=round(close * 1.05, 2),    # 止损价高于现价 → 必然触发
    )
    tracker.update(codes=[code], date=target, notify=False)
    state = tracker.state_of(code)
    assert state["state"] == "止损"
    assert state["risk_level"] == "danger"
    assert state["unrealized_pct"] < 0


def test_take_profit_triggers(services, tracker, trading_dates) -> None:
    """达到目标价应触发「止盈」。"""
    code = "000001.SZ"
    target = trading_dates[120]
    df = services.data.reader.read_daily(code, end=target)
    close = float(df["close"].iloc[-1])

    services.tracker.add_watch(
        code=code, group="测试", strategy="ma_cross",
        cost_price=round(close * 0.8, 2),
        shares=1000,
        target_price=round(close * 0.95, 2),  # 目标价低于现价 → 必然触发
    )
    tracker.update(codes=[code], date=target, notify=False)
    assert tracker.state_of(code)["state"] == "止盈"


def test_hit_stop_has_priority_over_signal(services, tracker, trading_dates) -> None:
    """止损优先级高于普通卖出信号。"""
    code = "600000.SH"
    stop_target = None
    for d in trading_dates[100:160]:
        df = services.data.reader.read_daily(code, end=d)
        strat = services.tracker.tracker.default_strategy
        detail = strat.evaluate(code, df)
        if detail is not None and detail.signal == -1:
            stop_target = d
            break
    if stop_target is None:
        pytest.skip("测试数据中没有出现卖出信号")

    df = services.data.reader.read_daily(code, end=stop_target)
    close = float(df["close"].iloc[-1])
    services.tracker.add_watch(
        code=code, group="测试", strategy="ma_cross",
        cost_price=round(close * 1.3, 2), shares=1000,
        stop_price=round(close * 1.1, 2),
    )
    tracker.update(codes=[code], date=stop_target, notify=False)
    assert tracker.state_of(code)["state"] == "止损"


def test_position_pnl_calculation(services, tracker, trading_dates) -> None:
    """持仓浮动盈亏计算。"""
    code = "600000.SH"
    target = trading_dates[100]
    close = float(services.data.reader.read_daily(code, end=target)["close"].iloc[-1])

    services.tracker.add_watch(
        code=code, group="测试", strategy="ma_cross",
        cost_price=round(close / 1.1, 2), shares=1000,
    )
    tracker.update(codes=[code], date=target, notify=False)
    state = tracker.state_of(code)
    assert state["unrealized_pct"] == pytest.approx(0.1, abs=0.01)
    assert state["unrealized_pnl"] > 0


# ----------------------------------------------------------------------
# 手动操作
# ----------------------------------------------------------------------
def test_set_state_manually(services, tracker) -> None:
    """手动设置状态并写入历史。"""
    services.tracker.add_watch(code="600000.SH", group="测试")
    record = tracker.set_state("600000.SH", "持仓", reason="已建仓")
    assert record["new_state"] == "持仓"
    assert record["trigger_type"] == "manual"
    assert tracker.state_of("600000.SH")["state"] == "持仓"

    with pytest.raises(ValueError):
        tracker.set_state("600000.SH", "非法状态")


def test_pause_and_resume(services, tracker, trading_dates) -> None:
    """暂停后不再自动流转，恢复后继续。"""
    services.tracker.add_watch(code="600000.SH", group="测试")
    services.tracker.pause("600000.SH", paused=True)
    tracker.update(codes=["600000.SH"], date=trading_dates[120], notify=False)
    assert tracker.state_of("600000.SH")["state"] == "暂停"

    services.tracker.pause("600000.SH", paused=False)
    tracker.update(codes=["600000.SH"], date=trading_dates[121], notify=False)
    assert tracker.state_of("600000.SH")["state"] != "暂停"


def test_timeline(services, tracker, trading_dates) -> None:
    """状态时间线包含状态与信号事件。"""
    _add_watch(services, codes=["600000.SH"])
    for d in trading_dates[60:140]:
        tracker.update(codes=["600000.SH"], date=d, notify=False)

    timeline = tracker.timeline("600000.SH")
    assert timeline["code"] == "600000.SH"
    assert isinstance(timeline["events"], list)
    dates = [e["date"] for e in timeline["events"] if e.get("date")]
    assert dates == sorted(dates)


def test_dashboard(services, tracker, trading_dates) -> None:
    """仪表盘聚合数据。"""
    _add_watch(services)
    for d in trading_dates[60:140]:
        tracker.update(codes=CODES, date=d, notify=False)

    data = tracker.dashboard()
    assert data["watch_count"] == 4
    assert data["tracked_count"] == 4
    assert isinstance(data["state_distribution"], dict)
    assert isinstance(data["risk_distribution"], dict)
    assert "storage" in data


def test_dashboard_empty(services, tracker) -> None:
    """空关注池时仪表盘不应崩溃。"""
    data = tracker.dashboard()
    assert data["watch_count"] == 0
    assert data["today_signals"] == []


def test_unknown_code_from_backtest_positions(services, tracker) -> None:
    """临时追踪未加入关注池的股票也应可用。"""
    result = tracker.update(codes=["600000.SH"], date=None, notify=False)
    assert result.processed == 1
    assert tracker.state_of("600000.SH") is not None
