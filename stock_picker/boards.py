"""板块看盘：通达信板块目录（行业 / 地区 / 概念 / 风格）与成分股索引。

与 :mod:`stock_picker.concepts` 的分工
--------------------------------------
``concepts.py`` 只认 **概念**（``GN_`` 前缀），因为它服务于选股 / 板块交集，
口径必须与通达信「概念分类」严格一致。本模块服务的是「板块看盘」——
通达信板块分析界面里能看到的**全部板块**，因此把四类都收进来：

====================  =========  ========  ==================================
类别                  类型码     数量      成分来源
====================  =========  ========  ==================================
行业                  2          145       ``tdxhy.cfg`` 的 **T 码**（前缀聚合）+ ``incon.dat#TDXNHY``
地区                  3          32       本地无归属文件 → 只有指数行情
概念                  4          269      ``infoharbor_block.dat`` 的 ``GN_``
风格                  5          158      ``infoharbor_block.dat`` 的 ``FG_``
====================  =========  ========  ==================================

板块指数代码一律是 ``880xxx``，全部位于 ``vipdoc/sh/lday/sh880xxx.day``
（实测 604/604 本地齐全），因此每个板块都能画 K 线，与有没有成分股无关。

为什么行业走 T 码而不是本项目的 X 码
------------------------------------
``tdxhy.cfg`` 每行同时带 **T 码**（通达信行业）和 **X 码**（研究行业）。项目
其余部分用 X 码（128 个二级行业，见 :mod:`stock_picker.industry`），但
``tdxzs.cfg`` 里行业板块的 145 个名字与 **T 码名称表** 才对得上：实测交集
110/145、覆盖 5584/5585 只股票；而 X 码名称只对上 47 个。剩下 35 个板块是
``TDX 交运`` / ``TDX 信息`` 这类通达信一级别名与旧行业名，本地没有归属文件，
它们只提供指数行情（``has_members = False``）。

自证（``meta.verify``）
-----------------------
======================  ====================================================
index_files             cfg 里的板块指数代码，本地有 ``.day`` 的数量
industry_t_vs_cfg       行业：T 码名称表里的名字 ∩ cfg 类型 2 的名字
industry_unmatched      cfg 有、T 码表没有的行业（只有行情、没有成分）
members_covered         有成分股的板块数 / 板块总数
member_codes            成分股去重后的只数（应接近 A 股正股数）
======================  ====================================================
"""

from __future__ import annotations

import bisect
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .concepts import parse_block_file, parse_index_cfg
from .industry import _decode, parse_hy_cfg
from .tdx_reader import normalize_code

#: ``tdxzs.cfg`` 类型码 -> 中文类别名（顺序即界面上的展示顺序）
TYPE_CATEGORY = {
    "2": "行业",
    "3": "地区",
    "4": "概念",
    "5": "风格",
}

#: 界面上用的全称。主视图只暴露「概念板块 / 行业板块」两类
#: （地区本地无成分文件、风格是因子指标而非题材），其余保留给 API 调用方。
TYPE_LABEL = {
    "行业": "行业板块",
    "地区": "地区板块",
    "概念": "概念板块",
    "风格": "风格板块",
}

#: 板块看盘主视图展示的类别（顺序即界面顺序）。
PRIMARY_CATEGORIES = ("概念", "行业")

#: 类别 -> ``infoharbor_block.dat`` 里对应的板块名前缀
BLOCK_PREFIX = {"概念": "GN", "风格": "FG"}

#: ``incon.dat`` 里通达信行业（T 码）名称表所在段的段名
TDX_NAME_SECTION = "#TDXNHY"

#: 板块指数文件所在市场（实测 604 个全在 ``sh/lday``）
INDEX_MARKET = "sh"

CACHE_FILE = "boards.json"

#: 解析逻辑版本号。任何影响解析结果的改动都要 +1，
#: 否则新旧代码会共用同一份缓存（只按源文件 mtime/size 校验是不够的）。
#: v2：行业按 T 码**前缀**聚合（一级/二级行业板块也要有成分）、
#:     ``show_name`` 是纯数字时回退成全名。
#: v3：``show_name`` 是 T 码时同样回退 （``tdxzs.cfg`` 第 6 列对全部 145 个
#:     行业板块放的是 T 码，对 32 个地区板块放的是序号，只有概念/风格是
#:     真正的全称 —— 见 :data:`_CODE_LIKE`）。
PARSE_VERSION = 3

#: ``tdxzs.cfg`` 第 6 列（显示名）在部分类别里放的其实是**代码或序号**，
#: 而不是名称。命中这个正则就说明拿到了假名字，应回退用全名：
#:
#: - 行业（类型码 2）：``T0101`` / ``T030203`` —— 全部 145 个都是这样
#: - 地区（类型码 3）：``1`` / ``2`` …（纯序号）—— 全部 32 个
#: - 概念（4）/ 风格（5）：真名，其中 53 个与第 1 列的简称不同（是全称，**要保留**）
_CODE_LIKE = re.compile(r"^(?:[Tt]\d+|\d+)$")


def parse_tdx_name_table(incon_path: Path) -> Dict[str, str]:
    """解析 ``incon.dat`` 的 ``#TDXNHY`` 段，返回 ``{T码: 名称}``。

    实测 145 条：``T01``（一级，13 个）/ ``T0101``（二级，56 个）/
    ``T010101``（三级，76 个）。
    """
    text = _decode(incon_path).replace("\r\n", "\n").replace("\r", "\n")
    names: Dict[str, str] = {}
    active = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            active = stripped == TDX_NAME_SECTION
            continue
        if not active or not stripped or "|" not in stripped:
            continue
        code, _, name = stripped.partition("|")
        code, name = code.strip(), name.strip()
        if code and name:
            names[code] = name
    return names


class BoardIndex:
    """通达信全部板块的目录与成分股索引（带 JSON 落盘缓存）。"""

    def __init__(self, tdx_dir: Optional[str], cache_file: Optional[Path] = None) -> None:
        self.tdx_dir = Path(str(tdx_dir)) if tdx_dir else None
        self.cache_file = Path(cache_file) if cache_file else None
        self._catalog: List[Dict[str, Any]] = []
        self._members: Dict[str, List[str]] = {}
        self._by_code: Dict[str, Dict[str, Any]] = {}
        self.meta: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 源文件
    # ------------------------------------------------------------------
    def _hq(self, name: str) -> Optional[Path]:
        if self.tdx_dir is None:
            return None
        p = self.tdx_dir / "T0002" / "hq_cache" / name
        return p if p.is_file() else None

    def _incon(self) -> Optional[Path]:
        if self.tdx_dir is None:
            return None
        p = self.tdx_dir / "incon.dat"
        return p if p.is_file() else None

    def _sources(self) -> Dict[str, Optional[Path]]:
        return {
            "zs": self._hq("tdxzs.cfg"),
            "block": self._hq("infoharbor_block.dat"),
            "hy": self._hq("tdxhy.cfg"),
            "incon": self._incon(),
        }

    def _signature(self) -> Dict[str, Any]:
        src = self._sources()
        stamps: Dict[str, Any] = {"v": PARSE_VERSION}
        for key, path in src.items():
            if path is None:
                stamps[key] = {"exists": False}
            else:
                st = path.stat()
                stamps[key] = {
                    "exists": True,
                    "size": st.st_size,
                    "mtime": round(st.st_mtime, 3),
                }
        return stamps

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def parse(self) -> Dict[str, Any]:
        """解析本地板块文件，返回 ``{catalog, members, meta}``（不落盘）。"""
        src = self._sources()
        zs_path = src["zs"]
        if zs_path is None:
            raise FileNotFoundError("未找到板块指数表 T0002/hq_cache/tdxzs.cfg")

        rows = [r for r in parse_index_cfg(zs_path) if r["type_code"] in TYPE_CATEGORY]
        # 指数代码 -> 排序位次（保持 cfg 文件顺序，与行情软件列表顺序一致）
        order_of = {r["index_code"]: i for i, r in enumerate(rows)}

        # --- 概念 / 风格：infoharbor_block.dat ------------------------
        block_members: Dict[str, List[str]] = {}   # 板块指数代码 -> 成分
        block_counts: Counter = Counter()
        if src["block"] is not None:
            blocks = parse_block_file(src["block"])
            wanted = {v: k for k, v in BLOCK_PREFIX.items()}   # GN -> 概念
            for name, item in blocks.items():
                prefix = item.get("prefix") or ""
                if prefix not in wanted:
                    continue
                code = (item.get("index_code") or "").strip()
                if not code:
                    continue
                clean = [normalize_code(c) for c in item.get("codes") or []]
                block_members[code] = clean
                block_counts[prefix] += 1

        # --- 行业：tdxhy.cfg 的 T 码 ----------------------------------
        # T 码是**层级码**：T01（一级，3 字符）/ T0101（二级，5）/ T010101（三级，7）。
        # 实测每只股票的归属码都是**三级**码，所以一级 / 二级行业板块（「煤炭」
        # 「石油」这类）不能等值匹配，必须按**前缀**聚合 —— 一个行业板块的成员
        # 就是「自身码及其下级码」覆盖的全部股票，这也正是通达信行业指数的口径。
        industry_members: Dict[str, List[str]] = {}
        t_names: Dict[str, str] = {}
        if src["hy"] is not None and src["incon"] is not None:
            t_names = parse_tdx_name_table(src["incon"])
            t_codes = [
                (v["t"], c)
                for c, v in parse_hy_cfg(src["hy"]).items()
                if v.get("t")
            ]
            t_codes.sort()                       # 按码排序 → 前缀范围查可二分
            sorted_keys = [t for t, _ in t_codes]
            by_name = {r["name"]: r["index_code"] for r in rows if r["type_code"] == "2"}
            for tcode, name in t_names.items():
                idx = by_name.get(name)
                if not idx:
                    continue
                lo = bisect.bisect_left(sorted_keys, tcode)
                hi = bisect.bisect_left(sorted_keys, tcode + "\uffff")
                industry_members[idx] = [c for _, c in t_codes[lo:hi]]

        # --- 组装目录 --------------------------------------------------
        members: Dict[str, List[str]] = {}
        catalog: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for r in rows:
            code = r["index_code"]
            if code in seen:
                continue
            seen.add(code)
            category = TYPE_CATEGORY[r["type_code"]]
            if category == "行业":
                raw = industry_members.get(code, [])
            elif category in BLOCK_PREFIX:
                raw = block_members.get(code, [])
            else:
                raw = []
            clean = list(dict.fromkeys(raw))          # 去重保序
            if clean:
                members[code] = clean
            # tdxzs.cfg 第 6 列（显示名）对行业放 T 码、对地区放序号，
            # 都不是名字；这种就回退成全名，免得界面上冒出一个叫
            # 「T030203」或者叫「1」的板块。
            show = (r["show_name"] or "").strip()
            if not show or _CODE_LIKE.match(show):
                show = r["name"]
            catalog.append(
                {
                    "index_code": code,
                    "name": r["name"],
                    "show_name": show,
                    "category": category,
                    "type_code": r["type_code"],
                    "order": order_of.get(code, 0),
                    "n": len(clean),
                    "has_members": bool(clean),
                }
            )

        # --- 自证 ------------------------------------------------------
        t2_names = {r["name"] for r in rows if r["type_code"] == "2"}
        t_name_set = set(t_names.values())
        covered = sum(1 for c in catalog if c["has_members"])
        all_codes: Set[str] = set()
        for codes in members.values():
            all_codes.update(codes)
        by_category = Counter(c["category"] for c in catalog)
        by_category_members = Counter(
            c["category"] for c in catalog if c["has_members"]
        )
        index_dir = (
            self.tdx_dir / "vipdoc" / INDEX_MARKET / "lday"
            if self.tdx_dir is not None
            else None
        )
        with_file = 0
        if index_dir is not None and index_dir.is_dir():
            with_file = sum(
                1
                for c in catalog
                if (index_dir / f"{INDEX_MARKET}{c['index_code']}.day").is_file()
            )
        meta: Dict[str, Any] = {
            "parse_version": PARSE_VERSION,
            "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "boards": len(catalog),
            "boards_with_members": covered,
            "members_total": sum(len(v) for v in members.values()),
            "member_codes": len(all_codes),
            "by_category": dict(by_category),
            "by_category_with_members": dict(by_category_members),
            "verify": {
                "index_files": with_file,
                "index_dir": str(index_dir) if index_dir else "",
                "industry_t_names": len(t_names),
                "industry_t_vs_cfg": len(t2_names & t_name_set),
                "industry_unmatched": sorted(t2_names - t_name_set),
                "industry_matched": len(industry_members),
            },
        }
        return {"catalog": catalog, "members": members, "meta": meta}

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    def _cache_valid(self) -> bool:
        if self.cache_file is None or not self.cache_file.is_file():
            return False
        try:
            blob = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except Exception:
            return False
        return blob.get("sources") == self._signature()

    def build(self) -> Dict[str, Any]:
        out = self.parse()
        blob = {
            "parse_version": PARSE_VERSION,
            "sources": self._signature(),
            **out["meta"],
            "catalog": out["catalog"],
            "members": out["members"],
        }
        if self.cache_file is not None:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_file)
        self._adopt(blob)
        return self.meta

    def _adopt(self, blob: Dict[str, Any]) -> None:
        self._catalog = list(blob.get("catalog") or [])
        self._members = {k: list(v) for k, v in (blob.get("members") or {}).items()}
        self._by_code = {c["index_code"]: c for c in self._catalog}
        self.meta = {
            k: v for k, v in blob.items() if k not in ("catalog", "members")
        }

    def load(self, force: bool = False) -> Dict[str, Any]:
        """加载（或从缓存读取）板块索引；源文件缺失时降级为空索引。

        降级分支也把 ``boards / boards_with_members / member_codes / verify /
        by_category / by_category_with_members`` 这些字段补齐 —— 接口契约必须
        稳定，前端不该因为「本地没有板块文件」而拿到另一套结构的返回。
        """
        if self._sources()["zs"] is None:
            self._catalog, self._members, self._by_code = [], {}, {}
            self.meta = {
                "source": "none",
                "reason": "未找到 T0002/hq_cache/tdxzs.cfg",
                "boards": 0,
                "boards_with_members": 0,
                "members_total": 0,
                "member_codes": 0,
                "by_category": {},
                "by_category_with_members": {},
                "verify": {},
            }
            return self.meta
        if not force and self._cache_valid():
            try:
                blob = json.loads(self.cache_file.read_text(encoding="utf-8"))
                self._adopt(blob)
                self.meta["source"] = "cache"
                return self.meta
            except Exception:
                pass
        meta = self.build()
        meta["source"] = "parsed"
        return meta

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self._catalog)

    def catalog(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """板块目录（cfg 文件顺序）；``category`` 为空表示全部。"""
        if not category:
            return [dict(c) for c in self._catalog]
        return [dict(c) for c in self._catalog if c["category"] == category]

    def categories(self) -> List[Dict[str, Any]]:
        """类别清单（含板块数与**有成分**数），用于界面筛选与 API 校验。

        ``value`` 是解析时用的短名（行业/地区/概念/风格），``label`` 是界面全称；
        ``primary`` 标记「板块看盘」主视图展示的类别（概念板块 / 行业板块）。
        ``n_members`` 是本地有成分文件的板块数 —— 界面上的数量用它，
        与「仅有成分」勾选（默认开）下的实际行数一致。
        """
        total = Counter(c["category"] for c in self._catalog)
        with_m = Counter(c["category"] for c in self._catalog if c["has_members"])
        out = [{"value": "全部", "label": "全部", "n": len(self._catalog)}]
        # 主视图的两类排前面（顺序即 PRIMARY_CATEGORIES），其余按 tdxzs.cfg 的类型码顺序
        order = list(PRIMARY_CATEGORIES) + [
            c for c in TYPE_CATEGORY.values() if c not in PRIMARY_CATEGORIES
        ]
        for cat in order:
            if total.get(cat):
                out.append(
                    {
                        "value": cat,
                        "label": TYPE_LABEL.get(cat, cat),
                        "n": total[cat],
                        "n_members": with_m.get(cat, 0),
                        "primary": cat in PRIMARY_CATEGORIES,
                    }
                )
        return out

    def get(self, index_code: str) -> Optional[Dict[str, Any]]:
        hit = self._by_code.get(str(index_code).strip())
        return dict(hit) if hit else None

    def members_of(self, index_code: str) -> List[str]:
        """板块成分股（无成分数据时返回空列表）。"""
        return list(self._members.get(str(index_code).strip(), ()))

    def index_codes(self, category: Optional[str] = None) -> List[str]:
        return [c["index_code"] for c in self.catalog(category)]

    def name_of(self, index_code: str) -> str:
        hit = self._by_code.get(str(index_code).strip())
        return str(hit["name"]) if hit else ""

    def info(self) -> Dict[str, Any]:
        return {
            **self.meta,
            "size": len(self._catalog),
            "categories": self.categories(),
            "cache_file": str(self.cache_file) if self.cache_file else "",
        }


def members_by_category(
    index: BoardIndex, categories: Sequence[str] = ()
) -> Dict[str, List[str]]:
    """``{板块指数代码: 成分}``，只取指定类别（空 = 全部）。"""
    wanted = {str(c) for c in categories} if categories else None
    out: Dict[str, List[str]] = {}
    for item in index.catalog():
        if wanted is not None and item["category"] not in wanted:
            continue
        codes = index.members_of(item["index_code"])
        if codes:
            out[item["index_code"]] = codes
    return out


def all_member_codes(index: BoardIndex, categories: Iterable[str] = ()) -> List[str]:
    """全部板块成分股去重（升序），用于确定聚合时的股票池。"""
    seen: Set[str] = set()
    for codes in members_by_category(index, tuple(categories)).values():
        seen.update(codes)
    return sorted(seen)
