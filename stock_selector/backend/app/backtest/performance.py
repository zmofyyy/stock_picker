"""回测绩效评估模块。

输入净值曲线与成交明细，输出完整的绩效指标：
    总收益率、年化收益率、最大回撤、夏普、索提诺、卡玛、胜率、盈亏比、
    交易次数、换手率、年化波动率，以及相对基准的 Alpha / Beta / 超额收益。

所有指标均内部处理除零与空数据，保证不会因为极端数据抛异常。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..core.logging import get_logger

logger = get_logger("performance")

# 指标中文名，用于报告输出
METRIC_LABELS: Dict[str, str] = {
    "total_return": "总收益率",
    "annual_return": "年化收益率",
    "max_drawdown": "最大回撤",
    "max_drawdown_days": "最大回撤持续天数",
    "sharpe": "夏普比率",
    "sortino": "索提诺比率",
    "calmar": "卡玛比率",
    "annual_volatility": "年化波动率",
    "win_rate": "胜率",
    "profit_loss_ratio": "盈亏比",
    "trade_count": "交易次数",
    "closed_trade_count": "平仓次数",
    "turnover": "年化换手率",
    "avg_hold_days": "平均持仓天数",
    "total_pnl": "累计盈亏",
    "benchmark_return": "基准收益率",
    "benchmark_annual_return": "基准年化收益",
    "excess_return": "超额收益",
    "alpha": "Alpha（年化）",
    "beta": "Beta",
    "final_equity": "期末权益",
    "rejected_count": "未成交笔数",
}

# 以百分比展示的指标
PERCENT_METRICS = {
    "total_return", "annual_return", "max_drawdown", "annual_volatility",
    "win_rate", "turnover", "benchmark_return", "benchmark_annual_return",
    "excess_return", "alpha",
}


@dataclass
class PerformanceResult:
    """绩效计算结果。"""

    metrics: Dict[str, float] = field(default_factory=dict)
    equity_curve: pd.DataFrame = field(default_factory=pd.DataFrame)
    drawdown_curve: pd.DataFrame = field(default_factory=pd.DataFrame)
    benchmark_curve: pd.DataFrame = field(default_factory=pd.DataFrame)
    monthly_returns: pd.DataFrame = field(default_factory=pd.DataFrame)
    trade_stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return {
            "metrics": {k: _clean(v) for k, v in self.metrics.items()},
            "metric_labels": METRIC_LABELS,
            "equity_curve": _df_to_records(self.equity_curve),
            "drawdown_curve": _df_to_records(self.drawdown_curve),
            "benchmark_curve": _df_to_records(self.benchmark_curve),
            "monthly_returns": _df_to_records(self.monthly_returns),
            "trade_stats": {k: _clean(v) for k, v in self.trade_stats.items()},
        }

    # ------------------------------------------------------------------
    def to_markdown(self, title: str = "回测绩效报告", extra: Optional[Dict[str, Any]] = None) -> str:
        """生成 Markdown 绩效报告。"""
        lines: List[str] = [f"# {title}", ""]
        if extra:
            for k, v in extra.items():
                lines.append(f"- **{k}**：{v}")
            lines.append("")

        lines.append("## 绩效指标")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|---|---|")
        for key, value in self.metrics.items():
            label = METRIC_LABELS.get(key, key)
            lines.append(f"| {label} | {_format_metric(key, value)} |")
        lines.append("")

        if self.trade_stats:
            lines.append("## 交易统计")
            lines.append("")
            lines.append("| 项目 | 数值 |")
            lines.append("|---|---|")
            for key, value in self.trade_stats.items():
                lines.append(f"| {key} | {value} |")
            lines.append("")

        if self.monthly_returns is not None and len(self.monthly_returns) > 0:
            lines.append("## 月度收益")
            lines.append("")
            cols = list(self.monthly_returns.columns)
            lines.append("| " + " | ".join(str(c) for c in cols) + " |")
            lines.append("|" + "---|" * len(cols))
            for _, row in self.monthly_returns.iterrows():
                cells = []
                for c in cols:
                    v = row[c]
                    cells.append(
                        f"{v:.2f}%" if isinstance(v, (int, float)) and not pd.isna(v) else "-"
                    )
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
        return "\n".join(lines)


def _clean(value: Any) -> Any:
    """把 numpy 值转成 Python 原生类型，NaN 转 None。"""
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return None if (np.isnan(f) or np.isinf(f)) else f
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def _df_to_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """DataFrame 转 records，并清理 NaN。"""
    if df is None or len(df) == 0:
        return []
    records: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        item: Dict[str, Any] = {}
        for col, value in row.items():
            if isinstance(value, pd.Timestamp):
                item[str(col)] = value.strftime("%Y-%m-%d")
            elif isinstance(value, (np.floating, float)):
                f = float(value)
                item[str(col)] = None if (np.isnan(f) or np.isinf(f)) else round(f, 6)
            elif isinstance(value, (np.integer,)):
                item[str(col)] = int(value)
            else:
                item[str(col)] = value
        records.append(item)
    return records


def _format_metric(key: str, value: Any) -> str:
    """按指标类型格式化数值。"""
    if value is None or (isinstance(value, float) and (np.isnan(value) or np.isinf(value))):
        return "-"
    if key in PERCENT_METRICS:
        return f"{float(value) * 100:.2f}%"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


# ----------------------------------------------------------------------
# 基础计算
# ----------------------------------------------------------------------
def to_returns(equity: pd.Series) -> pd.Series:
    """净值序列 → 日收益率序列。"""
    if equity is None or len(equity) < 2:
        return pd.Series(dtype="float64")
    return equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()


def drawdown_curve(equity: pd.Series) -> pd.Series:
    """回撤序列（负值，单位：小数）。"""
    if equity is None or len(equity) == 0:
        return pd.Series(dtype="float64")
    running_max = equity.cummax()
    dd = equity / running_max.where(running_max > 0) - 1.0
    return dd.fillna(0.0)


def max_drawdown_info(equity: pd.Series) -> Dict[str, Any]:
    """最大回撤及其持续天数。

    :return: ``{"max_drawdown": float, "max_drawdown_days": int,
              "peak_date":..., "trough_date":..., "recover_date":...}``
    """
    if equity is None or len(equity) < 2:
        return {
            "max_drawdown": 0.0,
            "max_drawdown_days": 0,
            "peak_date": None,
            "trough_date": None,
            "recover_date": None,
        }
    dd = drawdown_curve(equity)
    trough_idx = dd.idxmin()
    max_dd = float(dd.loc[trough_idx])
    if max_dd == 0:
        return {
            "max_drawdown": 0.0,
            "max_drawdown_days": 0,
            "peak_date": None,
            "trough_date": None,
            "recover_date": None,
        }

    peak_slice = equity.loc[:trough_idx]
    peak_idx = peak_slice.idxmax()
    peak_value = equity.loc[peak_idx]

    # 回撤持续：从峰值到恢复（或数据末尾）
    after = equity.loc[trough_idx:]
    recovered = after[after >= peak_value]
    recover_idx = recovered.index[0] if len(recovered) > 0 else None

    try:
        days = int((trough_idx - peak_idx).days)
    except Exception:  # pragma: no cover
        days = int(len(peak_slice))

    return {
        "max_drawdown": max_dd,
        "max_drawdown_days": max(days, 0),
        "peak_date": peak_idx,
        "trough_date": trough_idx,
        "recover_date": recover_idx,
    }


def annualized_return(equity: pd.Series, trading_days: int = 252) -> float:
    """年化收益率（按自然日跨度折算，更贴近真实体验）。"""
    if equity is None or len(equity) < 2:
        return 0.0
    start_value = float(equity.iloc[0])
    end_value = float(equity.iloc[-1])
    if start_value <= 0:
        return 0.0
    total = end_value / start_value - 1.0
    try:
        years = (pd.Timestamp(equity.index[-1]) - pd.Timestamp(equity.index[0])).days / 365.25
    except Exception:  # pragma: no cover
        years = len(equity) / trading_days
    if years <= 0:
        return total
    if total <= -1:
        return -1.0
    return float((1.0 + total) ** (1.0 / years) - 1.0)


def sharpe_ratio(returns: pd.Series, risk_free_rate: float = 0.02, trading_days: int = 252) -> float:
    """夏普比率（年化）。"""
    if returns is None or len(returns) < 2:
        return 0.0
    excess = returns - risk_free_rate / trading_days
    std = float(excess.std(ddof=1))
    if std <= 1e-12:
        return 0.0
    return float(excess.mean() / std * np.sqrt(trading_days))


def sortino_ratio(returns: pd.Series, risk_free_rate: float = 0.02, trading_days: int = 252) -> float:
    """索提诺比率（只惩罚下行波动）。"""
    if returns is None or len(returns) < 2:
        return 0.0
    excess = returns - risk_free_rate / trading_days
    downside = excess[excess < 0]
    if len(downside) < 2:
        return 0.0
    dd_std = float(downside.std(ddof=1))
    if dd_std <= 1e-12:
        return 0.0
    return float(excess.mean() / dd_std * np.sqrt(trading_days))


def _trade_stats(trades: List[Any]) -> Dict[str, Any]:
    """从成交明细中统计交易层面指标。"""
    sells = [t for t in trades if getattr(t, "direction", "") == "sell"]
    wins = [t for t in sells if t.pnl > 0]
    losses = [t for t in sells if t.pnl < 0]

    win_rate = len(wins) / len(sells) if sells else 0.0
    avg_win = float(np.mean([t.pnl for t in wins])) if wins else 0.0
    avg_loss = float(np.mean([t.pnl for t in losses])) if losses else 0.0
    profit_loss_ratio = (avg_win / abs(avg_loss)) if avg_loss < 0 else (
        float("inf") if avg_win > 0 else 0.0
    )
    hold_days = [t.hold_days for t in sells if t.hold_days is not None]

    return {
        "closed_trade_count": len(sells),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_loss_ratio": profit_loss_ratio,
        "max_win": float(max([t.pnl for t in sells])) if sells else 0.0,
        "max_loss": float(min([t.pnl for t in sells])) if sells else 0.0,
        "total_pnl": float(sum(t.pnl for t in sells)),
        "avg_hold_days": float(np.mean(hold_days)) if hold_days else 0.0,
        "total_commission": float(sum(t.commission for t in trades)),
        "total_stamp_tax": float(sum(t.stamp_tax for t in trades)),
        "total_slippage": float(sum(t.slippage_cost for t in trades)),
    }


def compute_turnover(trades: List[Any], equity: pd.Series, trading_days: int = 252) -> float:
    """年化换手率 = 累计成交金额 / 平均权益 / 年数。"""
    if not trades or equity is None or len(equity) < 2:
        return 0.0
    avg_equity = float(equity.mean())
    if avg_equity <= 0:
        return 0.0
    total_amount = float(sum(abs(t.amount) for t in trades))
    try:
        years = (pd.Timestamp(equity.index[-1]) - pd.Timestamp(equity.index[0])).days / 365.25
    except Exception:  # pragma: no cover
        years = len(equity) / trading_days
    if years <= 0:
        years = len(equity) / trading_days
    if years <= 0:
        return 0.0
    return total_amount / avg_equity / years


def monthly_return_table(equity: pd.Series) -> pd.DataFrame:
    """生成「年 × 月」收益率透视表（单位：%）。"""
    if equity is None or len(equity) < 2:
        return pd.DataFrame()
    try:
        s = equity.copy()
        s.index = pd.to_datetime(s.index)
        monthly = s.resample("ME").last().pct_change().dropna() * 100.0
        if len(monthly) == 0:
            return pd.DataFrame()
        table = pd.DataFrame(
            {
                "year": monthly.index.year,
                "month": monthly.index.month,
                "ret": monthly.to_numpy(),
            }
        )
        pivot = table.pivot(index="year", columns="month", values="ret")
        pivot = pivot.reindex(columns=range(1, 13))
        pivot.columns = [f"{m}月" for m in pivot.columns]
        pivot = pivot.round(2)
        return pivot.reset_index().rename(columns={"year": "年份"})
    except Exception as exc:  # pragma: no cover
        logger.debug("月度收益计算失败：%s", exc)
        return pd.DataFrame()


def compute_metrics(
    equity: pd.Series,
    trades: Optional[List[Any]] = None,
    benchmark: Optional[pd.Series] = None,
    risk_free_rate: float = 0.02,
    trading_days: int = 252,
    rejected_count: int = 0,
) -> PerformanceResult:
    """计算完整绩效。

    :param equity: 净值（总权益）序列，索引为日期
    :param trades: 成交明细列表（:class:`~app.backtest.broker.Trade`）
    :param benchmark: 基准净值序列（可选）
    :param risk_free_rate: 年化无风险利率
    :param trading_days: 年化交易日数
    :param rejected_count: 未成交（涨跌停/停牌/资金不足）笔数
    :return: :class:`PerformanceResult`
    """
    result = PerformanceResult()
    trades = trades or []

    if equity is None or len(equity) == 0:
        result.metrics = {}
        return result

    equity = equity.astype("float64")
    returns = to_returns(equity)
    dd = drawdown_curve(equity)
    dd_info = max_drawdown_info(equity)

    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0) if equity.iloc[0] > 0 else 0.0
    ann_return = annualized_return(equity, trading_days)
    ann_vol = float(returns.std(ddof=1) * np.sqrt(trading_days)) if len(returns) > 1 else 0.0
    max_dd = float(dd_info["max_drawdown"])
    calmar = (ann_return / abs(max_dd)) if max_dd < 0 else 0.0

    stats = _trade_stats(trades)
    turnover = compute_turnover(trades, equity, trading_days)

    metrics: Dict[str, float] = {
        "rejected_count": float(rejected_count),
        "total_return": total_return,
        "annual_return": ann_return,
        "max_drawdown": max_dd,
        "max_drawdown_days": float(dd_info["max_drawdown_days"]),
        "sharpe": sharpe_ratio(returns, risk_free_rate, trading_days),
        "sortino": sortino_ratio(returns, risk_free_rate, trading_days),
        "calmar": calmar,
        "annual_volatility": ann_vol,
        "win_rate": float(stats["win_rate"]),
        "profit_loss_ratio": float(stats["profit_loss_ratio"]),
        "trade_count": float(len(trades)),
        "closed_trade_count": float(stats["closed_trade_count"]),
        "turnover": float(turnover),
        "avg_hold_days": float(stats["avg_hold_days"]),
        "total_pnl": float(stats["total_pnl"]),
        "final_equity": float(equity.iloc[-1]),
    }

    # ---------------- 基准对比 ----------------
    if benchmark is not None and len(benchmark) > 1:
        try:
            bench = benchmark.astype("float64")
            bench = bench.reindex(equity.index).ffill().bfill()
            if bench.notna().sum() > 1 and bench.iloc[0] > 0:
                bench_norm = bench / bench.iloc[0] * equity.iloc[0]
                bench_return = float(bench.iloc[-1] / bench.iloc[0] - 1.0)
                bench_ann = annualized_return(bench, trading_days)
                bench_ret_series = to_returns(bench_norm)
                metrics["benchmark_return"] = bench_return
                metrics["benchmark_annual_return"] = bench_ann
                metrics["excess_return"] = total_return - bench_return

                joined = pd.concat(
                    [returns.rename("p"), bench_ret_series.rename("b")], axis=1
                ).dropna()
                if len(joined) > 2 and float(joined["b"].var(ddof=1)) > 1e-12:
                    beta = float(
                        np.cov(joined["p"], joined["b"], ddof=1)[0, 1]
                        / joined["b"].var(ddof=1)
                    )
                    alpha = ann_return - (
                        risk_free_rate + beta * (bench_ann - risk_free_rate)
                    )
                    metrics["beta"] = beta
                    metrics["alpha"] = float(alpha)
                    result.benchmark_curve = pd.DataFrame(
                        {
                            "date": bench_norm.index,
                            "strategy": (equity / equity.iloc[0]).to_numpy(),
                            "benchmark": (bench_norm / bench_norm.iloc[0]).to_numpy(),
                        }
                    )
        except Exception as exc:  # pragma: no cover
            logger.warning("基准对比计算失败：%s", exc)

    result.metrics = metrics
    result.equity_curve = pd.DataFrame(
        {
            "date": equity.index,
            "equity": equity.to_numpy(),
            "net_value": (equity / equity.iloc[0]).to_numpy() if equity.iloc[0] > 0 else 1.0,
            "return_pct": (returns.reindex(equity.index).fillna(0.0) * 100).to_numpy(),
        }
    )
    result.drawdown_curve = pd.DataFrame(
        {
            "date": dd.index,
            "drawdown": dd.to_numpy(),
            "drawdown_pct": (dd * 100).to_numpy(),
        }
    )
    result.monthly_returns = monthly_return_table(equity)
    result.trade_stats = stats
    return result
