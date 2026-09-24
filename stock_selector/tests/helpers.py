"""测试辅助工具：构造可复现的通达信本地数据。

核心函数 :func:`build_tdx_tree` 会在临时目录下生成完整的::

    {root}/vipdoc/sh/lday/sh600000.day
    {root}/vipdoc/sz/lday/sz000001.day
    ...

其中 ``.day`` 文件严格按照通达信二进制格式写入，因此既能被 ``pytdx``
读取，也能被内置备用解析器读取。
"""

from __future__ import annotations

import math
import struct
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np

MARKET_DIRS = {"sh": "vipdoc/sh/lday", "sz": "vipdoc/sz/lday", "bj": "vipdoc/bj/lday"}


def market_of(symbol: str) -> str:
    """由 6 位代码推断市场。"""
    if symbol.startswith(("6", "9", "5")):
        return "sh"
    if symbol.startswith(("4", "8")):
        return "bj"
    return "sz"


def index_market_of(symbol: str) -> str:
    """由指数代码推断其在通达信中的存放市场。

    注意：沪深 300、上证指数等在上交所号段（000xxx）的指数，
    通达信存放在 ``vipdoc/sh/lday`` 下（文件名为 ``sh000300.day``），
    而深证成指（399xxx）存放在 ``vipdoc/sz/lday``。
    """
    if symbol.startswith("399"):
        return "sz"
    if symbol.startswith("899"):
        return "bj"
    return "sh"


def trading_days(start: str = "2022-01-03", count: int = 260) -> List[date]:
    """生成一组连续交易日（跳过周末，不处理法定节假日）。

    :param start: 起始日期
    :param count: 交易日数量
    """
    y, m, d = (int(x) for x in start.split("-"))
    current = date(y, m, d)
    out: List[date] = []
    while len(out) < count:
        if current.weekday() < 5:
            out.append(current)
        current += timedelta(days=1)
    return out


def make_bars(
    dates: Sequence[date],
    base_price: float = 10.0,
    trend: float = 0.0004,
    wave: float = 0.12,
    period: int = 40,
    noise: float = 0.004,
    seed: int = 7,
    base_volume: float = 1_000_000.0,
    volume_wave: float = 0.5,
) -> List[Dict[str, float]]:
    """生成一段确定性的 K 线序列。

    使用「趋势 + 正弦波动 + 小幅噪声」构造价格，保证存在金叉/死叉、
    超买超卖与放量突破等形态，便于测试策略。

    :return: ``[{"date","open","high","low","close","volume","amount"}, ...]``
    """
    rng = np.random.default_rng(seed)
    closes: List[float] = []
    price = base_price
    for i in range(len(dates)):
        drift = trend
        cyc = wave * math.sin(2 * math.pi * i / period)
        price = base_price * (1.0 + drift * i + cyc + rng.normal(0, noise))
        closes.append(max(round(price, 2), 0.5))

    bars: List[Dict[str, float]] = []
    prev_close = closes[0]
    for i, d in enumerate(dates):
        close = closes[i]
        open_ = round(prev_close * (1 + rng.normal(0, 0.002)), 2)
        open_ = max(open_, 0.5)
        high = round(max(open_, close) * (1 + abs(rng.normal(0, 0.004))), 2)
        low = round(min(open_, close) * (1 - abs(rng.normal(0, 0.004))), 2)
        low = max(low, 0.5)
        high = max(high, open_, close)
        low = min(low, open_, close)

        # 放量脉冲：每 20 个交易日出现一次显著放量，用于测试「放量突破」
        spike = 2.5 if i % 20 == 13 else 1.0
        volume = round(base_volume * spike * (1 + volume_wave * math.sin(2 * math.pi * i / 17)), 0)
        volume = max(volume, 1000.0)
        amount = round(volume * close, 2)

        bars.append(
            {
                "date": d,
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume),
                "amount": float(amount),
            }
        )
        prev_close = close
    return bars


def write_day_file(path: Path, bars: Iterable[Dict[str, float]]) -> Path:
    """按通达信 ``.day`` 格式写入文件。

    每条记录 32 字节：
    ``<IIIIIfII`` = date, open, high, low, close, amount(float), volume, reserved

    :param path: 目标文件路径
    :param bars: :func:`make_bars` 的输出
    :return: 写入的文件路径
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    packed = []
    for bar in bars:
        d = bar["date"]
        date_int = int(d.strftime("%Y%m%d")) if isinstance(d, date) else int(d)
        packed.append(
            struct.pack(
                "<IIIIIfII",
                date_int,
                int(round(float(bar["open"]) * 100)),
                int(round(float(bar["high"]) * 100)),
                int(round(float(bar["low"]) * 100)),
                int(round(float(bar["close"]) * 100)),
                float(bar.get("amount", 0.0)),
                int(round(float(bar.get("volume", 0.0)))),
                0,
            )
        )
    with open(path, "wb") as fp:
        fp.write(b"".join(packed))
    return path


def write_lc5_file(path: Path, bars: Sequence[Dict[str, object]]) -> Path:
    """按通达信 ``.lc5`` 分钟线格式写入文件。

    每条记录 32 字节（与 ``pytdx.reader.TdxMinBarReader`` 一致）：
    ``<HHIIIIfII`` = date(uint16), time(uint16),
    open/high/low/close（均为「价格 ×100」的 int32）,
    amount(float32), volume(int32, 单位：股), reserved(uint32)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    packed = []
    for bar in bars:
        ts = bar["datetime"]
        packed_date = (ts.year - 2004) * 2048 + ts.month * 100 + ts.day
        packed_time = ts.hour * 60 + ts.minute
        packed.append(
            struct.pack(
                "<HHIIIIfII",
                packed_date,
                packed_time,
                int(round(float(bar["open"]) * 100)),
                int(round(float(bar["high"]) * 100)),
                int(round(float(bar["low"]) * 100)),
                int(round(float(bar["close"]) * 100)),
                float(bar.get("amount", 0.0)),
                int(bar.get("volume", 0)),
                0,
            )
        )
    with open(path, "wb") as fp:
        fp.write(b"".join(packed))
    return path


def build_tdx_tree(
    root: Path,
    symbols: Optional[Dict[str, Dict[str, object]]] = None,
    days: int = 260,
    start: str = "2022-01-03",
) -> Dict[str, List[Dict[str, float]]]:
    """构造完整的通达信目录树。

    :param root: 通达信安装目录（会创建 ``vipdoc`` 子目录）
    :param symbols: ``{"600000": {"seed": 1, "base_price": 12.0}, ...}``；
        为空时使用默认的三只股票
    :param days: 每只股票生成的交易日数量
    :param start: 起始日期
    :return: ``{标准代码: bars}``
    """
    if symbols is None:
        symbols = {
            "600000": {"seed": 1, "base_price": 12.0, "period": 40},
            "000001": {"seed": 2, "base_price": 9.0, "period": 30},
            "300750": {"seed": 3, "base_price": 25.0, "period": 50},
            "830000": {"seed": 4, "base_price": 6.0, "period": 25},
        }

    dates = trading_days(start, days)
    out: Dict[str, List[Dict[str, float]]] = {}
    for symbol, spec in symbols.items():
        market = market_of(symbol)
        bars = make_bars(dates, **{k: v for k, v in spec.items() if k != "symbol"})
        path = root / MARKET_DIRS[market] / f"{market}{symbol}.day"
        write_day_file(path, bars)
        out[f"{symbol}.{market.upper()}"] = bars
    return out


def build_minute_tree(
    root: Path,
    symbol: str = "600000",
    days: int = 5,
    bars_per_day: int = 48,
    start: str = "2022-01-03",
) -> List[Dict[str, object]]:
    """构造 ``.lc5`` 分钟线文件，返回写入的分钟 K 线。

    :param bars_per_day: 每天 5 分钟 K 线数量，48 根 = 4 小时交易时间
    """
    import pandas as pd

    market = market_of(symbol)
    dates = trading_days(start, days)
    rows: List[Dict[str, object]] = []
    price = 10.0
    for d in dates:
        for i in range(bars_per_day):
            hour = 9 + (i * 5 + 30) // 60
            minute = (i * 5 + 30) % 60
            ts = pd.Timestamp(year=d.year, month=d.month, day=d.day, hour=hour, minute=minute)
            price = round(price * (1 + 0.0005 * math.sin(i)), 3)
            rows.append(
                {
                    "datetime": ts,
                    "open": price,
                    "high": round(price * 1.001, 3),
                    "low": round(price * 0.999, 3),
                    "close": round(price, 3),
                    "volume": 10000 + i * 10,
                    "amount": round(price * (10000 + i * 10), 2),
                }
            )
    path = root / "vipdoc" / market / "fzline" / f"{market}{symbol}.lc5"
    write_lc5_file(path, rows)
    return rows


def build_index_file(
    root: Path,
    symbol: str = "000300",
    days: int = 260,
    start: str = "2022-01-03",
) -> Path:
    """构造指数 ``.day`` 文件（用于回测基准）。"""
    market = index_market_of(symbol)
    dates = trading_days(start, days)
    bars = make_bars(dates, base_price=4000.0, trend=0.0002, wave=0.08, period=60, seed=99)
    path = root / MARKET_DIRS[market] / f"{market}{symbol}.day"
    return write_day_file(path, bars)
