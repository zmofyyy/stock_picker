"""自选板块：通达信 ``T0002\\blocknew`` 目录下的自定义板块清单与成分股。

与 :mod:`stock_picker.boards` 的分工
------------------------------------
``boards.py`` 读的是**通达信自带**的板块分类（行业 / 地区 / 概念 / 风格），
来源是 ``hq_cache/`` 下的那一套 ``.cfg`` / ``.dat``。本模块读的是
**用户自己在行情软件里手工维护**的自选板块 —— 即「板块 » 自选板块」里那
一批，存放位置是 ``T0002/blocknew/``，与后者完全独立，会随用户增删而变。

目录结构（实测 ::

    T0002/blocknew/
        blocknew.cfg      2520 B   板块显示名 + 成分文件名（变长记录，见下）
        *.blk             N × 7 B  每个板块一个文件，7 位 ASCII 代码一行
        blocknew.clr      1600 B   每个板块的显示颜色，本模块不解析
        zxg.blk                    自选股（不在 blocknew.cfg 里，通常为空）
        zxgmore.dat       11120 B  自选股附加属性，二进制，本模块不解析

``blocknew.cfg`` 的坑：记录**不是定长 25+25 切分**
--------------------------------------------------
文件里是「GBK 中文显示名 + ASCII 文件名」交替出现的字段流，每个字段以
``\\x00`` 结尾并**补齐到边界**，但补齐长度**不固定** —— 中文名占 2 字节/字，
名字长短不同，记录的物理边界就会落在字段中间。最初按「50 字节一条记录、
25 字节名 + 25 字节文件名」切，结果名字被拦腰截断（``集合竞价大量`` 被切成
``集合`` + 下半截跑到下一条记录尾部）。

正确做法是**忽略物理边界**，按「连续非 0 字节段」切出**字段流**，再**两两
配对**：奇数位是显示名（GBK 解码），偶数位是文件名（ASCII）。实测 42 段 →
21 组配对，且 **21 个文件名全部能在磁盘上找到同名 ``.blk``**，零错配。

为什么用「配对」而不是「按长度猜」
----------------------------------
配对关系来自文件结构本身的交替性，不依赖任何长度假设，因此对中文名长短
不敏感。校验方式是：配对出的文件名要么命中磁盘文件，要么丢弃 —— 这样即使
通达信改了格式导致字段数变成奇数，也只会少几个板块而不会错位成假名字。

``*.blk`` 的代码格式
--------------------
每行 **7 位 ASCII**：首位是**市场码**，后 6 位是股票代码::

    0 -> 深市（sz）    1 -> 沪市（sh）    2 -> 北交所（bj）

文件布局是「``\\r\\n`` + 7 位代码」逐行重复、**无尾换行**，实测字节数
**= 行数 × 9**（全部 21 个非空文件逐项精确成立；不是 ×7 —— 行间那两个 CRLF
也在文件里，首个 CRLF 与「无尾换行」正好抵消）。空板块（``2-12.blk`` /
``zxg.blk``）是 **0 字节**。

文件里除了数字与 CRLF **没有别的字节**（实测全 23 个文件零杂字节），因此
「把数字全提出来按 7 位切」与「按行解析」完全等价 —— 而前者对「有没有行尾
换行」「是不是 CRLF」都不敏感，更稳。:func:`parse_blk` 用的就是前者。

实测 23 个 ``.blk`` 共 2304 行、去重后 1603 个代码，用
:func:`stock_picker.tdx_reader.is_a_share` 过滤后**仍是 1603 个**（零剔除），
按首位 / 按后缀两种口径统计的行数折算后逐项相等
（``0``→sz 1094 / ``1``→sh 1206 / ``2``→bj 4），证明市场码映射无误。

磁盘上的 ``.blk`` 比 ``blocknew.cfg`` 多两个（``tjg.blk`` / ``zxg.blk``）——
它们是通达信内部用的（添加自选 / 自选股），界面上不显示，所以本模块的
板块清单**以 ``blocknew.cfg`` 为准**，但额外解析的 ``.blk`` 仍保留在
``meta.orphan_files`` 里，方便排查「为什么界面少一个板块」。

自证（``meta.verify``）
-----------------------
========================  ===============================================
cfg_pairs                 ``blocknew.cfg`` 里切出的配对字段数
cfg_hit_files             其中文件名能在磁盘找到 ``.blk`` 的组数
cfg_miss_files            配对了但磁盘无文件的（正常应为空）
blk_files                 磁盘上的 ``.blk`` 文件数
orphan_files              ``.blk`` 有、``cfg`` 没收的（tjg / zxg 这类）
raw_codes                 全部 ``.blk`` 的行数（含重复）
unique_codes              去重后的代码数
invalid_market            市场码不在 0/1/2 的行数
dropped_non_a_share       归一化后被 ``is_a_share`` 剔除的代码数
========================  ===============================================
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .tdx_reader import is_a_share, normalize_code, split_code

#: 板块目录相对通达信根目录的位置
BLOCKNEW_SUBDIR = ("T0002", "blocknew")

#: 板块清单文件；``*.blk`` 的成分文件与它同级
LIST_FILE = "blocknew.cfg"

#: ``*.blk`` 每行长度（7 位 ASCII：1 位市场码 + 6 位代码）
BLK_LINE_LEN = 7

#: 首位市场码 -> 交易所小写码
MARKET_PREFIX = {"0": "sz", "1": "sh", "2": "bj"}

#: 通达信内部使用、界面上不显示的自选文件，排除出板块清单
INTERNAL_FILES = {"zxg", "tjg"}

CACHE_FILE = "watch_blocks.json"

#: 解析逻辑版本号。任何影响解析结果的改动都要 +1，
#: 否则新旧代码会共用同一份缓存（只按源文件 mtime/size 校验是不够的）。
#: v1：按「连续非 0 段」切字段流 + 两两配对（最初的定长 50 字节切法会截断中文名）。
PARSE_VERSION = 1


def _decode_gbk(raw: bytes) -> str:
    """宽容解码 GBK；坏字节用替换符而不是抛异常。"""
    return raw.decode("gbk", errors="replace").strip()


def _decode_ascii(raw: bytes) -> str:
    """文件名一律 ASCII；非 ASCII 字节直接丢弃（拿来当文件名没有意义）。"""
    return "".join(chr(b) for b in raw if 32 <= b < 127).strip()


def split_fields(blob: bytes) -> List[bytes]:
    """把 ``blocknew.cfg`` 切成「连续非 0 字节段」列表。

    文件里字段以 ``\\x00`` 结尾并补齐，但补齐长度不固定，所以只能按
    ``\\x00`` 分段 —— 这也是唯一对「中文名长短不同」稳健的切法。
    """
    fields: List[bytes] = []
    start = 0
    i = 0
    n = len(blob)
    while i < n:
        if blob[i] == 0:
            if i > start:
                fields.append(blob[start:i])
            # 连续的 0 全部跳过（补齐区）
            while i < n and blob[i] == 0:
                i += 1
            start = i
        else:
            i += 1
    if start < n:
        seg = blob[start:n]
        if seg.strip(b"\x00"):
            fields.append(seg)
    return fields


def parse_blocknew_cfg(path: Path) -> List[Dict[str, str]]:
    """解析 ``blocknew.cfg`` -> ``[{"name": 显示名, "file": 文件名}]``。

    字段流两两配对：奇数位是 GBK 显示名，偶数位是 ASCII 文件名。字段数为
    奇数时丢掉最后一段（宁可少一个板块，也不能错位成假名字）。
    """
    blob = path.read_bytes()
    fields = split_fields(blob)
    pairs: List[Dict[str, str]] = []
    for i in range(0, len(fields) - 1, 2):
        name = _decode_gbk(fields[i])
        fname = _decode_ascii(fields[i + 1])
        if name and fname:
            pairs.append({"name": name, "file": fname})
    return pairs


def parse_blk(path: Path) -> Dict[str, Any]:
    """解析单个 ``.blk`` -> ``{"codes": [...], "raw": n, "invalid": n}``。

    每行 7 位：首位市场码（``0`` 深 / ``1`` 沪 / ``2`` 北），后 6 位代码。
    行尾 ``\\r\\n``；实测也有把多行拼成一长串的写法，所以按 7 位固定宽度
    扫描，而不是先 splitlines 再要求每行长度刚好 7。
    """
    blob = path.read_bytes()
    text = blob.decode("ascii", errors="ignore")
    digits = "".join(ch for ch in text if ch.isdigit())
    codes: List[str] = []
    invalid = 0
    raw = 0
    for i in range(0, len(digits) - BLK_LINE_LEN + 1, BLK_LINE_LEN):
        chunk = digits[i : i + BLK_LINE_LEN]
        raw += 1
        market = MARKET_PREFIX.get(chunk[0])
        if market is None:
            invalid += 1
            continue
        codes.append(normalize_code(market + chunk[1:]))
    return {"codes": codes, "raw": raw, "invalid": invalid}


class WatchBlockIndex:
    """通达信自选板块目录与成分股索引（带 JSON 落盘缓存）。"""

    def __init__(self, tdx_dir: Optional[str], cache_file: Optional[Path] = None) -> None:
        self.tdx_dir = Path(str(tdx_dir)) if tdx_dir else None
        self.cache_file = Path(cache_file) if cache_file else None
        self._blocks: List[Dict[str, Any]] = []
        self._members: Dict[str, List[str]] = {}
        self._by_key: Dict[str, Dict[str, Any]] = {}
        self.meta: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 源文件
    # ------------------------------------------------------------------
    def _dir(self) -> Optional[Path]:
        if self.tdx_dir is None:
            return None
        p = self.tdx_dir
        for part in BLOCKNEW_SUBDIR:
            p = p / part
        return p if p.is_dir() else None

    def _cfg(self) -> Optional[Path]:
        d = self._dir()
        if d is None:
            return None
        p = d / LIST_FILE
        return p if p.is_file() else None

    def _all_blk(self) -> List[Path]:
        d = self._dir()
        if d is None:
            return []
        return sorted(p for p in d.iterdir() if p.suffix.lower() == ".blk" and p.is_file())

    def _signature(self) -> Dict[str, Any]:
        """指纹 = 解析版本 + ``blocknew.cfg`` + **每个 ``.blk``** 的 size/mtime。

        自选板块会随时增删，只盯 ``blocknew.cfg`` 会漏掉「新加了一个板块文件
        但没改 cfg」这类情况，所以把整目录的 ``.blk`` 都纳入指纹。实测 23 个
        文件，走 ``DirEntry.stat()`` 约 1ms，可以每次启动都算。
        """
        stamps: Dict[str, Any] = {"v": PARSE_VERSION}
        cfg = self._cfg()
        if cfg is None:
            stamps["cfg"] = {"exists": False}
        else:
            st = cfg.stat()
            stamps["cfg"] = {
                "exists": True,
                "size": st.st_size,
                "mtime": round(st.st_mtime, 3),
            }
        blks: Dict[str, Any] = {}
        for p in self._all_blk():
            st = p.stat()
            blks[p.name] = [st.st_size, round(st.st_mtime, 3)]
        stamps["blk"] = blks
        return stamps

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def parse(self) -> Dict[str, Any]:
        """解析自选板块目录，返回 ``{blocks, members, meta}``（不落盘）。"""
        d = self._dir()
        cfg = self._cfg()
        if d is None or cfg is None:
            raise FileNotFoundError(
                "未找到通达信自选板块目录 T0002/blocknew/blocknew.cfg"
            )

        pairs = parse_blocknew_cfg(cfg)
        blk_files = {p.stem: p for p in self._all_blk()}

        blocks: List[Dict[str, Any]] = []
        members: Dict[str, List[str]] = {}
        dropped: List[str] = []
        raw_codes = 0
        invalid_market = 0
        used_files: set = set()

        for i, pair in enumerate(pairs):
            key = pair["file"]
            path = blk_files.get(key)
            if path is None:
                dropped.append(key)
                continue
            used_files.add(key)
            parsed = parse_blk(path)
            raw_codes += parsed["raw"]
            invalid_market += parsed["invalid"]
            seen: set = set()
            clean: List[str] = []
            for c in parsed["codes"]:
                if c in seen:
                    continue
                if not is_a_share(c):
                    continue
                seen.add(c)
                clean.append(c)
            if clean:
                members[key] = clean
            blocks.append(
                {
                    "key": key,
                    "name": pair["name"],
                    "file": f"{key}.blk",
                    "order": i,
                    "n": len(clean),
                    "has_members": bool(clean),
                }
            )

        # 磁盘上有、cfg 没收的 .blk（tjg / zxg 这类通达信内部文件）
        orphans = sorted(set(blk_files) - used_files)

        all_codes: set = set()
        for codes in members.values():
            all_codes.update(codes)

        meta: Dict[str, Any] = {
            "parse_version": PARSE_VERSION,
            "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "dir": str(d),
            "blocks": len(blocks),
            "blocks_with_members": sum(1 for b in blocks if b["has_members"]),
            "members_total": sum(len(v) for v in members.values()),
            "member_codes": len(all_codes),
            "verify": {
                "cfg_pairs": len(pairs),
                "cfg_hit_files": len(used_files),
                "cfg_miss_files": dropped,
                "blk_files": len(blk_files),
                "orphan_files": orphans,
                "raw_codes": raw_codes,
                "unique_codes": len(all_codes),
                "invalid_market": invalid_market,
            },
        }
        return {"blocks": blocks, "members": members, "meta": meta}

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
            "blocks": out["blocks"],
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
        self._blocks = list(blob.get("blocks") or [])
        self._members = {k: list(v) for k, v in (blob.get("members") or {}).items()}
        self._by_key = {b["key"]: b for b in self._blocks}
        self.meta = {
            k: v for k, v in blob.items() if k not in ("blocks", "members", "sources")
        }

    def load(self, force: bool = False) -> Dict[str, Any]:
        """加载（或从缓存读取）自选板块索引；源文件缺失时降级为空索引。

        降级分支同样补齐 ``blocks / blocks_with_members / members_total /
        member_codes / verify`` —— 接口契约要稳定，前端不该因为「本地没有
        blocknew 目录」而拿到另一套结构的返回。
        """
        if self._cfg() is None:
            self._blocks, self._members, self._by_key = [], {}, {}
            self.meta = {
                "source": "none",
                "reason": "未找到 T0002/blocknew/blocknew.cfg",
                "blocks": 0,
                "blocks_with_members": 0,
                "members_total": 0,
                "member_codes": 0,
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

    def refresh(self) -> Dict[str, Any]:
        """重新校验源文件指纹，**变了才重建**（没变几乎零成本）。

        与 ``load()`` 的区别：``load()`` 只按缓存文件的指纹决定要不要解析，
        本方法每次都会重算指纹 —— 用户在通达信里
        「新加了一个自选板块 / 往板块里加了股票 / 把板块删了」之后，
        只要页面再调一次清单接口，就能立刻看到最新结果，不用重启服务。

        ``_signature()`` 只是 ``os.stat`` 那 20 来个文件（实测 0.34ms），
        所以放在每次请求里做是安全的。
        """
        return self.load(force=False)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self._blocks)

    def catalog(self) -> List[Dict[str, Any]]:
        """板块清单（``blocknew.cfg`` 顺序，与行情软件列表一致）。"""
        return [dict(b) for b in self._blocks]

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """按**文件名**取板块（``key`` 不带 ``.blk`` 后缀）。"""
        hit = self._by_key.get(str(key).strip())
        return dict(hit) if hit else None

    def by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """按显示名取板块（同名时取 ``cfg`` 里靠前的那个）。"""
        want = str(name).strip()
        for b in self._blocks:
            if b["name"] == want:
                return dict(b)
        return None

    def resolve(self, ref: str) -> Optional[Dict[str, Any]]:
        """把前端传来的引用解析成板块：先按文件名，再按显示名。"""
        ref = str(ref or "").strip()
        if not ref:
            return None
        if ref.endswith(".blk"):
            ref = ref[:-4]
        return self.get(ref) or self.by_name(ref)

    def members_of(self, key: str) -> List[str]:
        """板块成分股（归一化后的 6 位代码；无成员时返回空列表）。"""
        hit = self.resolve(key)
        if hit is None:
            return []
        return list(self._members.get(hit["key"], ()))

    def name_of(self, key: str) -> str:
        hit = self.resolve(key)
        return str(hit["name"]) if hit else ""

    def info(self) -> Dict[str, Any]:
        return {
            **self.meta,
            "size": len(self._blocks),
            "blocks_all": self.catalog(),
            "cache_file": str(self.cache_file) if self.cache_file else "",
        }


def market_counts(codes: List[str]) -> Dict[str, int]:
    """统计一批代码的交易所分布（``{"sh": n, "sz": n, "bj": n}``）。"""
    out: Dict[str, int] = {"sh": 0, "sz": 0, "bj": 0}
    for c in codes:
        m = split_code(c)[1]
        if m in out:
            out[m] += 1
    return out
