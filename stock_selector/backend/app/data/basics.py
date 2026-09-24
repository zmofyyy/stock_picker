"""股票基础信息（流通股本 / 流通市值）解析模块。

背景：
    通达信本地 ``.day`` 文件只包含行情，**不包含股本信息**；
    而「流通盘」类选股条件（例如「流通市值小于 150 亿」）必须知道流通股本。

设计（与 :mod:`app.data.names` 保持一致）：
    1. 读取用户提供的基础信息 CSV，默认 ``<cache_dir>/stock_basic.csv``，
       表头：``code,name,float_shares,total_shares,industry``；
    2. ``float_shares`` 单位统一为 **股**，同时兼容 ``"12.5亿"`` / ``"3500万"`` 等中文写法；
    3. 文件缺失时返回空映射，由上层（策略 / 选股器 / Web UI）明确提示用户，
       **绝不伪造股本数据**。

流通市值不需要额外维护：由「流通股本 × 当日收盘价」在策略中实时计算，
避免市值字段随时间过期导致选股口径错误。
"""

from __future__ import annotations

import csv
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import numpy as np
import pandas as pd

from ..core.logging import get_logger
from .tdx_reader import normalize_code

logger = get_logger("basics")

#: 内置模板文件的表头
_TEMPLATE_HEADER = ["code", "name", "float_shares", "total_shares", "industry"]

#: 单位后缀 → 倍数（长后缀在前，避免 "亿股" 被 "股" 抢先匹配）
_SHARE_UNITS = (
    ("亿股", 1e8),
    ("万股", 1e4),
    ("亿", 1e8),
    ("万", 1e4),
    ("股", 1.0),
)

#: 数据列在 ``float_shares`` 中的候选列名
_SHARE_FIELDS = ("float_shares", "floatshares", "circulating_shares", "流通股本", "流通股")
_NAME_FIELDS = ("name", "名称", "股票名称")
_TOTAL_FIELDS = ("total_shares", "totalshares", "总股本")
_INDUSTRY_FIELDS = ("industry", "行业")


def parse_shares(value: Any) -> Optional[float]:
    """把各种写法的股本解析为「股」。

    支持：``6570000000``、``"6570000000"``、``"65.7亿"``、``"65.7亿股"``、
    ``"3500万"``、``"1,234,000"``；无法识别或非正数时返回 ``None``。

    :param value: 原始值（数字或字符串）
    :return: 股本单位（float）或 None
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        number = float(value)
        if not np.isfinite(number) or number <= 0:
            return None
        return number

    text = str(value).strip()
    if not text:
        return None
    # 处理千分位与全角逗号
    text = text.replace(",", "").replace("，", "").replace(" ", "")

    scale = 1.0
    for suffix, factor in _SHARE_UNITS:
        if text.endswith(suffix):
            scale = factor
            text = text[: -len(suffix)]
            break
    try:
        number = float(text)
    except ValueError:
        return None
    if not np.isfinite(number) or number <= 0:
        return None
    return number * scale


@dataclass
class StockBasic:
    """单只股票的基础信息。"""

    code: str
    name: str = ""
    #: 流通股本（股）
    float_shares: Optional[float] = None
    #: 总股本（股）
    total_shares: Optional[float] = None
    industry: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return {
            "code": self.code,
            "name": self.name,
            "float_shares": self.float_shares,
            "float_shares_yi": (
                round(self.float_shares / 1e8, 4) if self.float_shares else None
            ),
            "total_shares": self.total_shares,
            "industry": self.industry,
        }


class StockBasicResolver:
    """股票基础信息解析器（线程安全，带内存缓存与 mtime 变更检测）。

    :param mapping_file: 基础信息 CSV 路径；为 None 时表示不可用
    """

    def __init__(self, mapping_file: Optional[Path | str] = None) -> None:
        self.mapping_file = Path(mapping_file) if mapping_file else None
        self._lock = threading.RLock()
        self._mapping: Dict[str, StockBasic] = {}
        self._loaded_mtime: Optional[float] = None
        self._loaded = False

    # ------------------------------------------------------------------
    # 加载 / 读取
    # ------------------------------------------------------------------
    def load(self, force: bool = False) -> Dict[str, StockBasic]:
        """加载基础信息文件（带 mtime 变更检测）。

        :param force: 强制重新读取
        :return: ``{标准代码: StockBasic}``
        """
        with self._lock:
            if self.mapping_file is None:
                self._loaded = True
                return self._mapping
            if not self.mapping_file.exists():
                self._loaded = True
                if not self._mapping:
                    logger.info(
                        "未找到股票基础信息文件 %s，流通盘相关策略将无法判断流通市值。"
                        "可调用 /api/data/basics/template 生成模板后用 /api/data/basics 上传。",
                        self.mapping_file,
                    )
                return self._mapping

            mtime = self.mapping_file.stat().st_mtime
            if self._loaded and not force and self._loaded_mtime == mtime:
                return self._mapping

            mapping: Dict[str, StockBasic] = {}
            try:
                with open(self.mapping_file, "r", encoding="utf-8-sig", newline="") as fp:
                    reader = csv.DictReader(fp)
                    if reader.fieldnames is None:
                        raise ValueError("空文件")
                    # 表头归一化：小写 + 去空白
                    raw_fields = [str(f).strip() for f in reader.fieldnames]
                    fields = [f.lower() for f in raw_fields]
                    code_key = _pick_field(fields, raw_fields, ("code", "代码", "证券代码"))
                    if code_key is None:
                        raise ValueError("缺少 code 表头")
                    share_key = _pick_field(fields, raw_fields, _SHARE_FIELDS)
                    name_key = _pick_field(fields, raw_fields, _NAME_FIELDS)
                    total_key = _pick_field(fields, raw_fields, _TOTAL_FIELDS)
                    industry_key = _pick_field(fields, raw_fields, _INDUSTRY_FIELDS)

                    for row in reader:
                        code_val = _row_value(row, raw_fields, code_key)
                        if not code_val:
                            continue
                        try:
                            std = normalize_code(code_val)
                        except ValueError:
                            continue
                        item = StockBasic(
                            code=std,
                            name=_row_value(row, raw_fields, name_key) or "",
                            float_shares=parse_shares(
                                _row_value(row, raw_fields, share_key)
                            ),
                            total_shares=parse_shares(
                                _row_value(row, raw_fields, total_key)
                            ),
                            industry=_row_value(row, raw_fields, industry_key) or "",
                        )
                        mapping[std] = item
            except Exception as exc:
                logger.error("读取股票基础信息文件失败：%s", exc)
                self._loaded = True
                return self._mapping

            self._mapping = mapping
            self._loaded_mtime = mtime
            self._loaded = True
            logger.info(
                "已加载 %d 条股票基础信息（其中有流通股本 %d 条）",
                len(mapping),
                sum(1 for v in mapping.values() if v.float_shares),
            )
            return self._mapping

    def get(self, code: str) -> Optional[StockBasic]:
        """返回某只股票的基础信息；未命中返回 None。"""
        try:
            std = normalize_code(code)
        except ValueError:
            return None
        if not self._loaded:
            self.load()
        return self._mapping.get(std)

    def float_shares(self, code: str) -> Optional[float]:
        """返回流通股本（股）；未命中返回 None。"""
        item = self.get(code)
        return item.float_shares if item else None

    def float_market_cap(self, code: str, price: float) -> Optional[float]:
        """返回流通市值（元）= 流通股本 × 价格；数据缺失时返回 None。"""
        shares = self.float_shares(code)
        if shares is None or price is None:
            return None
        if not np.isfinite(price) or price <= 0:
            return None
        return shares * float(price)

    # ------------------------------------------------------------------
    # 统计 / 写入
    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        """已加载的记录条数。"""
        if not self._loaded:
            self.load()
        return len(self._mapping)

    @property
    def available(self) -> bool:
        """基础信息文件是否存在。"""
        return bool(self.mapping_file and self.mapping_file.exists())

    def write_template(self, path: Optional[Path | str] = None) -> Path:
        """生成基础信息模板文件（含表头与示例行）。

        :param path: 目标路径，默认使用 ``self.mapping_file``
        :return: 实际写入路径
        """
        target = Path(path) if path else self.mapping_file
        if target is None:
            raise ValueError("未指定基础信息文件路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with open(target, "w", encoding="utf-8-sig", newline="") as fp:
                writer = csv.writer(fp)
                writer.writerow(_TEMPLATE_HEADER)
                # 示例行：写明单位，避免用户误填
                writer.writerow(["600000.SH", "浦发银行", "6570000000", "29352000000", "银行"])
                writer.writerow(["000001.SZ", "平安银行", "19400000000", "19406000000", "银行"])
        return target

    def upsert(self, items: Dict[str, Any]) -> int:
        """新增或更新基础信息并写回 CSV。

        :param items: 支持两种写法::

                {"600000.SH": {"float_shares": "65.7亿", "name": "浦发银行"}}
                {"600000.SH": 6570000000}

        :return: 更新后的总条数
        """
        if self.mapping_file is None:
            raise ValueError("未配置股票基础信息文件路径")
        with self._lock:
            self.load(force=True)
            merged: Dict[str, StockBasic] = dict(self._mapping)
            for code, payload in (items or {}).items():
                try:
                    std = normalize_code(code)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    name = str(payload.get("name") or "").strip()
                    shares = parse_shares(
                        payload.get("float_shares", payload.get("流通股本"))
                    )
                    total = parse_shares(payload.get("total_shares"))
                    industry = str(payload.get("industry") or "").strip()
                else:
                    name, industry = "", ""
                    shares = parse_shares(payload)
                    total = None

                current = merged.get(std) or StockBasic(code=std)
                merged[std] = StockBasic(
                    code=std,
                    name=name or current.name,
                    float_shares=shares if shares is not None else current.float_shares,
                    total_shares=total if total is not None else current.total_shares,
                    industry=industry or current.industry,
                )

            self.mapping_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.mapping_file, "w", encoding="utf-8-sig", newline="") as fp:
                writer = csv.writer(fp)
                writer.writerow(_TEMPLATE_HEADER)
                for code in sorted(merged):
                    item = merged[code]
                    writer.writerow(
                        [
                            item.code,
                            item.name,
                            "" if item.float_shares is None else int(round(item.float_shares)),
                            "" if item.total_shares is None else int(round(item.total_shares)),
                            item.industry,
                        ]
                    )
            self._mapping = merged
            self._loaded_mtime = self.mapping_file.stat().st_mtime
            self._loaded = True
            return len(merged)

    def to_list(self, keyword: str = "", limit: int = 500) -> list:
        """返回基础信息列表（供 Web UI 展示）。

        :param keyword: 代码/名称模糊过滤
        :param limit: 最多返回条数
        """
        data = self.load()
        rows: Iterable[StockBasic] = (data[k] for k in sorted(data))
        if keyword:
            key = keyword.strip().upper()
            rows = [
                r for r in rows
                if key in r.code.upper() or key in (r.name or "").upper()
            ]
        return [r.to_dict() for r in list(rows)[: max(int(limit), 0)]]


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _pick_field(
    fields: list, raw_fields: list, candidates: Iterable[str]
) -> Optional[str]:
    """在表头中找到第一个匹配的列名（返回原始列名）。"""
    for candidate in candidates:
        low = candidate.lower()
        if low in fields:
            return raw_fields[fields.index(low)]
    return None


def _row_value(row: Dict[str, Any], raw_fields: list, key: Optional[str]) -> str:
    """安全读取一行中某个列的值。"""
    if key is None:
        return ""
    value = row.get(key)
    if value is None:
        # csv.DictReader 对多余列会放到 None 键下，这里做一次兜底
        values = list(row.values())
        try:
            value = values[raw_fields.index(key)]
        except (ValueError, IndexError):
            return ""
    return str(value).strip()


# ----------------------------------------------------------------------
# 行情与基础信息的桥接
# ----------------------------------------------------------------------
def attach_float_shares(
    df: Optional[pd.DataFrame],
    code: str,
    resolver: Optional[StockBasicResolver],
) -> Optional[pd.DataFrame]:
    """把流通股本作为 ``float_shares`` 列附加到 K 线 DataFrame 上。

    策略只接收单只股票的 K 线，无法自行查询股本；由选股 / 回测 / 追踪
    在调用策略前统一注入，保证三处口径一致。

    :param df: 行情 DataFrame
    :param code: 股票代码
    :param resolver: 基础信息解析器；为 None 时原样返回
    :return: 带 ``float_shares`` 列的 DataFrame（未命中该股时为 NaN）
    """
    if df is None or len(df) == 0:
        return df
    if resolver is None or "float_shares" in df.columns:
        return df
    shares = resolver.float_shares(code)
    out = df.copy()
    out["float_shares"] = float(shares) if shares is not None else np.nan
    return out
