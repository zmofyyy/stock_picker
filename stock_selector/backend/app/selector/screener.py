"""选股模块。

给定策略、日期与股票池，输出满足条件的股票列表，包含：
    股票代码、名称、信号、关键因子值、触发原因、数据截止日期。

同时支持日期区间选股（逐个交易日筛选并汇总）与 CSV / Markdown 导出。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from ..core.logging import get_logger
from ..data.basics import StockBasicResolver, attach_float_shares
from ..data.names import NameResolver
from ..data.tdx_reader import TdxDataReader, normalize_code
from ..strategies.base import BaseStrategy, SignalDetail
from ..strategies.registry import get_strategy

logger = get_logger("screener")


@dataclass
class ScreeningResult:
    """选股结果容器。"""

    strategy: str = ""
    strategy_name: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    conditions: List[str] = field(default_factory=list)
    start: Optional[str] = None
    end: Optional[str] = None
    total_scanned: int = 0
    matched: int = 0
    elapsed: float = 0.0
    results: List[SignalDetail] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    def to_rows(self) -> List[Dict[str, Any]]:
        """转换为表格行（供 DataFrame / CSV / 前端表格使用）。"""
        rows: List[Dict[str, Any]] = []
        for item in self.results:
            d = item.to_dict()
            row: Dict[str, Any] = {
                "code": d["code"],
                "name": d["name"],
                "date": d["date"],
                "signal": d["signal_text"],
                "is_new_signal": "是" if d["is_new"] else "否",
                "close": d["price"],
                "pct_change(%)": (
                    None if d["pct_change"] is None else round(d["pct_change"] * 100, 2)
                ),
                "reason": d["reason_text"],
            }
            for key, value in (d["factors"] or {}).items():
                row[f"factor_{key}"] = value
            rows.append(row)
        return rows

    def to_dataframe(self) -> pd.DataFrame:
        """转换为 DataFrame。"""
        rows = self.to_rows()
        if not rows:
            return pd.DataFrame(
                columns=["code", "name", "date", "signal", "close", "pct_change(%)", "reason"]
            )
        return pd.DataFrame(rows)

    def to_dict(self) -> Dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return {
            "strategy": self.strategy,
            "strategy_name": self.strategy_name,
            "params": self.params,
            "conditions": self.conditions,
            "start": self.start,
            "end": self.end,
            "total_scanned": self.total_scanned,
            "matched": self.matched,
            "elapsed": round(self.elapsed, 3),
            "results": [r.to_dict() for r in self.results],
            "warnings": self.warnings,
        }

    # ------------------------------------------------------------------
    def to_markdown(self) -> str:
        """生成 Markdown 格式的选股报告。"""
        lines: List[str] = []
        title = f"# 选股报告 · {self.strategy_name or self.strategy}"
        lines.append(title)
        lines.append("")
        period = self.end or self.start or "-"
        lines.append(f"- **策略**：{self.strategy_name}（`{self.strategy}`）")
        lines.append(f"- **选股日期**：{period}")
        lines.append(f"- **参数**：`{self.params}`")
        lines.append(f"- **扫描股票数**：{self.total_scanned}")
        lines.append(f"- **命中数量**：{self.matched}")
        lines.append(f"- **耗时**：{self.elapsed:.2f} 秒")
        lines.append("")
        if self.conditions:
            lines.append("## 选股条件")
            lines.append("")
            for cond in self.conditions:
                lines.append(f"- {cond}")
            lines.append("")

        if not self.results:
            lines.append("> 本次选股没有符合条件的股票。")
            return "\n".join(lines)

        lines.append("## 选股结果")
        lines.append("")
        factor_cols = list(self.results[0].factors.keys())
        header = ["代码", "名称", "日期", "信号", "新信号", "收盘价", "涨跌幅(%)"] + factor_cols
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        for item in self.results:
            row = [
                item.code,
                item.name or item.code,
                item.data_date or "-",
                item.signal_text,
                "是" if item.is_new else "否",
                f"{item.price:.2f}" if item.price == item.price else "-",
                f"{item.pct_change * 100:.2f}"
                if item.pct_change == item.pct_change
                else "-",
            ]
            for col in factor_cols:
                value = item.factors.get(col)
                row.append("-" if value is None else f"{value:.4f}")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        lines.append("## 触发原因")
        lines.append("")
        for item in self.results:
            lines.append(f"- **{item.code} {item.name}**：{item.reason_text}")
        return "\n".join(lines)

    def to_csv(self, path: Path | str) -> Path:
        """导出 CSV。

        :param path: 目标文件路径
        :return: 实际写入路径
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig 便于 Excel 直接打开
        self.to_dataframe().to_csv(p, index=False, encoding="utf-8-sig")
        return p


class Screener:
    """选股引擎。

    :param reader: 通达信数据读取器
    :param name_resolver: 股票名称解析器
    :param basics: 股票基础信息（流通股本）解析器；
        当策略声明 ``requires_basics=True`` 时会向 K 线注入 ``float_shares`` 列
    """

    def __init__(
        self,
        reader: TdxDataReader,
        name_resolver: Optional[NameResolver] = None,
        basics: Optional[StockBasicResolver] = None,
    ) -> None:
        self.reader = reader
        self.names = name_resolver or NameResolver()
        self.basics = basics

    # ------------------------------------------------------------------
    def _prepare(
        self, code: str, df: Optional[pd.DataFrame], strategy: BaseStrategy
    ) -> Optional[pd.DataFrame]:
        """按策略需要为 K 线补充基础信息列。"""
        if df is None or not getattr(strategy, "requires_basics", False):
            return df
        return attach_float_shares(df, code, self.basics)

    def _basics_warning(self, strategy: BaseStrategy) -> str:
        """策略需要流通股本但数据缺失时给出明确提示。"""
        if not getattr(strategy, "requires_basics", False):
            return ""
        if self.basics is not None and self.basics.available:
            return ""
        allow_missing = bool(strategy.get_param("allow_missing_basics", False))
        tip = (
            "请在「数据管理」页生成 stock_basic.csv 模板并填写流通股本"
            "（或调用 POST /api/data/basics 上传）。"
        )
        if allow_missing:
            return (
                "未找到股票基础信息文件（流通股本），且策略已开启「缺少流通股本时放行」，"
                "本次结果的流通盘条件未校验，仅供参考。" + tip
            )
        return (
            "未找到股票基础信息文件（流通股本），所有股票都无法通过流通盘条件，"
            "本次选股结果为空。" + tip
        )

    # ------------------------------------------------------------------
    def resolve_universe(
        self,
        universe: Optional[Sequence[str]] = None,
        use_local_scan: bool = True,
        limit: Optional[int] = None,
    ) -> List[str]:
        """确定股票池。

        :param universe: 显式指定的代码列表；为空时扫描本地目录
        :param use_local_scan: universe 为空时是否扫描本地通达信目录
        :param limit: 限制股票池大小（用于快速验证）
        :return: 标准化后的代码列表
        """
        if universe:
            codes = []
            for c in universe:
                try:
                    codes.append(normalize_code(c))
                except ValueError:
                    logger.warning("忽略无法识别的代码：%s", c)
            return sorted(set(codes))
        if not use_local_scan:
            return []
        codes = self.reader.scan_symbols(include_index=False)
        # 显式传入 limit 时以它为准（limit=0 表示取 0 只，即空股票池）
        if limit is not None:
            return codes[: max(int(limit), 0)]
        return codes

    # ------------------------------------------------------------------
    def run(
        self,
        strategy: Optional[BaseStrategy | str] = None,
        date: Optional[str | _date | pd.Timestamp] = None,
        universe: Optional[Sequence[str]] = None,
        params: Optional[Dict[str, Any]] = None,
        min_bars: int = 60,
        max_results: int = 200,
        only_buy: bool = True,
        limit_universe: Optional[int] = None,
        progress: bool = True,
        start: Optional[str] = None,
        end: Optional[str] = None,
        scan_all: bool = False,
    ) -> ScreeningResult:
        """执行选股。

        :param strategy: 策略实例或策略名
        :param date: 目标交易日（单日选股）
        :param universe: 股票池；为空时扫描本地目录
        :param params: 策略参数覆盖
        :param min_bars: 最小 K 线根数
        :param max_results: 最多返回条数
        :param only_buy: 只返回买入信号
        :param limit_universe: 限制股票池数量（调试用）
        :param progress: 是否显示进度条
        :param start: 区间选股起始日期（与 ``scan_all=True`` 配合）
        :param end: 区间选股结束日期
        :param scan_all: True 时在区间内逐日扫描并汇总买入信号
        :return: :class:`ScreeningResult`
        """
        t0 = time.time()
        strat = self._ensure_strategy(strategy, params)

        result = ScreeningResult(
            strategy=strat.name,
            strategy_name=strat.display_name,
            params=strat.get_params(),
            conditions=strat.describe_conditions(),
            start=start,
            end=end,
        )

        basics_warning = self._basics_warning(strat)
        if basics_warning:
            result.warnings.append(basics_warning)

        codes = self.resolve_universe(universe, limit=limit_universe)
        if not codes:
            result.warnings.append(
                "股票池为空：请先配置通达信目录并扫描股票池，或在请求中显式指定 universe。"
            )
            result.elapsed = time.time() - t0
            return result

        result.total_scanned = len(codes)
        target = pd.Timestamp(str(date)) if date else None
        end_ts = pd.Timestamp(str(end)) if end else target
        start_ts = pd.Timestamp(str(start)) if start else None

        need = max(strat.required_bars(), int(min_bars or 0))
        matches: List[SignalDetail] = []

        iterator: Iterable = codes
        if progress:
            try:
                from tqdm import tqdm

                iterator = tqdm(codes, desc=f"[{strat.display_name}] 选股", ncols=88)
            except Exception:  # pragma: no cover
                iterator = codes

        names_map = self.names.load()

        for code in iterator:
            try:
                df = self.reader.read_daily(
                    code,
                    start=None,
                    end=(end_ts.strftime("%Y-%m-%d") if end_ts is not None else None),
                )
            except Exception as exc:  # pragma: no cover
                logger.debug("读取 %s 失败：%s", code, exc)
                continue
            if df is None or len(df) < need:
                continue

            # 需要流通股本的策略（如 volume_surge）在此注入 float_shares 列
            df = self._prepare(code, df, strat)
            if df is None or len(df) < need:
                continue

            if scan_all and start_ts is not None and end_ts is not None:
                matches.extend(
                    self._scan_range(
                        strat, code, df, start_ts, end_ts, need,
                        names_map.get(code, ""),
                    )
                )
            else:
                detail = strat.evaluate(
                    code,
                    df,
                    date=(end_ts if end_ts is not None else None),
                    name=names_map.get(code, code),
                    max_bars=need + 5,
                )
                if detail is None:
                    continue
                if only_buy and detail.signal != 1:
                    continue
                if not only_buy and detail.signal == 0:
                    continue
                matches.append(detail)

        # 排序：新信号优先 → 涨跌幅降序
        matches.sort(
            key=lambda d: (
                0 if d.is_new else 1,
                -(d.pct_change if d.pct_change == d.pct_change else -999),
            )
        )
        if max_results and len(matches) > max_results:
            matches = matches[:max_results]

        result.results = matches
        result.matched = len(matches)
        result.elapsed = time.time() - t0
        logger.info(
            "选股完成：扫描 %d 只，命中 %d 只，耗时 %.2fs",
            result.total_scanned, result.matched, result.elapsed,
        )
        return result

    # ------------------------------------------------------------------
    def _scan_range(
        self,
        strategy: BaseStrategy,
        code: str,
        df: pd.DataFrame,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
        need: int,
        name: str,
    ) -> List[SignalDetail]:
        """在日期区间内逐日评估，收集买入信号。"""
        out: List[SignalDetail] = []
        window = df[df["datetime"] <= end_ts]
        if len(window) < need:
            return out

        # 一次性计算信号，再按日期切片取用（避免重复计算）
        signals = strategy.generate_signals(window)
        dates = window["datetime"].to_numpy()
        mask = (window["datetime"] >= start_ts) & (window["datetime"] <= end_ts)

        idx_list = [i for i in range(len(window)) if mask.iloc[i] and i >= need - 1]
        for i in idx_list:
            # 通过 evaluate 复用同一套逻辑（传入已切片的数据，保证无未来函数）
            detail = strategy.evaluate(
                code, window.iloc[: i + 1], date=None, name=name, max_bars=need + 5
            )
            if detail is not None and detail.signal == 1:
                out.append(detail)
        _ = (signals, dates)  # 保留变量以便未来扩展（如批量导出全部信号）
        return out

    # ------------------------------------------------------------------
    def _ensure_strategy(
        self,
        strategy: Optional[BaseStrategy | str],
        params: Optional[Dict[str, Any]],
    ) -> BaseStrategy:
        """把策略名 / 实例统一为策略实例。"""
        if isinstance(strategy, BaseStrategy):
            if params:
                strategy.initialize({**strategy.get_params(), **params})
            return strategy
        return get_strategy(strategy, params)

    # ------------------------------------------------------------------
    def scan_signals(
        self,
        strategy: Optional[BaseStrategy | str] = None,
        codes: Optional[Sequence[str]] = None,
        date: Optional[str | pd.Timestamp] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, SignalDetail]:
        """批量评估一组股票的信号（不限买卖），用于追踪。

        :param strategy: 策略或策略名
        :param codes: 股票代码列表
        :param date: 目标日期
        :param params: 策略参数
        :return: ``{code: SignalDetail}``
        """
        strat = self._ensure_strategy(strategy, params)
        target = pd.Timestamp(str(date)) if date else None
        need = strat.required_bars()
        out: Dict[str, SignalDetail] = {}
        names_map = self.names.load()

        for code in codes or []:
            try:
                df = self.reader.read_daily(code)
            except Exception:  # pragma: no cover
                continue
            if df is None or len(df) < need:
                continue
            df = self._prepare(code, df, strat)
            if df is None or len(df) < need:
                continue
            detail = strat.evaluate(
                code,
                df,
                date=target,
                name=names_map.get(normalize_code(code), ""),
                max_bars=need + 5,
            )
            if detail is not None:
                out[detail.code] = detail
        return out
