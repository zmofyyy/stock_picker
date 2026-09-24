"""状态机与风险判定测试。"""

from __future__ import annotations

import pytest

from backend.app.tracker.state_machine import (
    DEFAULT_STATES,
    RISK_DANGER,
    RISK_NORMAL,
    RISK_WARNING,
    RiskAssessment,
    StateContext,
    StateMachine,
    evaluate_risk,
)


@pytest.fixture()
def machine() -> StateMachine:
    """默认状态机。"""
    return StateMachine()


def test_default_states_cover_requirement(machine: StateMachine) -> None:
    """需求要求的状态必须全部存在。"""
    required = {
        "候选", "观察", "触发", "买入", "持仓", "减仓",
        "清仓", "止盈", "止损", "失效", "暂停",
    }
    assert required <= set(machine.states)
    assert len(DEFAULT_STATES) == 11


def test_valid_state_check(machine: StateMachine) -> None:
    """状态合法性校验。"""
    assert machine.is_valid_state("持仓")
    assert not machine.is_valid_state("不存在的状态")


def test_transition_rules(machine: StateMachine) -> None:
    """标准迁移表。"""
    assert machine.can_transition("观察", "触发")
    assert machine.can_transition("持仓", "止损")
    assert not machine.can_transition("候选", "止盈")
    assert machine.can_transition("暂停", "持仓")  # 暂停可恢复
    assert machine.can_transition(None, "候选")


def test_custom_states() -> None:
    """支持自定义状态扩展。"""
    machine = StateMachine(["候选", "观察", "自定义状态"])
    assert machine.is_valid_state("自定义状态")
    assert machine.can_transition("候选", "自定义状态")


def test_new_stock_becomes_watch(machine: StateMachine) -> None:
    """无信号的新股票进入「观察」。"""
    decision = machine.decide("候选", StateContext())
    assert decision.state == "观察"
    assert decision.changed is True


def test_buy_signal_new(machine: StateMachine) -> None:
    """新的买入信号 → 买入。"""
    ctx = StateContext(signal=1, is_new_signal=True, has_position=False)
    decision = machine.decide("观察", ctx)
    assert decision.state == "买入"
    assert decision.changed


def test_buy_signal_persistent_no_position(machine: StateMachine) -> None:
    """买入信号持续但未建仓 → 触发。"""
    ctx = StateContext(signal=1, is_new_signal=False, has_position=False)
    assert machine.decide("观察", ctx).state == "触发"


def test_buy_to_holding(machine: StateMachine) -> None:
    """买入后下一日信号持续 → 持仓。"""
    ctx = StateContext(signal=1, is_new_signal=False, has_position=True)
    assert machine.decide("买入", ctx).state == "持仓"


def test_holding_normal(machine: StateMachine) -> None:
    """持仓且无风险 → 保持持仓。"""
    ctx = StateContext(signal=0, has_position=True)
    decision = machine.decide("持仓", ctx)
    assert decision.state == "持仓"
    assert decision.changed is False


def test_stop_loss_priority(machine: StateMachine) -> None:
    """止损优先级最高，即使同时出现止盈条件。"""
    risk = RiskAssessment(level=RISK_DANGER, hit_stop=True, hit_target=True, flags=["跌破止损价"])
    ctx = StateContext(signal=-1, has_position=True, risk=risk)
    assert machine.decide("持仓", ctx).state == "止损"


def test_take_profit(machine: StateMachine) -> None:
    """止盈触发。"""
    risk = RiskAssessment(level=RISK_NORMAL, hit_target=True, flags=["达到目标价"])
    ctx = StateContext(signal=0, has_position=True, risk=risk)
    assert machine.decide("持仓", ctx).state == "止盈"


def test_sell_signal_clears_position(machine: StateMachine) -> None:
    """持仓状态收到卖出信号 → 清仓。"""
    ctx = StateContext(signal=-1, has_position=True)
    assert machine.decide("持仓", ctx).state == "清仓"


def test_warning_reduces_position(machine: StateMachine) -> None:
    """风险警示 → 减仓。"""
    risk = RiskAssessment(level=RISK_WARNING, break_ma=True, flags=["跌破关键均线"])
    ctx = StateContext(signal=0, has_position=True, risk=risk)
    assert machine.decide("持仓", ctx).state == "减仓"


def test_sell_signal_without_position_invalidates(machine: StateMachine) -> None:
    """无持仓却出现卖出信号 → 失效。"""
    ctx = StateContext(signal=-1, has_position=False)
    assert machine.decide("观察", ctx).state == "失效"


def test_paused_skips_auto_transition(machine: StateMachine) -> None:
    """暂停状态不参与自动流转。"""
    ctx = StateContext(signal=1, is_new_signal=True, has_position=False)
    decision = machine.decide("暂停", ctx)
    assert decision.state == "暂停"
    assert decision.changed is False


def test_disabled_item_pauses(machine: StateMachine) -> None:
    """禁用的关注项自动转为暂停。"""
    ctx = StateContext(signal=1, is_new_signal=True, enabled=False)
    assert machine.decide("观察", ctx).state == "暂停"


def test_near_signal_triggers(machine: StateMachine) -> None:
    """接近买入条件 → 触发。"""
    ctx = StateContext(signal=0, near_signal=True, has_position=False)
    assert machine.decide("观察", ctx).state == "触发"


def test_force_state(machine: StateMachine) -> None:
    """手动强制状态。"""
    decision = machine.apply("观察", StateContext(), force_state="持仓")
    assert decision.state == "持仓"
    assert decision.changed
    with pytest.raises(ValueError):
        machine.apply("观察", StateContext(), force_state="瞎写的状态")


def test_decide_is_deterministic(machine: StateMachine) -> None:
    """相同输入必然得到相同输出（回放一致性的基础）。"""
    ctx = StateContext(signal=1, is_new_signal=True, has_position=False)
    results = {machine.decide("观察", ctx).state for _ in range(20)}
    assert results == {"买入"}


def test_to_dict(machine: StateMachine) -> None:
    """导出状态机定义。"""
    data = machine.to_dict()
    assert "states" in data and "transitions" in data
    assert "持仓" in data["holding_states"]


# ----------------------------------------------------------------------
# 风险判定
# ----------------------------------------------------------------------
def test_evaluate_risk_normal() -> None:
    """正常持仓无风险。"""
    risk = evaluate_risk({"close": 10.5, "volume": 1_000_000, "pct_change": 0.01},
                         cost_price=10.0, stop_price=9.0, target_price=15.0,
                         ma_value=10.0, volume_ma=1_000_000)
    assert risk.level == RISK_NORMAL
    assert risk.flags == []


def test_evaluate_risk_hit_stop() -> None:
    """跌破止损价 → 危险。"""
    risk = evaluate_risk({"close": 8.9, "volume": 1_000_000, "pct_change": -0.02},
                         cost_price=10.0, stop_price=9.0)
    assert risk.hit_stop is True
    assert risk.level == RISK_DANGER
    assert any("止损" in f for f in risk.flags)


def test_evaluate_risk_near_stop() -> None:
    """接近止损价 → 警示。"""
    risk = evaluate_risk({"close": 9.1, "pct_change": 0.0}, stop_price=9.0)
    assert risk.near_stop is True
    assert risk.level == RISK_WARNING


def test_evaluate_risk_hit_target() -> None:
    """达到目标价 → 止盈标记。"""
    risk = evaluate_risk({"close": 15.5, "pct_change": 0.05}, target_price=15.0)
    assert risk.hit_target is True
    assert any("目标价" in f for f in risk.flags)


def test_evaluate_risk_break_ma() -> None:
    """跌破关键均线 → 警示。"""
    risk = evaluate_risk({"close": 9.5, "pct_change": -0.01}, ma_value=10.0)
    assert risk.break_ma is True
    assert risk.level == RISK_WARNING


def test_evaluate_risk_volume_drop() -> None:
    """放量下跌 → 危险。"""
    risk = evaluate_risk(
        {"close": 9.0, "volume": 3_000_000, "pct_change": -0.05},
        volume_ma=1_000_000,
        vol_spike_ratio=1.5,
        drop_pct=0.03,
    )
    assert risk.volume_drop is True
    assert risk.level == RISK_DANGER


def test_evaluate_risk_invalid_close() -> None:
    """无有效行情时安全返回。"""
    risk = evaluate_risk({"close": None})
    assert risk.level == RISK_NORMAL
    assert risk.flags


def test_risk_to_dict() -> None:
    """风险对象序列化。"""
    risk = evaluate_risk({"close": 9.0, "pct_change": -0.05}, stop_price=9.0)
    data = risk.to_dict()
    assert data["level"] in (RISK_NORMAL, RISK_WARNING, RISK_DANGER)
    assert data["level_text"]
