"""流通股本索引：通达信本地 ``gbbq``（股本变迁）文件。

数据来源
--------
``<tdx_dir>/T0002/hq_cache/gbbq`` —— 通达信股本变迁记录（加密二进制，
200,067 条，覆盖全部 5,602 只 A 股）。用 ``pytdx`` 自带的
``GbbqReader`` 解密后得到 8 列::

    market | code | datetime | category | v4 | v5 | v6 | v7

``category == 5``（股本变化）时，四个数值的含义（单位：**万股**）::

    v4 = 变动前流通股本
    v5 = 变动前总股本
    v6 = 变动后流通股本   <- 本项目取这一列
    v7 = 变动后总股本

**其余 category 的四个字段含义完全不同**，不可混用。例如 ``category == 1``
（除权除息）的 ``v4`` 是「每 10 股派息（元）」：

    600519 2024-06-19  v4=308.76  ->  茅台每股派息 30.876 元

所以本模块**只取 category == 5**。

算法
----
按 ``(market, code)`` 分组，取 ``datetime`` 最大的那条记录的 ``v6``，
×10000 换算为「股」，即该标的**当前流通股本**。

字段含义的验证（与公开数据核对，单位：万股）
--------------------------------------------
======================  ==========  =============  =============  ===========
标的                    记录日期     流通(v6)       总(v7)         实际流通占比
======================  ==========  =============  =============  ===========
601398 工商银行          2015-02-12    26,961,222     35,640,624     75.6%
601857 中国石油          2013-11-08    16,192,208     18,302,098     88.5%
600519 贵州茅台          2026-05-28       125,008        125,008    100.0%
688981 中芯国际          2026-09-10       200,059        856,226     23.4%
======================  ==========  =============  =============  ===========

注：工商银行 / 中国石油的最后一次股本变化分别在 2015 / 2013 年，
之后股本确实没有再变，**不是数据陈旧**。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

#: 缓存文件名（与 industries.json 同目录）
CACHE_FILE = "float_shares.json"

#: 解析逻辑版本号。任何影响解析结果的改动都要 +1。
PARSE_VERSION = 1

#: gbbq 的 market 编码 -> 本项目代码后缀
MARKET_SUFFIX = {0: "SZ", 1: "SH", 2: "BJ"}

#: gbbq 中「股本变化」的类别号
CATEGORY_SHARE_CHANGE = 5

#: 字段下标：变动后流通股本 / 变动后总股本
IDX_FLOAT_AFTER = 6
IDX_TOTAL_AFTER = 7


class FloatShareIndex:
    """``{600519.SH: 流通股本(股)}`` 索引，带 JSON 落盘缓存。"""

    def __init__(self, tdx_dir: str | Path, cache_file: Path | str) -> None:
        self.tdx_dir = Path(tdx_dir)
        self.cache_file = Path(cache_file)
        self.shares: Dict[str, float] = {}
        self.totals: Dict[str, float] = {}
        self.meta: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 源文件
    # ------------------------------------------------------------------
    @property
    def gbbq_path(self) -> Path:
        return self.tdx_dir / "T0002" / "hq_cache" / "gbbq"

    def source_stamp(self) -> Dict[str, Any]:
        p = self.gbbq_path
        if not p.is_file():
            return {"exists": False}
        st = p.stat()
        return {
            "exists": True,
            "path": str(p),
            "size": st.st_size,
            "mtime": round(st.st_mtime, 3),
        }

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def parse(self) -> Dict[str, Any]:
        """解析 gbbq，返回 ``{code: 流通股本(股)}`` 及附带统计。"""
        p = self.gbbq_path
        if not p.is_file():
            raise FileNotFoundError(f"未找到股本变迁文件：{p}")

        from pytdx.reader.gbbq_reader import GbbqReader

        raw = GbbqReader().get_df(str(p))
        if raw is None or len(raw) == 0:
            raise ValueError("gbbq 解析结果为空")

        changed = raw[raw["category"] == CATEGORY_SHARE_CHANGE]
        if len(changed) == 0:
            raise ValueError("gbbq 中没有 category=5（股本变化）记录")

        # datetime 升序 -> 每组最后一条即「最近一次股本变化」
        changed = changed.sort_values("datetime", kind="stable")
        last = changed.groupby(["market", "code"], sort=False).tail(1)

        shares: Dict[str, float] = {}
        totals: Dict[str, float] = {}
        bad_ratio = 0
        for r in last.itertuples(index=False):
            suffix = MARKET_SUFFIX.get(int(r.market))
            if suffix is None:
                continue
            key = f"{r.code}.{suffix}"
            flt = float(getattr(r, "songgu_qianzongguben")) * 10000.0
            tot = float(getattr(r, "peigu_houzongguben")) * 10000.0
            if flt <= 0:
                continue
            if tot > 0 and flt > tot * 1.000001:   # 流通不可能大于总股本
                bad_ratio += 1
                continue
            shares[key] = flt
            if tot > 0:
                totals[key] = tot

        if not shares:
            raise ValueError("gbbq 未解析出任何流通股本")

        years = last["datetime"].astype(str).str[:4]
        meta = {
            "parse_version": PARSE_VERSION,
            "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": self.source_stamp(),
            "records_total": int(len(raw)),
            "records_share_change": int(len(changed)),
            "codes": len(shares),
            "skipped_ratio_anomaly": bad_ratio,
            "latest_change_year": {
                "min": str(years.min()),
                "max": str(years.max()),
            },
        }
        return {"shares": shares, "totals": totals, "meta": meta}

    # ------------------------------------------------------------------
    # 缓存读写
    # ------------------------------------------------------------------
    def _cache_valid(self) -> bool:
        if not self.cache_file.is_file():
            return False
        try:
            blob = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except Exception:
            return False
        if blob.get("parse_version") != PARSE_VERSION:
            return False
        return blob.get("source") == self.source_stamp()

    def build(self) -> Dict[str, Any]:
        out = self.parse()
        blob = {
            "parse_version": PARSE_VERSION,
            "source": self.source_stamp(),
            **out["meta"],
            "shares": out["shares"],
            "totals": out["totals"],
        }
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.cache_file)
        self._adopt(blob)
        return self.meta

    def _adopt(self, blob: Dict[str, Any]) -> None:
        self.shares = {k: float(v) for k, v in (blob.get("shares") or {}).items()}
        self.totals = {k: float(v) for k, v in (blob.get("totals") or {}).items()}
        self.meta = {
            k: v for k, v in blob.items() if k not in ("shares", "totals")
        }

    def load(self, force: bool = False) -> Dict[str, Any]:
        """加载索引；缓存失效或 ``force`` 时重新解析 gbbq。"""
        if not force and self._cache_valid():
            try:
                self._adopt(json.loads(self.cache_file.read_text(encoding="utf-8")))
                self.meta["from_cache"] = True
                return self.meta
            except Exception:
                pass
        meta = self.build()
        meta["from_cache"] = False
        return meta

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, code: str) -> Optional[float]:
        """返回流通股本（股），查不到返回 ``None``。"""
        return self.shares.get(code)

    def float_ratio(self, code: str) -> Optional[float]:
        """流通股本 / 总股本。"""
        f = self.shares.get(code)
        t = self.totals.get(code)
        if not f or not t:
            return None
        return f / t

    def mcap_yi(self, code: str, price: float) -> Optional[float]:
        """流通市值（亿元）= 流通股本 × 价格 ÷ 1e8。"""
        f = self.shares.get(code)
        if not f or price is None or price <= 0:
            return None
        return f * float(price) / 1e8

    def coverage(self, codes) -> Dict[str, Any]:
        """给定代码列表的覆盖率，用于自证数据可用性。"""
        codes = list(codes)
        hit = sum(1 for c in codes if c in self.shares)
        return {
            "total": len(codes),
            "covered": hit,
            "missing": len(codes) - hit,
            "ratio": round(hit / len(codes), 4) if codes else 0.0,
        }

    def info(self) -> Dict[str, Any]:
        return {
            **self.meta,
            "size": len(self.shares),
            "cache_file": str(self.cache_file),
        }
