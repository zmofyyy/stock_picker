"""回测引擎与绩效指标测试。

覆盖：
    * 绩效指标（收益、回撤、夏普、索提诺、卡玛、胜率、盈亏比、换手率）
    * 券商撮合（手续费、印花税、滑点、最低佣金、T+1、涨跌停）
    * 组合回测流程（信号 → 次日开盘成交 → 净值曲线 → 交易明细）
    * 无未来函数（延长回测区间不会改变历史成交）
    * 报告导出（Markdown / HTML / CSV）
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.app.backtest.broker import Broker, Position, Trade, build_broker
from backend.app.backtest.engine import BacktestEngine, BacktestResult, _markdown_to_simple_html
from backend.app.backtest.performance import (
    METRIC_LABELS,
    compute_metrics,
    drawdown_curve,
    max_drawdown_info,
    monthly_return_table,
    sharpe_ratio,
    sortino_ratio,
    to_returns,
)
from backend.app.data.names import NameResolver
from backend.app.data.tdx_reader import TdxDataReader
from backend.app.strategies import MaCrossStrategy, RsiStrategy


# ======================================================================
# 绩效指标
# ======================================================================
def _linear_equity(n: int = 252, start: float = 1_000_000.0, daily: float = 0.001) -> pd.Series:
    """构造稳定上涨的净值曲线。"""
    idx = pd.date_range("2022-01-03", periods=n, freq="B")
    values = [start * (1 + daily) ** i for i in range(n)]
    return pd.Series(values, index=idx, dtype="float64")


def test_to_returns() -> None:
    """收益率序列长度与数值。"""
    equity = _linear_equity(10, daily=0.01)
    returns = to_returns(equity)
    assert len(returns) == 9
    assert returns.iloc[0] == pytest.approx(0.01, rel=1e-9)


def test_total_and_annual_return() -> None:
    """总收益率与年化收益率。"""
    equity = _linear_equity(253, daily=0.001)
    result = compute_metrics(equity, [], None)
    m = result.metrics
    assert m["total_return"] == pytest.approx(equity.iloc[-1] / equity.iloc[0] - 1, rel=1e-6)
    assert m["annual_return"] > 0
    assert m["final_equity"] == pytest.approx(equity.iloc[-1])


def test_max_drawdown_known_value() -> None:
    """已知回撤路径的验证。"""
    idx = pd.date_range("2022-01-03", periods=5, freq="B")
    equity = pd.Series([100.0, 120.0, 90.0, 95.0, 130.0], index=idx)
    info = max_drawdown_info(equity)
    assert info["max_drawdown"] == pytest.approx(90.0 / 120.0 - 1.0)
    assert info["peak_date"] == idx[1]
    assert info["trough_date"] == idx[2]
    assert info["recover_date"] == idx[4]

    dd = drawdown_curve(equity)
    assert dd.min() == pytest.approx(info["max_drawdown"])
    assert dd.max() == pytest.approx(0.0)


def test_max_drawdown_flat_series() -> None:
    """无回撤时应返回 0。"""
    idx = pd.date_range("2022-01-03", periods=5, freq="B")
    info = max_drawdown_info(pd.Series([100.0] * 5, index=idx))
    assert info["max_drawdown"] == 0.0
    assert info["max_drawdown_days"] == 0


def test_sharpe_and_sortino() -> None:
    """夏普与索提诺：无波动时返回 0，正收益时为正。"""
    flat = pd.Series([0.0] * 30)
    assert sharpe_ratio(flat) == 0.0
    assert sortino_ratio(flat) == 0.0

    rng = np.random.default_rng(0)
    returns = pd.Series(rng.normal(0.001, 0.01, 260))
    assert sharpe_ratio(returns) != 0
    assert sortino_ratio(returns) != 0
    assert math.isfinite(sharpe_ratio(returns))


def test_trade_statistics() -> None:
    """胜率、盈亏比、交易次数统计。"""
    idx = pd.date_range("2022-01-03", periods=10, freq="B")
    equity = pd.Series(np.linspace(1_000_000, 1_100_000, 10), index=idx)

    trades = [
        Trade("600000.SH", "浦发", "buy", idx[0], 10.0, 1000, 10_000, 5, 0, 0),
        Trade("600000.SH", "浦发", "sell", idx[1], 11.0, 1000, 11_000, 5, 11, 0,
              pnl=900.0, pnl_pct=0.1, hold_days=3),
        Trade("600000.SH", "浦发", "buy", idx[2], 11.0, 1000, 11_000, 5, 0, 0),
        Trade("600000.SH", "浦发", "sell", idx[3], 10.0, 1000, 10_000, 5, 10, 0,
              pnl=-1100.0, pnl_pct=-0.1, hold_days=2),
        Trade("600000.SH", "浦发", "buy", idx[4], 10.0, 1000, 10_000, 5, 0, 0),
        Trade("600000.SH", "浦发", "sell", idx[5], 12.0, 1000, 12_000, 5, 12, 0,
              pnl=1900.0, pnl_pct=0.2, hold_days=5),
    ]
    result = compute_metrics(equity, trades, None, rejected_count=2)
    m = result.metrics
    assert m["win_rate"] == pytest.approx(2 / 3)
    assert m["trade_count"] == 6
    assert m["closed_trade_count"] == 3
    assert m["profit_loss_ratio"] == pytest.approx((900 + 1900) / 2 / 1100)
    assert m["rejected_count"] == 2
    assert m["turnover"] > 0
    assert result.trade_stats["win_count"] == 2
    assert result.trade_stats["loss_count"] == 1
    assert result.trade_stats["avg_hold_days"] == pytest.approx((3 + 2 + 5) / 3)


def test_benchmark_comparison() -> None:
    """基准对比：Alpha / Beta / 超额收益应被计算。"""
    idx = pd.date_range("2022-01-03", periods=252, freq="B")
    rng = np.random.default_rng(42)
    bench = pd.Series(4000 * np.cumprod(1 + rng.normal(0.0003, 0.01, 252)), index=idx)
    equity = pd.Series(1_000_000 * np.cumprod(1 + rng.normal(0.0006, 0.012, 252)), index=idx)

    result = compute_metrics(equity, [], bench)
    m = result.metrics
    assert "benchmark_return" in m
    assert "excess_return" in m
    assert "beta" in m
    assert "alpha" in m
    assert len(result.benchmark_curve) == 252


def test_metrics_handle_empty() -> None:
    """空数据不应抛异常。"""
    result = compute_metrics(pd.Series(dtype="float64"), [], None)
    assert result.metrics == {}


def test_monthly_return_table() -> None:
    """月度收益透视表。"""
    equity = _linear_equity(300, daily=0.0005)
    table = monthly_return_table(equity)
    assert "年份" in table.columns
    assert len(table) >= 1


def test_markdown_report() -> None:
    """绩效 Markdown 报告包含主要指标。"""
    equity = _linear_equity(260)
    result = compute_metrics(equity, [], None)
    md = result.to_markdown()
    assert "# 回测绩效报告" in md
    assert "总收益率" in md
    assert "最大回撤" in md
    assert METRIC_LABELS["sharpe"] in md


# ======================================================================
# 券商撮合
# ======================================================================
def test_broker_buy_sell_flow() -> None:
    """买入 → 卖出的资金、费用与盈亏。"""
    broker = Broker(initial_cash=100_000, commission=0.0003, stamp_tax=0.001,
                    slippage=0.0002, min_commission=5.0)
    broker.on_new_day(pd.Timestamp("2022-01-04"))
    trade = broker.try_buy(pd.Timestamp("2022-01-04"), "600000.SH", 10.0, 9.9, 30_000)
    assert trade is not None
    assert trade.shares % 100 == 0
    assert trade.price == pytest.approx(10.0 * 1.0002, rel=1e-6)
    assert broker.cash < 100_000

    # T+1：当日不可卖出
    broker.on_new_day(pd.Timestamp("2022-01-04"))
    assert broker.try_sell(pd.Timestamp("2022-01-04"), "600000.SH", 10.5, 10.0) is None
    assert any("T+1" in r["reason"] for r in broker.rejected)

    # 次日可卖
    broker.on_new_day(pd.Timestamp("2022-01-05"))
    sell = broker.try_sell(pd.Timestamp("2022-01-05"), "600000.SH", 11.0, 10.5)
    assert sell is not None
    assert sell.stamp_tax > 0
    assert sell.pnl != 0
    assert "600000.SH" not in broker.positions
    assert broker.cash > 0


def test_broker_limit_up_blocks_buy() -> None:
    """涨停无法买入。"""
    broker = Broker(initial_cash=100_000)
    broker.on_new_day(pd.Timestamp("2022-01-04"))
    # 主板 10% 涨停
    assert broker.try_buy(pd.Timestamp("2022-01-04"), "600000.SH", 11.0, 10.0, 50_000) is None
    assert any("涨停" in r["reason"] for r in broker.rejected)
    # 创业板 20%：+10% 不算涨停，+20% 才算
    assert broker.is_limit_up("300750.SZ", 11.0, 10.0) is False
    assert broker.is_limit_up("300750.SZ", 12.0, 10.0) is True


def test_broker_limit_down_blocks_sell() -> None:
    """跌停无法卖出。"""
    broker = Broker(initial_cash=100_000)
    broker.on_new_day(pd.Timestamp("2022-01-04"))
    broker.try_buy(pd.Timestamp("2022-01-04"), "600000.SH", 10.0, 10.0, 50_000)
    broker.on_new_day(pd.Timestamp("2022-01-05"))
    assert broker.try_sell(pd.Timestamp("2022-01-05"), "600000.SH", 9.0, 10.0) is None
    assert any("跌停" in r["reason"] for r in broker.rejected)


def test_broker_insufficient_cash() -> None:
    """资金不足时无法买入。"""
    broker = Broker(initial_cash=500)
    broker.on_new_day(pd.Timestamp("2022-01-04"))
    assert broker.try_buy(pd.Timestamp("2022-01-04"), "600000.SH", 50.0, 49.0, 1000) is None
    assert any("资金不足" in r["reason"] for r in broker.rejected)


def test_broker_min_commission() -> None:
    """小额交易按最低佣金收取。"""
    broker = Broker(initial_cash=100_000, commission=0.0003, min_commission=5.0)
    broker.on_new_day(pd.Timestamp("2022-01-04"))
    trade = broker.try_buy(pd.Timestamp("2022-01-04"), "600000.SH", 10.0, 9.9, 2_000)
    assert trade is not None
    assert trade.commission == pytest.approx(5.0)


def test_broker_position_averaging() -> None:
    """加仓后成本价被摊薄。"""
    broker = Broker(initial_cash=1_000_000, min_commission=0.0, commission=0.0, slippage=0.0)
    broker.on_new_day(pd.Timestamp("2022-01-04"))
    broker.try_buy(pd.Timestamp("2022-01-04"), "600000.SH", 10.0, 9.9, 100_000)
    broker.on_new_day(pd.Timestamp("2022-01-05"))
    broker.try_buy(pd.Timestamp("2022-01-05"), "600000.SH", 20.0, 19.0, 100_000)

    pos = broker.positions["600000.SH"]
    assert pos.shares == 15_000
    assert pos.avg_cost == pytest.approx(20.0 / 1.5, rel=1e-6)


def test_broker_t_plus_1_disabled() -> None:
    """关闭 T+1 后可当日卖出。"""
    broker = Broker(initial_cash=100_000, t_plus_1=False)
    broker.on_new_day(pd.Timestamp("2022-01-04"))
    broker.try_buy(pd.Timestamp("2022-01-04"), "600000.SH", 10.0, 9.9, 50_000)
    assert broker.try_sell(pd.Timestamp("2022-01-04"), "600000.SH", 10.5, 10.0) is not None


def test_build_broker_from_config() -> None:
    """从配置字典构建券商。"""
    broker = build_broker({"initial_cash": 500_000, "max_positions": 5, "slippage": 0.001})
    assert broker.initial_cash == 500_000
    assert broker.slippage == 0.001


# ======================================================================
# 回测引擎
# ======================================================================
@pytest.fixture()
def engine(tdx_root: Path) -> BacktestEngine:
    """构造回测引擎。"""
    reader = TdxDataReader(tdx_root)
    return BacktestEngine(
        reader=reader,
        strategy=MaCrossStrategy({"short_window": 5, "long_window": 20}),
        config={
            "initial_cash": 1_000_000,
            "max_positions": 3,
            "position_sizing": "equal",
            "exec_price": "next_open",
            "commission": 0.0003,
            "stamp_tax": 0.001,
            "slippage": 0.0002,
            "benchmark": "000300.SH",
        },
        name_resolver=NameResolver(),
    )


def test_engine_run(engine: BacktestEngine, tdx_root: Path) -> None:
    """完整回测流程。"""
    reader = TdxDataReader(tdx_root)
    codes = reader.scan_symbols(include_index=False)
    result = engine.run(codes, start="2022-03-01", end="2022-10-31", progress=False)

    assert isinstance(result, BacktestResult)
    assert result.universe_size == 4
    assert result.performance is not None
    assert len(result.performance.equity_curve) > 100
    metrics = result.performance.metrics
    for key in (
        "total_return", "annual_return", "max_drawdown", "sharpe", "sortino",
        "calmar", "win_rate", "profit_loss_ratio", "trade_count", "turnover",
    ):
        assert key in metrics, f"缺少指标 {key}"

    if result.trades:
        assert all(t["shares"] % 100 == 0 for t in result.trades)
        assert all(t["commission"] > 0 for t in result.trades)

    # 净值曲线与回撤曲线长度一致
    assert len(result.performance.equity_curve) == len(result.performance.drawdown_curve)


def test_engine_executes_at_next_open(engine: BacktestEngine, tdx_root: Path) -> None:
    """成交价应为成交日开盘价（含滑点）。"""
    reader = TdxDataReader(tdx_root)
    codes = reader.scan_symbols(include_index=False)
    result = engine.run(codes, start="2022-03-01", end="2022-08-31", progress=False)
    if not result.trades:
        pytest.skip("本次回测没有产生交易")

    slippage = 0.0002
    for trade in result.trades[:20]:
        df = reader.read_daily(trade["code"], end=result.end)
        row = df[df["datetime"] == pd.Timestamp(trade["date"])]
        if len(row) == 0:
            continue
        open_price = float(row["open"].iloc[0])
        expected = round(
            open_price * (1 + slippage) if trade["direction"] == "buy" else open_price * (1 - slippage),
            4,
        )
        assert trade["price"] == pytest.approx(expected, abs=1e-3)


def test_engine_no_lookahead(engine: BacktestEngine, tdx_root: Path) -> None:
    """延长回测区间不应改变区间内已有的成交记录。"""
    reader = TdxDataReader(tdx_root)
    codes = reader.scan_symbols(include_index=False)

    short = engine.run(codes, start="2022-03-01", end="2022-07-31", progress=False)
    long = engine.run(codes, start="2022-03-01", end="2022-12-31", progress=False)

    cutoff = pd.Timestamp("2022-07-31")
    short_trades = [(t["date"], t["code"], t["direction"], t["shares"]) for t in short.trades]
    long_trades = [
        (t["date"], t["code"], t["direction"], t["shares"])
        for t in long.trades
        if pd.Timestamp(t["date"]) <= cutoff
    ]
    assert short_trades == long_trades[: len(short_trades)]


def test_engine_empty_universe(engine: BacktestEngine) -> None:
    """空股票池应给出明确提示而不是崩溃。"""
    result = engine.run([], start="2022-01-01", end="2022-12-31", progress=False)
    assert result.universe_size == 0
    assert result.warnings


def test_engine_fixed_sizing(tdx_root: Path) -> None:
    """固定资金仓位模式。"""
    reader = TdxDataReader(tdx_root)
    engine = BacktestEngine(
        reader=reader,
        strategy=RsiStrategy({"period": 14, "buy_threshold": 45, "sell_threshold": 55}),
        config={
            "initial_cash": 1_000_000,
            "max_positions": 3,
            "position_sizing": "fixed",
            "fixed_amount": 100_000,
            "exec_price": "close",
            "benchmark": "",
        },
    )
    codes = reader.scan_symbols(include_index=False)
    result = engine.run(codes, start="2022-03-01", end="2022-10-31", progress=False)
    assert result.performance is not None
    if result.trades:
        first_buy = next(t for t in result.trades if t["direction"] == "buy")
        assert first_buy["amount"] <= 110_000


def test_engine_reports(engine: BacktestEngine, tdx_root: Path) -> None:
    """报告导出（Markdown / HTML / dict）。"""
    reader = TdxDataReader(tdx_root)
    codes = reader.scan_symbols(include_index=False)
    result = engine.run(codes, start="2022-03-01", end="2022-10-31", progress=False)

    md = result.to_markdown()
    assert "# 回测报告" in md
    assert "回测设置" in md

    html = result.to_html()
    assert html.startswith("<!DOCTYPE html>")
    assert "<table>" in html

    data = result.to_dict()
    assert data["strategy"] == "ma_cross"
    assert "equity_curve" in data
    assert "metrics" in data

    simple = _markdown_to_simple_html("# 标题\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<h1>标题</h1>" in simple
    assert "<td>1</td>" in simple
