"""数据服务：封装通达信读取、缓存与股票池管理。

对上层（API / CLI）只暴露业务语义的方法，屏蔽 reader / cache 的细节。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from ..core.config import Config
from ..core.logging import get_logger
from ..data.basics import StockBasicResolver
from ..data.cache import BarCache
from ..data.names import NameResolver
from ..data.tdx_reader import (
    MIN_PERIODS,
    TdxDataReader,
    is_index_code,
    normalize_code,
    pytdx_status,
)
from ..strategies.registry import strategy_names_requiring_basics as basics_strategy_names

logger = get_logger("data_service")


class DataService:
    """数据服务。

    :param config: 全局配置
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._reader: Optional[TdxDataReader] = None
        self._cache: Optional[BarCache] = None
        self._names: Optional[NameResolver] = None
        self._basics: Optional[StockBasicResolver] = None

    # ------------------------------------------------------------------
    @property
    def cache(self) -> BarCache:
        """K 线缓存（惰性创建）。"""
        if self._cache is None:
            cache_dir = self.config.get_path("data.cache_dir", "./cache")
            fmt = str(self.config.get("data.cache_format", "parquet"))
            self._cache = BarCache(cache_dir, fmt=fmt)
        return self._cache

    @property
    def names(self) -> NameResolver:
        """股票名称解析器（惰性创建）。"""
        if self._names is None:
            mapping = self.config.get_path("data.cache_dir", "./cache") / "names.csv"
            self._names = NameResolver(mapping)
        return self._names

    @property
    def basics(self) -> StockBasicResolver:
        """股票基础信息（流通股本）解析器（惰性创建）。

        路径优先取 ``data.basics_file``，留空时默认 ``<cache_dir>/stock_basic.csv``。
        """
        if self._basics is None:
            raw = str(self.config.get("data.basics_file", "") or "").strip()
            path = (
                self.config.resolve_path(raw)
                if raw
                else self.config.get_path("data.cache_dir", "./cache") / "stock_basic.csv"
            )
            self._basics = StockBasicResolver(path)
        return self._basics

    @property
    def reader(self) -> TdxDataReader:
        """通达信数据读取器（惰性创建）。"""
        if self._reader is None:
            self._reader = self._build_reader()
        return self._reader

    def _build_reader(self, tdx_dir: Optional[str] = None) -> TdxDataReader:
        """构造读取器。"""
        directory = tdx_dir or str(self.config.get("data.tdx_dir", "") or "")
        adjust_file = self.config.get("data.adjust_file") or None
        return TdxDataReader(
            tdx_dir=directory,
            cache=self.cache,
            adjust=str(self.config.get("data.adjust", "none") or "none"),
            adjust_file=(
                self.config.resolve_path(adjust_file) if adjust_file else None
            ),
            start_date=self.config.get("data.start_date"),
        )

    def set_tdx_dir(self, tdx_dir: str, persist: bool = True) -> Dict[str, Any]:
        """设置通达信目录并重建读取器。

        :param tdx_dir: 新的通达信目录
        :param persist: 是否写回配置文件
        """
        self.config.set("data.tdx_dir", tdx_dir, persist=persist)
        self._reader = self._build_reader(tdx_dir)
        logger.info("通达信目录已更新为：%s", tdx_dir)
        return self.status()

    def refresh_reader(self) -> TdxDataReader:
        """根据当前配置重建读取器（配置变更后调用）。"""
        self._cache = None
        self._names = None
        self._basics = None
        self._reader = self._build_reader()
        return self._reader

    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        """数据源整体状态。"""
        st = self.reader.status()
        st["pytdx"] = pytdx_status()
        st["cache"] = {
            "dir": str(self.cache.root),
            "format": self.cache.fmt,
        }
        st["names"] = {
            "file": str(self.names.mapping_file) if self.names.mapping_file else "",
            "available": self.names.available,
            "count": self.names.size,
        }
        st["adjust"] = str(self.config.get("data.adjust", "none"))
        st["basics"] = {
            "file": str(self.basics.mapping_file) if self.basics.mapping_file else "",
            "available": self.basics.available,
            "count": self.basics.size,
            "strategies_requiring_basics": basics_strategy_names(),
            "warning": (
                ""
                if self.basics.available
                else (
                    "未找到股票基础信息文件（含流通股本），"
                    f"依赖流通盘的策略（{', '.join(basics_strategy_names()) or '无'}）"
                    "会剔除全部股票。请生成模板并填写流通股本，或在策略中开启"
                    "「缺少流通股本时放行」。"
                    if basics_strategy_names()
                    else ""
                )
            ),
        }
        st["adjust_warning"] = (
            "本地通达信日线为不复权数据；当前配置为复权模式，"
            "若未提供复权因子文件（data.adjust_file），系统会回退为不复权并告警。"
            if str(self.config.get("data.adjust", "none")) in ("qfq", "hfq")
            else ""
        )
        # 最新数据日期（取前若干只股票的最大值，避免全量扫描）
        st["last_data_date"] = self._probe_last_date()
        return st

    def _probe_last_date(self, sample: int = 20) -> Optional[str]:
        """抽样探测本地数据的最后交易日。"""
        if not self.reader.is_ready():
            return None
        codes = self.reader.scan_symbols(include_index=False)[:sample]
        latest: Optional[str] = None
        for code in codes:
            try:
                df = self.reader.read_daily(code)
            except Exception:  # pragma: no cover
                continue
            if df is None or len(df) == 0:
                continue
            d = pd.Timestamp(df["datetime"].iloc[-1]).strftime("%Y-%m-%d")
            if latest is None or d > latest:
                latest = d
        return latest

    # ------------------------------------------------------------------
    def scan_stocks(
        self,
        include_index: bool = True,
        limit: Optional[int] = None,
        keyword: Optional[str] = None,
    ) -> Dict[str, Any]:
        """扫描本地股票池。

        :param include_index: 是否包含指数
        :param limit: 最多返回条数
        :param keyword: 代码/名称模糊过滤
        :return: ``{"count", "stocks", "markets"}``
        """
        rows = self.reader.list_stocks(include_index=include_index)
        name_map = self.names.load()
        for row in rows:
            row["name"] = name_map.get(row["code"], "") or row["code"]
        if keyword:
            k = keyword.strip().upper()
            rows = [
                r for r in rows
                if k in r["code"].upper() or k in str(r["name"]).upper()
            ]
        total = len(rows)
        if limit:
            rows = rows[:limit]
        markets: Dict[str, int] = {}
        for r in rows:
            markets[r["market_name"]] = markets.get(r["market_name"], 0) + 1
        return {"count": total, "returned": len(rows), "stocks": rows, "markets": markets}

    # ------------------------------------------------------------------
    def load(
        self,
        codes: Optional[Sequence[str]] = None,
        freq: str = "daily",
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
        force: bool = False,
        tdx_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """读取并缓存行情数据。

        :param codes: 指定代码；为空则使用本地股票池（受 ``limit`` 限制）
        :param freq: ``daily`` 或分钟周期
        :param start: 起始日期
        :param end: 结束日期
        :param limit: 最多处理多少只
        :param force: 忽略缓存强制重读
        :param tdx_dir: 临时覆盖通达信目录
        :return: 加载统计
        """
        if tdx_dir:
            self.set_tdx_dir(tdx_dir)

        if freq not in ("daily", *MIN_PERIODS.keys()):
            raise ValueError(f"不支持的周期：{freq}")

        if codes:
            code_list = []
            for c in codes:
                try:
                    code_list.append(normalize_code(c))
                except ValueError:
                    logger.warning("忽略无法识别的代码：%s", c)
        else:
            code_list = self.reader.scan_symbols(include_index=False)
            if limit:
                code_list = code_list[:limit]

        if not code_list:
            return {
                "total": 0, "loaded": 0, "failed": 0, "rows": 0,
                "message": "股票池为空，请检查通达信目录是否正确。",
                "details": [],
            }

        details: List[Dict[str, Any]] = []
        loaded = 0
        failed = 0
        total_rows = 0

        for code in code_list:
            try:
                if force:
                    self.cache.remove(code, freq if freq != "daily" else "daily")
                if freq == "daily":
                    df = self.reader.read_daily(code, start=start, end=end, use_cache=not force)
                else:
                    df = self.reader.read_minute(code, freq=freq, start=start, end=end, use_cache=not force)
            except Exception as exc:  # pragma: no cover
                failed += 1
                details.append({"code": code, "ok": False, "error": str(exc)})
                continue

            if df is None or len(df) == 0:
                failed += 1
                details.append({"code": code, "ok": False, "error": "无数据"})
                continue

            loaded += 1
            total_rows += len(df)
            details.append(
                {
                    "code": code,
                    "ok": True,
                    "rows": int(len(df)),
                    "start": pd.Timestamp(df["datetime"].iloc[0]).strftime("%Y-%m-%d"),
                    "end": pd.Timestamp(df["datetime"].iloc[-1]).strftime("%Y-%m-%d"),
                }
            )

        return {
            "total": len(code_list),
            "loaded": loaded,
            "failed": failed,
            "rows": total_rows,
            "freq": freq,
            "start": start,
            "end": end,
            "details": details[:500],
            "message": f"成功加载 {loaded} 只，失败 {failed} 只，共 {total_rows} 条记录。",
        }

    # ------------------------------------------------------------------
    def preview(
        self,
        code: str,
        freq: str = "daily",
        limit: int = 30,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> Dict[str, Any]:
        """预览单只股票的行情数据。"""
        std = normalize_code(code)
        if freq == "daily":
            df = self.reader.read_daily(std, start=start, end=end)
        else:
            df = self.reader.read_minute(std, freq=freq, start=start, end=end)
        if df is None or len(df) == 0:
            return {"code": std, "rows": [], "count": 0, "name": self.names.resolve(std)}
        tail = df.tail(int(limit)).copy()
        tail["datetime"] = tail["datetime"].dt.strftime("%Y-%m-%d")
        return {
            "code": std,
            "name": self.names.resolve(std),
            "count": int(len(df)),
            "rows": tail.to_dict(orient="records"),
        }

    # ------------------------------------------------------------------
    def quality_check(
        self,
        codes: Optional[Sequence[str]] = None,
        max_codes: int = 200,
    ) -> Dict[str, Any]:
        """数据质量检查。"""
        report = self.reader.quality_check(codes=codes, max_codes=max_codes)
        data = report.to_dict()
        data["pytdx"] = pytdx_status()
        data["adjust"] = str(self.config.get("data.adjust", "none"))
        return data

    # ------------------------------------------------------------------
    def cache_info(self, limit: int = 200) -> Dict[str, Any]:
        """缓存概览。"""
        return self.cache.info(limit=limit)

    def clear_cache(self, code: Optional[str] = None) -> Dict[str, Any]:
        """清理缓存。"""
        if code:
            n = self.cache.remove(code)
            return {"removed": n, "code": code}
        n = self.cache.clear()
        return {"removed": n}

    # ------------------------------------------------------------------
    def write_names_template(self) -> Dict[str, Any]:
        """生成股票名称映射模板文件。"""
        path = self.names.write_template()
        return {"path": str(path), "exists": path.exists()}

    def update_names(self, items: Dict[str, str]) -> Dict[str, Any]:
        """更新股票名称映射。"""
        count = self.names.upsert(items)
        return {"count": count, "file": str(self.names.mapping_file)}

    # ------------------------------------------------------------------
    # 股票基础信息（流通股本）
    # ------------------------------------------------------------------
    def write_basics_template(self) -> Dict[str, Any]:
        """生成股票基础信息模板文件。"""
        path = self.basics.write_template()
        return {"path": str(path), "exists": path.exists()}

    def update_basics(self, items: Dict[str, Any]) -> Dict[str, Any]:
        """更新股票基础信息（流通股本等）。"""
        count = self.basics.upsert(items)
        return {"count": count, "file": str(self.basics.mapping_file)}

    def basics_list(self, keyword: str = "", limit: int = 500) -> Dict[str, Any]:
        """列出已配置的股票基础信息。"""
        rows = self.basics.to_list(keyword=keyword, limit=limit)
        return {
            "count": self.basics.size,
            "returned": len(rows),
            "file": str(self.basics.mapping_file) if self.basics.mapping_file else "",
            "items": rows,
        }

    # ------------------------------------------------------------------
    def ensure_ready(self) -> None:
        """确保通达信目录可用。

        :raises RuntimeError: 目录不可用时抛出
        """
        if not self.reader.is_ready():
            raise RuntimeError(
                "通达信数据目录不可用。请在「设置」或 config.yaml 中配置正确的 "
                "data.tdx_dir（该目录下应存在 vipdoc/sh/lday 等子目录）。"
            )

    def resolve_universe(
        self, universe: Optional[Sequence[str]] = None, limit: Optional[int] = None
    ) -> List[str]:
        """确定股票池：优先显式指定，其次配置文件，最后扫描本地目录。"""
        if universe:
            out = []
            for c in universe:
                try:
                    out.append(normalize_code(c))
                except ValueError:
                    continue
            return sorted(set(out))[: limit or None]

        configured = self.config.get("data.universe") or []
        if configured:
            out = []
            for c in configured:
                try:
                    out.append(normalize_code(c))
                except ValueError:
                    continue
            if out:
                return sorted(set(out))[: limit or None]

        codes = self.reader.scan_symbols(
            include_index=False,
            exclude_st=bool(self.config.get("data.exclude_st", False)),
        )
        return codes[: limit or None]

    def index_codes(self) -> List[str]:
        """返回本地可用的指数代码列表。"""
        return [c for c in self.reader.scan_symbols(include_index=True) if is_index_code(c)]
