"""设置 API：读写 config.yaml、策略默认参数、追踪与通知配置。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from ..models.schemas import StrategyParamsRequest
from .deps import handle_error, ok, services

router = APIRouter(prefix="/api/settings", tags=["settings"])

# 敏感字段：返回前端前做脱敏
_SENSITIVE_KEYS = {"smtp_password", "webhook_secret"}


def _mask(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """对敏感配置做脱敏处理。"""
    out: Dict[str, Any] = {}
    for key, value in (cfg or {}).items():
        if isinstance(value, dict):
            out[key] = _mask(value)
        elif key in _SENSITIVE_KEYS and value:
            out[key] = "******"
        else:
            out[key] = value
    return out


@router.get("", summary="获取全部配置")
def get_settings(masked: bool = Query(True, description="是否对敏感字段脱敏")) -> Dict[str, Any]:
    """返回当前配置（默认脱敏）。"""
    try:
        svc = services()
        cfg = svc.config.as_dict()
        return ok(
            {
                "config": _mask(cfg) if masked else cfg,
                "path": str(svc.config.path),
                "storage": svc.tracker.storage_stats(),
                "notify": svc.tracker.notify_summary(),
                "scheduler": _scheduler_status(),
            }
        )
    except Exception as exc:
        raise handle_error(exc)


@router.post("", summary="更新配置")
def update_settings(req: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """深度合并更新配置。

    请求体可以是两种形式之一：

    1. ``{"patch": {...}, "persist": true}``
    2. 直接给出局部配置字典，例如 ``{"backtest": {"max_positions": 5}}``
    """
    try:
        svc = services()
        if "patch" in req and isinstance(req["patch"], dict):
            patch = req["patch"]
            persist = bool(req.get("persist", True))
        else:
            patch = req
            persist = True
        if not patch:
            raise ValueError("配置补丁为空")

        # 脱敏占位符不允许写回
        _reject_masked(patch)

        svc.config.update(patch, persist=persist)
        svc.refresh_after_settings_change()
        _restart_scheduler()
        return ok(
            {
                "config": _mask(svc.config.as_dict()),
                "path": str(svc.config.path),
            },
            message="配置已更新" + ("并写入文件" if persist else "（未写盘）"),
        )
    except Exception as exc:
        raise handle_error(exc)


@router.get("/strategies", summary="策略默认参数")
def get_strategy_settings() -> Dict[str, Any]:
    """返回各策略的默认参数与参数元信息。"""
    try:
        svc = services()
        return ok(
            {
                "default": svc.config.get("strategy.default", "ma_cross"),
                "strategies": svc.screen.strategies(),
            }
        )
    except Exception as exc:
        raise handle_error(exc)


@router.post("/strategies", summary="保存策略默认参数")
def save_strategy_settings(req: StrategyParamsRequest) -> Dict[str, Any]:
    """把某策略的参数写入 ``config.yaml`` 的 ``strategy.<name>`` 段。"""
    try:
        svc = services()
        from ..strategies.registry import STRATEGIES

        if req.name not in STRATEGIES:
            raise ValueError(f"未知策略：{req.name}")
        svc.config.set(f"strategy.{req.name}", dict(req.params or {}), persist=True)
        return ok(svc.config.get(f"strategy.{req.name}"), message=f"{req.name} 默认参数已保存")
    except Exception as exc:
        raise handle_error(exc)


@router.post("/default-strategy", summary="设置默认策略")
def set_default_strategy(name: str = Body(..., embed=True)) -> Dict[str, Any]:
    """设置默认策略名。"""
    try:
        svc = services()
        from ..strategies.registry import STRATEGIES

        if name not in STRATEGIES:
            raise ValueError(f"未知策略：{name}")
        svc.config.set("strategy.default", name, persist=True)
        return ok({"default": name}, message=f"默认策略已设为 {name}")
    except Exception as exc:
        raise handle_error(exc)


@router.get("/scheduler", summary="定时任务状态")
def scheduler_status() -> Dict[str, Any]:
    """返回追踪定时任务状态。"""
    try:
        return ok(_scheduler_status())
    except Exception as exc:
        raise handle_error(exc)


@router.post("/scheduler/start", summary="启动定时任务")
def scheduler_start() -> Dict[str, Any]:
    """按配置启动追踪定时任务。"""
    try:
        from ..services.container import get_scheduler

        return ok(get_scheduler().start(), message="定时任务已启动")
    except Exception as exc:
        raise handle_error(exc)


@router.post("/scheduler/stop", summary="停止定时任务")
def scheduler_stop() -> Dict[str, Any]:
    """停止追踪定时任务。"""
    try:
        from ..services.container import get_scheduler

        return ok(get_scheduler().stop(), message="定时任务已停止")
    except Exception as exc:
        raise handle_error(exc)


@router.post("/scheduler/run", summary="立即执行一次追踪")
def scheduler_run(notify: bool = Query(True)) -> Dict[str, Any]:
    """立即执行一次追踪更新（等同定时任务的单次运行）。"""
    try:
        from ..services.container import get_scheduler

        return ok(get_scheduler().run_once(notify=notify), message="已执行一次追踪")
    except Exception as exc:
        raise handle_error(exc)


@router.post("/reset-db", summary="重置追踪数据库（危险）")
def reset_db(
    tables: Optional[str] = Query(None, description="逗号分隔的表名；为空则清空全部"),
    confirm: bool = Query(False, description="必须显式传 true 才会执行"),
) -> Dict[str, Any]:
    """清空追踪数据库中的表（不可恢复，需二次确认）。"""
    try:
        if not confirm:
            raise ValueError("危险操作：请显式传递 confirm=true 以确认清空。")
        svc = services()
        names = [t for t in (tables or "").split(",") if t] or None
        deleted = svc.tracker.storage.reset(names)
        return ok(deleted, message="数据已清空")
    except Exception as exc:
        raise handle_error(exc)


# ----------------------------------------------------------------------
def _scheduler_status() -> Dict[str, Any]:
    """读取定时任务状态（延迟导入避免循环依赖）。"""
    try:
        from ..services.container import get_scheduler

        return get_scheduler().status()
    except Exception as exc:  # pragma: no cover
        return {"error": str(exc)}


def _restart_scheduler() -> None:
    """配置变更后按新配置重启定时任务。"""
    try:
        from ..services.container import get_scheduler

        scheduler = get_scheduler()
        scheduler.stop()
        scheduler.start()
    except Exception as exc:  # pragma: no cover
        pass


def _reject_masked(obj: Any, path: str = "") -> None:
    """禁止把脱敏占位符写回配置。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            _reject_masked(value, f"{path}.{key}" if path else key)
    elif obj == "******":
        raise ValueError(
            f"字段 {path} 为脱敏占位符，不能写回。请传入真实值，或从请求中移除该字段。"
        )
