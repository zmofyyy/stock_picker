"""回测 API。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..models.schemas import BacktestRequest, ImportFromBacktestRequest
from .deps import handle_error, ok, services

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


@router.get("/config", summary="回测配置与可用策略")
def backtest_config() -> Dict[str, Any]:
    """返回回测默认配置、可用策略与基准候选。"""
    try:
        svc = services()
        indexes = []
        try:
            indexes = svc.data.index_codes()
        except Exception:  # pragma: no cover
            indexes = []
        return ok(
            {
                "defaults": svc.backtest.base_config(),
                "strategies": svc.screen.strategies(),
                "benchmarks": indexes or ["000300.SH", "000001.SH"],
                "recent": svc.backtest.list_results(),
            }
        )
    except Exception as exc:
        raise handle_error(exc)


@router.post("/run", summary="执行回测")
def run_backtest(req: BacktestRequest) -> Dict[str, Any]:
    """运行组合回测，返回净值/回撤/持仓/交易明细/绩效指标 + Markdown 报告。"""
    try:
        svc = services()
        if not svc.data.reader.is_ready() and not req.universe:
            raise RuntimeError(
                "通达信目录不可用，无法获取行情数据。请先配置 data.tdx_dir，"
                "或在请求中显式提供 universe。"
            )
        cfg = req.to_engine_config(svc.backtest.base_config())
        result = svc.backtest.run(
            start=req.start,
            end=req.end,
            strategy=req.strategy,
            params=req.params,
            universe=req.universe,
            max_universe=req.max_universe,
            config_override=cfg,
            progress=False,
        )
        data = result.to_dict()
        data["markdown"] = result.to_markdown()
        data["result_id"] = svc.backtest.last_id
        metrics = data.get("metrics", {})
        return ok(
            data,
            message=(
                f"回测完成：总收益 {_pct(metrics.get('total_return'))}，"
                f"最大回撤 {_pct(metrics.get('max_drawdown'))}，"
                f"交易 {int(metrics.get('trade_count') or 0)} 笔"
            ),
        )
    except Exception as exc:
        raise handle_error(exc)


@router.get("/export", summary="导出回测结果")
def export_backtest(
    result_id: Optional[str] = Query(None, description="结果 ID；为空则导出最近一次"),
    fmt: str = Query("csv", pattern="^(csv|markdown|md|html)$"),
    kind: str = Query("trades", pattern="^(trades|positions|equity|report)$"),
) -> Any:
    """导出回测结果（交易明细 / 持仓 / 净值 / 完整报告）。"""
    try:
        svc = services()
        result = svc.backtest.get_result(result_id)
        if result is None:
            raise HTTPException(status_code=404, detail="没有可导出的回测结果，请先运行回测。")

        if fmt == "html":
            return HTMLResponse(
                result.to_html(),
                headers={"Content-Disposition": 'attachment; filename="backtest_report.html"'},
            )
        if fmt in ("markdown", "md"):
            return PlainTextResponse(
                result.to_markdown(),
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="backtest_report.md"'},
            )

        import pandas as pd

        if kind == "trades":
            df = pd.DataFrame(result.trades)
        elif kind == "positions":
            df = pd.DataFrame(result.positions)
        elif kind == "equity":
            perf = result.performance.to_dict() if result.performance else {}
            df = pd.DataFrame(perf.get("equity_curve", []))
        else:
            df = pd.DataFrame(result.trades)
        return PlainTextResponse(
            df.to_csv(index=False),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="backtest_{kind}.csv"'},
        )
    except Exception as exc:
        raise handle_error(exc)


@router.post("/to-watchlist", summary="回测持仓导入关注池")
def backtest_to_watchlist(req: ImportFromBacktestRequest) -> Dict[str, Any]:
    """把回测期末持仓导入关注池。"""
    try:
        svc = services()
        result = svc.backtest.get_result()
        if result is None:
            raise ValueError("没有可用的回测结果，请先运行回测。")
        stats = svc.tracker.import_from_backtest(result, group=req.group, only_open=req.only_open)
        return ok(stats, message=f"成功导入 {stats.get('added', 0)} 只")
    except Exception as exc:
        raise handle_error(exc)


def _pct(value: Any) -> str:
    """百分比格式化。"""
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "-"
