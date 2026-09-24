"""策略基类与信号数据结构。

设计约定（很重要）：
    1. 策略只实现 :meth:`BaseStrategy.generate_signals`，输入单只股票的
       标准 K 线 DataFrame，输出与之等长的信号 DataFrame；
    2. 选股（:mod:`app.selector`）、回测（:mod:`app.backtest`）、
       追踪（:mod:`app.tracker`）全部复用同一份信号计算逻辑，
       避免「回测一套、追踪另一套」导致结果不一致；
    3. 所有指标计算只允许使用「当前及之前」的数据，严禁未来函数。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

# 信号取值
SIGNAL_BUY = 1
SIGNAL_HOLD = 0
SIGNAL_SELL = -1

SIGNAL_TEXT = {SIGNAL_BUY: "买入", SIGNAL_HOLD: "无信号", SIGNAL_SELL: "卖出"}


@dataclass
class SignalDetail:
    """单只股票在某一个交易日上的策略评估结果。

    该结构被选股、追踪、报告统一使用。
    """

    code: str
    date: Optional[pd.Timestamp]
    signal: int = SIGNAL_HOLD
    name: str = ""
    strategy: str = ""
    price: float = float("nan")
    prev_close: float = float("nan")
    pct_change: float = float("nan")
    reasons: List[str] = field(default_factory=list)
    conditions: Dict[str, bool] = field(default_factory=dict)
    factors: Dict[str, float] = field(default_factory=dict)
    is_new: bool = False  # 相对上一交易日是否为新出现的信号
    data_date: Optional[str] = None  # 实际使用的最后一根 K 线日期

    @property
    def signal_text(self) -> str:
        """信号的中文描述。"""
        return SIGNAL_TEXT.get(self.signal, "未知")

    @property
    def action(self) -> str:
        """标准化动作：``buy`` / ``sell`` / ``hold``。"""
        return {SIGNAL_BUY: "buy", SIGNAL_SELL: "sell"}.get(self.signal, "hold")

    @property
    def reason_text(self) -> str:
        """把若干条原因合并为一行文本。"""
        return "；".join(self.reasons) if self.reasons else "无"

    def to_dict(self) -> Dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return {
            "code": self.code,
            "name": self.name or self.code,
            "date": pd.Timestamp(self.date).strftime("%Y-%m-%d") if self.date is not None and not pd.isna(self.date) else None,
            "data_date": self.data_date,
            "strategy": self.strategy,
            "signal": int(self.signal),
            "signal_text": self.signal_text,
            "action": self.action,
            "is_new": bool(self.is_new),
            "price": _safe_float(self.price),
            "prev_close": _safe_float(self.prev_close),
            "pct_change": _safe_float(self.pct_change),
            "reasons": list(self.reasons),
            "reason_text": self.reason_text,
            "conditions": {k: bool(v) for k, v in self.conditions.items()},
            "factors": {k: _safe_float(v) for k, v in self.factors.items()},
        }


def _safe_float(value: Any) -> Optional[float]:
    """把 numpy 数值安全转换为 Python float（NaN -> None）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(f) or np.isinf(f):
        return None
    return round(f, 6)


class BaseStrategy(abc.ABC):
    """所有策略的抽象基类。

    子类必须提供：
        * 类属性 :attr:`name` / :attr:`display_name` / :attr:`default_params`
        * 方法 :meth:`generate_signals`
        * 方法 :meth:`describe_conditions`（用于在 UI 上展示选股条件）

    使用方式::

        strategy = MaCrossStrategy({"short_window": 5, "long_window": 20})
        signals = strategy.generate_signals(df)
        detail = strategy.evaluate("600000.SH", df, date="2024-01-02")
    """

    #: 策略唯一标识（英文短名，用于 API / 配置）
    name: str = "base"
    #: 中文显示名
    display_name: str = "基础策略"
    #: 策略说明
    description: str = ""
    #: 默认参数
    default_params: Dict[str, Any] = {}
    #: 参数元信息：{参数名: {"type","label","min","max","step","help"}}
    param_schema: Dict[str, Dict[str, Any]] = {}
    #: 因子列（用于表格展示，必须存在于 generate_signals 的输出中）
    factor_columns: Sequence[str] = ()
    #: 至少需要多少根 K 线才能产生有效信号
    min_bars: int = 30
    #: 是否需要股票基础信息（流通股本）。
    #: 为 True 时，选股 / 回测 / 追踪会在调用策略前把 ``float_shares`` 列
    #: 注入 K 线 DataFrame（见 :func:`app.data.basics.attach_float_shares`）。
    requires_basics: bool = False

    def __init__(self, params: Optional[Dict[str, Any]] = None) -> None:
        self.params: Dict[str, Any] = {}
        self.initialize(params or {})

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def initialize(self, params: Optional[Dict[str, Any]] = None) -> None:
        """合并并校验参数。

        :param params: 用户参数，覆盖 :attr:`default_params`
        """
        merged = dict(self.default_params)
        for key, value in (params or {}).items():
            merged[key] = value
        self.params = self._validate_params(merged)

    def _validate_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """参数校验与类型转换，子类可覆盖。

        :param params: 待校验参数
        :return: 校验后的参数
        """
        out: Dict[str, Any] = {}
        for key, value in params.items():
            schema = self.param_schema.get(key, {})
            ptype = schema.get("type")
            if ptype == "int":
                out[key] = int(value)
            elif ptype == "float":
                out[key] = float(value)
            elif ptype == "bool":
                out[key] = bool(value)
            else:
                out[key] = value
            if "min" in schema and isinstance(out[key], (int, float)):
                if out[key] < schema["min"]:
                    raise ValueError(
                        f"参数 {key}={out[key]} 小于允许的最小值 {schema['min']}"
                    )
            if "max" in schema and isinstance(out[key], (int, float)):
                if out[key] > schema["max"]:
                    raise ValueError(
                        f"参数 {key}={out[key]} 大于允许的最大值 {schema['max']}"
                    )
        self._post_validate(out)
        return out

    def _post_validate(self, params: Dict[str, Any]) -> None:
        """跨参数校验钩子，子类可覆盖（如 short_window < long_window）。"""

    def get_params(self) -> Dict[str, Any]:
        """返回当前策略参数的副本。"""
        return dict(self.params)

    def get_param(self, key: str, default: Any = None) -> Any:
        """读取单个参数。"""
        return self.params.get(key, default)

    @classmethod
    def describe(cls) -> Dict[str, Any]:
        """返回策略元信息，供 Web UI 动态生成参数表单。"""
        return {
            "name": cls.name,
            "display_name": cls.display_name,
            "description": cls.description,
            "default_params": dict(cls.default_params),
            "param_schema": dict(cls.param_schema),
            "factor_columns": list(cls.factor_columns),
            "min_bars": cls.min_bars,
            "requires_basics": bool(cls.requires_basics),
        }

    def required_bars(self) -> int:
        """返回本策略参数下至少需要的 K 线根数。"""
        return max(self.min_bars, self._compute_required_bars())

    def _compute_required_bars(self) -> int:
        """由参数推导最小 K 线根数，子类覆盖。"""
        return self.min_bars

    # ------------------------------------------------------------------
    # 信号（子类实现）
    # ------------------------------------------------------------------
    @abc.abstractmethod
    def generate_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """计算信号列。

        :param data: 标准 K 线 DataFrame，列至少包含
            ``datetime/open/high/low/close/volume/amount``，按时间升序
        :return: 与 ``data`` 等长、同索引的 DataFrame，至少包含：
            * ``signal``（int，1 买入 / -1 卖出 / 0 无信号）
            * ``reason``（str，触发原因）
            以及 :attr:`factor_columns` 中声明的因子列
        """
        raise NotImplementedError

    @classmethod
    def describe_conditions(cls) -> List[str]:
        """返回人类可读的选股条件列表，用于报告与 UI。"""
        return []

    # ------------------------------------------------------------------
    # 通用能力
    # ------------------------------------------------------------------
    def min_required_bars(self) -> int:
        """兼容别名。"""
        return self.required_bars()

    def evaluate(
        self,
        code: str,
        data: pd.DataFrame,
        date: Optional[str | pd.Timestamp] = None,
        name: str = "",
        max_bars: Optional[int] = None,
    ) -> Optional[SignalDetail]:
        """评估某只股票在指定交易日的信号。

        只使用 ``date`` 及之前的数据（切片后再计算信号），从根本上杜绝未来函数。

        :param code: 股票代码
        :param data: 该股票的完整 K 线
        :param date: 目标日期；为 None 时取最后一根 K 线
        :param name: 股票名称
        :param max_bars: 只取最近 N 根 K 线参与计算（提升批量选股速度）
        :return: :class:`SignalDetail`；数据不足时返回 None
        """
        if data is None or len(data) == 0:
            return None

        df = data
        target: Optional[pd.Timestamp] = None
        if date is not None:
            target = pd.Timestamp(str(date))
            df = df[df["datetime"] <= target]
        if len(df) == 0:
            return None
        if max_bars and len(df) > max_bars:
            df = df.iloc[-max_bars:]
        if len(df) < self.required_bars():
            return None

        signals = self.generate_signals(df)
        if signals is None or len(signals) == 0:
            return None
        return self.build_detail(code, df, signals, len(df) - 1, name=name)

    def build_detail(
        self,
        code: str,
        data: pd.DataFrame,
        signals: pd.DataFrame,
        idx: int,
        name: str = "",
    ) -> Optional[SignalDetail]:
        """基于预先计算好的信号表构造 :class:`SignalDetail`。

        该方法被 :meth:`evaluate`、追踪回放（逐个历史交易日）与回测共用，
        确保三处的信号解读方式完全一致。

        :param code: 股票代码
        :param data: 与 ``signals`` 等长的 K 线数据
        :param signals: :meth:`generate_signals` 的输出
        :param idx: 行位置（0-based）
        :param name: 股票名称
        :return: :class:`SignalDetail`；下标越界时返回 None
        """
        if data is None or signals is None:
            return None
        if idx < 0 or idx >= len(data) or idx >= len(signals):
            return None

        row = signals.iloc[idx]
        last_bar = data.iloc[idx]

        prev_signal = SIGNAL_HOLD
        if idx >= 1:
            prev_signal = int(signals["signal"].iloc[idx - 1])

        cur_signal = int(row.get("signal", SIGNAL_HOLD))

        factors: Dict[str, float] = {}
        for col in self.factor_columns:
            if col in signals.columns:
                factors[col] = row.get(col)

        conditions: Dict[str, bool] = {}
        for col in signals.columns:
            if col.endswith("_cond"):
                conditions[col] = bool(row.get(col, False))

        reason_raw = row.get("reason", "")
        reasons = [r for r in str(reason_raw).split("|") if r] if reason_raw else []

        prev_close = (
            float(data["close"].iloc[idx - 1]) if idx >= 1 else float("nan")
        )
        price = float(last_bar["close"])
        pct = (
            (price / prev_close - 1.0)
            if prev_close and not np.isnan(prev_close)
            else float("nan")
        )

        return SignalDetail(
            code=code,
            name=name or code,
            date=pd.Timestamp(last_bar["datetime"]),
            data_date=pd.Timestamp(last_bar["datetime"]).strftime("%Y-%m-%d"),
            strategy=self.name,
            signal=cur_signal,
            price=price,
            prev_close=prev_close,
            pct_change=pct,
            reasons=reasons,
            conditions=conditions,
            factors=factors,
            is_new=(cur_signal != SIGNAL_HOLD and cur_signal != prev_signal),
        )

    def select_stocks(
        self,
        data: Dict[str, pd.DataFrame],
        date: Optional[str | pd.Timestamp] = None,
        names: Optional[Dict[str, str]] = None,
        min_bars: Optional[int] = None,
        only_buy: bool = True,
        progress: bool = False,
    ) -> List[SignalDetail]:
        """从股票池中筛选满足条件的股票。

        :param data: ``{code: DataFrame}``
        :param date: 目标交易日
        :param names: 代码到名称的映射
        :param min_bars: 覆盖策略默认的最小 K 线要求
        :param only_buy: 只保留买入信号（False 时保留所有非 0 信号）
        :param progress: 是否显示进度条
        :return: :class:`SignalDetail` 列表，按信号强度排序
        """
        need = max(self.required_bars(), int(min_bars or 0))
        items: Iterable = data.items()
        if progress:
            try:
                from tqdm import tqdm

                items = tqdm(list(data.items()), desc=f"{self.display_name} 选股", ncols=80)
            except Exception:  # pragma: no cover
                items = data.items()

        results: List[SignalDetail] = []
        for code, df in items:
            try:
                detail = self.evaluate(
                    code,
                    df,
                    date=date,
                    name=(names or {}).get(code, ""),
                    max_bars=need + 5,
                )
            except Exception:  # 单只股票失败不影响整体
                continue
            if detail is None:
                continue
            if only_buy and detail.signal != SIGNAL_BUY:
                continue
            if not only_buy and detail.signal == SIGNAL_HOLD:
                continue
            results.append(detail)

        # 排序：新信号优先，其次按当日涨幅
        results.sort(
            key=lambda d: (
                0 if d.is_new else 1,
                -(d.pct_change if d.pct_change == d.pct_change else -999),
            )
        )
        return results

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _empty_signals(data: pd.DataFrame) -> pd.DataFrame:
        """生成与输入等长的空信号表。"""
        return pd.DataFrame(
            {
                "signal": pd.Series(0, index=data.index, dtype="int64"),
                "reason": pd.Series("", index=data.index, dtype="object"),
            }
        )

    def _ensure_columns(self, data: pd.DataFrame) -> pd.DataFrame:
        """校验输入数据是否具备必要列。

        :raises ValueError: 缺少必要列时抛出
        """
        required = {"datetime", "open", "high", "low", "close", "volume"}
        missing = required - set(data.columns)
        if missing:
            raise ValueError(f"K 线数据缺少必要列：{sorted(missing)}")
        return data

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.__class__.__name__} params={self.params}>"
