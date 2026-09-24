"""股票状态机。

状态集合（可通过 ``config.yaml`` 的 ``tracker.states`` 扩展）::

    候选 / 观察 / 触发 / 买入 / 持仓 / 减仓 / 清仓 / 止盈 / 止损 / 失效 / 暂停

设计要点：
    * :meth:`StateMachine.decide` 是 **纯函数式** 的：给定相同的输入必然得到
      相同的输出，因此历史回放（replay）与实时更新结果完全一致；
    * 决策优先级：止损 > 止盈 > 清仓 > 减仓 > 持仓 > 买入 > 触发 > 观察 > 失效；
    * 优先级顺序保证了「风险事件永远优先于普通信号」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.logging import get_logger

logger = get_logger("state_machine")

# ----------------------------------------------------------------------
# 内置状态
# ----------------------------------------------------------------------
STATE_CANDIDATE = "候选"
STATE_WATCH = "观察"
STATE_TRIGGERED = "触发"
STATE_BUY = "买入"
STATE_HOLDING = "持仓"
STATE_REDUCE = "减仓"
STATE_CLEAR = "清仓"
STATE_TAKE_PROFIT = "止盈"
STATE_STOP_LOSS = "止损"
STATE_INVALID = "失效"
STATE_PAUSED = "暂停"

DEFAULT_STATES: List[str] = [
    STATE_CANDIDATE,
    STATE_WATCH,
    STATE_TRIGGERED,
    STATE_BUY,
    STATE_HOLDING,
    STATE_REDUCE,
    STATE_CLEAR,
    STATE_TAKE_PROFIT,
    STATE_STOP_LOSS,
    STATE_INVALID,
    STATE_PAUSED,
]

#: 视为「持有中」的状态
HOLDING_STATES = {STATE_BUY, STATE_HOLDING, STATE_REDUCE}

#: 视为「已结束」的状态
CLOSED_STATES = {STATE_CLEAR, STATE_STOP_LOSS, STATE_TAKE_PROFIT, STATE_INVALID}

#: 合法状态迁移表（用于手动改状态的合法性校验）
TRANSITIONS: Dict[str, set] = {
    STATE_CANDIDATE: {STATE_WATCH, STATE_TRIGGERED, STATE_BUY, STATE_INVALID, STATE_PAUSED},
    STATE_WATCH: {STATE_CANDIDATE, STATE_TRIGGERED, STATE_BUY, STATE_INVALID, STATE_PAUSED},
    STATE_TRIGGERED: {STATE_CANDIDATE, STATE_WATCH, STATE_BUY, STATE_INVALID, STATE_PAUSED},
    STATE_BUY: {
        STATE_HOLDING, STATE_REDUCE, STATE_CLEAR, STATE_TAKE_PROFIT,
        STATE_STOP_LOSS, STATE_INVALID, STATE_PAUSED,
    },
    STATE_HOLDING: {
        STATE_BUY, STATE_REDUCE, STATE_CLEAR, STATE_TAKE_PROFIT,
        STATE_STOP_LOSS, STATE_INVALID, STATE_PAUSED,
    },
    STATE_REDUCE: {
        STATE_HOLDING, STATE_BUY, STATE_CLEAR, STATE_TAKE_PROFIT,
        STATE_STOP_LOSS, STATE_INVALID, STATE_PAUSED,
    },
    STATE_CLEAR: {
        STATE_CANDIDATE, STATE_WATCH, STATE_TRIGGERED, STATE_BUY,
        STATE_INVALID, STATE_PAUSED,
    },
    STATE_TAKE_PROFIT: {
        STATE_CANDIDATE, STATE_WATCH, STATE_CLEAR, STATE_BUY, STATE_PAUSED,
    },
    STATE_STOP_LOSS: {
        STATE_CANDIDATE, STATE_WATCH, STATE_CLEAR, STATE_INVALID, STATE_PAUSED,
    },
    STATE_INVALID: {STATE_CANDIDATE, STATE_WATCH, STATE_TRIGGERED, STATE_PAUSED},
    # 暂停状态由用户手动控制，可以恢复到任意状态
    STATE_PAUSED: set(DEFAULT_STATES),
}

# 风险等级
RISK_NORMAL = "normal"
RISK_WARNING = "warning"
RISK_DANGER = "danger"

RISK_LABELS = {RISK_NORMAL: "正常", RISK_WARNING: "警示", RISK_DANGER: "危险"}


@dataclass
class RiskAssessment:
    """风险状态评估结果。"""

    level: str = RISK_NORMAL
    flags: List[str] = field(default_factory=list)
    #: 稳定标识（不带数值），用于判断「风险是否为今日新增」并避免重复提醒
    codes: List[str] = field(default_factory=list)
    hit_stop: bool = False
    hit_target: bool = False
    near_stop: bool = False
    break_ma: bool = False
    volume_drop: bool = False
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def level_text(self) -> str:
        """风险等级中文描述。"""
        return RISK_LABELS.get(self.level, self.level)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "level": self.level,
            "level_text": self.level_text,
            "flags": list(self.flags),
            "codes": list(self.codes),
            "hit_stop": self.hit_stop,
            "hit_target": self.hit_target,
            "near_stop": self.near_stop,
            "break_ma": self.break_ma,
            "volume_drop": self.volume_drop,
            "detail": dict(self.detail),
        }


@dataclass
class StateContext:
    """状态决策输入上下文。"""

    signal: int = 0                       # 1 买入 / -1 卖出 / 0 无信号
    is_new_signal: bool = False           # 是否为新出现的信号
    has_position: bool = False            # 是否持有仓位
    risk: RiskAssessment = field(default_factory=RiskAssessment)
    price: Optional[float] = None
    cost_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    near_signal: bool = False             # 是否接近触发条件（用于「触发」状态）
    enabled: bool = True                  # 关注池条目是否启用
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """状态决策结果。"""

    state: str
    prev_state: str
    changed: bool
    reason: str
    priority: int = 99

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "state": self.state,
            "prev_state": self.prev_state,
            "changed": self.changed,
            "reason": self.reason,
            "priority": self.priority,
        }


class StateMachine:
    """股票状态机。

    :param states: 允许的状态集合；默认使用内置 11 个状态
    """

    def __init__(self, states: Optional[Sequence[str]] = None) -> None:
        self.states: List[str] = list(states) if states else list(DEFAULT_STATES)
        # 扩展状态：不在内置迁移表中的自定义状态允许迁移到任意状态
        self.transitions: Dict[str, set] = {
            k: set(v) for k, v in TRANSITIONS.items()
        }
        for s in self.states:
            self.transitions.setdefault(s, set(self.states))

    # ------------------------------------------------------------------
    def is_valid_state(self, state: str) -> bool:
        """判断状态名是否合法。"""
        return state in self.states

    def can_transition(self, src: Optional[str], dst: str) -> bool:
        """判断从 ``src`` 迁移到 ``dst`` 是否合法。

        :param src: 原状态；为 None 或空表示新建
        :param dst: 目标状态
        """
        if not self.is_valid_state(dst):
            return False
        if not src:
            return True
        if dst == src:
            return True
        # 用户自定义扩展的状态（不在内置迁移表中）允许自由进出，
        # 这样新增状态无需修改内置迁移表即可使用。
        if dst not in TRANSITIONS:
            return True
        allowed = self.transitions.get(src, set(self.states))
        return dst in allowed

    # ------------------------------------------------------------------
    def decide(self, current: Optional[str], ctx: StateContext) -> Decision:
        """根据上下文推导目标状态。

        :param current: 当前状态
        :param ctx: 决策上下文
        :return: :class:`Decision`
        """
        prev = current or STATE_CANDIDATE

        # 0. 暂停优先级最高：处于「暂停」的股票不做任何自动流转，
        #    恢复由 :meth:`StockTracker.pause` 显式还原状态后完成。
        if prev == STATE_PAUSED:
            return Decision(prev, prev, False, "股票处于暂停状态，跳过自动更新", 0)
        if not ctx.enabled:
            return Decision(
                STATE_PAUSED, prev, prev != STATE_PAUSED, "关注项已暂停，跳过自动更新", 0
            )

        risk = ctx.risk or RiskAssessment()


        # 1. 持仓状态下的风险管理（最高优先级）
        if ctx.has_position:
            # 已经离场（清仓 / 止损 / 止盈 / 失效）后不再重复判定风险，
            # 否则会出现「清仓 → 减仓 → 持仓」的反复跳动；
            # 只有出现新的买入信号才重新建仓。
            if prev in CLOSED_STATES:
                if ctx.signal > 0 and ctx.is_new_signal:
                    return Decision(STATE_BUY, prev, True, "离场后出现新的买入信号，重新建仓", 7)
                return Decision(prev, prev, False, "已离场，等待新的买入信号", 5)
            if risk.hit_stop:
                return Decision(
                    STATE_STOP_LOSS, prev, prev != STATE_STOP_LOSS,
                    "触发止损：最新价已跌破止损价", 1,
                )
            if risk.hit_target:
                return Decision(
                    STATE_TAKE_PROFIT, prev, prev != STATE_TAKE_PROFIT,
                    "触发止盈：最新价已达到目标价", 2,
                )
            if ctx.signal < 0:
                return Decision(
                    STATE_CLEAR, prev, prev != STATE_CLEAR,
                    "策略发出卖出信号，清仓离场", 3,
                )
            if risk.level == RISK_DANGER:
                return Decision(
                    STATE_REDUCE, prev, prev != STATE_REDUCE,
                    f"风险等级为危险（{'、'.join(risk.flags) or '未知'}），建议减仓", 4,
                )
            if risk.level == RISK_WARNING:
                return Decision(
                    STATE_REDUCE, prev, prev != STATE_REDUCE,
                    f"风险警示（{'、'.join(risk.flags) or '未知'}），建议减仓", 5,
                )
            # 区分「首次登记为持仓」和「原本就在持仓状态」：前者是建仓动作，
            # 后者才是继续持有，避免把建仓写成「继续持有」引起误解。
            if prev in HOLDING_STATES:
                return Decision(STATE_HOLDING, prev, prev != STATE_HOLDING, "持仓正常，继续持有", 6)
            return Decision(
                STATE_HOLDING, prev, True,
                f"已登记持仓（成本价 {ctx.cost_price:.2f}），进入持仓跟踪"
                if ctx.cost_price else "已登记持仓，进入持仓跟踪",
                6,
            )

        # 2. 无持仓：信号驱动的状态流转
        if ctx.signal > 0:
            if ctx.is_new_signal:
                return Decision(
                    STATE_BUY, prev, prev != STATE_BUY,
                    "策略产生新的买入信号", 7,
                )
            if prev in (STATE_BUY, STATE_HOLDING):
                return Decision(
                    STATE_HOLDING, prev, prev != STATE_HOLDING,
                    "买入信号持续，视为已建仓持有", 8,
                )
            return Decision(
                STATE_TRIGGERED, prev, prev != STATE_TRIGGERED,
                "买入信号持续但尚未建仓，标记为触发", 9,
            )

        if ctx.signal < 0:
            if prev in CLOSED_STATES:
                return Decision(prev, prev, False, "已处于结束状态，无信号变化", 12)
            return Decision(
                STATE_INVALID, prev, prev != STATE_INVALID,
                "无持仓情况下出现卖出信号，信号失效", 10,
            )

        # 3. 无信号：接近触发条件则进入「触发」，否则回到「观察」
        if ctx.near_signal:
            return Decision(
                STATE_TRIGGERED, prev, prev != STATE_TRIGGERED,
                "接近买入条件，进入触发观察", 11,
            )
        if prev == STATE_CANDIDATE:
            return Decision(STATE_WATCH, prev, prev != STATE_WATCH, "纳入观察池", 13)
        return Decision(prev, prev, False, "无新信号，状态保持", 14)

    # ------------------------------------------------------------------
    def apply(
        self,
        current: Optional[str],
        ctx: StateContext,
        force_state: Optional[str] = None,
    ) -> Decision:
        """在 :meth:`decide` 基础上应用强制状态（手动改状态）。"""
        if force_state:
            if not self.is_valid_state(force_state):
                raise ValueError(f"非法状态：{force_state}")
            prev = current or STATE_CANDIDATE
            return Decision(
                force_state, prev, force_state != prev, f"手动设置状态为「{force_state}」", 0
            )
        return self.decide(current, ctx)

    @staticmethod
    def is_holding(state: Optional[str]) -> bool:
        """判断状态是否代表持仓中。"""
        return state in HOLDING_STATES

    @staticmethod
    def is_closed(state: Optional[str]) -> bool:
        """判断状态是否代表已结束。"""
        return state in CLOSED_STATES

    def to_dict(self) -> Dict[str, Any]:
        """导出状态机定义，供 Web UI 渲染。"""
        return {
            "states": self.states,
            "transitions": {k: sorted(v) for k, v in self.transitions.items()},
            "holding_states": sorted(HOLDING_STATES),
            "closed_states": sorted(CLOSED_STATES),
        }


def evaluate_risk(
    row: Any,
    cost_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    target_price: Optional[float] = None,
    ma_value: Optional[float] = None,
    volume_ma: Optional[float] = None,
    near_stop_ratio: float = 0.02,
    vol_spike_ratio: float = 1.5,
    drop_pct: float = 0.03,
) -> RiskAssessment:
    """评估单只股票的风险状态。

    :param row: 当日行情（需含 ``close``、可选 ``volume``、``pct_change``）
    :param cost_price: 成本价
    :param stop_price: 止损价
    :param target_price: 目标价
    :param ma_value: 关键均线值（如 20 日均线）
    :param volume_ma: 均量
    :param near_stop_ratio: 距止损价多近视为「接近止损」
    :param vol_spike_ratio: 放量倍数阈值
    :param drop_pct: 认定「下跌」的跌幅阈值
    :return: :class:`RiskAssessment`
    """
    risk = RiskAssessment()
    try:
        close = float(row.get("close")) if row is not None else float("nan")
    except (TypeError, ValueError):
        close = float("nan")

    if not close or close != close:  # NaN 检查
        risk.flags.append("无有效行情")
        risk.codes.append("no_data")
        risk.detail["close"] = None
        return risk

    pct = row.get("pct_change") if row is not None else None
    try:
        pct = float(pct) if pct is not None else float("nan")
    except (TypeError, ValueError):
        pct = float("nan")

    volume = row.get("volume") if row is not None else None
    try:
        volume = float(volume) if volume is not None else float("nan")
    except (TypeError, ValueError):
        volume = float("nan")

    detail: Dict[str, Any] = {
        "close": round(close, 4),
        "cost_price": cost_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "ma_value": ma_value,
        "volume_ma": volume_ma,
        "pct_change": round(pct, 6) if pct == pct else None,
    }

    # 止损
    if stop_price and stop_price > 0:
        if close <= stop_price:
            risk.hit_stop = True
            risk.flags.append(f"跌破止损价（{close:.2f} ≤ {stop_price:.2f}）")
            risk.codes.append("hit_stop")
        elif close <= stop_price * (1 + near_stop_ratio):
            risk.near_stop = True
            risk.flags.append(f"接近止损价（距 {near_stop_ratio * 100:.1f}% 以内）")
            risk.codes.append("near_stop")

    # 止盈
    if target_price and target_price > 0 and close >= target_price:
        risk.hit_target = True
        risk.flags.append(f"达到目标价（{close:.2f} ≥ {target_price:.2f}）")
        risk.codes.append("hit_target")

    # 跌破关键均线
    if ma_value and ma_value > 0 and close < ma_value:
        risk.break_ma = True
        risk.flags.append(f"跌破关键均线（{ma_value:.2f}）")
        risk.codes.append("break_ma")

    # 放量下跌
    if volume == volume and volume_ma and volume_ma > 0:
        ratio = volume / volume_ma
        detail["volume_ratio"] = round(ratio, 3)
        if ratio >= vol_spike_ratio and pct == pct and pct <= -drop_pct:
            risk.volume_drop = True
            risk.flags.append(
                f"放量下跌（量比 {ratio:.2f}，跌幅 {pct * 100:.2f}%）"
            )
            risk.codes.append("volume_drop")

    # 成本价浮亏
    if cost_price and cost_price > 0:
        loss = close / cost_price - 1.0
        detail["float_pct"] = round(loss, 6)
        if loss <= -0.15:
            risk.flags.append(f"浮亏较大（{loss * 100:.2f}%）")
            risk.codes.append("big_loss")

    if risk.hit_stop or risk.volume_drop:
        risk.level = RISK_DANGER
    elif risk.near_stop or risk.break_ma:
        risk.level = RISK_WARNING
    else:
        risk.level = RISK_NORMAL

    risk.detail = detail
    return risk
