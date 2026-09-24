"""追踪 API（关注池、状态、历史、更新、回放、实时推送）。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from ..core.logging import get_logger
from ..models.schemas import (
    NoteRequest,
    ReplayRequest,
    SetStateRequest,
    TrackerUpdateRequest,
    WatchAddRequest,
    WatchBatchAddRequest,
    WatchUpdateRequest,
)
from .deps import handle_error, ok, services

logger = get_logger("api.tracker")

router = APIRouter(prefix="/api/tracker", tags=["tracker"])

# 独立的 WebSocket 路由（挂载到 /ws/tracker）
ws_router = APIRouter(tags=["tracker-ws"])


# ======================================================================
# 实时广播
# ======================================================================
class Broadcaster:
    """极简的进程内广播器，用于 WebSocket / SSE 推送追踪事件。"""

    def __init__(self) -> None:
        self._queues: List[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def register(self) -> asyncio.Queue:
        """注册一个订阅者。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._queues.append(queue)
        return queue

    async def unregister(self, queue: asyncio.Queue) -> None:
        """注销订阅者。"""
        async with self._lock:
            if queue in self._queues:
                self._queues.remove(queue)

    async def publish(self, event: Dict[str, Any]) -> None:
        """推送事件到所有订阅者（队列满时丢弃最旧事件）。"""
        payload = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **event}
        async with self._lock:
            targets = list(self._queues)
        for queue in targets:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except Exception:
                    pass

    @property
    def subscriber_count(self) -> int:
        """当前订阅者数量。"""
        return len(self._queues)


broadcaster = Broadcaster()


def _publish_from_sync(event: Dict[str, Any]) -> None:
    """在同步路由中安全地触发广播。"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(broadcaster.publish(event))
        else:  # pragma: no cover
            loop.run_until_complete(broadcaster.publish(event))
    except RuntimeError:  # pragma: no cover - 无事件循环
        pass


# ======================================================================
# 关注池
# ======================================================================
@router.get("/watchlist", summary="获取关注池")
def get_watchlist(
    group: Optional[str] = None,
    enabled_only: bool = False,
    keyword: Optional[str] = None,
    with_state: bool = Query(True, description="是否附带当前状态"),
) -> Dict[str, Any]:
    """返回关注池列表，可选附带当前追踪状态。"""
    try:
        svc = services()
        items = svc.tracker.list_watch(
            group=group, enabled_only=enabled_only, keyword=keyword
        )
        if with_state:
            states = {
                s["code"]: s
                for s in svc.tracker.list_states(group=group)
            }
            for item in items:
                item["state_info"] = states.get(item["code"])
        return ok(
            {
                "items": items,
                "count": len(items),
                "groups": svc.tracker.groups(),
            }
        )
    except Exception as exc:
        raise handle_error(exc)


@router.post("/watchlist", summary="添加关注项")
def add_watch(req: WatchAddRequest) -> Dict[str, Any]:
    """添加单只股票到关注池。"""
    try:
        payload = req.model_dump()
        item = services().tracker.add_watch(**payload)
        return ok(item, message=f"{item['code']} 已加入关注池")
    except Exception as exc:
        raise handle_error(exc)


@router.post("/watchlist/batch", summary="批量添加关注项")
def add_watch_batch(req: WatchBatchAddRequest) -> Dict[str, Any]:
    """批量添加关注项（选股结果一键导入也走此接口）。"""
    try:
        items = [i.model_dump() for i in req.items]
        stats = services().tracker.add_watch_batch(
            items, group=req.group, source=req.source, overwrite=req.overwrite
        )
        return ok(stats, message=f"成功添加 {stats.get('added', 0)} 只")
    except Exception as exc:
        raise handle_error(exc)


@router.patch("/watchlist/{code}", summary="更新关注项")
def update_watch(code: str, req: WatchUpdateRequest) -> Dict[str, Any]:
    """更新关注项（分组、标签、备注、成本价、目标价、止损价等）。"""
    try:
        fields = {k: v for k, v in req.model_dump().items() if v is not None}
        item = services().tracker.update_watch(code, **fields)
        if item is None:
            raise HTTPException(status_code=404, detail=f"{code} 不在关注池中")
        return ok(item, message="已更新")
    except Exception as exc:
        raise handle_error(exc)


@router.delete("/watchlist/{code}", summary="移除关注项")
def remove_watch(
    code: str,
    keep_state: bool = Query(False, description="是否保留追踪状态记录"),
) -> Dict[str, Any]:
    """从关注池移除股票。"""
    try:
        removed = services().tracker.remove_watch(code, keep_state=keep_state)
        if not removed:
            raise HTTPException(status_code=404, detail=f"{code} 不在关注池中")
        return ok({"code": code}, message=f"{code} 已移除")
    except Exception as exc:
        raise handle_error(exc)


@router.get("/groups", summary="关注池分组")
def list_groups() -> Dict[str, Any]:
    """返回分组及数量。"""
    try:
        return ok(services().tracker.groups())
    except Exception as exc:
        raise handle_error(exc)


@router.post("/watchlist/{code}/pause", summary="暂停/恢复追踪")
def pause_watch(code: str, paused: bool = Query(True)) -> Dict[str, Any]:
    """暂停或恢复某只股票的自动追踪。"""
    try:
        item = services().tracker.pause(code, paused=paused)
        if item is None:
            raise HTTPException(status_code=404, detail=f"{code} 不在关注池中")
        return ok(item, message="已暂停" if paused else "已恢复")
    except Exception as exc:
        raise handle_error(exc)


# ======================================================================
# 追踪更新
# ======================================================================
@router.post("/update", summary="执行追踪更新")
def update_tracking(req: TrackerUpdateRequest) -> Dict[str, Any]:
    """按日期（或最新数据日）更新关注池状态。

    - 不传 ``date``：使用每只股票的最新可用数据；
    - 传入 ``date``：执行「按日期回放」到该日，可重复调用，结果幂等。
    """
    try:
        svc = services()
        if not svc.data.reader.is_ready():
            raise RuntimeError("通达信目录不可用，无法更新追踪状态。请先配置 data.tdx_dir。")
        result = svc.tracker.update(
            codes=req.codes, date=req.date, group=req.group,
            strategy=req.strategy, params=req.params, notify=req.notify,
        )
        data = result.to_dict()
        _publish_from_sync({"type": "update", "data": data})
        return ok(data, message=f"处理 {result.processed} 只，状态变更 {result.changed} 次")
    except Exception as exc:
        raise handle_error(exc)


@router.post("/replay", summary="历史回放")
def replay_tracking(req: ReplayRequest) -> Dict[str, Any]:
    """在日期区间内逐日重建追踪状态，用于验证追踪逻辑。"""
    try:
        svc = services()
        if not svc.data.reader.is_ready():
            raise RuntimeError("通达信目录不可用，无法执行历史回放。")
        result = svc.tracker.replay(
            codes=req.codes, start=req.start, end=req.end, group=req.group,
            strategy=req.strategy, params=req.params, reset=req.reset, notify=req.notify,
        )
        data = result.to_dict()
        _publish_from_sync({"type": "replay", "data": data})
        return ok(data, message=f"回放完成，处理 {result.processed} 条日次记录")
    except Exception as exc:
        raise handle_error(exc)


# ======================================================================
# 状态查询
# ======================================================================
@router.get("/states", summary="关注池当前状态列表")
def list_states(
    state: Optional[str] = Query(None, description="按状态过滤，多个用逗号分隔"),
    group: Optional[str] = None,
    risk_level: Optional[str] = None,
    keyword: Optional[str] = None,
) -> Dict[str, Any]:
    """返回关注池中所有股票的当前状态。"""
    try:
        states = [s for s in (state or "").split(",") if s] or None
        rows = services().tracker.list_states(
            states=states, group=group, risk_level=risk_level, keyword=keyword
        )
        return ok({"items": rows, "count": len(rows)})
    except Exception as exc:
        raise handle_error(exc)


@router.get("/state/{code}", summary="单只股票当前状态")
def get_state(code: str) -> Dict[str, Any]:
    """获取某只股票的当前追踪状态。"""
    try:
        svc = services()
        state = svc.tracker.get_state(code)
        if state is None:
            return ok(
                {
                    "code": code,
                    "state": None,
                    "message": "该股票尚无追踪记录，请先加入关注池并执行更新。",
                    "watch": svc.tracker.get_watch(code),
                }
            )
        return ok({"state": state, "watch": svc.tracker.get_watch(code)})
    except Exception as exc:
        raise handle_error(exc)


@router.post("/state/{code}", summary="手动设置状态")
def set_state(code: str, req: SetStateRequest) -> Dict[str, Any]:
    """手动改写某只股票的状态（会记录到状态历史）。"""
    try:
        record = services().tracker.set_state(code, req.state, reason=req.reason, note=req.note)
        return ok(record, message=f"{code} 状态已设置为 {req.state}")
    except Exception as exc:
        raise handle_error(exc)


@router.get("/history/{code}", summary="状态变更历史")
def get_history(
    code: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
) -> Dict[str, Any]:
    """获取某只股票的状态变迁历史。"""
    try:
        rows = services().tracker.get_history(code=code, start=start, end=end, limit=limit)
        return ok({"code": code, "items": rows, "count": len(rows)})
    except Exception as exc:
        raise handle_error(exc)


@router.get("/timeline/{code}", summary="状态时间线")
def get_timeline(code: str, limit: int = Query(100, ge=1, le=2000)) -> Dict[str, Any]:
    """获取状态与信号合并的时间线。"""
    try:
        return ok(services().tracker.get_timeline(code, limit=limit))
    except Exception as exc:
        raise handle_error(exc)


@router.get("/signals", summary="信号记录")
def get_signals(
    code: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    strategy: Optional[str] = None,
    signal: Optional[int] = None,
    only_new: bool = False,
    limit: int = Query(200, ge=1, le=5000),
) -> Dict[str, Any]:
    """查询策略信号记录。"""
    try:
        rows = services().tracker.get_signals(
            code=code, start=start, end=end, strategy=strategy,
            signal=signal, only_new=only_new, limit=limit,
        )
        return ok({"items": rows, "count": len(rows)})
    except Exception as exc:
        raise handle_error(exc)


@router.get("/alerts", summary="提醒记录")
def get_alerts(
    code: Optional[str] = None,
    level: Optional[str] = None,
    category: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    unacked_only: bool = False,
    limit: int = Query(200, ge=1, le=5000),
) -> Dict[str, Any]:
    """查询提醒记录。"""
    try:
        rows = services().tracker.get_alerts(
            code=code, level=level, category=category, start=start, end=end,
            unacked_only=unacked_only, limit=limit,
        )
        return ok({"items": rows, "count": len(rows)})
    except Exception as exc:
        raise handle_error(exc)


@router.post("/alerts/{alert_id}/ack", summary="标记提醒已读")
def ack_alert(alert_id: int) -> Dict[str, Any]:
    """把提醒标记为已读。"""
    try:
        ok_flag = services().tracker.ack_alert(alert_id)
        if not ok_flag:
            raise HTTPException(status_code=404, detail="提醒不存在")
        return ok({"id": alert_id}, message="已标记为已读")
    except Exception as exc:
        raise handle_error(exc)


@router.get("/dashboard", summary="仪表盘数据")
def dashboard(date: Optional[str] = None) -> Dict[str, Any]:
    """返回仪表盘所需的聚合数据。"""
    try:
        svc = services()
        data = svc.tracker.dashboard(date=date)
        data["data_status"] = svc.data.status()
        last_bt = svc.backtest.list_results()
        data["last_backtest"] = last_bt[-1] if last_bt else None
        return ok(data)
    except Exception as exc:
        raise handle_error(exc)


@router.get("/report", summary="追踪报告（兼容入口）")
def tracker_report(
    kind: str = Query("daily", pattern="^(daily|range|summary)$"),
    date: Optional[str] = None,
    code: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    group: Optional[str] = None,
    fmt: str = Query("json", pattern="^(json|markdown|md|html|csv)$"),
) -> Any:
    """生成追踪报告。

    与 ``/api/report`` 等价，保留此入口是为了兼容需求中约定的
    ``GET /api/tracker/report``。
    """
    try:
        from .report import build_report, render_report

        return render_report(build_report(kind, date, code, start, end, group), fmt)
    except Exception as exc:
        raise handle_error(exc)


@router.get("/storage", summary="存储统计")
def storage_stats() -> Dict[str, Any]:
    """返回追踪数据库各表记录数。"""
    try:
        return ok(services().tracker.storage_stats())
    except Exception as exc:
        raise handle_error(exc)


@router.get("/notify", summary="通知配置概览")
def notify_status() -> Dict[str, Any]:
    """返回通知通道状态与测试入口信息。"""
    try:
        svc = services()
        data = svc.tracker.notify_summary()
        data["subscribers"] = broadcaster.subscriber_count
        return ok(data)
    except Exception as exc:
        raise handle_error(exc)


@router.post("/notify/test", summary="发送测试通知")
def notify_test() -> Dict[str, Any]:
    """向所有已启用通道发送一条测试提醒。"""
    try:
        return ok(services().tracker.test_notify(), message="测试通知已发送")
    except Exception as exc:
        raise handle_error(exc)


@router.post("/notes/{code}", summary="添加备注")
def add_note(code: str, req: NoteRequest) -> Dict[str, Any]:
    """为某只股票添加备注。"""
    try:
        return ok(services().tracker.add_note(code, req.content, author=req.author), message="已添加")
    except Exception as exc:
        raise handle_error(exc)


@router.get("/notes/{code}", summary="备注列表")
def list_notes(code: str, limit: int = 100) -> Dict[str, Any]:
    """列出某只股票的备注。"""
    try:
        return ok(services().tracker.list_notes(code, limit=limit))
    except Exception as exc:
        raise handle_error(exc)


# ======================================================================
# 实时推送
# ======================================================================
@router.get("/stream", summary="追踪事件 SSE 流")
async def stream(days: int = Query(0, ge=0, le=30)) -> StreamingResponse:
    """Server-Sent Events 推送追踪事件（状态变更、信号、提醒）。"""

    async def event_generator():
        queue = await broadcaster.register()
        try:
            yield "retry: 5000\n\n"
            yield f"data: {json.dumps({'type': 'connected'}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # 心跳，避免代理断开连接
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:  # pragma: no cover
            raise
        finally:
            await broadcaster.unregister(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@ws_router.websocket("/ws/tracker")
async def tracker_websocket(websocket: WebSocket) -> None:
    """WebSocket 推送追踪事件，并支持客户端主动触发更新。"""
    await websocket.accept()
    queue = await broadcaster.register()
    await websocket.send_json({"type": "connected", "message": "追踪实时通道已连接"})

    async def receiver() -> None:
        """接收客户端指令。"""
        while True:
            try:
                message = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            try:
                payload = json.loads(message)
            except Exception:
                payload = {"action": message}

            action = payload.get("action")
            if action == "ping":
                await websocket.send_json({"type": "pong"})
            elif action == "update":
                try:
                    svc = services()
                    result = await asyncio.to_thread(
                        svc.tracker.update,
                        payload.get("codes"),
                        payload.get("date"),
                        payload.get("group"),
                        None,
                        None,
                        bool(payload.get("notify", False)),
                    )
                    await broadcaster.publish({"type": "update", "data": result.to_dict()})
                except Exception as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
            else:
                await websocket.send_json({"type": "ack", "action": action})

    receiver_task = asyncio.create_task(receiver())
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover
        logger.debug("WebSocket 连接异常：%s", exc)
    finally:
        receiver_task.cancel()
        await broadcaster.unregister(queue)
