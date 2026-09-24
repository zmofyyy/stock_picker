"""对打包后的 exe 做端到端冒烟测试。

用法：python packaging/smoke_test.py [base_url]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8778"
PASS, FAIL = [], []


def call(method: str, path: str, body=None, timeout: float = 120.0):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    elif method in ("POST", "PUT"):
        data = b""
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {}), time.perf_counter() - t
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")[:200]}, time.perf_counter() - t


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


print(f"目标：{BASE}\n")

# ---------- 0. 启动即预加载 ----------
# 服务一起来就在后台把本地已存在的数据全部载入（缓存缺失/过期/口径变更时重建，
# 全历史约 20s；本地行情有更新时也会自动重建）。
# 先等它就绪，后面的断言才是站在「数据齐全」的前提下做的。
print("0) 启动即预加载")


def wait_ready(secs: float = 240.0):
    t0 = time.perf_counter()
    last: dict = {}
    while time.perf_counter() - t0 < secs:
        try:
            st, d, _ = call("GET", "/api/bootstrap", timeout=15)
            if st == 200:
                last = d
                if d.get("ready"):
                    return d, time.perf_counter() - t0
        except Exception:
            pass
        time.sleep(0.7)
    return last, None


_boot, _bsecs = wait_ready()
_boot = _boot or {}
_bjob = _boot.get("job") or {}
_bres = _bjob.get("result") or {}
check("GET /api/bootstrap", bool(_boot.get("ok")),
      f"{_bsecs:.1f}s" if _bsecs is not None else "超时未就绪")
check("预加载任务完成", _bjob.get("status") == "done",
      str(_bjob.get("error") or _bjob.get("status")))
check("预加载覆盖全部索引",
      all((_bres.get("indexes") or {}).get(k)
          for k in ("names", "industries", "concepts", "float_shares")),
      str(_bres.get("indexes")))
check("预加载已备好行情",
      bool(_bres.get("codes_loaded")) and bool(_bres.get("rows")),
      f"{_bres.get('codes_loaded')} 只 / {_bres.get('rows')} 行 / last={_bres.get('last_date')} "
      f"/ rebuilt={_bres.get('rebuilt')}")
_steps = [s.get("step") for s in (_bres.get("steps") or [])]
check("预加载步骤齐全",
      all(n in _steps for n in ("名称表", "行业分类", "概念板块", "流通股本",
                                "行情缓存", "内存行情", "交易日索引")),
      " → ".join(_steps))
check("预加载后缓存不再过期", not (_boot.get("freshness") or {}).get("stale"),
      str((_boot.get("freshness") or {}).get("reason")))
check("预加载设置项可读（前端据此自动选股）",
      isinstance((_boot.get("settings") or {}).get("auto_screen"), bool),
      str(_boot.get("settings")))

# 二次预加载：缓存已最新，应当秒过且不再重建
st, _p, _ = call("POST", "/api/preload")
check("POST /api/preload", st == 200 and _p.get("job_id"), str(_p.get("job_id")))
_t0 = time.perf_counter()
try:
    _job2 = {}
    while time.perf_counter() - _t0 < 60:
        _st, _jd, _ = call("GET", f"/api/jobs/{_p.get('job_id')}", timeout=15)
        _job2 = _jd.get("job") or {}
        if _job2.get("status") in ("done", "error"):
            break
        time.sleep(0.3)
    _r2 = _job2.get("result") or {}
    check("缓存最新时预加载不重建",
          _job2.get("status") == "done" and _r2.get("rebuilt") is False,
          f"{time.perf_counter() - _t0:.2f}s · {_r2.get('cache_reason')}")
except Exception as e:
    check("缓存最新时预加载不重建", False, str(e))

# ---------- 1. 基础 ----------
print("1) 基础接口")
st, d, dt = call("GET", "/api/status")
check("GET /api/status", st == 200 and d.get("ready"), f"{dt*1000:.0f}ms")
check("缓存已建立", bool(d.get("cache", {}).get("exists")),
      f"latest={d.get('latest_date')} codes={d.get('cache', {}).get('codes_loaded')}")

# A 股总数会随本地通达信数据更新（新股上市 / 退市 / 改名）而变化，所以**不写死常量**，
# 改为与本地 vipdoc 目录按代码前缀统计的结果交叉核对 —— 既不怕数据更新，
# 又能真正验证「接口装的是全市场，而不是只装了一部分」。
_TDX_ROOT = Path(r"D:\new_tdx\vipdoc")
_A_PREFIX = {
    "sh": ("600", "601", "603", "605", "688", "689"),
    "sz": ("000", "001", "002", "003", "300", "301"),
    "bj": ("43", "83", "87", "88", "92"),
}


def count_local_a_share() -> int:
    total = 0
    for mkt, prefixes in _A_PREFIX.items():
        folder = _TDX_ROOT / mkt / "lday"
        if not folder.is_dir():
            continue
        for f in folder.glob("*.day"):
            code = f.stem[2:] if f.stem[:2] in ("sh", "sz", "bj") else f.stem
            if any(code.startswith(p) for p in prefixes):
                total += 1
    return total


_local_total = count_local_a_share()
check("A 股总数与本地 vipdoc 统计一致",
      bool(_local_total) and d.get("a_share_total") == _local_total,
      f"接口 {d.get('a_share_total')} vs 本地 {_local_total}")

st, d, _ = call("GET", "/api/health")
check("GET /api/health", st == 200)

# ---------- 2. 静态资源 ----------
print("\n2) 静态资源")
for p, key in (("/", b"<html"), ("/static/app.js", b"data-sort"), ("/static/style.css", b"screenTable"),
               ("/static/vendor/echarts.min.js", b"echarts"), ("/favicon.ico", b"<svg")):
    try:
        with urllib.request.urlopen(BASE + p, timeout=20) as r:
            blob = r.read()
        check(f"GET {p}", r.status == 200 and key in blob, f"{len(blob)} bytes")
    except Exception as e:
        check(f"GET {p}", False, str(e))

with urllib.request.urlopen(BASE + "/", timeout=20) as r:
    _html = r.read()
    _home_cc = r.headers.get("Cache-Control") or ""
check("首页含概念筛选与概念分布卡",
      b"conceptList" in _html and b"conceptSummary" in _html and b'data-sort="concept_n"' in _html)
check("首页含连板梯队页签与卡片",
      b'data-tab="streak"' in _html and b'id="panel-streak"' in _html
      and b'id="streakLadder"' in _html and b'id="streakChart"' in _html
      and b'id="streakTable"' in _html)
check("首页含板块交集页签与卡片",
      b'data-tab="intersect"' in _html and b'id="panel-intersect"' in _html
      and b'id="intersectVenn"' in _html and b'id="indPickList"' in _html
      and b'id="cptPickList"' in _html and b'id="intersectTable"' in _html)

# 前端资源指纹：改了 static 却仍旧页面 —— 反复踩的坑（曾表现为日期控件下界还卡在
# 旧的 250 个交易日）。三道防线都要在，缺一道旧标签页就可能继续跑旧脚本。
st, dver, _ = call("GET", "/api/version")
_ver = (dver or {}).get("asset_version") or ""
check("GET /api/version 返回资源指纹", st == 200 and len(_ver) == 10, f"asset_version={_ver}")
check("首页 __ASSET_VER__ 已全部替换", b"__ASSET_VER__" not in _html)
check("首页资源引用带指纹",
      f"/static/app.js?v={_ver}".encode() in _html and f"/static/style.css?v={_ver}".encode() in _html,
      f"?v={_ver}")
check("首页含版本过期提示条", b"staleBanner" in _html and b"staleReload" in _html)
check("首页 Cache-Control: no-store", _home_cc.startswith("no-store"), _home_cc)
with urllib.request.urlopen(BASE + f"/static/app.js?v={_ver}", timeout=20) as _r:
    _js = _r.read()
    _js_cc = _r.headers.get("Cache-Control") or ""
check("静态资源 Cache-Control: no-cache", _js_cc.startswith("no-cache"), _js_cc)
with urllib.request.urlopen(BASE + "/api/version", timeout=20) as _r:
    _api_cc = _r.headers.get("Cache-Control") or ""
check("接口 Cache-Control: no-store", _api_cc.startswith("no-store"), _api_cc)
check("app.js 拉全部交易日（n=0）", b"trade_dates?n=0" in _js)
check("app.js 含版本自检（loadVersion）", b"loadVersion" in _js and b"assetVersion" in _js)
check("app.js 含连板梯队逻辑",
      b"loadStreaks" in _js and b"/api/streaks" in _js and b"renderStreakLadder" in _js
      and b"exportStreakCsv" in _js)
check("app.js 含板块交集逻辑",
      b"loadIntersect" in _js and b"/api/intersect" in _js and b"renderVenn" in _js
      and b"renderIndPicker" in _js and b"exportIntersectCsv" in _js)

# 标签体检：一个多余的 </div> 就能把 #klineChart 挤出 .drawer-body，
# 让 .drawer-body 被 flex 压到 40px —— 计划买入/卖出价整行被压扁、图高从 620 压到 440，
# 但页面照样能打开，肉眼很难发现。这里把「标签配对」和「K 线图的父元素」都变成硬断言。
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "source", "area",
              "base", "col", "embed", "param", "track", "wbr"}


class _HtmlLint(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list = []          # [(tag, class)]
        self.errs: list = []
        self.chart_parent: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            return
        d = dict(attrs)
        if d.get("id") == "klineChart" and self.chart_parent is None:
            self.chart_parent = self.stack[-1][1] if self.stack else None
        self.stack.append((tag, d.get("class") or ""))

    def handle_endtag(self, tag):
        if tag in _VOID_TAGS:
            return
        if not self.stack:
            self.errs.append(f"第 {self.getpos()[0]} 行多余的 </{tag}>")
            return
        top, _ = self.stack.pop()
        if top != tag:
            self.errs.append(f"第 {self.getpos()[0]} 行 </{tag}> 与 <{top}> 错配")


_lint = _HtmlLint()
_lint.feed(_html.decode("utf-8", "replace"))
check("首页 HTML 标签配对", not _lint.errs and not _lint.stack,
      f"错配 {len(_lint.errs)} / 未闭合 {len(_lint.stack)}")
check("K 线图的父元素是 .drawer-body（回归点）",
      "drawer-body" in (_lint.chart_parent or ""),
      f"实际父元素 class={_lint.chart_parent!r}")

# ---------- 3. 选股 ----------
print("\n3) 选股（含流通市值过滤）")
payload = {"ma_window": 20, "volume_ratio": 2.0, "lookback_days": 1, "exclude_st": True,
           "markets": ["sh", "sz", "bj"], "boards": ["主板"], "industries": [],
           "sell_profit": 0.045, "max_results": 300, "max_float_mcap": 150}
st, d, dt = call("POST", "/api/screen", payload)
check("POST /api/screen", st == 200 and d.get("ok"), f"{dt:.1f}s")
# 命中数**随当日行情变化**（新股上市、个股放量与否），绝不能写死某个数字 ——
# 以前写死「212」就在数据更新后变成假失败。这里改成不依赖当日数据的自洽断言：
# 同参数两次一致 + 每一行都真的满足条件。
check("选股结果可复现（同参数两次一致）",
      call("POST", "/api/screen", payload)[1].get("total_hits") == d.get("total_hits"),
      f"hits={d.get('total_hits')} matched={d.get('matched')}")
check("命中数在合理区间",
      10 <= int(d.get("total_hits") or 0) <= 2000,
      f"命中 {d.get('total_hits')} / 全市场 {d.get('total_scanned')}")
rows = d.get("rows") or []
mcaps = [r["float_mcap_yi"] for r in rows if r.get("float_mcap_yi") is not None]
check("流通市值全部 < 150 亿", bool(mcaps) and max(mcaps) < 150, f"max={max(mcaps)}")
check("行内字段齐全（含 concepts）", bool(rows) and all(
    k in rows[0] for k in ("code", "name", "industry_l2", "concepts", "concept_n",
                           "pct_change", "vol_ratio",
                           "float_mcap_yi", "buy_price", "sell_price")))
check("无缺股本行", len(mcaps) == len(rows), f"{len(mcaps)}/{len(rows)}")
check("每行都满足放量条件（量比 ≥ 2）",
      bool(rows) and all(float(r.get("vol_ratio") or 0) >= 1.999 for r in rows),
      f"最小量比 {min((float(r.get('vol_ratio') or 0) for r in rows), default=0):.3f}")
check("每行都是主板且非 ST",
      all(r.get("board") == "主板" and "ST" not in (r.get("name") or "").upper() for r in rows),
      f"{len(rows)} 行")

# 排序字段可用
pcts = [r["pct_change"] for r in rows if r.get("pct_change") is not None]
check("涨跌幅字段可用于排序", len(pcts) == len(rows), f"最大 {max(pcts):.4f} / 最小 {min(pcts):.4f}")

# 概念（每行带分类）
cpt_lens = [len(r.get("concepts") or []) for r in rows]
check("每行带 concepts 列表", bool(rows) and all(isinstance(r.get("concepts"), list) for r in rows),
      f"平均 {sum(cpt_lens)/max(1,len(cpt_lens)):.2f} 个/只, 无概念 {sum(1 for n in cpt_lens if n==0)} 只")
check("行内 concept_n 与列表长度一致",
      all(r.get("concept_n") == len(r.get("concepts") or []) for r in rows))
check("概念总量 = 269", d.get("concept_total") == 269, str(d.get("concept_total")))
check("概念分布按全部命中统计",
      bool(d.get("concept_summary")) and d.get("concept_mapped", 0) + d.get("concept_unmapped", 0) == d.get("total_hits"),
      f"top={[g['concept'] for g in (d.get('concept_summary') or [])[:3]]}")

# ---------- 4. K 线 ----------
print("\n4) K 线")
code = rows[0]["code"]
st, d, dt = call("GET", f"/api/kline/{code}?bars=160&ma_window=20")
dates, ohlc = d.get("dates") or [], d.get("ohlc") or []
ma = d.get("ma") or {}
check(f"GET /api/kline/{code}", st == 200 and len(dates) == 160 and len(ohlc) == 160,
      f"{len(dates)} 根, {dt*1000:.0f}ms")
check("K 线含 MA 与成交量", bool(ma) and len(d.get("volume") or []) == 160,
      f"MA 组={sorted(ma.keys())}")
check("K 线含放量信号标记", isinstance(d.get("signals"), list), f"{len(d.get('signals') or [])} 个信号")
check("K 线日期升序且末位=最新", dates == sorted(dates), f"{dates[0]} → {dates[-1]}")
# 注意：未知代码返回的是 HTTP 200 + {"ok": false, "reason": ...}，不是 404。
# 这是既有约定（前端靠 body 里的 ok 判断），本次打包未改动，测试按真实契约断言。
st, d, _ = call("GET", "/api/kline/999999.SZ?bars=40")
check("K 线：未知代码返回 ok=false 且带 reason",
      st == 200 and d.get("ok") is False and "无" in str(d.get("reason")),
      f"status={st} reason={d.get('reason')}")
st, d, _ = call("GET", "/api/kline/600448.SH?bars=5")
check("K 线：bars 越界返回 422（ge=30）", st == 422, f"status={st}")

# ---------- 5. 交易日 / 行业 / 板块 ----------
print("\n5) 辅助接口")
st, d250, _ = call("GET", "/api/trade_dates?n=250")
check("GET /api/trade_dates?n=250", st == 200 and len(d250.get("dates", [])) == 250,
      f"{len(d250.get('dates', []))} 个交易日, 最新 {d250.get('latest')}")
st, dall, _ = call("GET", "/api/trade_dates?n=0")
dates_all = dall.get("dates") or []
check("GET /api/trade_dates?n=0 返回全部交易日",
      st == 200 and len(dates_all) > 5000 and dates_all == sorted(dates_all),
      f"{len(dates_all)} 个: {dall.get('first')} ~ {dall.get('latest')}")
# 回归本体：日期控件下界就是 dates[0]，所以 2022 年必须落在返回区间内。
# 曾经前端写死 ?n=250（只给最近一年），日历里 2022 年整个是灰的、点不动。
check("可选日期覆盖到 2022 年及更早（回归点）",
      bool(dates_all) and any(x.startswith(("2022-", "2021-", "2020-")) for x in dates_all),
      f"最早 {dall.get('first')}")

# ---------- 5.2 缓存历史深度 + 指定历史日期选股 ----------
print("\n5.2) 全历史缓存与指定历史日期")
st, stt, _ = call("GET", "/api/status")
cache = stt.get("cache") or {}
check("缓存为全历史（bars_per_code == 0）", int(cache.get("bars_per_code", -1)) == 0,
      f"rows={cache.get('rows')}, 平均 {cache.get('bars_per_code_avg')} 根/只, "
      f"最长 {cache.get('bars_per_code_max')}")
check("缓存记录了最早日期", bool(cache.get("first_date")), str(cache.get("first_date")))
# 与 .day 源文件根数交叉核对：抽 3 只，缓存根数必须等于文件字节数/32
_src_ok, _src_detail = True, []
for _c in ("600000.SH", "000001.SZ", "600519.SH"):
    _st, _ck, _ = call("GET", f"/api/kline/{_c}?bars=1000")
    _n = len(_ck.get("dates") or [])
    if _n != 1000:      # bars 上限 1000，只验证能取满
        _src_ok = False
        _src_detail.append(f"{_c}={_n}")
check("全历史下 K 线可取满 1000 根", _src_ok, " ".join(_src_detail) or "3 只均 1000 根")

# 指定 2022 年的交易日 → 必须真的选得出票，且信号日就是指定日
if any(x.startswith("2022-") for x in dates_all):
    _d22 = next(x for x in dates_all if x.startswith("2022-06"))
    st, h22, dt22 = call("POST", "/api/screen", {**payload, "date": _d22, "max_results": 20})
    _rows22 = h22.get("rows") or []
    check(f"指定日期选股 {_d22}（回归点）",
          st == 200 and h22.get("ok") and h22.get("matched", 0) > 0 and
          all(r.get("signal_date") == _d22 for r in _rows22),
          f"命中 {h22.get('matched')} 只, {dt22*1000:.0f}ms, "
          f"首行 {_rows22[0]['code'] if _rows22 else '-'}")
    check("历史日期选股仍满足量比条件",
          bool(_rows22) and all(float(r.get("vol_ratio") or 0) >= 1.999 for r in _rows22),
          f"最小量比 {min((float(r.get('vol_ratio') or 0) for r in _rows22), default=0):.3f}")
    # 越界日期要给出明确原因，不能静默返回空
    st, ob, _ = call("POST", "/api/screen", {**payload, "date": "1990-01-02"})
    check("越界日期返回明确原因（date_out_of_range）",
          st == 200 and ob.get("ok") is False and ob.get("date_out_of_range") is True,
          str(ob.get("reason"))[:60])
else:
    check("可选日期覆盖到 2022 年（回归点）", False, "交易日列表里没有 2022 年")
st, d, _ = call("GET", "/api/industries")
check("GET /api/industries", st == 200 and d.get("l2_count") == 128 and d.get("l1_count") == 30,
      f"{d.get('l1_count')} 一级 / {d.get('l2_count')} 二级, 覆盖 {d.get('size')} 只")
check("行业树结构嵌套正确",
      isinstance(d.get("tree"), dict) and all(isinstance(v, list) for v in d["tree"].values()),
      f"{len(d.get('tree') or {})} 个一级行业")
st, d, _ = call("GET", "/api/boards")
check("GET /api/boards", st == 200 and len(d.get("markets", [])) == 3,
      f"交易所 {[m['value'] for m in d.get('markets', [])]}")

# ---------- 5.5 概念筛选 ----------
print("\n5.5) 概念分类与筛选")
st, d, _ = call("GET", "/api/concepts")
check("GET /api/concepts", st == 200 and d.get("count") == 269 and len(d.get("items") or []) == 269,
      f"{d.get('count')} 个概念 / 覆盖 {d.get('size')} 只")
ver = ((d.get("meta") or {}).get("verify") or {})
bic = ver.get("by_index_code") or {}
check("概念口径 = tdxzs.cfg 类型4（按指数代码双向零残差）",
      bic.get("matched") == 269 and not bic.get("only_in_block") and not bic.get("only_in_cfg"),
      f"matched={bic.get('matched')}, 仅在block={len(bic.get('only_in_block') or [])}, "
      f"仅在cfg={len(bic.get('only_in_cfg') or [])}")
check("板块声明成员数 = 实际解析数",
      ver.get("declared_vs_actual_mismatch_count") == 0,
      f"差异 {ver.get('declared_vs_actual_mismatch_count')} 条")
check("成分行市场码与代码前缀无冲突", ver.get("market_suffix_mismatch") == 0,
      f"冲突 {ver.get('market_suffix_mismatch')} 条")

st, d1, _ = call("POST", "/api/screen", {**payload, "concepts": ["锂电池"], "max_results": 500})
leak = [r["code"] for r in (d1.get("rows") or []) if "锂电池" not in (r.get("concepts") or [])]
check("概念筛选：锂电池（无泄漏）",
      st == 200 and d1.get("ok") and bool(d1.get("matched")) and not leak,
      f"命中 {d1.get('matched')} 只, 泄漏 {len(leak)}")

st, d2, _ = call("POST", "/api/screen", {**payload, "concepts": ["新能源车", "固态电池"],
                                         "concept_mode": "all", "max_results": 500})
ok_all = bool(d2.get("rows")) and all(
    {"新能源车", "固态电池"} <= set(r.get("concepts") or []) for r in d2["rows"])
check("概念交集（concept_mode=all）", st == 200 and ok_all, f"命中 {d2.get('matched')} 只")

st, d3, _ = call("POST", "/api/screen", {**payload, "concepts": ["新能源车", "固态电池"], "max_results": 500})
check("概念并集 ≥ 交集", d3.get("matched", 0) >= d2.get("matched", 0),
      f"并集 {d3.get('matched')} / 交集 {d2.get('matched')}")

# 分布卡基数：必须不受「行业 / 概念」筛选影响。
# 回归点：曾把统计放在过滤之后，一点选行业，分布就只剩那一个行业、其余 chip 全消失。
st, dbase, _ = call("POST", "/api/screen", {**payload, "max_results": 500})
base_ind = {g["l2"]: g["n"] for g in (dbase.get("industry_summary") or [])}
base_cpt = {g["concept"]: g["n"] for g in (dbase.get("concept_summary") or [])}
check("分布基数 summary_base == 无筛选命中数",
      dbase.get("summary_base") == dbase.get("total_hits"),
      f"{dbase.get('summary_base')} vs {dbase.get('total_hits')}")

pick_l2 = next(iter(base_ind), None)
st, di, _ = call("POST", "/api/screen", {**payload, "industries": [pick_l2], "max_results": 500})
cur_ind = {g["l2"]: g["n"] for g in (di.get("industry_summary") or [])}
check(f"选行业「{pick_l2}」后行业分布卡不变（回归点）",
      st == 200 and cur_ind == base_ind and len(cur_ind) == len(base_ind),
      f"{len(cur_ind)} vs {len(base_ind)} 个行业")
check(f"选行业「{pick_l2}」→ chip 计数 == 实际命中数",
      cur_ind.get(pick_l2) == di.get("total_hits"),
      f"{cur_ind.get(pick_l2)} vs {di.get('total_hits')}")
check("选行业后结果无行业泄漏",
      all(r.get("industry_l2") == pick_l2 for r in (di.get("rows") or [])))

pick_cpt = next(iter(base_cpt), None)
st, dc, _ = call("POST", "/api/screen", {**payload, "concepts": [pick_cpt], "max_results": 500})
cur_cpt = {g["concept"]: g["n"] for g in (dc.get("concept_summary") or [])}
check(f"选概念「{pick_cpt}」后概念分布卡不变",
      st == 200 and cur_cpt == base_cpt and len(cur_cpt) == len(base_cpt),
      f"{len(cur_cpt)} vs {len(base_cpt)} 个概念")
check(f"选概念「{pick_cpt}」后行业分布卡也未受影响",
      {g["l2"]: g["n"] for g in (dc.get("industry_summary") or [])} == base_ind)

# ---------- 5.7 连板梯队 ----------
# 参照 stock_watch/views/streaks.py 实现。ST 5% 口径默认**关闭**：本机行情实测主板
# ST 股的日内带宽也是 10%（high/prev 的 p99.9 ≈ 1.10），套 5% 会显著多算涨停。
print("\n5.7) 连板梯队")
st, sk, dtsk = call("GET", "/api/streaks")
_m = sk.get("metrics") or {}
check("GET /api/streaks", st == 200 and sk.get("ok") is True,
      f"{dtsk:.2f}s · {sk.get('date')}")
check("默认锚定最新交易日", sk.get("date") == dall.get("latest"),
      f"{sk.get('date')} vs {dall.get('latest')}")
check("默认不启用 ST 5% 口径", sk.get("st_limit") is False, str(sk.get("st_limit")))
check("涨停家数 > 0", int(_m.get("limit_up") or 0) > 0,
      f"涨停 {_m.get('limit_up')} · 最高 {_m.get('max_streak')} 板")
check("首板 + 2板 + 3板+ = 涨停家数",
      _m.get("first", 0) + _m.get("second", 0) + _m.get("high3", 0) == _m.get("limit_up"),
      f"{_m.get('first')}+{_m.get('second')}+{_m.get('high3')} vs {_m.get('limit_up')}")
check("hist / ladder 求和都等于涨停家数",
      sum(h["n"] for h in (sk.get("hist") or [])) == _m.get("limit_up")
      and sum(l["n"] for l in (sk.get("ladder") or [])) == _m.get("limit_up"))
_skrows = sk.get("rows") or []
check("明细字段齐全", bool(_skrows) and all(
    k in _skrows[0] for k in ("code", "name", "board", "streak", "streak_label", "seal",
                              "limit_rate", "industry_l2", "concepts", "close",
                              "pct_change", "amount_yi", "float_mcap_yi")))
check("每行连板数 ≥ 1 且日期 = 锚点日",
      all(x["streak"] >= 1 and x["date"] == sk["date"] for x in _skrows))
check("每行涨跌幅贴近其涨停幅度",
      all(x["pct_change"] is None
          or abs(x["pct_change"] - (x["limit_factor"] - 100) / 100.0) <= 0.015
          for x in _skrows),
      f"{len(_skrows)} 行")
check("封板形态取值合法",
      all(x["seal"] in ("一字板", "T字板", "换手板") for x in _skrows),
      str(sorted({x["seal"] for x in _skrows})))
check("含行业 / 概念分布卡",
      isinstance(sk.get("industry_summary"), list) and isinstance(sk.get("concept_summary"), list)
      and len(sk.get("industry_summary") or []) > 0,
      f"行业 {len(sk.get('industry_summary') or [])} 个 / 概念 {len(sk.get('concept_summary') or [])} 个")
check("返回口径说明", bool(sk.get("conditions")) and bool(sk.get("warnings")),
      f"{len(sk.get('conditions') or [])} 条条件 / {len(sk.get('warnings') or [])} 条提示")

st, skb, _ = call("GET", "/api/streaks?boards=%E4%B8%BB%E6%9D%BF")   # 主板
check("板块筛选：只选主板 → 全为主板且非空",
      st == 200 and skb.get("ok") and len(skb.get("rows") or []) > 0
      and all(x["board"] == "主板" for x in skb["rows"]),
      f"主板 {skb.get('metrics', {}).get('limit_up')} 只")

st, sk3, _ = call("GET", "/api/streaks?min_streak=3")
check("min_streak=3 → 明细全 ≥ 3 板，但指标不变",
      st == 200 and all(x["streak"] >= 3 for x in (sk3.get("rows") or []))
      and (sk3.get("metrics") or {}).get("limit_up") == _m.get("limit_up"),
      f"{sk3.get('rows_count')} 只 / 指标 {sk3.get('metrics', {}).get('limit_up')}")

st, sko, _ = call("GET", "/api/streaks?date=1990-01-01")
check("越界日期返回明确原因（date_out_of_range）",
      st == 200 and sko.get("ok") is False and sko.get("date_out_of_range") is True,
      str(sko.get("reason"))[:60])

if any(x.startswith("2022-") for x in dates_all):
    _d22s = next(x for x in dates_all if x.startswith("2022-06"))
    st, sk22, dt22s = call("GET", f"/api/streaks?date={_d22s}")
    check(f"历史日期连板梯队 {_d22s}（回归点）",
          st == 200 and sk22.get("ok") and (sk22.get("metrics") or {}).get("limit_up", 0) > 0
          and all(x["date"] == _d22s for x in (sk22.get("rows") or [])),
          f"涨停 {sk22.get('metrics', {}).get('limit_up')} 只 · 最高 "
          f"{sk22.get('metrics', {}).get('max_streak')} 板 · {dt22s:.2f}s")

# ---------- 5.8 板块交集（二级行业 ∩ 概念）----------
print("\n5.8) 板块交集（二级行业 ∩ 概念）")
st, _ix_ind, _ = call("GET", "/api/industries")
_ix_counts = _ix_ind.get("l2_counts") or []
check("GET /api/industries 带 l2_counts（交集选器要标成员数）",
      st == 200 and len(_ix_counts) > 100,
      f"{len(_ix_counts)} 个行业带成员数")
_ix_l2 = [x["l2"] for x in _ix_counts[:2]]
st, _ix_cpt, _ = call("GET", "/api/concepts")
_ix_c = [x["name"] for x in sorted((_ix_cpt.get("items") or []),
                                   key=lambda z: -(z.get("n") or 0))[:2]]
check("交集用的行业 / 概念样本取自接口目录",
      len(_ix_l2) == 2 and len(_ix_c) == 2, f"{_ix_l2} × {_ix_c}")


def _ix_url(**over):
    q = {
        "industries": ",".join(_ix_l2), "concepts": ",".join(_ix_c),
        "boards": "主板,创业板,科创板,北交所", "exclude_st": "false",
    }
    q.update(over)
    return "/api/intersect?" + urllib.parse.urlencode(
        {k: v for k, v in q.items() if v is not None})


st, ix, dtix = call("GET", _ix_url(max_rows=3000), timeout=60)
check("GET /api/intersect", st == 200 and ix.get("ok"), f"{dtix:.2f}s")
_A, _B, _I = ix.get("set_a", 0), ix.get("set_b", 0), ix.get("intersect", 0)
check("集合自洽（容斥 / 拆分 / 上下界）",
      _A + _B - _I == ix.get("union")
      and ix.get("only_a") + _I == _A and ix.get("only_b") + _I == _B
      and _I <= min(_A, _B) and _A <= ix.get("candidates", 0),
      f"A={_A} B={_B} I={_I} union={ix.get('union')} 候选={ix.get('candidates')}")
_ixrows = ix.get("rows") or []
check("明细行数 = min(交集数, max_rows)",
      len(_ixrows) == min(_I, 3000), f"{len(_ixrows)} vs min({_I},3000)")
check("明细每行的行业都在选中集合内且命中至少 1 个选中概念",
      bool(_ixrows) and all(r["industry_l2"] in _ix_l2 for r in _ixrows)
      and all(r["hit_concepts"] and set(r["hit_concepts"]) <= set(_ix_c) for r in _ixrows),
      f"{len(_ixrows)} 行")
check("默认按成交额降序",
      all(_ixrows[i]["amount_yi"] >= _ixrows[i + 1]["amount_yi"]
          for i in range(len(_ixrows) - 1)))
check("返回口径说明",
      len(ix.get("conditions") or []) >= 6 and isinstance(ix.get("warnings"), list),
      f"{len(ix.get('conditions') or [])} 条条件")

# 行业多选 = 并集（二级行业是单值属性，两个行业的 A 必然互斥）
st, _a1, _ = call("GET", _ix_url(industries=_ix_l2[0], concepts=None), timeout=60)
st, _a2, _ = call("GET", _ix_url(industries=_ix_l2[1], concepts=None), timeout=60)
check("行业多选 = 并集（A1 + A2 == A12）",
      _a1.get("set_a", 0) + _a2.get("set_a", 0) == _A,
      f"{_a1.get('set_a')} + {_a2.get('set_a')} vs {_A}")

# 概念 all ≤ any，且 all 时每行命中全部
st, _any, _ = call("GET", _ix_url(concept_mode="any", max_rows=3000), timeout=60)
st, _all, _ = call("GET", _ix_url(concept_mode="all", max_rows=3000), timeout=60)
check("概念全部命中 ≤ 命中任一",
      _all.get("ok") and _all.get("set_b", 0) <= _any.get("set_b", 0),
      f"all={_all.get('set_b')} any={_any.get('set_b')}")
check("all 模式下每行都命中全部选中概念",
      all(set(_ix_c) <= set(r["hit_concepts"]) for r in (_all.get("rows") or [])),
      f"{_all.get('rows_count')} 行")

# 单位换算：min_amount 走亿元
st, _amt, _ = call("GET", _ix_url(min_amount="1", max_rows=3000), timeout=60)
check("min_amount 按亿元生效（明细都 ≥ 1 亿）",
      _amt.get("ok") and all(r["amount_yi"] >= 0.999 for r in (_amt.get("rows") or [])),
      f"{_amt.get('intersect')} 只 / 最小 "
      f"{min([r['amount_yi'] for r in (_amt.get('rows') or [])] or [0])}")

st, _cap, _ = call("GET", _ix_url(max_rows=5), timeout=60)
check("max_rows 截断生效（明细 ≤ 5，交集数不缩水）",
      len(_cap.get("rows") or []) <= 5 and _cap.get("intersect") >= len(_cap.get("rows") or []),
      f"{len(_cap.get('rows') or [])} 行 / 交集 {_cap.get('intersect')}")

st, _srt, _ = call("GET", _ix_url(sort="code", max_rows=400), timeout=60)
_codes = [r["code"] for r in (_srt.get("rows") or [])]
check("sort=code → 按代码升序", _codes == sorted(_codes), f"前 3 {_codes[:3]}")

st, _mb, _ = call("GET", _ix_url(boards="主板", max_rows=3000), timeout=60)
check("板块筛选：只选主板 → 结果全是主板",
      _mb.get("ok") and all(r["board"] == "主板" for r in (_mb.get("rows") or [])),
      f"{_mb.get('rows_count')} 行")

# 错误路径：业务错误走 200 + ok=false，不能是 500
st, _e1, _ = call("GET", "/api/intersect?industries=" + urllib.parse.quote("这个行业不存在"))
check("未知二级行业 → ok=false 且说明原因",
      st == 200 and _e1.get("ok") is False and "未知二级行业" in (_e1.get("reason") or ""),
      str(_e1.get("reason"))[:60])
st, _e2, _ = call("GET", "/api/intersect?industries=" + urllib.parse.quote(_ix_l2[0])
                  + "&concepts=" + urllib.parse.quote("这个也不存在"))
check("未知概念 → ok=false 且说明原因",
      st == 200 and _e2.get("ok") is False and "未知概念" in (_e2.get("reason") or ""),
      str(_e2.get("reason"))[:60])
st, _e3, _ = call("GET", "/api/intersect")
check("两侧都不选 → ok=false（不静默返回全市场）",
      st == 200 and _e3.get("ok") is False, str(_e3.get("reason"))[:60])
st, _e4, _ = call("GET", "/api/intersect?date=1990-01-01&industries="
                  + urllib.parse.quote(_ix_l2[0]))
check("越界日期 → ok=false 且带可选范围",
      st == 200 and _e4.get("ok") is False and _e4.get("date_out_of_range") is True
      and _e4.get("date_min") and _e4.get("date_max"),
      str(_e4.get("reason"))[:60])
st, _e5, _ = call("GET", "/api/intersect?industries="
                  + urllib.parse.quote(_ix_l2[0]) + "&concepts="
                  + urllib.parse.quote(_ix_c[0]) + "&boards=")
check("空 boards → ok=false（不静默套用默认板块）",
      st == 200 and _e5.get("ok") is False and "板块" in (_e5.get("reason") or ""),
      str(_e5.get("reason"))[:60])
st, _e6, _ = call("GET", "/api/intersect?industries="
                  + urllib.parse.quote(_ix_l2[0]) + "&concepts="
                  + urllib.parse.quote(_ix_c[0]) + "&markets=")
check("空 markets → ok=false（不静默变成全市场）",
      st == 200 and _e6.get("ok") is False and "交易所" in (_e6.get("reason") or ""),
      str(_e6.get("reason"))[:60])

# 单个名字必须整体传，不能被逐字拆开（曾经的真 bug）
st, _one, _ = call("GET", "/api/intersect?industries=" + urllib.parse.quote(_ix_l2[0])
                   + "&concepts=" + urllib.parse.quote(_ix_c[0])
                   + "&boards=" + urllib.parse.quote("主板,创业板,科创板,北交所"))
check("单个行业名不被逐字拆开",
      _one.get("ok") and _one.get("industries") == [_ix_l2[0]],
      str(_one.get("industries")))

# 历史日期：板块成分是当前成份，但行情取的是所选日
_d22x = next((x for x in dates_all if x.startswith("2022-06")), None)
if _d22x:
    st, _h, dth = call("GET", _ix_url(date=_d22x, max_rows=3000), timeout=60)
    check(f"历史日期板块交集 {_d22x}",
          _h.get("ok") and _h.get("set_a", 0) > 0 and _h.get("set_b", 0) > 0
          and all(r["trade_date"] == _d22x for r in (_h.get("rows") or [])),
          f"A={_h.get('set_a')} B={_h.get('set_b')} I={_h.get('intersect')} · {dth:.2f}s")
    check("回溯历史时提示「板块成分非历史快照」",
          any("当前" in w and "成份" in w for w in (_h.get("warnings") or [])),
          str((_h.get("warnings") or [])[:1])[:80])

# ---------- 6. 计划 / 追踪 CRUD（SQLite 落盘）----------
print("\n6) 计划 / 追踪 CRUD（SQLite）")
r0 = rows[0]
plan = {"code": r0["code"], "name": r0["name"], "signal_date": r0["signal_date"],
        "base_close": r0["close"], "volume": r0["volume"], "ma_volume": r0["ma_volume"],
        "vol_ratio": r0["vol_ratio"], "buy_price": r0["buy_price"],
        "sell_price": r0["sell_price"], "status": "待买入"}
st, d, _ = call("POST", "/api/plans", plan)
pid = d.get("id") or (d.get("item") or {}).get("id")
check("POST /api/plans", st == 200 and pid is not None, f"id={pid}")
st, d, _ = call("GET", "/api/plans")
items = d.get("items") or []
check("计划已落库", any(x.get("code") == r0["code"] for x in items), f"{len(items)} 条")
check("计划带 concepts", all(isinstance(x.get("concepts"), list) for x in items),
      f"样例 {[x.get('concepts', [])[:3] for x in items[:1]]}")

st, d, _ = call("POST", "/api/watchlist",
                {"code": r0["code"], "name": r0["name"], "signal_date": r0["signal_date"],
                 "base_close": r0["close"], "buy_price": r0["buy_price"],
                 "sell_price": r0["sell_price"], "status": "关注中"})
wid = d.get("id") or (d.get("item") or {}).get("id")
check("POST /api/watchlist", st == 200, str(d)[:120])
st, d, _ = call("GET", "/api/watchlist")
witems = d.get("items") or []
check("追踪已落库", any(x.get("code") == r0["code"] for x in witems), f"{len(witems)} 条")

# ---------- 7. 存储 / 备份 ----------
print("\n7) 存储与备份")
st, d, _ = call("GET", "/api/storage")
db_path = str(d.get("db_path") or "")
check("GET /api/storage", st == 200 and d.get("ok"), f"db_size={d.get('db_size')} bytes")
check("数据目录 = exe 同级的 data/",
      db_path.replace("/", "\\").lower().endswith(r"dist\data\stock_picker.db"), db_path)
check("备份目录在 data/backups", str(d.get("dir", "")).lower().endswith(r"dist\data\backups"),
      str(d.get("dir")))
# 直接读 DB 文件，确认 WAL 模式真的落到了 exe 旁边这份库上
import sqlite3
try:
    con = sqlite3.connect(db_path)
    jm = con.execute("PRAGMA journal_mode").fetchone()[0]
    ic = con.execute("PRAGMA integrity_check").fetchone()[0]
    n_plan = con.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    n_watch = con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    con.close()
    check("SQLite journal_mode=wal", jm.lower() == "wal", jm)
    check("SQLite integrity_check=ok", ic.lower() == "ok", ic)
    check("库内已有本轮写入的计划/追踪", n_plan >= 1 and n_watch >= 1,
          f"plans={n_plan} watchlist={n_watch}")
except Exception as e:
    check("直接读 SQLite 文件", False, repr(e))

st, d, _ = call("POST", "/api/storage/backup")
check("POST /api/storage/backup", st == 200 and d.get("ok"),
      str((d.get("item") or {}).get("file")))
st, d, _ = call("GET", "/api/storage/export")
check("GET /api/storage/export", st == 200 and d.get("plans"), f"导出 {len(d.get('plans') or [])} 条计划")

st, d, _ = call("GET", "/api/stats")
check("GET /api/stats", st == 200, str(d)[:120])

# ---------- 8. 收尾：只删掉本轮测试写入的记录 ----------
# 库里可能有用户真实使用的计划 / 追踪，**绝不整表清空**，只删自己刚创建的两条。
print("\n8) 收尾（只清理本轮写入）")
for path, _id, label in ((f"/api/plans/{pid}", pid, "计划"),
                         (f"/api/watchlist/{wid}", wid, "追踪")):
    if _id is None:
        check(f"测试{label}已清理", False, "未取到 id，跳过")
        continue
    st, _, _ = call("DELETE", path)
    check(f"测试{label}已清理", st == 200, f"DELETE {path} → {st}")

st, d, _ = call("GET", "/api/stats")
_left = [x for x in (call("GET", "/api/plans")[1].get("items") or []) if x.get("id") == pid]
_left_w = [x for x in (call("GET", "/api/watchlist")[1].get("items") or []) if x.get("id") == wid]
check("本轮测试记录无残留", not _left and not _left_w,
      f"库内剩余 plans={d.get('plans')} watch={d.get('watch')}（含用户既有数据）")

# ---------- 汇总 ----------
print(f"\n{'='*56}\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("全部通过 ✓")
