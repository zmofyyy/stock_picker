"""报告中心 API。

支持三类追踪报告（每日 / 区间 / 汇总），并统一支持：
    * ``format=json``     返回结构化数据 + Markdown + HTML（供前端渲染）
    * ``format=markdown`` 直接下载 .md
    * ``format=html``     直接下载 .html
    * ``format=csv``      直接下载明细 .csv
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..tracker.report import Report
from .deps import handle_error, ok, services

router = APIRouter(prefix="/api/report", tags=["report"])


# ----------------------------------------------------------------------
def build_report(
    kind: str = "daily",
    date: Optional[str] = None,
    code: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    group: Optional[str] = None,
) -> Report:
    """生成指定类型的报告。

    :param kind: ``daily`` / ``range`` / ``summary``
    """
    svc = services()
    kind = (kind or "daily").lower()
    if kind == "daily":
        return svc.tracker.daily_report(date=date, group=group)
    if kind == "range":
        return svc.tracker.range_report(code=code, start=start, end=end, group=group)
    if kind == "summary":
        return svc.tracker.summary_report(group=group)
    raise ValueError(f"不支持的报告类型：{kind}（可选 daily / range / summary）")


def render_report(report: Report, fmt: str = "json") -> Any:
    """按格式渲染报告。"""
    fmt = (fmt or "json").lower()
    if fmt == "markdown" or fmt == "md":
        return PlainTextResponse(
            report.markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{report.kind}_report.md"'},
        )
    if fmt == "html":
        return HTMLResponse(
            report.to_html(),
            headers={"Content-Disposition": f'attachment; filename="{report.kind}_report.html"'},
        )
    if fmt == "csv":
        import pandas as pd

        df = pd.DataFrame(report.csv_rows)
        return PlainTextResponse(
            df.to_csv(index=False),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{report.kind}_report.csv"'},
        )
    return ok(report.to_dict())


# ----------------------------------------------------------------------
@router.get("", summary="生成追踪报告")
def get_report(
    kind: str = Query("daily", pattern="^(daily|range|summary)$"),
    date: Optional[str] = None,
    code: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    group: Optional[str] = None,
    fmt: str = Query("json", pattern="^(json|markdown|md|html|csv)$"),
) -> Any:
    """生成每日 / 区间 / 汇总追踪报告。"""
    try:
        return render_report(build_report(kind, date, code, start, end, group), fmt)
    except Exception as exc:
        raise handle_error(exc)


@router.get("/daily", summary="每日追踪报告")
def daily_report(
    date: Optional[str] = None,
    group: Optional[str] = None,
    fmt: str = Query("json", pattern="^(json|markdown|md|html|csv)$"),
) -> Any:
    """今日新增信号、状态变更、风险提醒、持仓盈亏。"""
    try:
        return render_report(build_report("daily", date=date, group=group), fmt)
    except Exception as exc:
        raise handle_error(exc)


@router.get("/range", summary="区间追踪报告")
def range_report(
    code: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    group: Optional[str] = None,
    fmt: str = Query("json", pattern="^(json|markdown|md|html|csv)$"),
) -> Any:
    """某只股票（或整个关注池）在指定区间内的状态时间线。"""
    try:
        return render_report(
            build_report("range", code=code, start=start, end=end, group=group), fmt
        )
    except Exception as exc:
        raise handle_error(exc)


@router.get("/summary", summary="关注池汇总报告")
def summary_report(
    group: Optional[str] = None,
    fmt: str = Query("json", pattern="^(json|markdown|md|html|csv)$"),
) -> Any:
    """关注池所有股票的当前状态、上次状态、变更时间与关键指标。"""
    try:
        return render_report(build_report("summary", group=group), fmt)
    except Exception as exc:
        raise handle_error(exc)


@router.get("/types", summary="支持的报告类型")
def report_types() -> Dict[str, Any]:
    """返回报告类型元信息，供前端渲染选项卡。"""
    return ok(
        [
            {"kind": "daily", "name": "每日追踪报告", "desc": "今日信号、状态变更、风险提醒、持仓盈亏"},
            {"kind": "range", "name": "区间追踪报告", "desc": "指定区间内的状态时间线与信号明细"},
            {"kind": "summary", "name": "汇总报告", "desc": "关注池当前状态与关键指标总览"},
        ]
    )
