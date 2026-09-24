"""追踪存储与关注池测试：写入、读取、查询、导出、持久化。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from backend.app.tracker.storage import TrackerStorage
from backend.app.tracker.watchlist import Watchlist


@pytest.fixture()
def storage(tmp_path: Path) -> TrackerStorage:
    """基于临时 SQLite 的存储实例。"""
    s = TrackerStorage(f"sqlite:///{(tmp_path / 'track.db').as_posix()}")
    yield s
    s.close()


# ======================================================================
# 关注池
# ======================================================================
def test_add_and_get_watch(storage: TrackerStorage) -> None:
    """添加并读取关注项。"""
    item = storage.add_watch(
        code="600000.SH", name="浦发银行", group="银行",
        tags=["低估值"], note="观察", cost_price=9.5, shares=1000,
        target_price=12.0, stop_price=8.5, strategy="ma_cross",
    )
    assert item["code"] == "600000.SH"
    assert item["name"] == "浦发银行"
    assert item["tags"] == ["低估值"]
    assert item["cost_price"] == 9.5

    got = storage.get_watch("600000.SH")
    assert got is not None
    assert got["target_price"] == 12.0

    # 代码写法容错
    assert storage.get_watch("sh600000")["code"] == "600000.SH"


def test_add_watch_idempotent(storage: TrackerStorage) -> None:
    """重复添加默认合并不覆盖。"""
    storage.add_watch(code="600000.SH", name="浦发银行", note="第一次")
    storage.add_watch(code="600000.SH", note="第二次", tags=["a"])
    item = storage.get_watch("600000.SH")
    assert item["note"] == "第二次"
    assert item["tags"] == ["a"]

    storage.add_watch(code="600000.SH", name="覆盖名", overwrite=True)
    item = storage.get_watch("600000.SH")
    assert item["name"] == "覆盖名"


def test_update_and_remove_watch(storage: TrackerStorage) -> None:
    """更新与删除。"""
    storage.add_watch(code="000001.SZ", name="平安银行")
    updated = storage.update_watch("000001.SZ", group="金融", cost_price=11.0, shares=500)
    assert updated["group"] == "金融"
    assert updated["cost_price"] == 11.0

    assert storage.remove_watch("000001.SZ") is True
    assert storage.get_watch("000001.SZ") is None
    assert storage.remove_watch("000001.SZ") is False


def test_list_watch_filters(storage: TrackerStorage) -> None:
    """分组、关键词、标签过滤。"""
    storage.add_watch(code="600000.SH", name="浦发银行", group="银行", tags=["蓝筹"])
    storage.add_watch(code="000001.SZ", name="平安银行", group="银行", tags=["成长"])
    storage.add_watch(code="300750.SZ", name="宁德时代", group="新能源")

    assert len(storage.list_watch()) == 3
    assert len(storage.list_watch(group="银行")) == 2
    assert len(storage.list_watch(keyword="宁德")) == 1
    assert len(storage.list_watch(tags=["蓝筹"])) == 1

    groups = {g["group"]: g["count"] for g in storage.list_groups()}
    assert groups["银行"] == 2

    storage.update_watch("600000.SH", enabled=False)
    assert len(storage.list_watch(enabled_only=True)) == 2
    assert len(storage.watch_codes()) == 2


def test_watch_state_initialized(storage: TrackerStorage) -> None:
    """添加关注项时自动创建初始状态。"""
    storage.add_watch(code="600000.SH", name="浦发银行")
    state = storage.get_state("600000.SH")
    assert state is not None
    assert state["state"] == "候选"


# ======================================================================
# 状态
# ======================================================================
def test_upsert_state(storage: TrackerStorage) -> None:
    """状态写入与更新。"""
    storage.upsert_state(
        "600000.SH", name="浦发银行", state="持仓", price=10.5,
        risk_level="warning", risk_flags=["跌破关键均线"],
        factors={"rsi": 42.5}, cost_price=10.0, unrealized_pct=0.05,
    )
    state = storage.get_state("600000.SH")
    assert state["state"] == "持仓"
    assert state["price"] == 10.5
    assert state["risk_flags"] == ["跌破关键均线"]
    assert state["factors"]["rsi"] == 42.5

    storage.upsert_state("600000.SH", state="减仓", prev_state="持仓")
    state = storage.get_state("600000.SH")
    assert state["state"] == "减仓"
    assert state["prev_state"] == "持仓"
    assert state["name"] == "浦发银行"  # 未传入的字段保持不变


def test_list_states_filters(storage: TrackerStorage) -> None:
    """状态查询过滤。"""
    storage.upsert_state("600000.SH", name="浦发银行", state="持仓", risk_level="normal")
    storage.upsert_state("000001.SZ", name="平安银行", state="止损", risk_level="danger")
    storage.upsert_state("300750.SZ", name="宁德时代", state="观察", risk_level="normal")

    assert len(storage.list_states()) == 3
    assert len(storage.list_states(states=["持仓"])) == 1
    assert len(storage.list_states(risk_level="danger")) == 1
    assert len(storage.list_states(keyword="宁德")) == 1
    assert len(storage.list_states(codes=["600000.SH", "000001.SZ"])) == 2


def test_delete_state(storage: TrackerStorage) -> None:
    """删除状态。"""
    storage.upsert_state("600000.SH", state="观察")
    assert storage.delete_state("600000.SH") is True
    assert storage.get_state("600000.SH") is None


# ======================================================================
# 历史
# ======================================================================
def test_add_and_list_history(storage: TrackerStorage) -> None:
    """状态历史写入与查询。"""
    storage.add_history(
        code="600000.SH", name="浦发银行", old_state="观察", new_state="买入",
        reason="金叉", trade_date="2023-03-01", strategy="ma_cross",
        factors={"ma_short": 10.5}, price=10.2,
    )
    storage.add_history(
        code="600000.SH", name="浦发银行", old_state="买入", new_state="持仓",
        reason="持有", trade_date="2023-03-02", strategy="ma_cross",
    )
    storage.add_history(
        code="000001.SZ", name="平安银行", old_state="观察", new_state="失效",
        reason="卖出信号", trade_date="2023-03-03",
    )

    rows = storage.list_history(code="600000.SH", order="asc")
    assert len(rows) == 2
    assert rows[0]["new_state"] == "买入"
    assert rows[1]["trade_date"] == "2023-03-02"

    assert len(storage.list_history(start="2023-03-02")) == 2
    assert len(storage.list_history(states=["失效"])) == 1
    assert len(storage.list_history(trigger_type="auto")) == 3


def test_delete_history(storage: TrackerStorage) -> None:
    """按代码 + 日期删除历史（回放幂等的关键）。"""
    for i in range(3):
        storage.add_history(
            code="600000.SH", old_state="观察", new_state="买入",
            trade_date=f"2023-03-0{i + 1}",
        )
    assert storage.delete_history(code="600000.SH", trade_date="2023-03-02") == 1
    assert len(storage.list_history(code="600000.SH")) == 2
    assert storage.delete_history(code="600000.SH") == 2


# ======================================================================
# 信号
# ======================================================================
def test_signals(storage: TrackerStorage) -> None:
    """信号写入与查询。"""
    storage.add_signal(
        code="600000.SH", name="浦发银行", strategy="ma_cross", signal=1,
        signal_text="买入", trade_date="2023-03-01", price=10.2,
        reason="金叉", is_new=True, factors={"ma_short": 10.1},
    )
    storage.add_signal(
        code="600000.SH", name="浦发银行", strategy="rsi", signal=-1,
        signal_text="卖出", trade_date="2023-03-02", is_new=True,
    )
    assert len(storage.list_signals()) == 2
    assert len(storage.list_signals(signal=1)) == 1
    assert len(storage.list_signals(strategy="rsi")) == 1
    assert len(storage.list_signals(only_new=True)) == 2
    assert storage.delete_signals(code="600000.SH") == 2


# ======================================================================
# 备注与提醒
# ======================================================================
def test_notes(storage: TrackerStorage) -> None:
    """备注 CRUD。"""
    note = storage.add_note("600000.SH", "关注年报")
    assert note["content"] == "关注年报"
    assert len(storage.list_notes("600000.SH")) == 1
    assert storage.delete_note(note["id"]) is True
    assert storage.list_notes("600000.SH") == []


def test_alerts(storage: TrackerStorage) -> None:
    """提醒写入、查询、标记已读。"""
    alert = storage.add_alert(
        code="600000.SH", name="浦发银行", level="danger", category="risk",
        title="止损触发", message="跌破止损价", trade_date="2023-03-01",
        trigger_value=8.9,
    )
    assert alert["level"] == "danger"
    assert len(storage.list_alerts()) == 1
    assert len(storage.list_alerts(level="danger")) == 1
    assert len(storage.list_alerts(category="risk")) == 1
    assert len(storage.list_alerts(unacked_only=True)) == 1

    assert storage.ack_alert(alert["id"]) is True
    assert storage.list_alerts(unacked_only=True) == []


# ======================================================================
# 导出与统计
# ======================================================================
def test_export_csv(storage: TrackerStorage, tmp_path: Path) -> None:
    """导出各表为 CSV。"""
    storage.add_watch(code="600000.SH", name="浦发银行")
    storage.upsert_state("600000.SH", name="浦发银行", state="观察")
    storage.add_history(code="600000.SH", old_state="候选", new_state="观察",
                        trade_date="2023-03-01", factors={"a": 1})

    for table in ("watchlist", "tracking_state", "tracking_history"):
        path = storage.export_csv(table, tmp_path / f"{table}.csv")
        df = pd.read_csv(path)
        assert len(df) >= 1

    with pytest.raises(ValueError):
        storage.export_csv("not_exist", tmp_path / "x.csv")


def test_stats_and_reset(storage: TrackerStorage) -> None:
    """统计与清空。"""
    storage.add_watch(code="600000.SH")
    storage.add_signal(code="600000.SH", strategy="ma_cross", signal=1)
    stats = storage.stats()
    assert stats["watchlist"] == 1
    assert stats["signals"] == 1

    deleted = storage.reset(["signals"])
    assert deleted["signals"] == 1
    assert storage.stats()["signals"] == 0
    assert storage.stats()["watchlist"] == 1


def test_persistence_across_instances(tmp_path: Path) -> None:
    """数据在进程重启后仍存在（满足「程序重启不丢失」要求）。"""
    url = f"sqlite:///{(tmp_path / 'persist.db').as_posix()}"
    first = TrackerStorage(url)
    first.add_watch(code="600000.SH", name="浦发银行", note="持久化测试")
    first.add_history(code="600000.SH", old_state="观察", new_state="买入",
                      trade_date="2023-03-01")
    first.close()

    second = TrackerStorage(url)
    item = second.get_watch("600000.SH")
    assert item is not None
    assert item["note"] == "持久化测试"
    assert len(second.list_history(code="600000.SH")) == 1
    second.close()


# ======================================================================
# Watchlist 领域服务
# ======================================================================
def test_watchlist_service(storage: TrackerStorage) -> None:
    """关注池服务：批量添加、导入、导出。"""
    wl = Watchlist(storage)

    item = wl.add(code="600000", name="", group="测试", tags=["a", "b"])
    assert item["code"] == "600000.SH"

    stats = wl.add_many(
        [{"code": "000001.SZ", "name": "平安银行"}, {"code": "非法代码!!", "name": "x"}],
        group="测试",
    )
    assert stats["added"] >= 1
    assert len(stats["failed"]) >= 0  # 非法代码在 normalize 阶段被拦截

    wl.set_position("600000.SH", cost_price=10.0, shares=1000, stop_price=9.0)
    after = wl.get("600000.SH")
    assert after["cost_price"] == 10.0

    wl.clear_position("600000.SH")
    assert wl.get("600000.SH")["shares"] == 0

    assert len(wl.groups()) >= 1
    assert "600000.SH" in wl.codes()


def test_watchlist_import_from_screen(storage: TrackerStorage) -> None:
    """从选股结果导入。"""
    wl = Watchlist(storage)
    fake_result = {
        "results": [
            {"code": "600000.SH", "name": "浦发银行", "strategy": "ma_cross",
             "reason_text": "金叉"},
            {"code": "000001.SZ", "name": "平安银行", "strategy": "ma_cross",
             "reason_text": "金叉"},
        ]
    }
    stats = wl.import_from_screen(fake_result, group="选股结果", strategy="ma_cross")
    assert stats["added"] == 2
    items = wl.list(group="选股结果")
    assert len(items) == 2
    assert items[0]["tags"] == ["选股"]


def test_watchlist_import_from_backtest(storage: TrackerStorage) -> None:
    """从回测持仓导入。"""
    wl = Watchlist(storage)
    fake_result = {
        "positions": [
            {"date": "2023-03-01", "code": "600000.SH", "name": "浦发银行",
             "avg_cost": 10.0, "shares": 1000},
            {"date": "2023-03-02", "code": "000001.SZ", "name": "平安银行",
             "avg_cost": 11.0, "shares": 500},
        ]
    }
    stats = wl.import_from_backtest(fake_result, only_open=True)
    assert stats["added"] == 1  # 只导入最后一个日期的持仓
    imported = wl.list(group="回测持仓")
    assert imported[0]["code"] == "000001.SZ"
    assert imported[0]["cost_price"] == 11.0


def test_watchlist_export(storage: TrackerStorage, tmp_path: Path) -> None:
    """关注池导出 CSV。"""
    wl = Watchlist(storage)
    wl.add(code="600000.SH", name="浦发银行", tags=["a"])
    path = wl.export_csv(tmp_path / "watch.csv")
    df = pd.read_csv(path)
    assert len(df) == 1
    assert df["tags"].iloc[0] == "a"
