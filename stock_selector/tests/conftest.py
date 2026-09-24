"""pytest 全局夹具。

提供：
    * ``tdx_root``      —— 临时通达信数据目录（含 .day / .lc5 / 指数文件）
    * ``config_path``   —— 指向该目录的临时 config.yaml
    * ``services``      —— 基于临时配置构建的服务集合
    * ``client``        —— FastAPI TestClient
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

# 让测试可以直接 import backend.app.*
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.container import build_services, reset_services  # noqa: E402
from tests.helpers import build_index_file, build_minute_tree, build_tdx_tree  # noqa: E402

DEFAULT_SYMBOLS = {
    "600000": {"seed": 1, "base_price": 12.0, "period": 40},
    "000001": {"seed": 2, "base_price": 9.0, "period": 30},
    "300750": {"seed": 3, "base_price": 25.0, "period": 50},
    "830000": {"seed": 4, "base_price": 6.0, "period": 25},
}

TEST_START = "2022-01-03"
TEST_DAYS = 260


@pytest.fixture(scope="session")
def tdx_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """构造一个完整的临时通达信目录。"""
    root = tmp_path_factory.mktemp("tdx")
    build_tdx_tree(root, symbols=DEFAULT_SYMBOLS, days=TEST_DAYS, start=TEST_START)
    build_index_file(root, symbol="000300", days=TEST_DAYS, start=TEST_START)
    build_minute_tree(root, symbol="600000", days=5)
    return root


@pytest.fixture(scope="session")
def sample_bars(tdx_root: Path) -> dict:
    """返回测试用的原始 K 线（便于断言具体数值）。"""
    from tests.helpers import make_bars, trading_days

    dates = trading_days(TEST_START, TEST_DAYS)
    return {"600000.SH": make_bars(dates, **DEFAULT_SYMBOLS["600000"])}


@pytest.fixture()
def config_path(tmp_path: Path, tdx_root: Path) -> Path:
    """生成临时配置文件。"""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = (tmp_path / "tracking.db").as_posix()

    cfg = {
        "data": {
            "tdx_dir": tdx_root.as_posix(),
            "cache_dir": cache_dir.as_posix(),
            "cache_format": "pickle",  # 测试环境不依赖 pyarrow
            "start_date": TEST_START,
            "adjust": "none",
        },
        "strategy": {
            "default": "ma_cross",
            "ma_cross": {"short_window": 5, "long_window": 20},
            "rsi": {"period": 14, "buy_threshold": 30, "sell_threshold": 70},
            "volume_breakout": {"high_window": 20, "volume_ratio": 1.5, "volume_ma_window": 5},
        },
        "screen": {"min_bars": 30, "max_results": 50},
        "backtest": {
            "initial_cash": 1_000_000,
            "commission": 0.0003,
            "stamp_tax": 0.001,
            "slippage": 0.0002,
            "min_commission": 5.0,
            "max_positions": 3,
            "position_sizing": "equal",
            "exec_price": "next_open",
            "t_plus_1": True,
            "benchmark": "000300.SH",
            "risk_free_rate": 0.02,
            "trading_days": 252,
        },
        "tracker": {
            "enabled": True,
            "storage": f"sqlite:///{db_path}",
            "update_time": "15:30",
            "auto_update": False,
            "states": [
                "候选", "观察", "触发", "买入", "持仓", "减仓",
                "清仓", "止盈", "止损", "失效", "暂停",
            ],
            "alert_states": ["买入", "清仓", "止盈", "止损", "失效"],
            "risk": {
                "near_stop_ratio": 0.02,
                "vol_spike_ratio": 1.5,
                "drop_pct": 0.03,
                "break_ma_window": 20,
            },
            "notify": {"console": False, "csv": False, "webhook": "", "email": ""},
        },
        "web": {"host": "127.0.0.1", "port": 8000, "cors_origins": ["*"]},
        "logging": {"level": "WARNING", "file": (tmp_path / "test.log").as_posix(), "console": False},
    }

    path = tmp_path / "config.yaml"
    with open(path, "w", encoding="utf-8") as fp:
        yaml.safe_dump(cfg, fp, allow_unicode=True, sort_keys=False)
    return path


@pytest.fixture()
def services(config_path: Path):
    """构建基于临时配置的服务集合。"""
    os.environ["STOCK_SELECTOR_CONFIG"] = str(config_path)
    reset_services()
    svc = build_services(str(config_path))
    yield svc
    try:
        svc.tracker.storage.close()
    except Exception:
        pass
    reset_services()


@pytest.fixture()
def client(services, monkeypatch):
    """FastAPI 测试客户端。"""
    from fastapi.testclient import TestClient

    from backend.app.main import app

    with TestClient(app) as c:
        yield c
