"""示例策略一：双均线交叉。

交易逻辑：
    * 买入：短均线 **上穿** 长均线（金叉）；
    * 卖出：短均线 **下穿** 长均线（死叉）。

可选过滤：
    * ``volume_ratio``：金叉当日成交量需大于 ``volume_ratio`` 倍的 N 日均量；
    * ``confirm_days``：金叉后需连续 ``confirm_days`` 日收盘价站在长均线上方才确认。
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import pandas as pd

from .base import BaseStrategy
from .indicators import cross_over, cross_under, sma


class MaCrossStrategy(BaseStrategy):
    """双均线交叉策略。"""

    name = "ma_cross"
    display_name = "双均线交叉"
    description = "短均线上穿长均线买入，下穿卖出，可选成交量与持续性确认。"
    min_bars = 30

    default_params: Dict[str, Any] = {
        "short_window": 5,
        "long_window": 20,
        "volume_ma_window": 5,
        "volume_ratio": 0.0,   # 0 表示不做量能过滤
        "confirm_days": 0,     # 0 表示不做持续性确认
    }

    param_schema: Dict[str, Dict[str, Any]] = {
        "short_window": {
            "type": "int", "label": "短均线周期", "min": 2, "max": 120, "step": 1,
            "help": "快速均线窗口长度",
        },
        "long_window": {
            "type": "int", "label": "长均线周期", "min": 3, "max": 250, "step": 1,
            "help": "慢速均线窗口长度，必须大于短均线",
        },
        "volume_ma_window": {
            "type": "int", "label": "均量周期", "min": 2, "max": 60, "step": 1,
            "help": "计算平均成交量的窗口",
        },
        "volume_ratio": {
            "type": "float", "label": "量能倍数", "min": 0.0, "max": 10.0, "step": 0.1,
            "help": "金叉当日成交量需大于均量的该倍数，0 表示不启用",
        },
        "confirm_days": {
            "type": "int", "label": "确认天数", "min": 0, "max": 10, "step": 1,
            "help": "金叉后需连续站上长均线的天数，0 表示不启用",
        },
    }

    factor_columns = ("ma_short", "ma_long", "close", "ma_gap", "volume", "vol_ma")

    # ------------------------------------------------------------------
    def _post_validate(self, params: Dict[str, Any]) -> None:
        """校验短均线周期必须小于长均线周期。"""
        if params.get("short_window", 0) >= params.get("long_window", 0):
            raise ValueError("短均线周期必须小于长均线周期")

    def _compute_required_bars(self) -> int:
        """所需最少 K 线根数由长均线窗口与确认天数决定。"""
        return (
            int(self.params.get("long_window", 20))
            + int(self.params.get("confirm_days", 0))
            + 2
        )

    # ------------------------------------------------------------------
    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """计算双均线信号。

        :param data: 标准 K 线 DataFrame
        :return: 含 ``signal`` / ``reason`` 及因子列的 DataFrame
        """
        self._ensure_columns(data)

        close = data["close"].astype("float64")
        volume = data["volume"].astype("float64")
        sw = int(self.params["short_window"])
        lw = int(self.params["long_window"])
        vw = int(self.params.get("volume_ma_window", 5))
        vr = float(self.params.get("volume_ratio", 0.0))
        confirm = int(self.params.get("confirm_days", 0))

        ma_short = sma(close, sw)
        ma_long = sma(close, lw)

        golden = cross_over(ma_short, ma_long)
        dead = cross_under(ma_short, ma_long)

        vol_ma = sma(volume, vw)
        if vr > 0:
            vol_ok = (volume > vol_ma * vr).fillna(False)
            golden = golden & vol_ok

        if confirm > 0:
            # 连续 confirm 日收盘价位于长均线上方
            above = (close > ma_long).fillna(False).astype(int)
            stable = above.rolling(confirm, min_periods=confirm).sum() >= confirm
            golden = golden & stable.fillna(False)

        signal = np.where(golden.to_numpy(), 1, np.where(dead.to_numpy(), -1, 0))

        reason = np.where(
            golden.to_numpy(), "短均线上穿长均线（金叉）",
            np.where(dead.to_numpy(), "短均线下穿长均线（死叉）", ""),
        )

        out = pd.DataFrame(index=data.index)
        out["signal"] = signal.astype("int64")
        out["buy_cond"] = golden.to_numpy()
        out["sell_cond"] = dead.to_numpy()
        out["reason"] = reason
        out["ma_short"] = ma_short
        out["ma_long"] = ma_long
        out["ma_gap"] = ma_short - ma_long
        out["close"] = close
        out["volume"] = volume
        out["vol_ma"] = vol_ma
        return out

    @classmethod
    def describe_conditions(cls) -> List[str]:
        """人类可读的选股条件。"""
        return [
            "短周期均线由下向上穿越长周期均线（金叉）",
            "可选：突破当日成交量大于均量的指定倍数",
            "可选：金叉后连续 N 日收盘价站上长周期均线",
        ]
