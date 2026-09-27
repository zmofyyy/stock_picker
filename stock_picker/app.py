"""stock_picker：基于本地通达信数据的「放量选股 + 计划 + 追踪」工具。

启动::

    python -m stock_picker.app --port 8778
    # 或
    python stock_picker/run.py
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import APP_DIR, DATA_DIR, RESOURCE_DIR, Config, is_frozen
from .names import NameIndex
from .service import (
    BOARD_ORDER,
    DEFAULT_EXCLUDED_BOARDS,
    MarketService,
    as_multi,
    ymd_to_iso,
)
from .store import Store
from .tdx_reader import TdxReader, normalize_code, split_code, verify_readers

STATIC_DIR = RESOURCE_DIR / "static"


def _asset_version() -> str:
    """前端资源的内容指纹（app.js / style.css / index.html 任一变化就变）。

    解决的是「代码改了、页面没变」这个反复踩到的坑：浏览器对没有
    ``Cache-Control`` 的响应会做启发式缓存，重新打包后旧标签页可能继续
    跑旧的 ``app.js``（曾表现为：日期控件下界仍是旧的 250 个交易日）。

    用法有两层：
    1. ``/`` 返回的 HTML 里把 ``__ASSET_VER__`` 替换成它，于是浏览器请求的是
       ``/static/app.js?v=<指纹>`` —— 前端一变 URL 就变，缓存条目直接失效；
    2. ``GET /api/version`` 暴露同一指纹，页面定时对比，发现自己过期就提示刷新。
    """
    h = hashlib.md5()
    for name in ("app.js", "style.css", "index.html"):
        path = STATIC_DIR / name
        try:
            h.update(name.encode("utf-8"))
            h.update(path.read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:10]


ASSET_VERSION = _asset_version()

# ----------------------------------------------------------------------
# 依赖装配
# ----------------------------------------------------------------------
config = Config()
names = NameIndex(
    config.get("tdx_dir"),
    cache_file=DATA_DIR / "stock_names.json",
    extra_csv=config.get("names_csv") or None,
)
reader = TdxReader(config.get("tdx_dir"), mode=config.get("reader_mode", "fast"))
service = MarketService(config, reader, names)
store = Store(DATA_DIR / "stock_picker.db")

# ----------------------------------------------------------------------
# 后台任务
# ----------------------------------------------------------------------
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def _job_update(job_id: str, patch: Dict[str, Any]) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(patch)
            JOBS[job_id]["updated_at"] = time.time()


def _run_job(job_id: str, fn, *args, **kwargs) -> None:
    _job_update(job_id, {"status": "running", "started_at": time.time()})
    try:
        result = fn(*args, progress=lambda p: _job_update(job_id, {"progress": p}), **kwargs)
        _job_update(job_id, {"status": "done", "result": result, "progress": None})
    except Exception as exc:  # pragma: no cover
        _job_update(
            job_id,
            {"status": "error", "error": f"{type(exc).__name__}: {exc}", "progress": None},
        )


def _new_job(name: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "name": name,
            "status": "pending",
            "created_at": time.time(),
            "progress": None,
            "result": None,
            "error": None,
        }
    return job_id


# ----------------------------------------------------------------------
# 启动预加载（把本地已存在的数据全部载入）
# ----------------------------------------------------------------------
STARTUP: Dict[str, Any] = {"job_id": None, "started_at": None, "auto": False}


def _bootstrap_cfg() -> Dict[str, Any]:
    return config.section("bootstrap")


def _run_preload_job(*, auto: bool = False) -> str:
    """起一个后台任务做预加载，返回 job_id。"""
    job_id = _new_job("preload")

    def _work(progress=None):
        return service.preload(
            progress=progress,
            refresh_if_stale=bool(_bootstrap_cfg().get("refresh_if_stale", True)),
        )

    threading.Thread(target=_run_job, args=(job_id, _work), daemon=True).start()
    STARTUP.update({"job_id": job_id, "started_at": time.time(), "auto": auto})
    return job_id


def _on_startup() -> None:
    """服务一启动就把本地已存在的数据全部载入（后台线程，不阻塞端口就绪）。"""
    if not bool(_bootstrap_cfg().get("preload", True)):
        print("启动预加载已关闭（config.bootstrap.preload = false）")
        return
    job_id = _run_preload_job(auto=True)
    print(f"已在后台预加载本地数据（job {job_id}）…")


def _on_shutdown() -> None:
    """退出前补一份快照并关掉数据库连接（WAL 已 checkpoint）。"""
    try:
        store._maybe_backup("shutdown", 300.0)
    except Exception:
        pass
    store.close()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期（FastAPI 新版写法，替代已弃用的 ``@app.on_event``）。

    - **startup**：只做一件轻活 —— 起一个后台线程跑 ``MarketService.preload()``。
      重活都在那条线程里，所以端口先就绪、页面先打开，进度通过
      ``GET /api/bootstrap`` 暴露给前端。
    - **shutdown**：补一份 SQLite 快照再关连接。放在 ``finally`` 里，
      保证正常退出（uvicorn 收到 Ctrl+C）时一定执行。

    注意 ``preload`` 由 ``config.bootstrap.preload`` 控制，关掉即回到
    「手动点刷新数据」的老行为。
    """
    _on_startup()
    try:
        yield
    finally:
        _on_shutdown()


app = FastAPI(
    title="stock_picker",
    version="1.0.0",
    docs_url="/api/docs",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_cache(request, call_next):
    """本地单机工具，绝不让浏览器留旧副本。

    - ``/api/*`` → ``no-store``：接口返回的是瞬时数据（交易日、行情、命中集），
      且 GET 接口在没有 ``Cache-Control`` 时会被浏览器启发式缓存。
    - 页面 HTML → ``no-store``：保证每次打开都带上最新的资源指纹。
    - ``/static/*`` → ``no-cache, must-revalidate``：允许 304 省流量，但必须回源
      校验；配合 URL 上的 ``?v=指纹``，内容一变就彻底绕开旧缓存条目。
    """
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    else:
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/bootstrap")
def bootstrap() -> Dict[str, Any]:
    """启动预加载的进度与结果；前端据此显示进度并在就绪后自动选股。"""
    cfg = _bootstrap_cfg()
    jid = STARTUP.get("job_id")
    job = None
    with JOBS_LOCK:
        if jid and jid in JOBS:
            job = dict(JOBS[jid])
    status_ = (job or {}).get("status")
    running = status_ in ("pending", "running")
    return {
        "ok": True,
        "job_id": jid,
        "job": job,
        "running": running,
        # 失败也算「就绪」—— 前端不该被一个挂掉的预加载卡住，能拿多少用多少
        "ready": not running,
        "failed": status_ == "error",
        "freshness": service.cache_freshness(),
        "settings": {
            "preload": bool(cfg.get("preload", True)),
            "refresh_if_stale": bool(cfg.get("refresh_if_stale", True)),
            "auto_screen": bool(cfg.get("auto_screen", True)),
        },
        "data_dir": str(APP_DIR),
    }


@app.post("/api/preload")
def preload_again() -> Dict[str, Any]:
    """手动重跑一次预加载（等价于启动时那一次）。"""
    return {"ok": True, "job_id": _run_preload_job(auto=False)}


# ----------------------------------------------------------------------
# 基础接口
# ----------------------------------------------------------------------
@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": "stock_picker", "version": "1.0.0"}


@app.get("/api/version")
def version_info() -> Dict[str, Any]:
    """前端资源指纹 —— 页面靠它发现自己跑的是旧版本（然后提示刷新）。"""
    return {"ok": True, "asset_version": ASSET_VERSION, "frozen": is_frozen()}


@app.get("/api/status")
def status() -> Dict[str, Any]:
    return {**service.status(), "storage": store.backup_info()}


@app.post("/api/names/reload")
def names_reload() -> Dict[str, Any]:
    meta = names.load(force=True)
    return {"ok": True, "names": {"size": names.size, **meta}}


@app.post("/api/shares/reload")
def shares_reload() -> Dict[str, Any]:
    """重新解析本地 gbbq（股本变迁），刷新流通股本缓存。"""
    try:
        meta = service.shares.load(force=True)
    except Exception as exc:
        raise HTTPException(500, f"解析股本变迁失败：{exc}") from exc
    return {"ok": True, "float_shares": service.shares.info(), "meta": meta}


@app.get("/api/config")
def get_config() -> Dict[str, Any]:
    return {"ok": True, "config": config.data, "path": str(config.path)}


@app.patch("/api/config")
def patch_config(patch: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    global reader, service
    data = config.update(patch)
    new_dir = data.get("tdx_dir")
    new_mode = data.get("reader_mode", "fast")
    if str(new_dir) != str(reader.tdx_dir) or new_mode != reader.mode:
        reader = TdxReader(new_dir, mode=new_mode)
        service = MarketService(config, reader, names)
        names.tdx_dir = str(new_dir)
        names.load(force=False)
    return {"ok": True, "config": data}


@app.post("/api/verify")
def verify(sample: int = Query(24, ge=1, le=200)) -> Dict[str, Any]:
    """比对 pytdx 与向量化解析器结果是否逐字段一致。"""
    result = verify_readers(reader, sample=sample)
    return {"ok": bool(result.get("ok")), "pytdx_available": True, **result}


# ----------------------------------------------------------------------
# 数据刷新
# ----------------------------------------------------------------------
@app.post("/api/data/refresh")
def data_refresh(rebuild_names: bool = Query(True)) -> Dict[str, Any]:
    if rebuild_names:
        names.load(force=False)

    def _work(progress=None):
        return service.refresh(progress=progress)

    job_id = _new_job("refresh")
    threading.Thread(target=_run_job, args=(job_id, _work), daemon=True).start()
    return {"ok": True, "job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> Dict[str, Any]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(404, f"任务不存在：{job_id}")
        return {"ok": True, "job": dict(job)}


# ----------------------------------------------------------------------
# 选股 / 连板梯队 / K 线
# ----------------------------------------------------------------------
@app.post("/api/screen")
def screen(params: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    try:
        return service.screen(params or {})
    except Exception as exc:
        raise HTTPException(500, f"选股失败：{exc}") from exc


@app.get("/api/streaks")
def streaks(
    date: Optional[str] = Query(None, description="交易日 YYYY-MM-DD；留空 = 最新交易日"),
    min_streak: int = Query(1, ge=1, le=30, description="梯队与明细只保留 N 板及以上"),
    st_limit: bool = Query(False, description="主板 ST 按 5% 涨跌幅判定（默认关，见 service.streaks 文档）"),
    markets: Optional[str] = Query(None, description="逗号分隔：sh,sz,bj；留空 = 全部"),
    boards: Optional[str] = Query(None, description="逗号分隔：主板,创业板,科创板,北交所；留空 = 全部"),
    max_rows: int = Query(500, ge=1, le=2000),
) -> Dict[str, Any]:
    """连板梯队：指定交易日的涨停个股按连板高度分层（含行业 / 概念归属）。

    连板数 = 从该交易日往回连续涨停的天数（首板 = 1）。判定口径见
    ``limits.py``：涨停价按「分」整数四舍五入，各板块涨跌幅 10/20/30%，上市首日剔除。

    ``st_limit=True`` 时主板 ST 另按 5%；**默认关闭**，因为本机行情实测 ST 股
    （含主板）的日内带宽也是 10%，套 5% 会显著多算涨停。
    """
    params: Dict[str, Any] = {
        "date": date,
        "min_streak": min_streak,
        "st_limit": st_limit,
        "max_rows": max_rows,
    }
    if markets:
        params["markets"] = [s.strip() for s in markets.split(",") if s.strip()]
    if boards:
        params["boards"] = [s.strip() for s in boards.split(",") if s.strip()]
    try:
        return service.streaks(params)
    except Exception as exc:
        raise HTTPException(500, f"连板梯队计算失败：{exc}") from exc


@app.get("/api/kline/{code}")
def kline(
    code: str,
    bars: int = Query(160, ge=30, le=1000),
    ma_window: int = Query(20, ge=2, le=250),
) -> Dict[str, Any]:
    try:
        return service.kline(code, bars=bars, ma_window=ma_window)
    except Exception as exc:
        raise HTTPException(500, f"读取 K 线失败：{exc}") from exc


@app.get("/api/board_catalog")
def board_catalog(rebuild: bool = Query(False)) -> Dict[str, Any]:
    """板块目录（行业 / 地区 / 概念 / 风格）与每类的板块数。

    数据源是通达信 ``T0002/hq_cache/tdxzs.cfg``（板块指数表，604 条）——
    比「概念」多出行业与地区两类，用于板块看盘页的类别切换。
    """
    if rebuild:
        service.boards.load(force=True)
    return {"ok": True, **service.boards.info()}


@app.get("/api/board_panel")
def board_panel(
    date: Optional[str] = Query(None, description="交易日 YYYY-MM-DD；留空 = 最新交易日"),
    category: Optional[str] = Query(
        None, description="板块类别：行业 / 地区 / 概念 / 风格；留空 = 全部"
    ),
    sort: str = Query("pct", description="pct/close/amount_yi/vol_ratio/turnover/n_up/n_limit_up/avg_pct/name/code/order"),
    order: str = Query("desc", description="asc / desc"),
    keyword: str = Query("", max_length=40, description="按板块名或指数代码搜索"),
    limit: int = Query(0, ge=0, le=2000, description="0 = 不截断（604 个板块全返回）"),
    with_members_only: bool = Query(False, description="只保留有成分股数据的板块"),
) -> Dict[str, Any]:
    """板块看盘：每个板块在锚点日的行情与成分统计。

    指标分两部分：**板块指数自身行情**（涨幅 / 成交额 / 量比，读
    ``vipdoc/sh/lday/sh880xxx.day``）与**成分股聚合**（涨跌家数 / 涨停跌停数 /
    平均涨幅 / 换手率）。地区板块本地没有成分文件，只有前半部分。

    ``summary`` 在 ``limit`` 截断**之前**统计，用来看「604 个板块里几个在涨」。
    """
    params: Dict[str, Any] = {
        "date": date,
        "category": category,
        "sort": sort,
        "order": order,
        "keyword": keyword,
        "limit": max(0, int(limit)),
        "with_members_only": bool(with_members_only),
    }
    try:
        return service.board_panel(params)
    except Exception as exc:
        raise HTTPException(500, f"板块看盘计算失败：{exc}") from exc


@app.get("/api/board_members")
def board_members(
    board: str = Query(..., description="板块指数代码，如 880301（煤炭）"),
    date: Optional[str] = Query(None, description="交易日 YYYY-MM-DD；留空 = 最新交易日"),
    sort: str = Query("pct", description="pct/close/amount_yi/vol_ratio/turnover/float_mcap_yi/industry_l2/concept_n/code/name"),
    order: str = Query("desc", description="asc / desc"),
    limit: int = Query(0, ge=0, le=3000, description="0 = 不截断"),
    ma_window: int = Query(20, ge=2, le=250, description="量比基准的前 N 日均量窗口"),
) -> Dict[str, Any]:
    """板块成分股明细（点选板块后右侧列表）。

    只列**锚点日有行情**的成分股（停牌股不进列表），``n_total`` 是静态成分数、
    ``n_suspended`` 是两者之差。
    """
    params: Dict[str, Any] = {
        "board": board,
        "date": date,
        "sort": sort,
        "order": order,
        "limit": max(0, int(limit)),
        "ma_window": int(ma_window),
    }
    try:
        return service.board_members(params)
    except Exception as exc:
        raise HTTPException(500, f"读取板块成分股失败：{exc}") from exc


@app.get("/api/board_kline/{code}")
def board_kline(
    code: str,
    bars: int = Query(160, ge=30, le=1000),
) -> Dict[str, Any]:
    """板块指数 K 线（含均线、均量、放量标记与 MACD）。

    ``code`` 是板块指数代码（``880xxx``），读 ``vipdoc/sh/lday``，与个股 K 线
    返回**同一套结构**（前端一套渲染）。
    """
    try:
        return service.index_kline(code, bars=bars)
    except Exception as exc:
        raise HTTPException(500, f"读取板块指数 K 线失败：{exc}") from exc


@app.get("/api/trade_dates")
def trade_dates(n: int = Query(0, ge=0, le=20000)) -> Dict[str, Any]:
    """本地数据覆盖的交易日（升序 ISO）。

    ``n=0``（默认）返回全部 —— 前端「指定日期」控件用返回的第一个值当可选下界，
    若只给最近 250 天，日历会把更早的年份整个灰掉（曾经就只能选到最近一年）。
    """
    dates = service.trade_dates(n)
    return {
        "ok": True,
        "n": n,
        "count": len(dates),
        "dates": dates,
        "first": dates[0] if dates else None,
        "latest": dates[-1] if dates else None,
    }


@app.get("/api/intersect")
def intersect(
    date: Optional[str] = Query(None, description="交易日 YYYY-MM-DD；留空 = 最新交易日"),
    industries: Optional[str] = Query(
        None, description="逗号分隔的二级行业名（多选为并集）"
    ),
    concepts: Optional[str] = Query(None, description="逗号分隔的概念名"),
    concept_mode: str = Query(
        "any", description="多个概念：any 命中任一 / all 同时命中全部"
    ),
    boards: Optional[str] = Query(
        None, description="逗号分隔：主板,创业板,科创板,北交所；留空 = 全部"
    ),
    markets: Optional[str] = Query(None, description="逗号分隔：sh,sz,bj；留空 = 全部"),
    exclude_st: bool = Query(True, description="剔除 ST / 退市"),
    min_amount: float = Query(0.0, ge=0, description="最小成交额（亿元）"),
    max_float_mcap: float = Query(0.0, ge=0, description="流通市值上限（亿元）；0 = 不限"),
    max_rows: int = Query(1000, ge=1, le=3000),
    sort: str = Query("amount", description="amount/mcap/pct/close/concept_n/industry/code/name"),
) -> Dict[str, Any]:
    """二级行业板块 ∩ 概念板块 = 交集个股。

    集合运算口径（都在「锚点日有行情且通过过滤」的候选集内）::

        A = 二级行业命中 industries 的标的（多选为并集；为空 = 全集）
        B = 概念命中 concepts 的标的（concept_mode = any/all；为空 = 全集）
        结果 = A ∩ B

    行业侧不提供 ``all``：一只标的的二级行业是单值，选两个行业求交恒为空。

    ``min_amount`` 走**亿元**（与界面单位一致），服务层内部换算成元。

    打错行业名 / 概念名会明确报错，而不是静默返回空结果。

    注意 ``is not None`` 而不是真值判断：``?boards=``（空串）表示「用户一个板块
    都没勾」，必须原样传成空列表让服务层报错；用 ``if boards:`` 会把它当成
    「没传这个参数」而回退到配置默认值 —— 界面上条件收紧了、结果反而变多。
    """
    params: Dict[str, Any] = {
        "date": date,
        "concept_mode": concept_mode,
        "exclude_st": exclude_st,
        "min_amount": float(min_amount) * 1e8,
        "max_float_mcap": max_float_mcap,
        "max_rows": max_rows,
        "sort": sort,
    }
    if industries is not None:
        params["industries"] = as_multi(industries)
    if concepts is not None:
        params["concepts"] = as_multi(concepts)
    if boards is not None:
        params["boards"] = as_multi(boards)
    if markets is not None:
        params["markets"] = [s.lower() for s in as_multi(markets)]
    try:
        return service.intersect(params)
    except Exception as exc:
        raise HTTPException(500, f"板块交集计算失败：{exc}") from exc


@app.get("/api/industries")
def industries(rebuild: bool = Query(False)) -> Dict[str, Any]:
    """二级行业分类（通达信研究行业）：一级→二级树 + 全部二级名。"""
    return service.industries_info(force=rebuild)


@app.get("/api/concepts")
def concepts(rebuild: bool = Query(False)) -> Dict[str, Any]:
    """概念分类（通达信概念板块，本地 infoharbor_block.dat）：目录 + 全部概念名。"""
    return service.concepts_info(force=rebuild)


@app.post("/api/concepts/reload")
def concepts_reload() -> Dict[str, Any]:
    """重新解析本地板块文件，刷新概念索引（并顺带算一次覆盖率）。"""
    info = service.concepts_info(force=True)
    if not info.get("ok"):
        raise HTTPException(500, f"解析概念板块失败：{info.get('reason')}")
    return info


@app.get("/api/boards")
def boards() -> Dict[str, Any]:
    return {
        "ok": True,
        "markets": [
            {"value": "sh", "label": "上海"},
            {"value": "sz", "label": "深圳"},
            {"value": "bj", "label": "北京"},
        ],
        "boards": [
            {"value": b, "label": b, "default_on": b not in DEFAULT_EXCLUDED_BOARDS}
            for b in BOARD_ORDER
        ],
        "default_excluded": list(DEFAULT_EXCLUDED_BOARDS),
    }


@app.get("/api/stocks")
def stocks(
    keyword: str = Query("", max_length=40),
    limit: int = Query(30, ge=1, le=500),
) -> Dict[str, Any]:
    """按代码或名称检索本地标的（用于搜索框）。"""
    df = service.bars()
    if len(df) == 0:
        return {"ok": True, "items": []}
    codes = sorted(df["code"].unique().tolist())
    kw = keyword.strip().upper()
    items: List[Dict[str, Any]] = []
    for code in codes:
        name = names.get(code) or ""
        if kw and kw not in code.upper() and kw not in name.upper():
            continue
        items.append({"code": code, "name": name, "market": split_code(code)[1].upper()})
        if len(items) >= limit:
            break
    return {"ok": True, "items": items}


# ----------------------------------------------------------------------
# 计划
# ----------------------------------------------------------------------
@app.get("/api/plans")
def list_plans(status: Optional[str] = None) -> Dict[str, Any]:
    rows = store.list_plans(status)
    enriched = service.valuate(rows)
    return {"ok": True, "items": enriched, "count": len(enriched)}


@app.post("/api/plans")
def create_plan(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        if payload.get("code"):
            payload["code"] = normalize_code(str(payload["code"]))
        if not payload.get("name") and payload.get("code"):
            payload["name"] = names.get(payload["code"])
        row = store.upsert_plan(payload)
        return {"ok": True, "item": row}
    except Exception as exc:
        raise HTTPException(400, f"保存计划失败：{exc}") from exc


@app.patch("/api/plans/{plan_id}")
def update_plan(plan_id: int, patch: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    row = store.update_plan(plan_id, patch)
    if row is None:
        raise HTTPException(404, "计划不存在")
    return {"ok": True, "item": row}


@app.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: int) -> Dict[str, Any]:
    if not store.delete_plan(plan_id):
        raise HTTPException(404, "计划不存在")
    return {"ok": True}


@app.post("/api/plans/{plan_id}/to_watch")
def plan_to_watch(plan_id: int) -> Dict[str, Any]:
    plan = store.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, "计划不存在")
    row = store.upsert_watch(
        {
            "plan_id": plan["id"],
            "code": plan["code"],
            "name": plan.get("name") or names.get(plan["code"]),
            "signal_date": plan["signal_date"],
            "base_close": plan.get("base_close"),
            "buy_price": plan.get("buy_price"),
            "sell_price": plan.get("sell_price"),
            "qty": plan.get("qty"),
            "status": "关注中",
            "note": plan.get("note") or "",
        }
    )
    return {"ok": True, "item": row}


# ----------------------------------------------------------------------
# 追踪
# ----------------------------------------------------------------------
@app.get("/api/watchlist")
def list_watch(status: Optional[str] = None) -> Dict[str, Any]:
    rows = store.list_watch(status)
    enriched = service.valuate(rows)
    return {"ok": True, "items": enriched, "count": len(enriched)}


@app.post("/api/watchlist")
def create_watch(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        if payload.get("code"):
            payload["code"] = normalize_code(str(payload["code"]))
        if not payload.get("name") and payload.get("code"):
            payload["name"] = names.get(payload["code"])
        row = store.upsert_watch(payload)
        return {"ok": True, "item": store.get_watch(row["id"])}
    except Exception as exc:
        raise HTTPException(400, f"加入追踪失败：{exc}") from exc


@app.patch("/api/watchlist/{watch_id}")
def update_watch(watch_id: int, patch: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    row = store.update_watch(watch_id, patch)
    if row is None:
        raise HTTPException(404, "追踪条目不存在")
    return {"ok": True, "item": row}


@app.delete("/api/watchlist/{watch_id}")
def delete_watch(watch_id: int) -> Dict[str, Any]:
    if not store.delete_watch(watch_id):
        raise HTTPException(404, "追踪条目不存在")
    return {"ok": True}


@app.get("/api/stats")
def stats() -> Dict[str, Any]:
    return {"ok": True, **store.stats()}


# ----------------------------------------------------------------------
# 持久化：备份 / 导出
# ----------------------------------------------------------------------
@app.get("/api/storage")
def storage() -> Dict[str, Any]:
    """当前数据库位置、大小与备份清单。"""
    return {"ok": True, **store.backup_info()}


@app.post("/api/storage/backup")
def make_backup() -> Dict[str, Any]:
    """立刻生成一份一致性快照。"""
    try:
        item = store.create_backup("manual")
    except Exception as exc:
        raise HTTPException(500, f"备份失败：{exc}") from exc
    if item is None:
        raise HTTPException(400, "数据库文件不存在，无法备份")
    return {"ok": True, "item": item, **store.backup_info()}


@app.get("/api/storage/export")
def export_json() -> JSONResponse:
    """把计划与追踪导出成 JSON 文件（人工存档/迁移用）。"""
    payload = store.export()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"stock_picker-{stamp}.json"
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/storage/download/{name}")
def download_backup(name: str) -> FileResponse:
    """下载某一份备份（仅允许 backups 目录内的 .db 文件）。"""
    safe = Path(name).name
    if not safe.endswith(".db"):
        raise HTTPException(400, "只允许下载 .db 备份文件")
    target = (store.backup_dir / safe).resolve()
    try:
        target.relative_to(store.backup_dir.resolve())
    except ValueError:
        raise HTTPException(400, "非法的文件路径") from None
    if not target.is_file():
        raise HTTPException(404, f"备份不存在：{safe}")
    return FileResponse(str(target), filename=safe, media_type="application/octet-stream")


# ----------------------------------------------------------------------
# 静态资源
# ----------------------------------------------------------------------
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    idx = STATIC_DIR / "index.html"
    if not idx.is_file():
        return JSONResponse({"ok": False, "reason": "static/index.html 缺失"}, status_code=500)
    # 注入资源指纹：/static/app.js?v=xxx —— 前端一变 URL 就变，旧标签页不可能续用旧脚本
    html = idx.read_text(encoding="utf-8").replace("__ASSET_VER__", ASSET_VERSION)
    return HTMLResponse(html)


@app.get("/favicon.ico")
def favicon():
    p = STATIC_DIR / "favicon.svg"
    return FileResponse(str(p)) if p.is_file() else JSONResponse({}, status_code=204)


def _probe_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def _local_url(host: str, port: int) -> str:
    return f"http://{_probe_host(host)}:{port}/"


def _port_in_use(host: str, port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((_probe_host(host), port)) == 0


def _is_our_service(host: str, port: int) -> bool:
    """探一下 /api/status，判断占用端口的是不是本程序。"""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"{_local_url(host, port)}api/status", timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return isinstance(data, dict) and "latest_date" in data
    except Exception:
        return False


def _open_browser(url: str) -> None:
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass


def _open_browser_when_ready(host: str, port: int) -> None:
    """后台等端口起来再开页面 —— 打包后双击 exe 时没有控制台可点。"""
    import socket

    url = _local_url(host, port)
    ph = _probe_host(host)

    def _worker() -> None:
        deadline = time.time() + 90
        while time.time() < deadline:
            with socket.socket() as s:
                s.settimeout(0.3)
                if s.connect_ex((ph, port)) == 0:
                    break
            time.sleep(0.25)
        _open_browser(url)

    threading.Thread(target=_worker, name="open-browser", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser("stock_picker")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--tdx-dir", default=None)
    parser.add_argument("--reader", default=None, choices=["fast", "pytdx", "auto"])
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--browser", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    patch: Dict[str, Any] = {}
    if args.host:
        patch["host"] = args.host
    if args.port:
        patch["port"] = args.port
    if args.tdx_dir:
        patch["tdx_dir"] = args.tdx_dir
    if args.reader:
        patch["reader_mode"] = args.reader
    if patch:
        config.update(patch)

    global reader, service
    reader = TdxReader(config.get("tdx_dir"), mode=config.get("reader_mode", "fast"))
    service = MarketService(config, reader, names)
    names.tdx_dir = str(config.get("tdx_dir") or "")
    names.load(force=False)

    host = str(config.get("host", "127.0.0.1"))
    port = int(config.get("port", 8778))
    url = _local_url(host, port)
    # 打包后没有控制台提示，默认自动开页面；源码运行仍以 CLI 为主
    open_browser = args.browser or (is_frozen() and not args.no_browser)

    if args.reload and is_frozen():
        # uvicorn 的热重载要靠导入字符串重新执行进程，冻结后做不到，忽略即可
        print("打包版本不支持 --reload，已忽略。", file=sys.stderr)
        args.reload = False

    if not args.reload and _port_in_use(host, port):
        if _is_our_service(host, port):
            print(f"stock_picker 已在 {url} 运行，直接打开页面。")
            if open_browser:
                _open_browser(url)
            return
        print(
            f"端口 {port} 被其它程序占用。\n"
            f"请换一个端口重启，例如：stock_picker.exe --port 8779",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print(f"数据目录：{APP_DIR}")
    print(f"通达信目录：{config.get('tdx_dir')}")
    print(f"服务地址：{url}")

    if open_browser:
        _open_browser_when_ready(host, port)

    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
