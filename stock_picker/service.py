"""行情缓存、放量选股、K 线与追踪估值服务。"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .boards import BoardIndex
from .config import CACHE_DIR, Config
from .concepts import ConceptIndex
from .industry import IndustryIndex
from .limits import (
    factor_label,
    is_st_name,
    is_st_or_delisting,
    limit_factor,
    limit_masks,
    seal_shape,
)
from .names import NameIndex
from .shares import FloatShareIndex
from .tdx_reader import (
    BARS_COLUMNS,
    CACHE_DTYPES,
    DAY_DTYPE,
    TdxReader,
    board_of,
    is_a_share,
    normalize_code,
    split_code,
)

BARS_FILE = "bars.parquet"
META_FILE = "bars_meta.json"

#: 全部可选板块（顺序即界面展示顺序）
BOARD_ORDER = ("主板", "创业板", "科创板", "北交所")

#: 默认排除的板块：科创板（688/689）、创业板（300/301）、北交所
DEFAULT_EXCLUDED_BOARDS = ("科创板", "创业板", "北交所")

#: 「每只股票截至某日的 K 线根数」的记忆化上限。全历史下这是按需实时算的
#: （5605 次 searchsorted），结果只有 5605 个 int32，所以可以多留几份。
MAX_BARCOUNT_CACHE = 32

#: 连板判定的回溯窗口（交易日）。A 股历史上最长的连板记录在 30 板上下，
#: 留 60 根足以覆盖任何真实连板链；万一某只股票在窗口内从头到尾都涨停
#: （不可能发生，但要有下界），它的连板数会被截断并由 ``capped`` 标记出来。
STREAK_WINDOW = 60

#: 交易所取值
MARKET_ORDER = ("sh", "sz", "bj")


def as_multi(v) -> List[str]:
    """把 ``"sh,sz"`` / ``["sh","sz"]`` / ``"sh sz"`` / ``"主板、创业板"`` 统一成列表。

    专门拦「裸字符串被当成可迭代对象逐字符拆开」这个坑：``[b for b in "主板"]``
    会得到 ``['主', '板']``，然后静默匹配不到任何板块、返回空结果。
    """
    if v is None:
        return []
    if isinstance(v, str):
        s = v.replace("，", ",").replace("、", ",").replace(";", ",")
        return [t for t in s.replace(" ", ",").split(",") if t]
    return [str(t) for t in v]


def ymd_to_iso(v: int | str) -> str:
    s = str(int(v))
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def iso_to_ymd(v: str) -> int:
    return int(str(v).replace("-", "").replace("/", ""))


def fmt_amount_yi(amount: float) -> float:
    """元 -> 亿元。"""
    return round(float(amount) / 1e8, 3)


def fmt_ts(ts: float) -> Optional[str]:
    """POSIX 时间戳 -> ``YYYY-MM-DD HH:MM:SS``（0 / 空值返回 None）。"""
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


class MarketService:
    """把读取层、缓存、选股、估值串起来的服务对象。"""

    def __init__(
        self,
        config: Config,
        reader: TdxReader,
        names: NameIndex,
        industries: Optional[IndustryIndex] = None,
        concepts: Optional[ConceptIndex] = None,
    ) -> None:
        self.config = config
        self.reader = reader
        self.names = names
        self.cache_dir = CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.industries = industries or IndustryIndex(
            config.get("tdx_dir"), cache_file=CACHE_DIR.parent / "industries.json"
        )
        try:
            self.industries.load(force=False)
        except Exception:
            pass
        self.concepts = concepts or ConceptIndex(
            config.get("tdx_dir"), cache_file=CACHE_DIR.parent / "concepts.json"
        )
        try:
            self.concepts.load(force=False)
        except Exception:
            pass
        self.shares = FloatShareIndex(
            config.get("tdx_dir"), cache_file=CACHE_DIR.parent / "float_shares.json"
        )
        try:
            self.shares.load(force=False)
        except Exception:
            pass
        self.boards = BoardIndex(
            config.get("tdx_dir"), cache_file=CACHE_DIR.parent / "boards.json"
        )
        try:
            self.boards.load(force=False)
        except Exception:
            pass
        self._bars: Optional[pd.DataFrame] = None
        #: 板块指数日线尾部：``{指数代码: (文件 mtime, DataFrame)}`` —— 指数不在
        #: 主缓存里（那里只有 A 股正股），单独读且按 mtime 记忆化。
        self._index_cache: Dict[str, Any] = {}
        #: 全市场唯一交易日（升序 int32），随行情缓存失效
        self._all_dates: Optional[np.ndarray] = None
        #: 「每只股票截至某日的 K 线根数」按需算、按日期记忆化
        self._barcount_cache: "OrderedDict[int, np.ndarray]" = OrderedDict()
        #: 每只股票在长表里的起始行号（按 category 码索引）
        self._group_bounds_cache: Optional[np.ndarray] = None
        #: code -> 板块
        self._board_cache: Dict[str, str] = {}
        self._lock = threading.RLock()
        # 重建缓存可能同时来自「启动预加载」和用户手点「刷新数据」，串行化避免并发写 parquet
        self._refresh_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    @property
    def bars_path(self) -> Path:
        return self.cache_dir / BARS_FILE

    @property
    def meta_path(self) -> Path:
        return self.cache_dir / META_FILE

    def meta(self) -> Dict[str, Any]:
        if self.meta_path.is_file():
            try:
                return json.loads(self.meta_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def has_cache(self) -> bool:
        return self.bars_path.is_file() and self.bars_path.stat().st_size > 0

    @staticmethod
    def _coerce_bars_dtypes(df: pd.DataFrame) -> pd.DataFrame:
        """把长表的列对齐到 :data:`CACHE_DTYPES`。

        新写出的缓存已经是目标 dtype（这一步几乎不做事）；但**旧缓存**（如
        ``cache_bars=260`` 时代落盘的）``code`` 是 object 字符串、价格是
        float64，直接读进来会让 ``df["code"].cat.codes`` 崩掉，所以统一兜一层。
        只对**不一致的列**做 astype，避免白复制一遍 1700 万行。
        """
        if len(df) == 0:
            return df
        for col, dt in CACHE_DTYPES.items():
            if col in df.columns and str(df[col].dtype) != dt:
                df[col] = df[col].astype(dt)
        if "code" in df.columns and not isinstance(df["code"].dtype, pd.CategoricalDtype):
            df["code"] = df["code"].astype("category")
        return df

    def bars(self) -> pd.DataFrame:
        """返回内存中的长表（``code`` + OHLCV + ``pct``），惰性加载。

        保证：``code`` 为 category、按 ``(code, date)`` 升序、dtype 见
        :data:`CACHE_DTYPES`。下面的切片类算法（``_group_bounds`` /
        ``_attach_volume_stats``）都依赖这三点。
        """
        with self._lock:
            if self._bars is None:
                if not self.has_cache():
                    self._bars = pd.DataFrame(
                        columns=[
                            "code", "date", "open", "high", "low", "close",
                            "volume", "amount", "pct",
                        ]
                    )
                else:
                    self._bars = self._coerce_bars_dtypes(
                        pd.read_parquet(self.bars_path)
                    )
            return self._bars

    def invalidate(self) -> None:
        with self._lock:
            self._bars = None
            self._all_dates = None
            self._barcount_cache.clear()
            self._group_bounds_cache = None
            self._index_cache.clear()

    def refresh(
        self, progress: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> Dict[str, Any]:
        """重新读取全市场日线并重建缓存。

        可能被「启动预加载」与手点「刷新数据」同时触发，这里串行化，
        避免两个线程同时写 ``bars.parquet``。
        """
        with self._refresh_lock:
            return self._refresh_impl(progress=progress)

    def _refresh_impl(
        self, progress: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> Dict[str, Any]:
        """真正读取并落盘的部分；外部一律走 :meth:`refresh`。"""
        started = time.perf_counter()
        codes = self.reader.list_codes(a_share_only=True)
        # 行业表 / 概念板块与行情缓存相互独立，但一起刷新可保证口径同步
        # （各表都有源文件 mtime+size 校验，不会重复解析）
        try:
            self.industries.load(force=False)
        except Exception:
            pass
        try:
            self.concepts.load(force=False)
        except Exception:
            pass

        def _cb(done: int, total: int, code: str) -> None:
            if progress:
                progress(
                    {
                        "phase": "read",
                        "done": done,
                        "total": total,
                        "current": code,
                        "message": f"读取 {code}（{done}/{total}）",
                    }
                )

        if progress:
            progress({"phase": "scan", "message": f"发现 {len(codes)} 只 A 股，开始读取…"})

        # cache_bars <= 0 表示不限制，读入 .day 的全部历史
        tail_cfg = int(self.config.get("cache_bars", 0) or 0)
        tail = tail_cfg if tail_cfg > 0 else None
        table, stats = self.reader.read_many_bars(codes, tail=tail, progress=_cb)
        if len(table) == 0:
            return {"ok": False, "reason": "未读取到任何数据", "codes": len(codes)}

        if progress:
            progress({"phase": "compute", "message": "计算涨跌幅并落盘…"})

        # read_many_bars 已保证 (code, date) 有序，但这点很关键（下面所有
        # 「按股票切片」的算法都依赖它），所以还是廉价地校验一遍，真乱序才排。
        codes_arr = table["code"].cat.codes.to_numpy()
        dates_arr = table["date"].to_numpy()
        sorted_ok = bool(
            (codes_arr[1:] >= codes_arr[:-1]).all()
            and not ((dates_arr[1:] < dates_arr[:-1]) & (codes_arr[1:] == codes_arr[:-1])).any()
        )
        if not sorted_ok:
            table = table.sort_values(["code", "date"]).reset_index(drop=True)
            codes_arr = table["code"].cat.codes.to_numpy()

        # pct 用 float64 算完再降精度，避免在 float32 上做除法后累计误差
        close64 = table["close"].astype("float64")
        prev = close64.groupby(codes_arr, sort=False).shift(1)
        table["pct"] = (close64 / prev - 1.0).astype("float32")
        del close64, prev

        tmp = self.bars_path.with_suffix(".parquet.tmp")
        table.to_parquet(tmp, index=False)
        tmp.replace(self.bars_path)

        counts = table.groupby("code", sort=False, observed=True).size()
        last_date = int(table["date"].max())
        first_date = int(table["date"].min())
        meta = {
            "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": round(time.perf_counter() - started, 2),
            "codes_total": len(codes),
            "codes_loaded": int(table["code"].nunique()),
            "rows": int(len(table)),
            "bars_per_code": tail if tail else 0,          # 0 = 全历史
            "bars_per_code_avg": int(round(float(counts.mean()))),
            "bars_per_code_max": int(counts.max()),
            "first_date": ymd_to_iso(first_date),
            "file_mb": round(self.bars_path.stat().st_size / 1e6, 1),
            "last_date": ymd_to_iso(last_date),
            "last_date_int": last_date,
            "reader_mode": self.reader.mode,
            "read_by_mode": stats.by_mode,
            "failed": len(stats.failed),
            "failed_sample": stats.failed[:30],
        }
        self.meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.invalidate()
        if progress:
            progress({"phase": "done", "message": "缓存完成", **meta})
        return {"ok": True, **meta}

    # ------------------------------------------------------------------
    # 启动预加载（把本地已存在的数据全部载入）
    # ------------------------------------------------------------------
    def source_mtime(self) -> float:
        """本地 ``lday`` 目录下最新一个 ``.day`` 的 mtime。

        用 ``os.scandir`` 顺手取 ``DirEntry.stat()``，9741 个文件只遍历不额外 open，
        实测 30ms 量级 —— 比逐文件真读一遍便宜得多。
        """
        newest = 0.0
        for market in ("sh", "sz", "bj"):
            d = self.reader.daily_dir(market)
            if d is None or not d.is_dir():
                continue
            try:
                with os.scandir(d) as it:
                    for entry in it:
                        if not entry.name.endswith(".day"):
                            continue
                        try:
                            m = entry.stat().st_mtime
                        except OSError:
                            continue
                        if m > newest:
                            newest = m
            except OSError:
                continue
        return newest

    def cache_freshness(self) -> Dict[str, Any]:
        """判断行情缓存相对本地通达信数据是否过期。"""
        out: Dict[str, Any] = {
            "exists": self.has_cache(),
            "stale": True,
            "reason": "",
            "cache_built_at": None,
            "cache_last_date": None,
            "cache_mtime": None,
            "source_mtime": None,
        }
        if not self.has_cache():
            out["reason"] = "缓存不存在（首次运行）"
            out["source_mtime"] = fmt_ts(self.source_mtime())
            return out
        meta = self.meta()
        out["cache_built_at"] = meta.get("built_at")
        out["cache_last_date"] = meta.get("last_date")

        # 缓存口径也要对：只比 mtime 的话，把 cache_bars 从 260 改成 0（全历史）
        # 后旧缓存依然「比源数据新」，永远不重建 —— 日期控件就还是只能选最近一年。
        want = int(self.config.get("cache_bars", 0) or 0)
        got = int(meta.get("bars_per_code", -1) or 0)
        if want != got:
            out["reason"] = (
                f"缓存历史深度与配置不一致（缓存 {got or '全历史'} 根 / "
                f"配置 {want or '全历史'} 根），需重建"
            )
            out["source_mtime"] = fmt_ts(self.source_mtime())
            return out

        try:
            cache_mtime = self.bars_path.stat().st_mtime
        except OSError:
            out["reason"] = "缓存不可读"
            return out
        src = self.source_mtime()
        out["cache_mtime"] = fmt_ts(cache_mtime)
        out["source_mtime"] = fmt_ts(src)
        # 容忍 1 秒文件系统时间戳粒度
        if src > cache_mtime + 1:
            out["reason"] = f"本地行情比缓存新（{fmt_ts(src)} > {fmt_ts(cache_mtime)}）"
            return out
        out["stale"] = False
        out["reason"] = "缓存已是最新"
        return out

    def preload(
        self,
        progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        refresh_if_stale: bool = True,
    ) -> Dict[str, Any]:
        """启动即预加载：把本地**已存在**的数据全部载入就绪。

        顺序：名称表 → 行业 → 概念 → 板块目录 → 流通股本 → 行情缓存（缺失/过期则
        重建）→ 交易日索引。每一步都记进 ``steps``，供前端显示与人查证。
        """
        started = time.perf_counter()
        steps: List[Dict[str, Any]] = []

        def _step(name: str, ok: bool = True, **extra: Any) -> Dict[str, Any]:
            item: Dict[str, Any] = {"step": name, "ok": bool(ok), **extra}
            steps.append(item)
            if progress:
                progress(
                    {
                        "phase": "preload",
                        "step": name,
                        "message": f"预加载 {name}…",
                        **extra,
                    }
                )
            return item

        def _try(name: str, fn: Callable[[], Any], summarize: Callable[[Any], Dict[str, Any]]):
            try:
                raw = fn()
                return _step(name, ok=True, **summarize(raw))
            except Exception as exc:  # 单项失败不该阻断整体启动
                return _step(name, ok=False, error=f"{type(exc).__name__}: {exc}")

        _try("名称表", lambda: self.names.load(force=False),
             lambda _: {"size": self.names.size})
        _try("行业分类", lambda: self.industries.load(force=False),
             lambda _: {"codes": self.industries.size, "l2": len(self.industries.l2_names())})
        _try("概念板块", lambda: self.concepts.load(force=False),
             lambda _: {"concepts": self.concepts.concept_count, "codes": self.concepts.size})
        _try("板块目录", lambda: self.boards.load(force=False),
             lambda _: {
                 "boards": self.boards.size,
                 "with_members": int((self.boards.meta or {}).get("boards_with_members") or 0),
             })
        _try("流通股本", lambda: self.shares.load(force=False),
             lambda _: {"codes": int((self.shares.info() or {}).get("size") or 0)})

        # 行情缓存：不存在或过期时重建（这一步最慢，8~10s）。
        # 整段拿住重建锁，并在锁内重新判一次新旧 —— 否则可能与手点「刷新数据」
        # 同时往同一个 .parquet.tmp 里写。
        with self._refresh_lock:
            fresh = self.cache_freshness()
            rebuilt = False
            rebuild_error = None
            if (not fresh["exists"]) or (refresh_if_stale and fresh["stale"]):
                try:
                    res = self._refresh_impl(progress=progress)
                    rebuilt = bool(res.get("ok"))
                    if not rebuilt:
                        rebuild_error = res.get("reason") or "重建失败"
                    _step(
                        "行情缓存",
                        ok=rebuilt,
                        rebuilt=rebuilt,
                        reason=fresh["reason"],
                        **({"error": rebuild_error} if rebuild_error else {}),
                        **{
                            k: res.get(k)
                            for k in ("codes_loaded", "rows", "last_date", "elapsed")
                            if res.get(k) is not None
                        },
                    )
                except Exception as exc:
                    rebuilt = False
                    rebuild_error = f"{type(exc).__name__}: {exc}"
                    _step("行情缓存", ok=False, rebuilt=False,
                          reason=fresh["reason"], error=rebuild_error)
            else:
                _step("行情缓存", ok=True, rebuilt=False, reason=fresh["reason"])

        df = self.bars()
        codes_loaded = int(df["code"].nunique()) if len(df) else 0
        _step("内存行情", ok=len(df) > 0, rows=int(len(df)), codes=codes_loaded,
              last_date=self.latest_date())

        # 均量/根数都不落列了（按需实时算），这里只把「交易日索引」预热好 ——
        # 选股与日期控件都要用，300ms 左右，开页就不用等。
        try:
            dates = self.all_dates()
            self._group_bounds()
            _step(
                "交易日索引",
                ok=len(dates) > 0,
                trade_dates=int(len(dates)),
                first_date=ymd_to_iso(int(dates[0])) if len(dates) else None,
                last_date=ymd_to_iso(int(dates[-1])) if len(dates) else None,
            )
        except Exception as exc:
            _step("交易日索引", ok=False, error=f"{type(exc).__name__}: {exc}")

        meta = self.meta()
        return {
            "ok": len(df) > 0,
            "elapsed": round(time.perf_counter() - started, 2),
            "rebuilt": rebuilt,
            "rebuild_error": rebuild_error,
            "cache_reason": fresh["reason"],
            "codes_loaded": codes_loaded,
            "rows": int(len(df)),
            "last_date": self.latest_date() or meta.get("last_date"),
            "indexes": {
                "names": self.names.size,
                "industries": self.industries.size,
                "concepts": self.concepts.concept_count,
                "boards": self.boards.size,
                "float_shares": int((self.shares.info() or {}).get("size") or 0),
            },
            "steps": steps,
        }

    # ------------------------------------------------------------------
    # 统计辅助
    # ------------------------------------------------------------------
    def _group_bounds(self) -> np.ndarray:
        """每只股票在长表里的**起始行号**（按 category 码索引）。

        依赖 ``bars()`` 按 ``(code, date)`` 有序 —— 重建缓存时已校验过。
        """
        with self._lock:
            if self._group_bounds_cache is not None:
                return self._group_bounds_cache
            df = self.bars()
            codes = df["code"].cat.codes.to_numpy()
            n_cat = len(df["code"].cat.categories)
            out = np.full(n_cat, -1, dtype="int64")
            if len(codes):
                # 同一股票的行走在连续区间里（已按 code 有序），段首即切片起点
                starts = np.flatnonzero(np.r_[True, codes[1:] != codes[:-1]])
                out[codes[starts]] = starts
            self._group_bounds_cache = out
            return out

    def all_dates(self) -> np.ndarray:
        """全市场唯一交易日（升序 int32 数组），随行情缓存失效。"""
        with self._lock:
            if self._all_dates is None:
                df = self.bars()
                if len(df) == 0:
                    self._all_dates = np.empty(0, dtype="int32")
                else:
                    self._all_dates = np.unique(df["date"].to_numpy())
            return self._all_dates

    def _category_bar_counts(self, upto: int) -> np.ndarray:
        """每只股票**截至 ``upto``（含）**的 K 线根数，按 category 码索引。

        这就是原先 ``bar_total`` 想表达的东西，但**不落列**：全历史下
        ``bar_total`` 是 1700 万行的 int64（约 136MB），而这里只在 5605 个
        类别上算一次 searchsorted，结果仅 5605 个 int32（22KB）。
        """
        with self._lock:
            hit = self._barcount_cache.get(upto)
            if hit is not None:
                self._barcount_cache.move_to_end(upto)
                return hit
            df = self.bars()
            n_cat = len(df["code"].cat.categories)
            out = np.zeros(n_cat, dtype="int32")
            if len(df) != 0:
                codes = df["code"].cat.codes.to_numpy()
                dates = df["date"].to_numpy()
                starts = np.flatnonzero(np.r_[True, codes[1:] != codes[:-1]])
                ends = np.r_[starts[1:], len(codes)]
                out[codes[starts]] = [
                    int(np.searchsorted(dates[s:e], upto, "right"))
                    for s, e in zip(starts, ends)
                ]
            self._barcount_cache[upto] = out
            while len(self._barcount_cache) > MAX_BARCOUNT_CACHE:
                self._barcount_cache.popitem(last=False)
            return out

    def _rows_in_window(
        self, sel_dates: np.ndarray, cat_ok: np.ndarray
    ) -> np.ndarray:
        """挑出「日期落在 ``sel_dates`` 且类别通过 ``cat_ok``」的行号。

        这两步都在 numpy 层做行级布尔运算；``cat_ok`` 先在 5605 个类别上算好，
        再用 ``cat_ok[cat_codes]`` 展开成行级掩码 —— 避免对 1700 万行逐行
        调 :func:`split_code` / 查 dict。
        """
        df = self.bars()
        date_ok = np.isin(df["date"].to_numpy(), sel_dates)
        if not cat_ok.all():
            date_ok &= cat_ok[df["code"].cat.codes.to_numpy()]
        return np.flatnonzero(date_ok)

    def _attach_volume_stats(
        self, idx: np.ndarray, window: int
    ) -> pd.DataFrame:
        """取出 ``idx`` 这些行，并**只对这几行**补上 ``ma_vol`` / ``vol_ratio``。

        原先是对整张长表做 ``groupby().transform(rolling())``，全历史下要为
        1700 万行建两个新列（约 136MB）并跑一次全市场 rolling。其实选股只用得上
        目标日期那几行，所以改成：
        「该行在本股票切片内的序号 >= window」才有效，均量取它前面 window 根
        （跨股票不串行，故下界夹到本股票的起始行）。求和范围只有
        ``行数 × window``（约 5605×20 = 11 万个元素），代价可以忽略。

        ：param idx: 行号数组，需按股票分组连续（``bars()`` 已按 code 有序）
        """
        df = self.bars()
        window = int(window)
        sub = df.iloc[idx].copy()
        if len(idx) == 0:
            sub["ma_vol"] = np.empty(0, dtype="float64")
            sub["vol_ratio"] = np.empty(0, dtype="float64")
            return sub
        # 这一层只有最多「股票数 × 回看天数」行，把 code 还原成普通字符串，
        # 下游的 map / isin / itertuples 就都跟以前一样是 str，没有 categorical
        # 的各种边界情况（categorical 只用来省那 1700 万行的内存）。
        sub["code"] = sub["code"].astype(object)

        codes = df["code"].cat.codes.to_numpy()
        volume = df["volume"].to_numpy()          # uint32，「股」
        row_code = codes[idx]
        group_start = self._group_bounds()[row_code]

        ma_vol = np.full(len(idx), np.nan, dtype="float64")
        # idx 已按 code 升序 → 同一股票的行在 idx 里也是连续的，按段处理
        seg_start = np.flatnonzero(np.r_[True, row_code[1:] != row_code[:-1]])
        seg_end = np.r_[seg_start[1:], len(idx)]
        for s, e in zip(seg_start, seg_end):
            base = int(group_start[s])            # 本股票切片的起始行
            for t in range(int(s), int(e)):
                i = int(idx[t])
                if i - base >= window:            # 前面凑不齐 window 根 → 均量无效
                    lo = max(base, i - window)
                    ma_vol[t] = float(volume[lo:i].sum()) / window

        sub["ma_vol"] = ma_vol
        vol64 = sub["volume"].to_numpy(dtype="float64")
        with np.errstate(invalid="ignore", divide="ignore"):
            sub["vol_ratio"] = np.where(ma_vol > 0, vol64 / ma_vol, np.nan)
        return sub

    def board_map(self, codes: Sequence[str]) -> Dict[str, str]:
        """``代码 -> 板块``（带缓存；板块由代码前缀唯一决定，不会失效）。"""
        out: Dict[str, str] = {}
        missing: List[str] = []
        for c in codes:
            b = self._board_cache.get(c)
            if b is None:
                missing.append(c)
            else:
                out[c] = b
        if missing:
            for c in missing:
                b = board_of(c)
                self._board_cache[c] = b
                out[c] = b
        return out

    def latest_date(self) -> Optional[str]:
        meta = self.meta()
        if meta.get("last_date"):
            return str(meta["last_date"])
        df = self.bars()
        if len(df) == 0:
            return None
        return ymd_to_iso(int(df["date"].max()))

    def trade_dates(self, n: int = 0) -> List[str]:
        """本地数据覆盖的交易日（升序 ISO）。

        :param n: 只取最后 n 个；``<= 0`` 表示全部（全历史下约 8000 个）。
            前端「指定日期」控件的可选下界就取这里的第一个值，所以默认
            **返回全部**，否则日历会把更早的年份灰掉。
        """
        dates = self.all_dates()
        if n and n > 0:
            dates = dates[-n:]
        return [ymd_to_iso(int(d)) for d in dates]

    def resolve_industries(self, params: Dict[str, Any]) -> List[str]:
        """解析二级行业白名单（``params["industries"]``，可为字符串或列表）。"""
        raw = params.get("industries")
        if raw is None:
            return []
        if isinstance(raw, str):
            raw = [x for x in raw.replace("，", ",").split(",") if x.strip()]
        if not isinstance(raw, (list, tuple, set)):
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    def resolve_concepts(self, params: Dict[str, Any]) -> List[str]:
        """解析概念白名单（``params["concepts"]``，可为逗号分隔字符串或列表）。"""
        raw = params.get("concepts")
        if raw is None:
            return []
        if isinstance(raw, str):
            raw = [x for x in raw.replace("，", ",").split(",") if x.strip()]
        if not isinstance(raw, (list, tuple, set)):
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    def resolve_boards(self, params: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
        """确定本次选股纳入的板块。

        优先级：``params["boards"]``（白名单）> ``params["exclude_boards"]``
        > 配置项 ``screen.exclude_boards``（默认剔除科创板 / 创业板）。
        """
        req = params.get("boards")
        if isinstance(req, list):
            # 显式传入白名单（含空列表）以调用方为准，不静默回退到默认值
            wanted = {str(b) for b in req}
            return [b for b in BOARD_ORDER if b in wanted]
        excl = params.get("exclude_boards")
        if excl is None:
            excl = cfg.get("exclude_boards", list(DEFAULT_EXCLUDED_BOARDS))
        excl_set = {str(b) for b in (excl or [])}
        return [b for b in BOARD_ORDER if b not in excl_set]

    # ------------------------------------------------------------------
    # 选股
    # ------------------------------------------------------------------
    def screen(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """放量选股：成交量 >= N 倍「前 M 日均量」。

        :param params: ``ma_window`` / ``volume_ratio`` / ``lookback_days`` /
            ``date`` / ``min_bars`` / ``max_results`` / ``min_amount`` /
            ``exclude_st`` / ``markets`` / ``boards`` / ``exclude_boards`` /
            ``industries``（二级行业白名单，字符串或列表）/
            ``concepts``（概念白名单，字符串或列表）/ ``concept_mode``
            （``any`` 命中任一即保留，``all`` 需全部命中）
        """
        cfg = self.config.section("screen")
        plan_cfg = self.config.section("plan")
        t_start = time.perf_counter()
        ma_window = int(params.get("ma_window") or cfg.get("ma_window", 20))
        ratio = float(params.get("volume_ratio") or cfg.get("volume_ratio", 2.0))
        lookback = int(params.get("lookback_days") or cfg.get("lookback_days", 1))
        min_bars = int(params.get("min_bars") or cfg.get("min_bars", 30))
        max_results = int(params.get("max_results") or cfg.get("max_results", 300))
        min_amount = float(params.get("min_amount") or cfg.get("min_amount", 0.0) or 0.0)
        # 流通市值上限（亿元）；0 / 空 = 不限
        raw_mcap = params.get("max_float_mcap")
        if raw_mcap is None:
            raw_mcap = cfg.get("max_float_mcap", 0.0)
        max_float_mcap = float(raw_mcap or 0.0)
        exclude_st = params.get("exclude_st")
        if exclude_st is None:
            exclude_st = cfg.get("exclude_st", True)
        markets = params.get("markets") or cfg.get("markets") or ["sh", "sz", "bj"]
        markets = [str(m).lower() for m in markets]
        boards = self.resolve_boards(params, cfg)
        industries = self.resolve_industries(params)
        concepts = self.resolve_concepts(params)
        concept_mode = str(params.get("concept_mode") or "any").lower()
        if concept_mode not in ("any", "all"):
            concept_mode = "any"
        target_date = params.get("date")
        sell_profit = float(params.get("sell_profit") or plan_cfg.get("sell_profit", 0.045))
        buy_discount = float(params.get("buy_discount") or plan_cfg.get("buy_discount", 0.0))

        df = self.bars()
        if len(df) == 0:
            return {
                "ok": False,
                "reason": "本地行情缓存为空，请先点『刷新数据』",
                "rows": [],
                "matched": 0,
            }
        if not boards:
            return {
                "ok": False,
                "reason": "未勾选任何板块，请至少选择一个",
                "rows": [],
                "matched": 0,
            }

        # --- 1) 交易所 / 板块：在 5605 个类别上算掩码 -------------------
        cats = df["code"].cat.categories
        n_cat = len(cats)
        cat_ok = np.ones(n_cat, dtype=bool)
        if markets:
            mset = set(markets)
            cat_ok &= np.fromiter(
                (split_code(c)[1] in mset for c in cats), dtype=bool, count=n_cat
            )
        if len(boards) < len(BOARD_ORDER):
            bmap = self.board_map(list(cats))
            allowed = set(boards)
            cat_ok &= np.fromiter(
                (bmap.get(c) in allowed for c in cats), dtype=bool, count=n_cat
            )

        total_universe = int(df["code"].nunique())
        total_scanned = int(cat_ok.sum())

        # --- 2) 日期：以「全市场交易日」为锚 ---------------------------
        # 必须锚在全市场交易日上，否则长期停牌/退市股会拿自己的最后一根 K 线
        # 冒充当期信号（例如数据停在 2025-09-26 却出现在今日结果里）
        all_dates = self.all_dates()
        if target_date:
            d = iso_to_ymd(str(target_date))
            if not np.isin(d, all_dates):
                return {
                    "ok": False,
                    "reason": f"{ymd_to_iso(d)} 不是本地数据里的交易日"
                    f"（可选范围 {ymd_to_iso(int(all_dates[0]))} ~ "
                    f"{ymd_to_iso(int(all_dates[-1]))}）",
                    "rows": [],
                    "matched": 0,
                    "date_out_of_range": True,
                    "date_min": ymd_to_iso(int(all_dates[0])),
                    "date_max": ymd_to_iso(int(all_dates[-1])),
                }
            sel_dates = np.array([d], dtype="int32")
            window_desc = f"指定日期 {ymd_to_iso(d)}"
        else:
            sel_dates = all_dates[-max(1, lookback):].astype("int32", copy=False)
            latest = int(all_dates[-1])
            window_desc = (
                f"最新交易日 {ymd_to_iso(latest)}"
                if lookback <= 1
                else f"最近 {lookback} 个交易日（{ymd_to_iso(int(sel_dates[0]))} ~ {ymd_to_iso(latest)}）"
            )

        # --- 3) 行级取数 + 只对命中的这几行算均量 ----------------------
        idx = self._rows_in_window(sel_dates, cat_ok)
        if min_bars > 0 and len(idx):
            # 截至锚点日的 K 线根数（按需实时算，不落列）
            counts = self._category_bar_counts(int(sel_dates[-1]))
            idx = idx[counts[df["code"].cat.codes.to_numpy()[idx]] >= min_bars]
        work = self._attach_volume_stats(idx, ma_window)
        if min_amount > 0 and len(work):
            work = work[work["amount"] >= min_amount]

        hit = work[work["vol_ratio"] >= ratio]
        hit = hit[hit["ma_vol"].notna() & (hit["volume"] > 0)]

        if exclude_st:
            names_map = {c: self.names.get(c) for c in hit["code"].unique()}
            hit = hit[
                ~hit["code"].map(lambda c: is_st_or_delisting(names_map.get(c) or ""))
            ]

        # 流通市值过滤（亿元）= 流通股本 × 当日收盘价 ÷ 1e8
        # 股本来自本地 gbbq（股本变迁），与行情同源、无需联网。
        # 放在行业 / 概念过滤之前，是为了让下面的「分布基数」已经含市值条件。
        mcap_missing = 0
        if max_float_mcap > 0 and len(hit):
            share_map = {c: self.shares.get(c) for c in hit["code"].unique()}
            float_shares = hit["code"].map(share_map).astype("float64")
            mcap = float_shares * hit["close"] / 1e8
            mcap_missing = int(mcap.isna().sum())
            keep_mask = mcap.notna() & (mcap < max_float_mcap)
            hit = hit[keep_mask]

        # 行业 / 概念分布卡的统计基数 = 除「行业、概念」筛选本身以外的全部条件命中集。
        # 若改用过滤后的集合统计，一点选行业，分布就只剩那一个行业、其余 chip 全部
        # 消失 —— 既没法学着切换，也看不出该行业在整体中的占比。基数与行业/概念
        # 筛选无关，所以同一轮筛选下分布卡恒定不变，点击只是收敛结果表。
        summary_codes = hit["code"].drop_duplicates().tolist()

        # 二级行业过滤（通达信研究行业）
        industry_missing = 0
        if industries:
            wanted = set(industries)
            l2map = {c: self.industries.get_l2(c) for c in hit["code"].unique()}
            keep = hit["code"].map(l2map)
            industry_missing = int(keep.isna().sum())
            hit = hit[keep.isin(wanted)]

        # 概念过滤（通达信概念板块，本地 infoharbor_block.dat）
        concept_missing = 0
        if concepts:
            uniq = hit["code"].unique().tolist()
            keep_codes = self.concepts.filter_codes(uniq, concepts, mode=concept_mode)
            concept_missing = sum(1 for c in uniq if not self.concepts.get(c))
            hit = hit[hit["code"].isin(keep_codes)]

        hit = hit.sort_values("vol_ratio", ascending=False)

        # 每只股票只保留最近一次信号
        hit = hit.sort_values(["code", "date"]).drop_duplicates(subset=["code"], keep="last")
        hit = hit.sort_values("vol_ratio", ascending=False)
        total_hits = int(len(hit))
        # 行业 / 概念分布按「基数」统计：不受行业/概念筛选影响，也不是截取后的前 N 只
        industry_summary = self.industries.summary(summary_codes)
        concept_summary = self.concepts.summary(summary_codes)
        hit = hit.head(max_results)

        ind_cols: Dict[str, Dict[str, Optional[str]]] = {
            c: (self.industries.get(c) or {"l1": None, "l2": None, "l3": None})
            for c in hit["code"].unique()
        }
        concept_cols: Dict[str, List[str]] = {
            c: self.concepts.get(c, sort_by_size=True) for c in hit["code"].unique()
        }

        rows: List[Dict[str, Any]] = []
        for r in hit.itertuples(index=False):
            code = str(r.code)
            close = float(r.close)
            ind = ind_cols.get(code) or {"l1": None, "l2": None, "l3": None}
            row_concepts = concept_cols.get(code) or []
            float_shares = self.shares.get(code)
            float_mcap = self.shares.mcap_yi(code, close)
            rows.append(
                {
                    "code": code,
                    "symbol": code.split(".")[0],
                    "name": self.names.get(code),
                    "market": split_code(code)[1].upper(),
                    "board": board_of(code),
                    "industry_l1": ind["l1"],
                    "industry_l2": ind["l2"],
                    "industry_l3": ind["l3"],
                    "concepts": row_concepts,
                    "concept_n": len(row_concepts),
                    "signal_date": ymd_to_iso(int(r.date)),
                    "close": round(close, 3),
                    "open": round(float(r.open), 3),
                    "high": round(float(r.high), 3),
                    "low": round(float(r.low), 3),
                    "pct_change": None if pd.isna(r.pct) else round(float(r.pct), 5),
                    "volume": round(float(r.volume), 0),
                    "volume_hand": round(float(r.volume) / 100.0, 0),
                    "ma_volume": round(float(r.ma_vol), 0),
                    "vol_ratio": round(float(r.vol_ratio), 3),
                    "amount": round(float(r.amount), 0),
                    "amount_yi": fmt_amount_yi(r.amount),
                    "float_shares": None if float_shares is None else round(float_shares, 0),
                    "float_shares_yi": (
                        None if float_shares is None else round(float_shares / 1e8, 3)
                    ),
                    "float_mcap_yi": None if float_mcap is None else round(float_mcap, 2),
                    "total_mcap_yi": (
                        None
                        if not self.shares.totals.get(code)
                        else round(self.shares.totals[code] * close / 1e8, 2)
                    ),
                    "buy_price": round(close * (1.0 - buy_discount), 3),
                    "sell_price": round(close * (1.0 + sell_profit), 3),
                    "sell_profit": sell_profit,
                }
            )

        return {
            "ok": True,
            "conditions": [
                f"成交量 ≥ {ratio:g} × 前 {ma_window} 日均量",
                window_desc,
                f"至少 {min_bars} 根 K 线",
                f"成交额 ≥ {fmt_amount_yi(min_amount)} 亿元" if min_amount > 0 else "不限成交额",
                "剔除 ST / 退市" if exclude_st else "包含 ST",
                f"板块：{'/'.join(boards)}"
                + (f"（已剔除 {'/'.join(b for b in BOARD_ORDER if b not in boards)}）"
                   if len(boards) < len(BOARD_ORDER) else ""),
                f"交易所：{'/'.join(m.upper() for m in markets)}",
                (
                    f"二级行业限定：{'/'.join(industries)}"
                    if industries
                    else "不限二级行业（通达信研究行业）"
                ),
                (
                    f"概念限定：{'/'.join(concepts)}"
                    f"（{'同时命中全部' if concept_mode == 'all' else '命中任一'}）"
                    if concepts
                    else f"不限概念（通达信概念板块，本地收录 {self.concepts.concept_count} 个）"
                ),
                (
                    f"流通市值 < {max_float_mcap:g} 亿元"
                    if max_float_mcap > 0
                    else "不限流通市值"
                ),
                f"卖出价 = 放量日收盘价 × (1 + {sell_profit:.1%})",
                "使用本地通达信日线（不复权）",
            ],
            "params": {
                "ma_window": ma_window,
                "volume_ratio": ratio,
                "lookback_days": lookback,
                "date": target_date,
                "buy_discount": buy_discount,
                "sell_profit": sell_profit,
                "boards": boards,
                "exclude_boards": [b for b in BOARD_ORDER if b not in boards],
                "markets": markets,
                "industries": industries,
                "concepts": concepts,
                "concept_mode": concept_mode,
                "max_float_mcap": max_float_mcap,
            },
            "industry_summary": industry_summary["groups"],
            "industry_mapped": industry_summary["mapped"],
            "industry_unmapped": industry_summary["unmapped"],
            "industry_unmapped_sample": industry_summary["unmapped_sample"],
            "industry_filtered_missing": industry_missing,
            # 分布卡（行业 / 概念）的统计基数：不受行业/概念筛选影响
            "summary_base": len(summary_codes),
            "concept_summary": concept_summary["groups"],
            "concept_mapped": concept_summary["mapped"],
            "concept_unmapped": concept_summary["unmapped"],
            "concept_unmapped_sample": concept_summary["unmapped_sample"],
            "concept_tags": concept_summary["tags"],
            "concept_avg": concept_summary["avg"],
            "concept_filtered_missing": concept_missing,
            "concept_total": self.concepts.concept_count,
            "mcap_filtered_missing": mcap_missing,
            "max_float_mcap": max_float_mcap,
            "total_scanned": total_scanned,
            "total_universe": total_universe,
            "matched": len(rows),
            "total_hits": total_hits,
            "truncated": total_hits > len(rows),
            "elapsed": round(float(self.meta().get("elapsed", 0.0)), 2),
            "screen_elapsed": round(time.perf_counter() - t_start, 3),
            "last_date": self.latest_date(),
            "rows": rows,
            "rows_count": len(rows),
        }

    # ------------------------------------------------------------------
    # 连板梯队
    # ------------------------------------------------------------------
    def _streak_window(self, anchor: int, window: int) -> Dict[str, np.ndarray]:
        """每只股票「截至 ``anchor`` 日的最后 ``window`` 根」在长表里的行号。

        只需要「股票数 × 窗口」量级的行（5605 × 60 ≈ 34 万），所以全程在类别层
        完成，不碰 1700 万行的主表。

        ``rows`` 按「category 码升序 → 日期升序」排列，**同一只股票的行走在连续
        段里** —— 下面算「前一根收盘」和「连板数」都依赖这个性质，所以每段内
        最后一行 ``pos == seg_len - 1`` 就是「截至 anchor 该股的最新一根」。
        """
        df = self.bars()
        n_cat = len(df["code"].cat.categories)
        starts = self._group_bounds()
        counts = self._category_bar_counts(anchor)
        has = (starts >= 0) & (counts > 0)
        ends = np.where(has, starts + counts, 0)
        begin = np.maximum(starts, ends - window)

        cols = np.arange(window, dtype="int64")
        # (n_cat, window) 的绝对行号，**右对齐**（每段末尾落在同一列）
        off = ends[:, None] - window + cols[None, :]
        keep = has[:, None] & (off >= begin[:, None]) & (off < ends[:, None])
        rows = off[keep]
        seg_len = keep.sum(axis=1)
        cat = np.repeat(np.arange(n_cat, dtype="int64"), seg_len)
        seg_len_row = np.repeat(seg_len, seg_len)
        pos = np.arange(rows.size, dtype="int64") - np.repeat(
            np.cumsum(seg_len) - seg_len, seg_len
        )
        return {"rows": rows, "cat": cat, "pos": pos, "seg_len": seg_len_row,
                "starts": starts}

    def streaks(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """连板梯队：把指定交易日的涨停个股按连板高度分层。

        「连板数」= 从该交易日往回连续涨停的天数（首板 = 1）。涨停判定口径见
        :mod:`stock_picker.limits`（含各板块涨跌幅、上市首日剔除、已知边界）。

        **ST 5% 口径默认关闭**（``st_limit=False``）。理由是本机数据的实测结论：
        抽取 144 只名称含 ST 的主板个股，其中 142 只都存在「日内最高价 > 前收 × 1.05」
        的交易日（占比 8%~37%），``high/prev`` 的 p99.9 几乎全部落在 **1.10** —— 说明
        这批本地行情里主板 ST 股的涨跌幅限制**也是 10%**，套用 5% 会把涨幅 5%~10% 的
        普通交易日错判成涨停（``2026-06-01`` 一天就多出 40 只）。兄弟应用
        ``stock_watch/views/streaks.py`` 的口径也写明「ST 股按常规板近似」。
        ``st_limit=True`` 仍保留为开关，供数据确实带 5% 带宽的行情源使用。

        :param params: ``date``（锚点交易日，空 = 最新）/ ``min_streak``（梯队与
            明细只保留 N 板及以上，指标不受影响）/ ``markets`` / ``boards`` /
            ``st_limit``（是否按 ST 5% 判定，**默认关**）/ ``max_rows`` / ``window``
        """
        t_start = time.perf_counter()
        cfg = self.config.section("screen")
        window = max(10, int(params.get("window") or STREAK_WINDOW))
        min_streak = max(1, int(params.get("min_streak") or 1))
        max_rows = max(1, int(params.get("max_rows") or 500))
        st_limit = params.get("st_limit")
        st_limit = False if st_limit is None else bool(st_limit)
        markets = params.get("markets") or cfg.get("markets") or list(MARKET_ORDER)
        markets = [m.lower() for m in as_multi(markets)]
        unknown_m = [m for m in markets if m not in MARKET_ORDER]
        if unknown_m or not markets:
            return {"ok": False, "rows": [], "ladder": [],
                    "reason": f"交易所取值非法 {unknown_m or markets}"
                              f"（可选：{'/'.join(MARKET_ORDER)}）"}
        boards = params.get("boards")
        if boards is None:
            boards = list(BOARD_ORDER)   # 连板梯队默认看全部板块（涨停就是涨停）
        else:
            boards = as_multi(boards)
        if not boards:
            return {"ok": False, "reason": "未选择任何板块", "rows": [], "ladder": []}
        unknown_b = [b for b in boards if b not in BOARD_ORDER]
        if unknown_b:
            return {"ok": False, "rows": [], "ladder": [],
                    "reason": f"未知板块 {unknown_b}（可选：{'/'.join(BOARD_ORDER)}）"}

        df = self.bars()
        if len(df) == 0:
            return {"ok": False, "reason": "本地行情缓存为空，请先点『刷新数据』",
                    "rows": [], "ladder": []}
        all_dates = self.all_dates()
        if len(all_dates) == 0:
            return {"ok": False, "reason": "没有可用交易日", "rows": [], "ladder": []}

        date_min = ymd_to_iso(int(all_dates[0]))
        date_max = ymd_to_iso(int(all_dates[-1]))
        raw_date = params.get("date")
        if raw_date:
            d = iso_to_ymd(str(raw_date))
            if not np.isin(d, all_dates):
                return {
                    "ok": False, "rows": [], "ladder": [],
                    "reason": f"{ymd_to_iso(d)} 不是本地数据里的交易日"
                              f"（可选范围 {date_min} ~ {date_max}）",
                    "date_out_of_range": True, "date_min": date_min, "date_max": date_max,
                }
            anchor = int(d)
        else:
            anchor = int(all_dates[-1])

        at = int(np.searchsorted(all_dates, anchor))
        prev_date = int(all_dates[at - 1]) if at > 0 else None

        # --- 1) 交易所 / 板块掩码（在 5605 个类别上算，不逐行） -----------
        cats = df["code"].cat.categories
        n_cat = len(cats)
        cat_ok = np.ones(n_cat, dtype=bool)
        if markets:
            mset = set(markets)
            cat_ok &= np.fromiter(
                (split_code(c)[1] in mset for c in cats), dtype=bool, count=n_cat
            )
        if len(boards) < len(BOARD_ORDER):
            bmap = self.board_map(list(cats))
            allowed = set(boards)
            cat_ok &= np.fromiter(
                (bmap.get(c) in allowed for c in cats), dtype=bool, count=n_cat
            )

        # --- 2) 取窗口 + 涨停判定 ---------------------------------------
        win = self._streak_window(anchor, window)
        rows, cat, pos, seg_len = win["rows"], win["cat"], win["pos"], win["seg_len"]
        if rows.size == 0:
            return {"ok": False, "reason": "窗口内没有行情数据", "rows": [], "ladder": []}

        sub = df.iloc[rows]
        close = sub["close"].to_numpy(dtype="float64")
        open_ = sub["open"].to_numpy(dtype="float64")
        high = sub["high"].to_numpy(dtype="float64")
        low = sub["low"].to_numpy(dtype="float64")
        volume = sub["volume"].to_numpy(dtype="float64")
        amount = sub["amount"].to_numpy(dtype="float64")
        dates = sub["date"].to_numpy()

        # 基准收盘价 = 同一只股票窗口内的前一根收盘；段首没有前收（不判定）
        prev = np.full(rows.size, np.nan)
        if rows.size > 1:
            same_seg = cat[1:] == cat[:-1]
            prev[1:] = np.where(same_seg, close[:-1], np.nan)

        # 上市首日 = 该行就是这只股票在长表里的第一根（首日无涨跌幅限制）
        is_ipo = rows == win["starts"][cat]

        # 涨跌幅因子按类别算一次，再展开到行；名称用于识别主板 ST（5%）
        cat_names = [self.names.get(c) or "" for c in cats]
        factors = np.fromiter(
            (limit_factor(c, cat_names[i], st_limit) for i, c in enumerate(cats)),
            dtype="int64", count=n_cat,
        )
        factor_row = factors[cat]

        lu, tu, ld = limit_masks(close, high, low, prev, factor_row, is_ipo)
        seal = seal_shape(lu, open_, low, prev, factor_row)

        # --- 3) 连板数：全向量化，不逐股循环 ------------------------------
        # 「截至本行的连板数」= 本行位置 − 最近一个非涨停位置。段首也视为分界
        # （它前面还有不在窗口内的历史，本来就算不出完整连板数）。
        gp = np.arange(rows.size, dtype="int64")
        boundary = (~lu) | (pos == 0)
        marked = np.where(boundary, gp, -1)
        last_boundary = np.maximum.accumulate(marked)
        streak_all = np.where(boundary, 0, gp - last_boundary)

        seg_tail = pos == seg_len - 1
        in_pool = cat_ok[cat]
        # 窗口被连板链撑满时，连板数会被截断（现实中不会发生，标记出来以免误导）
        capped = seg_tail & lu & (streak_all >= seg_len - 1) & (seg_len >= window)

        on_anchor = seg_tail & (dates == anchor)
        hit = on_anchor & in_pool & (streak_all > 0)
        # 昨日涨停数：只按日期匹配即可 —— 停牌股在 prev_date 没有行，自动落选。
        # （不能用 seg_tail 判：段尾是 anchor 那天，prev_date 是它的前一行。）
        if prev_date is not None:
            prev_n = int(((dates == prev_date) & in_pool & (streak_all > 0)).sum())
        else:
            prev_n = 0

        idx_hit = np.flatnonzero(hit)
        code_arr = np.asarray(cats, dtype=object)[cat[idx_hit]]

        # --- 4) 组装明细 -------------------------------------------------
        uniq_codes = list(dict.fromkeys(code_arr.tolist()))
        ind_cols = {
            c: (self.industries.get(c) or {"l1": None, "l2": None, "l3": None})
            for c in uniq_codes
        }
        concept_cols = {c: self.concepts.get(c, sort_by_size=True) for c in uniq_codes}
        name_cols = {c: self.names.get(c) for c in uniq_codes}

        out: List[Dict[str, Any]] = []
        for k in idx_hit:
            k = int(k)
            code = str(cats[cat[k]])
            cl = float(close[k])
            pv = float(prev[k])
            pct = None if not np.isfinite(pv) or pv <= 0 else cl / pv - 1.0
            ind = ind_cols.get(code) or {"l1": None, "l2": None, "l3": None}
            cpts = concept_cols.get(code) or []
            fsh = self.shares.get(code)
            nm = name_cols.get(code) or ""
            mcap = self.shares.mcap_yi(code, cl)
            st = int(streak_all[k])
            out.append({
                "code": code,
                "symbol": code.split(".")[0],
                "name": nm or code.split(".")[0],
                "market": split_code(code)[1].upper(),
                "board": board_of(code),
                "streak": st,
                "streak_label": f"{st}连板" if st > 1 else "首板",
                "seal": str(seal[k]),
                "limit_factor": int(factor_row[k]),
                "limit_rate": factor_label(int(factor_row[k])),
                "is_st": is_st_name(nm),
                "is_delisting": "退" in nm,
                "date": ymd_to_iso(int(dates[k])),
                "close": round(cl, 2),
                "open": round(float(open_[k]), 2),
                "high": round(float(high[k]), 2),
                "low": round(float(low[k]), 2),
                "pct_change": None if pct is None else round(float(pct), 5),
                "amount": round(float(amount[k]), 0),
                "amount_yi": fmt_amount_yi(amount[k]),
                "volume_hand": round(float(volume[k]) / 100.0, 0),
                "float_shares_yi": None if fsh is None else round(fsh / 1e8, 3),
                "float_mcap_yi": None if mcap is None else round(float(mcap), 2),
                "industry_l1": ind["l1"],
                "industry_l2": ind["l2"],
                "industry_l3": ind["l3"],
                "concepts": cpts,
                "concept_n": len(cpts),
                "capped": bool(capped[k]),
            })
        # 连板高度降序 → 同高度按成交额降序（成交额大的更可能是主线）
        out.sort(key=lambda r: (-r["streak"], -(r["amount_yi"] or 0.0)))

        # --- 5) 指标 / 梯队 / 分布 ---------------------------------------
        groups: Dict[int, List[Dict[str, Any]]] = {}
        for r in out:
            groups.setdefault(r["streak"], []).append(r)
        hist = [
            {"streak": s, "n": len(v), "label": ("首板" if s == 1 else f"{s}连板"),
             "amount_yi": round(sum((x["amount_yi"] or 0.0) for x in v), 2)}
            for s, v in sorted(groups.items(), reverse=True)
        ]
        max_streak = max(groups) if groups else 0
        top = groups.get(max_streak, [])
        n_limit_up = len(out)
        promote_n = sum(len(v) for s, v in groups.items() if s >= 2)

        ind_sum = self.industries.summary(uniq_codes)
        cpt_sum = self.concepts.summary(uniq_codes)

        kept = [r for r in out if r["streak"] >= min_streak]
        ladder = [
            {
                "streak": h["streak"],
                "label": h["label"],
                "n": h["n"],
                "amount_yi": h["amount_yi"],
                "codes": [r["code"] for r in groups.get(h["streak"], [])],
            }
            for h in hist if h["streak"] >= min_streak
        ]

        conditions = [
            f"连板数 = 从 {ymd_to_iso(anchor)} 往回连续涨停的天数（首板 = 1）",
            "涨停价 = round(前收盘 × (1 ± 涨跌幅))，按「分」整数计算",
            "涨跌幅：主板 10% / 创业板·科创板 20% / 北交所 30%"
            + ("，主板 ST 5%" if st_limit else "（ST 按常规板近似，未启用 5% 口径）"),
            "基准价 = 前一根 K 线收盘（本地数据不复权）",
            f"板块：{'/'.join(boards)}",
            f"交易所：{'/'.join(m.upper() for m in markets)}",
            f"梯队与明细只保留 {min_streak} 板及以上" if min_streak > 1 else "显示全部连板高度",
        ]
        warnings: List[str] = []
        if capped.sum():
            warnings.append(
                f"有 {int(capped.sum())} 只股票在 {window} 根窗口内全为涨停，"
                "其连板数可能被窗口截断"
            )
        named = sum(1 for n in cat_names if n)
        if named < len(cat_names):
            ratio = named / max(1, len(cat_names))
            tip = (
                f"本地名称表覆盖 {named}/{len(cat_names)}（{ratio:.1%}）："
                "缺名称的标的会显示为代码"
            )
            if ratio < 0.8:
                tip += "；ST 股可能识别不出，涨跌幅口径会偏"
            warnings.append(tip)
        warnings.append(
            "除权日的前收用未复权收盘价，可能漏判当天涨停；创业/科创板上市前 5 日、"
            "北交所首日均无涨跌幅限制，本口径只剔除上市首日"
        )
        st_n = sum(1 for n in cat_names if is_st_name(n))
        if st_limit and st_n:
            warnings.append(
                f"已按 ST 5% 判定，涉及 {st_n} 只名称含 ST 的标的；本机行情实测这批标的的"
                "日内带宽多为 10%，若数据源并不含 5% 限制，会多算涨停 —— 建议关闭该开关"
            )
        elif st_n and not st_limit:
            warnings.append(
                f"{st_n} 只名称含 ST 的标的按常规板块（10%/20%/30%）判定涨跌停，"
                "与兄弟应用 stock_watch 的口径一致"
            )

        return {
            "ok": True,
            "date": ymd_to_iso(anchor),
            "anchor": anchor,
            "prev_date": ymd_to_iso(prev_date) if prev_date else None,
            "weekday": "一二三四五六日"[datetime.strptime(
                ymd_to_iso(anchor), "%Y-%m-%d").weekday()],
            "metrics": {
                "limit_up": n_limit_up,
                "touched_up": int((tu & on_anchor & in_pool).sum()),
                "limit_down": int((ld & on_anchor & in_pool).sum()),
                "first": len(groups.get(1, [])),
                "second": len(groups.get(2, [])),
                "high3": promote_n - len(groups.get(2, [])),
                "promote_n": promote_n,
                "max_streak": max_streak,
                "max_streak_name": top[0]["name"] if top else None,
                "max_streak_code": top[0]["code"] if top else None,
                "prev_limit_up": prev_n,
                "promotion": (round(promote_n / prev_n, 4) if prev_n else None),
                "one_word": sum(1 for r in out if r["seal"] == "一字板"),
                "st_count": sum(1 for r in out if r["is_st"]),
            },
            "hist": hist,
            "ladder": ladder,
            "rows": kept[:max_rows],
            "rows_count": len(kept),
            "truncated": len(kept) > max_rows,
            "min_streak": min_streak,
            "industry_summary": ind_sum["groups"],
            "industry_unmapped": ind_sum["unmapped"],
            "concept_summary": cpt_sum["groups"],
            "concept_unmapped": cpt_sum["unmapped"],
            "concept_avg": cpt_sum["avg"],
            "summary_base": len(uniq_codes),
            "conditions": conditions,
            "warnings": warnings,
            "date_min": date_min,
            "date_max": date_max,
            "window": window,
            "st_limit": st_limit,
            "boards": boards,
            "markets": markets,
            "elapsed": round(time.perf_counter() - t_start, 3),
        }

    # ------------------------------------------------------------------
    # 二级行业 × 概念 交集
    # ------------------------------------------------------------------
    def intersect(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """求「二级行业板块 ∩ 概念板块」的交集个股。

        与 :meth:`screen` 的区别：这里只做**板块归属的集合运算**，不涉及均量
        窗口，所以取数只用得上锚点日那一行；全部代价是 5605 行的字典查询，
        实测 ~0.05s。

        集合定义（都限定在「锚点日有行情、且通过轻量过滤」的候选集内）::

            A = 二级行业 ∈ industries 的标的（industries 为空 = 全集）
            B = 概念命中 concepts 的标的（concepts 为空 = 全集；mode = any/all）
            结果 = A ∩ B

        行业侧**只有并集**：一只标的的二级行业是单值，选两个行业求交恒为空，
        所以不做 ``all``；概念侧是列表，故 ``any`` / ``all`` 都有意义。

        :param params: ``date``（锚点交易日，空 = 最新）/ ``industries``
            （二级行业名，列表或逗号串）/ ``concepts`` / ``concept_mode``
            （``any`` 并集 / ``all`` 交集）/ ``boards`` / ``markets`` /
            ``exclude_st`` / ``min_amount``（元）/ ``max_float_mcap``（亿元）/
            ``max_rows`` / ``sort``（默认按成交额降序）
        """
        t_start = time.perf_counter()
        cfg = self.config.section("screen")
        industries = self.resolve_industries(params)
        concepts = self.resolve_concepts(params)
        concept_mode = str(params.get("concept_mode") or "any").lower()
        if concept_mode not in ("any", "all"):
            concept_mode = "any"
        boards = self.resolve_boards(params, cfg)
        # 与 boards 一致：**显式传了** markets（哪怕空列表）就以调用方为准，
        # 不能用 `or` 兜底 —— 那会让「取消全部交易所勾选」静默变成「全市场」，
        # 界面上看着条件收紧、结果反而变多。
        markets = params.get("markets")
        if markets is None:
            markets = cfg.get("markets") or list(MARKET_ORDER)
        markets = [str(m).lower() for m in markets]
        exclude_st = params.get("exclude_st")
        if exclude_st is None:
            exclude_st = cfg.get("exclude_st", True)
        min_amount = float(params.get("min_amount") or 0.0)
        # 交集页面默认**不限**流通市值（选股那边的 150 亿默认值在这里没有依据）
        raw_mcap = params.get("max_float_mcap")
        max_float_mcap = float(raw_mcap or 0.0)
        max_rows = max(1, int(params.get("max_rows") or 1000))
        sort_key = str(params.get("sort") or "amount").lower()

        # 打错一个行业名 / 概念名 → 结果恒为空且不报错，是最难自查的一种失败，
        # 所以在入口处就挡掉，并把本地实际存在的名称回给前端。
        if industries:
            known = set(self.industries.l2_names())
            unknown = [x for x in industries if x not in known]
            if unknown:
                return {
                    "ok": False,
                    "reason": f"未知二级行业：{'、'.join(unknown[:5])}"
                    + (f" 等 {len(unknown)} 个" if len(unknown) > 5 else ""),
                    "unknown": unknown,
                    "rows": [],
                }
        if concepts:
            known_c = set(self.concepts.names())
            unknown_c = [x for x in concepts if x not in known_c]
            if unknown_c:
                return {
                    "ok": False,
                    "reason": f"未知概念：{'、'.join(unknown_c[:5])}"
                    + (f" 等 {len(unknown_c)} 个" if len(unknown_c) > 5 else ""),
                    "unknown": unknown_c,
                    "rows": [],
                }
        if not industries and not concepts:
            return {
                "ok": False,
                "reason": "请至少选择一个二级行业或一个概念，否则求不出交集",
                "rows": [],
            }
        if not boards:
            return {"ok": False, "reason": "未勾选任何板块，请至少选择一个", "rows": []}
        if not markets:
            return {"ok": False, "reason": "未勾选任何交易所，请至少选择一个", "rows": []}

        df = self.bars()
        if len(df) == 0:
            return {
                "ok": False,
                "reason": "本地行情缓存为空，请先点『刷新数据』",
                "rows": [],
            }
        all_dates = self.all_dates()
        date_min = ymd_to_iso(int(all_dates[0]))
        date_max = ymd_to_iso(int(all_dates[-1]))
        target_date = params.get("date")
        if target_date:
            d = iso_to_ymd(str(target_date))
            if not np.isin(d, all_dates):
                return {
                    "ok": False,
                    "reason": f"{ymd_to_iso(d)} 不是本地数据里的交易日"
                    f"（可选范围 {date_min} ~ {date_max}）",
                    "rows": [],
                    "date_out_of_range": True,
                    "date_min": date_min,
                    "date_max": date_max,
                }
            anchor = int(d)
        else:
            anchor = int(all_dates[-1])
        anchor_iso = ymd_to_iso(anchor)

        # --- 候选集：锚点日有行情 + 交易所 / 板块 / 轻量过滤 ---------------
        cats = df["code"].cat.categories
        n_cat = len(cats)
        cat_ok = np.ones(n_cat, dtype=bool)
        mset = set(markets)
        cat_ok &= np.fromiter(
            (split_code(c)[1] in mset for c in cats), dtype=bool, count=n_cat
        )
        if len(boards) < len(BOARD_ORDER):
            bmap = self.board_map(list(cats))
            allowed = set(boards)
            cat_ok &= np.fromiter(
                (bmap.get(c) in allowed for c in cats), dtype=bool, count=n_cat
            )
        total_universe = int(df["code"].nunique())

        idx = self._rows_in_window(np.array([anchor], dtype="int32"), cat_ok)
        # 先 copy 再改列：后面还要做几轮布尔过滤，链式赋值会退化成
        # copy-of-slice 并抛 SettingWithCopyWarning（而且不一定真的写进去）
        work = df.iloc[idx].copy()
        # 这里只有最多 5605 行，把 code 还原成普通字符串，避免 categorical 的
        # 边界情况（整表 1700 万行才需要 category 省内存）
        work["code"] = work["code"].astype(object)
        if min_amount > 0 and len(work):
            work = work[work["amount"] >= min_amount]
        if exclude_st and len(work):
            nm = {c: self.names.get(c) for c in work["code"].unique()}
            work = work[
                ~work["code"].map(lambda c: is_st_or_delisting(nm.get(c) or ""))
            ]
        mcap_missing = 0
        if max_float_mcap > 0 and len(work):
            smap = {c: self.shares.get(c) for c in work["code"].unique()}
            fs = work["code"].map(smap).astype("float64")
            mcap = fs * work["close"] / 1e8
            mcap_missing = int(mcap.isna().sum())
            work = work[mcap.notna() & (mcap < max_float_mcap)]

        base_codes = work["code"].tolist()
        base_set = set(base_codes)

        # --- 集合 A（二级行业）与集合 B（概念） ---------------------------
        l2_of: Dict[str, Optional[str]] = {
            c: self.industries.get_l2(c) for c in base_codes
        }
        cpts_of: Dict[str, List[str]] = {
            c: self.concepts.get(c, sort_by_size=True) for c in base_codes
        }
        industry_missing = sum(1 for c in base_codes if not l2_of.get(c))
        concept_missing = sum(1 for c in base_codes if not cpts_of.get(c))

        if industries:
            want_ind = set(industries)
            set_a = {c for c in base_codes if l2_of.get(c) in want_ind}
            a_label = f"二级行业（任一命中）：{'、'.join(industries)}"
        else:
            set_a = set(base_set)
            a_label = "不限二级行业（全集）"
        if concepts:
            set_b = self.concepts.filter_codes(base_codes, concepts, mode=concept_mode)
            b_label = (
                f"概念（{'同时命中全部' if concept_mode == 'all' else '命中任一'}）："
                + "、".join(concepts)
            )
        else:
            set_b = set(base_set)
            b_label = "不限概念（全集）"

        inter = set_a & set_b
        only_a = set_a - set_b
        only_b = set_b - set_a

        # --- 明细 ---------------------------------------------------------
        want_cpt = set(concepts)
        rows: List[Dict[str, Any]] = []
        src = work[work["code"].isin(inter)]
        for r in src.itertuples(index=False):
            code = str(r.code)
            close = float(r.close)
            ind = self.industries.get(code) or {"l1": None, "l2": None, "l3": None}
            full_cpts = cpts_of.get(code) or []
            hit_cpts = [c for c in full_cpts if c in want_cpt]
            float_shares = self.shares.get(code)
            float_mcap = self.shares.mcap_yi(code, close)
            rows.append(
                {
                    "code": code,
                    "symbol": code.split(".")[0],
                    "name": self.names.get(code),
                    "market": split_code(code)[1].upper(),
                    "board": board_of(code),
                    "industry_l1": ind["l1"],
                    "industry_l2": ind["l2"],
                    "industry_l3": ind["l3"],
                    # 只带前 12 个（按成员数降序）—— 最多的一只票有 49 个概念，
                    # 1000 行全量返回会把响应体撑到几 MB，而前端也只展示前几个。
                    "concepts": full_cpts[:12],
                    "concept_n": len(full_cpts),
                    "hit_concepts": hit_cpts,
                    "hit_n": len(hit_cpts),
                    "is_st": is_st_name(self.names.get(code) or ""),
                    "trade_date": anchor_iso,
                    "close": round(close, 3),
                    "open": round(float(r.open), 3),
                    "high": round(float(r.high), 3),
                    "low": round(float(r.low), 3),
                    "pct_change": None if pd.isna(r.pct) else round(float(r.pct), 5),
                    "amount": round(float(r.amount), 0),
                    "amount_yi": fmt_amount_yi(r.amount),
                    "volume_hand": round(float(r.volume) / 100.0, 0),
                    "float_shares": (
                        None if float_shares is None else round(float(float_shares), 0)
                    ),
                    "float_shares_yi": (
                        None if float_shares is None else round(float_shares / 1e8, 3)
                    ),
                    "float_mcap_yi": (
                        None if float_mcap is None else round(float_mcap, 2)
                    ),
                }
            )
        sorters = {
            "amount": lambda x: (-(x["amount_yi"] or 0.0), x["code"]),
            "mcap": lambda x: (-(x["float_mcap_yi"] or 0.0), x["code"]),
            "pct": lambda x: (-(x["pct_change"] or 0.0), x["code"]),
            "close": lambda x: (-(x["close"] or 0.0), x["code"]),
            "concept_n": lambda x: (-x["concept_n"], x["code"]),
            "industry": lambda x: (x["industry_l2"] or "", x["code"]),
            "code": lambda x: x["code"],
            "name": lambda x: (x["name"] or "", x["code"]),
        }
        rows.sort(key=sorters.get(sort_key, sorters["amount"]))
        total = len(rows)
        rows = rows[:max_rows]

        # --- 交集为空时的「怎么调」建议 ------------------------------------
        # 空结果本身没法自查，于是把两侧各自最常见的标签回给前端：
        # 「A 里最热的概念」点一下就能进 B，「B 里最热的行业」点一下就能进 A。
        suggest: Dict[str, Any] = {}
        if not inter and base_codes:
            cnt_c: Counter = Counter()
            for c in set_a:
                cnt_c.update(cpts_of.get(c) or [])
            cnt_i: Counter = Counter()
            for c in set_b:
                l2 = l2_of.get(c)
                if l2:
                    cnt_i[l2] += 1
            suggest = {
                "concepts_in_industry": [
                    {"concept": k, "n": v} for k, v in cnt_c.most_common(15)
                ],
                "industries_in_concept": [
                    {"l2": k, "n": v} for k, v in cnt_i.most_common(15)
                ],
            }

        # --- 交集内部的两张分布（按交集统计，用于看结构） ------------------
        cnt_ind: Counter = Counter()
        cnt_cpt: Counter = Counter()
        for c in inter:
            l2 = l2_of.get(c)
            if l2:
                cnt_ind[l2] += 1
            cnt_cpt.update(cpts_of.get(c) or [])
        industry_summary = [
            {"l2": k, "n": v} for k, v in cnt_ind.most_common()
        ]
        concept_summary = [
            {"concept": k, "n": v} for k, v in cnt_cpt.most_common(40)
        ]

        conditions = [
            f"锚点交易日 {anchor_iso}",
            f"候选范围：{len(base_codes)} 只（当日有行情且通过条件；全市场 {total_universe} 只）",
            a_label,
            b_label,
            f"板块：{'/'.join(boards)}",
            f"交易所：{'/'.join(m.upper() for m in markets)}",
            "剔除 ST / 退市" if exclude_st else "包含 ST",
            f"成交额 ≥ {fmt_amount_yi(min_amount)} 亿元" if min_amount > 0 else "不限成交额",
            (
                f"流通市值 < {max_float_mcap:g} 亿元"
                if max_float_mcap > 0
                else "不限流通市值"
            ),
            "板块归属为本地文件当前成分（非历史快照）· 使用本地通达信日线（不复权）",
        ]
        warnings: List[str] = []
        if concept_missing:
            ratio = concept_missing / max(1, len(base_codes))
            warnings.append(
                f"{concept_missing} 只（{ratio:.1%}）在本地板块文件里没有概念归属，"
                "它们不会出现在概念集合 B 中"
            )
        if industry_missing:
            warnings.append(
                f"{industry_missing} 只在本地行业表里没有二级行业归属，"
                "选择任何行业时都不会命中它们"
            )
        if target_date:
            warnings.append(
                "概念 / 行业是本地文件的**当前**成份，回溯历史日期时并不等于"
                "当时的板块成分（是「用今天的板块看历史行情」）"
            )
        if warnings:
            warnings.append("工具只做数据统计，不构成任何投资建议")

        return {
            "ok": True,
            "anchor": anchor_iso,
            "anchor_raw": anchor,
            "date_query": target_date,
            "date_min": date_min,
            "date_max": date_max,
            "industries": industries,
            "industry_label": a_label,
            "concepts": concepts,
            "concept_mode": concept_mode,
            "concept_label": b_label,
            "boards": boards,
            "markets": markets,
            "exclude_st": exclude_st,
            "min_amount": min_amount,
            "max_float_mcap": max_float_mcap,
            "candidates": len(base_codes),
            "total_universe": total_universe,
            "set_a": len(set_a),
            "set_b": len(set_b),
            "intersect": len(inter),
            "only_a": len(only_a),
            "only_b": len(only_b),
            "union": len(set_a | set_b),
            "ratio_a": round(len(inter) / len(set_a), 4) if set_a else 0.0,
            "ratio_b": round(len(inter) / len(set_b), 4) if set_b else 0.0,
            "industry_missing": industry_missing,
            "concept_missing": concept_missing,
            "mcap_filtered_missing": mcap_missing,
            "concept_total": self.concepts.concept_count,
            "industry_summary": industry_summary,
            "concept_summary": concept_summary,
            "suggest": suggest,
            "conditions": conditions,
            "warnings": warnings,
            "sort": sort_key,
            "total": total,
            "truncated": total > len(rows),
            "rows": rows,
            "rows_count": len(rows),
            "elapsed": round(time.perf_counter() - t_start, 3),
        }

    # ------------------------------------------------------------------
    # K 线
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 板块看盘（行业 / 地区 / 概念 / 风格）
    # ------------------------------------------------------------------
    def _index_dir(self) -> Optional[Path]:
        """板块指数日线目录（``vipdoc/sh/lday``）。"""
        vipdoc = self.reader.vipdoc
        if vipdoc is None:
            return None
        d = vipdoc / "sh" / "lday"
        return d if d.is_dir() else None

    def _index_bars(self, index_code: str, tail: int = 25) -> Optional[pd.DataFrame]:
        """读板块指数日线（只取尾部 ``tail`` 根），按文件 mtime 记忆化。

        板块指数（``880xxx``）**不在主行情缓存里** —— 那里只装 A 股正股
        （``is_a_share`` 的规则排除指数）。指数文件很大（约 3600 根 / 116KB），
        而看盘只用到尾部几十根，所以这里 **seek 到文件尾部只读需要的字节**：
        604 个板块齐扫约 20ms，整读一遍要 0.9s。

        缓存记的是 ``(mtime, 已读根数, df)`` —— **必须带「已读根数」**：
        板块列表只要 22 根（算 20 日均量），而 K 线图要 200+ 根，只按 mtime
        命中就会把 22 根的那份交给 200 根的请求（K 线图会只剩一小截）。
        """
        d = self._index_dir()
        if d is None:
            return None
        path = d / f"sh{index_code}.day"
        if not path.is_file():
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        n = max(1, int(tail))
        hit = self._index_cache.get(index_code)
        if hit is not None and hit[0] == st.st_mtime and hit[1] >= n:
            return hit[2]
        take = min(st.st_size, n * DAY_DTYPE.itemsize)
        if take <= 0:
            return None
        with path.open("rb") as fh:
            fh.seek(st.st_size - take)
            raw = fh.read(take)
        cnt = len(raw) // DAY_DTYPE.itemsize
        if cnt == 0:
            return None
        arr = np.frombuffer(raw[: cnt * DAY_DTYPE.itemsize], dtype=DAY_DTYPE)
        df = pd.DataFrame(
            {
                "date": arr["date"].astype("int64"),
                "open": arr["open"].astype("float64") / 100.0,
                "high": arr["high"].astype("float64") / 100.0,
                "low": arr["low"].astype("float64") / 100.0,
                "close": arr["close"].astype("float64") / 100.0,
                "volume": arr["volume"].astype("float64"),
                "amount": arr["amount"].astype("float64"),
            }
        )
        df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
        df = df.reset_index(drop=True)
        # 涨跌幅按**前一根收盘**算（与主行情缓存同口径：``.day`` 自带的第 8 个
        # 字段早已失效，见 limits.py）。窗口第一根没有前收盘 → NaN，
        # 调用方（K 线图）总会多读 70 根，展示区间内的 pct 都是有效的。
        df["pct"] = df["close"].pct_change()
        # 文件本身比请求的还短 → 已经读到文件开头，之后任何 tail 都够用
        covered = n if cnt >= n else 10**9
        with self._lock:
            self._index_cache[index_code] = (st.st_mtime, covered, df)
        return df

    def _anchor(
        self, params: Dict[str, Any]
    ) -> tuple[Optional[int], Optional[Dict[str, Any]]]:
        """解析锚点交易日（``params["date"]``，空 = 最新）。

        与选股 / 连板一样**锚在全市场交易日上**，否则停牌板块会拿自己的最后一根
        冒充当日行情。
        """
        all_dates = self.all_dates()
        if len(all_dates) == 0:
            return None, {"ok": False, "reason": "本地行情缓存为空，请先点『刷新数据』"}
        raw = params.get("date")
        if raw:
            d = iso_to_ymd(str(raw))
            if not np.isin(d, all_dates):
                return None, {
                    "ok": False,
                    "rows": [],
                    "reason": f"{ymd_to_iso(d)} 不是本地数据里的交易日"
                    f"（可选范围 {ymd_to_iso(int(all_dates[0]))} ~ "
                    f"{ymd_to_iso(int(all_dates[-1]))}）",
                    "date_out_of_range": True,
                    "date_min": ymd_to_iso(int(all_dates[0])),
                    "date_max": ymd_to_iso(int(all_dates[-1])),
                }
            return int(d), None
        return int(all_dates[-1]), None

    def _anchor_snapshot(self, anchor: int) -> pd.DataFrame:
        """锚点日全市场一行（``code`` 还原成字符串 + OHLCV + ``pct``）。"""
        df = self.bars()
        idx = np.flatnonzero(df["date"].to_numpy() == anchor)
        sub = df.iloc[idx].copy()
        sub["code"] = sub["code"].astype(object)
        return sub

    def _anchor_limits(
        self, anchor: int, st_limit: bool = False
    ) -> Dict[str, int]:
        """锚点日各股票的涨跌停判定，``{代码: 1 涨停 / -1 跌停}``（其余不出现）。

        口径完全复用连板梯队的 :mod:`stock_picker.limits`：基准价取**前一根
        收盘**、上市首日不判定、ST 5% 默认关闭（本机行情实测主板 ST 也是 10%）。
        这里只要锚点日一天，所以窗口取 2 根就够（够取到「前一根收盘」）。
        """
        win = self._streak_window(anchor, 2)
        rows, cat, pos, seg_len = win["rows"], win["cat"], win["pos"], win["seg_len"]
        if rows.size == 0:
            return {}
        df = self.bars()
        sub = df.iloc[rows]
        close = sub["close"].to_numpy(dtype="float64")
        open_ = sub["open"].to_numpy(dtype="float64")
        high = sub["high"].to_numpy(dtype="float64")
        low = sub["low"].to_numpy(dtype="float64")
        dates = sub["date"].to_numpy()
        prev = np.full(rows.size, np.nan)
        if rows.size > 1:
            same = cat[1:] == cat[:-1]
            prev[1:] = np.where(same, close[:-1], np.nan)
        is_ipo = rows == win["starts"][cat]
        cats = df["code"].cat.categories
        cat_names = [self.names.get(c) or "" for c in cats]
        factors = np.fromiter(
            (limit_factor(c, cat_names[i], st_limit) for i, c in enumerate(cats)),
            dtype="int64",
            count=len(cats),
        )
        lu, _tu, ld = limit_masks(close, high, low, prev, factors[cat], is_ipo)
        tail = (pos == seg_len - 1) & (dates == anchor)
        code_of_row = np.asarray(cats, dtype=object)[cat]
        out: Dict[str, int] = {}
        for i in np.flatnonzero(tail):
            i = int(i)
            if lu[i]:
                out[str(code_of_row[i])] = 1
            elif ld[i]:
                out[str(code_of_row[i])] = -1
        return out

    def _board_aggregate(
        self, anchor: int, index_codes: Sequence[str]
    ) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        """把锚点日行情按板块成分聚合（涨跌家数 / 涨停数 / 成交额 / 换手）。

        一只股票属于多个板块（实测平均 8.5 个概念），所以先把
        ``(板块, 股票)`` 展平成对，再与当日行情内联接 —— 一次 ``groupby``
        就能出全部板块的统计，不做逐板块循环。

        :return: ``(每板块统计, 全市场口径)``。第二个元素里的涨跌停数是**去重**
            的（全市场当日涨停多少只），可以直接和连板梯队对上；板块级的
            ``n_limit_up`` 是「该板块内有几只涨停」，跨板块相加会重复计同一只股。
        """
        pairs: List[tuple] = []
        for ic in index_codes:
            for c in self.boards.members_of(ic):
                pairs.append((ic, c))
        if not pairs:
            return {}, {}
        pair_df = pd.DataFrame(pairs, columns=["board", "code"])
        snap = self._anchor_snapshot(anchor)[
            ["code", "close", "pct", "volume", "amount"]
        ]
        m = pair_df.merge(snap, on="code", how="inner")
        whole = {
            "quoted_all": int(len(snap)),
            "limit_up_all": 0,
            "limit_down_all": 0,
        }
        if len(m) == 0:
            return {}, whole

        lim = self._anchor_limits(anchor)
        whole["limit_up_all"] = sum(1 for v in lim.values() if v == 1)
        whole["limit_down_all"] = sum(1 for v in lim.values() if v == -1)
        m["lim"] = m["code"].map(lim).fillna(0).astype("int8")
        m["is_up"] = m["pct"] > 0
        m["is_down"] = m["pct"] < 0
        m["is_flat"] = m["pct"] == 0
        m["is_lu"] = m["lim"] == 1
        m["is_ld"] = m["lim"] == -1
        share_map = {c: self.shares.get(c) for c in m["code"].unique()}
        m["fs"] = m["code"].map(share_map).astype("float64")

        g = m.groupby("board", sort=False)
        agg = g.agg(
            n_members=("code", "size"),
            n_up=("is_up", "sum"),
            n_down=("is_down", "sum"),
            n_flat=("is_flat", "sum"),
            n_limit_up=("is_lu", "sum"),
            n_limit_down=("is_ld", "sum"),
            avg_pct=("pct", "mean"),
            amount=("amount", "sum"),
            volume=("volume", "sum"),
            float_shares=("fs", "sum"),
        )
        out: Dict[str, Dict[str, Any]] = {}
        for board, r in agg.iterrows():
            fs = float(r["float_shares"])
            vol = float(r["volume"])
            avg = r["avg_pct"]
            out[str(board)] = {
                "n_members": int(r["n_members"]),
                "n_up": int(r["n_up"]),
                "n_down": int(r["n_down"]),
                "n_flat": int(r["n_flat"]),
                "n_limit_up": int(r["n_limit_up"]),
                "n_limit_down": int(r["n_limit_down"]),
                "avg_pct": None if pd.isna(avg) else round(float(avg), 5),
                "amount_yi": fmt_amount_yi(float(r["amount"])),
                "turnover": round(vol / fs, 5) if fs > 0 else None,
            }
        return out, whole

    def _index_quote(
        self, index_code: str, anchor: int, ma_window: int = 20
    ) -> Dict[str, Any]:
        """板块指数在锚点日的行情（涨幅 / 成交额 / 量比）。"""
        bars = self._index_bars(index_code, tail=max(2, ma_window + 2))
        if bars is None or len(bars) == 0:
            return {}
        sub = bars[bars["date"] <= anchor]
        if len(sub) == 0:
            return {}
        last = sub.iloc[-1]
        if int(last["date"]) != anchor:
            # 该板块指数在锚点日没有数据（停牌 / 新板块），不当成 0 涨幅
            return {"stale_date": ymd_to_iso(int(last["date"]))}
        prev = float(sub["close"].iloc[-2]) if len(sub) >= 2 else None
        close = float(last["close"])
        vols = sub["volume"].to_numpy(dtype="float64")
        ma_vol = float(vols[-ma_window - 1 : -1].mean()) if len(vols) > ma_window else None
        vol = float(last["volume"])
        return {
            "close": round(close, 3),
            "prev_close": None if prev is None else round(prev, 3),
            "pct": None if not prev else round(close / prev - 1.0, 5),
            "open": round(float(last["open"]), 3),
            "high": round(float(last["high"]), 3),
            "low": round(float(last["low"]), 3),
            "volume": vol,
            "amount_yi": fmt_amount_yi(float(last["amount"])),
            "vol_ratio": (
                round(vol / ma_vol, 3) if ma_vol and ma_vol > 0 else None
            ),
        }

    #: 板块列表的可排序字段（键即前端 ``sort`` 参数）
    BOARD_SORT_KEYS = (
        "order", "code", "name", "pct", "close", "amount_yi",
        "vol_ratio", "turnover", "n_up", "n_down", "n_limit_up", "avg_pct",
    )

    @staticmethod
    def _sorted_rows(
        rows: List[Dict[str, Any]],
        key: str,
        desc: bool,
        getter: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> List[Dict[str, Any]]:
        """按 ``key`` 排序，**缺值的永远沉底**（不随 asc / desc 翻转）。

        不能直接 ``rows.sort(key=..., reverse=True)``：那会把「缺值」这个哨兵
        一起反转，于是没有当日行情的板块在降序时反而排到最前面（实测踩过）。
        先分成「有值 / 缺值」两层，各自再排。

        :param getter: 取排序值的函数；默认取 ``r[key]``（成分股表的键与字段名
            不同名，例如 ``code`` 取的是 ``symbol``）。
        """
        get = getter or (lambda r: r.get(key))
        present: List[Dict[str, Any]] = []
        absent: List[Dict[str, Any]] = []
        for r in rows:
            (absent if get(r) is None else present).append(r)
        present.sort(key=get, reverse=desc)
        return present + absent

    def board_panel(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """板块看盘：全部板块在锚点日的指标列表。

        指标由两部分拼成：**板块指数自己的行情**（涨幅 / 成交额 / 量比，读
        ``sh880xxx.day``）与**成分股聚合**（涨跌家数 / 涨停跌停数 / 平均涨幅 /
        换手率）。地区板块本地没有成分文件，只有前半部分。

        :param params: ``date``（锚点交易日）/ ``category``（行业/地区/概念/风格，
            空 = 全部）/ ``sort`` + ``order``（排序）/ ``keyword``（名称或代码）/
            ``limit`` / ``with_members_only``
        """
        t_start = time.perf_counter()
        if self.boards.size == 0:
            return {"ok": False, "reason": "未找到板块指数表 tdxzs.cfg", "rows": []}
        anchor, err = self._anchor(params)
        if err is not None:
            return {**err, "rows": []}
        assert anchor is not None

        category = str(params.get("category") or "").strip()
        if category in ("", "全部", "all"):
            category = ""
        known = {c["value"] for c in self.boards.categories()}
        if category and category not in known:
            return {
                "ok": False,
                "rows": [],
                "reason": f"未知板块类别 {category}（可选：行业 / 地区 / 概念 / 风格）",
            }

        items = self.boards.catalog(category)
        index_codes = [c["index_code"] for c in items]
        agg, whole = self._board_aggregate(anchor, index_codes)

        rows: List[Dict[str, Any]] = []
        stale = 0
        for it in items:
            ic = it["index_code"]
            q = self._index_quote(ic, anchor)
            if not q:
                stale += 1
            elif q.get("stale_date"):
                stale += 1
            a = agg.get(ic) or {}
            rows.append(
                {
                    "index_code": ic,
                    "name": it["name"],
                    "show_name": it["show_name"],
                    "category": it["category"],
                    "order": it["order"],
                    "has_members": it["has_members"],
                    "n_members": it["n"] or None,
                    "n_quoted": a.get("n_members"),
                    "close": q.get("close"),
                    "pct": q.get("pct"),
                    "amount_yi": q.get("amount_yi"),
                    "vol_ratio": q.get("vol_ratio"),
                    "stale_date": q.get("stale_date"),
                    "n_up": a.get("n_up"),
                    "n_down": a.get("n_down"),
                    "n_flat": a.get("n_flat"),
                    "n_limit_up": a.get("n_limit_up"),
                    "n_limit_down": a.get("n_limit_down"),
                    "avg_pct": a.get("avg_pct"),
                    "turnover": a.get("turnover"),
                }
            )

        keyword = str(params.get("keyword") or "").strip().upper()
        if keyword:
            rows = [
                r
                for r in rows
                if keyword in str(r["name"]).upper()
                or keyword in str(r["index_code"])
            ]
        if params.get("with_members_only"):
            rows = [r for r in rows if r["has_members"]]

        key = str(params.get("sort") or "pct")
        if key not in self.BOARD_SORT_KEYS:
            key = "pct"
        order = str(params.get("order") or "desc")
        if key in ("order", "code", "name"):
            order = "asc"          # 这三列按升序看更自然（cfg 顺序 / 代码 / 名称）
        rows = self._sorted_rows(rows, key, order == "desc")
        # 汇总要在 limit 截断**之前**算，否则「604 个板块里几个在涨」会被
        # 首屏那几十行带偏
        summary = {
            "boards_up": sum(1 for r in rows if (r["pct"] or 0) > 0),
            "boards_down": sum(1 for r in rows if (r["pct"] or 0) < 0),
            "boards_flat": sum(1 for r in rows if r["pct"] is not None and not r["pct"]),
            "boards_no_quote": sum(1 for r in rows if r["pct"] is None),
            # 全市场口径（去重），可与连板梯队直接对照；不是板块级 n_limit_up 的求和
            "stocks_quoted": whole.get("quoted_all", 0),
            "limit_up_stocks": whole.get("limit_up_all", 0),
            "limit_down_stocks": whole.get("limit_down_all", 0),
        }
        total = len(rows)
        limit = int(params.get("limit") or 0)
        if limit > 0:
            rows = rows[:limit]

        return {
            "ok": True,
            "date": ymd_to_iso(anchor),
            "category": category or "全部",
            "total": total,
            "count": len(rows),
            "sort": key,
            "order": order,
            "stale": stale,
            "categories": self.boards.categories(),
            "summary": summary,
            "elapsed": round(time.perf_counter() - t_start, 3),
            "rows": rows,
        }

    def board_members(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """板块成分股明细（点选板块后右侧列表）。

        只统计**锚点日有行情**的成分股（停牌股不进列表，与行情软件一致）；
        ``n_total`` 给的是静态成分数，两者之差就是当日停牌 / 未上市的数量。
        """
        t_start = time.perf_counter()
        index_code = str(params.get("board") or params.get("index_code") or "").strip()
        if not index_code:
            return {"ok": False, "reason": "缺少板块参数 board（板块指数代码）", "rows": []}
        info = self.boards.get(index_code)
        if info is None:
            return {"ok": False, "reason": f"未知板块 {index_code}", "rows": []}
        anchor, err = self._anchor(params)
        if err is not None:
            return {**err, "rows": []}
        assert anchor is not None

        codes = self.boards.members_of(index_code)
        base = {
            "ok": True,
            "board": info,
            "date": ymd_to_iso(anchor),
            "n_total": len(codes),
            "rows": [],
        }
        if not codes:
            base["reason"] = "本地没有该板块的成分股数据，只有指数行情"
            return base

        df = self.bars()
        if len(df) == 0:
            return {"ok": False, "reason": "本地行情缓存为空，请先点『刷新数据』", "rows": []}

        want = set(codes)
        cats = df["code"].cat.categories
        cat_ok = np.fromiter(
            (c in want for c in cats), dtype=bool, count=len(cats)
        )
        idx = self._rows_in_window(np.array([anchor], dtype="int32"), cat_ok)
        ma_window = int(params.get("ma_window") or 20)
        work = self._attach_volume_stats(idx, ma_window)
        if len(work) == 0:
            base["reason"] = f"{ymd_to_iso(anchor)} 该板块没有成分股行情"
            return base

        lim = self._anchor_limits(anchor)
        names_map = {c: self.names.get(c) or "" for c in work["code"].unique()}
        ind_map = {c: self.industries.get_l2(c) for c in work["code"].unique()}
        share_map = {c: self.shares.get(c) for c in work["code"].unique()}
        concept_n = {c: len(self.concepts.get(c)) for c in work["code"].unique()}

        out: List[Dict[str, Any]] = []
        for r in work.itertuples(index=False):
            code = str(r.code)
            close = float(r.close)
            fs = share_map.get(code)
            limv = lim.get(code, 0)
            out.append(
                {
                    "code": code,
                    "symbol": split_code(code)[0],
                    "name": names_map.get(code),
                    "board": board_of(code),
                    "industry_l2": ind_map.get(code),
                    "close": round(close, 3),
                    "pct": None if pd.isna(r.pct) else round(float(r.pct), 5),
                    "amount_yi": fmt_amount_yi(float(r.amount)),
                    "volume_hand": round(float(r.volume) / 100.0, 0),
                    "vol_ratio": None if pd.isna(r.vol_ratio) else round(float(r.vol_ratio), 3),
                    "ma_volume": round(float(r.ma_vol) / 100.0, 0) if pd.notna(r.ma_vol) else None,
                    "turnover": (
                        round(float(r.volume) / float(fs), 5) if fs else None
                    ),
                    "float_mcap_yi": (
                        round(float(fs) * close / 1e8, 2) if fs else None
                    ),
                    "concept_n": concept_n.get(code, 0),
                    "limit": "涨停" if limv == 1 else ("跌停" if limv == -1 else ""),
                }
            )

        key = str(params.get("sort") or "pct")
        cols = {
            "code": lambda r: r["symbol"],
            "name": lambda r: r["name"] or "",
            "pct": lambda r: r["pct"],
            "close": lambda r: r["close"],
            "amount_yi": lambda r: r["amount_yi"],
            "vol_ratio": lambda r: r["vol_ratio"],
            "turnover": lambda r: r["turnover"],
            "float_mcap_yi": lambda r: r["float_mcap_yi"],
            "industry_l2": lambda r: r["industry_l2"] or "",
            "concept_n": lambda r: r["concept_n"],
        }
        if key not in cols:
            key = "pct"
        getter = cols[key]
        order = str(params.get("order") or "desc")
        text_key = key in ("code", "name", "industry_l2")
        # 文本列一律升序（「涨停」排在「跌停」前面这种默认），数字列按请求方向
        out = self._sorted_rows(out, key, order == "desc" and not text_key, getter)
        total = len(out)
        limit = int(params.get("limit") or 0)
        if limit > 0:
            out = out[:limit]

        base.update(
            {
                "n_quoted": total,
                "n_limit_up": sum(1 for r in out if r["limit"] == "涨停"),
                "n_limit_down": sum(1 for r in out if r["limit"] == "跌停"),
                "n_suspended": max(0, len(codes) - total),
                "count": len(out),
                "sort": key,
                "order": order,
                "elapsed": round(time.perf_counter() - t_start, 3),
                "rows": out,
            }
        )
        return base

    def index_kline(self, index_code: str, bars: int = 160) -> Dict[str, Any]:
        """板块指数 K 线（含均线、均量、MACD）。"""
        ic = str(index_code).strip()
        info = self.boards.get(ic)
        if info is None:
            return {"ok": False, "reason": f"未知板块 {ic}", "code": ic}
        n = max(30, int(bars))
        full = self._index_bars(ic, tail=n + 70)
        if full is None or len(full) == 0:
            return {"ok": False, "reason": f"本地没有板块指数 {ic} 的日线数据", "code": ic}
        payload = self._kline_payload(full, n, self._default_ma_window())
        payload.update(
            {
                "code": ic,
                "name": info["name"],
                "category": info["category"],
                "is_index": True,
                "n_members": info["n"],
            }
        )
        return payload

    def _default_ma_window(self) -> int:
        return int(self.config.section("screen").get("ma_window", 20) or 20)

    def _kline_payload(
        self, full: pd.DataFrame, bars: int, ma_window: int
    ) -> Dict[str, Any]:
        """把一段按日期升序的日线整理成前端 K 线图要用的载荷。

        个股与板块指数共用（两个图除了数据来源不同，形态要求完全一致：
        主图 + 成交量 + MACD）。
        """
        sub = full.tail(max(30, int(bars)))
        closes = full["close"].astype("float64")
        volumes = full["volume"].astype("float64")
        mas: Dict[str, List[Optional[float]]] = {}
        for w in (5, 10, 20, 60):
            if len(full) >= w:
                mas[f"ma{w}"] = [
                    None if pd.isna(v) else round(float(v), 3)
                    for v in closes.rolling(w, min_periods=w).mean()
                ][-len(sub):]
            else:
                mas[f"ma{w}"] = [None] * len(sub)
        ma_vol_series = volumes.shift(1).rolling(
            int(ma_window), min_periods=int(ma_window)
        ).mean()
        vol_ratio_series = volumes / ma_vol_series

        dates = [ymd_to_iso(int(d)) for d in sub["date"]]
        ohlc = [
            [round(float(o), 3), round(float(c), 3), round(float(l), 3), round(float(h), 3)]
            for o, c, l, h in zip(sub["open"], sub["close"], sub["low"], sub["high"])
        ]
        vols = [round(float(v) / 100.0, 0) for v in sub["volume"]]  # 手
        ratios = [
            None if pd.isna(v) else round(float(v), 2)
            for v in vol_ratio_series.tail(len(sub))
        ]
        ma_volume = [
            None if pd.isna(v) else round(float(v) / 100.0, 0)
            for v in ma_vol_series.tail(len(sub))
        ]
        pct = [None if pd.isna(v) else round(float(v), 5) for v in sub["pct"]] if "pct" in sub else [None] * len(sub)

        signals = [
            {"date": d, "index": i, "vol_ratio": r}
            for i, (d, r) in enumerate(zip(dates, ratios))
            if r is not None and r >= 2.0
        ]
        return {
            "ok": True,
            "bars": len(sub),
            "dates": dates,
            "ohlc": ohlc,
            "volume": vols,
            "volume_ma": ma_volume,
            "vol_ratio": ratios,
            "pct": pct,
            "ma": mas,
            "macd": self._macd(closes, tail=len(sub)),
            "signals": signals,
            "latest": {
                "date": dates[-1] if dates else None,
                "close": round(float(sub["close"].iloc[-1]), 3),
            },
        }

    @staticmethod
    def _macd(
        closes: pd.Series,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        tail: int = 0,
    ) -> Dict[str, List[Optional[float]]]:
        """MACD(12, 26, 9)，与通达信 / 同花顺默认口径一致。

        ``dif = EMA12 − EMA26``、``dea = EMA9(dif)``、柱 = ``2 × (dif − dea)``
        （通达信的柱状图是两倍，不是常见的 1 倍）；取 ``tail`` 根与 K 线对齐。
        """
        c = pd.Series(closes).astype("float64").reset_index(drop=True)
        if len(c) == 0:
            return {"dif": [], "dea": [], "macd": []}
        ema_fast = c.ewm(span=fast, adjust=False).mean()
        ema_slow = c.ewm(span=slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=signal, adjust=False).mean()
        bar = (dif - dea) * 2.0
        pick = (lambda s: s.tail(int(tail))) if tail else (lambda s: s)
        r4 = lambda s: [None if pd.isna(v) else round(float(v), 4) for v in pick(s)]
        return {"dif": r4(dif), "dea": r4(dea), "macd": r4(bar)}

    def kline(self, code: str, bars: int = 160, ma_window: int = 20) -> Dict[str, Any]:
        """返回单只股票的 K 线（含均线、均量、放量标记与 MACD）。

        载荷的组装与板块指数共用 :meth:`_kline_payload`，两者的图形结构因此
        完全一致（主图 + 成交量 + MACD），前端一套渲染即可。
        """
        std = normalize_code(code)
        df = self.bars()
        if len(df) == 0:
            return {"ok": False, "reason": "行情缓存为空，请先刷新数据", "code": std}
        full = df[df["code"] == std].sort_values("date")
        if len(full) == 0:
            return {"ok": False, "reason": f"本地无 {std} 的日线数据", "code": std}
        # 买卖价标记由前端叠加（抽屉里有输入框，见 app.js::loadKline）
        payload = self._kline_payload(full, bars, ma_window)
        payload.update({"code": std, "name": self.names.get(std)})
        return payload

    # ------------------------------------------------------------------
    # 追踪估值
    # ------------------------------------------------------------------
    def valuate(self, items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """给追踪条目补充：最新价、是否触达买入价、是否达标卖出价、浮动盈亏。"""
        df = self.bars()
        out: List[Dict[str, Any]] = []
        if len(df) == 0:
            for it in items:
                out.append(
                    {
                        **self._with_concepts(it),
                        "valuation": {"available": False, "reason": "无行情缓存"},
                    }
                )
            return out

        for it in items:
            code = str(it.get("code") or "")
            base = self._with_concepts(it)
            info: Dict[str, Any] = {"available": False}
            try:
                std = normalize_code(code)
                sub = df[(df["code"] == std)].sort_values("date")
                if len(sub) == 0:
                    info["reason"] = "本地无该标的日线"
                    out.append({**base, "name": it.get("name") or "", "valuation": info})
                    continue
                sig = it.get("signal_date")
                if sig:
                    sub = sub[sub["date"] >= iso_to_ymd(str(sig))]
                if len(sub) == 0:
                    info["reason"] = "信号日之后无行情"
                    out.append({**base, "valuation": info})
                    continue

                latest = sub.iloc[-1]
                buy = it.get("buy_price")
                sell = it.get("sell_price")
                entry = it.get("entry_price") if it.get("entry_price") else buy
                lowest = float(sub["low"].min())
                highest = float(sub["high"].max())
                close_now = float(latest["close"])

                info = {
                    "available": True,
                    "latest_date": ymd_to_iso(int(latest["date"])),
                    "latest_close": round(close_now, 3),
                    "bars_since": int(len(sub)),
                    "low_since": round(lowest, 3),
                    "high_since": round(highest, 3),
                    "touched_buy": bool(buy is not None and lowest <= float(buy)),
                    "hit_target": bool(sell is not None and highest >= float(sell)),
                    "reach_buy_pct": (
                        round((float(buy) - close_now) / close_now, 5) if buy else None
                    ),
                }
                if entry:
                    info["ret_pct"] = round(close_now / float(entry) - 1.0, 5)
                    info["ret_amount"] = round(
                        (close_now - float(entry)) * float(it.get("qty") or 0), 2
                    )
                if sell:
                    info["to_target_pct"] = round(float(sell) / close_now - 1.0, 5)
                ind = self.industries.get(std) or {"l1": None, "l2": None, "l3": None}
                out.append(
                    {
                        **base,
                        "name": it.get("name") or self.names.get(std),
                        "industry_l1": ind["l1"],
                        "industry_l2": ind["l2"],
                        "industry_l3": ind["l3"],
                        "valuation": info,
                    }
                )
            except Exception as exc:  # pragma: no cover
                info["reason"] = f"估值失败：{exc}"
                out.append({**base, "valuation": info})
        return out

    def _with_concepts(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """给计划 / 追踪条目附上概念标签（与行情无关，故不参与异常分支）。"""
        codes = self.concepts.get(str(item.get("code") or ""), sort_by_size=True)
        return {**item, "concepts": codes, "concept_n": len(codes)}

    # ------------------------------------------------------------------
    # 行业
    # ------------------------------------------------------------------
    def industries_info(self, force: bool = False) -> Dict[str, Any]:
        """行业分类元数据与树（供前端下拉、分布展示）。"""
        if force or self.industries.size == 0:
            meta = self.industries.load(force=force)
        else:
            meta = self.industries.meta
        return {
            "ok": True,
            "size": self.industries.size,
            "l1_count": len(self.industries.tree()),
            "l2_count": len(self.industries.l2_names()),
            "tree": self.industries.tree(),
            "l2_names": self.industries.l2_names(),
            # 每个二级行业的静态成员数（选择器用来标量级）
            "l2_counts": self.industries.l2_counts(),
            "meta": meta,
        }

    # ------------------------------------------------------------------
    # 概念
    # ------------------------------------------------------------------
    def concepts_info(self, force: bool = False) -> Dict[str, Any]:
        """概念分类元数据与目录（供前端下拉、分布展示）。"""
        universe = None
        if force or self.concepts.concept_count == 0:
            if force:
                # 显式重建时顺带算一次 A 股覆盖率，作为自证数据
                try:
                    universe = self.reader.list_codes(a_share_only=True)
                except Exception:
                    universe = None
            try:
                meta = self.concepts.load(force=force, universe=universe)
            except Exception as exc:
                return {
                    "ok": False,
                    "reason": str(exc),
                    "size": 0,
                    "count": 0,
                    "items": [],
                    "names": [],
                    "names_by_size": [],
                    "meta": {},
                }
        else:
            meta = self.concepts.meta
        return {
            "ok": True,
            "size": self.concepts.size,
            "count": self.concepts.concept_count,
            "items": self.concepts.catalog(),
            "names": self.concepts.names(),
            "names_by_size": self.concepts.names(sort_by="size"),
            "meta": meta,
        }

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        meta = self.meta()
        counts = self.reader.counts()
        codes = self.reader.list_codes(a_share_only=True)
        a_share_by_market: Dict[str, int] = {}
        for c in codes:
            m = split_code(c)[1]
            a_share_by_market[m] = a_share_by_market.get(m, 0) + 1
        return {
            "ok": True,
            "tdx_dir": str(self.reader.tdx_dir) if self.reader.tdx_dir else "",
            "vipdoc": str(self.reader.vipdoc) if self.reader.vipdoc else "",
            "ready": self.reader.is_ready(),
            "reader_mode": self.reader.mode,
            "pytdx_available": self.reader.mode != "none",
            "file_counts": counts,
            "file_total": sum(counts.values()),
            "a_share_counts": a_share_by_market,
            "a_share_total": len(codes),
            "names": {"size": self.names.size, **self.names.meta},
            "industries": {
                "size": self.industries.size,
                "l1": len(self.industries.tree()),
                "l2": len(self.industries.l2_names()),
                **{k: v for k, v in self.industries.meta.items()
                   if k in ("source", "parse_version", "codes_total", "codes_mapped",
                            "market_field_mismatch")},
            },
            "concepts": {
                "size": self.concepts.size,
                "count": self.concepts.concept_count,
                "assignments": self.concepts.meta.get("assignments"),
                "avg": self.concepts.meta.get("avg_concepts_per_stock"),
                **{k: v for k, v in self.concepts.meta.items()
                   if k in ("source", "parse_version", "coverage", "concepts_nonempty")},
            },
            "float_shares": self.shares.info(),
            "cache": {
                "exists": self.has_cache(),
                "path": str(self.bars_path),
                **(meta or {}),
            },
            "latest_date": self.latest_date(),
        }
