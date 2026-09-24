"""配置管理模块。

职责：
    1. 加载 / 保存 ``config.yaml``；
    2. 提供基于「点号路径」的读写接口，例如 ``cfg.get("strategy.ma_cross.short_window")``；
    3. 统一解析相对路径（以配置文件所在目录为基准）；
    4. 支持运行时覆盖（CLI 参数、Web UI 提交），并记录覆盖项。

设计说明：
    配置对象为进程内单例。Web UI 修改配置后调用 :meth:`Config.save` 落盘，
    并调用 :meth:`Config.reload` 让内存状态与磁盘保持一致。
"""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

# 项目根目录：.../stock_selector
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# 默认配置文件路径
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class ConfigError(Exception):
    """配置相关异常。"""


# 内置默认配置：当 config.yaml 缺失或字段不全时兜底，保证程序不会崩溃。
DEFAULTS: Dict[str, Any] = {
    "data": {
        "tdx_dir": "",
        "cache_dir": "./cache",
        "cache_format": "parquet",
        "start_date": "2015-01-01",
        "universe": [],
        "exclude_st": False,
        "adjust": "none",
        "adjust_file": "",
        "basics_file": "",
    },
    "strategy": {
        "default": "ma_cross",
        "ma_cross": {"short_window": 5, "long_window": 20},
        "rsi": {"period": 14, "buy_threshold": 30, "sell_threshold": 70},
        "volume_breakout": {
            "high_window": 20,
            "volume_ratio": 2.0,
            "volume_ma_window": 5,
        },
        "volume_surge": {
            "volume_ma_window": 20,
            "volume_ratio": 2.0,
            "cap_metric": "float_market_cap",
            "max_float_market_cap": 150.0,
            "max_float_shares": 150.0,
            "exclude_current_volume": True,
            "exit_ma_window": 20,
            "use_ma_exit": True,
            "allow_missing_basics": False,
        },
    },
    "screen": {"min_bars": 60, "max_results": 200},
    "backtest": {
        "initial_cash": 1_000_000,
        "commission": 0.0003,
        "stamp_tax": 0.001,
        "slippage": 0.0002,
        "max_positions": 10,
        "position_sizing": "equal",
        "fixed_amount": 100_000,
        "min_commission": 5.0,
        "exec_price": "next_open",
        "limit_pct": 0.10,
        "limit_pct_star": 0.20,
        "t_plus_1": True,
        "benchmark": "000300.SH",
        "risk_free_rate": 0.02,
        "trading_days": 252,
    },
    "tracker": {
        "enabled": True,
        "storage": "sqlite:///tracking.db",
        "update_time": "15:30",
        "auto_update": False,
        "states": [
            "候选", "观察", "触发", "买入", "持仓", "减仓",
            "清仓", "止盈", "止损", "失效", "暂停",
        ],
        "alert_states": ["买入", "清仓", "止盈", "止损", "失效"],
        "risk": {
            "near_stop_ratio": 0.02,
            "volume_drop_ratio": 0.6,
            "vol_spike_ratio": 1.5,
            "drop_pct": 0.03,
            "break_ma_window": 20,
        },
        "notify": {
            "console": True,
            "csv": True,
            "csv_path": "./reports/alerts.csv",
            "webhook": "",
            "webhook_type": "dingtalk",
            "webhook_secret": "",
            "email": "",
            "smtp_host": "",
            "smtp_port": 465,
            "smtp_user": "",
            "smtp_password": "",
            "smtp_ssl": True,
        },
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8000,
        "cors_origins": ["http://localhost:5173", "http://127.0.0.1:5173"],
        "serve_frontend": True,
    },
    "logging": {
        "level": "INFO",
        "file": "./logs/stock_selector.log",
        "console": True,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并字典，``override`` 优先。

    :param base: 基础字典（不会被修改）
    :param override: 覆盖字典
    :return: 合并后的新字典
    """
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config:
    """配置对象（线程安全）。

    :param path: 配置文件路径，默认 ``<project>/config.yaml``
    :param auto_create: 配置文件不存在时是否自动创建
    """

    def __init__(
        self,
        path: Optional[os.PathLike | str] = None,
        auto_create: bool = True,
    ) -> None:
        self.path: Path = Path(path).resolve() if path else DEFAULT_CONFIG_PATH
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._overrides: Dict[str, Any] = {}
        self.load(auto_create=auto_create)

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------
    def load(self, auto_create: bool = True) -> Dict[str, Any]:
        """从磁盘加载配置并与默认值合并。

        :param auto_create: 文件不存在时是否写出默认配置
        :return: 合并后的配置字典
        """
        with self._lock:
            raw: Dict[str, Any] = {}
            if self.path.exists():
                try:
                    with open(self.path, "r", encoding="utf-8") as fp:
                        raw = yaml.safe_load(fp) or {}
                    if not isinstance(raw, dict):
                        raise ConfigError(
                            f"配置文件格式错误，顶层应为字典：{self.path}"
                        )
                except ConfigError:
                    raise
                except Exception as exc:  # pragma: no cover - 依赖文件系统状态
                    raise ConfigError(f"读取配置文件失败：{exc}") from exc
            elif auto_create:
                self._data = copy.deepcopy(DEFAULTS)
                self.save()
                return copy.deepcopy(self._data)

            self._data = _deep_merge(DEFAULTS, raw)
            return copy.deepcopy(self._data)

    def reload(self) -> Dict[str, Any]:
        """重新从磁盘加载配置。"""
        return self.load(auto_create=False)

    def save(self) -> None:
        """将当前配置写回磁盘（UTF-8，保留中文）。"""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fp:
                yaml.safe_dump(
                    self._data,
                    fp,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                )

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """按点号路径读取配置。

        :param key: 形如 ``"backtest.max_positions"``
        :param default: 路径不存在时返回的默认值
        """
        with self._lock:
            node: Any = self._data
            for part in key.split("."):
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    return default
            return copy.deepcopy(node)

    def set(self, key: str, value: Any, persist: bool = False) -> None:
        """按点号路径写入配置。

        :param key: 形如 ``"tracker.enabled"``
        :param value: 新值
        :param persist: 是否立即写回磁盘
        """
        with self._lock:
            parts = key.split(".")
            node = self._data
            for part in parts[:-1]:
                if part not in node or not isinstance(node[part], dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = value
            if persist:
                self.save()

    def update(self, patch: Dict[str, Any], persist: bool = True) -> Dict[str, Any]:
        """批量深度更新配置。

        :param patch: 局部配置字典，例如 ``{"backtest": {"max_positions": 5}}``
        :param persist: 是否立即写回磁盘
        :return: 更新后的完整配置
        """
        with self._lock:
            self._data = _deep_merge(self._data, patch or {})
            if persist:
                self.save()
            return copy.deepcopy(self._data)

    def as_dict(self) -> Dict[str, Any]:
        """返回配置的深拷贝字典。"""
        with self._lock:
            return copy.deepcopy(self._data)

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------
    def resolve_path(self, value: str | os.PathLike) -> Path:
        """将配置中的路径解析为绝对路径。

        相对路径以配置文件所在目录为基准；``~`` 会被展开为用户目录。

        :param value: 原始路径
        :return: 绝对 Path
        """
        p = Path(os.path.expanduser(str(value)))
        if not p.is_absolute():
            p = (self.path.parent / p).resolve()
        return p

    def get_path(self, key: str, default: str = "") -> Path:
        """读取路径类配置并解析为绝对路径。"""
        return self.resolve_path(self.get(key, default))

    def ensure_dirs(self) -> None:
        """创建运行期所需的目录（缓存 / 日志 / 报告）。"""
        for key in ("data.cache_dir",):
            raw = self.get(key)
            if raw:
                self.resolve_path(raw).mkdir(parents=True, exist_ok=True)
        log_file = self.get("logging.file")
        if log_file:
            self.resolve_path(log_file).parent.mkdir(parents=True, exist_ok=True)
        csv_path = self.get("tracker.notify.csv_path")
        if csv_path:
            self.resolve_path(csv_path).parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 数据库
    # ------------------------------------------------------------------
    def sqlalchemy_url(self, key: str = "tracker.storage") -> str:
        """把配置中的 storage 字符串转换成 SQLAlchemy 可用的 URL。

        支持 ``sqlite:///tracking.db`` 形式的相对路径，会转换成
        ``sqlite:///<绝对路径>``，保证换工作目录后数据库位置不变。
        """
        raw = str(self.get(key, "sqlite:///tracking.db") or "sqlite:///tracking.db")
        if raw.startswith("sqlite:///") and not raw.startswith("sqlite:////"):
            rel = raw[len("sqlite:///"):]
            if rel in ("", ":memory:"):
                return "sqlite://"
            return "sqlite:///" + self.resolve_path(rel).as_posix()
        return raw


# ----------------------------------------------------------------------
# 单例
# ----------------------------------------------------------------------
_config: Optional[Config] = None
_config_lock = threading.Lock()


def get_config(path: Optional[os.PathLike | str] = None, reload: bool = False) -> Config:
    """获取全局配置单例。

    :param path: 首次调用时指定配置文件路径；后续调用若传入不同路径会重新创建
    :param reload: 是否强制重新读取磁盘
    """
    global _config
    with _config_lock:
        if _config is None or (
            path is not None and Path(path).resolve() != _config.path
        ):
            _config = Config(path)
        elif reload:
            _config.reload()
        return _config


def set_config(cfg: Config) -> None:
    """替换全局配置单例（主要供测试使用）。"""
    global _config
    with _config_lock:
        _config = cfg


def get_strategy_params(name: str) -> Dict[str, Any]:
    """读取指定策略在配置中的默认参数。

    :param name: 策略名，例如 ``"ma_cross"``
    :return: 参数字典（找不到时返回空字典）
    """
    params = get_config().get(f"strategy.{name}", {})
    return params if isinstance(params, dict) else {}


def available_strategy_names() -> List[str]:
    """返回配置文件中已登记的策略名（排除 ``default`` 字段）。"""
    strategy_cfg = get_config().get("strategy", {}) or {}
    return [k for k, v in strategy_cfg.items() if isinstance(v, dict)]


def parse_cli_overrides(items: Optional[Iterable[str]]) -> Dict[str, Any]:
    """把 ``["a.b=1", "c.d=x"]`` 形式的 CLI 覆盖项转成嵌套字典。

    值的类型会自动推断：整数 / 浮点 / 布尔 / null / 字符串。

    :param items: 形如 ``key.subkey=value`` 的字符串序列
    :return: 可用于 :meth:`Config.update` 的嵌套字典，例如
        ``{"a": {"b": 1}, "c": {"d": "x"}}``
    """
    result: Dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            continue
        key, _, raw = item.partition("=")
        key = key.strip()
        if not key:
            continue
        parts = [part for part in key.split(".") if part]
        if not parts:
            continue
        value = _coerce(raw.strip())
        # 按点号路径逐层下钻，保证 --set data.tdx_dir=xxx 真的改到 data.tdx_dir
        node = result
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
    return result


def _coerce(raw: str) -> Any:
    """把字符串字面量转换为 Python 值。"""
    low = raw.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    # 支持 JSON 风格的 list / dict
    if raw.startswith("[") or raw.startswith("{"):
        try:
            return yaml.safe_load(raw)
        except Exception:  # pragma: no cover
            return raw
    return raw
