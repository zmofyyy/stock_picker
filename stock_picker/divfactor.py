"""复权因子索引：通达信本地 ``gbbq`` 的 **除权除息**（``category == 1``）记录。

为什么需要它
------------
本项目行情缓存里三列**全部是不复权原始值、同尺度**（实测见下）：

======================  ==================  ==========================================
列                      口径                证据
======================  ==================  ==========================================
``close``               **不复权**          ``000002.SZ`` 2003-05-23（10 送 10）从
                                            13.81 腰斩到 6.79
``amount``              **原始金额（元）**  同日 1.536 亿 → 1.619 亿，**连续不跳变**
``volume``              **原始股数**        同日放量到 2365 万股（送股后股本翻倍）
======================  ==================  ==========================================

全市场 20 万条抽样：``close / (amount/volume)`` 中位数 **1.0001**、**99.97%** 落在
``[0.9, 1.1]`` —— 三列同口径。于是 ``amount / volume`` 是**不复权**均价，
**与 ``close`` 同尺度**；但**跨除权日**它会把两种价位尺度混在一起。
用它做「250 日成交额加权均价」再与 ``close`` 相减，跨除权日就把两种尺度混
在一起了 —— 这正是通达信公式里写 ``FQ := DIVFACTOR(1)/CONST(DIVFACTOR(1))``
的用意（那边 ``FQ`` 恒为 1，是因为通达信内部的 ``AMOUNT``/``VOL`` 已随复权
口径调整；本项目缓存没有，所以要自己算并乘上去）。

数据来源
--------
``<tdx_dir>/T0002/hq_cache/gbbq``，``category == 1`` 的四列（**每 10 股口径**）::

    hongli_panqianliutong     每 10 股派息（元）
    peigujia_qianzongguben    配股价（元/股）
    songgu_qianzongguben      每 10 股送转股（股）
    peigu_houzongguben        每 10 股配股（股）

实测样例::

    000002  2003-05-23  hongli=2.0   songgu=10.0   -> 10 送 10 派 2 元
    600519  2024-06-19  hongli=308.76              -> 每股派息 30.876 元
    600000  2025-07-16  hongli=4.10                -> 每 10 股派 4.1 元

算法（前复权，直接由**不复权**行情与除权事件算出）
--------------------------------------------------
本项目缓存的 ``close`` / ``amount`` / ``volume`` **三者都是不复权原始值**
（全市场 20 万条抽样：``close / (amount/volume)`` 中位数 1.0001、99.97% 落在
``[0.9, 1.1]``；且 ``000002.SZ`` 在 2003-05-23 从 13.81 腰斩到 6.79 —— 若为
前复权则该跳空会被抹平）。因此因子必须**自己从原始价算**。

除权日 ``d``：``P_raw(d-1)`` 为前一日**不复权**收盘（缓存直接可取），
除权参考价::

    P_ref = (P_raw(d-1) - hongli/10 + peigujia*peigu/10) / ratio
    ratio = 1 + (songgu + peigu)/10

单步因子 ``f_d = P_ref / P_raw(d-1)``，整理得::

    f_d = [ 1 - (hongli/10 - peigujia*peigu/10) / P_raw(d-1) ] / ratio

**不需要递推** —— 直接用缓存里的前收盘即可。某日 ``t`` 的前复权因子
``F(t)`` = ``t`` **之后**所有除权日 ``f`` 的**连乘**（最新一日恒为 1.0）。

校验（``000002.SZ`` 2003-05-23，``hongli=2.0``、``songgu=10.0``、``P_raw(05-22)=13.81``）::

    f   = [1 - 0.2/13.81] / 2.0 = 0.49638
    P_adj(05-22) = 13.81 * 0.49638 = 6.855   <- 与 05-23 的 6.79 连续（前复权应有的样子）
    P_adj(05-23) =  6.79 * 1.0     = 6.790


约定
----
只落盘**事件日**的 ``(date, factor_pct)``（``factor_pct = round(F * 10000)``），
非事件日用「上一个事件日的因子」填充（阶梯函数），避免存全序列。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: 缓存文件名（与 float_shares.json 同目录）
CACHE_FILE = "div_factors.json"

#: 解析逻辑版本号。任何影响结果的改动都要 +1。
PARSE_VERSION = 1

#: gbbq 的 market 编码 -> 本项目代码后缀
MARKET_SUFFIX = {0: "SZ", 1: "SH", 2: "BJ"}

#: gbbq 中「除权除息」的类别号
CATEGORY_DIVIDEND = 1

#: 因子定点精度：存成「万分之一」的整数
SCALE = 10000


class DivFactorIndex:
    """``{code: [[日期, 万分之一因子], ...]}``（按日期升序），带 JSON 落盘缓存。

    **只含除权事件日**；非事件日的因子 = 其前最近一个事件日的因子。
    用法：``idx.load()`` → ``idx.step_factor(code, date_int, prev_close_adj)``
    逐日递推得到全序列（见 :func:`build_factor_series`）。
    """

    def __init__(self, tdx_dir: str | Path, cache_file: Path | str) -> None:
        self.tdx_dir = Path(tdx_dir)
        self.cache_file = Path(cache_file)
        #: ``{code: [[date_int, hongli_x1000, peigujia_x1000, songgu_x1000, peigu_x1000], ...]}``
        self.events: Dict[str, List[List[int]]] = {}
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
        """解析 gbbq 的除权除息记录，得到每只票的**事件表**（按日期升序）。

        这里只做解析、不做因子运算 —— 因子依赖行情 ``close``，属于服务层职责。
        """
        p = self.gbbq_path
        if not p.is_file():
            raise FileNotFoundError(f"未找到股本变迁文件：{p}")

        from pytdx.reader.gbbq_reader import GbbqReader

        raw = GbbqReader().get_df(str(p))
        if raw is None or len(raw) == 0:
            raise ValueError("gbbq 解析结果为空")

        div = raw[raw["category"] == CATEGORY_DIVIDEND]
        if len(div) == 0:
            raise ValueError("gbbq 中没有 category=1（除权除息）记录")
        div = div.sort_values("datetime", kind="stable")

        events: Dict[str, List[List[int]]] = {}
        n_events = 0
        n_skipped = 0
        for (market, code), grp in div.groupby(["market", "code"], sort=False):
            suffix = MARKET_SUFFIX.get(int(market))
            if suffix is None:
                continue
            key = f"{code}.{suffix}"
            rows: List[List[int]] = []
            for r in grp.itertuples(index=False):
                hongli = float(getattr(r, "hongli_panqianliutong") or 0.0)
                peigujia = float(getattr(r, "peigujia_qianzongguben") or 0.0)
                songgu = float(getattr(r, "songgu_qianzongguben") or 0.0)
                peigu = float(getattr(r, "peigu_houzongguben") or 0.0)
                ratio = 1.0 + (songgu + peigu) / 10.0
                if ratio <= 0:
                    n_skipped += 1
                    continue
                # 全部 ×1000 定点化，避免 JSON 浮点尾巴；读时 ÷1000
                rows.append([
                    int(r.datetime),
                    int(round(hongli * 1000)),
                    int(round(peigujia * 1000)),
                    int(round(songgu * 1000)),
                    int(round(peigu * 1000)),
                ])
                n_events += 1
            if rows:
                events[key] = rows

        if not events:
            raise ValueError("gbbq 未解析出任何除权除息记录")

        meta = {
            "parse_version": PARSE_VERSION,
            "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": self.source_stamp(),
            "records_total": int(len(raw)),
            "records_dividend": int(len(div)),
            "codes": len(events),
            "events": n_events,
            "skipped": n_skipped,
        }
        return {"events": events, "meta": meta}

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
        if int(blob.get("meta", {}).get("parse_version") or -1) != PARSE_VERSION:
            return False
        return blob.get("meta", {}).get("source") == self.source_stamp()

    def load(self, force: bool = False) -> Dict[str, Any]:
        """加载（或从缓存读取）除权事件表；源文件缺失时降级为空索引。"""
        if not self.gbbq_path.is_file():
            self.events, self.meta = {}, {
                "source": {"exists": False},
                "reason": "未找到 gbbq",
                "codes": 0,
            }
            return self.meta
        if not force and self._cache_valid():
            try:
                blob = json.loads(self.cache_file.read_text(encoding="utf-8"))
                self.events = {k: v for k, v in blob.get("events", {}).items()}
                self.meta = dict(blob.get("meta") or {})
                self.meta["source_kind"] = "cache"
                return self.meta
            except Exception:
                pass
        built = self.parse()
        self.events, self.meta = built["events"], built["meta"]
        self.meta["source_kind"] = "parsed"
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps({"meta": self.meta, "events": self.events},
                           ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception:
            pass
        return self.meta

    def refresh(self) -> Dict[str, Any]:
        """重新校验 gbbq 指纹，**变了才重建**（供服务层每请求调用）。"""
        return self.load(force=False)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def events_of(self, code: str) -> List[Tuple[int, float, float, float, float]]:
        """某只票的除权事件，返回 ``[(日期, 派息/10股, 配股价, 送转/10股, 配股/10股)]``。"""
        out: List[Tuple[int, float, float, float, float]] = []
        for ev in self.events.get(str(code)) or []:
            out.append((int(ev[0]), ev[1] / 1000.0, ev[2] / 1000.0,
                        ev[3] / 1000.0, ev[4] / 1000.0))
        return out

    def size(self) -> int:
        return len(self.events)


def step_factor(hongli: float, peigujia: float,
                songgu: float, peigu: float,
                prev_close_raw: float) -> Optional[float]:
    """除权日的**单步前复权因子** ``f_d = P_ref / P_raw(d-1)``。

    公式（推导见模块 docstring）::

        f_d = [ 1 - (hongli/10 - peigujia*peigu/10) / P_raw(d-1) ] / ratio
        ratio = 1 + (songgu + peigu)/10

    ``prev_close_raw`` 是除权日**前一日**的**不复权**收盘价 —— 本项目缓存的
    ``close`` 就是不复权，直接传入即可，**不需要任何还原**。

    返回 ``None`` 表示参数不可用（比例非正 / 价格非正 / 结果越界）。
    """
    ratio = 1.0 + (songgu + peigu) / 10.0
    if ratio <= 0 or prev_close_raw <= 0:
        return None
    cash = hongli / 10.0 - peigujia * peigu / 10.0
    f = (1.0 - cash / prev_close_raw) / ratio
    # 因子必须落在 (0, 1]：> 1 说明派息为负或价格口径不对
    if not (0.0 < f <= 1.0 + 1e-9):
        return None
    return min(f, 1.0)


def build_factor_series(
    dates: List[int],
    closes_raw: List[float],
    event_list: List[Tuple[int, float, float, float, float]],
) -> List[float]:
    """给一段**按日期升序**的**不复权**日线，算出逐日**前复权因子**（最新一日 = 1.0）。

    ``event_list`` 是全历史除权事件（``[(日期, 派息/10股, 配股价, 送转/10股, 配股/10股)]``），
    本函数只用到落在 ``dates`` 区间内的那些。

    实现：从最新往回扫，每遇到除权日就把「累积因子」乘上该步的 ``f_d``；
    这样越早的日期累积的因子越小（价格被压缩得越多），符合前复权定义。
    """
    n = len(dates)
    if n == 0:
        return []
    ev_map = {int(e[0]): e for e in event_list}
    out = [1.0] * n
    f = 1.0
    for i in range(n - 1, -1, -1):
        out[i] = f
        # 第 i 日若是除权日 → 第 i-1 日及之前都要再乘一个 f_i
        if i - 1 >= 0:
            ev = ev_map.get(int(dates[i]))
            if ev is not None:
                nf = step_factor(ev[1], ev[2], ev[3], ev[4], closes_raw[i - 1])
                if nf is not None:
                    f = f * nf
    return out

