"""追踪报告测试：每日 / 区间 / 汇总报告与 Markdown→HTML 渲染。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backend.app.tracker.report import ReportBuilder, markdown_to_html

CODES = ["600000.SH", "000001.SZ", "300750.SZ", "830000.BJ"]


@pytest.fixture()
def prepared(services):
    """准备关注池与若干天的追踪数据。"""
    from backend.app.tracker.notifier import Notifier

    services.tracker.notifier = Notifier({"console": False, "csv": False})
    services.tracker._tracker = None
    tracker = services.tracker.tracker

    for code in CODES:
        services.tracker.add_watch(code=code, group="测试", strategy="ma_cross")
    services.tracker.add_watch(
        code="600000.SH", group="测试", strategy="ma_cross",
        cost_price=10.0, shares=1000, stop_price=8.0, target_price=20.0,
    )

    df = services.data.reader.read_daily("600000.SH")
    dates = [d.strftime("%Y-%m-%d") for d in df["datetime"]]
    for d in dates[60:140]:
        tracker.update(codes=CODES, date=d, notify=False)

    return services, ReportBuilder(services.tracker.storage, services.data.names), dates


# ----------------------------------------------------------------------
def test_daily_report(prepared) -> None:
    """每日追踪报告。"""
    services, builder, dates = prepared
    report = builder.daily(date=dates[130])
    assert report.kind == "daily"
    md = report.markdown
    assert "# 每日追踪报告" in md
    assert "## 一、今日概览" in md
    assert "## 二、今日新增信号" in md
    assert "## 三、今日状态变更" in md
    assert "## 四、风险提醒" in md
    assert "## 五、持仓盈亏" in md
    assert report.summary["date"] == dates[130]
    assert report.summary["watch_count"] == 4


def test_daily_report_default_date(prepared) -> None:
    """不指定日期时使用状态表的最新数据日期。"""
    services, builder, dates = prepared
    report = builder.daily()
    assert report.summary["date"] == dates[139]


def test_daily_report_empty(services) -> None:
    """空关注池时生成空报告而不是报错。"""
    builder = ReportBuilder(services.tracker.storage, services.data.names)
    report = builder.daily()
    assert "关注池为空" in report.markdown


def test_range_report(prepared) -> None:
    """区间追踪报告（含时间线）。"""
    services, builder, dates = prepared
    report = builder.range_report(code="600000.SH", start=dates[60], end=dates[139])
    assert report.kind == "range"
    md = report.markdown
    assert "一、状态时间线" in md
    assert "二、信号明细" in md
    assert "三、状态分布" in md
    assert report.summary["code"] == "600000.SH"
    assert report.summary["state_changes"] >= 0


def test_range_report_no_code(prepared) -> None:
    """不指定股票时统计整个关注池。"""
    services, builder, dates = prepared
    report = builder.range_report(start=dates[60], end=dates[139])
    assert "全部关注池" in report.title


def test_range_report_no_data(prepared) -> None:
    """区间内无记录时的提示。"""
    services, builder, dates = prepared
    report = builder.range_report(code="830000.BJ", start="1990-01-01", end="1990-12-31")
    assert "没有状态变更记录" in report.markdown


def test_summary_report(prepared) -> None:
    """汇总报告。"""
    services, builder, dates = prepared
    report = builder.summary()
    assert report.kind == "summary"
    md = report.markdown
    assert "# 关注池汇总报告" in md
    assert "一、状态分布" in md
    assert "二、关注池明细" in md
    assert "三、关键指标" in md
    assert report.summary["total"] == 4
    assert sum(report.summary["state_distribution"].values()) == 4


def test_summary_report_group_filter(prepared) -> None:
    """分组过滤。"""
    services, builder, dates = prepared
    report = builder.summary(group="不存在的分组")
    assert report.summary["total"] == 0


def test_report_to_html(prepared) -> None:
    """报告可导出为独立 HTML。"""
    services, builder, dates = prepared
    html = builder.daily(date=dates[130]).to_html()
    assert html.startswith("<!DOCTYPE html>")
    assert "<table>" in html
    assert "</html>" in html


def test_report_to_csv(prepared, tmp_path: Path) -> None:
    """报告明细可导出 CSV。"""
    services, builder, dates = prepared
    report = builder.range_report(code="600000.SH", start=dates[60], end=dates[139])
    path = report.to_csv(str(tmp_path / "history.csv"))
    df = pd.read_csv(path)
    assert "code" in df.columns or len(df) == 0


def test_report_to_dict(prepared) -> None:
    """报告字典包含 markdown 与 html。"""
    services, builder, dates = prepared
    data = builder.daily(date=dates[130]).to_dict()
    assert set(("kind", "title", "markdown", "html", "summary", "rows")) <= set(data)


def test_screening_and_backtest_wrappers(services) -> None:
    """选股 / 回测结果的报告包装。"""
    builder = ReportBuilder(services.tracker.storage, services.data.names)

    screen = services.screen.run(
        strategy="ma_cross", date="2022-09-30", universe=["600000.SH"], progress=False
    )
    report = builder.screening(screen)
    assert report.kind == "screening"
    assert "选股报告" in report.title

    codes = services.data.reader.scan_symbols(include_index=False)
    bt = services.backtest.run(
        start="2022-03-01", end="2022-10-31", strategy="ma_cross",
        universe=codes, progress=False,
    )
    report = builder.backtest(bt)
    assert report.kind == "backtest"
    assert "回测报告" in report.title
    assert "metrics" in report.to_dict()["summary"] or report.summary


# ----------------------------------------------------------------------
# Markdown → HTML
# ----------------------------------------------------------------------
def test_markdown_to_html_basic() -> None:
    """标题、段落、粗体、代码。"""
    html = markdown_to_html("# 标题\n\n这是**粗体**和`代码`。\n")
    assert "<h1>标题</h1>" in html
    assert "<strong>粗体</strong>" in html
    assert "<code>代码</code>" in html


def test_markdown_to_html_table() -> None:
    """表格转换。"""
    md = "| 名称 | 数值 |\n|---|---|\n| A | 1 |\n| B | 2 |\n"
    html = markdown_to_html(md)
    assert html.count("<table>") == 1
    assert html.count("<tr>") == 3
    assert "<th>名称</th>" in html
    assert "<td>2</td>" in html


def test_markdown_to_html_list_and_quote() -> None:
    """列表与引用。"""
    html = markdown_to_html("- 一\n- 二\n\n> 引用\n")
    assert html.count("<li>") == 2
    assert "<blockquote>引用</blockquote>" in html


def test_markdown_to_html_escapes() -> None:
    """HTML 特殊字符应被转义，避免注入。"""
    html = markdown_to_html("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
