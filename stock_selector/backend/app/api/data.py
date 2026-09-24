"""数据管理 API。

提供通达信目录配置、股票池扫描、行情读取、缓存管理与数据质量检查。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from ..models.schemas import (
    BasicsUploadRequest,
    DataLoadRequest,
    NamesUploadRequest,
    QualityCheckRequest,
)
from .deps import handle_error, ok, services

router = APIRouter(prefix="/api/data", tags=["data"])


@router.get("/status", summary="数据源状态")
def data_status() -> Dict[str, Any]:
    """返回通达信目录、缓存、名称映射、pytdx 可用性等状态。"""
    try:
        return ok(services().data.status())
    except Exception as exc:  # pragma: no cover
        raise handle_error(exc)


@router.get("/stocks", summary="扫描本地股票池")
def list_stocks(
    include_index: bool = Query(True, description="是否包含指数"),
    limit: Optional[int] = Query(None, ge=1, description="最多返回条数"),
    keyword: Optional[str] = Query(None, description="代码或名称模糊搜索"),
) -> Dict[str, Any]:
    """扫描通达信本地目录，返回股票池。"""
    try:
        svc = services()
        if not svc.data.reader.is_ready():
            return ok(
                {
                    "count": 0,
                    "returned": 0,
                    "stocks": [],
                    "markets": {},
                    "warning": "通达信目录不可用，请先在设置中配置 data.tdx_dir",
                }
            )
        return ok(svc.data.scan_stocks(include_index=include_index, limit=limit, keyword=keyword))
    except Exception as exc:
        raise handle_error(exc)


@router.post("/load", summary="读取并缓存行情数据")
def load_data(req: DataLoadRequest) -> Dict[str, Any]:
    """读取日线或分钟线数据并写入缓存。"""
    try:
        svc = services()
        if not svc.data.reader.is_ready() and not req.tdx_dir:
            raise RuntimeError(
                "通达信目录不可用。请配置 data.tdx_dir（需包含 vipdoc/sh/lday 等子目录）。"
            )
        result = svc.data.load(
            codes=req.codes,
            freq=req.freq,
            start=req.start,
            end=req.end,
            limit=req.limit,
            force=req.force,
            tdx_dir=req.tdx_dir,
        )
        return ok(result, message=result.get("message", ""))
    except Exception as exc:
        raise handle_error(exc)


@router.get("/preview/{code}", summary="预览单只股票行情")
def preview(
    code: str,
    freq: str = Query("daily"),
    limit: int = Query(30, ge=1, le=1000),
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[str, Any]:
    """预览某只股票的最近若干根 K 线。"""
    try:
        return ok(services().data.preview(code, freq=freq, limit=limit, start=start, end=end))
    except Exception as exc:
        raise handle_error(exc)


@router.post("/quality", summary="数据质量检查")
def quality_check(req: QualityCheckRequest) -> Dict[str, Any]:
    """检查本地数据的完整性、异常值与新鲜度。"""
    try:
        return ok(services().data.quality_check(codes=req.codes, max_codes=req.max_codes))
    except Exception as exc:
        raise handle_error(exc)


@router.get("/cache", summary="缓存概览")
def cache_info(limit: int = Query(200, ge=1, le=5000)) -> Dict[str, Any]:
    """返回缓存文件数量、体积与明细。"""
    try:
        return ok(services().data.cache_info(limit=limit))
    except Exception as exc:
        raise handle_error(exc)


@router.delete("/cache", summary="清理缓存")
def clear_cache(code: Optional[str] = Query(None, description="为空则清空全部缓存")) -> Dict[str, Any]:
    """清理缓存。"""
    try:
        result = services().data.clear_cache(code=code)
        return ok(result, message=f"已删除 {result.get('removed', 0)} 个缓存文件")
    except Exception as exc:
        raise handle_error(exc)


@router.get("/tdx-dir", summary="获取通达信目录候选")
def tdx_dir_info(directory: Optional[str] = Query(None, description="待检测目录")) -> Dict[str, Any]:
    """检测指定目录是否为有效的通达信数据目录。"""
    try:
        from ..data.tdx_reader import find_vipdoc_dir

        svc = services()
        target = directory or str(svc.config.get("data.tdx_dir", ""))
        vipdoc = find_vipdoc_dir(target) if target else None
        return ok(
            {
                "input": target,
                "vipdoc": str(vipdoc) if vipdoc else "",
                "valid": vipdoc is not None,
                "current": str(svc.config.get("data.tdx_dir", "")),
            }
        )
    except Exception as exc:
        raise handle_error(exc)


@router.post("/tdx-dir", summary="设置通达信目录")
def set_tdx_dir(payload: Dict[str, Any]) -> Dict[str, Any]:
    """设置并验证通达信目录。

    请求体：``{"tdx_dir": "C:/new_tdx", "persist": true}``
    """
    try:
        from ..data.tdx_reader import find_vipdoc_dir

        directory = str(payload.get("tdx_dir", "") or "").strip()
        if not directory:
            raise ValueError("tdx_dir 不能为空")
        persist = bool(payload.get("persist", True))
        svc = services()
        if find_vipdoc_dir(directory) is None:
            raise ValueError(
                f"目录 {directory} 中未找到 vipdoc 子目录，请确认是否为通达信安装目录。"
            )
        return ok(svc.data.set_tdx_dir(directory, persist=persist), message="通达信目录已更新")
    except Exception as exc:
        raise handle_error(exc)


# ----------------------------------------------------------------------
# 股票名称映射
# ----------------------------------------------------------------------
@router.post("/names/template", summary="生成名称映射模板")
def create_names_template() -> Dict[str, Any]:
    """在缓存目录生成 names.csv 模板。"""
    try:
        return ok(services().data.write_names_template(), message="模板已生成")
    except Exception as exc:
        raise handle_error(exc)


@router.post("/names", summary="更新股票名称映射")
def update_names(req: NamesUploadRequest) -> Dict[str, Any]:
    """批量写入 ``{code: name}`` 映射。"""
    try:
        if not req.names:
            raise ValueError("names 不能为空")
        result = services().data.update_names(req.names)
        return ok(result, message=f"已更新 {result.get('count', 0)} 条名称映射")
    except Exception as exc:
        raise handle_error(exc)


@router.get("/strategies", summary="列出可用策略")
def list_strategy_options() -> Dict[str, Any]:
    """返回策略列表（数据管理页可用于查看默认参数）。"""
    try:
        return ok(services().screen.strategies())
    except Exception as exc:
        raise handle_error(exc)


# ----------------------------------------------------------------------
# 股票基础信息（流通股本 / 流通市值）
# ----------------------------------------------------------------------
@router.get("/basics", summary="查询股票基础信息")
def list_basics(
    keyword: Optional[str] = Query(None, description="代码或名称模糊搜索"),
    limit: int = Query(500, ge=1, le=5000, description="最多返回条数"),
) -> Dict[str, Any]:
    """返回已配置的流通股本等基础信息（供 volume_surge 等策略使用）。"""
    try:
        return ok(services().data.basics_list(keyword=keyword or "", limit=limit))
    except Exception as exc:
        raise handle_error(exc)


@router.post("/basics/template", summary="生成基础信息模板")
def create_basics_template() -> Dict[str, Any]:
    """在缓存目录生成 stock_basic.csv 模板（含表头与示例行）。"""
    try:
        return ok(services().data.write_basics_template(), message="模板已生成")
    except Exception as exc:
        raise handle_error(exc)


@router.post("/basics", summary="更新股票基础信息")
def update_basics(req: BasicsUploadRequest) -> Dict[str, Any]:
    """批量写入流通股本。

    请求体：``{"basics": {"600000.SH": {"float_shares": "65.7亿"}}}``，
    也支持 ``{"600000.SH": 6570000000}`` 的简写；单位统一为「股」，
    兼容 ``"12.5亿"`` / ``"3500万"`` 文本。
    """
    try:
        if not req.basics:
            raise ValueError("basics 不能为空")
        result = services().data.update_basics(req.basics)
        return ok(result, message=f"已更新 {result.get('count', 0)} 条股票基础信息")
    except Exception as exc:
        raise handle_error(exc)
