"""概念板块索引：通达信本地板块文件（概念 / 风格 / 指数分类）。

数据来源（均为通达信本地文件，无需联网）
---------------------------------------
1. ``T0002/hq_cache/infoharbor_block.dat`` —— 板块成分股，742 KB。
   以 ``#`` 开头的行是板块头，格式::

       #<类别前缀>_<板块名>,<声明成员数>,<板块指数代码>,<起始日>,<更新日>,,

   实测三类前缀：``GN_``（概念，269）、``FG_``（风格，161）、``ZS_``（指数，117）。
   板块头之后是成分行，一行多个、逗号分隔的 ``<市场>#<代码>``::

       0#000408,0#000538,0#000895,1#600519,1#688041,...
       ^ 市场 0 深 / 1 沪 / 2 京

2. ``T0002/hq_cache/tdxzs.cfg`` —— 板块指数表，用来确认「概念」口径与板块名。
   行格式 ``板块名|指数代码|类型码|?|0|显示名``，其中类型码::

       2 = 行业类   3 = 地区   4 = 概念   5 = 风格

   类型码 ``4`` 的条数与 ``infoharbor_block.dat`` 里 ``GN_`` 的板块数一致
   （实测均为 269），两个独立来源因此可以互相印证（见 ``meta.verify``）。

口径说明
--------
- 本模块的「概念」严格等于 **``GN_`` 前缀的板块**（同时与 ``tdxzs.cfg``
  类型码 4 交叉验证）。``FG_``（风格）与 ``ZS_``（指数）不混入。
- 概念名以板块头为准（全称，如「铜缆高速连接」）；``tdxzs.cfg`` 第 6 列
  是通达信界面上的短名（「铜缆连接」），仅作对照。
- 通达信自身把「含H股」「次新股」「ST板块」这类**属性标记**也归在概念里，
  本模块**不做主观筛选**，原样保留，避免与行情软件口径不一致。
- 一只股票可以属于很多概念（实测主板标的常见 5~40 个），故接口返回列表，
  由前端决定折叠展示。

自证（``meta.verify``）
-----------------------
============================  ==========================================
gn_vs_type4                   ``GN_`` 板块集合 vs ``tdxzs.cfg`` 类型 4 的差集
index_code_matched            ``GN_`` 板块头指数代码与 cfg 能对上号的数量
market_suffix_mismatch        板块内市场码 vs 按代码前缀推断的市场，不一致数
declared_vs_actual_mismatch   板块头声明成员数 vs 实际成分数的差异（前若干条）
coverage                      A 股覆盖率（由调用方传入全市场代码算）
============================  ==========================================
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .industry import _decode
from .tdx_reader import _guess_market, normalize_code

#: 板块类别前缀 -> 中文类别名
CATEGORY_NAMES = {"GN": "概念", "FG": "风格", "ZS": "指数"}

#: 本项目「概念分类」采用的类别前缀
CONCEPT_PREFIX = "GN"

#: ``tdxzs.cfg`` 中「概念板块」的类型码
CONCEPT_TYPE_CODE = "4"

#: 板块文件里的市场码 -> 代码后缀
MARKET_SUFFIX = {0: "SZ", 1: "SH", 2: "BJ"}

#: 缓存文件名（与 industries.json / float_shares.json 同目录）
CACHE_FILE = "concepts.json"

#: 解析逻辑版本号。任何影响解析结果的改动都要 +1，
#: 否则新旧代码会共用同一份缓存（只按源文件 mtime/size 校验是不够的）。
PARSE_VERSION = 1


def _source_stamp(path: Optional[Path], rel: str) -> Dict[str, Any]:
    if path is None or not path.is_file():
        return {"exists": False, "name": rel}
    st = path.stat()
    return {
        "exists": True,
        "name": rel,
        "size": st.st_size,
        "mtime": round(st.st_mtime, 3),
    }


def parse_block_file(path: Path) -> Dict[str, Dict[str, Any]]:
    """解析 ``infoharbor_block.dat``。

    返回 ``{板块名: {prefix, name, index_code, declared, start_date, updated, codes}}``，
    顺序与文件一致（同名板块后者覆盖前者，实测无重名）。
    """
    text = _decode(path).replace("\r\n", "\n").replace("\r", "\n")
    out: Dict[str, Dict[str, Any]] = {}
    cur: Optional[Dict[str, Any]] = None
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            head = line[1:].split(",")
            tag = head[0].strip()
            prefix, sep, name = tag.partition("_")
            if not sep:
                # 兜底：兼容没有类别前缀的老式写法（#板块名）
                prefix, name = "", tag
            if not name:
                cur = None
                continue
            declared: Optional[int] = None
            if len(head) > 1 and head[1].strip().isdigit():
                declared = int(head[1].strip())
            cur = {
                "name": name,
                "prefix": prefix,
                "category": CATEGORY_NAMES.get(prefix, prefix or "其他"),
                "index_code": head[2].strip() if len(head) > 2 else "",
                "declared": declared,
                "start_date": head[3].strip() if len(head) > 3 else "",
                "updated": head[4].strip() if len(head) > 4 else "",
                "codes": [],
            }
            out[name] = cur
            continue
        if cur is None:
            continue
        for token in line.split(","):
            token = token.strip()
            if not token or "#" not in token:
                continue
            mkt_s, _, symbol = token.partition("#")
            symbol = symbol.strip()
            if len(symbol) != 6 or not symbol.isdigit():
                continue
            try:
                mkt = int(mkt_s.strip())
            except ValueError:
                continue
            suffix = MARKET_SUFFIX.get(mkt)
            if suffix is None:
                continue
            cur["codes"].append(f"{symbol}.{suffix}")
    return out


def parse_index_cfg(path: Path) -> List[Dict[str, str]]:
    """解析 ``tdxzs.cfg``，返回板块指数条目（保持文件顺序）。"""
    text = _decode(path).replace("\r\n", "\n").replace("\r", "\n")
    rows: List[Dict[str, str]] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line or "|" not in line:
            continue
        f = line.split("|")
        if len(f) < 6:
            continue
        rows.append(
            {
                "name": f[0].strip(),
                "index_code": f[1].strip(),
                "type_code": f[2].strip(),
                "show_name": f[5].strip(),
            }
        )
    return rows


class ConceptIndex:
    """``{代码: [概念名...]}`` 索引，带 JSON 落盘缓存与自证元数据。"""

    def __init__(self, tdx_dir: Optional[str], cache_file: Optional[Path] = None) -> None:
        self.tdx_dir = Path(str(tdx_dir)) if tdx_dir else None
        self.cache_file = Path(cache_file) if cache_file else None
        self._map: Dict[str, List[str]] = {}
        self._members: Dict[str, List[str]] = {}
        self._catalog: List[Dict[str, Any]] = []
        self._n_by_name: Dict[str, int] = {}
        self.meta: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 源文件
    # ------------------------------------------------------------------
    def _block_path(self) -> Optional[Path]:
        if self.tdx_dir is None:
            return None
        p = self.tdx_dir / "T0002" / "hq_cache" / "infoharbor_block.dat"
        return p if p.is_file() else None

    def _zs_path(self) -> Optional[Path]:
        if self.tdx_dir is None:
            return None
        p = self.tdx_dir / "T0002" / "hq_cache" / "tdxzs.cfg"
        return p if p.is_file() else None

    def _sources(self) -> Dict[str, Optional[Path]]:
        return {"block": self._block_path(), "zs": self._zs_path()}

    def _signature(self) -> Dict[str, Any]:
        src = self._sources()
        return {
            "v": PARSE_VERSION,
            "block": _source_stamp(src["block"], "T0002/hq_cache/infoharbor_block.dat"),
            "zs": _source_stamp(src["zs"], "T0002/hq_cache/tdxzs.cfg"),
        }

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def parse(self, universe: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """解析本地板块文件，返回概念索引与统计（不自证时必须给出 universe）。

        :param universe: 全市场 A 股代码列表，用于计算覆盖率；为 ``None`` 时跳过。
        """
        src = self._sources()
        block_path = src["block"]
        if block_path is None:
            raise FileNotFoundError(
                "未找到板块文件 T0002/hq_cache/infoharbor_block.dat"
            )

        blocks = parse_block_file(block_path)
        zs_rows = parse_index_cfg(src["zs"]) if src["zs"] else []
        type4 = [r for r in zs_rows if r["type_code"] == CONCEPT_TYPE_CODE]
        type4_names: Set[str] = {r["name"] for r in type4}
        zs_by_name = {r["name"]: r for r in zs_rows}

        # 注意：空板块（如「含可转债」，通达信声明 0 成员）也保留在目录里，
        # 这样概念总数与通达信口径一致；它们自然不会命中任何股票。
        concepts = {
            name: item
            for name, item in blocks.items()
            if item["prefix"] == CONCEPT_PREFIX
        }

        # 概念 -> 成员（去重保序）
        members: Dict[str, List[str]] = {}
        mapping: Dict[str, List[str]] = {}
        market_mismatch = 0
        market_mismatch_sample: List[str] = []
        declared_mismatch: List[Dict[str, Any]] = []
        for name, item in concepts.items():
            seen: Set[str] = set()
            clean: List[str] = []
            for code in item["codes"]:
                symbol, _, mkt = code.partition(".")
                if _guess_market(symbol).upper() != mkt:
                    market_mismatch += 1
                    if len(market_mismatch_sample) < 10:
                        market_mismatch_sample.append(code)
                if code in seen:
                    continue
                seen.add(code)
                clean.append(code)
            members[name] = clean
            for code in clean:
                mapping.setdefault(code, []).append(name)
            if item["declared"] is not None and item["declared"] != len(clean):
                declared_mismatch.append(
                    {"concept": name, "declared": item["declared"], "actual": len(clean)}
                )

        # 概念目录（保持文件顺序，便于与行情软件的板块顺序对上）
        catalog: List[Dict[str, Any]] = []
        for i, (name, item) in enumerate(concepts.items()):
            zs = zs_by_name.get(name) or {}
            catalog.append(
                {
                    "name": name,
                    "order": i,
                    "n": len(members[name]),
                    "declared": item["declared"],
                    "index_code": item["index_code"] or zs.get("index_code", ""),
                    "show_name": zs.get("show_name", ""),
                    "start_date": item["start_date"],
                    "updated": item["updated"],
                }
            )

        gn_names = set(concepts.keys())
        # 名字对不上时（block 用简称「锂电池」，cfg 用全称「锂电池概念」），
        # 改用板块指数代码做交叉验证 —— 这是两个文件里真正一致的键。
        type4_codes = {r["index_code"] for r in type4 if r["index_code"]}
        block_codes = {
            item["index_code"] for item in concepts.values() if item["index_code"]
        }
        verify = {
            "gn_vs_type4": {
                "gn": len(gn_names),
                "type4": len(type4_names),
                "only_in_block": sorted(gn_names - type4_names),
                "only_in_cfg": sorted(type4_names - gn_names),
            },
            "by_index_code": {
                "block": len(block_codes),
                "cfg_type4": len(type4_codes),
                "matched": len(block_codes & type4_codes),
                "only_in_block": sorted(block_codes - type4_codes),
                "only_in_cfg": sorted(type4_codes - block_codes),
            },
            "index_code_matched": sum(
                1
                for c in catalog
                if c["index_code"] and (zs_by_name.get(c["name"]) or {}).get("index_code") == c["index_code"]
            ),
            "market_suffix_mismatch": market_mismatch,
            "market_suffix_mismatch_sample": market_mismatch_sample,
            "declared_vs_actual_mismatch": declared_mismatch[:10],
            "declared_vs_actual_mismatch_count": len(declared_mismatch),
            "blocks_total": len(blocks),
            "block_categories": dict(
                Counter(item["prefix"] for item in blocks.values()).most_common()
            ),
        }

        assignments = sum(len(v) for v in mapping.values())
        avg = round(assignments / len(mapping), 2) if mapping else 0.0
        per_stock_top = Counter(len(v) for v in mapping.values()).most_common(3)
        empty = [c["name"] for c in catalog if c["n"] == 0]
        meta: Dict[str, Any] = {
            "parse_version": PARSE_VERSION,
            "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sources": self._signature(),
            "concepts": len(catalog),
            "concepts_nonempty": len(catalog) - len(empty),
            "stocks": len(mapping),
            "assignments": assignments,
            "avg_concepts_per_stock": avg,
            "concept_count_top": [{"n_concepts": k, "stocks": v} for k, v in per_stock_top],
            "empty_concepts": empty,
            "index_cfg_concepts": len(type4),
            "verify": verify,
        }
        if universe is not None:
            codes = list(universe)
            hit = sum(1 for c in codes if c in mapping)
            meta["coverage"] = {
                "universe": len(codes),
                "covered": hit,
                "missing": len(codes) - hit,
                "ratio": round(hit / len(codes), 4) if codes else 0.0,
            }
        return {
            "map": mapping,
            "members": members,
            "catalog": catalog,
            "meta": meta,
        }

    # ------------------------------------------------------------------
    # 缓存读写
    # ------------------------------------------------------------------
    def _cache_valid(self) -> bool:
        if self.cache_file is None or not self.cache_file.is_file():
            return False
        try:
            blob = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except Exception:
            return False
        if blob.get("parse_version") != PARSE_VERSION:
            return False
        return blob.get("sources") == self._signature()

    def build(self, universe: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        out = self.parse(universe=universe)
        blob = {
            "parse_version": PARSE_VERSION,
            "sources": self._signature(),
            **out["meta"],
            "map": out["map"],
            "members": out["members"],
            "catalog": out["catalog"],
        }
        if self.cache_file is not None:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_file)
        self._adopt(blob)
        return self.meta

    def _adopt(self, blob: Dict[str, Any]) -> None:
        self._map = {k: list(v) for k, v in (blob.get("map") or {}).items()}
        self._members = {k: list(v) for k, v in (blob.get("members") or {}).items()}
        self._catalog = list(blob.get("catalog") or [])
        self._n_by_name = {
            str(c.get("name")): int(c.get("n") or 0) for c in self._catalog
        }
        self.meta = {
            k: v for k, v in blob.items() if k not in ("map", "members", "catalog")
        }

    def load(
        self, force: bool = False, universe: Optional[Sequence[str]] = None
    ) -> Dict[str, Any]:
        """加载（或从缓存读取）概念索引；源文件缺失时降级为空索引。"""
        src = self._sources()
        if src["block"] is None:
            self._map, self._members, self._catalog = {}, {}, []
            self._n_by_name = {}
            self.meta = {
                "source": "none",
                "reason": "未找到 T0002/hq_cache/infoharbor_block.dat",
                "concepts": 0,
                "stocks": 0,
            }
            return self.meta

        if not force and self._cache_valid():
            try:
                blob = json.loads(self.cache_file.read_text(encoding="utf-8"))
                self._adopt(blob)
                self.meta["source"] = "cache"
                self.meta["from_cache"] = True
                return self.meta
            except Exception:
                pass

        meta = self.build(universe=universe)
        meta["source"] = "parsed"
        meta["from_cache"] = False
        return meta

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        """已建立概念归属的标的数。"""
        return len(self._map)

    @property
    def concept_count(self) -> int:
        """概念数（含成员为 0 的空概念）。"""
        return len(self._catalog)

    def get(self, code: str, sort_by_size: bool = False) -> List[str]:
        """返回该标的的概念名列表（无则空列表）。

        :param sort_by_size: ``True`` 时按概念成员数降序排（大概念在前），
            列表展示用；默认保持通达信板块文件顺序，便于对表。
        """
        try:
            std = normalize_code(code)
        except Exception:
            return []
        items = list(self._map.get(std, ()))
        if sort_by_size and items:
            items.sort(key=lambda n: (-self._n_by_name.get(n, 0), n))
        return items

    def size_of(self, concept: str) -> int:
        """某概念的成员数（未知概念返回 0）。"""
        return self._n_by_name.get(str(concept), 0)

    def members_of(self, concept: str) -> List[str]:
        return list(self._members.get(concept, ()))

    def catalog(self) -> List[Dict[str, Any]]:
        """概念目录（文件顺序）。"""
        return [dict(c) for c in self._catalog]

    def names(self, sort_by: str = "order") -> List[str]:
        """全部概念名。``order``＝通达信板块顺序；``size``＝按成员数降序。"""
        items = self._catalog
        if sort_by == "size":
            items = sorted(items, key=lambda c: (-c.get("n", 0), c["name"]))
        return [c["name"] for c in items]

    def filter_codes(
        self, codes: Iterable[str], wanted: Iterable[str], mode: str = "any"
    ) -> Set[str]:
        """按概念筛出代码集合。

        :param mode: ``any``＝命中任一概念即保留（并集，默认）；
            ``all``＝必须同时命中全部概念（交集）。
        """
        wants = {str(w).strip() for w in (wanted or []) if str(w).strip()}
        if not wants:
            return set(codes)
        out: Set[str] = set()
        for c in codes:
            owned = set(self.get(c))
            if not owned:
                continue
            if (mode == "all" and wants <= owned) or (mode != "all" and (wants & owned)):
                out.add(c)
        return out

    def summary(self, codes: Sequence[str]) -> Dict[str, Any]:
        """对一批代码做概念计数（用于「概念分布」）。"""
        cnt: Counter = Counter()
        unmapped: List[str] = []
        for c in codes:
            items = self.get(c)
            if not items:
                unmapped.append(c)
                continue
            cnt.update(items)
        total = sum(cnt.values())
        return {
            "groups": [{"concept": k, "n": v} for k, v in cnt.most_common()],
            "mapped": len(codes) - len(unmapped),
            "unmapped": len(unmapped),
            "unmapped_sample": unmapped[:10],
            "tags": total,
            "avg": round(total / len(codes), 2) if codes else 0.0,
        }

    def info(self) -> Dict[str, Any]:
        return {
            **self.meta,
            "size": len(self._map),
            "concepts": self.concept_count,
            "cache_file": str(self.cache_file) if self.cache_file else "",
        }
