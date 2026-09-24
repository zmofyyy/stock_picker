"""通达信本地数据读取测试。

覆盖：
    * ``.day`` 二进制格式解析（内置备用解析器）
    * 通过 :class:`TdxDataReader` 读取（优先 pytdx，不可用则回退）
    * 代码标准化、股票池扫描、日期区间过滤
    * 分钟线（``.lc5``）读取与周期聚合
    * 缓存写入 / 读取 / 失效判定
    * 数据质量检查
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.app.data.cache import BarCache
from backend.app.data.tdx_reader import (
    TdxDataReader,
    _finalize_bars,
    _from_pytdx,
    _parse_day_file_fallback,
    _parse_min_file_fallback,
    code_from_filename,
    find_vipdoc_dir,
    is_index_code,
    normalize_code,
    pytdx_status,
    resample_minute,
    split_code,
)
from tests.helpers import (
    build_minute_tree,
    build_tdx_tree,
    make_bars,
    trading_days,
    write_day_file,
)

# ----------------------------------------------------------------------
# 代码工具
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("600000", "600000.SH"),
        ("600000.SH", "600000.SH"),
        ("sh600000", "600000.SH"),
        ("SH600000", "600000.SH"),
        ("000001", "000001.SZ"),
        ("300750", "300750.SZ"),
        ("688981", "688981.SH"),
        ("830000", "830000.BJ"),
        ("430047", "430047.BJ"),
    ],
)
def test_normalize_code(raw: str, expected: str) -> None:
    """代码标准化应覆盖常见写法。"""
    assert normalize_code(raw) == expected


def test_normalize_code_invalid() -> None:
    """非法代码应抛出 ValueError。"""
    with pytest.raises(ValueError):
        normalize_code("ABCDEF")
    with pytest.raises(ValueError):
        normalize_code("")


def test_split_code() -> None:
    """代码拆分。"""
    assert split_code("600000.SH") == ("600000", "sh")
    assert split_code("000001") == ("000001", "sz")


def test_code_from_filename() -> None:
    """文件名解析。"""
    assert code_from_filename("sh600000.day") == "600000.SH"
    assert code_from_filename("sz000001.day") == "000001.SZ"
    assert code_from_filename("bj830000.day") == "830000.BJ"
    assert code_from_filename("README.txt") is None


def test_is_index_code() -> None:
    """指数识别。"""
    assert is_index_code("000300.SH")
    assert is_index_code("399001.SZ")
    assert not is_index_code("600000.SH")


def test_find_vipdoc_dir(tdx_root: Path) -> None:
    """vipdoc 目录定位（支持传入根目录或其本身）。"""
    assert find_vipdoc_dir(tdx_root) == tdx_root / "vipdoc"
    assert find_vipdoc_dir(tdx_root / "vipdoc") == tdx_root / "vipdoc"
    assert find_vipdoc_dir(tdx_root / "not-exists") is None


# ----------------------------------------------------------------------
# .day 解析
# ----------------------------------------------------------------------
def test_parse_day_file_fallback(tmp_path: Path) -> None:
    """内置解析器应精确还原写入的数值。"""
    dates = trading_days("2023-01-02", 25)
    bars = make_bars(dates, base_price=10.0, seed=11)
    path = tmp_path / "sh600000.day"
    write_day_file(path, bars)

    df = _parse_day_file_fallback(path)
    assert len(df) == len(bars)
    assert list(df.columns) == ["datetime", "open", "high", "low", "close", "volume", "amount"]
    assert df["datetime"].is_monotonic_increasing

    first = bars[0]
    assert df["open"].iloc[0] == pytest.approx(first["open"], abs=1e-6)
    assert df["high"].iloc[0] == pytest.approx(first["high"], abs=1e-6)
    assert df["low"].iloc[0] == pytest.approx(first["low"], abs=1e-6)
    assert df["close"].iloc[0] == pytest.approx(first["close"], abs=1e-6)
    assert df["volume"].iloc[0] == pytest.approx(first["volume"], abs=1.0)
    assert str(df["datetime"].iloc[0].date()) == str(first["date"])


def test_parse_day_file_empty(tmp_path: Path) -> None:
    """空文件应返回空 DataFrame 而不是抛异常。"""
    path = tmp_path / "sh600000.day"
    path.write_bytes(b"")
    df = _parse_day_file_fallback(path)
    assert len(df) == 0
    assert "close" in df.columns


def test_parse_day_file_truncated(tmp_path: Path) -> None:
    """截断文件应安全退出（不抛出 struct.error）。"""
    dates = trading_days("2023-01-02", 10)
    bars = make_bars(dates, seed=3)
    path = tmp_path / "sh600000.day"
    write_day_file(path, bars)
    raw = path.read_bytes()
    path.write_bytes(raw[: 32 * 5 + 7])  # 最后一条记录不完整

    df = _parse_day_file_fallback(path)
    assert len(df) == 5


# ----------------------------------------------------------------------
# 读取器
# ----------------------------------------------------------------------
def test_reader_status(tdx_root: Path) -> None:
    """读取器状态应能识别目录与文件数。"""
    reader = TdxDataReader(tdx_root)
    status = reader.status()
    assert status["ready"] is True
    assert status["total_files"] >= 5
    assert status["markets"]["sh"] >= 1
    assert "pytdx" in status


def test_reader_read_daily(tdx_root: Path) -> None:
    """读取单只股票日线，价格应与原始写入一致。"""
    reader = TdxDataReader(tdx_root)
    df = reader.read_daily("600000.SH")
    assert len(df) == 260
    assert df["datetime"].is_monotonic_increasing
    assert (df["high"] >= df["low"]).all()
    assert (df["close"] > 0).all()
    assert df["volume"].sum() > 0


def test_reader_pytdx_or_fallback_consistency(tdx_root: Path) -> None:
    """无论走 pytdx 还是内置解析器，OHLC 都应一致。"""
    reader = TdxDataReader(tdx_root)
    path = reader.daily_file("600000.SH")
    assert path is not None

    from_pytdx_path = reader._read_daily_raw(path, "600000.SH")
    from_fallback = _parse_day_file_fallback(path)

    assert len(from_pytdx_path) == len(from_fallback)
    for col in ("open", "high", "low", "close"):
        assert from_pytdx_path[col].to_numpy() == pytest.approx(
            from_fallback[col].to_numpy(), abs=1e-6
        )
    assert pytdx_status()["available"] in (True, False)  # 记录当前环境


def test_reader_date_filter(tdx_root: Path) -> None:
    """日期区间过滤为闭区间。"""
    reader = TdxDataReader(tdx_root)
    full = reader.read_daily("600000.SH")
    start = full["datetime"].iloc[10].strftime("%Y-%m-%d")
    end = full["datetime"].iloc[30].strftime("%Y-%m-%d")
    part = reader.read_daily("600000.SH", start=start, end=end)
    assert len(part) == 21
    assert part["datetime"].iloc[0].strftime("%Y-%m-%d") == start
    assert part["datetime"].iloc[-1].strftime("%Y-%m-%d") == end


def test_reader_missing_file(tdx_root: Path) -> None:
    """不存在的股票应返回空 DataFrame。"""
    reader = TdxDataReader(tdx_root)
    df = reader.read_daily("601999.SH")
    assert len(df) == 0


def test_reader_invalid_dir(tmp_path: Path) -> None:
    """无效目录不应崩溃。"""
    reader = TdxDataReader(tmp_path / "nothing")
    assert reader.is_ready() is False
    assert reader.scan_symbols() == []
    assert len(reader.read_daily("600000.SH")) == 0


def test_scan_symbols(tdx_root: Path) -> None:
    """股票池扫描。"""
    reader = TdxDataReader(tdx_root)
    codes = reader.scan_symbols(include_index=False)
    assert "600000.SH" in codes
    assert "000001.SZ" in codes
    assert "300750.SZ" in codes
    assert "830000.BJ" in codes
    assert "000300.SH" not in codes

    with_index = reader.scan_symbols(include_index=True)
    assert "000300.SH" in with_index


def test_list_stocks(tdx_root: Path) -> None:
    """股票池明细包含市场、文件大小等信息。"""
    reader = TdxDataReader(tdx_root)
    rows = reader.list_stocks(include_index=False)
    assert len(rows) == 4
    row = next(r for r in rows if r["code"] == "600000.SH")
    assert row["market"] == "sh"
    assert row["file"] == "sh600000.day"
    assert row["size"] > 0


def test_read_many(tdx_root: Path) -> None:
    """批量读取。"""
    reader = TdxDataReader(tdx_root)
    data = reader.read_many(["600000.SH", "000001.SZ"], min_bars=100)
    assert set(data) == {"600000.SH", "000001.SZ"}
    assert len(reader.read_many(["600000.SH"], min_bars=99999)) == 0


def test_read_index(tdx_root: Path) -> None:
    """读取指数数据（回测基准）。"""
    reader = TdxDataReader(tdx_root)
    df = reader.read_index("000300.SH")
    assert len(df) == 260
    assert df["close"].iloc[0] > 1000


# ----------------------------------------------------------------------
# 分钟线
# ----------------------------------------------------------------------
def test_parse_min_file_fallback(tmp_path: Path) -> None:
    """``.lc5`` 解析。"""
    path = tmp_path / "sh600000.lc5"
    build_minute_tree(tmp_path, symbol="600000", days=3)
    # build_minute_tree 写出到 tmp_path/vipdoc/sh/fzline/
    real = tmp_path / "vipdoc" / "sh" / "fzline" / "sh600000.lc5"
    df = _parse_min_file_fallback(real)
    assert len(df) == 3 * 48
    assert df["datetime"].is_monotonic_increasing
    assert (df["close"] > 0).all()


def test_reader_read_minute(tdx_root: Path) -> None:
    """通过读取器读取 5 分钟线。"""
    reader = TdxDataReader(tdx_root)
    df = reader.read_minute("600000.SH", freq="5min")
    assert len(df) == 5 * 48
    assert "datetime" in df.columns


def test_resample_minute(tdx_root: Path) -> None:
    """5 分钟 → 30 分钟聚合后根数应减少约 6 倍。"""
    reader = TdxDataReader(tdx_root)
    five = reader.read_minute("600000.SH", freq="5min")
    tmp = resample_minute(five, "30min")
    assert 0 < len(tmp) < len(five)
    assert (tmp["high"] >= tmp["low"]).all()


# ----------------------------------------------------------------------
# 缓存
# ----------------------------------------------------------------------
def test_cache_roundtrip(tmp_path: Path) -> None:
    """缓存写入 / 读取 / 新鲜度。"""
    cache = BarCache(tmp_path / "cache", fmt="pickle")
    dates = trading_days("2023-01-02", 30)
    df = _parse_day_file_fallback(write_day_file(tmp_path / "x.day", make_bars(dates, seed=5)))

    cache.save("600000.SH", "daily", df, source_mtime=1000.0, adjust="none")
    assert cache.is_fresh("600000.SH", "daily", source_mtime=1000.0, adjust="none")
    assert not cache.is_fresh("600000.SH", "daily", source_mtime=2000.0, adjust="none")
    assert not cache.is_fresh("600000.SH", "daily", source_mtime=1000.0, adjust="qfq")

    loaded = cache.load("600000.SH", "daily")
    assert loaded is not None
    assert len(loaded) == len(df)
    assert loaded["close"].iloc[-1] == pytest.approx(df["close"].iloc[-1])

    info = cache.info()
    assert info["count"] >= 1
    assert info["entries"][0]["code"] == "600000.SH"

    assert cache.remove("600000.SH") >= 1
    assert cache.load("600000.SH", "daily") is None


def test_cache_clear(tmp_path: Path) -> None:
    """清空缓存。"""
    cache = BarCache(tmp_path / "cache", fmt="pickle")
    dates = trading_days("2023-01-02", 10)
    df = _parse_day_file_fallback(write_day_file(tmp_path / "y.day", make_bars(dates, seed=6)))
    cache.save("000001.SZ", "daily", df, source_mtime=1.0)
    assert cache.info()["count"] == 1
    cache.clear()
    assert cache.info()["count"] == 0


def test_reader_uses_cache(tdx_root: Path, tmp_path: Path) -> None:
    """读取器第二次读取应命中缓存且结果一致。"""
    cache = BarCache(tmp_path / "c", fmt="pickle")
    reader = TdxDataReader(tdx_root, cache=cache)
    first = reader.read_daily("600000.SH")
    assert cache.info()["count"] == 1
    second = reader.read_daily("600000.SH")
    pd.testing.assert_frame_equal(first, second)


# ----------------------------------------------------------------------
# 质量检查
# ----------------------------------------------------------------------
def test_quality_check(tdx_root: Path) -> None:
    """质量检查应返回结构化结果。"""
    reader = TdxDataReader(tdx_root)
    report = reader.quality_check(max_codes=4, expect_recent_days=2000)
    data = report.to_dict()
    assert data["total"] == 4
    assert data["ok"] >= 1
    assert isinstance(data["issues"], list)


def test_quality_check_detects_bad_data(tmp_path: Path) -> None:
    """质量检查应识别异常 OHLC 与重复日期。"""
    dates = trading_days("2023-01-02", 80)
    bars = make_bars(dates, seed=8)
    bars[10]["high"] = bars[10]["low"] * 0.5  # high < low
    bars[20]["close"] = 0.0                   # 非法价格
    bars.append(dict(bars[30]))               # 重复日期
    write_day_file(tmp_path / "vipdoc" / "sh" / "lday" / "sh600000.day", bars)

    reader = TdxDataReader(tmp_path)
    report = reader.quality_check(max_codes=5, expect_recent_days=2000)
    messages = " ".join(i.message for i in report.issues)
    assert "异常 OHLC" in messages or "重复交易日" in messages or "无法解析" in messages


# ----------------------------------------------------------------------
# 复权
# ----------------------------------------------------------------------
def test_adjust_without_factors_returns_raw(tdx_root: Path, tmp_path: Path) -> None:
    """缺少复权因子时应返回不复权数据并附告警标记。"""
    from backend.app.data.tdx_reader import apply_adjust

    reader = TdxDataReader(tdx_root)
    df = reader.read_daily("600000.SH")
    adjusted = apply_adjust(df, None, "qfq")
    pd.testing.assert_series_equal(adjusted["close"], df["close"], check_names=False)
    assert adjusted.attrs.get("adjust_applied") is False
    assert adjusted.attrs.get("adjust_warning")


def test_adjust_with_factors(tdx_root: Path) -> None:
    """提供复权因子时应实际改写价格。"""
    from backend.app.data.tdx_reader import apply_adjust

    reader = TdxDataReader(tdx_root)
    df = reader.read_daily("600000.SH")
    # 构造一个逐日递增的累计因子序列（1.0 -> 1.0 + 0.001*(n-1)）
    factors = pd.Series(
        1.0 + 0.001 * np.arange(len(df)), index=pd.DatetimeIndex(df["datetime"])
    )
    adjusted = apply_adjust(df, factors, "qfq")
    assert adjusted.attrs.get("adjust_applied") is True
    # 前复权以最新价格为基准：首日价格被改写，末日价格保持不变
    assert adjusted["close"].iloc[0] != pytest.approx(df["close"].iloc[0])
    assert adjusted["close"].iloc[-1] == pytest.approx(df["close"].iloc[-1])


# ----------------------------------------------------------------------
# pytdx 适配：索引名与成交量量纲
# ----------------------------------------------------------------------
def test_finalize_bars_handles_pytdx_index_name() -> None:
    """pytdx 返回的行索引名为 date（不是 datetime），必须能正确识别。"""
    raw = pd.DataFrame(
        {
            "open": [1.0],
            "high": [1.1],
            "low": [0.9],
            "close": [1.0],
            "amount": [100.0],
            "volume": [10.0],
        },
        index=pd.DatetimeIndex(["2024-01-02"], name="date"),
    )
    out = _finalize_bars(raw)
    assert list(out.columns) == [
        "datetime", "open", "high", "low", "close", "volume", "amount",
    ]
    assert out["datetime"].iloc[0] == pd.Timestamp("2024-01-02")


def test_from_pytdx_scales_volume_to_shares() -> None:
    """pytdx 的成交量单位是「手」，转换为标准结构后应放大 100 倍为「股」。"""
    raw = pd.DataFrame(
        {
            "open": [10.0],
            "high": [10.5],
            "low": [9.8],
            "close": [10.2],
            "amount": [10200.0],
            "volume": [1000.0],  # 1000 手
        },
        index=pd.DatetimeIndex(["2024-01-02"], name="date"),
    )
    out = _from_pytdx(raw)
    assert out["volume"].iloc[0] == pytest.approx(100_000.0)
    # 价格与成交额不应被改写
    assert out["close"].iloc[0] == pytest.approx(10.2)
    assert out["amount"].iloc[0] == pytest.approx(10200.0)


def test_pytdx_matches_fallback(tdx_root: Path) -> None:
    """pytdx 与内置解析器对同一文件应给出一致结果（价格 + 成交量量纲统一）。"""
    from backend.app.data import tdx_reader as mod

    path = tdx_root / "vipdoc" / "sh" / "lday" / "sh600000.day"
    fallback = _parse_day_file_fallback(path)
    reader_df = TdxDataReader(tdx_root).read_daily("600000.SH", use_cache=False)

    assert len(reader_df) == len(fallback)
    assert reader_df["close"].to_numpy() == pytest.approx(fallback["close"].to_numpy())
    assert reader_df["volume"].to_numpy() == pytest.approx(fallback["volume"].to_numpy())
    assert reader_df["amount"].to_numpy() == pytest.approx(fallback["amount"].to_numpy())

    if mod._PYTDX_DAILY_READER is None:
        pytest.skip("pytdx 不可用，仅校验回退路径")

    pytdx_df = _from_pytdx(mod._PYTDX_DAILY_READER().get_df_by_file(str(path)))
    assert len(pytdx_df) == len(fallback)
    assert pytdx_df["close"].to_numpy() == pytest.approx(fallback["close"].to_numpy())
    assert pytdx_df["volume"].to_numpy() == pytest.approx(fallback["volume"].to_numpy())


def test_min_struct_layout_matches_pytdx() -> None:
    """内置分钟线解析器的二进制布局必须与 pytdx 一致。

    真实 .lc5 的价格字段是「价格 ×100」的 int32，而不是 float32；
    若此处写错，pytdx 与内置解析器会读出完全不同的价格。
    """
    from backend.app.data import tdx_reader as mod

    assert mod._MIN_STRUCT.size == 32
    assert mod._MIN_STRUCT.format == "<HHIIIIfII"


def test_pytdx_matches_fallback_minute(tdx_root: Path) -> None:
    """分钟线：pytdx 与内置解析器应给出一致价格与成交量。

    注意分钟线与日线的量纲规则**不同**：pytdx 的分钟线 reader 不做
    「手 → 股」换算，因此这里不能套用日线的 ×100 系数。
    """
    from backend.app.data import tdx_reader as mod

    path = tdx_root / "vipdoc" / "sh" / "fzline" / "sh600000.lc5"
    fallback = _parse_min_file_fallback(path)
    reader_df = TdxDataReader(tdx_root).read_minute("600000.SH", freq="5min", use_cache=False)

    assert len(reader_df) > 0
    assert len(reader_df) == len(fallback)
    assert reader_df["close"].to_numpy() == pytest.approx(fallback["close"].to_numpy())
    assert reader_df["volume"].to_numpy() == pytest.approx(fallback["volume"].to_numpy())

    if mod._PYTDX_MIN_READER is None:
        pytest.skip("pytdx 不可用，仅校验回退路径")

    pytdx_df = mod._PYTDX_MIN_READER().get_df(str(path))
    # pytdx 分钟线不做手/股换算：其 volume 应与文件原始值（即「股」）一致
    assert pytdx_df["volume"].to_numpy() == pytest.approx(fallback["volume"].to_numpy())
    assert pytdx_df["close"].to_numpy() == pytest.approx(fallback["close"].to_numpy())

    # 经由读取器归一化后，两条路径结果必须完全一致
    normalized = _from_pytdx(pytdx_df, scale_volume=False)
    assert normalized["volume"].to_numpy() == pytest.approx(fallback["volume"].to_numpy())
