"""通用技术指标计算。

所有指标均为「因果」实现：第 i 个值只依赖 ``[0, i]`` 区间内的数据，
不会引入未来函数。需要排除当日数据的场景（如「N 日高点突破」）由调用方
显式使用 ``.shift(1)``。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """简单移动平均。

    :param series: 输入序列
    :param window: 窗口长度
    :return: 均线序列，前 ``window-1`` 个值为 NaN
    """
    if window <= 0:
        raise ValueError("window 必须为正整数")
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """指数移动平均。"""
    if span <= 0:
        raise ValueError("span 必须为正整数")
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI 指标。

    使用 ``ewm(alpha=1/period, adjust=False)`` 复现 Wilder 平滑方式，
    与通达信 / TA-Lib 的 RSI 结果基本一致。

    :param close: 收盘价序列
    :param period: 周期，默认 14
    :return: RSI 序列（0~100）
    """
    if period <= 0:
        raise ValueError("period 必须为正整数")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))
    # 连续上涨（avg_loss == 0）时 RSI 定义为 100
    result = result.where(avg_loss != 0, 100.0)
    # 全程无涨跌时定义为 50
    result = result.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    result = result.where(avg_gain.notna() & avg_loss.notna(), np.nan)
    return result


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """真实波幅 TR。"""
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """平均真实波幅 ATR（Wilder 平滑）。"""
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rolling_high(series: pd.Series, window: int, exclude_current: bool = True) -> pd.Series:
    """滚动最高值。

    :param series: 输入序列（通常是 high）
    :param window: 窗口长度
    :param exclude_current: 是否排除当日，即只统计前 ``window`` 根 K 线
    """
    rolled = series.rolling(window=window, min_periods=window).max()
    return rolled.shift(1) if exclude_current else rolled


def rolling_low(series: pd.Series, window: int, exclude_current: bool = True) -> pd.Series:
    """滚动最低值。"""
    rolled = series.rolling(window=window, min_periods=window).min()
    return rolled.shift(1) if exclude_current else rolled


def pct_change(series: pd.Series, periods: int = 1) -> pd.Series:
    """简单涨跌幅（小数形式）。"""
    return series.pct_change(periods=periods)


def cross_over(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """金叉：``fast`` 上穿 ``slow``。

    返回布尔序列，且在任一序列为 NaN 时为 False。
    """
    curr = fast > slow
    prev = fast.shift(1) <= slow.shift(1)
    valid = fast.notna() & slow.notna() & fast.shift(1).notna() & slow.shift(1).notna()
    return (curr & prev & valid).fillna(False)


def cross_under(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """死叉：``fast`` 下穿 ``slow``。"""
    curr = fast < slow
    prev = fast.shift(1) >= slow.shift(1)
    valid = fast.notna() & slow.notna() & fast.shift(1).notna() & slow.shift(1).notna()
    return (curr & prev & valid).fillna(False)
