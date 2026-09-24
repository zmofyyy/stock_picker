"""生成一份「模拟通达信」本地数据，便于在没有真实通达信环境时体验本软件。

会在指定目录下生成::

    {out}/vipdoc/sh/lday/sh600000.day
    {out}/vipdoc/sz/lday/sz000001.day
    {out}/vipdoc/sh/lday/sh000300.day      # 沪深300 指数（回测基准）
    {out}/vipdoc/sh/fzline/sh600000.lc5    # 5 分钟线

文件严格按照通达信二进制格式写入，因此既能被 ``pytdx`` 读取，
也能被内置备用解析器读取 —— 与真实通达信目录的行为一致。

用法::

    python scripts/make_demo_data.py --out demo_tdx --days 500
    python cli.py read-data --tdx-dir ./demo_tdx

注意：生成的是**人造行情**，仅用于功能验证，不代表任何真实市场数据。
"""

from __future__ import annotations

import argparse
import csv
import math
import shutil
import struct
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Sequence

import numpy as np

# 允许直接以脚本方式运行（把项目根目录加入 sys.path）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MARKET_DIRS = {"sh": "vipdoc/sh/lday", "sz": "vipdoc/sz/lday", "bj": "vipdoc/bj/lday"}

#: 演示用股票池：代码 -> 参数
DEMO_SYMBOLS: Dict[str, Dict[str, float]] = {
    "600000": {"seed": 1, "base_price": 12.0, "period": 40, "trend": 0.0004},
    "600036": {"seed": 11, "base_price": 35.0, "period": 55, "trend": 0.0005},
    "601318": {"seed": 12, "base_price": 48.0, "period": 33, "trend": -0.0002},
    "000001": {"seed": 2, "base_price": 9.0, "period": 30, "trend": 0.0003},
    "000002": {"seed": 21, "base_price": 15.0, "period": 47, "trend": -0.0001},
    "002415": {"seed": 22, "base_price": 30.0, "period": 61, "trend": 0.0006},
    "300750": {"seed": 3, "base_price": 25.0, "period": 50, "trend": 0.0008},
    "300059": {"seed": 31, "base_price": 18.0, "period": 27, "trend": 0.0002},
    "688981": {"seed": 32, "base_price": 55.0, "period": 44, "trend": 0.0003},
    "830000": {"seed": 4, "base_price": 6.0, "period": 25, "trend": 0.0001},
}

#: 演示用股票名称（写入 names.csv，供界面展示）
DEMO_NAMES = {
    "600000": "浦发银行",
    "600036": "招商银行",
    "601318": "中国平安",
    "000001": "平安银行",
    "000002": "万科A",
    "002415": "海康威视",
    "300750": "宁德时代",
    "300059": "东方财富",
    "688981": "中芯国际",
    "830000": "云星宇",
    "000300": "沪深300",
}

#: 演示用流通股本（单位：股），写入 stock_basic.csv，供「小盘放量」策略判断流通盘。
#: 刻意让流通市值分别落在 150 亿上下两侧，便于观察流通盘过滤是否生效。
DEMO_FLOAT_SHARES = {
    "600000": 5.0e8,    # 约 60 亿  -> 通过
    "600036": 8.0e8,    # 约 280 亿 -> 被过滤
    "601318": 1.5e8,    # 约 72 亿  -> 通过
    "000001": 20.0e8,   # 约 180 亿 -> 被过滤
    "000002": 9.0e8,    # 约 135 亿 -> 通过
    "002415": 3.0e8,    # 约 90 亿  -> 通过
    "300750": 4.0e8,    # 约 100 亿 -> 通过
    "300059": 9.0e8,    # 约 162 亿 -> 被过滤
    "688981": 1.5e8,    # 约 82 亿  -> 通过
    "830000": 1.2e8,    # 约 7 亿   -> 通过
}

#: 演示用所属行业
DEMO_INDUSTRY = {
    "600000": "银行", "600036": "银行", "601318": "保险",
    "000001": "银行", "000002": "房地产", "002415": "电子",
    "300750": "电池", "300059": "证券", "688981": "半导体",
    "830000": "软件服务",
}


def market_of(symbol: str, is_index: bool = False) -> str:
    """由代码推断通达信存放的市场目录。"""
    if is_index:
        if symbol.startswith("399"):
            return "sz"
        if symbol.startswith("899"):
            return "bj"
        return "sh"
    if symbol.startswith(("6", "9", "5")):
        return "sh"
    if symbol.startswith(("4", "8")):
        return "bj"
    return "sz"


def trading_days(start: date, count: int) -> List[date]:
    """生成连续交易日（跳过周末，不处理法定节假日）。"""
    out: List[date] = []
    current = start
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
    noise: float = 0.006,
    seed: int = 7,
    base_volume: float = 1_000_000.0,
) -> List[Dict[str, float]]:
    """生成确定性的 K 线序列（趋势 + 正弦波动 + 噪声 + 周期性放量）。"""
    rng = np.random.default_rng(seed)
    closes: List[float] = []
    for i in range(len(dates)):
        price = base_price * (
            1.0 + trend * i + wave * math.sin(2 * math.pi * i / period) + rng.normal(0, noise)
        )
        closes.append(max(round(price, 2), 0.5))

    bars: List[Dict[str, float]] = []
    prev_close = closes[0]
    for i, d in enumerate(dates):
        close = closes[i]
        open_ = max(round(prev_close * (1 + rng.normal(0, 0.002)), 2), 0.5)
        high = round(max(open_, close) * (1 + abs(rng.normal(0, 0.004))), 2)
        low = max(round(min(open_, close) * (1 - abs(rng.normal(0, 0.004))), 2), 0.5)
        high = max(high, open_, close)
        low = min(low, open_, close)

        spike = 2.5 if i % 20 == 13 else 1.0
        volume = max(round(base_volume * spike * (1 + 0.5 * math.sin(2 * math.pi * i / 17)), 0), 1000.0)
        bars.append(
            {
                "date": d,
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume),
                "amount": float(round(volume * close, 2)),
            }
        )
        prev_close = close
    return bars


def write_day_file(path: Path, bars: Sequence[Dict[str, float]]) -> Path:
    """按通达信 ``.day`` 格式写入（每条记录 32 字节）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fp:
        for bar in bars:
            d = bar["date"]
            fp.write(
                struct.pack(
                    "<IIIIIfII",
                    int(d.strftime("%Y%m%d")),
                    int(round(bar["open"] * 100)),
                    int(round(bar["high"] * 100)),
                    int(round(bar["low"] * 100)),
                    int(round(bar["close"] * 100)),
                    float(bar["amount"]),
                    int(round(bar["volume"])),
                    0,
                )
            )
    return path


def write_lc5_file(path: Path, bars: Sequence[Dict[str, object]]) -> Path:
    """按通达信 ``.lc5`` 分钟线格式写入（每条记录 32 字节）。

    布局与 ``pytdx.reader.TdxMinBarReader`` 一致：
    ``<HHIIIIfII`` = date, time, open, high, low, close（均为「价格 ×100」的 int32）,
    amount(float32), volume(int32, 单位：股), reserved。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fp:
        for bar in bars:
            ts = bar["datetime"]
            packed_date = (ts.year - 2004) * 2048 + ts.month * 100 + ts.day
            packed_time = ts.hour * 60 + ts.minute
            fp.write(
                struct.pack(
                    "<HHIIIIfII",
                    packed_date,
                    packed_time,
                    int(round(float(bar["open"]) * 100)),
                    int(round(float(bar["high"]) * 100)),
                    int(round(float(bar["low"]) * 100)),
                    int(round(float(bar["close"]) * 100)),
                    float(bar["amount"]),
                    int(round(float(bar["volume"]))),
                    0,
                )
            )
    return path


def build_minute(out: Path, symbol: str, days: int, start: date, bars_per_day: int = 48) -> int:
    """生成 5 分钟线文件，返回写入的 K 线根数。"""
    import pandas as pd

    market = market_of(symbol)
    rows: List[Dict[str, object]] = []
    price = 10.0
    for d in trading_days(start, days):
        for i in range(bars_per_day):
            hour = 9 + (i * 5 + 30) // 60
            minute = (i * 5 + 30) % 60
            ts = pd.Timestamp(year=d.year, month=d.month, day=d.day, hour=hour, minute=minute)
            price = round(price * (1 + 0.0008 * math.sin(i)), 3)
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
    path = out / "vipdoc" / market / "fzline" / f"{market}{symbol}.lc5"
    write_lc5_file(path, rows)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成模拟通达信本地数据")
    parser.add_argument("--out", default="demo_tdx", help="输出目录（默认 ./demo_tdx）")
    parser.add_argument("--days", type=int, default=500, help="生成多少个交易日（默认 500）")
    parser.add_argument("--start", default="2023-01-03", help="起始日期（默认 2023-01-03）")
    parser.add_argument("--min-days", type=int, default=5, help="分钟线生成天数（默认 5）")
    parser.add_argument("--cache-dir", default=None, help="把 names.csv 安装到该缓存目录（如 ./cache）")
    args = parser.parse_args()

    cache_dir = args.cache_dir

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    y, m, d = (int(x) for x in args.start.split("-"))
    start = date(y, m, d)
    dates = trading_days(start, args.days)

    total = 0
    for symbol, spec in DEMO_SYMBOLS.items():
        market = market_of(symbol)
        bars = make_bars(dates, **spec)
        write_day_file(out / MARKET_DIRS[market] / f"{market}{symbol}.day", bars)
        total += len(bars)
        print(f"  日线 {symbol}.{market.upper():2s}  {len(bars)} 根  "
              f"价格 {bars[0]['close']:.2f} → {bars[-1]['close']:.2f}")

    # 沪深 300 指数（回测基准）
    index_bars = make_bars(dates, base_price=4000.0, trend=0.0002, wave=0.08, period=60, seed=99)
    write_day_file(out / MARKET_DIRS["sh"] / "sh000300.day", index_bars)
    print(f"  指数 000300.SH  {len(index_bars)} 根（回测基准）")

    # 5 分钟线
    n_min = build_minute(out, "600000", days=args.min_days, start=start)
    print(f"  分钟 600000.SH  {n_min} 根（5 分钟）")

    # 股票名称表（CSV：code,name）
    # 默认放在缓存目录下被自动读取：<cache_dir>/names.csv
    names_file = out / "names.csv"
    with open(names_file, "w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["code", "name"])
        for symbol, name in DEMO_NAMES.items():
            market = market_of(symbol, is_index=symbol == "000300")
            writer.writerow([f"{symbol}.{market.upper()}", name])

    # 股票基础信息表（CSV：code,name,float_shares,total_shares,industry）
    # 供「小盘放量」等需要流通盘数据的策略使用（通达信 .day 不含股本信息）
    basics_file = out / "stock_basic.csv"
    with open(basics_file, "w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["code", "name", "float_shares", "total_shares", "industry"])
        for symbol in DEMO_SYMBOLS:
            market = market_of(symbol)
            shares = DEMO_FLOAT_SHARES.get(symbol)
            writer.writerow(
                [
                    f"{symbol}.{market.upper()}",
                    DEMO_NAMES.get(symbol, symbol),
                    "" if shares is None else int(shares),
                    "" if shares is None else int(shares * 1.2),
                    DEMO_INDUSTRY.get(symbol, ""),
                ]
            )

    print()
    print(f"演示数据已生成：{out}")
    print(f"  交易日区间：{dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日，日线合计 {total} 根）")
    print(f"  名称表：{names_file}")
    print(f"  基础信息表：{basics_file}（流通股本，供「小盘放量」策略使用）")

    # 若指定 --cache-dir，直接把名称表与基础信息表放进缓存目录，界面即可显示股票名称
    if cache_dir:
        cache_path = Path(cache_dir).expanduser()
        cache_path.mkdir(parents=True, exist_ok=True)
        shutil.copy2(names_file, cache_path / "names.csv")
        shutil.copy2(basics_file, cache_path / "stock_basic.csv")
        print(f"  已安装名称表到：{cache_path / 'names.csv'}（界面将显示股票名称）")
        print(f"  已安装基础信息表到：{cache_path / 'stock_basic.csv'}（流通盘策略可正常判断）")
    else:
        print("  提示：加 --cache-dir ./cache 可自动安装名称表与基础信息表，"
              "或手动复制到缓存目录（默认 ./cache/）后再于界面显示股票名称。")

    print()
    print("下一步：")
    print(f"  python cli.py read-data --tdx-dir {out} --persist")
    print(f"  python cli.py screen --strategy ma_cross --date {dates[-1].isoformat()}")
    print(f"  python cli.py screen --strategy volume_surge --date {dates[-1].isoformat()}  # 小盘放量")
    print("  python cli.py backtest --strategy ma_cross --start "
          f"{dates[60].isoformat()} --end {dates[-1].isoformat()}")
    print("  python cli.py serve        # 启动 Web UI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
