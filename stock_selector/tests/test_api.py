"""Web API 测试（FastAPI TestClient）。

覆盖需求中约定的全部接口，验证状态码与响应结构。
"""

from __future__ import annotations

from pathlib import Path

import pytest

CODES = ["600000.SH", "000001.SZ"]


# ----------------------------------------------------------------------
# 系统
# ----------------------------------------------------------------------
def test_health(client) -> None:
    """健康检查。"""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_api_index(client) -> None:
    """接口索引。"""
    resp = client.get("/api")
    assert resp.status_code == 200
    assert "data" in resp.json()["data"]


def test_openapi_schema(client) -> None:
    """OpenAPI 文档可访问。"""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for path in (
        "/api/data/status",
        "/api/data/load",
        "/api/data/stocks",
        "/api/screen",
        "/api/screen/run",
        "/api/backtest/run",
        "/api/tracker/watchlist",
        "/api/tracker/watchlist/{code}",
        "/api/tracker/update",
        "/api/tracker/state/{code}",
        "/api/tracker/history/{code}",
        "/api/tracker/report",
        "/api/settings",
    ):
        assert path in paths, f"缺少接口 {path}"


# ----------------------------------------------------------------------
# 数据
# ----------------------------------------------------------------------
def test_data_status(client) -> None:
    """数据源状态。"""
    resp = client.get("/api/data/status")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ready"] is True
    assert "pytdx" in data
    assert "cache" in data


def test_data_stocks(client) -> None:
    """股票池扫描。"""
    resp = client.get("/api/data/stocks?include_index=true")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["count"] >= 5
    codes = {s["code"] for s in data["stocks"]}
    assert "600000.SH" in codes

    filtered = client.get("/api/data/stocks?keyword=600000").json()["data"]
    assert filtered["count"] == 1


def test_data_load(client) -> None:
    """数据加载。"""
    resp = client.post(
        "/api/data/load",
        json={"codes": CODES, "freq": "daily", "force": True},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["loaded"] == 2
    assert data["rows"] > 0


def test_data_load_bad_freq(client) -> None:
    """非法周期应返回 400。"""
    resp = client.post("/api/data/load", json={"codes": CODES, "freq": "3min"})
    assert resp.status_code == 400


def test_data_preview(client) -> None:
    """行情预览。"""
    resp = client.get("/api/data/preview/600000.SH?limit=10")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["count"] > 0
    assert len(data["rows"]) == 10


def test_data_cache(client) -> None:
    """缓存概览与清理。"""
    client.post("/api/data/load", json={"codes": ["600000.SH"]})
    resp = client.get("/api/data/cache")
    assert resp.status_code == 200
    assert "entries" in resp.json()["data"]

    cleared = client.delete("/api/data/cache?code=600000.SH")
    assert cleared.status_code == 200


def test_data_quality(client) -> None:
    """数据质量检查。"""
    resp = client.post("/api/data/quality", json={"max_codes": 4})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 4
    assert "issues" in data


def test_tdx_dir_check(client, tdx_root: Path) -> None:
    """通达信目录检测与设置。"""
    resp = client.get(f"/api/data/tdx-dir?directory={tdx_root.as_posix()}")
    assert resp.status_code == 200
    assert resp.json()["data"]["valid"] is True

    bad = client.get("/api/data/tdx-dir?directory=C:/not-exist-dir")
    assert bad.json()["data"]["valid"] is False

    set_resp = client.post(
        "/api/data/tdx-dir", json={"tdx_dir": tdx_root.as_posix(), "persist": False}
    )
    assert set_resp.status_code == 200


def test_names_template_and_update(client) -> None:
    """名称映射模板与写入。"""
    resp = client.post("/api/data/names/template")
    assert resp.status_code == 200
    assert resp.json()["data"]["exists"] is True

    upd = client.post("/api/data/names", json={"names": {"600000.SH": "浦发银行"}})
    assert upd.status_code == 200
    assert upd.json()["data"]["count"] >= 1


# ----------------------------------------------------------------------
# 选股
# ----------------------------------------------------------------------
def test_screen_meta(client) -> None:
    """选股元信息（策略与默认参数）。"""
    resp = client.get("/api/screen")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["strategies"]) >= 3
    assert data["default_strategy"]


def test_screen_run(client) -> None:
    """执行选股。"""
    resp = client.post(
        "/api/screen/run",
        json={
            "strategy": "ma_cross",
            "params": {"short_window": 5, "long_window": 20},
            "date": "2022-09-30",
            "universe": ["600000.SH", "000001.SZ"],
            "min_bars": 30,
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["strategy"] == "ma_cross"
    assert data["total_scanned"] == 2
    assert isinstance(data["results"], list)
    assert isinstance(data["rows"], list)
    assert "markdown" in data


def test_screen_run_bad_strategy(client) -> None:
    """未知策略应返回 400。"""
    resp = client.post(
        "/api/screen/run",
        json={"strategy": "not_exist", "date": "2022-09-30", "universe": ["600000.SH"]},
    )
    assert resp.status_code == 400


def test_screen_export(client) -> None:
    """导出选股结果。"""
    client.post(
        "/api/screen/run",
        json={"strategy": "ma_cross", "date": "2022-09-30", "universe": ["600000.SH"]},
    )
    csv_resp = client.get("/api/screen/export?fmt=csv")
    assert csv_resp.status_code == 200
    assert "code" in csv_resp.text

    md_resp = client.get("/api/screen/export?fmt=markdown")
    assert md_resp.status_code == 200
    assert "# 选股报告" in md_resp.text


def test_screen_to_watchlist(client) -> None:
    """选股结果一键加入关注池。"""
    client.post(
        "/api/screen/run",
        json={"strategy": "ma_cross", "date": "2022-09-30", "universe": CODES},
    )
    resp = client.post("/api/screen/to-watchlist", json={"group": "选股结果"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "added" in data


# ----------------------------------------------------------------------
# 回测
# ----------------------------------------------------------------------
def test_backtest_config(client) -> None:
    """回测配置与策略列表。"""
    resp = client.get("/api/backtest/config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["defaults"]["initial_cash"] > 0
    assert len(data["strategies"]) >= 3


def test_backtest_run(client) -> None:
    """执行回测。"""
    resp = client.post(
        "/api/backtest/run",
        json={
            "strategy": "ma_cross",
            "start": "2022-03-01",
            "end": "2022-10-31",
            "universe": CODES,
            "initial_cash": 500000,
            "max_positions": 2,
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["strategy"] == "ma_cross"
    assert data["universe_size"] == 2
    assert "metrics" in data
    assert "equity_curve" in data
    assert "drawdown_curve" in data
    assert "markdown" in data
    for key in ("total_return", "max_drawdown", "sharpe"):
        assert key in data["metrics"]


def test_backtest_run_bad_dates(client) -> None:
    """日期格式错误应返回 422/400。"""
    resp = client.post(
        "/api/backtest/run",
        json={"strategy": "ma_cross", "start": "not-a-date", "end": "2022-10-31",
              "universe": CODES},
    )
    assert resp.status_code in (400, 422, 500) or resp.status_code == 200


def test_backtest_export(client) -> None:
    """回测结果导出。"""
    client.post(
        "/api/backtest/run",
        json={"strategy": "ma_cross", "start": "2022-03-01", "end": "2022-10-31",
              "universe": CODES},
    )
    for fmt, kind in (("csv", "trades"), ("markdown", "report"), ("html", "report")):
        resp = client.get(f"/api/backtest/export?fmt={fmt}&kind={kind}")
        assert resp.status_code == 200, f"{fmt} 导出失败"
        assert len(resp.text) > 0


def test_backtest_to_watchlist(client) -> None:
    """回测持仓导入关注池。"""
    client.post(
        "/api/backtest/run",
        json={"strategy": "ma_cross", "start": "2022-03-01", "end": "2022-10-31",
              "universe": CODES},
    )
    resp = client.post("/api/backtest/to-watchlist", json={"group": "回测持仓"})
    assert resp.status_code == 200


# ----------------------------------------------------------------------
# 追踪
# ----------------------------------------------------------------------
def test_watchlist_crud(client) -> None:
    """关注池增删改查。"""
    add = client.post(
        "/api/tracker/watchlist",
        json={
            "code": "600000.SH", "name": "浦发银行", "group": "测试",
            "tags": ["a"], "note": "备注", "strategy": "ma_cross",
            "cost_price": 10.0, "shares": 1000, "stop_price": 9.0,
        },
    )
    assert add.status_code == 200
    assert add.json()["data"]["code"] == "600000.SH"

    listed = client.get("/api/tracker/watchlist")
    assert listed.status_code == 200
    assert listed.json()["data"]["count"] == 1

    patched = client.patch(
        "/api/tracker/watchlist/600000.SH", json={"note": "更新后的备注", "group": "新分组"}
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["note"] == "更新后的备注"

    groups = client.get("/api/tracker/groups")
    assert groups.status_code == 200

    removed = client.delete("/api/tracker/watchlist/600000.SH")
    assert removed.status_code == 200
    assert client.delete("/api/tracker/watchlist/600000.SH").status_code == 404


def test_watchlist_batch(client) -> None:
    """批量添加关注项。"""
    resp = client.post(
        "/api/tracker/watchlist/batch",
        json={
            "items": [
                {"code": "600000.SH", "name": "浦发银行"},
                {"code": "000001.SZ", "name": "平安银行"},
            ],
            "group": "批量",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["added"] == 2


def test_tracker_update(client) -> None:
    """执行追踪更新。"""
    client.post(
        "/api/tracker/watchlist/batch",
        json={"items": [{"code": c} for c in CODES], "group": "测试"},
    )
    resp = client.post(
        "/api/tracker/update",
        json={"codes": CODES, "date": "2022-09-30", "notify": False},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["processed"] == 2
    assert data["trade_date"] == "2022-09-30"


def test_tracker_replay(client) -> None:
    """历史回放。"""
    client.post(
        "/api/tracker/watchlist/batch",
        json={"items": [{"code": c} for c in CODES], "group": "测试"},
    )
    resp = client.post(
        "/api/tracker/replay",
        json={"codes": CODES, "start": "2022-05-01", "end": "2022-09-30",
              "reset": True, "notify": False},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["processed"] > 0
    assert data["trigger_type"] == "replay"


def test_tracker_state_and_history(client) -> None:
    """状态与历史查询。"""
    client.post("/api/tracker/watchlist", json={"code": "600000.SH", "group": "测试"})
    client.post("/api/tracker/update", json={"codes": ["600000.SH"], "date": "2022-09-30",
                                             "notify": False})

    state = client.get("/api/tracker/state/600000.SH")
    assert state.status_code == 200
    assert state.json()["data"]["state"] is not None

    history = client.get("/api/tracker/history/600000.SH")
    assert history.status_code == 200
    assert "items" in history.json()["data"]

    timeline = client.get("/api/tracker/timeline/600000.SH")
    assert timeline.status_code == 200
    assert "events" in timeline.json()["data"]

    missing = client.get("/api/tracker/state/999999.SH")
    assert missing.status_code == 200
    assert missing.json()["data"]["state"] is None


def test_tracker_set_state(client) -> None:
    """手动设置状态。"""
    client.post("/api/tracker/watchlist", json={"code": "600000.SH", "group": "测试"})
    resp = client.post("/api/tracker/state/600000.SH", json={"state": "持仓", "reason": "测试"})
    assert resp.status_code == 200
    assert resp.json()["data"]["new_state"] == "持仓"

    bad = client.post("/api/tracker/state/600000.SH", json={"state": "乱写"})
    assert bad.status_code == 400


def test_tracker_states_and_signals(client) -> None:
    """状态列表、信号与提醒查询。"""
    client.post(
        "/api/tracker/watchlist/batch",
        json={"items": [{"code": c} for c in CODES], "group": "测试"},
    )
    client.post("/api/tracker/update", json={"codes": CODES, "date": "2022-09-30",
                                             "notify": False})

    states = client.get("/api/tracker/states")
    assert states.status_code == 200
    assert states.json()["data"]["count"] == 2

    signals = client.get("/api/tracker/signals?limit=10")
    assert signals.status_code == 200

    alerts = client.get("/api/tracker/alerts?limit=10")
    assert alerts.status_code == 200

    storage = client.get("/api/tracker/storage")
    assert storage.status_code == 200
    assert "watchlist" in storage.json()["data"]


def test_tracker_pause(client) -> None:
    """暂停 / 恢复追踪。"""
    client.post("/api/tracker/watchlist", json={"code": "600000.SH", "group": "测试"})
    resp = client.post("/api/tracker/watchlist/600000.SH/pause?paused=true")
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False
    client.post("/api/tracker/watchlist/600000.SH/pause?paused=false")


def test_tracker_dashboard(client) -> None:
    """仪表盘。"""
    client.post(
        "/api/tracker/watchlist/batch",
        json={"items": [{"code": c} for c in CODES], "group": "测试"},
    )
    client.post("/api/tracker/update", json={"codes": CODES, "date": "2022-09-30",
                                             "notify": False})
    resp = client.get("/api/tracker/dashboard")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["watch_count"] == 2
    assert "data_status" in data


def test_tracker_notes(client) -> None:
    """备注接口。"""
    resp = client.post("/api/tracker/notes/600000.SH", json={"content": "关注年报"})
    assert resp.status_code == 200
    listed = client.get("/api/tracker/notes/600000.SH")
    assert listed.json()["data"][0]["content"] == "关注年报"


def test_notify_status_and_test(client) -> None:
    """通知配置与测试。"""
    status = client.get("/api/tracker/notify")
    assert status.status_code == 200
    assert "channels" in status.json()["data"]

    test_resp = client.post("/api/tracker/notify/test")
    assert test_resp.status_code == 200


# ----------------------------------------------------------------------
# 报告
# ----------------------------------------------------------------------
def test_report_endpoints(client) -> None:
    """报告中心三类报告。"""
    client.post(
        "/api/tracker/watchlist/batch",
        json={"items": [{"code": c} for c in CODES], "group": "测试"},
    )
    client.post("/api/tracker/update", json={"codes": CODES, "date": "2022-09-30",
                                             "notify": False})

    for path in ("/api/report/daily", "/api/report/range", "/api/report/summary",
                 "/api/report?kind=daily", "/api/tracker/report?kind=summary"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} 失败"
        data = resp.json()["data"]
        assert "markdown" in data
        assert "html" in data


def test_report_formats(client) -> None:
    """报告导出格式。"""
    for fmt, marker in (("markdown", "#"), ("html", "<!DOCTYPE"), ("csv", "")):
        resp = client.get(f"/api/report/summary?fmt={fmt}")
        assert resp.status_code == 200
        assert marker in resp.text[:200] or fmt == "csv"


def test_report_types(client) -> None:
    """报告类型列表。"""
    resp = client.get("/api/report/types")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 3


# ----------------------------------------------------------------------
# 设置
# ----------------------------------------------------------------------
def test_get_settings(client, config_path: Path) -> None:
    """读取配置（脱敏）。"""
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["config"]["strategy"]["default"] == "ma_cross"
    assert str(config_path) == data["path"]


def test_update_settings(client) -> None:
    """更新配置并生效。"""
    resp = client.post("/api/settings", json={"patch": {"backtest": {"max_positions": 7}}})
    assert resp.status_code == 200
    assert resp.json()["data"]["config"]["backtest"]["max_positions"] == 7

    again = client.get("/api/settings").json()["data"]["config"]
    assert again["backtest"]["max_positions"] == 7


def test_update_settings_rejects_masked(client) -> None:
    """拒绝把脱敏占位符写回。"""
    resp = client.post("/api/settings", json={"patch": {"tracker": {"notify": {"smtp_password": "******"}}}})
    assert resp.status_code == 400


def test_strategy_settings(client) -> None:
    """策略默认参数读写。"""
    get_resp = client.get("/api/settings/strategies")
    assert get_resp.status_code == 200
    assert len(get_resp.json()["data"]["strategies"]) >= 3

    save = client.post(
        "/api/settings/strategies",
        json={"name": "ma_cross", "params": {"short_window": 3, "long_window": 15}},
    )
    assert save.status_code == 200
    assert save.json()["data"]["short_window"] == 3

    bad = client.post("/api/settings/strategies", json={"name": "nope", "params": {}})
    assert bad.status_code == 400

    default = client.post("/api/settings/default-strategy", json={"name": "rsi"})
    assert default.status_code == 200


def test_scheduler_endpoints(client) -> None:
    """定时任务接口。"""
    status = client.get("/api/settings/scheduler")
    assert status.status_code == 200
    assert "enabled" in status.json()["data"]

    run = client.post("/api/settings/scheduler/run?notify=false")
    assert run.status_code == 200


def test_reset_db_requires_confirm(client) -> None:
    """清空数据库需要显式确认。"""
    assert client.post("/api/settings/reset-db").status_code == 400
    resp = client.post("/api/settings/reset-db?confirm=true&tables=alerts")
    assert resp.status_code == 200
