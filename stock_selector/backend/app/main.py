"""FastAPI 应用入口。

启动方式::

    python -m uvicorn backend.app.main:app --reload --port 8000
    # 或
    python run.py
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import backtest as backtest_api
from .api import data as data_api
from .api import report as report_api
from .api import screen as screen_api
from .api import settings as settings_api
from .api import tracker as tracker_api
from . import __version__
from .core.logging import get_logger
from .services.container import build_services, get_scheduler

logger = get_logger("main")

# 前端构建产物目录：stock_selector/frontend/dist
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化服务与定时任务，关闭时清理资源。"""
    services = build_services(os.environ.get("STOCK_SELECTOR_CONFIG"))
    logger.info("stock_selector 后端启动完成（版本 %s）", __version__)

    scheduler = get_scheduler()
    try:
        if services.config.get("tracker.auto_update", False):
            scheduler.start()
    except Exception as exc:  # pragma: no cover
        logger.warning("定时任务启动失败：%s", exc)

    yield

    try:
        scheduler.stop()
    except Exception:  # pragma: no cover
        pass
    try:
        services.tracker.storage.close()
    except Exception:  # pragma: no cover
        pass
    logger.info("stock_selector 后端已关闭")


app = FastAPI(
    title="stock_selector · A股选股回测追踪系统",
    description=(
        "本地通达信数据驱动的 A 股选股、回测与持续追踪系统。\n\n"
        "- 数据来源：本地通达信 vipdoc（通过 pytdx 读取）\n"
        "- 提供选股、回测、追踪、报告与设置等 REST 接口\n"
        "- 追踪状态支持 WebSocket / SSE 实时推送"
    ),
    version=__version__,
    lifespan=lifespan,
)


# ----------------------------------------------------------------------
# 中间件
# ----------------------------------------------------------------------
def _cors_origins() -> list:
    """读取 CORS 白名单（只读配置，不触发服务容器构建）。"""
    try:
        from .core.config import get_config

        cfg = get_config(os.environ.get("STOCK_SELECTOR_CONFIG"))
        return list(cfg.get("web.cors_origins") or ["*"])
    except Exception:  # pragma: no cover
        return ["*"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理，保证接口始终返回 JSON。"""
    logger.exception("未处理异常：%s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "message": f"服务器内部错误：{exc}", "data": None},
    )


# ----------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------
app.include_router(data_api.router)
app.include_router(screen_api.router)
app.include_router(backtest_api.router)
app.include_router(tracker_api.router)
app.include_router(tracker_api.ws_router)
app.include_router(report_api.router)
app.include_router(settings_api.router)


@app.get("/api/health", tags=["system"], summary="健康检查")
def health() -> Dict[str, Any]:
    """健康检查接口。"""
    return {"ok": True, "version": __version__, "service": "stock_selector"}


@app.get("/api", tags=["system"], summary="接口索引")
def api_index() -> Dict[str, Any]:
    """列出主要接口，便于快速自检。"""
    return {
        "ok": True,
        "data": {
            "data": [
                "GET  /api/data/status",
                "POST /api/data/load",
                "GET  /api/data/stocks",
                "POST /api/data/quality",
                "GET  /api/data/cache",
            ],
            "screen": ["GET /api/screen", "POST /api/screen/run", "POST /api/screen/to-watchlist"],
            "backtest": ["GET /api/backtest/config", "POST /api/backtest/run", "GET /api/backtest/export"],
            "tracker": [
                "GET    /api/tracker/watchlist",
                "POST   /api/tracker/watchlist",
                "DELETE /api/tracker/watchlist/{code}",
                "POST   /api/tracker/update",
                "POST   /api/tracker/replay",
                "GET    /api/tracker/state/{code}",
                "GET    /api/tracker/history/{code}",
                "GET    /api/tracker/timeline/{code}",
                "GET    /api/tracker/report",
                "GET    /api/tracker/stream",
                "WS     /ws/tracker",
            ],
            "report": [
                "GET /api/report/daily",
                "GET /api/report/range",
                "GET /api/report/summary",
            ],
            "settings": ["GET /api/settings", "POST /api/settings"],
            "docs": ["/docs", "/redoc", "/openapi.json"],
        },
    }


# ----------------------------------------------------------------------
# 前端静态资源
# ----------------------------------------------------------------------
def _mount_frontend() -> bool:
    """挂载前端构建产物（如果存在）。"""
    index_file = FRONTEND_DIST / "index.html"
    if not index_file.exists():
        return False

    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/", include_in_schema=False)
    def serve_index() -> FileResponse:
        return FileResponse(str(index_file))

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str):
        """SPA 路由回退：非 API 路径一律返回 index.html。"""
        if full_path.startswith(("api/", "ws/")):
            return JSONResponse(status_code=404, content={"ok": False, "message": "接口不存在"})
        candidate = FRONTEND_DIST / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(index_file))

    return True


FRONTEND_MOUNTED = _mount_frontend()

if not FRONTEND_MOUNTED:

    @app.get("/", include_in_schema=False)
    def dev_hint() -> HTMLResponse:
        """前端未构建时的提示页。"""
        return HTMLResponse(
            """
            <!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
            <title>stock_selector</title>
            <style>body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
            max-width:760px;margin:64px auto;line-height:1.8;color:#222;padding:0 18px}
            code{background:#f2f2f2;padding:2px 6px;border-radius:4px}
            pre{background:#f7f7f7;padding:14px;border-radius:8px;overflow:auto}
            h1{color:#1677ff}</style></head><body>
            <h1>stock_selector 后端已启动</h1>
            <p>前端尚未构建。请任选一种方式访问界面：</p>
            <h3>方式一：开发模式（推荐）</h3>
            <pre>cd frontend
npm install
npm run dev</pre>
            <p>然后浏览器打开 <code>http://localhost:5173</code>，Vite 会把
            <code>/api</code> 代理到本服务。</p>
            <h3>方式二：构建后由后端托管</h3>
            <pre>cd frontend
npm install
npm run build</pre>
            <p>构建完成后刷新本页面即可看到 Web UI（若仍显示此页，请重启后端）。</p>
            <h3>接口文档</h3>
            <p><a href="/docs">/docs</a> · <a href="/redoc">/redoc</a> ·
            <a href="/api">/api</a>（接口索引）</p>
            </body></html>
            """
        )


def create_app() -> FastAPI:
    """返回 FastAPI 应用（便于测试与 ASGI 服务器引用）。"""
    return app


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    services = build_services()
    host = str(services.config.get("web.host", "0.0.0.0"))
    port = int(services.config.get("web.port", 8000))
    uvicorn.run("backend.app.main:app", host=host, port=port, reload=False)
