# -*- coding: utf-8 -*-
"""源码级回归：分布卡（行业 / 概念）的基数不受行业/概念筛选影响。

用法（源码态，不需要起服务）::

    python packaging/summary_base_test.py

背景：industry_summary / concept_summary 曾是在行业、概念过滤之后统计的，
所以一点选行业，分布卡就只剩那一个行业、其余 chip 全部消失，无法再切换。
修法是把统计基数改为「除行业/概念筛选本身以外的全部条件命中集」
（响应字段 `summary_base`），同时把市值过滤前移到行业过滤之前，让基数口径完整。

本脚本同时回归「市值过滤语义未因过滤顺序调整而改变」。
断言全通过时退出码 0，否则 1。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_picker.app import service  # noqa: E402

FAILD = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"   {detail}" if detail else ""))
    if not cond:
        FAILD.append(name)


if not getattr(service, "ready", False):
    print("行情缓存未就绪，先刷新…")
    service.refresh()

print("=" * 78)
print("一、基线（无行业 / 概念筛选）")
print("=" * 78)
base = service.screen({"max_results": 100000})
b_ind = {g["l2"]: g["n"] for g in base["industry_summary"]}
b_cpt = {g["concept"]: g["n"] for g in base["concept_summary"]}
print(f"命中 {base['total_hits']} 只 · 基数 {base['summary_base']} · "
      f"行业 {len(b_ind)} 个 · 概念 {len(b_cpt)} 个")
check("基线 summary_base == total_hits", base["summary_base"] == base["total_hits"],
      f"{base['summary_base']} vs {base['total_hits']}")

print()
print("=" * 78)
print("二、点选行业：分布卡必须保持不变")
print("=" * 78)
top5 = [g["l2"] for g in base["industry_summary"][:5]]
for l2 in top5:
    r = service.screen({"industries": [l2], "max_results": 100000})
    cur = {g["l2"]: g["n"] for g in r["industry_summary"]}
    check(f"选「{l2}」→ 分布卡仍 {len(b_ind)} 个行业", len(cur) == len(b_ind),
          f"{len(cur)} vs {len(b_ind)}")
    check(f"选「{l2}」→ 分布计数与基线逐项一致", cur == b_ind)
    check(f"选「{l2}」→ 该行业计数 == 实际命中数", cur.get(l2) == r["total_hits"],
          f"{cur.get(l2)} vs {r['total_hits']}")
    check(f"选「{l2}」→ 结果行行业无泄漏",
          all(x.get("industry_l2") == l2 for x in r["rows"]))
    check(f"选「{l2}」→ summary_base 不变", r["summary_base"] == base["summary_base"],
          f"{r['summary_base']} vs {base['summary_base']}")

print()
print("=" * 78)
print("三、点选概念：分布卡同样保持不变")
print("=" * 78)
top3 = [g["concept"] for g in base["concept_summary"][:3]]
for nm in top3:
    r = service.screen({"concepts": [nm], "max_results": 100000})
    cur = {g["concept"]: g["n"] for g in r["concept_summary"]}
    check(f"选「{nm}」→ 概念分布卡仍 {len(b_cpt)} 个", len(cur) == len(b_cpt),
          f"{len(cur)} vs {len(b_cpt)}")
    check(f"选「{nm}」→ 概念分布与基线逐项一致", cur == b_cpt)
    check(f"选「{nm}」→ 该概念计数 == 实际命中数", cur.get(nm) == r["total_hits"],
          f"{cur.get(nm)} vs {r['total_hits']}")
    check(f"选「{nm}」→ 行业分布卡也未被影响",
          {g["l2"]: g["n"] for g in r["industry_summary"]} == b_ind)

print()
print("=" * 78)
print("四、行业 + 概念 组合筛选，两张分布卡都不变")
print("=" * 78)
combo = service.screen({"industries": [top5[0]], "concepts": [top3[0]], "max_results": 100000})
check("组合筛选 → 行业分布不变",
      {g["l2"]: g["n"] for g in combo["industry_summary"]} == b_ind)
check("组合筛选 → 概念分布不变",
      {g["concept"]: g["n"] for g in combo["concept_summary"]} == b_cpt)
check("组合筛选 → 结果同时满足两个条件",
      all(x.get("industry_l2") == top5[0] and top3[0] in (x.get("concepts") or [])
          for x in combo["rows"]))
print(f"  组合命中 {combo['total_hits']} 只（行业「{top5[0]}」且概念「{top3[0]}」）")

print()
print("=" * 78)
print("五、回归：市值过滤语义未因顺序调整而改变")
print("=" * 78)
nolimit = service.screen({"max_results": 100000, "max_float_mcap": 0})
limited = service.screen({"max_results": 100000, "max_float_mcap": 150})
exp_codes = {x["code"] for x in nolimit["rows"]
             if x.get("float_mcap_yi") is not None and x["float_mcap_yi"] < 150}
got_codes = {x["code"] for x in limited["rows"]}
check("上限 150 亿的命中集 == 全量中流通市值 < 150 亿的子集",
      got_codes == exp_codes, f"{len(got_codes)} vs {len(exp_codes)}")
check("上限 150 亿时无一行越界",
      all(x["float_mcap_yi"] < 150 for x in limited["rows"] if x.get("float_mcap_yi") is not None))
check("设上限后 total_hits 与行数一致（未截断时）",
      limited["total_hits"] == len(limited["rows"]))

print()
print("=" * 78)
if FAILD:
    print(f"失败 {len(FAILD)} 项：")
    for n in FAILD:
        print("  -", n)
    sys.exit(1)
print("全部断言通过 ✅")
