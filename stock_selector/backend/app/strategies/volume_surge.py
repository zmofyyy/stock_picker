"""策略四：小盘放量（20 日均量两倍）。

交易逻辑（正是本条需求描述的口径）：

    * **买入**：同时满足
        1. 流通盘足够小 —— 流通市值 < ``max_float_market_cap``（默认 150 亿元），
           或（把 ``cap_metric`` 切到 ``float_shares`` 时）流通股本
           < ``max_float_shares``（默认 150 亿股）；
        2. 当日成交量 ≥ ``volume_ma_window``（默认 20）日均量 × ``volume_ratio``（默认 2.0）。
    * **卖出**：收盘价跌破 ``exit_ma_window`` 日均线（可用 ``use_ma_exit`` 关闭）。

关于口径的三点说明（重要）：

1. **流通盘数据来源**：通达信 ``.day`` 文件不含股本信息，流通股本必须来自
   ``<cache_dir>/stock_basic.csv``（见 :mod:`app.data.basics`）。数据缺失的股票
   默认**不通过**流通盘条件（``allow_missing_basics=True`` 可放行），
   避免把「未知」当成「符合」而产生错误结论。
2. **均量是否含当日**：``exclude_current_volume=True``（默认）用「前 N 日均量」，
   与通达信「均量线」在盘中未收盘时的显示更接近，也更保守；设为 False 则
   使用含当日的 N 日均量。两种口径均只使用当日及之前的数据，**不存在未来函数**。
3. **流通市值实时计算**：市值 = 流通股本 × 当日收盘价，不使用可能过期的静态市值字段。
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import pandas as pd

from .base import BaseStrategy
from .indicators import sma

#: 流通盘口径
CAP_METRIC_MARKET_CAP = "float_market_cap"
CAP_METRIC_SHARES = "float_shares"


class VolumeSurgeStrategy(BaseStrategy):
    """小盘放量策略：小流通盘 + 成交量达到均量两倍。"""

    name = "volume_surge"
    display_name = "小盘放量（20日均量2倍）"
    description = (
        "流通盘小于阈值（默认流通市值 150 亿）的股票，当日成交量达到 20 日均量的 "
        "2 倍时买入；收盘价跌破离场均线时卖出。"
    )
    min_bars = 30
    #: 需要流通股本数据（由选股 / 回测 / 追踪注入 ``float_shares`` 列）
    requires_basics = True

    default_params: Dict[str, Any] = {
        "volume_ma_window": 20,
        "volume_ratio": 2.0,
        "cap_metric": CAP_METRIC_MARKET_CAP,
        "max_float_market_cap": 150.0,
        "max_float_shares": 150.0,
        "exclude_current_volume": True,
        "exit_ma_window": 20,
        "use_ma_exit": True,
        "allow_missing_basics": False,
    }

    param_schema: Dict[str, Dict[str, Any]] = {
        "volume_ma_window": {
            "type": "int", "label": "均量窗口（日）", "min": 2, "max": 250, "step": 1,
            "help": "计算基准平均成交量的窗口，需求口径为 20 日",
        },
        "volume_ratio": {
            "type": "float", "label": "放量倍数", "min": 1.0, "max": 20.0, "step": 0.1,
            "help": "当日成交量需达到均量的该倍数，需求口径为 2 倍",
        },
        "cap_metric": {
            "type": "str", "label": "流通盘口径",
            "options": [
                {"value": CAP_METRIC_MARKET_CAP, "label": "流通市值（亿元）"},
                {"value": CAP_METRIC_SHARES, "label": "流通股本（亿股）"},
            ],
            "help": "「小于 150 亿流动股本」两种常见理解；市值口径 = 流通股本 × 当日收盘价",
        },
        "max_float_market_cap": {
            "type": "float", "label": "流通市值上限（亿元）", "min": 0.01, "max": 100000.0,
            "step": 10.0,
            "help": "流通盘口径选择「流通市值」时生效，默认 150 亿元",
        },
        "max_float_shares": {
            "type": "float", "label": "流通股本上限（亿股）", "min": 0.01, "max": 10000.0,
            "step": 10.0,
            "help": "流通盘口径选择「流通股本」时生效，默认 150 亿股",
        },
        "exclude_current_volume": {
            "type": "bool", "label": "均量不含当日",
            "help": "开启（默认）用前 N 日均量作基准；关闭则使用含当日的 N 日均量",
        },
        "exit_ma_window": {
            "type": "int", "label": "离场均线周期", "min": 2, "max": 250, "step": 1,
            "help": "收盘价跌破该均线时发出卖出信号",
        },
        "use_ma_exit": {
            "type": "bool", "label": "启用均线离场",
            "help": "关闭后本策略不产生卖出信号，仅用于选股",
        },
        "allow_missing_basics": {
            "type": "bool", "label": "缺少流通股本时放行",
            "help": "默认关闭：无流通股本数据的股票直接剔除；开启后跳过流通盘校验（结果仅供参考）",
        },
    }

    factor_columns = (
        "close", "volume", "vol_ma", "volume_r",
        "float_cap", "float_shares_yi", "exit_ma",
    )

    # ------------------------------------------------------------------
    def _compute_required_bars(self) -> int:
        """所需最少 K 线根数。"""
        return max(
            int(self.params.get("volume_ma_window", 20)),
            int(self.params.get("exit_ma_window", 20)),
        ) + 3

    # ------------------------------------------------------------------
    def _post_validate(self, params: Dict[str, Any]) -> None:
        """跨参数校验。"""
        metric = str(params.get("cap_metric", CAP_METRIC_MARKET_CAP))
        if metric not in (CAP_METRIC_MARKET_CAP, CAP_METRIC_SHARES):
            raise ValueError(
                f"cap_metric 只能是 {CAP_METRIC_MARKET_CAP} 或 {CAP_METRIC_SHARES}，"
                f"当前为 {metric}"
            )
        if float(params.get("volume_ratio", 2.0)) <= 1.0:
            raise ValueError("volume_ratio 必须大于 1，否则不构成放量")

    # ------------------------------------------------------------------
    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """计算小盘放量信号。

        :param data: 标准 K 线 DataFrame；若含 ``float_shares`` 列则参与流通盘判断
        :return: 含 ``signal`` / ``reason`` 及因子列的 DataFrame
        """
        self._ensure_columns(data)

        close = data["close"].astype("float64")
        volume = data["volume"].astype("float64")

        window = int(self.params["volume_ma_window"])
        ratio = float(self.params["volume_ratio"])
        exclude_current = bool(self.params.get("exclude_current_volume", True))
        exit_ma_window = int(self.params.get("exit_ma_window", 20))
        use_ma_exit = bool(self.params.get("use_ma_exit", True))
        allow_missing = bool(self.params.get("allow_missing_basics", False))
        cap_metric = str(self.params.get("cap_metric", CAP_METRIC_MARKET_CAP))

        # ---- 放量条件：当日成交量 vs N 日均量 ----
        vol_ma = sma(volume, window)
        if exclude_current:
            # 前 N 日均量（不含当日），完全由当日及之前的历史数据决定
            vol_ma = vol_ma.shift(1)
        volume_r = volume / vol_ma.replace(0.0, np.nan)
        vol_ok = (volume_r >= ratio).fillna(False)

        # ---- 流通盘条件 ----
        if "float_shares" in data.columns:
            float_shares = pd.to_numeric(data["float_shares"], errors="coerce").astype("float64")
        else:
            float_shares = pd.Series(np.nan, index=data.index, dtype="float64")
        has_shares = float_shares.notna() & (float_shares > 0)

        float_market_cap = float_shares * close            # 元
        float_cap_yi = float_market_cap / 1e8              # 亿元
        float_shares_yi = float_shares / 1e8               # 亿股

        if cap_metric == CAP_METRIC_SHARES:
            metric_value = float_shares_yi
            limit = float(self.params.get("max_float_shares", 150.0))
            metric_label, metric_unit = "流通股本", "亿股"
        else:
            metric_value = float_cap_yi
            limit = float(self.params.get("max_float_market_cap", 150.0))
            metric_label, metric_unit = "流通市值", "亿元"

        cap_ok = (metric_value <= limit).fillna(False)
        if allow_missing:
            # 明确选择「数据缺失也放行」时才跳过流通盘校验
            cap_ok = cap_ok | (~has_shares)

        buy_cond = vol_ok & cap_ok

        # ---- 离场条件 ----
        exit_ma = sma(close, exit_ma_window)
        if use_ma_exit:
            sell_cond = (close < exit_ma).fillna(False)
        else:
            sell_cond = pd.Series(False, index=data.index)

        signal = np.where(
            buy_cond.to_numpy(), 1, np.where(sell_cond.to_numpy(), -1, 0)
        )

        # ---- 原因文本 ----
        r_str = volume_r.round(2).astype(str)
        cap_str = metric_value.round(2).astype(str)
        cap_desc = cap_str.where(has_shares, "未知")
        buy_reason = (
            f"成交量达 {window} 日均量的 " + r_str + f" 倍（阈值 {ratio:g} 倍）且 "
            + metric_label + " " + cap_desc + f" {metric_unit}（上限 {limit:g} {metric_unit}）"
        )
        exit_reason = f"收盘价跌破 {exit_ma_window} 日均线"
        reason = pd.Series("", index=data.index, dtype="object")
        reason = reason.mask(sell_cond, exit_reason)
        reason = reason.mask(buy_cond, buy_reason)

        out = pd.DataFrame(index=data.index)
        out["signal"] = signal.astype("int64")
        out["buy_cond"] = buy_cond.to_numpy()
        out["sell_cond"] = sell_cond.to_numpy()
        out["volume_cond"] = vol_ok.to_numpy()
        out["cap_cond"] = cap_ok.to_numpy()
        out["close"] = close
        out["volume"] = volume
        out["vol_ma"] = vol_ma
        out["volume_r"] = volume_r
        out["float_shares"] = float_shares
        out["float_cap"] = float_cap_yi
        out["float_shares_yi"] = float_shares_yi
        out["float_metric"] = metric_value
        out["exit_ma"] = exit_ma
        out["reason"] = reason
        return out

    # ------------------------------------------------------------------
    @classmethod
    def describe_conditions(cls) -> List[str]:
        """人类可读的选股条件。"""
        return [
            "流通市值小于 150 亿元（或切换口径后：流通股本小于 150 亿股）",
            "当日成交量达到 20 日均量的 2 倍及以上",
            "卖出条件：收盘价跌破 20 日均线",
            "说明：流通股本来自缓存目录下的 stock_basic.csv，缺失时默认剔除该股",
        ]
