# -*- coding: utf-8 -*-
"""源码级回归：板块看盘（行业 / 地区 / 概念 / 风格）的数据层。

用法（源码态，不需要起服务）::

    python packaging/board_test.py

覆盖本模块几处**容易悄悄坏掉**的地方：

1. 索引解析与分类计数（`tdxzs.cfg` 604 条的落盘结果）
2. `tdxzs.cfg` 第 6 列「显示名」是代码/序号时必须回退成全名
   （行业放 T 码、地区放序号 —— 曾让界面出现叫「T030203」「1」的板块）
3. 行业按 **T 码前缀**聚合（等值匹配会让一级/二级行业板块一个成员都没有）
4. 板块指数尾部读取的缓存键必须带**已读根数**
   （曾只按 mtime 命中：列表要 22 根、K 线图要 160 根，后者拿到 22 根只剩一小截）
5. 排序时**缺值必须沉底**（不能随 asc/desc 一起被翻到最前）
6. `summary` 不受 `limit` 截断影响，且分项求和守恒
7. 板块统计的全市场涨跌停口径与**连板梯队**逐条一致
8. 板块级统计与成分股明细自洽（涨跌家数 / 涨停数 / 停牌数）

断言全通过时退出码 0，否则 1。**不写死会随时间变化的期望值**（如某日涨停家数），
只写结构性不变量与交叉核对。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_picker.app import service  # noqa: E402
from stock_picker.boards import parse_tdx_name_table  # noqa: E402
from stock_picker.industry import parse_hy_cfg  # noqa: E402

FAILD = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"   {detail}" if detail else ""))
    if not cond:
        FAILD.append(name)


if not getattr(service, "ready", False):
    print("行情缓存未就绪，先刷新…")
    service.refresh()

BI = service.boards
# 用模块自己的数据源解析器，别在测试里重写一遍路径规则
# （incon.dat 在 TDX 根目录、不在 hq_cache 下 —— 写死会随目录结构变动而失效）
SRC = BI._sources()
print("数据源：" + " · ".join(f"{k}={'有' if v else '缺'}" for k, v in SRC.items()))
print("=" * 78)
print("一、索引解析与分类计数")
print("=" * 78)
info = BI.info()
print(f"  板块 {info['boards']} 个 · 有成分 {info['boards_with_members']} 个 · "
      f"成分归属 {info['members_total']} 条 · 去重成分股 {info['member_codes']} 只")
check("解析版本与缓存一致", info.get("parse_version") is not None)
check("全部板块都有 880xxx 指数代码",
      all(r["index_code"].startswith("880") for r in BI.catalog()))
check("分类计数之和 == 板块总数",
      sum(info["by_category"].values()) == info["boards"],
      f"{sum(info['by_category'].values())} vs {info['boards']}")
check("每类「有成分」数 <= 该类总数",
      all(v <= info["by_category"][k] for k, v in info["by_category_with_members"].items()))
check("地区板块本地无成分（预期全部为 0）",
      all(not BI.members_of(r["index_code"]) for r in BI.catalog("地区")))
check("members_of 返回的代码已归一化（形如 600000.SH）",
      all("." in c and len(c.split(".")[0]) == 6
          for r in BI.catalog("行业")[:20] for c in BI.members_of(r["index_code"])[:3]))

# 「板块看盘」主视图只暴露概念板块与行业板块两类（用户要求，地区无成分、风格是因子指标）
cats = {c["value"]: c for c in BI.categories()}
prim = [c["value"] for c in BI.categories() if c.get("primary")]
check("categories() 覆盖四大类 + 全部",
      {"行业", "地区", "概念", "风格", "全部"} <= set(cats),
      " / ".join(cats))
check("主视图类别正好是概念 + 行业（顺序即界面顺序）", prim == ["概念", "行业"], " / ".join(prim))
check("类别 label 是界面全称",
      [cats[k]["label"] for k in ("概念", "行业", "地区", "风格")]
      == ["概念板块", "行业板块", "地区板块", "风格板块"])
check("主视图两类的 n_members 都 > 0（下拉里的数字不能是 0）",
      all(cats[k]["n_members"] > 0 for k in ("概念", "行业")),
      " / ".join(f"{k}={cats[k]['n_members']}" for k in ("概念", "行业")))
check("非主视图类别未被下架（API 仍可传 category=地区/风格）",
      cats["地区"]["n"] > 0 and cats["风格"]["n"] > 0)
check("n_members <= n",
      all(c["n_members"] <= c["n"] for c in BI.categories() if "n_members" in c))
check("概念 / 行业 都能列出板块（非空且 ok）",
      all(service.board_panel({"category": k, "limit": 0}).get("total", 0) > 0
          for k in ("概念", "行业")))

print()
print("=" * 78)
print("二、显示名（tdxzs.cfg 第 6 列）不得是代码或序号")
print("=" * 78)
import re  # noqa: E402
CODE_LIKE = re.compile(r"^(?:[Tt]\d+|\d+)$")
allrows = BI.catalog()
bad = [r for r in allrows if not r["show_name"] or CODE_LIKE.match(r["show_name"])]
for r in bad[:5]:
    print(f"    漏网：{r['index_code']} {r['category']} name={r['name']} show={r['show_name']}")
check("没有任何板块的名字是 T 码 / 纯序号 / 空", not bad, f"{len(bad)} 个")
diff = [r for r in allrows if r["show_name"] != r["name"]]
print(f"  显示名与短名不同的板块 {len(diff)} 个（应为概念/风格的全称，需保留）")
check("显示名不同的板块只出现在概念 / 风格里",
      all(r["category"] in ("概念", "风格") for r in diff),
      f"越界类别：{sorted({r['category'] for r in diff if r['category'] not in ('概念', '风格')})}")
# 反证：确实存在被回退的板块（否则这条断言是空转的）
t_like = sum(1 for ln in SRC["zs"].read_text(encoding="gbk", errors="replace").splitlines()
             if len(ln.split("|")) >= 6 and CODE_LIKE.match(ln.split("|")[5]))
check("源文件里确实有需要回退的脏显示名（非空转）", t_like > 0, f"{t_like} 行")

print()
print("=" * 78)
print("三、行业板块按 T 码前缀聚合")
print("=" * 78)
cache = Path(service.reader.tdx_dir) / "T0002" / "hq_cache"
t_names = parse_tdx_name_table(SRC["incon"])                  # {T码: 名称}
hy = parse_hy_cfg(SRC["hy"])                                  # {代码: {"t":..,"x":..}}
t_codes = sorted((v["t"], c) for c, v in hy.items() if v.get("t"))
name2t = {n: t for t, n in t_names.items()}
print(f"  T 码名称 {len(t_names)} 条 · 有 T 码归属的股票 {len(t_codes)} 只")

mismatch, checked, empty_ok = [], 0, []
for r in BI.catalog("行业"):
    tc = name2t.get(r["name"])
    if not tc:
        continue
    checked += 1
    expect = {c for t, c in t_codes if t.startswith(tc)}
    got = set(BI.members_of(r["index_code"]))
    if expect != got:
        mismatch.append((r["index_code"], r["name"], tc, len(expect), len(got)))
    else:
        empty_ok.append((r["name"], tc, len(got)))
print(f"  可核对行业板块 {checked} 个，全部一致 {checked - len(mismatch)} 个")
for m in mismatch[:5]:
    print("    不一致：", m)
check("每个行业板块的成分 == 「T 码以该板块 T 码开头」的股票集合", not mismatch)
# T 码是层级码：T01(一级,3) / T0101(二级,5) / T010101(三级,7)。
# 每只股票的归属码实测都是**三级**码，所以二级板块必须靠前缀聚合才有成分。
lvl1 = [x for x in empty_ok if len(x[1]) == 3]
lvl2 = [x for x in empty_ok if len(x[1]) == 5]
lvl3 = [x for x in empty_ok if len(x[1]) == 7]
print(f"  可核对行业板块按层级：一级 {len(lvl1)} / 二级 {len(lvl2)} / 三级 {len(lvl3)}")
# 一级 T 码（T01…T13）在 tdxzs.cfg 里叫「TDX 交运」这类别名，与 incon.dat 名称对不上，
# 因此**本来就没有**一级行业板块进入匹配集 —— 它们是 verify.industry_unmatched 那 13 个。
unmatched = set(info["verify"].get("industry_unmatched") or [])
check("一级行业板块确实不参与匹配（那 13 个 TDX 别名）",
      not lvl1 and len(unmatched) == 13, f"unmatched={len(unmatched)}")
check("二级行业板块有成分（前缀聚合的关键受益者）",
      bool(lvl2) and all(n > 0 for _, _, n in lvl2), f"{len(lvl2)} 个")
check("三级行业板块有成分", bool(lvl3) and all(n > 0 for _, _, n in lvl3), f"{len(lvl3)} 个")
# 前缀语义的直接验证：三级行业的成分必须是其二级行业成分的**真子集**
catalog_hy = {r["name"]: r["index_code"] for r in BI.catalog("行业")}
child_ok, pairs = True, 0
for n, t, _ in lvl3:
    parent_t = t[:5]
    parent = next((pn for pn, pt, _ in lvl2 if pt == parent_t), None)
    if parent is None or n not in catalog_hy or parent not in catalog_hy:
        continue
    pairs += 1
    child = set(BI.members_of(catalog_hy[n]))
    par = set(BI.members_of(catalog_hy[parent]))
    if not child <= par:
        child_ok = False
        print(f"    子集关系破裂：{n}({t}) ⊄ {parent}({parent_t})")
check("三级行业成分是其二级行业成分的子集", child_ok and pairs > 0, f"核对 {pairs} 对")
# 再抽一对人工可读的例子
ex = next(((n, t, pn) for n, t, _ in lvl3
           for pn, pt, _ in lvl2 if pt == t[:5] and n in catalog_hy and pn in catalog_hy), None)
if ex:
    print(f"  例：{ex[0]}({ex[1]}) ⊂ {ex[2]} —— {len(BI.members_of(catalog_hy[ex[0]]))} "
          f"⊂ {len(BI.members_of(catalog_hy[ex[2]]))} 只")
big = max(empty_ok, key=lambda x: x[2])
check("最大的行业板块成分数 > 0", big[2] > 0, f"{big[0]} {big[2]} 只")

print()
print("=" * 78)
print("四、板块指数尾部读取：缓存键必须带已读根数")
print("=" * 78)
code = next((r["index_code"] for r in BI.catalog("行业") if BI.members_of(r["index_code"])), None)
check("找到可用于测试的板块", bool(code), str(code))
short = service._index_bars(code, 22)
long_ = service._index_bars(code, 160)
n_short = 0 if short is None else len(short)
n_long = 0 if long_ is None else len(long_)
print(f"  先说 22 根 → {n_short} 行；再要 160 根 → {n_long} 行")
check("先读短再读长：长请求必须拿到 >= 160 行（缓存键带根数）", n_long >= 160, f"{n_long}")
again_short = service._index_bars(code, 22)
check("再读短：不得把长缓存误当短（允许命中长的那份）",
      again_short is not None and len(again_short) >= 22, f"{len(again_short)}")
# 反向顺序（先长后短）也要正确
k = service.index_kline(code, bars=120)
check("index_kline 拿到足够根数", len(k.get("ohlc") or []) >= 120, f"{len(k.get('ohlc') or [])}")

print()
print("=" * 78)
print("五、排序：缺值必须沉底（不随 asc/desc 翻转）")
print("=" * 78)
for order in ("desc", "asc"):
    p = service.board_panel({"sort": "pct", "order": order, "limit": 0})
    vals = [r["pct"] for r in p["rows"]]
    none_at = [i for i, v in enumerate(vals) if v is None]
    nones_last = (not none_at) or (min(none_at) >= len(vals) - len(none_at))
    print(f"  order={order}: {len(vals)} 行，缺值 {len(none_at)} 个，首个缺值位置 "
          f"{none_at[0] if none_at else '-'}")
    check(f"order={order} 时缺值全部在末尾", nones_last)
    nums = [v for v in vals if v is not None]
    check(f"order={order} 时有值部分单调",
          all(nums[i] >= nums[i + 1] for i in range(len(nums) - 1)) if order == "desc"
          else all(nums[i] <= nums[i + 1] for i in range(len(nums) - 1)))

print()
print("=" * 78)
print("六、summary 不受 limit 影响，且分项守恒")
print("=" * 78)
full = service.board_panel({"limit": 0})
cut = service.board_panel({"limit": 5})
print(f"  limit=0 返回 {len(full['rows'])} 行；limit=5 返回 {len(cut['rows'])} 行")
check("limit=5 只影响行数", len(cut["rows"]) == 5)
check("limit 不同但 summary 完全一致", full["summary"] == cut["summary"],
      f"{cut['summary']} vs {full['summary']}")
s = full["summary"]
tot = s["boards_up"] + s["boards_down"] + s["boards_flat"] + s["boards_no_quote"]
check("涨+跌+平+无行情 == 板块总数（limit=0 全部类别）", tot == full["total"],
      f"{tot} vs {full['total']}")
check("total >= 返回行数", full["total"] >= len(full["rows"]))
withm = service.board_panel({"limit": 0, "with_members_only": True})
sw = withm["summary"]
totw = sw["boards_up"] + sw["boards_down"] + sw["boards_flat"] + sw["boards_no_quote"]
check("仅有成分时的分项和 == 该口径下的 total", totw == withm["total"],
      f"{totw} vs {withm['total']}")
check("仅有成分的 total <= 全部板块数", withm["total"] <= full["total"])
check("仅有成分时每一行都确实有成分",
      all(r["has_members"] for r in withm["rows"]))
kw = service.board_panel({"limit": 0, "keyword": "煤"})
check("关键字过滤后每行名称或代码命中关键字",
      all(("煤" in r["name"]) or ("煤" in r["index_code"]) for r in kw["rows"]))
check("关键字过滤缩小了结果集", kw["total"] <= full["total"])

print()
print("=" * 78)
print("七、与连板梯队交叉核对（全市场涨跌停口径）")
print("=" * 78)
st = service.streaks({})
m = st["metrics"]
print(f"  连板梯队：涨停 {m['limit_up']} / 跌停 {m['limit_down']}")
print(f"  板块看盘：涨停 {s['limit_up_stocks']} / 跌停 {s['limit_down_stocks']} "
      f"（去重口径）")
check("全市场涨停数与连板梯队一致", s["limit_up_stocks"] == m["limit_up"],
      f"{s['limit_up_stocks']} vs {m['limit_up']}")
check("全市场跌停数与连板梯队一致", s["limit_down_stocks"] == m["limit_down"],
      f"{s['limit_down_stocks']} vs {m['limit_down']}")
check("板块看盘与连板梯队锚在同一天", full["date"] == st.get("date"),
      f"{full['date']} vs {st.get('date')}")

print()
print("=" * 78)
print("八、板块级统计与成分股明细自洽")
print("=" * 78)
sample = [r for r in service.board_panel({"limit": 0})["rows"] if r["n_members"]][:12]
print(f"  抽查 {len(sample)} 个板块")
for r in sample:
    mem = service.board_members({"board": r["index_code"], "limit": 0})
    ups = sum(1 for x in mem["rows"] if (x.get("pct") or 0) > 0)
    lus = sum(1 for x in mem["rows"] if x.get("limit") == "涨停")
    check(f"{r['index_code']} {r['name']} 涨跌家数与明细一致",
          ups == r["n_up"] and len(mem["rows"]) == r["n_quoted"],
          f"明细 {ups}/{len(mem['rows'])} vs 汇总 {r['n_up']}/{r['n_quoted']}")
    check(f"{r['index_code']} {r['name']} 涨停数与明细一致",
          lus == r["n_limit_up"], f"明细 {lus} vs 汇总 {r['n_limit_up']}")
    check(f"{r['index_code']} {r['name']} n_members >= n_quoted",
          r["n_members"] >= r["n_quoted"])
    check(f"{r['index_code']} {r['name']} 明细全为归一化代码",
          all("." in x["code"] for x in mem["rows"]))
    check(f"{r['index_code']} {r['name']} 停牌数 == 成分数 - 有行情数",
          mem["n_suspended"] == mem["n_total"] - mem["n_quoted"],
          f"{mem['n_suspended']} vs {mem['n_total']}-{mem['n_quoted']}")

print()
print("=" * 78)
print("九、板块指数 K 线载荷（与个股共用 _kline_payload）")
print("=" * 78)
k = service.index_kline(code, bars=160)
n_ohlc = len(k.get("ohlc") or [])
print(f"  {k.get('code')} {k.get('name')}：{k.get('bars')} 根 · "
      f"最新 {k.get('latest', {}).get('date')} 收 {k.get('latest', {}).get('close')}")
check("载荷 ok=True", k.get("ok") is True, str(k.get("reason")))
check("含 code/name/category/is_index/n_members",
      {"code", "name", "category", "is_index", "n_members"} <= set(k))
check("bars 与实际根数一致", k.get("bars") == n_ohlc, f"{k.get('bars')} vs {n_ohlc}")
check("dates / ohlc / volume 三者等长",
      len(k["dates"]) == n_ohlc == len(k["volume"]), f"{len(k['dates'])}/{n_ohlc}/{len(k['volume'])}")
check("macd(dif/dea/macd) 与 ohlc 等长",
      all(len(k["macd"][x]) == n_ohlc for x in ("dif", "dea", "macd")),
      str({x: len(k["macd"][x]) for x in ("dif", "dea", "macd")}))
check("ma5/10/20/60 都与 ohlc 等长",
      all(len(k["ma"][m]) == n_ohlc for m in ("ma5", "ma10", "ma20", "ma60")))
# ohlc 的字段顺序是 [open, close, low, high]
check("ohlc 满足 high >= max(o,c) 且 low <= min(o,c)",
      all(bar[3] >= max(bar[0], bar[1]) - 1e-6 and bar[2] <= min(bar[0], bar[1]) + 1e-6
          for bar in k["ohlc"]))
check("dates 升序且无重复",
      all(k["dates"][i] < k["dates"][i + 1] for i in range(len(k["dates"]) - 1)))
check("latest.close == 最后一根收盘", k["latest"]["close"] == k["ohlc"][-1][1])
check("latest.date == 最后一个日期", k["latest"]["date"] == k["dates"][-1])
# MACD 柱 = 2 × (dif - dea)（通达信口径）
bad_bar = [
    i for i in range(n_ohlc)
    if k["macd"]["macd"][i] is not None and k["macd"]["dif"][i] is not None
    and abs(k["macd"]["macd"][i] - 2 * (k["macd"]["dif"][i] - k["macd"]["dea"][i])) > 2e-3
]
check("MACD 柱 == 2 × (DIF - DEA)（通达信口径）", not bad_bar, f"越界 {bad_bar[:5]}")
# 板块指数末根涨幅应与板块列表里的 pct 一致（同一锚点日）
panel = service.board_panel({"limit": 0})
panel_row = next(r for r in panel["rows"] if r["index_code"] == code)
check("K 线末根 pct 与板块列表 pct 一致",
      abs((k["pct"][-1] or 0) - panel_row["pct"]) <= 1e-4,
      f"{k['pct'][-1]} vs {panel_row['pct']}")
check("K 线末根收盘与板块列表 close 一致",
      abs(k["ohlc"][-1][1] - panel_row["close"]) <= 1e-3,
      f"{k['ohlc'][-1][1]} vs {panel_row['close']}")
check("K 线末根日期 == 板块列表锚点日",
      k["latest"]["date"] == panel["date"],
      f"{k['latest']['date']} vs {panel['date']}")
check("该板块非 stale（stale_date 为空）", panel_row.get("stale_date") is None,
      str(panel_row.get("stale_date")))

print()
print("=" * 78)
if FAILD:
    print(f"失败 {len(FAILD)} 项：")
    for n in FAILD:
        print("  -", n)
    sys.exit(1)
print("全部断言通过 ✅")
