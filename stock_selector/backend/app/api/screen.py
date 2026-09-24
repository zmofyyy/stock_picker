"""选股 API。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import PlainTextResponse

from ..models.schemas import AddToWatchlistRequest, ScreenRequest
from .deps import handle_error, ok, services

router = APIRouter(prefix="/api/screen", tags=["screen"])


@router.get("", summary="选股元信息")
def screen_meta() -> Dict[str, Any]:
    """返回可用策略、默认参数与最近一次选股摘要。

    前端「选股」页初始化时调用，用于渲染策略下拉框与参数表单。
    """
    try:
        svc = services()
        last = svc.screen.last_result()
        return ok(
            {
                "strategies": svc.screen.strategies(),
                "default_strategy": svc.config.get("strategy.default", "ma_cross"),
                "defaults": {
                    "min_bars": svc.config.get("screen.min_bars", 60),
                    "max_results": svc.config.get("screen.max_results", 200),
                },
                "last_result": {
                    "strategy": last.strategy,
                    "strategy_name": last.strategy_name,
                    "matched": last.matched,
                    "total_scanned": last.total_scanned,
                    "end": last.end,
                }
                if last
                else None,
            }
        )
    except Exception as exc:
        raise handle_error(exc)


@router.post("/run", summary="执行选股")
def run_screen(req: ScreenRequest) -> Dict[str, Any]:
    """按策略、日期与股票池执行选股。

    返回结构化结果 + Markdown 报告，前端可直接渲染表格与报告。
    """
    try:
        svc = services()
        if not svc.data.reader.is_ready() and not req.universe:
            raise RuntimeError(
                "通达信目录不可用，无法扫描股票池。请先配置 data.tdx_dir，"
                "或在请求中显式提供 universe。"
            )
        result = svc.screen.run(
            strategy=req.strategy,
            params=req.params,
            date=req.date,
            start=req.start,
            end=req.end,
            scan_all=req.scan_all,
            universe=req.universe,
            min_bars=req.min_bars,
            max_results=req.max_results,
            only_buy=req.only_buy,
            limit_universe=req.limit_universe,
            progress=False,
        )
        data = result.to_dict()
        data["markdown"] = result.to_markdown()
        data["rows"] = result.to_rows()
        return ok(data, message=f"扫描 {result.total_scanned} 只，命中 {result.matched} 只")
    except Exception as exc:
        raise handle_error(exc)


@router.get("/export", summary="导出选股结果")
def export_screen(
    result_id: Optional[str] = Query(None, description="结果 ID；为空则导出最近一次"),
    fmt: str = Query("csv", pattern="^(csv|markdown|md)$"),
) -> Any:
    """导出选股结果为 CSV 或 Markdown。"""
    try:
        svc = services()
        result = svc.screen.get_result(result_id) if result_id else svc.screen.last_result()
        if result is None:
            raise HTTPException(status_code=404, detail="没有可导出的选股结果，请先执行选股。")
        if fmt in ("markdown", "md"):
            return PlainTextResponse(
                result.to_markdown(),
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="screening_report.md"'},
            )
        csv_text = result.to_dataframe().to_csv(index=False)
        return PlainTextResponse(
            csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="screening_result.csv"'},
        )
    except Exception as exc:
        raise handle_error(exc)


@router.post("/to-watchlist", summary="选股结果加入关注池")
def to_watchlist(req: AddToWatchlistRequest) -> Dict[str, Any]:
    """把选股结果（或指定代码）批量加入关注池。"""
    try:
        svc = services()
        if req.codes:
            items: List[Dict[str, Any]] = [
                {
                    "code": c,
                    "strategy": req.strategy or "",
                    "note": req.note or "手动加入",
                    "tags": ["手动"],
                }
                for c in req.codes
            ]
            stats = svc.tracker.add_watch_batch(
                items, group=req.group, source="manual"
            )
        else:
            last = svc.screen.last_result()
            if last is None:
                raise ValueError("没有可用的选股结果，请先执行选股或显式提供 codes。")
            stats = svc.tracker.import_from_screen(last, group=req.group, strategy=req.strategy)
        return ok(stats, message=f"成功加入 {stats.get('added', 0)} 只")
    except Exception as exc:
        raise handle_error(exc)
