"""通达信本地日线读取层。

设计原则
--------
1. **pytdx 为主**：`TdxDailyBarReader` 是默认读取实现，`mode="pytdx"`。
2. **向量化加速**：全市场 9700+ 文件逐个走 pytdx 的 Python 循环需要约 60s，
   而 `.day` 是定长 32 字节记录、可被 numpy ``frombuffer`` 一次性向量化解析
   （实测 0.5s，快约 117 倍），因此提供 ``mode="fast"``。
   两条路径的字段布局与量纲已实测逐字段一致，`verify_readers()` 可随时复核。
3. **pytdx 覆盖不到的标的自动回退**：科创板 688 与北交所会抛
   ``Unknown security exchange``，此时无论哪种模式都由向量化解析器兜底。

量纲约定（已实测确认，勿改）
---------------------------
- `.day` 文件：价格 = int32 分（÷100 得元）；成交量 = uint32 **股**。
- pytdx 返回：价格已 ÷100（元）；成交量额外 ÷100（**手**），需 ×100 还原为股。
- 归一化后：``open/high/low/close`` 单位「元」，``volume`` 单位「股」，
  ``amount`` 单位「元」。
"""

from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------
# pytdx 可选导入
# ----------------------------------------------------------------------
try:  # pragma: no cover - 取决于运行环境
    from pytdx.reader import TdxDailyBarReader as _TdxDailyBarReader
except Exception:  # pragma: no cover
    _TdxDailyBarReader = None  # type: ignore[assignment]

MARKETS = ("sh", "sz", "bj")
MARKET_NAMES = {"sh": "上海", "sz": "深圳", "bj": "北京"}

#: `.day` 单条记录：date/open/high/low/close 为 uint32，amount 为 float32，
#: volume 为 uint32（单位：股）。与 pytdx.reader.TdxDailyBarReader 完全一致。
#:
#: 第 8 个 uint32 名义上是**上日收盘价（×100）**，但实测（2026-09-23，全市场抽样）
#: **自 2015-12-16 起全部失效**：沪/深约 63~67% 的行是占位值 ``65536``、约 10% 是
#: ``0``，有效值只存在于 2015 年及更早；北交所**从来没有**有效值。因此它对本项目
#: 关注的近期行情没有价值，**不进主缓存**（落列要多 68MB 常驻内存），只留
#: :func:`prev_close_from_raw` 供自检与研究使用。
#: 涨跌停判定因此一律用「前一根收盘」，见 :mod:`stock_picker.limits`。
DAY_DTYPE = np.dtype(
    [
        ("date", "<u4"),
        ("open", "<u4"),
        ("high", "<u4"),
        ("low", "<u4"),
        ("close", "<u4"),
        ("amount", "<f4"),
        ("volume", "<u4"),
        ("prev_close", "<u4"),
    ]
)

#: 上日收盘价的无效区间（「分」）。0 = 未回填；65536 = 北交所/近年的占位值。
PREV_CLOSE_INVALID = 65536

BARS_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]

#: 行情缓存的列 dtype。放开到全历史后全市场约 1700 万行，若沿用 float64 +
#: object 字符串会吃掉 2GB 以上，所以一律收缩。每一列都先核算过精度，不是随手降的：
#:
#: - ``code`` → category（int16 码）：5605 个类别只占 2 字节/行；换成 object
#:   字符串约 54 字节/行，单这一列就差 ~900MB。
#: - ``date`` → int32：YYYYMMDD 最大 8 位（8591 根的最长标的也远未溢出）。
#: - 价格 ``open/high/low/close`` → float32：源数据是「分」网格（0.01 元），
#:   实测最大收盘价 3275 元处 float32 的 ULP 仅 2.4e-4，round(x,2) 不会跳变。
#: - ``amount`` → float32：源本身就是 float32；最大成交额 1.41e11 元处 ULP
#:   16384 元，而展示口径是「亿」保留 3 位（1e5 元分辨率），差两个数量级。
#: - ``volume`` → **uint32**：源本来就是 uint32（单位「股」），属于无损存储。
#:   **绝不能降成 float32** —— 实测最大成交量 4.17e9 股处 float32 的 ULP 是
#:   256 股，会让前端「手」的显示值偏 1~2 手，与通达信对不上。
#: - ``pct`` → float32：相对量，相对误差 ~6e-8，展示到百分数 2 位无影响。
CACHE_DTYPES: Dict[str, str] = {
    "date": "int32",
    "open": "float32",
    "high": "float32",
    "low": "float32",
    "close": "float32",
    "volume": "uint32",
    "amount": "float32",
    "pct": "float32",
}


# ----------------------------------------------------------------------
# 代码工具
# ----------------------------------------------------------------------
def _guess_market(symbol: str) -> str:
    """按 6 位数字代码推断交易所。

    注意顺序：北交所新代码段 ``92xxxx``（如 ``920427``）必须以 ``92`` 前缀
    先判为 ``bj``，否则会被下面的「``9`` 开头 = 沪市」规则误判成 ``SH``。
    """
    if len(symbol) != 6:
        return "sz"
    head1, head2 = symbol[0], symbol[:2]
    if head2 == "92":
        return "bj"
    if head1 in ("6", "9", "5"):
        return "sh"
    if head1 in ("0", "1", "2", "3"):
        return "sz"
    if head1 in ("4", "8"):
        return "bj"
    return "sz"


def normalize_code(code: str) -> str:
    """把 ``600000`` / ``sh600000`` / ``600000.SH`` 统一为 ``600000.SH``。"""
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


def split_code(code: str) -> Tuple[str, str]:
    """返回 ``(symbol, market)``，market 为小写。"""
    std = normalize_code(code)
    symbol, _, market = std.partition(".")
    return symbol, market.lower()


def code_from_filename(fname: str) -> Optional[str]:
    """``sh600000.day`` -> ``600000.SH``。"""
    stem = Path(fname).stem.lower()
    if len(stem) < 8:
        return None
    market, symbol = stem[:2], stem[2:]
    if market not in MARKETS or not symbol.isdigit():
        return None
    return f"{symbol}.{market.upper()}"


def is_a_share(code: str) -> bool:
    """是否为 A 股正股（剔除指数、基金、可转债、B 股等）。"""
    symbol, market = split_code(code)
    if market == "sh":
        return symbol.startswith(("600", "601", "603", "605", "688", "689"))
    if market == "sz":
        return symbol.startswith(("000", "001", "002", "003", "300", "301"))
    if market == "bj":
        return symbol.startswith(("43", "83", "87", "88", "92"))
    return False


def board_of(code: str) -> str:
    """板块名称。"""
    symbol, market = split_code(code)
    if market == "bj":
        return "北交所"
    if symbol.startswith("688"):
        return "科创板"
    if symbol.startswith(("300", "301")):
        return "创业板"
    return "主板"


def find_vipdoc_dir(tdx_dir: os.PathLike | str) -> Optional[Path]:
    """定位 ``vipdoc`` 目录（允许传入通达信根目录或 vipdoc 本身）。"""
    if not tdx_dir:
        return None
    base = Path(os.path.expanduser(str(tdx_dir)))
    if not base.exists():
        return None
    direct = base / "vipdoc"
    if direct.is_dir():
        return direct
    if base.name.lower() == "vipdoc" and any((base / m).is_dir() for m in MARKETS):
        return base
    for child in base.iterdir():
        if child.is_dir() and child.name.lower() == "vipdoc":
            return child
    return None


# ----------------------------------------------------------------------
# 读取实现
# ----------------------------------------------------------------------
def _empty_bars() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="float64") for c in BARS_COLUMNS})
    df["date"] = pd.Series(dtype="int64")
    return df


def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    """排序、去重、统一列顺序与类型。"""
    if df is None or len(df) == 0:
        return _empty_bars()
    out = df[BARS_COLUMNS].copy()
    out["date"] = pd.to_numeric(out["date"], errors="coerce").astype("int64")
    for c in ("open", "high", "low", "close", "volume", "amount"):
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    out = out[out["date"] > 0]
    out = out.drop_duplicates(subset=["date"], keep="last")
    return out.sort_values("date").reset_index(drop=True)


def prev_close_from_raw(raw: np.ndarray) -> np.ndarray:
    """把 `.day` 的「上日收盘（分）」字段转成「元」，无效值给 NaN。

    **只用于自检与研究**，不进缓存 —— 该字段自 2015-12-16 起已全部失效，
    详见 :data:`DAY_DTYPE` 的说明。无效的两种情况：``0``（未回填）与
    ``≥65536``（占位值）。
    """
    pc = np.asarray(raw, dtype="int64")
    with np.errstate(invalid="ignore"):
        ok = (pc > 0) & (pc < PREV_CLOSE_INVALID)
    return np.where(ok, pc.astype("float64") / 100.0, np.nan)


def read_day_fast(path: os.PathLike | str, tail: Optional[int] = None) -> pd.DataFrame:
    """向量化解析 `.day` 文件（``mode="fast"``）。

    :param path: 文件路径
    :param tail: 只保留最后 N 根 K 线；None 表示全部
    """
    raw = Path(path).read_bytes()
    n = len(raw) // 32
    if n == 0:
        return _empty_bars()
    arr = np.frombuffer(raw[: n * 32], dtype=DAY_DTYPE)
    if tail is not None and n > tail:
        arr = arr[-tail:]

    df = pd.DataFrame(
        {
            "date": arr["date"].astype("int64"),
            "open": arr["open"].astype("float64") / 100.0,
            "high": arr["high"].astype("float64") / 100.0,
            "low": arr["low"].astype("float64") / 100.0,
            "close": arr["close"].astype("float64") / 100.0,
            "volume": arr["volume"].astype("float64"),
            "amount": arr["amount"].astype("float64"),
        }
    )
    return _finalize(df)


def read_day_pytdx(path: os.PathLike | str, tail: Optional[int] = None) -> pd.DataFrame:
    """用 pytdx 的 ``TdxDailyBarReader`` 解析 `.day` 文件（``mode="pytdx"``）。

    注意两处与文件原始值的差异，必须在此归一化：

    1. 行索引名是 ``date``（不是 ``datetime``），且不在列中；
    2. 成交量被额外 ÷100（返回「手」），需 ×100 还原为「股」。

    :raises RuntimeError: pytdx 不可用或该标的类型不受支持
    """
    if _TdxDailyBarReader is None:
        raise RuntimeError("pytdx 不可用，请先安装：pip install pytdx")

    reader = _TdxDailyBarReader()
    # pytdx 遇到不支持的证券类型会先向 stdout 打印提示再抛异常，这里屏蔽其输出
    with contextlib.redirect_stdout(io.StringIO()):
        raw = reader.get_df_by_file(str(path))
    if raw is None or len(raw) == 0:
        return _empty_bars()

    df = raw.reset_index()
    first = df.columns[0]
    if first != "date":
        df = df.rename(columns={first: "date"})
    df["date"] = pd.to_numeric(df["date"], errors="coerce")
    # datetime -> YYYYMMDD 整数
    df["date"] = (
        pd.to_datetime(df["date"]).dt.strftime("%Y%m%d").astype("int64")
    )
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100.0  # 手 -> 股
    for c in ("open", "high", "low", "close", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = _finalize(df)
    if tail is not None and len(df) > tail:
        df = df.tail(tail).reset_index(drop=True)
    return df


@dataclass
class ReadStats:
    """一次批量读取的统计。"""

    total: int = 0
    by_mode: Dict[str, int] = None  # type: ignore[assignment]
    failed: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.by_mode = self.by_mode or {}
        self.failed = self.failed or []


class TdxReader:
    """通达信日线读取器。

    :param tdx_dir: 通达信安装目录或 vipdoc 目录
    :param mode: ``pytdx`` 走 pytdx；``fast`` 走向量化解析；``auto`` 先 pytdx 失败再 fast
    """

    def __init__(self, tdx_dir: os.PathLike | str, mode: str = "fast") -> None:
        self.tdx_dir = Path(os.path.expanduser(str(tdx_dir))) if tdx_dir else None
        self.mode = (mode or "fast").lower()
        self._vipdoc: Optional[Path] = None

    # ------------------------------------------------------------------
    @property
    def vipdoc(self) -> Optional[Path]:
        if self._vipdoc is None and self.tdx_dir is not None:
            self._vipdoc = find_vipdoc_dir(self.tdx_dir)
        return self._vipdoc

    def is_ready(self) -> bool:
        return self.vipdoc is not None

    def daily_dir(self, market: str) -> Optional[Path]:
        vipdoc = self.vipdoc
        if vipdoc is None:
            return None
        d = vipdoc / market / "lday"
        return d if d.is_dir() else None

    def daily_file(self, code: str) -> Optional[Path]:
        symbol, market = split_code(code)
        d = self.daily_dir(market)
        if d is None:
            return None
        p = d / f"{market}{symbol}.day"
        return p if p.is_file() else None

    def list_codes(self, a_share_only: bool = True) -> List[str]:
        """扫描本地目录得到股票池。"""
        vipdoc = self.vipdoc
        if vipdoc is None:
            return []
        codes: List[str] = []
        for market in MARKETS:
            d = self.daily_dir(market)
            if d is None:
                continue
            for f in d.glob("*.day"):
                code = code_from_filename(f.name)
                if code is None:
                    continue
                if a_share_only and not is_a_share(code):
                    continue
                codes.append(code)
        return sorted(set(codes))

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for market in MARKETS:
            d = self.daily_dir(market)
            out[market] = len(list(d.glob("*.day"))) if d else 0
        return out

    # ------------------------------------------------------------------
    def read_daily(self, code: str, tail: Optional[int] = None) -> Tuple[pd.DataFrame, str]:
        """读取单只股票日线。

        :return: ``(DataFrame, 实际使用的读取路径)``；路径取值为
            ``pytdx`` / ``fast`` / ``none``
        """
        path = self.daily_file(code)
        if path is None:
            return _empty_bars(), "none"

        if self.mode == "pytdx":
            try:
                df = read_day_pytdx(path, tail=tail)
                if len(df):
                    return df, "pytdx"
            except Exception:
                # 688 / 北交所等 pytdx 不支持的标的，落到向量化解析器
                return read_day_fast(path, tail=tail), "fast"
            return _empty_bars(), "none"

        if self.mode == "auto":
            try:
                df = read_day_pytdx(path, tail=tail)
                if len(df):
                    return df, "pytdx"
            except Exception:
                pass
            return read_day_fast(path, tail=tail), "fast"

        return read_day_fast(path, tail=tail), "fast"

    def read_many_bars(
        self,
        codes: Sequence[str],
        tail: Optional[int] = None,
        progress=None,
    ) -> Tuple[pd.DataFrame, ReadStats]:
        """批量读取并拼成长表（``code`` + OHLCV 列）。

        返回的长表**已按 ``(code, date)`` 升序**：``codes`` 自身有序，且每只
        内部按日期排序，所以 ``concat`` 完就是有序的，调用方不必再排。
        列 dtype 见 :data:`CACHE_DTYPES`（已收缩，勿当 float64 用）。

        :param tail: 每只股票保留最后 N 根 K 线；None = 读入全部历史
        :param progress: 可选回调 ``fn(done, total, code)``
        """
        stats = ReadStats(total=len(codes))
        cats = sorted({normalize_code(c) for c in codes})
        code_dtype = pd.CategoricalDtype(categories=cats, ordered=False)
        code_to_idx = {c: i for i, c in enumerate(cats)}
        # 5605 < 32767，用 int16 存类别码就够；超过则退回 int32
        code_bits = "int16" if len(cats) <= 32767 else "int32"

        frames: List[pd.DataFrame] = []
        for i, code in enumerate(codes):
            std = normalize_code(code)
            try:
                df, used = self.read_daily(std, tail=tail)
            except Exception:
                stats.failed.append(code)
                continue
            if df is None or len(df) == 0:
                stats.failed.append(code)
                continue
            stats.by_mode[used] = stats.by_mode.get(used, 0) + 1
            cols = {k: v for k, v in CACHE_DTYPES.items() if k in df.columns}
            if "volume" in cols:
                # astype('uint32') 遇到 NaN 会直接抛，先兜一下
                df = df.copy()
                df["volume"] = df["volume"].fillna(0.0)
            df = df.astype(cols)
            # 关键：所有 frame 共用同一个 CategoricalDtype，concat 后仍是 2 字节/行。
            # 若先拼成 object 字符串再转 category，中间那步会多占 ~900MB。
            df.insert(
                0,
                "code",
                pd.Categorical.from_codes(
                    np.full(len(df), code_to_idx[std], dtype=code_bits),
                    dtype=code_dtype,
                ),
            )
            frames.append(df)
            if progress is not None and (i % 200 == 0 or i == len(codes) - 1):
                progress(i + 1, len(codes), std)
        if not frames:
            return pd.DataFrame(), stats
        return pd.concat(frames, ignore_index=True), stats


def verify_readers(
    reader: TdxReader, sample: int = 24, seed: int = 7
) -> Dict[str, object]:
    """逐字段比对 ``pytdx`` 与 ``fast`` 两条读取路径。

    用于证明向量化加速路径与 pytdx 结果一致（价格、成交量、成交额、日期）。

    :return: 含 ``checked`` / ``mismatches`` / ``details`` 的字典
    """
    codes = reader.list_codes(a_share_only=True)
    if not codes:
        return {"ok": False, "reason": "本地没有可读取的标的", "checked": 0, "mismatches": []}

    rng = np.random.default_rng(seed)
    idx = rng.choice(len(codes), size=min(sample, len(codes)), replace=False)
    picked = [codes[int(i)] for i in idx]

    mismatches: List[Dict[str, object]] = []
    checked = 0
    for code in picked:
        path = reader.daily_file(code)
        if path is None:
            continue
        try:
            a = read_day_pytdx(path)
        except Exception:
            # pytdx 不支持的标的（688 / 北交所）无法比对，跳过
            continue
        b = read_day_fast(path)
        if len(a) != len(b):
            mismatches.append({"code": code, "reason": f"行数不一致 {len(a)} vs {len(b)}"})
            continue
        checked += 1
        if len(a) == 0:
            continue
        diffs = {}
        if not np.array_equal(a["date"].to_numpy(), b["date"].to_numpy()):
            diffs["date"] = "日期序列不一致"
        for col in ("open", "high", "low", "close", "volume", "amount"):
            va = a[col].to_numpy(dtype="float64")
            vb = b[col].to_numpy(dtype="float64")
            scale = np.maximum(np.abs(va), 1.0)
            bad = np.abs(va - vb) > np.maximum(scale * 1e-9, 1e-6)
            if bad.any():
                k = int(np.argmax(bad))
                diffs[col] = {"rows": int(bad.sum()), "first": [float(va[k]), float(vb[k])]}
        if diffs:
            mismatches.append({"code": code, "reason": diffs})

    return {
        "ok": len(mismatches) == 0,
        "mode": reader.mode,
        "checked": checked,
        "requested": len(picked),
        "mismatches": mismatches,
    }
