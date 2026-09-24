"""股票名称索引。

通达信本地没有 CSV 形式的名称表，但 ``T0002/hq_cache/{shs,szs,bjs}.tnf``
是标准的证券名称表。实测其结构为：

- 文件 = ``50`` 字节尾部预留 + ``N × 360`` 字节定长记录；
- 每条记录内：名称占 ``[81:89]``（8 字节 GBK，``\\x00`` 补齐），
  名称之前的 40 字节窗口内最后一段连续 6 位数字即证券代码。

解析后按 ``代码.市场`` 建索引，并缓存为 JSON，命中率对本地 vipdoc 约 96.9%
（未命中主要是已退市个股与可转债）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, Optional

from .tdx_reader import split_code

RECORD_SIZE = 360
NAME_OFFSET = 81
NAME_LEN = 8
TRAILER = 50
TNf_BY_MARKET = {"sh": "shs.tnf", "sz": "szs.tnf", "bj": "bjs.tnf"}


def parse_tnf(path: os.PathLike | str) -> Dict[str, str]:
    """解析单个 ``.tnf`` 文件，返回 ``{代码: 名称}``（代码为 6 位数字）。"""
    data = Path(path).read_bytes()
    body = data[: len(data) - TRAILER] if (len(data) - TRAILER) % RECORD_SIZE == 0 else data
    n = len(body) // RECORD_SIZE
    out: Dict[str, str] = {}
    for i in range(n):
        rec = body[i * RECORD_SIZE : (i + 1) * RECORD_SIZE]
        raw = rec[NAME_OFFSET : NAME_OFFSET + NAME_LEN].split(b"\x00")[0]
        if not raw:
            continue
        name = raw.decode("gbk", errors="replace").strip()
        if not name:
            continue
        hits = re.findall(rb"\d{6}", rec[NAME_OFFSET - 40 : NAME_OFFSET])
        if not hits:
            continue
        out.setdefault(hits[-1].decode("ascii"), name)
    return out


def _tnf_paths(tdx_dir: os.PathLike | str) -> Dict[str, Path]:
    base = Path(os.path.expanduser(str(tdx_dir))) if tdx_dir else None
    if base is None:
        return {}
    candidates = [base / "T0002" / "hq_cache"]
    found = {}
    for folder in candidates:
        if not folder.is_dir():
            continue
        for market, fname in TNf_BY_MARKET.items():
            p = folder / fname
            if p.is_file() and market not in found:
                found[market] = p
    return found


class NameIndex:
    """代码 -> 名称 的索引，带磁盘缓存与用户 CSV 覆盖。

    :param tdx_dir: 通达信目录
    :param cache_file: 名称缓存 JSON 路径
    :param extra_csv: 可选的用户名称表（列 ``code,name``），优先级最高
    """

    def __init__(
        self,
        tdx_dir: os.PathLike | str,
        cache_file: os.PathLike | str,
        extra_csv: Optional[os.PathLike | str] = None,
    ) -> None:
        self.tdx_dir = str(tdx_dir) if tdx_dir else ""
        self.cache_file = Path(cache_file)
        self.extra_csv = Path(extra_csv) if extra_csv else None
        self._names: Dict[str, str] = {}
        self._meta: Dict[str, object] = {}

    # ------------------------------------------------------------------
    def _signature(self) -> Dict[str, float]:
        return {
            market: p.stat().st_mtime
            for market, p in _tnf_paths(self.tdx_dir).items()
        }

    def load(self, force: bool = False) -> Dict[str, object]:
        """加载索引；缓存有效时直接读 JSON，否则重新解析 `.tnf`。"""
        sig = self._signature()
        if not force and self.cache_file.is_file():
            try:
                payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if payload.get("signature") == sig and payload.get("names"):
                    self._names = dict(payload["names"])
                    self._meta = payload.get("meta", {})
                    self._meta["from_cache"] = True
                    self._apply_extra()
                    return self._meta
            except Exception:
                pass

        names: Dict[str, str] = {}
        per_market: Dict[str, int] = {}
        paths = _tnf_paths(self.tdx_dir)
        for market, path in paths.items():
            part = parse_tnf(path)
            per_market[market] = len(part)
            for symbol, name in part.items():
                names[f"{symbol}.{market.upper()}"] = name

        self._names = names
        self._meta = {
            "total": len(names),
            "per_market": per_market,
            "source": "tnf" if names else "none",
            "from_cache": False,
        }
        self._apply_extra()
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(
                    {"signature": sig, "names": names, "meta": self._meta},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
        return self._meta

    def _apply_extra(self) -> None:
        """叠加用户 CSV（若提供），用户数据优先。"""
        if self.extra_csv is None or not self.extra_csv.is_file():
            return
        try:
            import csv

            with self.extra_csv.open("r", encoding="utf-8-sig", newline="") as fp:
                for row in csv.DictReader(fp):
                    code = (row.get("code") or "").strip()
                    name = (row.get("name") or "").strip()
                    if code and name:
                        try:
                            self._names[normalize_full(code)] = name
                        except Exception:
                            continue
            self._meta["extra_csv"] = str(self.extra_csv)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def get(self, code: str) -> str:
        """取名称，未命中返回空串。"""
        if not code:
            return ""
        key = code.strip().upper()
        if key in self._names:
            return self._names[key]
        try:
            return self._names.get(normalize_full(code), "")
        except Exception:
            return ""

    def coverage(self, codes) -> Dict[str, object]:
        """统计对给定代码集合的覆盖率。"""
        codes = list(codes)
        hit = sum(1 for c in codes if self.get(c) or self._names.get(c))
        return {
            "codes": len(codes),
            "named": hit,
            "ratio": round(hit / len(codes), 4) if codes else 0.0,
            "missing_sample": [c for c in codes if not self._names.get(c)][:20],
        }

    @property
    def meta(self) -> Dict[str, object]:
        return dict(self._meta)

    @property
    def size(self) -> int:
        return len(self._names)


def normalize_full(code: str) -> str:
    """``600000`` / ``sh600000`` -> ``600000.SH``。"""
    std = code.strip().upper()
    if "." in std:
        symbol, _, market = std.partition(".")
        return f"{symbol}.{market}"
    symbol, market = split_code(std)
    return f"{symbol}.{market.upper()}"
