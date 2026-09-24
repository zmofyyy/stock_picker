"""FastAPI 依赖注入。

统一从服务容器中取出各个服务，便于测试时替换。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import HTTPException, Request

from ..services.container import Services, get_services


def services() -> Services:
    """返回服务集合（单例）。"""
    return get_services()


def handle_error(exc: Exception) -> HTTPException:
    """把业务异常转换为 HTTP 异常。"""
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, (ValueError, KeyError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=500, detail=f"内部错误：{exc}")


def ok(data: Any = None, message: str = "") -> Dict[str, Any]:
    """统一成功响应结构。"""
    return {"ok": True, "message": message, "data": data}
