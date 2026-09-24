# -*- coding: utf-8 -*-
"""连板梯队专项回归测试。

跑法（源码态，只读，不碰用户数据）：
    python packaging/streak_test.py

验证策略
--------
1. **口径基准交叉核对**：``F:\\source\\stock_blocks\\stock_watch`` 这个兄弟应用有一份
   现成的 ``universe.parquet``（含 ``limit_up`` 列）。本项目的涨停判定与它**逐只**
   比对 —— 关掉 ST 5% 口径时要求零差异；打开后多出来的必须全部是 ST 股。
2. **独立朴素实现重算连板数**：service 走的是「类别层 + 60 根窗口 + 向量化分段」
   的复杂路径，这里另写一条最笨的路径（整读单只 ``.day`` 全历史、逐根比较、
   循环往回数），两条路径对同一批股票必须给出相同的连板数。
3. **不变量与边界**：一手自洽（各组数字相加 = 涨停家数）、每行确实涨停、
   停牌股不冒充当期、上市首日不参与、越界日期明确报错、北交所 30% / ST 5% 口径。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from stock_picker.config import DATA_DIR, Config  # noqa: E402
from stock_picker.limits import (  # noqa: E402
    factor_label,
    is_st_name,
    limit_factor,
    limit_prices,
)
from stock_picker.names import NameIndex  # noqa: E402
from stock_picker.service import MarketService  # noqa: E402
from stock_picker.tdx_reader import TdxReader, board_of, read_day_fast  # noqa: E402

#: 兄弟应用的涨停判定基准（若不存在则自动跳过该节）
BASE_PARQUET = Path(
    r"F:\source\stock_blocks\stock_watch\data\universe.parquet"
)

OK = 0
BAD = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global OK, BAD
    if cond:
        OK += 1
        print(f"  [OK]   {name}" + (f" — {detail}" if detail else ""))
    else:
        BAD += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def build_service() -> MarketService:
    cfg = Config()
    reader = TdxReader(cfg.get("tdx_dir"), mode=cfg.get("reader_mode", "fast"))
    names = NameIndex(cfg.get("tdx_dir"), cache_file=DATA_DIR / "stock_names.json")
    names.load()
    return MarketService(cfg, reader, names)


# ----------------------------------------------------------------------
# 独立实现：不用 service 任何代码，直接读单只 .day 全历史算连板数
# ----------------------------------------------------------------------
def naive_streak(
    svc: MarketService, code: str, anchor: int, lookback: int = 60,
    st_limit: bool = False,
) -> int:
    """最笨的路径：整读该股 ``.day`` → 逐根判涨停 → 从末尾往回数。

    ``st_limit`` 必须与 service 的口径一致（默认 False）—— 这是上一版测试误报的
    原因：service 默认关了 5% 口径，而这里用默认的 True，ST 股就会多数出一板。
    """
    path = svc.reader.daily_file(code)
    if path is None:
        return -1
    df = read_day_fast(path)                       # 全历史，不做任何窗口裁剪
    df = df[df["date"] <= anchor]
    if len(df) == 0:
        return -1
    df = df.tail(lookback)
    close = df["close"].to_numpy(dtype="float64")
    if len(close) < 2:
        return 0
    prev = np.concatenate(([np.nan], close[:-1]))
    name = svc.names.get(code) or ""
    factor = limit_factor(code, name, st_limit=st_limit)
    # 逐根按「分」整数判涨停（与 limits.limit_masks 同一公式，但独立写一遍）
    cnt = 0
    for i in range(len(close) - 1, -1, -1):
        if not np.isfinite(prev[i]):
            break
        pc = int(round(prev[i] * 100))
        cc = int(round(close[i] * 100))
        lup = (pc * factor + 50) // 100
        if cc >= lup:
            cnt += 1
        else:
            break
    return cnt


def sym_of(x: dict) -> str:
    """明细行 → 与基准 ``universe.parquet`` 同形态的 sym（如 ``sh600753``）。"""
    return x["market"].lower() + x["symbol"]


def st_band_probe(svc: MarketService) -> tuple:
    """度量主板 ST 股的**真实**涨跌幅带宽，用于验证「ST 5%」在本机数据里是否成立。

    做法：对每只主板 ST 股算 2025 年以来 ``max(high / 前收)``。真正的 5% 股这个值
    不会超过 1.05（留 1 个百分点容差取 1.06）；实测普遍落在 1.10。

    :return: ``(主板 ST 股数, 其中带宽 > 1.06 的只数, 这些最大值的 p99.9)``
    """
    df = svc.bars()
    codes = np.asarray(df["code"].cat.categories, dtype=object)
    cat = df["code"].cat.codes.to_numpy()
    close = np.rint(df["close"].to_numpy(dtype="float64") * 100).astype("int64")
    high = np.rint(df["high"].to_numpy(dtype="float64") * 100).astype("int64")
    date = df["date"].to_numpy()
    prev = np.zeros(close.size, dtype="int64")
    same = cat[1:] == cat[:-1]
    prev[1:] = np.where(same, close[:-1], 0)
    ok = (prev > 0) & (date >= 20250101)
    ratio = np.where(ok, high / np.maximum(prev, 1), 0.0)
    mx = np.zeros(codes.size, dtype="float64")
    np.maximum.at(mx, cat, ratio)

    nm = [svc.names.get(c) or "" for c in codes]
    flags = np.array(
        [board_of(c) == "主板" and is_st_name(n) and "退" not in n
         for c, n in zip(codes, nm)],
        dtype=bool,
    )
    sel = mx[flags]
    return int(flags.sum()), int((sel > 1.06).sum()), (
        float(np.percentile(sel, 99.9)) if sel.size else 0.0
    )


def main() -> int:
    print("=" * 72)
    print("连板梯队专项回归测试")
    print("=" * 72)

    if not (DATA_DIR / "cache" / "bars.parquet").is_file():
        print("!! 行情缓存不存在，请先跑一次应用或 packaging/fullhistory_test.py")
        return 1

    t0 = time.perf_counter()
    svc = build_service()
    print(f"载入服务 {time.perf_counter() - t0:.1f}s | 行情 {len(svc.bars()):,} 行")
    dates = svc.trade_dates(0)
    print(f"交易日 {len(dates)} 个：{dates[0]} ~ {dates[-1]}")

    # ------------------------------------------------------------------
    print("\n[1] 基本可用性 / 不变量")
    # ------------------------------------------------------------------
    r = svc.streaks({})
    check("返回 ok", r.get("ok") is True)
    check("锚定最新交易日", r["date"] == dates[-1], r["date"])
    m = r["metrics"]
    check("耗时 < 1s", r["elapsed"] < 1.0, f"{r['elapsed']}s")
    check("涨停家数 > 0", m["limit_up"] > 0, str(m["limit_up"]))
    check(
        "首板 + 2板 + 3板+ = 涨停家数",
        m["first"] + m["second"] + m["high3"] == m["limit_up"],
        f"{m['first']}+{m['second']}+{m['high3']} vs {m['limit_up']}",
    )
    check(
        "hist 求和 = 涨停家数",
        sum(h["n"] for h in r["hist"]) == m["limit_up"],
        f"{sum(h['n'] for h in r['hist'])} vs {m['limit_up']}",
    )
    check(
        "ladder 求和 = 涨停家数",
        sum(l["n"] for l in r["ladder"]) == m["limit_up"],
    )
    check(
        "最高连板 = hist 首项",
        m["max_streak"] == r["hist"][0]["streak"],
        f"{m['max_streak']} vs {r['hist'][0]['streak']}",
    )
    check(
        "晋级率 = 晋级数 / 前一日涨停",
        m["promotion"] is None
        or abs(m["promotion"] - m["promote_n"] / max(1, m["prev_limit_up"])) < 1e-4,
        f"{m['promotion']} promote_n={m['promote_n']} prev={m['prev_limit_up']}"
        "（promotion 保留 4 位小数，容差 1e-4）",
    )
    check(
        "2 板及以上 = promote_n",
        m["second"] + m["high3"] == m["promote_n"],
    )
    check("默认不启用 ST 5% 口径", r["st_limit"] is False)
    check(
        "显式 st_limit=False 与默认口径一致",
        svc.streaks({"st_limit": False})["metrics"] == m,
    )
    check(
        "连板高度降序",
        all(r["hist"][i]["streak"] > r["hist"][i + 1]["streak"] for i in range(len(r["hist"]) - 1)),
    )

    # 每行必须满足：当天涨停 + 连板数 ≥ 1 + 日期就是锚点日
    rows = r["rows"]
    check("明细非空", len(rows) > 0, f"{len(rows)} 行")
    bad_pct = []
    for x in rows:
        p = x["pct_change"]
        if p is None:
            continue
        rate = (x["limit_factor"] - 100) / 100.0
        # 涨停价四舍五入到「分」会让涨幅与名义幅度有极小偏离（低价股最多约 0.5pp）
        if abs(p - rate) > 0.015:
            bad_pct.append((x["name"], round(p, 4), rate))
    check("每行涨跌幅都贴近其涨停幅度", not bad_pct,
          f"{len(bad_pct)} 只异常 {bad_pct[:3]}" if bad_pct else f"{len(rows)} 只")
    check("每行连板数 ≥ 1", all(x["streak"] >= 1 for x in rows))
    check("每行日期 = 锚点日", all(x["date"] == r["date"] for x in rows))
    check(
        "封板形态取值合法",
        all(x["seal"] in ("一字板", "T字板", "换手板") for x in rows),
        str(sorted({x["seal"] for x in rows})),
    )
    check(
        "涨停幅度与板块匹配",
        all(
            (x["limit_factor"] == 130 and x["board"] == "北交所")
            or (x["limit_factor"] == 120 and x["board"] in ("创业板", "科创板"))
            or (x["limit_factor"] in (105, 110) and x["board"] == "主板")
            for x in rows
        ),
    )

    # 同参数两次结果一致
    r2 = svc.streaks({})
    check(
        "同参数两次结果一致",
        r2["metrics"] == m and len(r2["rows"]) == len(rows)
        and [x["code"] for x in r2["rows"]] == [x["code"] for x in rows],
    )

    # ------------------------------------------------------------------
    print("\n[2] 与兄弟应用 stock_watch 的涨停判定交叉核对")
    # ------------------------------------------------------------------
    if not BASE_PARQUET.is_file():
        print("  (跳过：找不到基准 universe.parquet)")
    else:
        base = pd.read_parquet(BASE_PARQUET, columns=["sym", "date", "limit_up"])
        base["date"] = base["date"].dt.strftime("%Y%m%d").astype("int64")
        days = [
            "2026-09-23", "2026-09-22", "2026-09-18", "2026-09-11",
            "2026-06-01", "2026-05-06", "2025-12-15", "2024-11-07",
        ]
        clean = True
        for day in days:
            b = base[base["date"] == int(day.replace("-", ""))]
            if not len(b):
                continue
            bs = set(b.loc[b["limit_up"], "sym"])
            # 默认口径（= 关闭 ST 5%）→ 应与基准逐只一致
            rn = svc.streaks({"date": day})
            mn = {sym_of(x) for x in rn["rows"]}
            diff = mn ^ bs
            if diff:
                clean = False
                check(f"{day} 默认口径与基准一致", False, f"差 {len(diff)}：{sorted(diff)[:5]}")
            # 打开 ST 5% 口径 → 多出来的必须都是名称含 ST 的标的
            rs = svc.streaks({"date": day, "st_limit": True})
            extra = {sym_of(x) for x in rs["rows"]} - mn
            st_codes = {sym_of(x) for x in rs["rows"] if x["is_st"]}
            stray = extra - st_codes
            if stray:
                clean = False
                check(f"{day} ST 口径多出的都是 ST 股", False, f"异常 {sorted(stray)[:5]}")
        if clean:
            check(f"{len(days)} 个交易日全部对齐基准（默认口径逐只一致）", True)
        # 打开 ST 口径后涨停家数应 ≥ 默认
        a = svc.streaks({"date": "2026-06-01"})["metrics"]["limit_up"]
        b2 = svc.streaks({"date": "2026-06-01", "st_limit": True})["metrics"]["limit_up"]
        check("ST 口径放宽后涨停家数不减少", b2 >= a, f"{a} → {b2}")

        # 把「本机数据 ST 股带宽其实是 10%」这条结论固化成可复现的度量
        st_main, st_violate, maxratio = st_band_probe(svc)
        print(f"  · ST 带宽实测：主板 ST 股 {st_main} 只，其中 {st_violate} 只出现过"
              f"「最高价 > 前收×1.05」，high/prev 的 p99.9 上界 ≈ {maxratio:.4f}")
        check("ST 5% 口径开启后确实会多算涨停（本机数据带宽为 10%）",
              st_main > 0 and st_violate / max(1, st_main) > 0.5,
              f"{st_violate}/{st_main} = {st_violate / max(1, st_main):.1%}")

    # ------------------------------------------------------------------
    print("\n[3] 独立朴素实现重算连板数（逐只 .day 全历史）")
    # ------------------------------------------------------------------
    anchor = int(r["anchor"])
    sample = [x for x in rows if x["streak"] >= 2][:12] + rows[:8]
    mismatch = []
    checked = 0
    for x in sample:
        want = naive_streak(svc, x["code"], anchor)
        if want < 0:
            continue
        checked += 1
        if want != x["streak"]:
            mismatch.append((x["code"], x["name"], x["streak"], want))
    check("样本连板数与独立实现一致", not mismatch,
          f"核对 {checked} 只" + (f"，不一致 {mismatch[:4]}" if mismatch else ""))
    check("样本量足够", checked >= 10, f"{checked} 只")

    # 再用一个历史日期整批核对（覆盖较早的窗口）
    rh = svc.streaks({"date": "2022-06-01"})
    bad_h = []
    for x in rh["rows"][:25]:
        want = naive_streak(svc, x["code"], rh["anchor"])
        if want >= 0 and want != x["streak"]:
            bad_h.append((x["code"], x["streak"], want))
    check("历史日期（2022-06-01）连板数一致", not bad_h,
          f"核对 {min(25, len(rh['rows']))} 只" + (f"，不一致 {bad_h[:4]}" if bad_h else ""))

    # ------------------------------------------------------------------
    print("\n[4] 边界：首板识别 / 停牌 / 越界 / 板块筛选")
    # ------------------------------------------------------------------
    # 首板的 streak 必须 = 1，且前一交易日不能是涨停
    firsts = [x for x in rows if x["streak"] == 1][:6]
    bad_first = []
    ymd_dates = np.array([int(d.replace("-", "")) for d in dates])
    idx = int(np.searchsorted(ymd_dates, r["anchor"]))
    pd_ymd = int(ymd_dates[idx - 1]) if idx > 0 else None
    for x in firsts:
        if pd_ymd is None:
            break
        path = svc.reader.daily_file(x["code"])
        if path is None:
            continue
        df = read_day_fast(path)
        df = df[df["date"] <= pd_ymd]
        if len(df) < 2:
            continue
        c = df["close"].to_numpy(dtype="float64")
        pc, cc = int(round(c[-2] * 100)), int(round(c[-1] * 100))
        f = limit_factor(x["code"], svc.names.get(x["code"]) or "", st_limit=False)
        if cc >= (pc * f + 50) // 100:
            bad_first.append(x["code"])
    check("首板股的前一交易日确实没涨停", not bad_first, str(bad_first[:4]))

    # 停牌股不冒充当期：所有明细行的日期都等于锚点日（上面已查），再查「有行但日期旧」的反例
    check("没有停牌股混入（日期全部 = 锚点）", all(x["date"] == r["date"] for x in rows))

    # 越界日期
    ro = svc.streaks({"date": "1990-01-01"})
    check("越界日期明确报错", ro.get("ok") is False and ro.get("date_out_of_range") is True,
          str(ro.get("reason"))[:60])
    check("越界时给出可选范围", bool(ro.get("date_min") and ro.get("date_max")))

    # 板块筛选
    rb = svc.streaks({"boards": ["主板"]})
    check("只选主板 → 全部为主板且非空",
          rb["ok"] and len(rb["rows"]) > 0 and all(x["board"] == "主板" for x in rb["rows"]),
          f"主板 {rb['metrics']['limit_up']} 只")
    rall = svc.streaks({"boards": ["主板", "创业板", "科创板", "北交所"]})
    check("全板块 ≥ 单板块", rall["metrics"]["limit_up"] >= rb["metrics"]["limit_up"],
          f"{rall['metrics']['limit_up']} vs {rb['metrics']['limit_up']}")
    check("空板块报错", svc.streaks({"boards": []}).get("ok") is False)
    # 裸字符串不能被拆成单字（曾把 "主板" 拆成 ['主','板'] 静默返回空）
    rs1 = svc.streaks({"boards": "主板"})
    check("boards 传裸字符串与列表等价",
          rs1["metrics"]["limit_up"] == rb["metrics"]["limit_up"] > 0,
          f"{rs1['metrics']['limit_up']} vs {rb['metrics']['limit_up']}")
    check("boards 支持顿号/逗号混写",
          svc.streaks({"boards": "主板、创业板"})["metrics"]["limit_up"]
          >= rb["metrics"]["limit_up"])
    check("未知板块明确报错",
          svc.streaks({"boards": ["主板", "主板板"]}).get("ok") is False
          and "未知板块" in (svc.streaks({"boards": ["主板板"]}).get("reason") or ""))
    check("未知交易所明确报错",
          svc.streaks({"markets": ["sh", "xx"]}).get("ok") is False)

    # min_streak 过滤
    r3 = svc.streaks({"min_streak": 3})
    check("min_streak=3 → 明细全 ≥ 3 板",
          all(x["streak"] >= 3 for x in r3["rows"]) and len(r3["rows"]) > 0,
          f"{r3['rows_count']} 只")
    check("min_streak 不影响指标",
          r3["metrics"]["limit_up"] == m["limit_up"],
          f"{r3['metrics']['limit_up']} vs {m['limit_up']}")
    check("min_streak=3 时 ladder 层数减少",
          len(r3["ladder"]) < len(r["ladder"]),
          f"{len(r3['ladder'])} vs {len(r['ladder'])}")

    # ------------------------------------------------------------------
    print("\n[5] 涨跌幅口径（limits 单元级）")
    # ------------------------------------------------------------------
    check("主板 10%", limit_factor("600000.SH") == 110)
    check("创业板 20%", limit_factor("300750.SZ") == 120)
    check("科创板 20%", limit_factor("688111.SH") == 120)
    check("北交所 30%", limit_factor("920427.BJ") == 130)
    check("北交所 8xxxxx 也是 30%", limit_factor("830799.BJ") == 130)
    check("主板 ST 5%", limit_factor("600000.SH", "*ST测试") == 105)
    check("创业板 ST 仍 20%", limit_factor("300001.SZ", "*ST测试") == 120)
    check("北交所 ST 仍 30%", limit_factor("920427.BJ", "*ST测试") == 130)
    check("关掉 ST 口径 → 主板回到 10%", limit_factor("600000.SH", "*ST测试", st_limit=False) == 110)
    check("退市股不误判成 ST 5%", limit_factor("600000.SH", "退市测试") == 110)
    check("is_st_name 识别 *ST / ST",
          is_st_name("*ST华微") and is_st_name("ST海王") and not is_st_name("华微电子"))
    check("factor_label", factor_label(105) == "5%" and factor_label(110) == "10%"
          and factor_label(130) == "30%")

    # 涨停价四舍五入（分整数）—— 标量入参也要能跑（_to_cent 用 atleast_1d）
    lup, ldp = limit_prices(10.15, 110)
    check("涨停价 10.15×1.1 → 11.17（四舍五入到分）", int(lup[0]) == 1117, f"{int(lup[0])}")
    check("跌停价 10.15×0.9 → 9.14", int(ldp[0]) == 914, f"{int(ldp[0])}")
    lup2, _ = limit_prices(3.33, 105)
    check("ST 涨停价 3.33×1.05 → 3.50", int(lup2[0]) == 350, f"{int(lup2[0])}")

    # 数组入参与标量入参结果一致（形状差异不影响取值）
    lup_a, ldp_a = limit_prices(np.array([10.15, 3.33, 100.00]), np.array([110, 105, 120]))
    check("数组入参", list(map(int, lup_a)) == [1117, 350, 12000], str(list(lup_a)))
    check("数组入参（跌停）", list(map(int, ldp_a)) == [914, 316, 8000], str(list(ldp_a)))
    check("标量/形状都返回 1 维数组",
          np.asarray(limit_prices(1.0, 110)[0]).shape == (1,))

    # NaN / 非正前收不判定
    from stock_picker.limits import limit_masks  # noqa: E402
    lu, tu, ld = limit_masks(
        np.array([1.10, 1.10, 1.10]),
        np.array([1.10, 1.10, 1.10]),
        np.array([1.10, 1.10, 1.10]),
        np.array([np.nan, 0.0, 1.00]),
        np.array([110, 110, 110]),
    )
    check("前收 NaN / 0 不判定", not lu[0] and not lu[1] and not tu[0] and not ld[0])
    check("前收 1.00 涨停 1.10 命中", bool(lu[2]))
    lu2, tu2, _ = limit_masks(
        np.array([1.10]), np.array([1.10]), np.array([1.10]), np.array([1.00]),
        np.array([110]), np.array([True]),
    )
    check("上市首日剔除", not lu2[0] and not tu2[0])

    # ------------------------------------------------------------------
    print("\n[6] 历史日期可用性")
    # ------------------------------------------------------------------
    for day in ("2018-05-10", "2015-06-05", "2022-06-01"):
        rr = svc.streaks({"date": day})
        check(f"{day} 可算", rr.get("ok") is True and rr["metrics"]["limit_up"] > 0,
              f"涨停 {rr.get('metrics', {}).get('limit_up')} 只 · 最高 "
              f"{rr.get('metrics', {}).get('max_streak')} 板")

    print()
    print("=" * 72)
    print(f"结果：{OK} 通过 / {BAD} 失败")
    print("=" * 72)
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
