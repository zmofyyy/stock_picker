"""全历史缓存的规模 / 内存 / 精度自检。

用法（源码态）：

    python packaging/fullhistory_test.py [--no-rebuild]

做四件事：
1. （可选）重建行情缓存，报告行数、体积、耗时、各列 dtype 与常驻内存；
2. 核对 ``cache_bars <= 0`` 时确实读到了 .day 的全历史（对比源文件根数）；
3. 在指定历史日期跑一次选股，确认能命中（这正是「选不了 22 年」的回归）；
4. 抽一只股票，用 .day 原始字节**独立重算**前 N 日均量，与选股口径逐值比对。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock_picker.config import DATA_DIR, Config  # noqa: E402
from stock_picker.names import NameIndex  # noqa: E402
from stock_picker.service import MarketService  # noqa: E402
from stock_picker.tdx_reader import DAY_DTYPE, TdxReader  # noqa: E402


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    # 注意：Windows 上必须用 DWORD(4B) 起头，且函数在 kernel32 里叫
    # K32GetProcessMemoryInfo（psapi 那个老名字取不到，会静默返回 0）。
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _pmc() -> _PROCESS_MEMORY_COUNTERS:
    c = _PROCESS_MEMORY_COUNTERS()
    c.cb = ctypes.sizeof(c)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.K32GetProcessMemoryInfo(
        ctypes.wintypes.HANDLE(k32.GetCurrentProcess()), ctypes.byref(c), c.cb
    )
    return c


def rss_mb() -> float:
    """当前进程工作集（MB）。"""
    return _pmc().WorkingSetSize / 1e6


def peak_mb() -> float:
    return _pmc().PeakWorkingSetSize / 1e6


def build_service() -> MarketService:
    cfg = Config()
    reader = TdxReader(cfg.get("tdx_dir"), mode=cfg.get("reader_mode", "fast"))
    names = NameIndex(
        cfg.get("tdx_dir"),
        cache_file=DATA_DIR / "stock_names.json",
        extra_csv=cfg.get("names_csv") or None,
    )
    names.load(force=False)
    return MarketService(cfg, reader, names)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-rebuild", action="store_true", help="跳过重建，只用现有缓存")
    ap.add_argument("--date", default="2022-06-01", help="用于回归的历史日期")
    args = ap.parse_args()

    ok = True
    svc = build_service()
    print(f"配置 cache_bars = {svc.config.get('cache_bars')!r}（0=全历史）")

    # ---------------------------------------------------------------- 1
    if not args.no_rebuild:
        t0 = time.perf_counter()
        res = svc.refresh()
        el = time.perf_counter() - t0
        print(f"\n[1] 重建缓存：ok={res.get('ok')}  墙钟 {el:.1f}s")
        for k in ("codes_loaded", "rows", "bars_per_code", "bars_per_code_avg",
                  "bars_per_code_max", "first_date", "last_date", "file_mb", "elapsed"):
            print(f"      {k:20s} = {res.get(k)}")
        if not res.get("ok"):
            print("      !! 重建失败：", res.get("reason"))
            return 1
        ok &= int(res.get("bars_per_code") or 0) == 0

    # ---------------------------------------------------------------- 2
    df = svc.bars()
    mem = df.memory_usage(deep=True)
    print(f"\n[2] 内存长表：{len(df):,} 行 × {len(df.columns)} 列")
    print(f"      memory_usage(deep) = {mem.sum()/1e6:.1f} MB"
          f"   （列明细见下）")
    for col in df.columns:
        print(f"        {col:8s} {str(df[col].dtype):9s} {mem[col]/1e6:8.1f} MB")
    print(f"      进程工作集 {rss_mb():.0f} MB / 峰值 {peak_mb():.0f} MB")

    dts = svc.all_dates()
    print(f"      交易日 {len(dts):,} 个：{dts[0]} ~ {dts[-1]}")

    # 与源文件根数交叉核对（只抽 3 只，避免再扫一遍全市场）
    print("\n      与 .day 源文件根数核对：")
    for code in ("600000.SH", "000001.SZ", "600519.SH"):
        path = svc.reader.daily_file(code)
        if path is None:
            continue
        src_n = path.stat().st_size // 32
        cache_n = int((df["code"] == code).sum())
        flag = "OK" if cache_n == src_n else "!! 不一致"
        print(f"        {code}  源 {src_n:5d} 根 / 缓存 {cache_n:5d} 根  {flag}")
        ok &= cache_n == src_n

    # ---------------------------------------------------------------- 3
    print(f"\n[3] 指定历史日期选股：{args.date}")
    out = svc.screen({"date": args.date, "max_results": 10})
    if not out.get("ok"):
        print("      !! 失败：", out.get("reason"))
        ok = False
    else:
        print(f"      命中 {out['matched']} 只（截取前 10）"
              f"  扫描 {out['total_scanned']} 只 · 耗时 {out.get('screen_elapsed')}s")
        for r in out["rows"][:5]:
            print(f"        {r['code']} {r['name']}  信号日 {r['signal_date']} "
                  f"收 {r['close']}  量比 {r['vol_ratio']}")
        ok &= out["matched"] > 0

    # ---------------------------------------------------------------- 4
    # 独立重算：直接从 .day 字节流取「前 M 日均量」，与选股口径逐值比对
    print(f"\n[4] 均量口径交叉核对（独立从 .day 重算）")
    code = "600000.SH"
    dates = svc.trade_dates(0)
    target = args.date
    if target in dates:
        i = dates.index(target)
        probe = dates[max(0, i - 3): i + 1]          # 目标日往前几天，逐日核对
        path = svc.reader.daily_file(code)
        raw = path.read_bytes()
        arr = np.frombuffer(raw[: (len(raw) // 32) * 32], dtype=DAY_DTYPE)
        src_dates = arr["date"].astype("int64")
        src_vol = arr["volume"].astype("float64")
        for d in probe:
            di = int(d.replace("-", ""))
            pos = int(np.searchsorted(src_dates, di, "left"))
            if pos >= len(src_dates) or int(src_dates[pos]) != di:
                print(f"        {code} {d} 源文件无该日，跳过")
                continue
            # 目标日 == pos，均量 = 它前面 window 根的均值（不含当日）
            window = 20
            exp = float(src_vol[pos - window: pos].mean())
            got_row = df[(df["code"] == code) & (df["date"] == di)]
            if len(got_row) == 0:
                print(f"        {code} {d} 缓存缺该日  !!")
                ok = False
                continue
            got_idx = got_row.index[0]
            idx_all = df.index.get_loc(got_idx)
            sub = svc._attach_volume_stats(np.array([idx_all]), window)
            got = float(sub["ma_vol"].iloc[0])
            same = abs(got - exp) <= max(abs(exp) * 1e-9, 1e-6)
            print(f"        {code} {d}  源 {exp:,.2f} / 缓存 {got:,.2f}  "
                  f"{'OK' if same else '!! 不一致'}")
            ok &= same
    else:
        print(f"        {target} 不在交易日列表里，跳过")

    # ---------------------------------------------------------------- 5
    print("\n[5] 缓存新鲜度与交易日接口")
    fresh = svc.cache_freshness()
    print(f"      stale={fresh['stale']}  reason={fresh['reason']}")
    # 全历史缓存 + cache_bars=0 → 口径一致，不该判为过期
    same_cfg = int(svc.config.get("cache_bars", 0) or 0) == int(
        (svc.meta() or {}).get("bars_per_code", -1) or 0
    )
    if same_cfg:
        ok &= fresh["stale"] is False
        print(f"      口径一致 → stale 应为 False：{'OK' if not fresh['stale'] else '!! 不一致'}")
    else:
        ok &= fresh["stale"] is True
        print(f"      口径不一致 → stale 应为 True：{'OK' if fresh['stale'] else '!! 不一致'}")

    alltd = svc.trade_dates(0)
    tail250 = svc.trade_dates(250)
    print(f"      trade_dates(0)   = {len(alltd):,} 个（{alltd[0]} ~ {alltd[-1]}）")
    print(f"      trade_dates(250) = {len(tail250)} 个（{tail250[0]} ~ {tail250[-1]}）")
    print(f"      日期控件下界因此从 {tail250[0]} 放宽到 {alltd[0]}")
    ok &= len(alltd) == len(dts) and len(tail250) == 250
    # 回归本体：2022 年必须是可选日期
    ok &= any(d.startswith("2022-") for d in alltd)
    print(f"      2022 年在可选范围内：{'OK' if any(d.startswith('2022-') for d in alltd) else '!! 缺失'}")

    # ---------------------------------------------------------------- 6
    # 旧缓存兼容：把 code 打回 object 字符串，_coerce_bars_dtypes 应能救回来
    print("\n[6] 旧缓存兼容（code 为 object 字符串时的 dtype 归一）")
    legacy = df[df["code"].isin(["600000.SH", "000001.SZ"])].copy()
    legacy["code"] = legacy["code"].astype(object)
    legacy["close"] = legacy["close"].astype("float64")
    fixed = MarketService._coerce_bars_dtypes(legacy.copy())
    leg_ok = (
        isinstance(fixed["code"].dtype, pd.CategoricalDtype)
        and str(fixed["close"].dtype) == "float32"
    )
    print(f"      code -> {fixed['code'].dtype} / close -> {fixed['close'].dtype}  "
          f"{'OK' if leg_ok else '!! 未归一'}")
    ok &= leg_ok

    print("\n结论：", "全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
