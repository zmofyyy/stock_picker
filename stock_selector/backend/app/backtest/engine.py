"""回测引擎。

流程::

    读取数据 → 逐股票计算信号 → 拼接面板 → 逐交易日撮合 → 计算绩效

交易时序（默认 ``exec_price=next_open``）::

    T 日收盘   ：根据 T 日及之前的信号生成委托
    T+1 日开盘 ：以开盘价撮合委托（受涨跌停、停牌、资金约束）

组合规则：
    * 等权仓位（``position_sizing=equal``）：每笔买入使用 ``总权益 / max_positions``；
    * 固定资金（``position_sizing=fixed``）：每笔买入使用 ``fixed_amount``；
    * 最大持仓数量限制：超出 ``max_positions`` 的买入信号会被忽略。

一致性保证：
    信号完全由 :class:`~app.strategies.base.BaseStrategy` 产生，
    与选股、追踪共用同一套逻辑，不会出现「回测赚钱、追踪不认」的问题。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..core.logging import get_logger
from ..data.basics import StockBasicResolver, attach_float_shares
from ..data.names import NameResolver
from ..data.tdx_reader import TdxDataReader, normalize_code
from ..strategies.base import BaseStrategy
from ..strategies.registry import get_strategy
from .broker import Broker, Order, build_broker
from .performance import PerformanceResult, compute_metrics

logger = get_logger("backtest")


@dataclass
class BacktestResult:
    """回测结果容器。"""

    strategy: str = ""
    strategy_name: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    start: Optional[str] = None
    end: Optional[str] = None
    universe_size: int = 0
    elapsed: float = 0.0
    performance: Optional[PerformanceResult] = None
    trades: List[Dict[str, Any]] = field(default_factory=list)
    positions: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    account: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    benchmark_code: str = ""

    # ------------------------------------------------------------------
    def to_dict(self, include_curve: bool = True) -> Dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        perf = self.performance.to_dict() if self.performance else {}
        data: Dict[str, Any] = {
            "strategy": self.strategy,
            "strategy_name": self.strategy_name,
            "params": self.params,
            "start": self.start,
            "end": self.end,
            "universe_size": self.universe_size,
            "elapsed": round(self.elapsed, 3),
            "metrics": perf.get("metrics", {}),
            "metric_labels": perf.get("metric_labels", {}),
            "trade_stats": perf.get("trade_stats", {}),
            "trades": self.trades,
            "positions": self.positions,
            "rejected": self.rejected,
            "account": self.account,
            "warnings": self.warnings,
            "benchmark_code": self.benchmark_code,
        }
        if include_curve:
            data["equity_curve"] = perf.get("equity_curve", [])
            data["drawdown_curve"] = perf.get("drawdown_curve", [])
            data["benchmark_curve"] = perf.get("benchmark_curve", [])
            data["monthly_returns"] = perf.get("monthly_returns", [])
        return data

    # ------------------------------------------------------------------
    def to_markdown(self) -> str:
        """生成 Markdown 格式的回测报告。"""
        lines: List[str] = ["# 回测报告", ""]
        lines.append("## 回测设置")
        lines.append("")
        lines.append("| 项目 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| 策略 | {self.strategy_name} (`{self.strategy}`) |")
        lines.append(f"| 参数 | `{self.params}` |")
        lines.append(f"| 区间 | {self.start} ~ {self.end} |")
        lines.append(f"| 股票池数量 | {self.universe_size} |")
        lines.append(f"| 基准 | {self.benchmark_code or '未设置'} |")
        lines.append(f"| 耗时 | {self.elapsed:.2f} 秒 |")
        lines.append("")

        if self.warnings:
            lines.append("## 提示")
            lines.append("")
            for w in self.warnings:
                lines.append(f"> {w}")
            lines.append("")

        if self.performance:
            body = self.performance.to_markdown(title="绩效指标")
            lines.append(body)
            lines.append("")

        if self.trades:
            lines.append("## 交易明细（最近 50 笔）")
            lines.append("")
            lines.append(
                "| 日期 | 代码 | 名称 | 方向 | 价格 | 数量 | 金额 | 手续费 | 印花税 | 盈亏 | 盈亏% | 原因 |"
            )
            lines.append("|" + "---|" * 12)
            for t in self.trades[-50:]:
                lines.append(
                    "| {date} | {code} | {name} | {direction_text} | {price} | {shares} | "
                    "{amount} | {commission} | {stamp_tax} | {pnl} | {pnl_pct} | {reason} |".format(**t)
                )
            lines.append("")

        if self.rejected:
            lines.append("## 未成交记录（最近 20 笔）")
            lines.append("")
            lines.append("| 日期 | 代码 | 方向 | 原因 |")
            lines.append("|---|---|---|---|")
            for r in self.rejected[-20:]:
                lines.append(
                    f"| {r['date']} | {r['code']} | {r['direction']} | {r['reason']} |"
                )
            lines.append("")
        return "\n".join(lines)

    def to_html(self) -> str:
        """生成可直接下载的 HTML 报告。"""
        md = self.to_markdown()
        body = _markdown_to_simple_html(md)
        return (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            f"<title>回测报告 - {self.strategy_name}</title>"
            "<style>body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
            "margin:32px;max-width:1200px;line-height:1.7;color:#222}"
            "table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}"
            "th,td{border:1px solid #ddd;padding:6px 10px;text-align:left}"
            "th{background:#f5f5f5}h1{border-bottom:2px solid #eee;padding-bottom:8px}"
            "blockquote{border-left:4px solid #faad14;background:#fffbe6;margin:8px 0;padding:8px 12px}"
            "</style></head><body>" + body + "</body></html>"
        )


def _markdown_to_simple_html(md: str) -> str:
    """极简 Markdown → HTML 转换（仅用于导出，不依赖第三方库）。

    支持标题、表格、列表、引用、粗体与行内代码。
    """
    html_lines: List[str] = []
    in_table = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-: ") and c for c in cells):
                continue  # 分隔行
            if not in_table:
                html_lines.append("<table>")
                in_table = True
                html_lines.append(
                    "<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells) + "</tr>"
                )
            else:
                html_lines.append(
                    "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>"
                )
            continue
        if in_table:
            html_lines.append("</table>")
            in_table = False

        if not line:
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            text = line[level:].strip()
            html_lines.append(f"<h{min(level, 6)}>{_inline(text)}</h{min(level, 6)}>")
        elif line.startswith(">"):
            html_lines.append(f"<blockquote>{_inline(line[1:].strip())}</blockquote>")
        elif line.startswith("- "):
            html_lines.append(f"<li>{_inline(line[2:])}</li>")
        else:
            html_lines.append(f"<p>{_inline(line)}</p>")
    if in_table:
        html_lines.append("</table>")
    return "\n".join(html_lines)


def _inline(text: str) -> str:
    """处理行内粗体与代码样式。"""
    import html
    import re

    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
    return escaped


def export_trades_csv(trades: List[Dict[str, Any]], path: str) -> str:
    """导出成交明细 CSV。

    :param trades: 成交明细字典列表
    :param path: 目标路径
    :return: 写入路径
    """
    df = pd.DataFrame(trades)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


# ----------------------------------------------------------------------
# 引擎
# ----------------------------------------------------------------------
class BacktestEngine:
    """日线级组合回测引擎。

    :param reader: 通达信数据读取器
    :param strategy: 策略实例或策略名
    :param config: 回测配置字典（``config.yaml`` 的 ``backtest`` 段）
    :param name_resolver: 股票名称解析器
    """

    def __init__(
        self,
        reader: TdxDataReader,
        strategy: Optional[BaseStrategy | str] = None,
        config: Optional[Dict[str, Any]] = None,
        name_resolver: Optional[NameResolver] = None,
        basics: Optional[StockBasicResolver] = None,
    ) -> None:
        self.reader = reader
        self.names = name_resolver or NameResolver()
        self.basics = basics
        self.cfg: Dict[str, Any] = {
            "initial_cash": 1_000_000,
            "commission": 0.0003,
            "stamp_tax": 0.001,
            "slippage": 0.0002,
            "min_commission": 5.0,
            "max_positions": 10,
            "position_sizing": "equal",
            "fixed_amount": 100_000,
            "exec_price": "next_open",
            "limit_pct": 0.10,
            "limit_pct_star": 0.20,
            "t_plus_1": True,
            "benchmark": "000300.SH",
            "risk_free_rate": 0.02,
            "trading_days": 252,
        }
        self.cfg.update(config or {})
        self.strategy = self._ensure_strategy(strategy)

    # ------------------------------------------------------------------
    def _ensure_strategy(self, strategy: Optional[BaseStrategy | str]) -> BaseStrategy:
        """统一策略入参。"""
        if isinstance(strategy, BaseStrategy):
            return strategy
        if strategy is None:
            return get_strategy(None)
        return get_strategy(strategy)

    # ------------------------------------------------------------------
    def run(
        self,
        codes: Sequence[str],
        start: str,
        end: str,
        params: Optional[Dict[str, Any]] = None,
        progress: bool = True,
        max_universe: Optional[int] = None,
    ) -> BacktestResult:
        """执行回测。

        :param codes: 股票池
        :param start: 回测起始日 ``YYYY-MM-DD``
        :param end: 回测结束日 ``YYYY-MM-DD``
        :param params: 覆盖策略参数
        :param progress: 是否显示进度条
        :param max_universe: 限制股票池大小
        :return: :class:`BacktestResult`
        """
        t0 = time.time()
        if params:
            self.strategy.initialize({**self.strategy.get_params(), **params})

        result = BacktestResult(
            strategy=self.strategy.name,
            strategy_name=self.strategy.display_name,
            params=self.strategy.get_params(),
            start=str(start),
            end=str(end),
            benchmark_code=str(self.cfg.get("benchmark", "") or ""),
        )

        if getattr(self.strategy, "requires_basics", False) and (
            self.basics is None or not self.basics.available
        ):
            result.warnings.append(
                f"策略「{self.strategy.display_name}」需要流通股本数据，但未找到股票基础信息文件"
                "（默认 <cache_dir>/stock_basic.csv）。所有股票都无法通过流通盘条件，"
                "回测不会产生任何成交。请先配置流通股本，或在策略中开启「缺少流通股本时放行」。"
            )

        code_list = []
        for c in codes:
            try:
                code_list.append(normalize_code(c))
            except ValueError:
                continue
        code_list = sorted(set(code_list))
        if max_universe:
            code_list = code_list[:max_universe]
        if not code_list:
            result.warnings.append("股票池为空，无法回测。请先配置通达信目录并扫描股票池。")
            result.elapsed = time.time() - t0
            result.performance = PerformanceResult()
            return result

        # ---------------- 1. 载入数据并计算信号 ----------------
        panel = self._build_panel(code_list, start, end, progress=progress)
        if panel is None or len(panel) == 0:
            result.warnings.append(
                f"在 {start} ~ {end} 区间内没有可用的行情数据，请检查通达信数据与日期范围。"
            )
            result.elapsed = time.time() - t0
            result.performance = PerformanceResult()
            return result

        result.universe_size = int(panel.index.get_level_values(1).nunique())

        # ---------------- 2. 逐日撮合 ----------------
        broker, equity_series, position_records = self._simulate(panel, result)

        # ---------------- 3. 基准 ----------------
        benchmark = self._load_benchmark(str(start), str(end), result)

        # ---------------- 4. 绩效 ----------------
        result.performance = compute_metrics(
            equity=equity_series,
            trades=broker.trades,
            benchmark=benchmark,
            risk_free_rate=float(self.cfg.get("risk_free_rate", 0.02)),
            trading_days=int(self.cfg.get("trading_days", 252)),
            rejected_count=len(broker.rejected),
        )
        result.trades = [t.to_dict() for t in broker.trades]
        result.positions = position_records
        result.rejected = broker.rejected
        result.account = broker.summary()
        result.elapsed = time.time() - t0

        logger.info(
            "回测完成：股票池 %d，区间 %s ~ %s，成交 %d 笔，耗时 %.2fs",
            result.universe_size, start, end, len(broker.trades), result.elapsed,
        )
        return result

    # ------------------------------------------------------------------
    def _build_panel(
        self,
        codes: List[str],
        start: str,
        end: str,
        progress: bool = True,
    ) -> Optional[pd.DataFrame]:
        """构建回测面板数据。

        对每只股票计算信号后纵向拼接，索引为 ``(datetime, code)``。

        :return: 面板 DataFrame；无数据时返回 None
        """
        need = self.strategy.required_bars()
        names = self.names.load()
        frames: List[pd.DataFrame] = []

        iterator: Iterable = codes
        if progress:
            try:
                from tqdm import tqdm

                iterator = tqdm(codes, desc="回测准备数据", ncols=88)
            except Exception:  # pragma: no cover
                iterator = codes

        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)

        for code in iterator:
            try:
                df = self.reader.read_daily(code, end=end)
            except Exception as exc:  # pragma: no cover
                logger.debug("读取 %s 失败：%s", code, exc)
                continue
            if df is None or len(df) < need + 1:
                continue

            # 需要流通股本的策略（如 volume_surge）在此注入 float_shares 列
            if getattr(self.strategy, "requires_basics", False):
                df = attach_float_shares(df, code, self.basics)
                if df is None or len(df) < need + 1:
                    continue

            try:
                signals = self.strategy.generate_signals(df)
            except Exception as exc:  # pragma: no cover
                logger.debug("计算 %s 信号失败：%s", code, exc)
                continue

            part = pd.DataFrame(
                {
                    "datetime": df["datetime"].to_numpy(),
                    "code": code,
                    "name": names.get(code, code),
                    "open": df["open"].to_numpy(dtype="float64"),
                    "high": df["high"].to_numpy(dtype="float64"),
                    "low": df["low"].to_numpy(dtype="float64"),
                    "close": df["close"].to_numpy(dtype="float64"),
                    "prev_close": df["close"].shift(1).to_numpy(dtype="float64"),
                    "volume": df["volume"].to_numpy(dtype="float64"),
                    "signal": signals["signal"].to_numpy(dtype="int64"),
                    "reason": signals["reason"].astype("object").to_numpy(),
                }
            )
            part = part[
                (part["datetime"] >= start_ts) & (part["datetime"] <= end_ts)
            ]
            if len(part) > 0:
                frames.append(part)

        if not frames:
            return None
        panel = pd.concat(frames, ignore_index=True)
        panel = panel.set_index(["datetime", "code"]).sort_index()
        return panel

    # ------------------------------------------------------------------
    def _simulate(self, panel: pd.DataFrame, result: BacktestResult):
        """执行逐日撮合。

        :param panel: ``(datetime, code)`` 索引的面板数据
        :param result: 用于记录警告
        :return: ``(broker, equity_series, position_records)``
        """
        broker = build_broker(self.cfg)
        max_positions = int(self.cfg.get("max_positions", 10))
        sizing = str(self.cfg.get("position_sizing", "equal")).lower()
        fixed_amount = float(self.cfg.get("fixed_amount", 100_000))
        exec_price = str(self.cfg.get("exec_price", "next_open")).lower()

        dates = pd.DatetimeIndex(panel.index.get_level_values(0).unique()).sort_values()
        equity_points: List[Dict[str, Any]] = []
        position_records: List[Dict[str, Any]] = []
        pending: List[Order] = []

        for dt in dates:
            try:
                day = panel.loc[dt]
            except KeyError:  # pragma: no cover
                continue
            if isinstance(day, pd.Series):
                day = day.to_frame().T
            if len(day) == 0:
                continue

            broker.on_new_day(dt)
            closes = {
                code: float(row["close"])
                for code, row in day.iterrows()
                if not pd.isna(row["close"])
            }
            opens = {
                code: float(row["open"])
                for code, row in day.iterrows()
                if not pd.isna(row["open"])
            }
            prev_closes = {
                code: float(row["prev_close"]) if not pd.isna(row["prev_close"]) else float("nan")
                for code, row in day.iterrows()
            }

            # ---- 执行隔夜委托（T+1 开盘） ----
            if pending and exec_price == "next_open":
                self._execute(broker, pending, dt, opens, prev_closes, sizing, max_positions, day)
                pending = []

            # ---- 执行 T+1 收盘委托 ----
            if pending and exec_price == "next_close":
                self._execute(broker, pending, dt, closes, prev_closes, sizing, max_positions, day)
                pending = []

            # ---- 生成新委托 ----
            new_orders = self._make_orders(broker, day, dt, max_positions)
            if exec_price == "close":
                self._execute(broker, new_orders, dt, closes, prev_closes, sizing, max_positions, day)
                pending = []
            else:
                pending = new_orders

            # ---- 记录净值与持仓 ----
            equity = broker.equity(closes)
            equity_points.append(
                {
                    "date": dt,
                    "equity": equity,
                    "cash": broker.cash,
                    "position_value": broker.position_value(closes),
                    "position_count": len(broker.positions),
                }
            )
            if broker.positions:
                total_value = broker.position_value(closes)
                for row in broker.snapshots(dt, closes):
                    row["weight"] = (
                        round(row["market_value"] / total_value, 6) if total_value > 0 else 0.0
                    )
                    position_records.append(row)

        # 结束时强制平掉持仓，便于统计完整交易
        if pending:
            pending = []
        equity_series = pd.Series(
            [p["equity"] for p in equity_points],
            index=pd.DatetimeIndex([p["date"] for p in equity_points]),
            dtype="float64",
        )
        if len(equity_series) > 0 and equity_series.iloc[0] <= 0:
            result.warnings.append("期初权益异常，请检查初始资金设置。")
        return broker, equity_series, position_records

    # ------------------------------------------------------------------
    def _execute(
        self,
        broker: Broker,
        orders: List[Order],
        dt: pd.Timestamp,
        prices: Dict[str, float],
        prev_closes: Dict[str, float],
        sizing: str,
        max_positions: int,
        day: pd.DataFrame,
        fixed_amount: float = 100_000.0,
    ) -> None:
        """按顺序执行委托：先卖后买（卖出释放资金）。"""
        if not orders:
            return

        sells = [o for o in orders if o.direction == "sell"]
        buys = [o for o in orders if o.direction == "buy"]

        for order in sells:
            price = prices.get(order.code)
            if price is None or pd.isna(price):
                broker._reject(dt, order.code, "sell", "停牌或当日无行情")
                continue
            broker.try_sell(
                dt,
                order.code,
                price,
                prev_closes.get(order.code, float("nan")),
                reason=order.reason,
            )

        if not buys:
            return

        # 计算每笔买入的预算
        for order in buys:
            if order.code in broker.positions:
                continue
            if len(broker.positions) >= max_positions:
                broker._reject(dt, order.code, "buy", f"已达最大持仓数 {max_positions}")
                continue
            price = prices.get(order.code)
            if price is None or pd.isna(price):
                broker._reject(dt, order.code, "buy", "停牌或当日无行情")
                continue

            if sizing == "fixed":
                budget = fixed_amount
            else:
                # 等权：按当前总权益 / 最大持仓数分配
                equity = broker.equity(prices)
                budget = equity / max(max_positions, 1)

            budget = min(budget, broker.cash)
            if budget <= 0:
                broker._reject(dt, order.code, "buy", "可用资金不足")
                continue

            broker.try_buy(
                dt,
                order.code,
                price,
                prev_closes.get(order.code, float("nan")),
                budget,
                reason=order.reason,
                name=order.name,
            )

    # ------------------------------------------------------------------
    @staticmethod
    def _make_orders(
        broker: Broker,
        day: pd.DataFrame,
        dt: pd.Timestamp,
        max_positions: int,
    ) -> List[Order]:
        """根据当日收盘信号生成次日委托。

        :return: :class:`Order` 列表（卖出优先）
        """
        sells: List[Order] = []
        buys: List[Order] = []

        for code, row in day.iterrows():
            signal = int(row["signal"]) if not pd.isna(row["signal"]) else 0
            if signal == 0:
                continue
            reason = str(row["reason"] or "")
            name = str(row["name"] or code)

            if signal < 0:
                if code in broker.positions:
                    sells.append(
                        Order(code=code, direction="sell", signal_date=dt, reason=reason, name=name)
                    )
            elif signal > 0:
                if code not in broker.positions:
                    buys.append(
                        Order(code=code, direction="buy", signal_date=dt, reason=reason, name=name)
                    )

        buys.sort(key=lambda o: o.code)
        limit = max(max_positions - len(broker.positions) + len(sells), 0)
        return sells + buys[:limit]

    # ------------------------------------------------------------------
    def _load_benchmark(
        self, start: str, end: str, result: BacktestResult
    ) -> Optional[pd.Series]:
        """载入基准指数净值序列。"""
        code = str(self.cfg.get("benchmark", "") or "")
        if not code:
            return None
        try:
            df = self.reader.read_index(code, start=None, end=end)
        except Exception as exc:  # pragma: no cover
            logger.warning("读取基准 %s 失败：%s", code, exc)
            df = None
        if df is None or len(df) == 0:
            result.warnings.append(
                f"未能读取基准指数 {code} 的本地数据（需 {code.split('.')[1].lower()}lday 目录下存在对应文件），"
                "本次回测不含基准对比。"
            )
            result.benchmark_code = ""
            return None

        series = pd.Series(
            df["close"].to_numpy(dtype="float64"),
            index=pd.DatetimeIndex(df["datetime"]),
        )
        series = series[(series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))]
        if len(series) < 2:
            result.benchmark_code = ""
            return None
        return series
