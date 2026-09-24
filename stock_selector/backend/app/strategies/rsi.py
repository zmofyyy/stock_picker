"""示例策略二：RSI 超买超卖。

交易逻辑：
    * 买入：RSI 低于 ``buy_threshold``（超卖）；
    * 卖出：RSI 高于 ``sell_threshold``（超买）；
    * 可选 ``require_turn``：仅在 RSI 由下向上拐头时才发出买入信号，
      避免在持续下跌过程中连续触发买入。
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import pandas as pd

from .base import BaseStrategy
from .indicators import rsi


class RsiStrategy(BaseStrategy):
    """RSI 超买超卖策略。"""

    name = "rsi"
    display_name = "RSI 超买超卖"
    description = "RSI 低于买入阈值时买入，高于卖出阈值时卖出，支持拐头确认。"
    min_bars = 30

    default_params: Dict[str, Any] = {
        "period": 14,
        "buy_threshold": 30.0,
        "sell_threshold": 70.0,
        "require_turn": True,
    }

    param_schema: Dict[str, Dict[str, Any]] = {
        "period": {
            "type": "int", "label": "RSI 周期", "min": 2, "max": 120, "step": 1,
            "help": "Wilder 平滑周期，常用 6 / 12 / 14 / 24",
        },
        "buy_threshold": {
            "type": "float", "label": "买入阈值", "min": 1.0, "max": 50.0, "step": 1.0,
            "help": "RSI 低于该值时视为超卖",
        },
        "sell_threshold": {
            "type": "float", "label": "卖出阈值", "min": 50.0, "max": 99.0, "step": 1.0,
            "help": "RSI 高于该值时视为超买",
        },
        "require_turn": {
            "type": "bool", "label": "要求拐头确认",
            "help": "开启后仅在 RSI 由下向上拐头时才产生买入信号",
        },
    }

    factor_columns = ("rsi", "close", "volume")

    # ------------------------------------------------------------------
    def _post_validate(self, params: Dict[str, Any]) -> None:
        """校验买入阈值必须低于卖出阈值。"""
        if float(params.get("buy_threshold", 30)) >= float(params.get("sell_threshold", 70)):
            raise ValueError("买入阈值必须小于卖出阈值")

    def _compute_required_bars(self) -> int:
        """RSI 需要 ``period`` 根 K 线完成首次平滑。"""
        return int(self.params.get("period", 14)) + 2

    # ------------------------------------------------------------------
    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """计算 RSI 信号。

        :param data: 标准 K 线 DataFrame
        :return: 含 ``signal`` / ``reason`` 及因子列的 DataFrame
        """
        self._ensure_columns(data)

        close = data["close"].astype("float64")
        period = int(self.params["period"])
        buy_th = float(self.params["buy_threshold"])
        sell_th = float(self.params["sell_threshold"])
        require_turn = bool(self.params.get("require_turn", True))

        rsi_values = rsi(close, period)

        oversold = (rsi_values < buy_th).fillna(False)
        overbought = (rsi_values > sell_th).fillna(False)

        # 拐头：相对上一日 RSI 走高
        turn_up = (rsi_values > rsi_values.shift(1)).fillna(False)
        turn_down = (rsi_values < rsi_values.shift(1)).fillna(False)

        if require_turn:
            buy_cond = oversold & turn_up
            sell_cond = overbought & turn_down
            buy_reason = f"RSI 处于超卖区（<{buy_th:g}）且由下向上拐头"
            sell_reason = f"RSI 处于超买区（>{sell_th:g}）且由上向下拐头"
        else:
            buy_cond = oversold
            sell_cond = overbought
            buy_reason = f"RSI 低于买入阈值 {buy_th:g}"
            sell_reason = f"RSI 高于卖出阈值 {sell_th:g}"

        signal = np.where(
            buy_cond.to_numpy(), 1, np.where(sell_cond.to_numpy(), -1, 0)
        )
        reason = np.where(
            buy_cond.to_numpy(), buy_reason,
            np.where(sell_cond.to_numpy(), sell_reason, ""),
        )

        out = pd.DataFrame(index=data.index)
        out["signal"] = signal.astype("int64")
        out["buy_cond"] = buy_cond.to_numpy()
        out["sell_cond"] = sell_cond.to_numpy()
        out["reason"] = reason
        out["rsi"] = rsi_values
        out["close"] = close
        out["volume"] = data["volume"].astype("float64")
        return out

    @classmethod
    def describe_conditions(cls) -> List[str]:
        """人类可读的选股条件。"""
        return [
            "RSI 指标低于买入阈值（超卖）",
            "可选：RSI 由下向上拐头时才算有效买入信号",
            "RSI 高于卖出阈值（超买）时产生卖出信号",
        ]
