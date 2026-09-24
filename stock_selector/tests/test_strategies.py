"""策略模块测试。

覆盖：
    * 三个内置策略的信号生成正确性
    * 参数校验与注册表
    * 无未来函数（look-ahead bias）的关键验证
    * ``select_stocks`` / ``evaluate`` 的输出格式
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.app.data.tdx_reader import TdxDataReader
from backend.app.strategies import (
    SIGNAL_BUY,
    SIGNAL_HOLD,
    SIGNAL_SELL,
    BaseStrategy,
    MaCrossStrategy,
    RsiStrategy,
    VolumeBreakoutStrategy,
    get_strategy,
    get_strategy_class,
    list_strategies,
    strategy_names,
)
from backend.app.strategies.indicators import cross_over, cross_under, rsi, sma


@pytest.fixture()
def bars(tdx_root: Path) -> pd.DataFrame:
    """读取一只测试股票的日线。"""
    reader = TdxDataReader(tdx_root)
    return reader.read_daily("600000.SH")


# ----------------------------------------------------------------------
# 指标
# ----------------------------------------------------------------------
def test_sma_basic() -> None:
    """SMA 前 window-1 个值应为 NaN。"""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = sma(s, 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(2.0)
    assert out.iloc[4] == pytest.approx(4.0)


def test_rsi_bounds() -> None:
    """RSI 应位于 0~100 之间，且单调上涨时接近 100。"""
    up = pd.Series(np.linspace(10, 30, 60))
    values = rsi(up, 14)
    assert values.dropna().between(0, 100).all()
    assert values.iloc[-1] > 95

    down = pd.Series(np.linspace(30, 10, 60))
    values_down = rsi(down, 14)
    assert values_down.iloc[-1] < 5


def test_cross_helpers() -> None:
    """金叉 / 死叉判定。"""
    fast = pd.Series([1.0, 2.0, 3.0, 2.0, 1.0])
    slow = pd.Series([2.0, 2.0, 2.0, 2.0, 2.0])
    over = cross_over(fast, slow)
    under = cross_under(fast, slow)
    assert bool(over.iloc[2]) is True   # 1->2->3 上穿 2
    assert bool(under.iloc[4]) is True  # 3->2->1 下穿 2
    assert not bool(over.iloc[0])
    assert not bool(under.iloc[0])


# ----------------------------------------------------------------------
# 注册表
# ----------------------------------------------------------------------
def test_registry() -> None:
    """注册表应包含 3 个内置策略。"""
    names = strategy_names()
    assert {"ma_cross", "rsi", "volume_breakout"} <= set(names)
    assert issubclass(get_strategy_class("ma_cross"), BaseStrategy)

    metas = list_strategies()
    assert len(metas) >= 3
    for meta in metas:
        assert meta["name"] and meta["display_name"]
        assert isinstance(meta["param_schema"], dict)


def test_get_strategy_with_params() -> None:
    """实例化策略并覆盖参数。"""
    strat = get_strategy("ma_cross", {"short_window": 3, "long_window": 8})
    assert isinstance(strat, MaCrossStrategy)
    assert strat.get_params()["short_window"] == 3
    assert strat.required_bars() >= 8


def test_unknown_strategy() -> None:
    """未知策略应报错。"""
    with pytest.raises(KeyError):
        get_strategy_class("not_exist")


# ----------------------------------------------------------------------
# 双均线
# ----------------------------------------------------------------------
def test_ma_cross_signals(bars: pd.DataFrame) -> None:
    """双均线信号正确性。"""
    strat = MaCrossStrategy({"short_window": 5, "long_window": 20})
    out = strat.generate_signals(bars)

    assert len(out) == len(bars)
    assert set(out["signal"].unique()) <= {-1, 0, 1}

    ma_short = sma(bars["close"], 5)
    ma_long = sma(bars["close"], 20)
    expected_buy = cross_over(ma_short, ma_long)
    assert (out["buy_cond"] == expected_buy).all()
    assert (out["signal"] == 1).sum() == int(expected_buy.sum())
    assert (out["signal"] == -1).sum() == int(cross_under(ma_short, ma_long).sum())
    assert (out["ma_short"] - out["ma_long"]).abs().max() > 0

    # 金叉必然发生在 ma_short > ma_long 时
    buy_rows = out[out["signal"] == 1]
    assert (buy_rows["ma_short"] > buy_rows["ma_long"]).all()


def test_ma_cross_param_validation() -> None:
    """短均线周期必须小于长均线周期。"""
    with pytest.raises(ValueError):
        MaCrossStrategy({"short_window": 20, "long_window": 5})


def test_ma_cross_volume_filter(bars: pd.DataFrame) -> None:
    """量能过滤会减少买入信号数量。"""
    base = MaCrossStrategy({"short_window": 5, "long_window": 20})
    filtered = MaCrossStrategy(
        {"short_window": 5, "long_window": 20, "volume_ratio": 3.0}
    )
    n_base = int((base.generate_signals(bars)["signal"] == 1).sum())
    n_filtered = int((filtered.generate_signals(bars)["signal"] == 1).sum())
    assert n_filtered <= n_base


def test_ma_cross_required_bars() -> None:
    """最小 K 线需求。"""
    strat = MaCrossStrategy({"short_window": 5, "long_window": 60})
    assert strat.required_bars() >= 60


# ----------------------------------------------------------------------
# RSI
# ----------------------------------------------------------------------
def test_rsi_strategy_signals(bars: pd.DataFrame) -> None:
    """RSI 策略信号在阈值区间之外才触发。"""
    strat = RsiStrategy({"period": 14, "buy_threshold": 35, "sell_threshold": 65})
    out = strat.generate_signals(bars)
    assert set(out["signal"].unique()) <= {-1, 0, 1}

    buy_rows = out[out["signal"] == 1]
    if len(buy_rows) > 0:
        assert (buy_rows["rsi"] < 35).all()
    sell_rows = out[out["signal"] == -1]
    if len(sell_rows) > 0:
        assert (sell_rows["rsi"] > 65).all()


def test_rsi_param_validation() -> None:
    """买入阈值必须小于卖出阈值。"""
    with pytest.raises(ValueError):
        RsiStrategy({"buy_threshold": 80, "sell_threshold": 20})


def test_rsi_require_turn_reduces_signals(bars: pd.DataFrame) -> None:
    """启用拐头确认后信号数量不应增加。"""
    loose = RsiStrategy({"period": 14, "buy_threshold": 40, "sell_threshold": 60, "require_turn": False})
    strict = RsiStrategy({"period": 14, "buy_threshold": 40, "sell_threshold": 60, "require_turn": True})
    assert int((strict.generate_signals(bars)["signal"] == 1).sum()) <= int(
        (loose.generate_signals(bars)["signal"] == 1).sum()
    )


# ----------------------------------------------------------------------
# 放量突破
# ----------------------------------------------------------------------
def test_volume_breakout_signals(bars: pd.DataFrame) -> None:
    """放量突破：买入需同时满足突破与放量。"""
    strat = VolumeBreakoutStrategy(
        {"high_window": 20, "volume_ratio": 1.5, "volume_ma_window": 5}
    )
    out = strat.generate_signals(bars)
    assert set(out["signal"].unique()) <= {-1, 0, 1}

    buys = out[out["signal"] == 1]
    if len(buys) > 0:
        assert (buys["close"] > buys["high_n"]).all()
        assert (buys["volume_r"] > 1.5).all()

    # 突破窗口使用的是「不含当日」的历史高点
    assert out["high_n"].isna().sum() >= 19


def test_volume_breakout_no_future_data(bars: pd.DataFrame) -> None:
    """``high_n`` 必须只由当日之前的数据决定。"""
    strat = VolumeBreakoutStrategy({"high_window": 10, "volume_ma_window": 5})
    out = strat.generate_signals(bars)

    manual = bars["high"].rolling(10, min_periods=10).max().shift(1)
    pd.testing.assert_series_equal(
        out["high_n"].reset_index(drop=True), manual.reset_index(drop=True),
        check_names=False,
    )


# ----------------------------------------------------------------------
# 无未来函数
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "strategy",
    [
        MaCrossStrategy({"short_window": 5, "long_window": 20}),
        RsiStrategy({"period": 14}),
        VolumeBreakoutStrategy({"high_window": 20, "volume_ma_window": 5}),
    ],
)
def test_no_lookahead_bias(bars: pd.DataFrame, strategy: BaseStrategy) -> None:
    """截断未来数据不应改变历史信号。

    这是检测未来函数最直接的方法：对完整的 K 线计算信号，
    再对前 k 根 K 线单独计算，两者在第 k-1 行必须完全一致。
    """
    full = strategy.generate_signals(bars)
    for k in (40, 60, 90, 120, len(bars) - 1):
        truncated = strategy.generate_signals(bars.iloc[:k].reset_index(drop=True))
        assert int(truncated["signal"].iloc[-1]) == int(full["signal"].iloc[k - 1]), (
            f"{strategy.name} 在第 {k} 行出现未来函数"
        )
        for col in strategy.factor_columns:
            a = truncated[col].iloc[-1]
            b = full[col].iloc[k - 1]
            if pd.isna(a) and pd.isna(b):
                continue
            assert a == pytest.approx(b, rel=1e-9, nan_ok=True), (
                f"{strategy.name} 因子 {col} 在第 {k} 行不一致"
            )


# ----------------------------------------------------------------------
# evaluate / select_stocks
# ----------------------------------------------------------------------
def test_evaluate_output_format(bars: pd.DataFrame) -> None:
    """``evaluate`` 返回结构化 :class:`SignalDetail`。"""
    strat = MaCrossStrategy({"short_window": 5, "long_window": 20})
    detail = strat.evaluate(
        "600000.SH", bars, date=bars["datetime"].iloc[-1], name="浦发银行"
    )
    assert detail is not None
    d = detail.to_dict()
    assert d["code"] == "600000.SH"
    assert d["name"] == "浦发银行"
    assert d["signal"] in (-1, 0, 1)
    assert d["signal_text"] in ("买入", "卖出", "无信号")
    assert isinstance(d["factors"], dict)
    assert d["data_date"] == bars["datetime"].iloc[-1].strftime("%Y-%m-%d")


def test_evaluate_insufficient_data(bars: pd.DataFrame) -> None:
    """数据不足时应返回 None。"""
    strat = MaCrossStrategy({"short_window": 5, "long_window": 120})
    assert strat.evaluate("600000.SH", bars.iloc[:50]) is None


def test_evaluate_date_slicing_equals_full(bars: pd.DataFrame) -> None:
    """按日期切片评估的结果应与直接截断数据一致。"""
    strat = MaCrossStrategy({"short_window": 5, "long_window": 20})
    target = bars["datetime"].iloc[150]
    sliced = strat.evaluate("600000.SH", bars, date=target)
    manual = strat.evaluate("600000.SH", bars.iloc[:151].reset_index(drop=True))
    assert sliced is not None and manual is not None
    assert sliced.signal == manual.signal
    assert sliced.factors["ma_short"] == pytest.approx(manual.factors["ma_short"])


def test_select_stocks(bars: pd.DataFrame) -> None:
    """``select_stocks`` 只返回买入信号，并按新信号优先排序。"""
    strat = MaCrossStrategy({"short_window": 5, "long_window": 20})
    data = {"600000.SH": bars, "000001.SZ": bars.copy()}
    names = {"600000.SH": "浦发银行", "000001.SZ": "平安银行"}

    results = strat.select_stocks(data, date=bars["datetime"].iloc[-1], names=names)
    for item in results:
        assert item.signal == SIGNAL_BUY
        assert item.name

    all_signals = strat.select_stocks(
        data, date=bars["datetime"].iloc[-1], names=names, only_buy=False
    )
    assert len(all_signals) >= len(results)


def test_strategy_describe() -> None:
    """``describe`` 提供 UI 需要的元信息。"""
    meta = MaCrossStrategy.describe()
    assert meta["name"] == "ma_cross"
    assert "short_window" in meta["param_schema"]
    assert meta["param_schema"]["short_window"]["type"] == "int"
    assert MaCrossStrategy.describe_conditions()
