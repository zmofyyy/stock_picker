"""示例策略三：放量突破。

交易逻辑：
    * 买入：收盘价突破 **前 N 日最高价**，且成交量大于 **前 M 日均量的 R 倍**；
    * 卖出：收盘价跌破 **前 N 日最低价**，或跌破 ``exit_ma_window`` 日均线。

注意：
    「前 N 日最高价」使用的是 **不含当日** 的历史区间（``shift(1)``），
    因此不构成未来函数；当日收盘价与当日成交量在收盘时点均已知。
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import pandas as pd

from .base import BaseStrategy
from .indicators import rolling_high, rolling_low, sma


class VolumeBreakoutStrategy(BaseStrategy):
    """放量突破策略。"""

    name = "volume_breakout"
    display_name = "放量突破"
    description = "收盘价突破前 N 日高点且成交量显著放大时买入，跌破区间低点或均线时卖出。"
    min_bars = 30

    default_params: Dict[str, Any] = {
        "high_window": 20,
        "volume_ratio": 2.0,
        "volume_ma_window": 5,
        "exit_ma_window": 10,
        "use_ma_exit": True,
    }

    param_schema: Dict[str, Dict[str, Any]] = {
        "high_window": {
            "type": "int", "label": "突破窗口（N 日）", "min": 3, "max": 250, "step": 1,
            "help": "收盘价需突破前 N 日的最高价",
        },
        "volume_ratio": {
            "type": "float", "label": "放量倍数", "min": 1.0, "max": 20.0, "step": 0.1,
            "help": "当日成交量需大于前 M 日均量的该倍数",
        },
        "volume_ma_window": {
            "type": "int", "label": "均量窗口（M 日）", "min": 2, "max": 120, "step": 1,
            "help": "计算基准平均成交量的窗口",
        },
        "exit_ma_window": {
            "type": "int", "label": "离场均线周期", "min": 2, "max": 250, "step": 1,
            "help": "收盘价跌破该均线时发出卖出信号",
        },
        "use_ma_exit": {
            "type": "bool", "label": "启用均线离场",
            "help": "关闭后仅在被跌破区间低点时才卖出",
        },
    }

    factor_columns = (
        "close", "high_n", "low_n", "volume", "vol_ma", "volume_r",
        "break_pct", "exit_ma",
    )

    # ------------------------------------------------------------------
    def _compute_required_bars(self) -> int:
        """所需最少 K 线根数。"""
        return max(
            int(self.params.get("high_window", 20)),
            int(self.params.get("volume_ma_window", 5)),
            int(self.params.get("exit_ma_window", 10)),
        ) + 3

    # ------------------------------------------------------------------
    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """计算放量突破信号。

        :param data: 标准 K 线 DataFrame
        :return: 含 ``signal`` / ``reason`` 及因子列的 DataFrame
        """
        self._ensure_columns(data)

        close = data["close"].astype("float64")
        high = data["high"].astype("float64")
        low = data["low"].astype("float64")
        volume = data["volume"].astype("float64")

        n = int(self.params["high_window"])
        m = int(self.params["volume_ma_window"])
        ratio = float(self.params["volume_ratio"])
        exit_ma_window = int(self.params.get("exit_ma_window", 10))
        use_ma_exit = bool(self.params.get("use_ma_exit", True))

        # 前 N 日高点 / 低点（不含当日）
        high_n = rolling_high(high, n, exclude_current=True)
        low_n = rolling_low(low, n, exclude_current=True)

        vol_ma = sma(volume, m).shift(1)
        volume_r = volume / vol_ma.replace(0.0, np.nan)

        breakout = (close > high_n).fillna(False)
        vol_ok = (volume_r > ratio).fillna(False)
        buy_cond = breakout & vol_ok

        exit_ma = sma(close, exit_ma_window)
        break_down = (close < low_n).fillna(False)
        if use_ma_exit:
            ma_exit = (close < exit_ma).fillna(False)
            sell_cond = break_down | ma_exit
        else:
            ma_exit = pd.Series(False, index=data.index)
            sell_cond = break_down

        signal = np.where(
            buy_cond.to_numpy(), 1, np.where(sell_cond.to_numpy(), -1, 0)
        )
        reason = np.where(
            buy_cond.to_numpy(),
            f"收盘价突破前 {n} 日高点，且成交量大于前 {m} 日均量的 {ratio:g} 倍",
            np.where(
                break_down.to_numpy(),
                f"收盘价跌破前 {n} 日低点",
                np.where(
                    ma_exit.to_numpy(),
                    f"收盘价跌破 {exit_ma_window} 日均线",
                    "",
                ),
            ),
        )

        out = pd.DataFrame(index=data.index)
        out["signal"] = signal.astype("int64")
        out["buy_cond"] = buy_cond.to_numpy()
        out["sell_cond"] = sell_cond.to_numpy()
        out["break_cond"] = breakout.to_numpy()
        out["volume_cond"] = vol_ok.to_numpy()
        out["reason"] = reason
        out["close"] = close
        out["high_n"] = high_n
        out["low_n"] = low_n
        out["volume"] = volume
        out["vol_ma"] = vol_ma
        out["volume_r"] = volume_r
        # 突破幅度：收盘价相对前 N 日高点的百分比
        out["break_pct"] = (close / high_n - 1.0) * 100.0
        out["exit_ma"] = exit_ma
        return out

    @classmethod
    def describe_conditions(cls) -> List[str]:
        """人类可读的选股条件。"""
        return [
            "收盘价突破前 N 个交易日的最高价",
            "当日成交量大于前 M 个交易日平均成交量的 R 倍",
            "卖出条件：跌破前 N 日最低价或跌破离场均线",
        ]
