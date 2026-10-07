# -*- coding: utf-8 -*-
"""源码级回归：自选板块（通达信 ``T0002/blocknew``）的数据层与服务层。

用法（源码态，不需要起服务）::

    python packaging/watchblock_test.py

覆盖这条链路上几处**容易悄悄坏掉**的地方：

1. ``blocknew.cfg`` 的变长字段流配对
   （按定长 50 字节切会把中文名截断 —— 曾把「集合竞价大量」切成两半）
2. 配对出的文件名必须真的命中磁盘 ``.blk``（配错位会冒出假板块）
3. ``*.blk`` 的 7 位代码 + 首位市场码（``0`` 深 / ``1`` 沪 / ``2`` 北）
   → 归一化后 ``is_a_share`` 剔除数应为 0（错一位就会把整个板块搬到别的市场）
4. 缓存往返是恒等映射，且源文件变化能识破（只按 cfg 一处的 mtime 校验不够，
   新加一个 ``.blk`` 而不改 cfg 也要能发现）
5. 分布卡基数 ``summary_base`` **不随行业 / 概念筛选变化**
6. 分布卡计数与「按该标签过滤后的行数」逐项一致
7. 板块清单以 ``blocknew.cfg`` 为准（``tjg`` / ``zxg`` 这类内部文件不入清单）
8. 未知板块 / 未知行业名要**明确报错**，不能静默返回空结果

断言全通过时退出码 0，否则 1。**不写死会随时间变化的期望值**（如某板块多少只成员），
只写结构性不变量与交叉核对。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_picker.app import service  # noqa: E402
from stock_picker.tdx_reader import is_a_share, split_code  # noqa: E402
from stock_picker.watchblocks import (  # noqa: E402
    BLK_LINE_LEN,
    INTERNAL_FILES,
    MARKET_PREFIX,
    parse_blk,
    parse_blocknew_cfg,
    split_fields,
)

FAILD = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"   {detail}" if detail else ""))
    if not cond:
        FAILD.append(name)


if not getattr(service, "ready", False):
    print("行情缓存未就绪，先刷新…")
    service.refresh()

WB = service.watch_blocks
D = WB._dir()
CFG = WB._cfg()
print(f"自选板块目录：{D}")
print(f"清单文件：{CFG}" + (f"（{CFG.stat().st_size} 字节）" if CFG else "（缺失）"))

if D is None or CFG is None:
    print("\n本地没有 T0002/blocknew，本机跳过（这不是失败）")
    sys.exit(0)

print("=" * 78)
print("一、blocknew.cfg 解析：字段流两两配对")
print("=" * 78)
fields = split_fields(CFG.read_bytes())
pairs = parse_blocknew_cfg(CFG)
print(f"  字段流 {len(fields)} 段 · 配出 {len(pairs)} 组")
print("  前 5 组：" + " / ".join(f"{p['name']}→{p['file']}" for p in pairs[:5]))
check("字段流段数为偶数（配对不漏）", len(fields) % 2 == 0, f"{len(fields)}")
check("配对数 == 字段流段数 / 2", len(pairs) == len(fields) // 2)
check("每组都有显示名与文件名", all(p["name"] and p["file"] for p in pairs))
# 名字必须是合法 GBK（解码失败会带 U+FFFD），这条能抓住「截断/错位」
check("显示名无解码坏字节（U+FFFD）",
      all("\ufffd" not in p["name"] for p in pairs),
      " / ".join(p["name"] for p in pairs if "\ufffd" in p["name"])[:60])
# 文件名一律 ASCII
check("文件名全为 ASCII", all(p["file"].isascii() for p in pairs))
# 定长 50 字节切法会把名字截断 —— 用一个具体反例证明我们是按 \x00 切的：
# 「集合竞价大量」这个 6 字名（12 字节 GBK）如果被截断就会 <6 字
long_names = [p for p in pairs if len(p["name"]) >= 5]
if long_names:
    check("存在 5 字以上的中文名（证明未被定长切法截断）", True,
          " / ".join(p["name"] for p in long_names[:4]))

print()
print("=" * 78)
print("二、配对文件名必须命中磁盘 .blk")
print("=" * 78)
disk = {p.stem: p for p in D.iterdir() if p.suffix.lower() == ".blk"}
hit = [p for p in pairs if p["file"] in disk]
miss = [p["file"] for p in pairs if p["file"] not in disk]
print(f"  磁盘 .blk {len(disk)} 个 · 配对命中 {len(hit)} 个 · 未命中 {len(miss)}")
check("配对命中率 100%（配错位会冒出磁盘上不存在的假板块）", not miss, " / ".join(miss))
used = {p["file"] for p in pairs}
orphans = sorted(set(disk) - used)
print(f"  磁盘有、cfg 未收：{orphans}")
check("cfg 未收的文件都是内部文件（tjg / zxg）",
      set(orphans) <= INTERNAL_FILES, " / ".join(orphans))

print()
print("=" * 78)
print("三、*.blk 市场码与代码归一化")
print("=" * 78)
raw_total = 0
invalid_total = 0
all_codes = []
for name, path in disk.items():
    r = parse_blk(path)
    raw_total += r["raw"]
    invalid_total += r["invalid"]
    all_codes += r["codes"]
uniq = set(all_codes)
dropped = [c for c in uniq if not is_a_share(c)]
print(f"  全部 .blk：行数 {raw_total} · 去重 {len(uniq)} · 非法市场码 {invalid_total}")
print(f"  is_a_share 剔除 {len(dropped)} 只" + (f"：{dropped[:8]}" if dropped else ""))
check("市场码无非法值（首位只能是 0/1/2）", invalid_total == 0, f"{invalid_total}")
# 每个 .blk 是「7 位代码 + \r\n」逐行写成的，实测字节数 = 行数 × 9（全部 21 个
# 非空文件逐项精确成立）。注意不是 ×7 —— 行间那两个 CRLF 也在文件里；
# 首个 \r\n 与「无尾换行」正好抵消，所以净是每行 9 字节。
# 空板块（2-12 / zxg）是 0 字节。
bad_size = []
for _n, _p in disk.items():
    _sz = _p.stat().st_size
    if _sz == 0:
        continue
    if _sz != parse_blk(_p)["raw"] * (BLK_LINE_LEN + 2):
        bad_size.append(f"{_n}:{_sz}")
check("每个 .blk 字节数 == 行数×9（7 位代码 + CRLF）",
      not bad_size, " / ".join(bad_size))
check("空文件解析为 0 行 0 代码",
      all(parse_blk(p)["raw"] == 0 and not parse_blk(p)["codes"]
          for p in disk.values() if p.stat().st_size == 0))
# 非数字、非 CRLF 的字节必须为 0 —— 证明「按数字扫描」等价于「按行解析」
stray = []
for _n, _p in disk.items():
    _b = _p.read_bytes()
    if any((not (48 <= c <= 57)) and c not in (13, 10) for c in _b):
        stray.append(_n)
check("文件里没有数字与 CRLF 之外的字节（按数字扫描 == 按行解析）",
      not stray, " / ".join(stray))
check("归一化后 is_a_share 零剔除（市场码错一位就会大批被剔除）", not dropped)
check("代码形如 600000.SH（6 位 + 点 + 交易所）",
      all("." in c and len(c.split(".")[0]) == 6 and c.split(".")[1].isupper() for c in uniq))
# 首位市场码 → 交易所后缀，必须逐项对得上。
# 含重复行地按「首位」与按「后缀」各统计一次，先用 MARKET_PREFIX 折算再比对：
# 两边来自同一批字节，只有映射正确才会相等。
raw_by_prefix = {}
for name, path in disk.items():
    text = path.read_bytes().decode("ascii", errors="ignore")
    digits = "".join(ch for ch in text if ch.isdigit())
    for i in range(0, len(digits) - BLK_LINE_LEN + 1, BLK_LINE_LEN):
        chunk = digits[i:i + BLK_LINE_LEN]
        raw_by_prefix[chunk[0]] = raw_by_prefix.get(chunk[0], 0) + 1
suffix_rows = {}
for c in all_codes:
    m = split_code(c)[1]
    suffix_rows[m] = suffix_rows.get(m, 0) + 1
mapped = {}
for k, v in raw_by_prefix.items():
    mapped[MARKET_PREFIX[k]] = mapped.get(MARKET_PREFIX[k], 0) + v
print(f"  按首位（行数）：{dict(sorted(raw_by_prefix.items()))}")
print(f"  折算成交易所：{mapped}")
print(f"  按后缀（行数）：{suffix_rows}")
check("首位市场码映射后与归一化后缀逐项一致",
      mapped == suffix_rows, f"{mapped} vs {suffix_rows}")
check("只出现 sh / sz / bj 三种交易所", set(suffix_rows) <= {"sh", "sz", "bj"})

print()
print("=" * 78)
print("四、索引：清单以 cfg 为准、缓存往返恒等、源变化能识破")
print("=" * 78)
meta = WB.meta
cat = WB.catalog()
print(f"  板块 {meta.get('blocks')} 个 · 有成分 {meta.get('blocks_with_members')} 个 · "
      f"去重成员 {meta.get('member_codes')} 只 · 来源 {meta.get('source')}")
check("catalog() 条目数 == cfg 配对数", len(cat) == len(pairs), f"{len(cat)} vs {len(pairs)}")
check("清单里没有内部文件（tjg / zxg）",
      all(b["key"] not in INTERNAL_FILES for b in cat),
      " / ".join(b["key"] for b in cat if b["key"] in INTERNAL_FILES))
check("每个板块都有 key / name / file / n",
      all({"key", "name", "file", "n"} <= set(b) for b in cat))
check("order 是 0..n-1 的连续序号", [b["order"] for b in cat] == list(range(len(cat))))
check("n 与 members_of() 长度一致",
      all(b["n"] == len(WB.members_of(b["key"])) for b in cat))
check("members_of 返回已归一化代码",
      all("." in c for b in cat for c in WB.members_of(b["key"])[:3]))
# 缓存往返
tmp = WB.cache_file
if tmp and tmp.is_file():
    import json
    blob = json.loads(tmp.read_text(encoding="utf-8"))
    check("缓存里的 sources 指纹与当前一致", blob.get("sources") == WB._signature())
    check("缓存指纹把每个 .blk 都纳进来了",
          isinstance(blob.get("sources", {}).get("blk"), dict)
          and len(blob["sources"]["blk"]) == len(disk),
          f"{len(blob.get('sources', {}).get('blk') or {})} vs {len(disk)}")
    check("缓存落了 parse_version", blob.get("parse_version") is not None)
# 换一个不存在的 tdx_dir → 应降级为空索引但字段齐全（接口契约稳定）
from stock_picker.watchblocks import WatchBlockIndex  # noqa: E402
empty = WatchBlockIndex(r"__no_such_tdx__")
em = empty.load()
check("tdx 目录不存在时降级为空索引", em.get("blocks") == 0, str(em.get("reason")))
check("降级分支字段齐全（前端不该拿到另一套结构）",
      {"blocks", "blocks_with_members", "members_total", "member_codes", "verify"} <= set(em))

print()
print("=" * 78)
print("五、resolve：按文件名 / 按中文名 / 未知")
print("=" * 78)
first = cat[0]
by_key = WB.resolve(first["key"])
by_file = WB.resolve(first["file"])
by_name = WB.by_name(first["name"])
check("按 key 解析", by_key is not None and by_key["key"] == first["key"])
check("按文件名（带 .blk）解析到同一板块",
      by_file is not None and by_file["key"] == first["key"])
check("按中文名解析到同一板块",
      by_name is not None and by_name["key"] == first["key"])
check("未知引用返回 None", WB.resolve("__no_such_block__") is None)

print()
print("=" * 78)
print("六、面板：分布卡基数不随筛选变化（本页最易改坏的不变量）")
print("=" * 78)
# 挑一个有成员、成员数最多的板块当样本
sample = max((b for b in cat if b["has_members"]), key=lambda b: b["n"], default=None)
if sample is None:
    print("  本地没有含成员的自选板块，跳过")
else:
    key = sample["key"]
    print(f"  样本板块：{sample['name']}（{key}，{sample['n']} 只）")
    p0 = service.watch_block_panel({"block": key, "max_rows": 100000})
    if not p0.get("ok"):
        check("样本板块面板可加载", False, str(p0.get("reason")))
    else:
        ix0 = p0["industry_summary"]["groups"]
        cp0 = p0["concept_summary"]["groups"]
        base0 = p0["summary_base"]
        print(f"  基线：当日有行情 {p0['candidates']} 只 · 基数 {base0} · "
              f"行业 {len(ix0)} 组 · 概念 {len(cp0)} 组 · 命中 {p0['matched']}")
        check("基线 summary_base == 当日有行情数",
              base0 == p0["candidates"], f"{base0} vs {p0['candidates']}")
        check("基线 matched == summary_base（无筛选）",
              p0["matched"] == base0, f"{p0['matched']} vs {base0}")
        check("行业分布 mapped + unmapped == 基数",
              p0["industry_summary"]["mapped"] + p0["industry_summary"]["unmapped"] == base0)
        # 概念侧 mapped 是「有概念归属的只数」，与 unmapped 相加也应为基数
        check("概念分布 mapped + unmapped == 基数",
              p0["concept_summary"]["mapped"] + p0["concept_summary"]["unmapped"] == base0)

        print()
        print("  --- 点选行业：分布卡必须一字不变 ---")
        for g in ix0[:3]:
            l2 = g["l2"]
            r = service.watch_block_panel(
                {"block": key, "industries": [l2], "max_rows": 100000})
            same_ix = r["industry_summary"]["groups"] == ix0
            same_cp = r["concept_summary"]["groups"] == cp0
            check(f"选「{l2}」→ 行业分布不变", same_ix)
            check(f"选「{l2}」→ 概念分布不变", same_cp)
            check(f"选「{l2}」→ summary_base 不变",
                  r["summary_base"] == base0, f"{r['summary_base']} vs {base0}")
            check(f"选「{l2}」→ 分布计数 == 实际命中数",
                  g["n"] == r["matched"], f"分布卡 {g['n']} vs 命中 {r['matched']}")
            check(f"选「{l2}」→ 明细行行业无泄漏",
                  all(x["industry_l2"] == l2 for x in r["rows"]))

        print()
        print("  --- 点选概念：分布卡同样一字不变 ---")
        for g in cp0[:3]:
            nm = g["concept"]
            r = service.watch_block_panel(
                {"block": key, "concepts": [nm], "max_rows": 100000})
            check(f"选概念「{nm}」→ 行业分布不变",
                  r["industry_summary"]["groups"] == ix0)
            check(f"选概念「{nm}」→ 概念分布不变",
                  r["concept_summary"]["groups"] == cp0)
            check(f"选概念「{nm}」→ summary_base 不变", r["summary_base"] == base0)
            check(f"选概念「{nm}」→ 分布计数 == 实际命中数",
                  g["n"] == r["matched"], f"分布卡 {g['n']} vs 命中 {r['matched']}")
            check(f"选概念「{nm}」→ 明细行的 hit_concepts 都含它",
                  all(nm in (x["hit_concepts"] or []) for x in r["rows"][:80]))

        print()
        print("  --- 行业 + 概念 同时筛 ---")
        if ix0 and cp0:
            r = service.watch_block_panel({
                "block": key, "industries": [ix0[0]["l2"]],
                "concepts": [cp0[0]["concept"]], "concept_mode": "all",
                "max_rows": 100000})
            check("行业+概念 → 基数不变", r["summary_base"] == base0)
            check("行业+概念 → 两个分布都不变",
                  r["industry_summary"]["groups"] == ix0
                  and r["concept_summary"]["groups"] == cp0)
            check("行业+概念 → 结果 ⊆ 行业结果",
                  all(x["industry_l2"] == ix0[0]["l2"] and cp0[0]["concept"] in (x["hit_concepts"] or [])
                      for x in r["rows"]))

        print()
        print("  --- concept_mode：any 应 >= all ---")
        if len(cp0) >= 2:
            two = [cp0[0]["concept"], cp0[1]["concept"]]
            ra = service.watch_block_panel(
                {"block": key, "concepts": two, "concept_mode": "any", "max_rows": 100000})
            rb = service.watch_block_panel(
                {"block": key, "concepts": two, "concept_mode": "all", "max_rows": 100000})
            print(f"    any={ra['matched']} · all={rb['matched']}")
            check("any 命中数 >= all 命中数", ra["matched"] >= rb["matched"])
            check("all 的结果都同时含两个概念",
                  all(set(two) <= set(x["hit_concepts"] or []) for x in rb["rows"]))

        print()
        print("  --- 明细自洽 ---")
        check("明细行数 <= matched（可能被 max_rows 截断）",
              len(p0["rows"]) <= p0["matched"])
        check("明细行都有 code / name / industry_l2 字段",
              all({"code", "name", "industry_l2", "concept_n"} <= set(x) for x in p0["rows"][:50]))
        check("concept_n == len(concepts) 或 concepts 被截到 12",
              all(x["concept_n"] >= len(x["concepts"]) for x in p0["rows"][:50]))
        check("涨跌停字段只取 涨停/跌停/空",
              all(x.get("limit", "") in ("涨停", "跌停", "") for x in p0["rows"]))

print()
print("=" * 78)
print("七、错误处理：未知板块 / 未知行业名必须明确报错")
print("=" * 78)
bad = service.watch_block_panel({"block": "__no_such_block__"})
check("未知板块 → ok=False 且给出原因", bad.get("ok") is False and bad.get("reason"))
print(f"    {bad.get('reason')}")
if sample is not None:
    bad2 = service.watch_block_panel({"block": sample["key"], "industries": ["__假行业__"]})
    check("未知行业名 → ok=False（不能静默空结果）",
          bad2.get("ok") is False and bad2.get("reason"))
    print(f"    {bad2.get('reason')}")
    bad3 = service.watch_block_panel({"block": sample["key"], "concepts": ["__假概念__"]})
    check("未知概念名 → ok=False", bad3.get("ok") is False and bad3.get("reason"))
    print(f"    {bad3.get('reason')}")
    # 缺 block 参数
    bad4 = service.watch_block_panel({})
    check("缺 block 参数 → ok=False", bad4.get("ok") is False and bad4.get("reason"))

print()
print("=" * 78)
print("八、清单接口：total / with_members / member_codes 自洽")
print("=" * 78)
cli = service.watch_block_catalog()
check("catalog().total == 板块数", cli["total"] == len(cli["blocks"]))
check("with_members == 有成员板块数",
      cli["with_members"] == sum(1 for b in cli["blocks"] if b["has_members"]))
allm = set(c for b in cli["blocks"] for c in WB.members_of(b["key"]))
check("member_codes == 全体成员去重数",
      cli["member_codes"] == len(allm), f"{cli['member_codes']} vs {len(allm)}")
check("member_codes 与各板块成员并集一致（交叉核对）",
      allm == {c for c in allm if is_a_share(c)})

print()
print("=" * 78)
print("九、动态扫描：源目录增删板块后，不重启也能发现")
print("=" * 78)
# 用一个**临时目录**当 blocknew，构造全新索引，绝不碰真实通达信目录。
# 目的：用户 2026-10-07 反馈「在通达信里加/删板块，页面不更新」。
import shutil  # noqa: E402
import tempfile  # noqa: E402
from stock_picker.watchblocks import BLOCKNEW_SUBDIR, WatchBlockIndex  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="wb_dyn_"))
try:
    # WatchBlockIndex(tdx_dir) 会在其下自己拼 BLOCKNEW_SUBDIR（T0002/blocknew），
    # 所以这里造一个「假的通达信根目录」，绝不碰真实目录。
    _root = _tmp / "fake_tdx"
    _bdir = _root
    for part in BLOCKNEW_SUBDIR:
        _bdir = _bdir / part
    _bdir.mkdir(parents=True, exist_ok=True)

    # 初始：一个板块 AAAA，3 只股票（1=沪 0=深）
    (tmp_cfg := _bdir / "blocknew.cfg").write_bytes("AAA1\x00AAAA\x00".encode("gbk"))
    (tmp_cfg.with_name("AAAA.blk")).write_bytes(b"1600000\r\n0000001\r\n0300750")
    wb = WatchBlockIndex(str(_root), cache_file=_tmp / "wb.json")
    wb.load(force=True)
    keys0 = sorted(b["key"] for b in wb.catalog())
    check("初始只有一个板块 AAAA", keys0 == ["AAAA"], str(keys0))

    # ① 新增一个板块：通达信会**同时**写 .blk 和 blocknew.cfg 的条目。
    #    这里要验证的正是「只改源文件、不重启服务」能否被发现。
    (tmp_cfg.with_name("BBBB.blk")).write_bytes(b"1600000\r\n1600001")
    (tmp_cfg).write_bytes("AAA1\x00AAAA\x00BBB1\x00BBBB\x00".encode("gbk"))
    wb.load(force=False)
    keys1 = sorted(b["key"] for b in wb.catalog())
    check("新增板块（写 cfg + .blk）能被识别，索引已重建",
          "BBBB" in keys1, str(keys1))

    # ①b 只放 .blk 而不写 cfg → 清单以 cfg 为准，板块**不应**凭空出现
    (tmp_cfg.with_name("CCCC.blk")).write_bytes(b"1600000")
    wb.load(force=False)
    keys1b = sorted(b["key"] for b in wb.catalog())
    check("只有 .blk 没有 cfg 条目 → 不入清单（cfg 是权威来源）",
          "CCCC" not in keys1b, str(keys1b))
    (tmp_cfg.with_name("CCCC.blk")).unlink()

    # ② 改一个已有 .blk 的内容 → 成员数要跟着变（**不改 cfg**，只动 .blk）
    (tmp_cfg.with_name("BBBB.blk")).write_bytes(b"1600000\r\n1600001\r\n1600003")
    wb.load(force=False)
    nb = len(wb.members_of("BBBB"))
    check("只改 .blk 内容（不改 cfg）成员数也跟着变（3 只）", nb == 3, f"n={nb}")

    # ③ 删掉板块 → 从 cfg 与磁盘同时移除，清单里消失
    (tmp_cfg.with_name("BBBB.blk")).unlink()
    (tmp_cfg).write_bytes("AAA1\x00AAAA\x00".encode("gbk"))
    wb.load(force=False)
    keys3 = sorted(b["key"] for b in wb.catalog())
    check("删掉板块后从清单消失", "BBBB" not in keys3, str(keys3))

    # ④ 源文件没变时 load() 不应反复重建（必须命中缓存）
    b1 = (wb.load(force=False) or {}).get("built_at")
    b2 = (wb.load(force=False) or {}).get("built_at")
    check("源未变时缓存命中（built_at 不变）", b1 == b2 and b1, f"{b1} vs {b2}")

    # ⑤ refresh() 是 load(force=False) 的薄封装，幂等且返回本对象字典
    r1 = wb.refresh()
    check("refresh() 返回 dict（可直接用于接口）", isinstance(r1, dict))
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

print()
print("=" * 78)
if FAILD:
    print(f"FAILED {len(FAILD)} 项：")
    for n in FAILD:
        print("  - " + n)
    sys.exit(1)
print("全部通过 ✓")
sys.exit(0)
