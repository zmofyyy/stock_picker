# -*- coding: utf-8 -*-
"""板块交集（二级行业 ∩ 概念）专项回归测试。

跑法（源码态，只读，不碰用户数据）：
    python packaging/intersect_test.py

验证策略
--------
1. **独立朴素实现交叉核对**：service 走的是「锚点日取行 → 类别掩码 → 两个索引
   的集合运算」，这里另写一条最笨的路径（直接扫长表那一行、逐只查
   ``industries.get_l2`` / ``concepts.get``、用 set 手工求交），两条路径对同一批
   条件必须给出**逐只相同**的 A / B / A∩B，而不只是数字相同。
2. **集合代数不变量**：容斥关系 ``A + B - I == A∪B``、``only_a + I == A``、
   ``only_b + I == B``、``I <= min(A,B)`` —— 这些与数据无关，永远成立。
3. **语义不变量**：行业多选是并集（两个行业各自的 A 相加 = 合并后的 A）；
   概念 all ≤ any；all 时每行都必须命中全部选中概念；行业侧任何一行的 l2
   都在选中集合里；命中的概念确实属于该股。
4. **过滤真的生效**：板块 / 交易所 / 剔除 ST / 成交额 / 流通市值逐条验证。
5. **边界**：交集为 0 时的建议、未知行业名与概念名明确报错、两个条件都空报错、
   越界日期报错、历史日期可用、max_rows 截断。

**不写死期望值**：任何「A=763」这类具体数字都会随本地数据更新而过期，
这里只断言不变量、交叉核对、与独立实现的一致性。
"""
from __future__ import annotations

import sys  # noqa: E402
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_picker.config import DATA_DIR, Config  # noqa: E402
from stock_picker.limits import is_st_or_delisting  # noqa: E402
from stock_picker.names import NameIndex  # noqa: E402
from stock_picker.service import MarketService  # noqa: E402
from stock_picker.tdx_reader import TdxReader, board_of, split_code  # noqa: E402

ALL_BOARDS = ["主板", "创业板", "科创板", "北交所"]

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
# 独立实现：不用 service.intersect 的任何代码，直接扫长表 + 两个索引
# ----------------------------------------------------------------------
def naive_sets(
    svc: MarketService,
    anchor: int,
    industries=None,
    concepts=None,
    concept_mode: str = "any",
    boards=None,
    markets=None,
    exclude_st: bool = True,
    min_amount: float = 0.0,
    max_float_mcap: float = 0.0,
):
    """返回 ``(candidates, set_a, set_b)``。"""
    df = svc.bars()
    sub = df[df["date"] == anchor]
    codes = []
    for r in sub.itertuples(index=False):
        c = str(r.code)
        if boards is not None and board_of(c) not in boards:
            continue
        if markets is not None and split_code(c)[1] not in markets:
            continue
        if exclude_st and is_st_or_delisting(svc.names.get(c) or ""):
            continue
        if min_amount and float(r.amount) < min_amount:
            continue
        if max_float_mcap:
            fs = svc.shares.get(c)
            if fs is None or float(fs) * float(r.close) / 1e8 >= max_float_mcap:
                continue
        codes.append(c)

    if industries:
        want_i = set(industries)
        set_a = {c for c in codes if svc.industries.get_l2(c) in want_i}
    else:
        set_a = set(codes)

    if concepts:
        want_c = set(concepts)
        set_b = set()
        for c in codes:
            owned = set(svc.concepts.get(c))
            if not owned:
                continue
            if (concept_mode == "all" and want_c <= owned) or (
                concept_mode != "all" and (want_c & owned)
            ):
                set_b.add(c)
    else:
        set_b = set(codes)

    return codes, set_a, set_b


def top_names(svc: MarketService, k: int = 3):
    inds = [g["l2"] for g in svc.industries.l2_counts()[:k]]
    cpts = [c["name"] for c in sorted(svc.concepts.catalog(), key=lambda x: -x["n"])[:k]]
    return inds, cpts


# ----------------------------------------------------------------------
# [1] 基本可用性与自洽
# ----------------------------------------------------------------------
def section_basic(svc: MarketService) -> None:
    print("\n[1] 基本可用性与自洽")
    anchor = svc.latest_date()
    inds, cpts = top_names(svc, 3)
    t0 = time.perf_counter()
    d = svc.intersect({
        "industries": inds, "concepts": cpts,
        "boards": ALL_BOARDS, "exclude_st": False, "max_rows": 3000,
    })
    el = time.perf_counter() - t0
    if not check("主流行业 × 主流概念 返回 ok", d.get("ok"), str(d.get("reason"))):
        return
    check("锚点日 = 最新交易日", d["anchor"] == anchor, f"{d['anchor']} vs {anchor}")
    check("计算耗时 < 1.5s", el < 1.5, f"{el:.3f}s（接口自报 {d['elapsed']}s）")

    A, B, I = d["set_a"], d["set_b"], d["intersect"]
    # 容斥与拆分：与数据无关，必须恒成立
    check("容斥 A+B-I == A∪B", A + B - I == d["union"], f"{A}+{B}-{I} vs {d['union']}")
    check("only_a + I == A", d["only_a"] + I == A, f"{d['only_a']}+{I} vs {A}")
    check("only_b + I == B", d["only_b"] + I == B, f"{d['only_b']}+{I} vs {B}")
    check("A、B 都不超过候选集", A <= d["candidates"] and B <= d["candidates"],
          f"A={A} B={B} 候选={d['candidates']}")
    check("交集 <= min(A,B)", I <= min(A, B), f"{I} vs min({A},{B})")
    # 接口把占比 round 到 4 位，这里按同精度比
    check("占比自洽（A、B 两个口径）",
          abs(d["ratio_a"] - I / max(1, A)) < 1e-4
          and abs(d["ratio_b"] - I / max(1, B)) < 1e-4,
          f"{d['ratio_a']} / {d['ratio_b']}")

    rows = d["rows"]
    check("明细行数 = min(交集数, max_rows)", len(rows) == min(I, 3000),
          f"{len(rows)} vs min({I}, 3000)")
    check("明细里代码不重复", len({r["code"] for r in rows}) == len(rows))
    check("每行的二级行业都在选中集合内",
          all(r["industry_l2"] in set(inds) for r in rows),
          f"越界 {[r['symbol'] for r in rows if r['industry_l2'] not in set(inds)][:3]}")
    # 命中概念必须真的挂在该股上 —— 拿概念索引独立查一遍，而不是信返回的 concepts
    # （concepts 只带前 12 个，用返回值自证会漏掉排在后面的命中项）
    bad_hit = [
        (r["symbol"], c) for r in rows
        for c in r["hit_concepts"] if c not in set(svc.concepts.get(r["code"]))
    ]
    check("命中概念确实挂在该股上（独立查索引）", not bad_hit, f"异常 {bad_hit[:3]}")
    hit_ok = all(h in set(cpts) for r in rows for h in r["hit_concepts"])
    check("命中概念都在选中概念内", hit_ok)
    any_ok = all(len(r["hit_concepts"]) >= 1 for r in rows)
    check("任一模式下每行至少命中 1 个概念", any_ok)
    check("hit_n == len(hit_concepts)",
          all(r["hit_n"] == len(r["hit_concepts"]) for r in rows))
    check("concept_n >= hit_n", all(r["concept_n"] >= r["hit_n"] for r in rows))
    check("每行都有交易日、收盘价、成交额",
          all(r["trade_date"] == anchor and r["close"] > 0 and r["amount_yi"] >= 0
              for r in rows))
    check("默认按成交额降序",
          all(rows[i]["amount_yi"] >= rows[i + 1]["amount_yi"] for i in range(len(rows) - 1)))
    check("conditions 有内容", len(d["conditions"]) >= 6, f"{len(d['conditions'])} 条")
    # 选了行业 → 交集里每只票必有二级行业 → 行业分布之和应**恰好**等于交集数
    ind_sum = sum(g["n"] for g in d["industry_summary"])
    check("交集内部行业分布之和 == 交集数", ind_sum == I, f"{ind_sum} vs {I}")

    # 同参数两次必须一致（不能有随机性）
    d2 = svc.intersect({
        "industries": inds, "concepts": cpts,
        "boards": ALL_BOARDS, "exclude_st": False, "max_rows": 3000,
    })
    check("同参数两次结果一致",
          (d["set_a"], d["set_b"], d["intersect"]) ==
          (d2["set_a"], d2["set_b"], d2["intersect"]),
          f"{d['set_a']}/{d['set_b']}/{d['intersect']}")


# ----------------------------------------------------------------------
# [2] 与独立实现逐只核对
# ----------------------------------------------------------------------
def section_crosscheck(svc: MarketService) -> None:
    print("\n[2] 与独立实现逐只核对（集合级别，不是只比数字）")
    anchor_iso = svc.latest_date()
    anchor = int(anchor_iso.replace("-", ""))
    inds, cpts = top_names(svc, 3)
    cases = [
        ("行业 3 个 × 概念 3 个（任一）", {"industries": inds, "concepts": cpts}),
        ("单行业 × 单概念", {"industries": [inds[0]], "concepts": [cpts[0]]}),
        ("只选行业", {"industries": inds}),
        ("只选概念", {"concepts": cpts}),
        ("概念全部命中", {"concepts": cpts[:2], "concept_mode": "all"}),
        ("限主板 + 剔 ST", {"industries": inds, "concepts": cpts,
                            "boards": ["主板"], "exclude_st": True}),
        ("限创业板 + 沪市", {"industries": [], "concepts": [cpts[0]],
                             "boards": ["创业板"], "markets": ["sz"]}),
        ("成交额 ≥ 1 亿", {"industries": inds, "concepts": cpts,
                           "min_amount": 1e8}),
        ("流通市值 < 100 亿", {"industries": inds, "concepts": cpts,
                               "max_float_mcap": 100.0}),
    ]
    for label, params in cases:
        # 显式给出 markets，避免「接口走 config 默认、朴素实现走全市场」的口径漂移
        mkts = params.get("markets") or ["sh", "sz", "bj"]
        p = {**params, "boards": params.get("boards", ALL_BOARDS),
             "markets": mkts, "max_rows": 5000}
        d = svc.intersect(p)
        if not d.get("ok"):
            check(f"{label} 可用", False, str(d.get("reason")))
            continue
        cand, na, nb = naive_sets(
            svc, anchor,
            industries=params.get("industries"),
            concepts=params.get("concepts"),
            concept_mode=params.get("concept_mode", "any"),
            boards=params.get("boards", ALL_BOARDS),
            markets=mkts,
            exclude_st=params.get("exclude_st", True),
            min_amount=params.get("min_amount", 0.0),
            max_float_mcap=params.get("max_float_mcap", 0.0),
        )
        ni = na & nb
        same = (d["candidates"] == len(cand) and d["set_a"] == len(na)
                and d["set_b"] == len(nb) and d["intersect"] == len(ni))
        detail = (f"接口 A={d['set_a']}/B={d['set_b']}/I={d['intersect']} "
                  f"候选={d['candidates']} · 朴素 A={len(na)}/B={len(nb)}/I={len(ni)} "
                  f"候选={len(cand)}")
        check(f"{label} 与独立实现一致", same, detail)
        if same:
            api_codes = {r["code"] for r in d["rows"]}
            # 明细受 max_rows 截断，只比较被返回的那部分是否都属于朴素交集
            check(f"{label} 明细全部落在朴素交集内", api_codes <= ni,
                  f"多出 {sorted(api_codes - ni)[:3]}")


# ----------------------------------------------------------------------
# [3] 语义不变量
# ----------------------------------------------------------------------
def section_semantics(svc: MarketService) -> None:
    print("\n[3] 语义不变量")
    inds, cpts = top_names(svc, 4)
    base = {"boards": ALL_BOARDS, "exclude_st": False}

    d1 = svc.intersect({**base, "industries": [inds[0]], "concepts": [cpts[0]]})
    d2 = svc.intersect({**base, "industries": [inds[1]], "concepts": [cpts[0]]})
    dm = svc.intersect({**base, "industries": [inds[0], inds[1]], "concepts": [cpts[0]]})
    # 二级行业是单值属性 → 两个行业的 A 互不相交 → 合并后应当恰好等于两者相加
    check("行业多选 = 并集（A1 + A2 == A12）",
          dm["set_a"] == d1["set_a"] + d2["set_a"],
          f"{d1['set_a']} + {d2['set_a']} vs {dm['set_a']}")
    check("行业并集后交集 = 两个交集的并（B 相同）",
          dm["intersect"] == d1["intersect"] + d2["intersect"],
          f"{d1['intersect']} + {d2['intersect']} vs {dm['intersect']}")

    da = svc.intersect({**base, "industries": [inds[0]], "concepts": cpts[:3],
                        "concept_mode": "any"})
    dl = svc.intersect({**base, "industries": [inds[0]], "concepts": cpts[:3],
                        "concept_mode": "all"})
    check("概念 all ≤ any", dl["set_b"] <= da["set_b"], f"all={dl['set_b']} any={da['set_b']}")
    check("all 模式下每行都命中全部选中概念",
          all(set(cpts[:3]) <= set(r["hit_concepts"]) for r in dl["rows"]),
          f"行数 {len(dl['rows'])}")
    check("all 模式下 hit_n == 选中概念数",
          all(r["hit_n"] == len(cpts[:3]) for r in dl["rows"]))
    check("any 模式下 hit_n 可以小于选中概念数（有 1 即算命中）",
          all(r["hit_n"] >= 1 for r in da["rows"]))

    # 单概念时 any 与 all 必然等价
    s_any = svc.intersect({**base, "concepts": [cpts[0]], "concept_mode": "any"})
    s_all = svc.intersect({**base, "concepts": [cpts[0]], "concept_mode": "all"})
    check("单概念时 any 与 all 等价", s_any["set_b"] == s_all["set_b"],
          f"{s_any['set_b']} vs {s_all['set_b']}")

    # 不选行业 → A 应等于候选集
    d3 = svc.intersect({**base, "concepts": [cpts[0]]})
    check("不选行业时 A == 候选集", d3["set_a"] == d3["candidates"],
          f"{d3['set_a']} vs {d3['candidates']}")
    d4 = svc.intersect({**base, "industries": [inds[0]]})
    check("不选概念时 B == 候选集", d4["set_b"] == d4["candidates"],
          f"{d4['set_b']} vs {d4['candidates']}")


# ----------------------------------------------------------------------
# [4] 过滤与排序
# ----------------------------------------------------------------------
def section_filters(svc: MarketService) -> None:
    print("\n[4] 过滤与排序")
    inds, cpts = top_names(svc, 3)
    anchor = svc.latest_date()

    d = svc.intersect({"industries": inds, "concepts": cpts,
                       "boards": ["主板"], "exclude_st": True})
    check("只选主板 → 结果全是主板",
          d["ok"] and all(r["board"] == "主板" for r in d["rows"]),
          f"{len(d['rows'])} 行")
    check("剔 ST → 结果里没有 ST/退市",
          all(not is_st_or_delisting(r["name"] or "") for r in d["rows"]),
          f"异常 {[r['name'] for r in d['rows'] if is_st_or_delisting(r['name'] or '')][:3]}")

    d_st = svc.intersect({"industries": inds, "concepts": cpts,
                          "boards": ["主板"], "exclude_st": False})
    check("包含 ST 时命中数不少于剔除时", d_st["set_a"] >= d["set_a"],
          f"{d['set_a']} → {d_st['set_a']}")

    d_mk = svc.intersect({"industries": inds, "concepts": cpts,
                          "boards": ALL_BOARDS, "markets": ["sh"]})
    check("限沪市 → 结果全是 SH",
          all(r["market"] == "SH" for r in d_mk["rows"]),
          f"异常 {[r['market'] for r in d_mk['rows'] if r['market'] != 'SH'][:3]}")

    d_amt = svc.intersect({"industries": inds, "concepts": cpts,
                           "boards": ALL_BOARDS, "exclude_st": False,
                           "min_amount": 1e8})
    check("成交额 ≥ 1 亿 生效",
          all(r["amount_yi"] >= 0.999 for r in d_amt["rows"]),
          f"最小 {min([r['amount_yi'] for r in d_amt['rows']] or [0])}")

    d_mc = svc.intersect({"industries": inds, "concepts": cpts,
                          "boards": ALL_BOARDS, "exclude_st": False,
                          "max_float_mcap": 80.0})
    bad = [r for r in d_mc["rows"] if r["float_mcap_yi"] is not None
           and r["float_mcap_yi"] >= 80.0]
    check("流通市值 < 80 亿 生效", not bad, f"异常 {[(r['symbol'], r['float_mcap_yi']) for r in bad][:3]}")
    check("市值过滤后命中数不大于过滤前",
          d_mc["intersect"] <= d_mc["candidates"])

    d_sort = svc.intersect({"industries": inds, "concepts": cpts,
                            "boards": ALL_BOARDS, "exclude_st": False,
                            "sort": "code", "max_rows": 400})
    codes = [r["code"] for r in d_sort["rows"]]
    check("sort=code → 按代码升序", codes == sorted(codes), f"前 3 {codes[:3]}")
    d_pct = svc.intersect({"industries": inds, "concepts": cpts,
                           "boards": ALL_BOARDS, "exclude_st": False,
                           "sort": "pct", "max_rows": 400})
    vals = [r["pct_change"] or 0 for r in d_pct["rows"]]
    check("sort=pct → 按涨跌幅降序", vals == sorted(vals, reverse=True), f"前 3 {vals[:3]}")

    d_cap = svc.intersect({"industries": inds, "concepts": cpts,
                           "boards": ALL_BOARDS, "exclude_st": False, "max_rows": 5})
    check("max_rows 截断生效（明细 ≤ 5，交集数不缩水）",
          len(d_cap["rows"]) <= 5 and d_cap["intersect"] >= len(d_cap["rows"]),
          f"{len(d_cap['rows'])} 行 / 交集 {d_cap['intersect']}")


# ----------------------------------------------------------------------
# [5] 空交集与建议
# ----------------------------------------------------------------------
def section_empty(svc: MarketService) -> None:
    print("\n[5] 空交集与调整建议")
    l2_all = set(svc.industries.l2_names())
    print(f"      本地二级行业共 {len(l2_all)} 个，金融相关："
          f"{[n for n in l2_all if any(k in n for k in ('银行', '保险', '证券', '金融'))]}")

    # 找一个真实的、成员很少的行业 × 一个它极不可能沾边的概念，制造空交集
    counted = [g for g in svc.industries.l2_counts() if g["n"] > 0]
    if not counted:
        check("存在可用于空交集测试的行业", False, "本地行业表为空")
        return
    l2_small = sorted(counted, key=lambda g: g["n"])[0]["l2"]
    d = svc.intersect({"industries": [l2_small], "concepts": ["光刻机"],
                       "boards": ALL_BOARDS, "exclude_st": False})
    check(f"「{l2_small}」× 「光刻机」返回 ok", d.get("ok"), str(d.get("reason")))
    if not d.get("ok"):
        return
    print(f"      A={d['set_a']} B={d['set_b']} A∩B={d['intersect']}（{l2_small}）")
    if d["intersect"] == 0:
        sg = d.get("suggest") or {}
        ci = sg.get("concepts_in_industry") or []
        ic = sg.get("industries_in_concept") or []
        check("交集为空时给出两侧建议", bool(ci) and bool(ic),
              f"概念建议 {len(ci)} 条 / 行业建议 {len(ic)} 条")
        check("建议按出现次数降序",
              all(ci[i]["n"] >= ci[i + 1]["n"] for i in range(len(ci) - 1)),
              str([x["n"] for x in ci[:6]]))
        check("概念建议每条的出现次数不超过 A 的规模",
              all(x["n"] <= d["set_a"] for x in ci), f"A={d['set_a']}")
        check("行业建议的出现次数合计不超过 B 的规模",
              sum(x["n"] for x in ic) <= d["set_b"], f"B={d['set_b']}")
        check("明细为空", d["rows_count"] == 0 and d["total"] == 0)
        check("空交集时并集 = A + B",
              d["union"] == d["set_a"] + d["set_b"],
              f"{d['union']} vs {d['set_a']}+{d['set_b']}")
    else:
        check("有交集时不返回建议", not (d.get("suggest") or {}),
              f"交集 {d['intersect']} 只")


# ----------------------------------------------------------------------
# [6] 边界与错误
# ----------------------------------------------------------------------
def section_edges(svc: MarketService) -> None:
    print("\n[6] 边界与错误")
    inds, cpts = top_names(svc, 2)

    r = svc.intersect({"industries": ["这个行业不存在"], "concepts": [cpts[0]]})
    check("未知二级行业明确报错", r["ok"] is False and "未知二级行业" in (r.get("reason") or ""),
          str(r.get("reason")))
    check("未知行业时返回 unknown 列表", bool(r.get("unknown")))

    r = svc.intersect({"industries": [inds[0]], "concepts": ["这个也不存在"]})
    check("未知概念明确报错", r["ok"] is False and "未知概念" in (r.get("reason") or ""),
          str(r.get("reason")))

    r = svc.intersect({"industries": [], "concepts": []})
    check("两侧都空 → 明确报错（而不是悄悄返回全市场）",
          r["ok"] is False and "至少" in (r.get("reason") or ""), str(r.get("reason")))

    r = svc.intersect({"industries": [inds[0]], "boards": []})
    check("空板块 → 明确报错", r["ok"] is False and "板块" in (r.get("reason") or ""),
          str(r.get("reason")))

    r = svc.intersect({"industries": [inds[0]], "markets": []})
    check("空交易所 → 明确报错", r["ok"] is False and "交易所" in (r.get("reason") or ""),
          str(r.get("reason")))

    r = svc.intersect({"date": "1900-01-01", "industries": [inds[0]]})
    check("越界日期 → 明确报错并给范围",
          r["ok"] is False and r.get("date_out_of_range")
          and r.get("date_min") and r.get("date_max"),
          f"{r.get('reason')}")

    # 历史日期：挑一个中间的交易日，A / B 必须都能算出来
    dates = svc.trade_dates(0)
    mid = dates[len(dates) // 2]
    d = svc.intersect({"date": mid, "industries": inds, "concepts": cpts,
                       "boards": ALL_BOARDS, "exclude_st": False})
    if check(f"历史日期 {mid} 可用", d.get("ok"), str(d.get("reason"))):
        check("历史日期每行 trade_date == 所选日期",
              all(r["trade_date"] == mid for r in d["rows"]),
              f"{len(d['rows'])} 行")
        check("历史日期下 A/B 非空", d["set_a"] > 0 and d["set_b"] > 0,
              f"A={d['set_a']} B={d['set_b']}")
        check("回溯历史时给出「板块成分非历史快照」提示",
              any("当前" in w and "成份" in w for w in d["warnings"]),
              str(d["warnings"])[:120])

    # 概念归属缺失的股票不应混进 B
    d_c = svc.intersect({"concepts": cpts, "boards": ALL_BOARDS, "exclude_st": False,
                         "max_rows": 3000})
    check("概念集合 B 里的每只票都确实有概念归属",
          all(r["concept_n"] > 0 for r in d_c["rows"]),
          f"异常 {[r['symbol'] for r in d_c['rows'] if r['concept_n'] <= 0][:3]}")

    # 行业归属缺失 → 选任何行业都不该命中它
    d_i = svc.intersect({"industries": inds, "boards": ALL_BOARDS,
                         "exclude_st": False, "max_rows": 3000})
    check("行业集合 A 里每只票都有二级行业",
          all(r["industry_l2"] for r in d_i["rows"]),
          f"异常 {[x['symbol'] for x in d_i['rows'] if not x['industry_l2']][:3]}")

    # 归属覆盖率：本地索引的缺口要如实报进 warnings
    print(f"      概念归属缺失 {d_i['concept_missing']} 只 / 行业归属缺失 "
          f"{d_i['industry_missing']} 只（接口自报，候选 {d_i['candidates']} 只）")
    if d_c["concept_missing"]:
        check("概念归属有缺口时给出 warning",
              any("概念归属" in w for w in d_c["warnings"]),
              str(d_c["warnings"])[:140])
    if d_i["industry_missing"]:
        check("行业归属有缺口时给出 warning",
              any("二级行业归属" in w for w in d_i["warnings"]),
              str(d_i["warnings"])[:140])
    check("warnings 收尾带风险提示",
          not d_c["warnings"]
          or any("不构成任何投资建议" in w for w in d_c["warnings"]),
          str(d_c["warnings"])[-60:])


# ----------------------------------------------------------------------
# [7] 接口层（FastAPI）：单位换算与参数解析
# ----------------------------------------------------------------------
def section_api() -> None:
    print("\n[7] 接口层 /api/intersect（亿元单位换算、逗号解析）")
    try:
        from fastapi.testclient import TestClient

        from stock_picker import app as app_mod
    except Exception as exc:  # pragma: no cover
        check("TestClient 可用", False, f"跳过：{exc}")
        return

    svc = app_mod.service
    # 不进 with：那会触发 lifespan（启动预加载），测试只需要接口本身
    client = TestClient(app_mod.app)
    top = sorted(svc.concepts.catalog(), key=lambda x: -x["n"])[:2]
    cpts = [c["name"] for c in top]
    inds = [g["l2"] for g in svc.industries.l2_counts()[:2]]

    r = client.get("/api/intersect", params={
        "industries": ",".join(inds),
        "concepts": ",".join(cpts),
        "boards": "主板,创业板,科创板,北交所",
        "exclude_st": "false",
    })
    check("HTTP 200", r.status_code == 200, str(r.status_code))
    d = r.json()
    check("接口返回 ok", d.get("ok"), str(d.get("reason")))

    # 与直接调用 service 对齐（同参数）
    direct = svc.intersect({
        "industries": inds, "concepts": cpts,
        "boards": ALL_BOARDS, "exclude_st": False,
    })
    check("接口结果与 service 直调一致",
          (d["set_a"], d["set_b"], d["intersect"]) ==
          (direct["set_a"], direct["set_b"], direct["intersect"]),
          f"{d['set_a']}/{d['set_b']}/{d['intersect']} vs "
          f"{direct['set_a']}/{direct['set_b']}/{direct['intersect']}")

    # min_amount 走亿元：传 1 应等价于 service 的 1e8
    r2 = client.get("/api/intersect", params={
        "industries": ",".join(inds), "concepts": ",".join(cpts),
        "boards": "主板,创业板,科创板,北交所", "exclude_st": "false",
        "min_amount": "1",
    })
    d2 = r2.json()
    direct2 = svc.intersect({
        "industries": inds, "concepts": cpts, "boards": ALL_BOARDS,
        "exclude_st": False, "min_amount": 1e8,
    })
    check("min_amount 按亿元换算（1 亿 == 1e8 元）",
          bool(d2.get("ok")) and d2["intersect"] == direct2["intersect"],
          f"接口 {d2.get('intersect')} vs service {direct2['intersect']}")
    check("min_amount 生效后明细都 ≥ 1 亿",
          all(x["amount_yi"] >= 0.999 for x in d2["rows"]),
          f"最小 {min([x['amount_yi'] for x in d2['rows']] or [0])}")

    # 未知行业经接口应报 200 + ok=false（业务错误，不是 500）
    r3 = client.get("/api/intersect", params={"industries": "不存在的行业"})
    check("未知行业经接口返回 ok=false 而非 500",
          r3.status_code == 200 and r3.json().get("ok") is False,
          f"{r3.status_code} {r3.json().get('reason')}")

    # 裸字符串被逐字拆开的老坑：必须整体当成一个名字
    r4 = client.get("/api/intersect", params={
        "industries": inds[0], "concepts": cpts[0],
        "boards": "主板,创业板,科创板,北交所",
    })
    d4 = r4.json()
    check("单个行业名不被逐字拆开",
          bool(d4.get("ok")) and d4.get("industries") == [inds[0]],
          str(d4.get("industries")))

    # 空串参数 ≠ 未传参数：`?boards=` 必须报错，不能回退成默认板块。
    # 这是接口层独有的坑（service 直调不会遇到），真踩过一次。
    r5 = client.get("/api/intersect", params={"industries": inds[0], "boards": ""})
    d5 = r5.json()
    check("空 boards（?boards=）经接口报错，不回退默认",
          r5.status_code == 200 and d5.get("ok") is False and "板块" in (d5.get("reason") or ""),
          f"{d5.get('ok')} {d5.get('reason')}")
    r6 = client.get("/api/intersect", params={
        "industries": inds[0], "concepts": cpts[0], "markets": ""})
    d6 = r6.json()
    check("空 markets（?markets=）经接口报错，不变成全市场",
          r6.status_code == 200 and d6.get("ok") is False
          and "交易所" in (d6.get("reason") or ""),
          f"{d6.get('ok')} {d6.get('reason')}")
    # 未传这两个参数时应照旧走默认（主板 / 全市场），不能因为上面的改动变得必填
    r7 = client.get("/api/intersect", params={
        "industries": inds[0], "concepts": cpts[0]})
    d7 = r7.json()
    check("不传 boards / markets 时照旧可用（走配置默认）",
          bool(d7.get("ok")) and d7.get("boards") and d7.get("markets")
          and d7.get("set_a", 0) > 0,
          f"板块={d7.get('boards')} 交易所={d7.get('markets')} "
          f"A={d7.get('set_a')} B={d7.get('set_b')} I={d7.get('intersect')}")


def main() -> int:
    t0 = time.perf_counter()
    print("=" * 74)
    print("板块交集（二级行业 ∩ 概念）专项测试")
    print("=" * 74)
    svc = build_service()
    print(f"service 就绪 {time.perf_counter() - t0:.2f}s · 最新交易日 {svc.latest_date()}"
          f" · 行情 {len(svc.bars()):,} 行 · 二级行业 "
          f"{len(svc.industries.l2_names())} 个 · 概念 {svc.concepts.concept_count} 个")

    section_basic(svc)
    section_crosscheck(svc)
    section_semantics(svc)
    section_filters(svc)
    section_empty(svc)
    section_edges(svc)
    section_api()

    print("\n" + "=" * 74)
    print(f"结果：{OK} 通过 / {BAD} 失败（耗时 {time.perf_counter() - t0:.1f}s）")
    print("=" * 74)
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
