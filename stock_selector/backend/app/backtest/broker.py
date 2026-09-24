"""回测撮合与账户模块。

职责：
    * 维护现金、持仓、成交明细；
    * 处理手续费、印花税、滑点、最低佣金；
    * 处理 A 股特有规则：一手 100 股、T+1、涨跌停无法成交。

约定：
    * 所有成交都以「目标价」为基准，买入加滑点、卖出减滑点；
    * 涨跌停判断以「成交价是否触及涨跌停价」为准，触及则视为无法成交；
    * 停牌（当日无 K 线）由引擎层过滤，本模块不重复处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.logging import get_logger
from ..data.tdx_reader import split_code

logger = get_logger("broker")

# A 股最小交易单位（1 手 = 100 股）
LOT_SIZE = 100


@dataclass
class Position:
    """持仓记录。"""

    code: str
    name: str = ""
    shares: int = 0
    available: int = 0          # 可卖数量（受 T+1 限制）
    avg_cost: float = 0.0       # 摊薄成本价（含费用）
    first_buy_date: Optional[pd.Timestamp] = None
    last_buy_date: Optional[pd.Timestamp] = None
    realized_pnl: float = 0.0   # 已实现盈亏
    total_cost: float = 0.0     # 累计买入金额（含费用）

    @property
    def cost_amount(self) -> float:
        """当前持仓成本金额。"""
        return self.avg_cost * self.shares

    def market_value(self, price: float) -> float:
        """按给定价格计算市值。"""
        return self.shares * price

    def unrealized_pnl(self, price: float) -> float:
        """浮动盈亏。"""
        return (price - self.avg_cost) * self.shares

    def unrealized_pct(self, price: float) -> float:
        """浮动盈亏比例。"""
        if self.avg_cost <= 0:
            return 0.0
        return price / self.avg_cost - 1.0

    def to_dict(self, price: Optional[float] = None) -> Dict[str, Any]:
        """转换为可序列化字典。"""
        p = float(price) if price is not None else self.avg_cost
        return {
            "code": self.code,
            "name": self.name or self.code,
            "shares": int(self.shares),
            "avg_cost": round(float(self.avg_cost), 4),
            "price": round(float(p), 4),
            "market_value": round(self.market_value(p), 2),
            "unrealized_pnl": round(self.unrealized_pnl(p), 2),
            "unrealized_pct": round(self.unrealized_pct(p) * 100, 4),
            "first_buy_date": _fmt(self.first_buy_date),
            "last_buy_date": _fmt(self.last_buy_date),
        }


@dataclass
class Trade:
    """成交明细。"""

    code: str
    name: str
    direction: str            # buy / sell
    date: pd.Timestamp
    price: float              # 实际成交价（含滑点）
    shares: int
    amount: float             # 成交金额 = price * shares
    commission: float
    stamp_tax: float
    slippage_cost: float
    reason: str = ""
    pnl: float = 0.0          # 仅卖出时有意义
    pnl_pct: float = 0.0
    hold_days: int = 0

    @property
    def total_cost(self) -> float:
        """本笔交易的税费合计。"""
        return self.commission + self.stamp_tax

    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化字典。"""
        return {
            "code": self.code,
            "name": self.name or self.code,
            "direction": self.direction,
            "direction_text": "买入" if self.direction == "buy" else "卖出",
            "date": _fmt(self.date),
            "price": round(float(self.price), 4),
            "shares": int(self.shares),
            "amount": round(float(self.amount), 2),
            "commission": round(float(self.commission), 2),
            "stamp_tax": round(float(self.stamp_tax), 2),
            "slippage_cost": round(float(self.slippage_cost), 2),
            "total_cost": round(float(self.total_cost), 2),
            "pnl": round(float(self.pnl), 2),
            "pnl_pct": round(float(self.pnl_pct) * 100, 4),
            "hold_days": int(self.hold_days),
            "reason": self.reason,
        }


@dataclass
class Order:
    """待执行委托（T 日收盘生成、T+1 执行）。"""

    code: str
    direction: str
    signal_date: pd.Timestamp
    reason: str = ""
    name: str = ""


def _fmt(ts: Optional[pd.Timestamp]) -> Optional[str]:
    """格式化时间戳。"""
    if ts is None or pd.isna(ts):
        return None
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


class Broker:
    """模拟券商账户。

    :param initial_cash: 初始资金
    :param commission: 佣金费率（双边）
    :param stamp_tax: 印花税率（仅卖出）
    :param slippage: 滑点率
    :param min_commission: 单笔最低佣金
    :param limit_pct: 主板涨跌停幅度
    :param limit_pct_star: 创业板/科创板涨跌停幅度
    :param t_plus_1: 是否启用 T+1
    """

    def __init__(
        self,
        initial_cash: float = 1_000_000.0,
        commission: float = 0.0003,
        stamp_tax: float = 0.001,
        slippage: float = 0.0002,
        min_commission: float = 5.0,
        limit_pct: float = 0.10,
        limit_pct_star: float = 0.20,
        t_plus_1: bool = True,
    ) -> None:
        self.initial_cash = float(initial_cash)
        self.cash = float(initial_cash)
        self.commission = float(commission)
        self.stamp_tax = float(stamp_tax)
        self.slippage = float(slippage)
        self.min_commission = float(min_commission)
        self.limit_pct = float(limit_pct)
        self.limit_pct_star = float(limit_pct_star)
        self.t_plus_1 = bool(t_plus_1)

        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.rejected: List[Dict[str, Any]] = []  # 未成交记录（涨跌停、资金不足等）
        self._current_date: Optional[pd.Timestamp] = None

    # ------------------------------------------------------------------
    # 规则
    # ------------------------------------------------------------------
    def limit_ratio(self, code: str) -> float:
        """返回该股票的涨跌停幅度。

        创业板（300/301）、科创板（688/689）、北交所（8/4/920）为 20%，
        其余主板为 10%。ST 股无法从本地日线文件名判断，此处不做区分。
        """
        try:
            symbol, market = split_code(code)
        except ValueError:  # pragma: no cover
            return self.limit_pct
        if market == "bj":
            return self.limit_pct_star
        if symbol.startswith(("300", "301", "688", "689")):
            return self.limit_pct_star
        return self.limit_pct

    def is_limit_up(self, code: str, price: float, prev_close: float) -> bool:
        """判断价格是否处于涨停（无法买入）。"""
        if not prev_close or prev_close <= 0:
            return False
        limit_price = round(prev_close * (1 + self.limit_ratio(code)), 2)
        return price >= limit_price - 1e-6

    def is_limit_down(self, code: str, price: float, prev_close: float) -> bool:
        """判断价格是否处于跌停（无法卖出）。"""
        if not prev_close or prev_close <= 0:
            return False
        limit_price = round(prev_close * (1 - self.limit_ratio(code)), 2)
        return price <= limit_price + 1e-6

    def on_new_day(self, date: pd.Timestamp) -> None:
        """进入新交易日：解锁 T+1 限制的可卖数量。"""
        self._current_date = pd.Timestamp(date)
        for pos in self.positions.values():
            if pos.last_buy_date is None or pd.Timestamp(pos.last_buy_date) < pd.Timestamp(date):
                pos.available = pos.shares
            elif not self.t_plus_1:
                pos.available = pos.shares

    # ------------------------------------------------------------------
    # 成交
    # ------------------------------------------------------------------
    def max_affordable_shares(self, price: float, budget: float) -> int:
        """计算给定预算下最多可买入的整手股数。

        :param price: 成交价（含滑点后的价格）
        :param budget: 可用资金
        :return: 股数（100 的整数倍）
        """
        if price <= 0 or budget <= 0:
            return 0
        # 预留手续费，按佣金+最高费率近似
        effective = price * (1 + self.commission)
        lots = int(budget // (effective * LOT_SIZE))
        return max(lots, 0) * LOT_SIZE

    def try_buy(
        self,
        date: pd.Timestamp,
        code: str,
        price: float,
        prev_close: float,
        budget: float,
        reason: str = "",
        name: str = "",
    ) -> Optional[Trade]:
        """尝试买入。

        :param date: 成交日
        :param code: 股票代码
        :param price: 目标成交价（不含滑点）
        :param prev_close: 前收盘价（用于涨跌停判断）
        :param budget: 本次计划投入资金
        :param reason: 触发原因
        :param name: 股票名称
        :return: 成交记录；未成交返回 None
        """
        if price is None or price <= 0 or pd.isna(price):
            self._reject(date, code, "buy", "无有效价格（可能停牌）")
            return None
        if self.is_limit_up(code, price, prev_close):
            self._reject(date, code, "buy", "涨停无法买入")
            return None

        exec_price = round(price * (1 + self.slippage), 4)
        budget = min(budget, self.cash)
        shares = self.max_affordable_shares(exec_price, budget) if budget > 0 else 0
        if shares <= 0:
            self._reject(date, code, "buy", "资金不足，无法买入 1 手")
            return None

        amount = exec_price * shares
        commission = max(amount * self.commission, self.min_commission)
        total = amount + commission
        # 资金不足时按手递减
        while total > self.cash and shares >= LOT_SIZE:
            shares -= LOT_SIZE
            amount = exec_price * shares
            commission = max(amount * self.commission, self.min_commission)
            total = amount + commission
        if shares <= 0:
            self._reject(date, code, "buy", "资金不足，无法买入 1 手")
            return None

        self.cash -= total
        slippage_cost = (exec_price - price) * shares

        pos = self.positions.get(code)
        if pos is None:
            pos = Position(
                code=code,
                name=name,
                shares=shares,
                available=0,
                avg_cost=total / shares,
                first_buy_date=pd.Timestamp(date),
                last_buy_date=pd.Timestamp(date),
                total_cost=total,
            )
            self.positions[code] = pos
        else:
            old_cost = pos.avg_cost * pos.shares
            pos.shares += shares
            pos.total_cost += total
            pos.avg_cost = (old_cost + total) / pos.shares
            pos.last_buy_date = pd.Timestamp(date)
            if name:
                pos.name = name
            if not self.t_plus_1:
                pos.available = pos.shares

        trade = Trade(
            code=code,
            name=name,
            direction="buy",
            date=pd.Timestamp(date),
            price=exec_price,
            shares=shares,
            amount=amount,
            commission=commission,
            stamp_tax=0.0,
            slippage_cost=slippage_cost,
            reason=reason,
        )
        self.trades.append(trade)
        return trade

    def try_sell(
        self,
        date: pd.Timestamp,
        code: str,
        price: float,
        prev_close: float,
        reason: str = "",
        shares: Optional[int] = None,
    ) -> Optional[Trade]:
        """尝试卖出。

        :param date: 成交日
        :param code: 股票代码
        :param price: 目标成交价（不含滑点）
        :param prev_close: 前收盘价
        :param reason: 触发原因
        :param shares: 卖出股数；None 表示全部可卖数量
        :return: 成交记录；未成交返回 None
        """
        pos = self.positions.get(code)
        if pos is None or pos.shares <= 0:
            return None
        if price is None or price <= 0 or pd.isna(price):
            self._reject(date, code, "sell", "无有效价格（可能停牌）")
            return None
        if self.is_limit_down(code, price, prev_close):
            self._reject(date, code, "sell", "跌停无法卖出")
            return None

        sellable = pos.available if self.t_plus_1 else pos.shares
        if sellable <= 0:
            self._reject(date, code, "sell", "T+1 限制，当日买入不可卖出")
            return None

        qty = min(int(shares), sellable) if shares else sellable
        qty = qty // LOT_SIZE * LOT_SIZE or (sellable if sellable < LOT_SIZE else 0)
        if qty <= 0:
            self._reject(date, code, "sell", "可卖数量不足 1 手")
            return None

        exec_price = round(price * (1 - self.slippage), 4)
        amount = exec_price * qty
        commission = max(amount * self.commission, self.min_commission)
        tax = amount * self.stamp_tax
        proceeds = amount - commission - tax
        slippage_cost = (price - exec_price) * qty

        cost = pos.avg_cost * qty
        pnl = proceeds - cost
        pnl_pct = (exec_price / pos.avg_cost - 1.0) if pos.avg_cost > 0 else 0.0

        self.cash += proceeds
        pos.shares -= qty
        pos.available = max(pos.available - qty, 0)
        pos.realized_pnl += pnl
        pos.total_cost = max(pos.total_cost - cost, 0.0)

        hold_days = 0
        if pos.first_buy_date is not None:
            hold_days = int((pd.Timestamp(date) - pd.Timestamp(pos.first_buy_date)).days)

        trade = Trade(
            code=code,
            name=pos.name,
            direction="sell",
            date=pd.Timestamp(date),
            price=exec_price,
            shares=qty,
            amount=amount,
            commission=commission,
            stamp_tax=tax,
            slippage_cost=slippage_cost,
            reason=reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            hold_days=hold_days,
        )
        self.trades.append(trade)

        if pos.shares <= 0:
            del self.positions[code]
        return trade

    # ------------------------------------------------------------------
    # 估值
    # ------------------------------------------------------------------
    def position_value(self, prices: Dict[str, float]) -> float:
        """按最新价格计算持仓市值（缺价时退化为成本价）。"""
        total = 0.0
        for code, pos in self.positions.items():
            price = prices.get(code)
            if price is None or pd.isna(price) or price <= 0:
                price = pos.avg_cost
            total += pos.shares * float(price)
        return total

    def equity(self, prices: Dict[str, float]) -> float:
        """总权益 = 现金 + 持仓市值。"""
        return self.cash + self.position_value(prices)

    def snapshots(self, date: pd.Timestamp, prices: Dict[str, float]) -> List[Dict[str, Any]]:
        """返回当日持仓快照列表。"""
        rows: List[Dict[str, Any]] = []
        for code, pos in self.positions.items():
            row = pos.to_dict(prices.get(code))
            row["date"] = _fmt(date)
            rows.append(row)
        return sorted(rows, key=lambda r: r["code"])

    # ------------------------------------------------------------------
    def _reject(self, date: pd.Timestamp, code: str, direction: str, reason: str) -> None:
        """记录一笔未成交。"""
        self.rejected.append(
            {
                "date": _fmt(date),
                "code": code,
                "direction": direction,
                "reason": reason,
            }
        )

    # ------------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        """账户层面的汇总信息。"""
        sells = [t for t in self.trades if t.direction == "sell"]
        return {
            "initial_cash": round(self.initial_cash, 2),
            "final_cash": round(self.cash, 2),
            "trade_count": len(self.trades),
            "closed_trade_count": len(sells),
            "rejected_count": len(self.rejected),
            "total_commission": round(sum(t.commission for t in self.trades), 2),
            "total_stamp_tax": round(sum(t.stamp_tax for t in self.trades), 2),
            "total_slippage": round(sum(t.slippage_cost for t in self.trades), 2),
            "realized_pnl": round(sum(t.pnl for t in sells), 2),
        }


def build_broker(cfg: Dict[str, Any]) -> Broker:
    """根据配置字典构建 :class:`Broker`。

    :param cfg: ``config.yaml`` 中 ``backtest`` 段的字典
    """
    return Broker(
        initial_cash=cfg.get("initial_cash", 1_000_000),
        commission=cfg.get("commission", 0.0003),
        stamp_tax=cfg.get("stamp_tax", 0.001),
        slippage=cfg.get("slippage", 0.0002),
        min_commission=cfg.get("min_commission", 5.0),
        limit_pct=cfg.get("limit_pct", 0.10),
        limit_pct_star=cfg.get("limit_pct_star", 0.20),
        t_plus_1=cfg.get("t_plus_1", True),
    )
