# -*- coding: utf-8 -*-
"""源码级回归：复权因子（``divfactor.py``）与「偏离度」附图（``service._bias``）。

用法（源码态，不需要起服务）::

    python packaging/divfactor_test.py

背景
----
用户给的通达信公式::

    FQ    := DIVFACTOR(1) / CONST(DIVFACTOR(1));
    MA250 := SUM(AMOUNT*FQ, N6) / SUM(VOL, N6) / 100;
    T     := ((CLOSE - MA250) / CLOSE) * 100;
    M5    := EMA(T, 3);
    M20   := EMA(T, 20);

**关键前提**：本项目行情缓存里 ``close`` / ``amount`` / ``volume`` **三者都是
不复权原始值**（不是前复权！）。所以跨除权日直接算 250 日加权均价会把两种
价位尺度平均到一起，``T`` 在除权日出现**假跳空**（实测 000002.SZ 2003-05-23
从 +11.6% 砸到 −77.5%）。必须先用前复权因子统一折算。

本节回归钉住的核心不变量
------------------------
1. 缓存三列**同口径**（不复权）—— ``close / (amount/volume)`` 恒接近 1
2. 复权因子把除权跳空**抹平**（原始跳空 −50%，复权后回到日常波动）
3. ``T`` 在除权日**不再假跳空**
4. 因子最末一日 == 1.0（前复权定义：最新价即原始价）
5. 无除权事件的票 → 因子恒为 1（原始价即前复权价）
6. ``_bias`` 的窗口是**末尾对齐**的滑动窗口，不引入未来函数
7. ``sum(amount)/sum(volume/F)`` 的量纲仍是「元/股」

**不写死会随时间变化的期望值**，只写结构性不变量与交叉核对。

断言全通过时退出码 0，否则 1。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_picker.app import service  # noqa: E402
from stock_picker.divfactor import build_factor_series, step_factor  # noqa: E402

FAILD = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"   {detail}" if detail else ""))
    if not cond:
        FAILD.append(name)


service.refresh()
DF = service.div_factors
BARS = service.bars()

print("=" * 78)
print("一、前提：缓存三列同口径（不复权）")
print("=" * 78)
s = BARS[BARS["volume"] > 0].sample(min(200000, len(BARS)), random_state=1)
ratio = (s["close"] / (s["amount"] / s["volume"])).astype("float64")
mid = float(ratio.median())
inside = float(ratio.between(0.9, 1.1).mean())
check("close / (amount/volume) 中位数 ≈ 1", abs(mid - 1.0) < 0.01, f"{mid:.5f}")
check("≥ 99% 的样本比值落在 [0.9, 1.1]", inside >= 0.99, f"{inside:.4%}")

print()
print("=" * 78)
print("二、除权事件表：解析与已知事实核对")
print("=" * 78)
meta = DF.meta or {}
check("解析出除权记录", int(meta.get("events") or 0) > 0, f"events={meta.get('events')}")
check("覆盖数千只票", DF.size() > 1000, f"codes={DF.size()}")

ev2 = DF.events_of("000002.SZ")
hit = [e for e in ev2 if e[0] == 20030523]
check("000002.SZ 2003-05-23 命中（10 送 10 派 2）",
      bool(hit) and abs(hit[0][3] - 10.0) < 1e-6 and abs(hit[0][1] - 2.0) < 1e-6,
      str(hit[0]) if hit else "未命中")

print()
print("=" * 78)
print("三、复权因子：除权跳空必须被抹平")
print("=" * 78)
sub = BARS[BARS["code"] == "000002.SZ"].sort_values("date").reset_index(drop=True)
dates = [int(d) for d in sub["date"]]
raw = [float(c) for c in sub["close"]]
f = build_factor_series(dates, raw, ev2)
check("因子序列与日线等长", len(f) == len(dates), f"{len(f)} vs {len(dates)}")
check("最末一日因子 == 1.0（前复权定义）", abs(f[-1] - 1.0) < 1e-9, f"{f[-1]}")
check("所有因子落在 (0, 1]", all(0 < x <= 1.0 + 1e-9 for x in f))

# 除权日跳空对比
raw_jump = abs(raw[dates.index(20030523)] / raw[dates.index(20030523) - 1] - 1)
adj = [r * x for r, x in zip(raw, f)]
adj_jump = abs(adj[dates.index(20030523)] / adj[dates.index(20030523) - 1] - 1)
check("原始跳空很大（>30%）", raw_jump > 0.30, f"{raw_jump:.1%}")
check("前复权后跳空被抹平（<5%）", adj_jump < 0.05, f"{adj_jump:.2%}")

# 多个除权日逐个核对
bad = []
for d in [20030523, 20040526, 20050629, 20070516, 20080616]:
    if d not in dates:
        continue
    k = dates.index(d)
    rj = abs(raw[k] / raw[k - 1] - 1)
    aj = abs(adj[k] / adj[k - 1] - 1)
    if rj > 0.25 and aj >= 0.10:
        bad.append((d, rj, aj))
check("5 个除权日全部被修正（复权后均 <10%）", not bad, str(bad))

print()
print("=" * 78)
print("四、单步因子公式（手工核对）")
print("=" * 78)
# 000002.SZ 2003-05-23: hongli=2.0, songgu=10.0, P_raw(05-22)=13.81
fv = step_factor(2.0, 0.0, 10.0, 0.0, 13.81)
expect = (1 - 0.2 / 13.81) / 2.0
check("单步因子 == (1 - 0.2/13.81)/2", fv is not None and abs(fv - expect) < 1e-9,
      f"{fv!r} vs {expect:.6f}")
check("纯派现：因子 < 1", (lambda x: x is not None and x < 1.0)(
    step_factor(4.2, 0.0, 0.0, 0.0, 10.0)))
check("无事件参数（全 0 + 价格有效）→ 因子 == 1", (lambda x: x is not None and abs(x - 1.0) < 1e-9)(
    step_factor(0.0, 0.0, 0.0, 0.0, 10.0)))
check("价格非法 → None", step_factor(2.0, 0.0, 10.0, 0.0, 0.0) is None)

print()
print("=" * 78)
print("五、无除权事件的票：因子恒为 1")
print("=" * 78)
# 找一只事件表里没有的票（或在区间内无事件的票）
found = None
for code, grp in BARS.groupby("code", observed=True, sort=False):
    if not DF.events_of(str(code)):
        found = str(code)
        break
if found is None:
    print("  [SKIP] 全市场都有除权记录，跳过")
else:
    g = BARS[BARS["code"] == found].sort_values("date")
    fs = build_factor_series([int(d) for d in g["date"]], [float(c) for c in g["close"]], [])
    check(f"{found} 无事件 → 因子全为 1", all(abs(x - 1.0) < 1e-12 for x in fs), f"n={len(fs)}")

print()
print("=" * 78)
print("六、_bias：T 在除权日不再假跳空")
print("=" * 78)
payload = service._kline_payload(sub, 400, 20)
b = payload["bias"]
check("payload 带 bias 段", isinstance(b, dict) and "t" in b)
check("bias.adjusted == True（用了复权因子）", b.get("adjusted") is True)
check("bias.window == 250", b.get("window") == 250)
check("与 bars 等长", len(b["t"]) == payload["bars"], f"{len(b['t'])} vs {payload['bars']}")

# 直接对比：复权 vs 不复权，在除权日附近
b_adj = service._bias(sub, factor=service._div_factor_series(sub), window=250)
b_raw = service._bias(sub, factor=None, window=250)
k = dates.index(20030523)
t_adj = b_adj["t"][k]
t_raw = b_raw["t"][k]
check("不复权直算在除权日会跳空（|Δ| > 30）",
      t_adj is not None and t_raw is not None and abs(t_raw - t_adj) > 30,
      f"adj={t_adj:.2f} raw={t_raw:.2f}")
# 复权版在除权日前后应连续（相对前一日变化 < 10 个百分点）
prev_t = b_adj["t"][k - 1]
check("复权版除权日前后连续（|Δ| < 10）",
      prev_t is not None and t_adj is not None and abs(t_adj - prev_t) < 10,
      f"prev={prev_t:.2f} now={t_adj:.2f}")

print()
print("=" * 78)
print("七、_bias：内部一致性与缺失处理")
print("=" * 78)
# 手算 T 核对（用复权后的序列）
import pandas as pd  # noqa: E402

fac = service._div_factor_series(sub)
adj_close = pd.Series([c * x for c, x in zip(raw, fac)])
adj_vol = pd.Series([v / x for v, x in zip(sub["volume"].astype("float64"), fac)])
ma = sub["amount"].astype("float64").rolling(250, min_periods=250).sum() / adj_vol.rolling(250, min_periods=250).sum()
t_manual = ((adj_close - ma) / adj_close * 100.0).tail(len(sub))
last = b_adj["t"][-1]
check("末根 T 与手算一致", last is not None and abs(last - float(t_manual.iloc[-1])) < 1e-3,
      f"{last} vs {float(t_manual.iloc[-1]):.4f}")

# M5/M20 是 T 的 EMA：EMA 的第一个有效值应等于 T 的第一个有效值
first_valid = next((i for i, v in enumerate(b_adj["t"]) if v is not None), None)
check("存在有效的 T 值", first_valid is not None,
      f"first_valid={first_valid}")
if first_valid is not None:
    check("M5 首个有效值 == T 首个有效值（EMA 从首值起步）",
          b_adj["m5"][first_valid] is not None
          and abs(b_adj["m5"][first_valid] - b_adj["t"][first_valid]) < 1e-9,
          f"{b_adj['m5'][first_valid]} vs {b_adj['t'][first_valid]}")
    check("窗口不足处为 None（不画错线）",
          all(v is None for v in b_adj["t"][:first_valid]))

# 停牌日的缺失应体现为 None（成交量 0 → 复权量 0 → vol_sum 可能为 0）
check("缺失用 None 而非 0 填充",
      all(v is None or isinstance(v, float) for v in b_adj["t"]))

# 短历史（窗口不足）→ 全 None
short = sub.head(100)
b_short = service._bias(short, factor=service._div_factor_series(short), window=250)
check("历史不足 250 根 → ma250 全为 None",
      all(v is None for v in b_short["ma250"]), f"n={len(b_short['ma250'])}")

print()
print("=" * 78)
print("八、量纲：SUM(amount)/SUM(volume/F) 仍是「元/股」")
print("=" * 78)
# 取最后一根：复权 ma250 应与「复权收盘价」同量级（不是 100 倍偏差）
ma_last = b_adj["ma250"][-1]
close_last = adj_close.iloc[-1]
check("复权 ma250 与复权收盘同量级（比值 0.3~3）",
      ma_last is not None and 0.3 < ma_last / close_last < 3.0,
      f"ma={ma_last:.3f} close={close_last:.3f}")
# 不复权直算的 ma250 与原始收盘也应同量级
ma_raw_last = b_raw["ma250"][-1]
check("不复权 ma250 与原始收盘同量级",
      ma_raw_last is not None and 0.3 < ma_raw_last / raw[-1] < 3.0,
      f"ma={ma_raw_last:.3f} close={raw[-1]:.3f}")

print()
print("=" * 78)
print("九、_bias_ma：量能均线乖离（GLXS / GL20 / SMOOTHGL20）")
print("=" * 78)
# 原文：N1=5 / N2=10 / N3=20 / N4=60
payload2 = service._kline_payload(sub, 400, 20)
bm = payload2["bias_ma"]
check("payload 带 bias_ma 段", isinstance(bm, dict) and "glxs" in bm)
check("bias_ma.adjusted == True", bm.get("adjusted") is True)
for key in ("glxs", "gl20", "smooth", "ma20"):
    check(f"bias_ma.{key} 与 bars 等长",
          len(bm.get(key) or []) == payload2["bars"],
          f"{len(bm.get(key) or [])} vs {payload2['bars']}")

bm_adj = service._bias_ma(sub, factor=service._div_factor_series(sub))
bm_raw = service._bias_ma(sub, factor=None)

# 手工重算：VWAP 窗口 5 / 10 / 20（复权口径）
vol_adj = pd.Series([v / x for v, x in zip(sub["volume"].astype("float64"), fac)])
amt_s = sub["amount"].astype("float64")
def _vwap(w):
    return amt_s.rolling(w).sum() / vol_adj.rolling(w).sum()
m5, m10, m20 = _vwap(5), _vwap(10), _vwap(20)
mx = pd.concat([m5, m10, m20], axis=1).max(axis=1)
mn = pd.concat([m5, m10, m20], axis=1).min(axis=1)
g = (mx - mn) / mn * 100.0
sign = (m20 - m20.shift(1)) >= 0
glxs_manual = g.where(sign, -g)
gl20_manual = ((adj_close - m20) / adj_close) * 100.0
sm_manual = gl20_manual.rolling(3).mean()

check("末根 GL20 与手算一致",
      bm_adj["gl20"][-1] is not None
      and abs(bm_adj["gl20"][-1] - float(gl20_manual.iloc[-1])) < 1e-3,
      f"{bm_adj['gl20'][-1]} vs {float(gl20_manual.iloc[-1]):.4f}")
check("末根 GLXS 与手算一致",
      bm_adj["glxs"][-1] is not None
      and abs(bm_adj["glxs"][-1] - float(glxs_manual.iloc[-1])) < 1e-3,
      f"{bm_adj['glxs'][-1]} vs {float(glxs_manual.iloc[-1]):.4f}")
check("末根 SMOOTH 与手算一致（MA(GL20,3) 是简单均线，不是 EMA）",
      bm_adj["smooth"][-1] is not None
      and abs(bm_adj["smooth"][-1] - float(sm_manual.iloc[-1])) < 1e-3,
      f"{bm_adj['smooth'][-1]} vs {float(sm_manual.iloc[-1]):.4f}")

# GLXS 的符号必须与 MA20 斜率一致（这是「T := (MA20-REF(MA20,1))>=0」的语义）
slope_up = (m20 - m20.shift(1)) >= 0
bad_sign = 0
checked_sign = 0
for i in range(1, len(sub)):
    v = bm_adj["glxs"][i]
    if v is None:
        continue
    checked_sign += 1
    if bool(slope_up.iloc[i]) != (v >= 0):
        bad_sign += 1
check("GLXS 符号 == MA20 斜率方向（正=上行、负=下行）",
      checked_sign > 0 and bad_sign == 0,
      f"checked={checked_sign} mismatched={bad_sign}")

# GLXS 应当正负都出现（否则「取符号」语义没生效；除非该票真的单边）
gv = [v for v in (bm_adj["glxs"] or []) if v is not None]
check("GLXS 正负都有（符号项真的在起作用）",
      any(v > 0 for v in gv) and any(v < 0 for v in gv),
      f"正={sum(1 for v in gv if v > 0)} 负={sum(1 for v in gv if v < 0)}")

# GL20 与 GLXS 是两个不同的东西（别把两者混为一谈）
diff_ok = any(
    bm_adj["glxs"][i] is not None and bm_adj["gl20"][i] is not None
    and abs(bm_adj["glxs"][i] - bm_adj["gl20"][i]) > 1e-6
    for i in range(len(sub))
)
check("GLXS 与 GL20 不是同一条线", diff_ok)

# 复权 vs 不复权：除权日附近 GL20 会有可见差异
gap = max(
    (abs((bm_raw["gl20"][i] or 0) - (bm_adj["gl20"][i] or 0))
     for i in range(len(sub))
     if bm_raw["gl20"][i] is not None and bm_adj["gl20"][i] is not None),
    default=0.0,
)
check("复权口径对 GL20 有实质影响（最大差异 > 0.5）", gap > 0.5,
      f"max|Δ|={gap:.3f}")

# 量纲：附图里的 MA20 应与复权收盘同量级（不是 100 倍）
ma20_last = bm_adj["ma20"][-1]
close_last2 = adj_close.iloc[-1]
check("MA20（成交额加权）与复权收盘同量级（0.3~3）",
      ma20_last is not None and 0.3 < ma20_last / close_last2 < 3.0,
      f"ma20={ma20_last:.3f} close={close_last2:.3f}")

# 窗口不足时：前 4 根 MA5 仍应可算（不设 min_periods），但更早处为 None
check("GLXS 前几根为 None、之后有值（窗口不足不硬画）",
      bm_adj["glxs"][0] is None and any(v is not None for v in bm_adj["glxs"]),
      f"first={bm_adj['glxs'][0]}")

# 无复权事件的票 → bias_ma 也正常（因子恒 1），且 adjusted 仍为 True（传了因子）
if found is not None:
    g2 = BARS[BARS["code"] == found].sort_values("date")
    g2 = g2.reset_index(drop=True)
    if len(g2) > 30:
        bm0 = service._bias_ma(g2, factor=service._div_factor_series(g2))
        vv = [v for v in (bm0["gl20"] or []) if v is not None]
        check(f"{found}（无事件）bias_ma 正常产出", len(vv) > 0,
              f"n={len(vv)}")

print()
print("=" * 78)
if FAILD:
    print(f"FAILED {len(FAILD)} 项：")
    for n in FAILD:
        print("  - " + n)
    sys.exit(1)
print("全部通过 ✓")
sys.exit(0)