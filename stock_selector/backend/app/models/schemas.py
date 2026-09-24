"""Pydantic 请求/响应模型。

只承载 HTTP 层的输入输出校验，业务实体仍以 ORM 字典为准，
避免出现「Schema 与 ORM 双份维护」的问题。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator


class ApiResponse(BaseModel):
    """统一响应包装。"""

    ok: bool = True
    message: str = ""
    data: Any = None


# ----------------------------------------------------------------------
# 数据
# ----------------------------------------------------------------------
class DataLoadRequest(BaseModel):
    """数据加载请求。"""

    codes: Optional[List[str]] = Field(default=None, description="指定代码；为空则扫描股票池")
    freq: str = Field(default="daily", description="daily / 1min / 5min / 15min / 30min / 60min")
    start: Optional[str] = Field(default=None, description="起始日期 YYYY-MM-DD")
    end: Optional[str] = Field(default=None, description="结束日期 YYYY-MM-DD")
    limit: Optional[int] = Field(default=None, ge=1, description="最多加载多少只")
    force: bool = Field(default=False, description="是否强制重新读取（忽略缓存）")
    tdx_dir: Optional[str] = Field(default=None, description="临时覆盖通达信目录")


class QualityCheckRequest(BaseModel):
    """数据质量检查请求。"""

    codes: Optional[List[str]] = None
    max_codes: int = Field(default=200, ge=1, le=5000)


class NamesUploadRequest(BaseModel):
    """股票名称映射上传。"""

    names: Dict[str, str] = Field(default_factory=dict, description="{code: name}")


class BasicsUploadRequest(BaseModel):
    """股票基础信息（流通股本）上传。

    支持两种写法::

        {"600000.SH": {"float_shares": "65.7亿", "name": "浦发银行"}}
        {"600000.SH": 6570000000}
    """

    basics: Dict[str, Any] = Field(default_factory=dict, description="{code: 流通股本或明细}")


# ----------------------------------------------------------------------
# 选股
# ----------------------------------------------------------------------
class ScreenRequest(BaseModel):
    """选股请求。"""

    strategy: Optional[str] = Field(default=None, description="策略名，默认取配置")
    params: Dict[str, Any] = Field(default_factory=dict, description="策略参数覆盖")
    date: Optional[str] = Field(default=None, description="选股日期 YYYY-MM-DD")
    start: Optional[str] = Field(default=None, description="区间选股起始日")
    end: Optional[str] = Field(default=None, description="区间选股结束日")
    scan_all: bool = Field(default=False, description="是否在区间内逐日扫描")
    universe: Optional[List[str]] = Field(default=None, description="股票池；为空则用本地全部")
    min_bars: int = Field(default=60, ge=0)
    max_results: int = Field(default=200, ge=1, le=5000)
    only_buy: bool = Field(default=True)
    limit_universe: Optional[int] = Field(default=None, ge=1, description="限制股票池大小（调试）")

    @field_validator("params")
    @classmethod
    def _strip_none(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """去掉值为 None 的参数，避免覆盖配置默认值。"""
        return {k: val for k, val in (v or {}).items() if val is not None}


class AddToWatchlistRequest(BaseModel):
    """把选股结果加入关注池。"""

    codes: Optional[List[str]] = Field(default=None, description="指定代码；为空则用最近一次选股结果")
    group: str = "选股结果"
    strategy: Optional[str] = None
    note: str = ""


# ----------------------------------------------------------------------
# 回测
# ----------------------------------------------------------------------
class BacktestRequest(BaseModel):
    """回测请求。"""

    strategy: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    start: str = Field(description="起始日期 YYYY-MM-DD")
    end: str = Field(description="结束日期 YYYY-MM-DD")
    universe: Optional[List[str]] = None
    max_universe: Optional[int] = Field(default=None, ge=1)

    initial_cash: Optional[float] = Field(default=None, gt=0)
    commission: Optional[float] = Field(default=None, ge=0)
    stamp_tax: Optional[float] = Field(default=None, ge=0)
    slippage: Optional[float] = Field(default=None, ge=0)
    min_commission: Optional[float] = Field(default=None, ge=0)
    max_positions: Optional[int] = Field(default=None, ge=1, le=200)
    position_sizing: Optional[str] = Field(default=None, pattern="^(equal|fixed)$")
    fixed_amount: Optional[float] = Field(default=None, gt=0)
    exec_price: Optional[str] = Field(default=None, pattern="^(next_open|next_close|close)$")
    t_plus_1: Optional[bool] = None
    benchmark: Optional[str] = None
    risk_free_rate: Optional[float] = None
    trading_days: Optional[int] = Field(default=None, ge=1)

    @field_validator("params")
    @classmethod
    def _strip_none(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        """去掉空参数。"""
        return {k: val for k, val in (v or {}).items() if val is not None}

    def to_engine_config(self, base: Dict[str, Any]) -> Dict[str, Any]:
        """把请求中的非空字段合并到基础配置上。"""
        cfg = dict(base or {})
        for key in (
            "initial_cash", "commission", "stamp_tax", "slippage", "min_commission",
            "max_positions", "position_sizing", "fixed_amount", "exec_price",
            "t_plus_1", "benchmark", "risk_free_rate", "trading_days",
        ):
            value = getattr(self, key)
            if value is not None:
                cfg[key] = value
        return cfg


# ----------------------------------------------------------------------
# 追踪
# ----------------------------------------------------------------------
class WatchAddRequest(BaseModel):
    """添加关注项。"""

    code: str
    name: str = ""
    group: str = "默认分组"
    tags: List[str] = Field(default_factory=list)
    note: str = ""
    strategy: str = ""
    strategy_params: Dict[str, Any] = Field(default_factory=dict)
    cost_price: Optional[float] = None
    shares: Optional[int] = None
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    source: str = "manual"
    overwrite: bool = False
    state: Optional[str] = None


class WatchBatchAddRequest(BaseModel):
    """批量添加关注项。"""

    items: List[WatchAddRequest]
    group: str = "默认分组"
    source: str = "manual"
    overwrite: bool = False


class WatchUpdateRequest(BaseModel):
    """更新关注项。"""

    name: Optional[str] = None
    group: Optional[str] = None
    tags: Optional[List[str]] = None
    note: Optional[str] = None
    strategy: Optional[str] = None
    strategy_params: Optional[Dict[str, Any]] = None
    cost_price: Optional[float] = None
    shares: Optional[int] = None
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    enabled: Optional[bool] = None


class TrackerUpdateRequest(BaseModel):
    """触发追踪更新。"""

    codes: Optional[List[str]] = None
    date: Optional[str] = None
    group: Optional[str] = None
    strategy: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    notify: bool = True


class ReplayRequest(BaseModel):
    """历史回放。"""

    codes: Optional[List[str]] = None
    start: Optional[str] = None
    end: Optional[str] = None
    group: Optional[str] = None
    strategy: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    reset: bool = True
    notify: bool = False


class SetStateRequest(BaseModel):
    """手动设置状态。"""

    state: str
    reason: str = ""
    note: str = ""


class NoteRequest(BaseModel):
    """新增备注。"""

    content: str
    author: str = "user"


class ImportFromBacktestRequest(BaseModel):
    """从回测持仓导入关注池。"""

    codes: Optional[List[str]] = None
    group: str = "回测持仓"
    only_open: bool = True


# ----------------------------------------------------------------------
# 设置
# ----------------------------------------------------------------------
class SettingsUpdateRequest(BaseModel):
    """更新配置（深度合并）。"""

    patch: Dict[str, Any] = Field(default_factory=dict, description="局部配置字典")
    persist: bool = True


class NotifyTestRequest(BaseModel):
    """测试通知通道。"""

    channel: Optional[str] = Field(default=None, description="留空表示测试全部启用通道")


class StrategyParamsRequest(BaseModel):
    """保存策略默认参数。"""

    name: str
    params: Dict[str, Any] = Field(default_factory=dict)
