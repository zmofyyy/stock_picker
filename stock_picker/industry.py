"""行业分类索引：通达信研究行业（一级 / 二级 / 三级）。

数据来源（均为通达信本地文件，无需联网）
---------------------------------------
1. ``T0002/hq_cache/tdxhy.cfg``  —— 每只标的的行业归属
   行格式（GBK，``|`` 分隔，实测固定 6 字段）::

       市场 | 代码   | 通达信行业码 | 空 | 空 | 研究行业码
       0    | 000001 | T1001        |    |    | X500102

   ``市场``：``0`` 深 / ``1`` 沪 / ``2`` 京。

2. ``incon.dat`` 的 ``#TDXRSHY`` 段 —— 研究行业名称表
   行格式 ``码|名称``，共 474 条，按长度分三级::

       X10      -> 一级（X + 2 位，30 个）
       X1001    -> 二级（X + 4 位，128 个）
       X100101  -> 三级（X + 6 位，316 个）

   ``incon.dat`` 里另有两套分类：``#TDXNHY``（通达信行业，T 码）与
   ``#SWHY``（申万行业，纯数字码）。**但只有 T 码和 X 码能通过 tdxhy.cfg
   映射到具体股票**，申万码没有对应的股票归属文件，因此本项目采用 X 码。

设计说明
--------
- 二级行业 = 研究行业码前 5 个字符对应的名称。
- 若某只标的只有一级码（如 ``X9901``），二级降级为一级名称，不丢数据。
- ``verified`` 字段记录「代码前缀推断的市场」与「tdxhy.cfg 市场字段」的一致率，
  用于自证解析正确。
"""

from __future__ import annotations

import json
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .tdx_reader import _guess_market, normalize_code

#: 行业级别：一级 / 二级 / 三级 的码长度（含前导 ``X``）
LEVEL_LEN = {"l1": 3, "l2": 5, "l3": 7}

SEP = "######"
NAME_SECTION = "#TDXRSHY"

CACHE_FILE = "industries.json"

#: 解析逻辑版本号。任何影响解析结果的改动都要 +1，
#: 否则新旧代码会共用同一份缓存（只按源文件 mtime/size 校验是不够的）。
PARSE_VERSION = 2


def _decode(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("gbk", errors="replace")


def parse_name_table(incon_path: Path) -> Dict[str, str]:
    """解析 ``incon.dat`` 的 ``#TDXRSHY`` 段，返回 ``{行业码: 名称}``。"""
    text = _decode(incon_path).replace("\r\n", "\n").replace("\r", "\n")
    names: Dict[str, str] = {}
    active = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            # 段头。注意 '######' 是段结束标记，不是段名
            active = stripped == NAME_SECTION
            continue
        if not active or not stripped or "|" not in stripped:
            continue
        code, _, name = stripped.partition("|")
        code, name = code.strip(), name.strip()
        if code and name:
            names[code] = name
    return names


def parse_hy_cfg(hy_path: Path) -> Dict[str, Dict[str, str]]:
    """解析 ``tdxhy.cfg``，返回 ``{标准代码: {"t": T码, "x": X码}}``。

    同一 6 位代码在 A 股范围内唯一，但为稳妥仍按市场字段还原成
    ``600000.SH`` 形式。
    """
    text = _decode(hy_path).replace("\r\n", "\n").replace("\r", "\n")
    out: Dict[str, Dict[str, str]] = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        f = line.split("|")
        if len(f) < 6:
            continue
        mkt, symbol, tcode, xcode = f[0].strip(), f[1].strip(), f[2].strip(), f[5].strip()
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        suffix = {"0": "SZ", "1": "SH", "2": "BJ"}.get(mkt)
        if suffix is None:
            # 兜底：按代码前缀推断
            suffix = _guess_market(symbol).upper()
        out[f"{symbol}.{suffix}"] = {"t": tcode, "x": xcode}
    return out


def split_code_x(xcode: str, names: Dict[str, str]) -> Dict[str, Optional[str]]:
    """把研究行业码拆成三级名称。"""
    x = (xcode or "").strip()
    if not x.startswith("X"):
        return {"l1": None, "l2": None, "l3": None}
    l1 = names.get(x[: LEVEL_LEN["l1"]])
    l2 = names.get(x[: LEVEL_LEN["l2"]]) if len(x) >= LEVEL_LEN["l2"] else None
    l3 = names.get(x[: LEVEL_LEN["l3"]]) if len(x) >= LEVEL_LEN["l3"] else None
    # 粒度不足时逐级降级，保证 secondary 永远有值（只要 x 合法）
    if l2 is None:
        l2 = l1
    if l1 is None:
        l1 = l2
    return {"l1": l1, "l2": l2, "l3": l3}


class IndustryIndex:
    """行业索引与查询。"""

    def __init__(self, tdx_dir: Optional[str], cache_file: Optional[Path] = None) -> None:
        self.tdx_dir = Path(str(tdx_dir)) if tdx_dir else None
        self.cache_file = Path(cache_file) if cache_file else None
        self._map: Dict[str, Dict[str, Optional[str]]] = {}
        self._names: Dict[str, str] = {}
        self._tree: "OrderedDict[str, List[str]]" = OrderedDict()
        self._codes_t: Dict[str, str] = {}
        self.meta: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        """已建立行业归属的标的数。"""
        return len(self._map)

    def _sources(self) -> Dict[str, Optional[Path]]:
        base = self.tdx_dir
        if base is None:
            return {"hy": None, "incon": None}
        hy = base / "T0002" / "hq_cache" / "tdxhy.cfg"
        incon = base / "incon.dat"
        return {
            "hy": hy if hy.is_file() else None,
            "incon": incon if incon.is_file() else None,
        }

    def load(self, force: bool = False) -> Dict[str, Any]:
        """建立（或读取缓存）行业索引。"""
        src = self._sources()
        if src["hy"] is None or src["incon"] is None:
            self._map, self._names, self._tree = {}, {}, OrderedDict()
            self.meta = {
                "source": "none",
                "reason": "未找到 tdxhy.cfg 或 incon.dat",
                "coverage": 0,
            }
            return self.meta

        sig = {
            "v": PARSE_VERSION,
            "hy": {"mtime": src["hy"].stat().st_mtime, "size": src["hy"].stat().st_size},
            "incon": {"mtime": src["incon"].stat().st_mtime, "size": src["incon"].stat().st_size},
        }

        if not force and self.cache_file and self.cache_file.is_file():
            try:
                cached = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if cached.get("sig") == sig:
                    self._map = cached["map"]
                    self._names = cached["names"]
                    self._tree = OrderedDict((k, v) for k, v in cached["tree"])
                    self.meta = {**cached.get("meta", {}), "source": "cache"}
                    return self.meta
            except Exception:
                pass

        names = parse_name_table(src["incon"])
        entries = parse_hy_cfg(src["hy"])
        mapping: Dict[str, Dict[str, Optional[str]]] = {}
        for code, codes in entries.items():
            item = split_code_x(codes.get("x", ""), names)
            if item["l2"] is None:
                continue
            mapping[code] = item

        # 树：一级 -> [二级...]（保持名称表内的出现顺序）
        order_l1: List[str] = []
        tree: "OrderedDict[str, List[str]]" = OrderedDict()
        for xcode, nm in names.items():
            if len(xcode) == LEVEL_LEN["l2"]:
                l1 = names.get(xcode[: LEVEL_LEN["l1"]], "其他")
                if l1 not in tree:
                    tree[l1] = []
                    order_l1.append(l1)
                if nm not in tree[l1]:
                    tree[l1].append(nm)
        # 补齐只出现在三级表里的一级（理论上不会发生）
        for item in mapping.values():
            l1, l2 = item["l1"], item["l2"]
            if l1 and l1 not in tree:
                tree[l1] = []
            if l1 and l2 and l2 not in tree[l1]:
                tree[l1].append(l2)
        self._map, self._names, self._tree = mapping, names, tree
        self._codes_t = {c: v.get("t", "") for c, v in entries.items()}

        # 自证：代码前缀推断的市场 vs tdxhy.cfg 市场字段
        mismatch = 0
        for code in entries:
            symbol, _, mkt = code.partition(".")
            if _guess_market(symbol).upper() != mkt:
                mismatch += 1

        self.meta = {
            "source": "parsed",
            "parse_version": PARSE_VERSION,
            "loaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "names": len(names),
            "l1_count": sum(1 for c in names if len(c) == LEVEL_LEN["l1"]),
            "l2_count": sum(1 for c in names if len(c) == LEVEL_LEN["l2"]),
            "l3_count": sum(1 for c in names if len(c) == LEVEL_LEN["l3"]),
            "codes_total": len(entries),
            "codes_mapped": len(mapping),
            "market_field_mismatch": mismatch,
            "hy_file": str(src["hy"]),
            "incon_file": str(src["incon"]),
        }

        if self.cache_file:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(
                    {
                        "sig": sig,
                        "map": mapping,
                        "names": names,
                        "tree": list(tree.items()),
                        "meta": self.meta,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return self.meta

    # ------------------------------------------------------------------
    def get(self, code: str) -> Optional[Dict[str, Optional[str]]]:
        """返回 ``{"l1","l2","l3"}``；无该标的则返回 None。"""
        try:
            std = normalize_code(code)
        except Exception:
            return None
        return self._map.get(std)

    def get_l2(self, code: str) -> Optional[str]:
        item = self.get(code)
        return item["l2"] if item else None

    def tree(self) -> Dict[str, List[str]]:
        return {k: list(v) for k, v in self._tree.items()}

    def l2_names(self) -> List[str]:
        """全部二级行业名（按一级分组排序）。"""
        out: List[str] = []
        for l1, l2s in self._tree.items():
            for l2 in l2s:
                if l2 not in out:
                    out.append(l2)
        return out

    def l2_counts(self, sort_by_size: bool = True) -> List[Dict[str, Any]]:
        """全部二级行业的成员数（**静态**归属，与行情 / 筛选条件无关）。

        用途：「板块交集」页面的行业选择器要标出每个行业有多大，否则
        128 个行业名排在一起看不出哪个是主流。因为这只是一份静态索引，
        与「当日有行情的标的数」不同（停牌股也算成员），所以只作参考量级。
        """
        cnt: Counter = Counter()
        for item in self._map.values():
            nm = item.get("l2")
            if nm:
                cnt[nm] += 1
        groups = [{"l2": k, "n": v} for k, v in cnt.items()]
        if sort_by_size:
            groups.sort(key=lambda g: (-g["n"], g["l2"]))
        return groups

    def summary(self, codes: List[str]) -> Dict[str, Any]:
        """对一批代码做二级行业计数。"""
        cnt = Counter()
        unmapped: List[str] = []
        for c in codes:
            l2 = self.get_l2(c)
            if l2:
                cnt[l2] += 1
            else:
                unmapped.append(c)
        return {
            "groups": [{"l2": k, "n": v} for k, v in cnt.most_common()],
            "mapped": sum(cnt.values()),
            "unmapped": len(unmapped),
            "unmapped_sample": unmapped[:10],
        }

    def describe(self) -> Dict[str, Any]:
        return {
            "size": self.size,
            "l1_count": len(self._tree),
            "l2_count": len(self.l2_names()),
            "tree": self.tree(),
            **{k: v for k, v in self.meta.items() if k in ("source", "coverage")},
        }
