"""选股模块测试：结果格式、CSV 导出、股票池解析、日期切片。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backend.app.data.names import NameResolver
from backend.app.data.tdx_reader import TdxDataReader
from backend.app.selector import Screener
from backend.app.strategies import MaCrossStrategy, VolumeBreakoutStrategy


@pytest.fixture()
def screener(tdx_root: Path) -> Screener:
    """构造选股器。"""
    reader = TdxDataReader(tdx_root)
    return Screener(reader, NameResolver())


@pytest.fixture()
def universe(tdx_root: Path) -> list:
    """测试股票池。"""
    return TdxDataReader(tdx_root).scan_symbols(include_index=False)


def test_resolve_universe(screener: Screener, tdx_root: Path) -> None:
    """股票池解析：显式指定 vs 扫描本地。"""
    codes = screener.resolve_universe(["600000", "000001"])
    assert codes == ["000001.SZ", "600000.SH"]

    scanned = screener.resolve_universe(None)
    assert len(scanned) == 4
    assert screener.resolve_universe(None, use_local_scan=False) == []


def test_run_on_specific_date(screener: Screener, universe: list) -> None:
    """指定日期选股。"""
    date = "2022-09-30"
    result = screener.run(
        strategy=MaCrossStrategy({"short_window": 5, "long_window": 20}),
        date=date,
        universe=universe,
        min_bars=30,
        progress=False,
    )
    assert result.strategy == "ma_cross"
    assert result.total_scanned == 4
    assert result.matched == len(result.results)
    assert result.elapsed >= 0

    for item in result.results:
        assert item.signal == 1
        assert item.data_date <= date


def test_result_rows_and_dataframe(screener: Screener, universe: list) -> None:
    """结果行与 DataFrame 格式（供前端表格使用）。"""
    result = screener.run(
        strategy=MaCrossStrategy({"short_window": 5, "long_window": 20}),
        date="2022-09-30",
        universe=universe,
        progress=False,
    )
    rows = result.to_rows()
    if rows:
        row = rows[0]
        for key in ("code", "name", "date", "signal", "close", "pct_change(%)", "reason"):
            assert key in row

    df = result.to_dataframe()
    assert isinstance(df, pd.DataFrame)
    assert "code" in df.columns


def test_empty_result_dataframe(screener: Screener) -> None:
    """没有命中时 DataFrame 仍应有列，避免前端报错。"""
    result = screener.run(
        strategy=MaCrossStrategy({"short_window": 5, "long_window": 20}),
        date="2022-09-30",
        universe=["999999.SH"],
        progress=False,
    )
    assert result.matched == 0
    df = result.to_dataframe()
    assert "code" in df.columns


def test_markdown_report(screener: Screener, universe: list) -> None:
    """Markdown 选股报告。"""
    result = screener.run(
        strategy=VolumeBreakoutStrategy({"high_window": 20, "volume_ratio": 1.2}),
        date="2022-09-30",
        universe=universe,
        progress=False,
    )
    md = result.to_markdown()
    assert "# 选股报告" in md
    assert "选股条件" in md
    assert "扫描股票数" in md
    if result.results:
        assert "选股结果" in md
        assert "| 代码 |" in md


def test_csv_export(screener: Screener, universe: list, tmp_path: Path) -> None:
    """CSV 导出。"""
    result = screener.run(
        strategy=MaCrossStrategy({"short_window": 5, "long_window": 20}),
        date="2022-09-30",
        universe=universe,
        progress=False,
    )
    path = result.to_csv(tmp_path / "screen" / "result.csv")
    assert Path(path).exists()
    df = pd.read_csv(path)
    assert "code" in df.columns


def test_result_to_dict(screener: Screener, universe: list) -> None:
    """结果字典包含前端需要的字段。"""
    result = screener.run(
        strategy=MaCrossStrategy({"short_window": 5, "long_window": 20}),
        date="2022-09-30",
        universe=universe,
        progress=False,
    )
    data = result.to_dict()
    for key in ("strategy", "strategy_name", "params", "conditions", "results", "matched", "warnings"):
        assert key in data


def test_empty_universe_warning(screener: Screener) -> None:
    """空股票池应返回明确提示。"""
    result = screener.run(
        strategy=MaCrossStrategy(),
        date="2022-09-30",
        universe=None,
        progress=False,
        limit_universe=0,
    )
    # universe 为 None 且限制为 0 时股票池为空
    assert result.total_scanned == 0 or result.warnings


def test_scan_all_range(screener: Screener, universe: list) -> None:
    """区间逐日扫描。"""
    result = screener.run(
        strategy=MaCrossStrategy({"short_window": 5, "long_window": 20}),
        start="2022-06-01",
        end="2022-08-31",
        scan_all=True,
        universe=universe[:2],
        progress=False,
        max_results=500,
    )
    assert result.matched == len(result.results)
    for item in result.results:
        assert item.signal == 1
        assert "2022-06-01" <= item.data_date <= "2022-08-31"


def test_scan_signals(screener: Screener, universe: list) -> None:
    """批量信号评估（不限买卖）。"""
    details = screener.scan_signals(
        strategy="ma_cross", codes=universe, date="2022-09-30"
    )
    assert len(details) == 4
    for code, detail in details.items():
        assert detail.signal in (-1, 0, 1)
        assert detail.code == code


def test_only_buy_flag(screener: Screener, universe: list) -> None:
    """only_buy=False 时应包含卖出信号。"""
    buys = screener.run(strategy="ma_cross", date="2022-09-30", universe=universe,
                        only_buy=True, progress=False)
    alls = screener.run(strategy="ma_cross", date="2022-09-30", universe=universe,
                        only_buy=False, progress=False)
    assert alls.matched >= buys.matched
