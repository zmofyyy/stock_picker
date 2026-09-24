"""通达信（TDX）本地数据读取模块。

核心原则：
    1. **默认优先使用 ``pytdx``** 的 ``TdxDailyBarReader`` / ``TdxMinBarReader``
       读取本地 ``.day`` / ``.lc5`` 文件，不使用任何在线行情接口；
    2. 仅当 ``pytdx`` 未安装或读取抛错时，才回退到本模块内置的二进制解析器
       （``_fallback_*`` 系列函数），并在日志中明确告警；
    3. 复权：通达信本地日线为 **不复权** 数据。若配置了 qfq/hfq 但没有可靠的
       除权除息数据源，会记录显式警告而 **不会** 静默返回错误结果。

文件布局::

    {tdx_dir}/vipdoc/sh/lday/sh600000.day
    {tdx_dir}/vipdoc/sz/lday/sz000001.day
    {tdx_dir}/vipdoc/bj/lday/bj830000.day
    {tdx_dir}/vipdoc/sh/minline/sh600000.lc1
    {tdx_dir}/vipdoc/sh/fzline/sh600000.lc5
"""

from __future__ import annotations

import contextlib
import io
import os
import struct
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..core.logging import get_logger

logger = get_logger("tdx_reader")

# ----------------------------------------------------------------------
# pytdx 可选导入
# ----------------------------------------------------------------------
_PYTDX_DAILY_READER = None
_PYTDX_MIN_READER = None
_PYTDX_IMPORT_ERROR: Optional[str] = None

try:  # pragma: no cover - 取决于运行环境
    from pytdx.reader import TdxDailyBarReader as _TdxDailyBarReader
    from pytdx.reader import TdxMinBarReader as _TdxMinBarReader

    _PYTDX_DAILY_READER = _TdxDailyBarReader
    _PYTDX_MIN_READER = _TdxMinBarReader
except Exception as exc:  # pragma: no cover
    _PYTDX_IMPORT_ERROR = str(exc)
    logger.warning(
        "pytdx 导入失败（%s），将使用内置备用解析器读取 .day 文件。"
        "生产环境请安装 pytdx：pip install pytdx",
        exc,
    )


def pytdx_available() -> bool:
    """返回 ``pytdx`` 是否可用。"""
    return _PYTDX_DAILY_READER is not None


def pytdx_status() -> Dict[str, object]:
    """返回 pytdx 的可用性信息，供 Web UI 展示。"""
    return {
        "available": pytdx_available(),
        "daily_reader": bool(_PYTDX_DAILY_READER),
        "min_reader": bool(_PYTDX_MIN_READER),
        "error": _PYTDX_IMPORT_ERROR,
    }


# ----------------------------------------------------------------------
# 常量与代码工具
# ----------------------------------------------------------------------
MARKETS = ("sh", "sz", "bj")

MARKET_NAMES = {"sh": "上海", "sz": "深圳", "bj": "北京"}

# 支持的周期别名 -> 通达信目录/后缀
MIN_PERIODS = {
    "1min": ("minline", ".lc1", 1),
    "5min": ("fzline", ".lc5", 5),
    "15min": ("fzline", ".lc5", 15),
    "30min": ("fzline", ".lc5", 30),
    "60min": ("fzline", ".lc5", 60),
}

DAILY_COLUMNS = ["datetime", "open", "high", "low", "close", "volume", "amount"]


def normalize_code(code: str) -> str:
    """把各种写法的股票代码统一为 ``600000.SH`` 形式。

    支持输入：``600000`` / ``600000.SH`` / ``sh600000`` / ``SH600000``。

    :param code: 原始代码
    :return: 标准代码，如 ``600000.SH``
    :raises ValueError: 代码无法识别时抛出
    """
    if not code:
        raise ValueError("股票代码不能为空")
    raw = str(code).strip().upper().replace(" ", "")

    if "." in raw:
        symbol, _, market = raw.partition(".")
        if market in ("SH", "SZ", "BJ") and symbol.isdigit():
            return f"{symbol}.{market}"
        raise ValueError(f"无法识别的股票代码：{code}")

    if raw[:2] in ("SH", "SZ", "BJ") and raw[2:].isdigit():
        return f"{raw[2:]}.{raw[:2]}"

    if raw.isdigit():
        return f"{raw}.{_guess_market(raw).upper()}"

    raise ValueError(f"无法识别的股票代码：{code}")


def _guess_market(symbol: str) -> str:
    """根据 6 位数字代码推断交易所。

    :param symbol: 6 位数字代码
    :return: ``sh`` / ``sz`` / ``bj``
    """
    if len(symbol) != 6:
        # 非 6 位（如港股 5 位）暂按深市处理
        return "sz"
    head1 = symbol[0]
    head2 = symbol[:2]
    head3 = symbol[:3]
    if head1 in ("6", "9", "5"):
        return "sh"
    if head1 in ("0", "1", "2", "3"):
        return "sz"
    if head1 in ("4", "8"):
        return "bj"
    if head3 == "920":  # 北交所新号段
        return "bj"
    if head2 in ("92",):
        return "bj"
    return "sz"


def split_code(code: str) -> Tuple[str, str]:
    """拆分标准代码为 ``(symbol, market)``。

    :param code: 标准代码或任意可识别写法
    :return: ``("600000", "sh")``
    """
    std = normalize_code(code)
    symbol, _, market = std.partition(".")
    return symbol, market.lower()


def code_from_filename(fname: str) -> Optional[str]:
    """从通达信文件名解析股票代码。

    :param fname: 例如 ``sh600000.day``
    :return: 标准代码，无法解析时返回 None
    """
    stem = Path(fname).stem.lower()
    if len(stem) < 8:
        return None
    market, symbol = stem[:2], stem[2:]
    if market not in MARKETS or not symbol.isdigit():
        return None
    return f"{symbol}.{market.upper()}"


def is_index_code(code: str) -> bool:
    """粗略判断是否为指数代码。

    上证指数 000001.SH、沪深300 000300.SH、深证成指 399001.SZ 等。
    """
    symbol, market = split_code(code)
    if market == "sh" and symbol.startswith("000"):
        return True
    if market == "sz" and symbol.startswith("399"):
        return True
    return False


def find_vipdoc_dir(tdx_dir: os.PathLike | str) -> Optional[Path]:
    """定位通达信的 ``vipdoc`` 目录。

    允许用户传入通达信安装根目录或直接传入 ``vipdoc`` 目录。

    :param tdx_dir: 通达信目录
    :return: ``vipdoc`` 目录 Path；找不到返回 None
    """
    if not tdx_dir:
        return None
    base = Path(os.path.expanduser(str(tdx_dir)))
    if not base.exists():
        return None

    direct = base / "vipdoc"
    if direct.is_dir():
        return direct

    # 传入的就是 vipdoc
    if base.name.lower() == "vipdoc" and any(
        (base / m).is_dir() for m in MARKETS
    ):
        return base

    # 某些版本会把 vipdoc 放在子目录下，做一层浅搜索
    for child in base.iterdir():
        if child.is_dir() and child.name.lower() == "vipdoc":
            return child
    return None


# ----------------------------------------------------------------------
# 内置备用解析器（仅在 pytdx 不可用时启用）
# ----------------------------------------------------------------------
_DAY_STRUCT = struct.Struct("<IIIIIfII")
# 分钟线：OHLC 为 int32（价格 ×100），amount 为 float32，volume 为 int32（单位：股）。
# 该布局与 pytdx.reader.TdxMinBarReader 完全一致，保证两条读取路径结果相同。
_MIN_STRUCT = struct.Struct("<HHIIIIfII")


def _parse_day_file_fallback(path: os.PathLike | str) -> pd.DataFrame:
    """解析通达信日线 ``.day`` 文件（备用实现）。

    每条记录 32 字节：
    ``date(uint32) open(uint32) high(uint32) low(uint32) close(uint32)
    amount(float32) volume(uint32) reserved(uint32)``，
    价格单位为「分」，需除以 100。

    :param path: ``.day`` 文件路径
    :return: 含 ``datetime/open/high/low/close/volume/amount`` 的 DataFrame
    """
    dates: List[int] = []
    opens: List[float] = []
    highs: List[float] = []
    lows: List[float] = []
    closes: List[float] = []
    amounts: List[float] = []
    volumes: List[float] = []

    with open(path, "rb") as fp:
        while True:
            buf = fp.read(32)
            if len(buf) < 32:
                break
            try:
                d, o, h, low, c, amount, vol, _ = _DAY_STRUCT.unpack(buf)
            except struct.error:  # pragma: no cover - 截断文件
                break
            if d <= 0:
                continue
            dates.append(d)
            opens.append(o / 100.0)
            highs.append(h / 100.0)
            lows.append(low / 100.0)
            closes.append(c / 100.0)
            amounts.append(float(amount))
            volumes.append(float(vol))

    if not dates:
        return _empty_bars()

    df = pd.DataFrame(
        {
            "datetime": pd.to_datetime([str(d) for d in dates], format="%Y%m%d"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "amount": amounts,
        }
    )
    return _finalize_bars(df)


def _parse_min_file_fallback(path: os.PathLike | str) -> pd.DataFrame:
    """解析通达信分钟线 ``.lc1`` / ``.lc5`` 文件（备用实现）。

    每条记录 32 字节：
    ``date(uint16) time(uint16) open(int32) high(int32) low(int32)
    close(int32) amount(float32) volume(int32) reserved(uint32)``；
    OHLC 为「价格 ×100」的整数，需除以 100 还原；
    日期编码 ``year = date // 2048 + 2004``。
    该布局与 ``pytdx.reader.TdxMinBarReader`` 保持一致。

    :param path: ``.lc1`` / ``.lc5`` 文件路径
    :return: 含标准列的时间序列 DataFrame
    """
    stamps: List[pd.Timestamp] = []
    rows: List[Tuple[float, float, float, float, float, float]] = []

    with open(path, "rb") as fp:
        while True:
            buf = fp.read(32)
            if len(buf) < 32:
                break
            try:
                packed_date, packed_time, o, h, low, c, amount, vol, _ = (
                    _MIN_STRUCT.unpack(buf)
                )
            except struct.error:  # pragma: no cover
                break
            year = packed_date // 2048 + 2004
            month = (packed_date % 2048) // 100
            day = packed_date % 2048 % 100
            hour = packed_time // 60
            minute = packed_time % 60
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue
            try:
                stamps.append(
                    pd.Timestamp(
                        year=year, month=month, day=day, hour=hour, minute=minute
                    )
                )
            except ValueError:
                continue
            rows.append(
                (o / 100.0, h / 100.0, low / 100.0, c / 100.0, float(amount), float(vol))
            )

    if not stamps:
        return _empty_bars()

    arr = np.asarray(rows, dtype="float64")
    df = pd.DataFrame(
        {
            "datetime": pd.DatetimeIndex(stamps),
            "open": arr[:, 0],
            "high": arr[:, 1],
            "low": arr[:, 2],
            "close": arr[:, 3],
            "amount": arr[:, 4],
            "volume": arr[:, 5],
        }
    )
    return _finalize_bars(df)


def _empty_bars() -> pd.DataFrame:
    """返回空的标准 K 线 DataFrame。"""
    df = pd.DataFrame({col: pd.Series(dtype="float64") for col in DAILY_COLUMNS})
    df["datetime"] = pd.Series(dtype="datetime64[ns]")
    return df


def _finalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """统一 K 线 DataFrame 的列、类型与排序。

    :param df: 原始 DataFrame
    :return: 列为 ``datetime/open/high/low/close/volume/amount``，按时间升序
    """
    if df is None or len(df) == 0:
        return _empty_bars()

    out = df.copy()
    if "datetime" not in out.columns:
        if "date" in out.columns:
            # pytdx 的 get_df_by_file 返回的列名为 date（不是 datetime）
            out = out.rename(columns={"date": "datetime"})
        else:
            # 兜底：把索引还原成列。索引名可能是 index / date / 自定义名称，
            # 因此直接按位置把第一列改名为 datetime。
            out = out.reset_index()
            first = out.columns[0]
            if first != "datetime":
                out = out.rename(columns={first: "datetime"})

    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col not in out.columns:
            out[col] = np.nan

    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["datetime"])
    out = out[DAILY_COLUMNS]
    for col in ("open", "high", "low", "close", "volume", "amount"):
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out.drop_duplicates(subset=["datetime"], keep="last")
    out = out.sort_values("datetime").reset_index(drop=True)
    return out


#: pytdx 日线 reader 遵循 zipline 约定，把文件中的成交量（单位：股）除以 100
#: 后返回（单位：手）；而 ``.day`` 文件的原始字段与内置解析器均为「股」。
#: 为保证两条读取路径量纲一致，日线需要放大 100 倍还原为「股」。
#: 注意：pytdx 的**分钟线** reader 不做这个换算（原样返回），因此不能套用此系数。
_PYTDX_VOLUME_SCALE = 100.0

#: pytdx 各 reader 的读取方法名（按可用性依次尝试）。
#: 日线 reader 用 ``get_df_by_file``，分钟线 reader 只有 ``get_df`` /
#: ``parse_data_by_file``，这里统一处理，保证「优先走 pytdx」。
_PYTDX_GETTER_METHODS: Tuple[str, ...] = (
    "get_df_by_file",
    "get_df",
    "parse_data_by_file",
    "parse_data_from_file",
)


def _from_pytdx(raw: pd.DataFrame, scale_volume: bool = True) -> pd.DataFrame:
    """把 pytdx 的返回结果转换为标准 K 线结构。

    pytdx 与内置解析器有两点差异，必须在此统一：

    1. 行索引名为 ``date``（不是 ``datetime``），且不在列中；
    2. **日线**的成交量被除以了 100（pytdx 返回「手」，文件与内置解析器是「股」），
       需要放大 100 倍还原。分钟线 reader 不做该换算，故由 ``scale_volume`` 控制。

    :param raw: pytdx reader 读取结果
    :param scale_volume: 是否需要把成交量从「手」还原为「股」（日线为 True）
    :return: 标准 K 线 DataFrame
    """
    df = _finalize_bars(raw)
    if scale_volume and len(df) and "volume" in df.columns:
        df = df.copy()
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * _PYTDX_VOLUME_SCALE
    return df


def _read_with_pytdx(
    reader: Any, path: Path, scale_volume: bool = True
) -> pd.DataFrame:
    """调用 pytdx 读取文件并标准化结果。

    pytdx 各 reader 的公开方法名并不统一，实测：

    - ``TdxDailyBarReader`` 提供 ``get_df_by_file``；
    - ``TdxMinBarReader`` **没有** ``get_df_by_file``，只有 ``get_df`` /
      ``parse_data_by_file``（历史上曾是不同的方法名）。

    因此这里按候选顺序逐个尝试，任一成功即返回，避免因方法名差异而
    静默退化成内置解析器（那样虽然结果一致，但不符合「优先使用 pytdx」的要求）。

    pytdx 遇到不支持的证券类型（如科创板 688、北交所）时会先向 stdout
    打印提示再抛 ``NotImplementedError``；这里屏蔽其输出，让日志保持整洁。

    :param reader: pytdx reader 实例
    :param path: 数据文件路径
    :param scale_volume: 日线为 True（pytdx 返回「手」，需还原为「股」），
        分钟线为 False（pytdx 原样返回「股」）
    :return: 标准 K 线 DataFrame
    :raises AttributeError: 所有候选方法都不可用时
    """
    getter_name = next(
        (name for name in _PYTDX_GETTER_METHODS if callable(getattr(reader, name, None))),
        None,
    )
    if getter_name is None:
        raise AttributeError(
            f"{type(reader).__name__} 不提供任何已知的读取方法：{_PYTDX_GETTER_METHODS}"
        )

    buf = io.StringIO()
    getter = getattr(reader, getter_name)
    with contextlib.redirect_stdout(buf):
        raw = getter(str(path))
    return _from_pytdx(raw, scale_volume=scale_volume)


# ----------------------------------------------------------------------
# 复权
# ----------------------------------------------------------------------
def apply_adjust(
    df: pd.DataFrame,
    factors: Optional[pd.Series] = None,
    mode: str = "none",
) -> pd.DataFrame:
    """对 K 线做复权处理。

    :param df: 标准 K 线 DataFrame
    :param factors: 复权因子序列（索引为日期，值为累计因子）；为 None 时无法复权
    :param mode: ``none`` / ``qfq`` / ``hfq``
    :return: 复权后的新 DataFrame（``none`` 或因子缺失时返回原数据副本）
    """
    if mode not in ("qfq", "hfq") or df is None or len(df) == 0:
        return df.copy()

    # 因子缺失：记录明确警告，返回原数据（绝不静默伪造）
    if factors is None or len(factors) == 0:
        logger.warning(
            "已请求 %s 复权，但本地通达信日线不含除权除息信息且未提供复权因子文件，"
            "本次返回的是【不复权】原始数据，请勿据此得出复权结论。",
            mode,
        )
        out = df.copy()
        out.attrs["adjust_applied"] = False
        out.attrs["adjust_warning"] = "缺少复权因子，返回不复权数据"
        return out

    out = df.copy()
    series = factors.reindex(pd.DatetimeIndex(out["datetime"])).ffill().bfill()
    ratio = series.to_numpy(dtype="float64")
    if mode == "qfq":
        # 前复权：以最新价格为基准，历史价格等比缩小
        denom = ratio[-1] if ratio[-1] else 1.0
        ratio = ratio / denom
    else:
        ratio = ratio / (ratio[0] if ratio[0] else 1.0)

    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * ratio

    out.attrs["adjust_applied"] = True
    out.attrs["adjust_mode"] = mode
    return out


def load_adjust_factors(path: os.PathLike | str) -> pd.DataFrame:
    """从 CSV 读取复权因子。

    CSV 格式（表头必须存在）::

        code,date,factor
        600000.SH,2020-06-15,1.02

    或除权除息数据::

        code,date,cash_dividend,split_ratio

    :param path: CSV 路径
    :return: 含 ``code/date/factor`` 的 DataFrame
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"复权因子文件不存在：{p}")
    raw = pd.read_csv(p)
    raw.columns = [c.strip().lower() for c in raw.columns]
    if "factor" in raw.columns and "date" in raw.columns:
        raw["date"] = pd.to_datetime(raw["date"])
        return raw

    # 由除权除息数据推导累计因子
    if {"date", "cash_dividend", "split_ratio"} <= set(raw.columns):
        raw["date"] = pd.to_datetime(raw["date"])
        raw["step"] = (1.0 + raw.get("split_ratio", 0.0)) / (
            1.0 + raw.get("cash_dividend", 0.0).fillna(0.0)
        )
        out = []
        for code, group in raw.sort_values("date").groupby("code"):
            g = group.copy()
            g["factor"] = g["step"].cumprod()
            out.append(g[["code", "date", "factor"]])
        return pd.concat(out, ignore_index=True) if out else raw

    raise ValueError(
        "复权文件格式不正确，需包含列 factor，或包含 cash_dividend+split_ratio"
    )


# ----------------------------------------------------------------------
# 数据质量检查结果
# ----------------------------------------------------------------------
@dataclass
class QualityIssue:
    """单条数据质量问题。"""

    code: str
    level: str  # info / warning / error
    message: str


@dataclass
class QualityReport:
    """数据质量检查汇总。"""

    total: int = 0
    ok: int = 0
    issues: List[QualityIssue] = field(default_factory=list)

    def add(self, code: str, level: str, message: str) -> None:
        self.issues.append(QualityIssue(code=code, level=level, message=message))

    @property
    def problem_codes(self) -> List[str]:
        """存在问题的股票代码列表。"""
        return sorted({i.code for i in self.issues})

    def to_dict(self) -> Dict[str, object]:
        """转换为可 JSON 序列化的字典。"""
        return {
            "total": self.total,
            "ok": self.ok,
            "problem_count": len(self.problem_codes),
            "issues": [
                {"code": i.code, "level": i.level, "message": i.message}
                for i in self.issues
            ],
        }


# ----------------------------------------------------------------------
# 读取器
# ----------------------------------------------------------------------
class TdxDataReader:
    """通达信本地数据读取器。

    :param tdx_dir: 通达信安装目录（或 vipdoc 目录）
    :param cache: 可选的 :class:`~app.data.cache.BarCache` 实例
    :param adjust: 复权模式 ``none`` / ``qfq`` / ``hfq``
    :param adjust_file: 复权因子 CSV 路径（可选）
    :param start_date: 数据起始日期过滤
    """

    def __init__(
        self,
        tdx_dir: os.PathLike | str,
        cache: Optional[object] = None,
        adjust: str = "none",
        adjust_file: Optional[os.PathLike | str] = None,
        start_date: Optional[str] = None,
    ) -> None:
        self.tdx_dir = Path(os.path.expanduser(str(tdx_dir))) if tdx_dir else None
        self.cache = cache
        self.adjust = (adjust or "none").lower()
        self.adjust_file = adjust_file
        self.start_date = start_date
        self._vipdoc: Optional[Path] = None
        self._adjust_cache: Optional[Dict[str, pd.Series]] = None
        self._adjust_warned = False

    # ------------------------------------------------------------------
    # 目录
    # ------------------------------------------------------------------
    @property
    def vipdoc(self) -> Optional[Path]:
        """惰性定位 ``vipdoc`` 目录。"""
        if self._vipdoc is None and self.tdx_dir is not None:
            self._vipdoc = find_vipdoc_dir(self.tdx_dir)
        return self._vipdoc

    def is_ready(self) -> bool:
        """通达信目录是否可用。"""
        return self.vipdoc is not None

    def status(self) -> Dict[str, object]:
        """返回数据源状态，供 API / UI 展示。"""
        vipdoc = self.vipdoc
        markets: Dict[str, int] = {}
        if vipdoc is not None:
            for market in MARKETS:
                day_dir = vipdoc / market / "lday"
                if day_dir.is_dir():
                    markets[market] = len(list(day_dir.glob("*.day")))
        return {
            "tdx_dir": str(self.tdx_dir) if self.tdx_dir else "",
            "vipdoc": str(vipdoc) if vipdoc else "",
            "ready": vipdoc is not None,
            "markets": markets,
            "total_files": sum(markets.values()),
            "adjust": self.adjust,
            "start_date": self.start_date,
            "pytdx": pytdx_status(),
        }

    def _day_dir(self, market: str) -> Optional[Path]:
        """返回某市场的日线目录。"""
        vipdoc = self.vipdoc
        if vipdoc is None:
            return None
        d = vipdoc / market / "lday"
        return d if d.is_dir() else None

    def _min_dir(self, market: str, freq: str) -> Optional[Path]:
        """返回某市场的分钟线目录。"""
        vipdoc = self.vipdoc
        if vipdoc is None or freq not in MIN_PERIODS:
            return None
        sub = MIN_PERIODS[freq][0]
        d = vipdoc / market / sub
        return d if d.is_dir() else None

    def daily_file(self, code: str) -> Optional[Path]:
        """定位某只股票的 ``.day`` 文件。

        :param code: 股票代码
        :return: 文件路径，不存在返回 None
        """
        symbol, market = split_code(code)
        d = self._day_dir(market)
        if d is None:
            return None
        path = d / f"{market}{symbol}.day"
        return path if path.is_file() else None

    # ------------------------------------------------------------------
    # 股票池
    # ------------------------------------------------------------------
    def scan_symbols(
        self,
        markets: Sequence[str] = MARKETS,
        include_index: bool = True,
        exclude_st: bool = False,
    ) -> List[str]:
        """扫描本地目录生成股票池。

        :param markets: 需要扫描的市场
        :param include_index: 是否包含指数文件（如 sh000300）
        :param exclude_st: 是否做粗略的 ST 过滤（按代码前缀，非精确）
        :return: 标准代码列表，按代码排序
        """
        vipdoc = self.vipdoc
        if vipdoc is None:
            logger.warning("通达信目录不可用，无法扫描股票池：%s", self.tdx_dir)
            return []

        codes: List[str] = []
        for market in markets:
            d = self._day_dir(market)
            if d is None:
                continue
            for f in d.glob("*.day"):
                code = code_from_filename(f.name)
                if code is None:
                    continue
                if not include_index and is_index_code(code):
                    continue
                if exclude_st and _is_st_like(code):
                    continue
                codes.append(code)
        codes = sorted(set(codes))
        logger.info("扫描到 %d 个标的（含指数=%s）", len(codes), include_index)
        return codes

    def list_stocks(self, include_index: bool = True, limit: Optional[int] = None) -> List[Dict[str, object]]:
        """返回股票池明细（代码、市场、文件大小、最后修改时间）。

        :param include_index: 是否包含指数
        :param limit: 最多返回多少条
        """
        vipdoc = self.vipdoc
        if vipdoc is None:
            return []
        rows: List[Dict[str, object]] = []
        for market in MARKETS:
            d = self._day_dir(market)
            if d is None:
                continue
            for f in d.glob("*.day"):
                code = code_from_filename(f.name)
                if code is None:
                    continue
                if not include_index and is_index_code(code):
                    continue
                stat = f.stat()
                rows.append(
                    {
                        "code": code,
                        "symbol": code.split(".")[0],
                        "market": market,
                        "market_name": MARKET_NAMES[market],
                        "is_index": is_index_code(code),
                        "file": f.name,
                        "size": stat.st_size,
                        "mtime": datetime.fromtimestamp(stat.st_mtime)
                        .replace(microsecond=0)
                        .isoformat(),
                    }
                )
        rows.sort(key=lambda r: r["code"])
        return rows[:limit] if limit else rows

    def last_data_date(self, code: str) -> Optional[str]:
        """返回某只股票本地数据的最后交易日（用于数据新鲜度提示）。"""
        df = self.read_daily(code, use_cache=True)
        if df is None or len(df) == 0:
            return None
        return pd.Timestamp(df["datetime"].iloc[-1]).strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def read_daily(
        self,
        code: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """读取单只股票的日线数据。

        :param code: 股票代码，任意可识别写法
        :param start: 起始日期 ``YYYY-MM-DD``（含）
        :param end: 结束日期 ``YYYY-MM-DD``（含）
        :param use_cache: 是否使用本地缓存
        :return: 标准 K 线 DataFrame；失败时返回空 DataFrame（不会抛异常）
        """
        std = normalize_code(code)
        path = self.daily_file(std)
        if path is None:
            logger.debug("未找到日线文件：%s", std)
            return _empty_bars()

        mtime = path.stat().st_mtime
        df: Optional[pd.DataFrame] = None

        if use_cache and self.cache is not None:
            try:
                if self.cache.is_fresh(std, "daily", mtime, adjust=self.adjust):
                    df = self.cache.load(std, "daily")
            except Exception as exc:  # pragma: no cover - 缓存损坏
                logger.warning("读取缓存失败（%s）：%s", std, exc)
                df = None

        if df is None or len(df) == 0:
            df = self._read_daily_raw(path, std)
            if df is None or len(df) == 0:
                return _empty_bars()
            if self.cache is not None and use_cache:
                try:
                    self.cache.save(std, "daily", df, mtime)
                except Exception as exc:  # pragma: no cover
                    logger.warning("写入缓存失败（%s）：%s", std, exc)

        df = self._apply_adjust_for(std, df)
        return self.filter_range(df, start=start, end=end)

    def _read_daily_raw(self, path: Path, code: str) -> pd.DataFrame:
        """读取原始日线文件（优先 pytdx，失败回退内置解析器）。"""
        if _PYTDX_DAILY_READER is not None:
            try:
                reader = _PYTDX_DAILY_READER()
                df = _read_with_pytdx(reader, path)
                if len(df) > 0:
                    return df
                logger.warning("pytdx 解析 %s 返回空数据，尝试备用解析器", path.name)
            except NotImplementedError:
                # pytdx 不支持科创板（688）/ 北交所等证券类型，属预期情况
                logger.debug("pytdx 不支持 %s 的证券类型，改用内置解析器", path.name)
            except Exception as exc:
                logger.warning(
                    "pytdx 读取 %s 失败（%s），回退到内置解析器", path.name, exc
                )
        try:
            return _parse_day_file_fallback(path)
        except Exception as exc:
            logger.error("解析日线文件失败 %s：%s", path, exc)
            return _empty_bars()

    def read_minute(
        self,
        code: str,
        freq: str = "5min",
        start: Optional[str] = None,
        end: Optional[str] = None,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """读取分钟线数据。

        :param code: 股票代码
        :param freq: ``1min`` / ``5min`` / ``15min`` / ``30min`` / ``60min``
        :param start: 起始日期
        :param end: 结束日期
        :param use_cache: 是否使用缓存
        :return: 标准 K 线 DataFrame
        """
        std = normalize_code(code)
        symbol, market = split_code(std)
        if freq not in MIN_PERIODS:
            logger.error("不支持的分钟周期：%s", freq)
            return _empty_bars()

        sub, suffix, _ = MIN_PERIODS[freq]
        d = self._min_dir(market, freq)
        if d is None:
            logger.debug("未找到 %s 的 %s 目录", market, sub)
            return _empty_bars()
        path = d / f"{market}{symbol}{suffix}"
        if not path.is_file():
            logger.debug("未找到分钟线文件：%s", path)
            return _empty_bars()

        mtime = path.stat().st_mtime
        key = freq
        df: Optional[pd.DataFrame] = None
        if use_cache and self.cache is not None:
            try:
                if self.cache.is_fresh(std, key, mtime, adjust="none"):
                    df = self.cache.load(std, key)
            except Exception:  # pragma: no cover
                df = None

        if df is None or len(df) == 0:
            df = self._read_min_raw(path)
            if df is None or len(df) == 0:
                return _empty_bars()
            # 分钟线周期聚合（本地 .lc5 就是 5 分钟，15/30/60 需重采样）
            df = resample_minute(df, freq)
            if self.cache is not None and use_cache:
                try:
                    self.cache.save(std, key, df, mtime)
                except Exception:  # pragma: no cover
                    pass

        return self.filter_range(df, start=start, end=end, intraday=True)

    def _read_min_raw(self, path: Path) -> pd.DataFrame:
        """读取原始分钟线文件（优先 pytdx，失败回退内置解析器）。"""
        if _PYTDX_MIN_READER is not None:
            try:
                reader = _PYTDX_MIN_READER()
                # 分钟线：pytdx 不做「手→股」换算，故 scale_volume=False
                df = _read_with_pytdx(reader, path, scale_volume=False)
                if len(df) > 0:
                    return df
            except NotImplementedError:
                logger.debug("pytdx 不支持 %s 的证券类型，改用内置解析器", path.name)
            except Exception as exc:
                logger.warning(
                    "pytdx 读取分钟线 %s 失败（%s），回退内置解析器", path.name, exc
                )
        try:
            return _parse_min_file_fallback(path)
        except Exception as exc:
            logger.error("解析分钟线失败 %s：%s", path, exc)
            return _empty_bars()

    def read_many(
        self,
        codes: Iterable[str],
        start: Optional[str] = None,
        end: Optional[str] = None,
        use_cache: bool = True,
        min_bars: int = 0,
        progress: bool = False,
    ) -> Dict[str, pd.DataFrame]:
        """批量读取多只股票日线。

        :param codes: 代码序列
        :param start: 起始日期
        :param end: 结束日期
        :param use_cache: 是否使用缓存
        :param min_bars: 过滤条件：数据长度小于该值的股票会被丢弃
        :param progress: 是否显示 tqdm 进度条
        :return: ``{code: DataFrame}``
        """
        codes = list(codes)
        iterator: Iterable[str] = codes
        if progress:
            try:
                from tqdm import tqdm

                iterator = tqdm(codes, desc="读取日线", ncols=80)
            except Exception:  # pragma: no cover - tqdm 缺失
                iterator = codes

        result: Dict[str, pd.DataFrame] = {}
        for code in iterator:
            try:
                df = self.read_daily(code, start=start, end=end, use_cache=use_cache)
            except Exception as exc:  # pragma: no cover - 单只股票异常不影响整体
                logger.error("读取 %s 失败：%s", code, exc)
                continue
            if df is None or len(df) == 0:
                continue
            if min_bars and len(df) < min_bars:
                continue
            result[normalize_code(code)] = df
        return result

    def read_index(
        self,
        code: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """读取指数日线（用于回测基准）。

        :param code: 指数代码，如 ``000300.SH``
        :return: 标准 K 线 DataFrame
        """
        return self.read_daily(code, start=start, end=end, use_cache=True)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _apply_adjust_for(self, code: str, df: pd.DataFrame) -> pd.DataFrame:
        """对指定股票应用复权。"""
        if self.adjust in ("none", "", None):
            return df
        factors = self._load_factors_for(code)
        if factors is None and not self._adjust_warned:
            self._adjust_warned = True
            logger.warning(
                "配置的复权模式为 %s，但未找到复权因子文件（data.adjust_file）。"
                "本地通达信 .day 为不复权数据，系统将返回原始价格。",
                self.adjust,
            )
        return apply_adjust(df, factors, self.adjust)

    def _load_factors_for(self, code: str) -> Optional[pd.Series]:
        """加载某只股票的复权因子序列。"""
        if not self.adjust_file:
            return None
        if self._adjust_cache is None:
            self._adjust_cache = {}
            try:
                raw = load_adjust_factors(self.adjust_file)
                for c, group in raw.groupby("code"):
                    s = pd.Series(
                        group["factor"].to_numpy(dtype="float64"),
                        index=pd.DatetimeIndex(group["date"]),
                    )
                    self._adjust_cache[normalize_code(str(c))] = s.sort_index()
            except Exception as exc:
                logger.error("加载复权因子失败：%s", exc)
                self._adjust_cache = {}
        return self._adjust_cache.get(code)

    @staticmethod
    def filter_range(
        df: pd.DataFrame,
        start: Optional[str] = None,
        end: Optional[str] = None,
        intraday: bool = False,
    ) -> pd.DataFrame:
        """按日期区间过滤 K 线（闭区间）。

        :param df: K 线数据
        :param start: 起始日期 ``YYYY-MM-DD``
        :param end: 结束日期 ``YYYY-MM-DD``；``intraday=True`` 时按当日 23:59 处理
        :param intraday: 是否为分钟级数据
        :return: 过滤后的 DataFrame
        """
        if df is None or len(df) == 0:
            return _empty_bars()
        out = df
        if start:
            s = pd.Timestamp(str(start))
            out = out[out["datetime"] >= s]
        if end:
            e = pd.Timestamp(str(end))
            if intraday:
                e = e + timedelta(days=1) - timedelta(seconds=1)
            out = out[out["datetime"] <= e]
        return out.reset_index(drop=True)

    # ------------------------------------------------------------------
    # 数据质量
    # ------------------------------------------------------------------
    def quality_check(
        self,
        codes: Optional[Iterable[str]] = None,
        max_codes: int = 200,
        expect_recent_days: int = 20,
    ) -> QualityReport:
        """对本地数据做基础质量检查。

        检查项：文件可读、行数是否充足、OHLC 是否存在非法值（<=0 或 high<low）、
        是否存在重复日期、数据是否过于陈旧。

        :param codes: 待检查代码；为空则自动扫描股票池
        :param max_codes: 最多检查多少只（避免全量扫描过慢）
        :param expect_recent_days: 最后一个交易日距今超过该交易日数视为陈旧
        :return: :class:`QualityReport`
        """
        report = QualityReport()
        targets = list(codes) if codes else self.scan_symbols(include_index=False)
        targets = targets[:max_codes]

        for code in targets:
            report.total += 1
            std = normalize_code(code)
            path = self.daily_file(std)
            if path is None:
                report.add(std, "error", "缺少 .day 文件")
                continue
            df = self.read_daily(std)
            if df is None or len(df) == 0:
                report.add(std, "error", "文件无法解析或数据为空")
                continue
            if len(df) < 60:
                report.add(std, "warning", f"历史数据偏少，仅 {len(df)} 根 K 线")
            bad = df[
                (df["open"] <= 0)
                | (df["high"] <= 0)
                | (df["low"] <= 0)
                | (df["close"] <= 0)
                | (df["high"] < df["low"])
            ]
            if len(bad) > 0:
                report.add(std, "warning", f"存在 {len(bad)} 条异常 OHLC 记录")
            if df["datetime"].duplicated().any():
                report.add(std, "warning", "存在重复交易日")
            if df["volume"].fillna(0).sum() <= 0:
                report.add(std, "warning", "成交量为 0，可能为长期停牌")
            last = pd.Timestamp(df["datetime"].iloc[-1])
            if (pd.Timestamp.today().normalize() - last).days > expect_recent_days * 2:
                report.add(
                    std,
                    "warning",
                    f"数据可能陈旧，最后交易日为 {last:%Y-%m-%d}",
                )
            if not any(i.code == std for i in report.issues):
                report.ok += 1
        return report


def resample_minute(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """把 5 分钟 K 线聚合为更大周期。

    :param df: 分钟 K 线（本地 ``.lc5`` 为 5 分钟）
    :param freq: 目标周期
    :return: 聚合后的 DataFrame
    """
    if df is None or len(df) == 0 or freq in ("1min", "5min"):
        return df
    minutes = {"15min": 15, "30min": 30, "60min": 60}.get(freq)
    if not minutes:
        return df

    out = df.set_index("datetime")
    agg = out.resample(f"{minutes}min", label="right", closed="right").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "amount": "sum",
        }
    )
    agg = agg.dropna(subset=["open", "close"]).reset_index()
    return _finalize_bars(agg)


def _is_st_like(code: str) -> bool:
    """按代码做粗略的 ST/风险股过滤。

    通达信日线文件名不含股票名称，无法精确判断 ST。
    这里仅过滤北交所的 4/8 开头较少参与的品种是错误做法，
    因此仅在用户显式开启 ``data.exclude_st`` 时用于剔除已知退市代码段。
    """
    symbol, _ = split_code(code)
    return symbol.startswith(("400", "420"))
