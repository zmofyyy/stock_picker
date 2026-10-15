"""配置加载与保存。

目录分两类，打包成 exe 后必须区分开：

- ``RESOURCE_DIR``：代码与内置静态资源所在目录。打包后位于 PyInstaller 的
  解包目录（onefile 模式是临时目录，**退出即删**），只读。
- ``APP_DIR``：可写的应用目录，放 ``config.json`` / ``data/`` / ``data/backups/``。
  优先级：环境变量 ``STOCK_PICKER_HOME`` > exe 所在目录（打包后）> 包目录（源码运行）。

把可写状态留在包目录里，onefile 打包后计划/追踪会随解包目录一起被清掉，
所以两者一定要分开。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

RESOURCE_DIR = Path(__file__).resolve().parent


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def _app_dir() -> Path:
    env = os.environ.get("STOCK_PICKER_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return RESOURCE_DIR


APP_DIR = _app_dir()
ROOT = RESOURCE_DIR          # 兼容既有引用（语义 = 资源目录）
DATA_DIR = APP_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
DEFAULT_CONFIG_FILE = APP_DIR / "config.json"

DEFAULTS: Dict[str, Any] = {
    "tdx_dir": r"D:\new_tdx64",
    "host": "127.0.0.1",
    "port": 8778,
    "reader_mode": "fast",
    "names_csv": "",
    # 通达信量化公式引擎（PYPlugins/sys/tqcenter.py + TPythClient.dll）。
    # 需要对应版本的 TdxW.exe **正在运行**，且客户端已下载盘后数据。
    "tdx_quant": {
        "enabled": True,
        "sdk_dir": "",      # 留空 = <tdx_dir>/PYPlugins/sys
        "dll_path": "",     # 留空 = <tdx_dir>/PYPlugins/TPythClient.dll
        # 回看窗口（交易日）= 喂给公式的 K 线根数 = 每股返回的点数。
        # 「信号日」只能在这个窗口内选：窗口越大越慢、RPC 载荷越大。
        # 全市场实测：window=500 → 11.5s，覆盖约 2 年；window=30 → 5.4s，约 6.5 周。
        "window": 500,
        "max_window": 500,  # 前端「回看」输入框与接口的上限
        "dividend_type": 1, # 0 不复权 / 1 前复权 / 2 后复权
        "timeout_warn": 30, # 单次选股超过该秒数在返回里给出提示
    },
    "screen": {
        "ma_window": 20,
        "volume_ratio": 2.0,
        "lookback_days": 1,
        "min_bars": 30,
        "max_results": 300,
        "min_amount": 0.0,
        "max_float_mcap": 150.0,
        "exclude_st": True,
        "markets": ["sh", "sz", "bj"],
    },
    "plan": {
        "sell_profit": 0.045,
        "buy_discount": 0.0,
    },
    # 每只股票缓存多少根 K 线。0 或负数 = 不限制，读入 .day 的全部历史。
    # 全市场（5605 只）全历史约 1700 万行，所以缓存列一律走收缩 dtype，
    # 详见 tdx_reader.CACHE_DTYPES。
    "cache_bars": 0,
    "bootstrap": {
        "preload": True,          # 启动即在后台把本地已存在的数据全部载入
        "refresh_if_stale": True, # 本地行情比缓存新时自动重建缓存
        # 只作用于「本地筛选」模式。**通达信策略永远不自动取数** ——
        # 打开页面 / 换公式 / 改信号日都只标「待执行」，必须点「执行策略」。
        "auto_screen": True,
    },
    "notify": {"console": True},
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """轻量配置对象（JSON 存储，避免额外依赖）。"""

    def __init__(self, path: Path | str = DEFAULT_CONFIG_FILE) -> None:
        self.path = Path(path)
        self.data: Dict[str, Any] = dict(DEFAULTS)
        self.reload()

    def reload(self) -> Dict[str, Any]:
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = _deep_merge(DEFAULTS, raw)
            except Exception:
                self.data = dict(DEFAULTS)
        else:
            self.save()
        return self.data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def section(self, key: str) -> Dict[str, Any]:
        value = self.data.get(key)
        return dict(value) if isinstance(value, dict) else {}

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """深合并写入并落盘。"""
        self.data = _deep_merge(self.data, patch)
        self.save()
        return self.data
