"""「小盘放量」策略（volume_surge）与流通股本数据源的测试。

覆盖：
    * ``parse_shares`` 中文单位解析（亿 / 万 / 千分位 / 非法值）
    * ``StockBasicResolver`` 模板、加载、upsert、流通市值计算
    * ``attach_float_shares`` 注入行为（不修改原始 DataFrame）
    * ``VolumeSurgeStrategy`` 两条买入条件的与逻辑、流通盘口径切换、
      数据缺失时的两种行为
    * 选股器在缺少流通股本时的告警与结果
    * 基础信息相关 API
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.app.data.basics import (
    StockBasicResolver,
    attach_float_shares,
    parse_shares,
)
from backend.app.strategies import VolumeSurgeStrategy, get_strategy
from backend.app.strategies.registry import (
    strategy_names,
    strategy_names_requiring_basics,
)

# ----------------------------------------------------------------------
# 工具：构造可复现的行情（每 20 根出现一次 5 倍放量）
# ----------------------------------------------------------------------
def make_frame(n: int = 120, price: float = 10.0) -> pd.DataFrame:
    """构造一段含周期性放量的行情，便于精确断言放量条件。"""
    rng = np.random.default_rng(7)
    close = pd.Series(price + rng.normal(0, 0.2, n)).round(2)
    volume = pd.Series(
        np.where(np.arange(n) % 20 == 15, 5_000_000.0, 1_000_000.0), dtype="float64"
    )
    return pd.DataFrame(
        {
            "datetime": pd.date_range("2024-01-01", periods=n, freq="B"),
            "open": close,
            "high": (close * 1.01).round(2),
            "low": (close * 0.99).round(2),
            "close": close,
            "volume": volume,
            "amount": (volume * close).round(2),
        }
    )


# ----------------------------------------------------------------------
# 股本解析
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        (6570000000, 6570000000.0),
        ("6570000000", 6570000000.0),
        ("65.7亿", 6570000000.0),
        ("65.7亿股", 6570000000.0),
        ("3500万", 35000000.0),
        ("3500万股", 35000000.0),
        ("1,234,000", 1234000.0),
        ("abc", None),
        ("", None),
        (None, None),
        (0, None),
        (-5, None),
        (True, None),             # 布尔值不算库存股
    ],
)
def test_parse_shares(raw: object, expected: float | None) -> None:
    """股本解析应同时支持数字与中文单位写法。"""
    result = parse_shares(raw)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# ----------------------------------------------------------------------
# 解析器
# ----------------------------------------------------------------------
def test_resolver_template_and_load(tmp_path: Path) -> None:
    """模板生成 + 读取 + 流通市值计算。"""
    path = tmp_path / "stock_basic.csv"
    resolver = StockBasicResolver(path)

    assert resolver.available is False
    resolver.write_template()
    assert path.exists()
    # 模板不会覆盖已存在的文件
    resolver.write_template()

    # 手工改写为真实的映射（含中文单位与千分位）
    path.write_text(
        "code,name,float_shares,total_shares,industry\n"
        "600000.SH,浦发银行,65.7亿,2.9亿,银行\n"
        "000001.SZ,平安银行,\"19,400,000,000\",19406000000,银行\n"
        "830000.BJ,云星宇,3500万,,软件服务\n"
        "bogus,无效代码,1亿,,\n",
        encoding="utf-8-sig",
    )

    data = resolver.load(force=True)
    assert set(data) == {"600000.SH", "000001.SZ", "830000.BJ"}
    assert resolver.float_shares("600000.SH") == pytest.approx(6.57e9)
    assert resolver.float_shares("000001.SZ") == pytest.approx(1.94e10)
    assert resolver.float_shares("830000.BJ") == pytest.approx(3.5e7)
    # 未知代码与无股本数据的场景
    assert resolver.float_shares("999999.SZ") is None

    # 流通市值 = 流通股本 × 价格
    assert resolver.float_market_cap("600000.SH", 10.0) == pytest.approx(6.57e10)
    assert resolver.float_market_cap("999999.SZ", 10.0) is None
    assert resolver.float_market_cap("600000.SH", float("nan")) is None

    assert resolver.get("600000.SH").name == "浦发银行"
    assert resolver.get("600000.SH").industry == "银行"


def test_resolver_missing_file_returns_empty(tmp_path: Path) -> None:
    """文件不存在时应返回空映射，而不是报错。"""
    resolver = StockBasicResolver(tmp_path / "not-exists.csv")
    assert resolver.load() == {}
    assert resolver.size == 0
    assert resolver.float_shares("600000.SH") is None


def test_resolver_upsert(tmp_path: Path) -> None:
    """upsert 支持「明细字典」与「直接给股本」两种写法。"""
    path = tmp_path / "stock_basic.csv"
    resolver = StockBasicResolver(path)
    count = resolver.upsert(
        {
            "600000.SH": {"name": "浦发银行", "float_shares": "65.7亿", "industry": "银行"},
            "000001.SZ": 1.94e10,
        }
    )
    assert count == 2
    assert resolver.float_shares("600000.SH") == pytest.approx(6.57e9)
    assert resolver.float_shares("000001.SZ") == pytest.approx(1.94e10)

    # 再次 upsert 只更新指定字段，不丢已有信息
    resolver.upsert({"600000.SH": {"float_shares": "70亿"}})
    item = resolver.get("600000.SH")
    assert item.float_shares == pytest.approx(7e9)
    assert item.name == "浦发银行"
    assert item.industry == "银行"

    # 重新从磁盘加载后结果一致（确认真的落盘了）
    reloaded = StockBasicResolver(path)
    assert reloaded.float_shares("600000.SH") == pytest.approx(7e9)


def test_attach_float_shares_does_not_mutate(tmp_path: Path) -> None:
    """注入流通股本不应修改传入的 DataFrame。"""
    path = tmp_path / "stock_basic.csv"
    resolver = StockBasicResolver(path)
    resolver.upsert({"600000.SH": 5e8})

    df = make_frame(30)
    out = attach_float_shares(df, "600000.SH", resolver)
    assert "float_shares" not in df.columns
    assert out is not df
    assert (out["float_shares"] == 5e8).all()

    # 未命中的代码 -> NaN
    out_miss = attach_float_shares(df, "999999.SZ", resolver)
    assert out_miss["float_shares"].isna().all()

    # resolver 为 None 或 df 为空时原样返回
    assert attach_float_shares(df, "600000.SH", None) is df
    assert attach_float_shares(None, "600000.SH", resolver) is None


# ----------------------------------------------------------------------
# 策略：注册与参数
# ----------------------------------------------------------------------
def test_volume_surge_registered() -> None:
    """新策略应出现在注册表中，并声明需要流通股本。"""
    assert "volume_surge" in strategy_names()
    assert strategy_names_requiring_basics() == ["volume_surge"]

    meta = VolumeSurgeStrategy.describe()
    assert meta["requires_basics"] is True
    assert meta["display_name"]
    assert meta["param_schema"]["volume_ma_window"]["type"] == "int"
    assert meta["param_schema"]["cap_metric"]["options"]


def test_volume_surge_default_params_match_requirement() -> None:
    """默认参数必须与需求口径一致：20 日均量、2 倍、150 亿。"""
    strat = get_strategy("volume_surge")
    params = strat.get_params()
    assert params["volume_ma_window"] == 20
    assert params["volume_ratio"] == 2.0
    assert params["cap_metric"] == "float_market_cap"
    assert params["max_float_market_cap"] == 150.0
    assert params["max_float_shares"] == 150.0


@pytest.mark.parametrize(
    "params",
    [
        {"volume_ratio": 1.0},          # 放量倍数必须 > 1
        {"cap_metric": "bogus"},        # 未知口径
    ],
)
def test_volume_surge_param_validation(params: dict) -> None:
    """非法参数应直接报错。"""
    with pytest.raises(ValueError):
        VolumeSurgeStrategy(params)


# ----------------------------------------------------------------------
# 策略：信号逻辑
# ----------------------------------------------------------------------
def test_volume_surge_buy_requires_both_conditions() -> None:
    """买入必须同时满足「放量」与「小流通盘」。"""
    strat = VolumeSurgeStrategy({"volume_ma_window": 20, "volume_ratio": 2.0})
    df = make_frame()

    # (1) 无流通股本数据 -> 严格模式下不产生买入
    out = strat.generate_signals(df)
    assert int((out["signal"] == 1).sum()) == 0
    assert out["cap_cond"].sum() == 0

    # (2) 小盘（5 亿股 × 10 元 = 50 亿 < 150 亿）-> 放量日产生买入
    small = df.assign(float_shares=5e8)
    out_small = strat.generate_signals(small)
    buy_rows = out_small[out_small["signal"] == 1]
    assert len(buy_rows) > 0
    assert (buy_rows["volume_r"] >= 2.0).all()
    assert (buy_rows["float_cap"] <= 150.0).all()
    assert np.allclose(buy_rows["float_shares_yi"].to_numpy(), 5.0)

    # (3) 大盘（30 亿股 × 10 元 = 300 亿 > 150 亿）-> 放量也不买
    big = df.assign(float_shares=3e9)
    out_big = strat.generate_signals(big)
    assert int((out_big["signal"] == 1).sum()) == 0
    # 放量条件本身是满足的，被流通盘挡住
    assert int(out_big["volume_cond"].sum()) > 0
    assert out_big["cap_cond"].sum() == 0


def test_volume_surge_volume_threshold() -> None:
    """放量阈值应为「≥ 均量的 2 倍」，且均量默认不含当日。"""
    df = make_frame()
    strat = VolumeSurgeStrategy(
        {"volume_ma_window": 20, "volume_ratio": 2.0, "exclude_current_volume": True}
    )
    out = strat.generate_signals(df.assign(float_shares=5e8))

    manual_ma = df["volume"].rolling(20, min_periods=20).mean().shift(1)
    pd.testing.assert_series_equal(
        out["vol_ma"].reset_index(drop=True), manual_ma.reset_index(drop=True),
        check_names=False,
    )
    expected = (df["volume"] / manual_ma >= 2.0).fillna(False)
    assert out["volume_cond"].reset_index(drop=True).equals(expected.reset_index(drop=True))

    # 关闭「均量不含当日」后，基准均量会不同
    incl = VolumeSurgeStrategy(
        {"volume_ma_window": 20, "volume_ratio": 2.0, "exclude_current_volume": False}
    )
    out_incl = incl.generate_signals(df.assign(float_shares=5e8))
    assert not out_incl["vol_ma"].reset_index(drop=True).equals(
        out["vol_ma"].reset_index(drop=True)
    )


def test_volume_surge_cap_metric_shares() -> None:
    """口径切到流通股本时应按「亿股」比较。"""
    strat = VolumeSurgeStrategy(
        {"cap_metric": "float_shares", "max_float_shares": 150.0, "volume_ratio": 2.0}
    )
    df = make_frame()

    # 100 亿股 < 150 亿股 -> 通过（市值 1000 亿也照样通过，因为口径是股本）
    out_pass = strat.generate_signals(df.assign(float_shares=100e8))
    assert int((out_pass["signal"] == 1).sum()) > 0

    # 200 亿股 > 150 亿股 -> 不通过
    out_fail = strat.generate_signals(df.assign(float_shares=200e8))
    assert int((out_fail["signal"] == 1).sum()) == 0


def test_volume_surge_allow_missing_basics() -> None:
    """开启「缺少流通股本时放行」后，仅按放量条件选股。"""
    df = make_frame()
    strict = VolumeSurgeStrategy({"allow_missing_basics": False})
    loose = VolumeSurgeStrategy({"allow_missing_basics": True})

    assert int((strict.generate_signals(df)["signal"] == 1).sum()) == 0
    loose_out = loose.generate_signals(df)
    assert int((loose_out["signal"] == 1).sum()) > 0
    # 放行时买入信号数量应与「放量」条件数量一致
    assert int((loose_out["signal"] == 1).sum()) == int(loose_out["volume_cond"].sum())


def test_volume_surge_exit_signal() -> None:
    """跌破离场均线应产生卖出信号；关闭均线离场后不再卖出。"""
    strat = VolumeSurgeStrategy({"exit_ma_window": 20, "use_ma_exit": True})
    df = make_frame()
    out = strat.generate_signals(df.assign(float_shares=5e8))
    assert int((out["signal"] == -1).sum()) > 0

    no_exit = VolumeSurgeStrategy({"use_ma_exit": False})
    out_no_exit = no_exit.generate_signals(df.assign(float_shares=5e8))
    assert int((out_no_exit["signal"] == -1).sum()) == 0


def test_volume_surge_no_lookahead() -> None:
    """截断未来数据不应改变历史信号与因子（无未来函数）。"""
    strat = VolumeSurgeStrategy({"volume_ma_window": 20, "volume_ratio": 2.0})
    df = make_frame().assign(float_shares=5e8)
    full = strat.generate_signals(df)

    for k in (40, 60, 90, len(df) - 1):
        truncated = strat.generate_signals(df.iloc[:k].reset_index(drop=True))
        assert int(truncated["signal"].iloc[-1]) == int(full["signal"].iloc[k - 1])
        for col in strat.factor_columns:
            a = truncated[col].iloc[-1]
            b = full[col].iloc[k - 1]
            assert a == pytest.approx(b, rel=1e-9, nan_ok=True), f"{col} 在第 {k} 行不一致"


def test_volume_surge_evaluate_output() -> None:
    """``evaluate`` 输出的结构应可直接用于报告与前端表格。"""
    strat = VolumeSurgeStrategy({"volume_ratio": 1.5})
    df = make_frame().assign(float_shares=5e8)
    detail = strat.evaluate("600000.SH", df, date=df["datetime"].iloc[-1], name="浦发银行")
    assert detail is not None
    d = detail.to_dict()
    assert d["code"] == "600000.SH"
    assert d["strategy"] == "volume_surge"
    # 因子齐全
    for col in ("volume_r", "vol_ma", "float_cap", "float_shares_yi"):
        assert col in d["factors"]
    # 条件字典来自 *_cond 列
    assert {"buy_cond", "volume_cond", "cap_cond"} <= set(d["conditions"])


# ----------------------------------------------------------------------
# 选股器：注入与告警
# ----------------------------------------------------------------------
def test_screener_warns_when_basics_missing(services) -> None:
    """缺少流通股本文件时应告警，且严格模式下命中为 0。"""
    assert services.data.basics.available is False

    result = services.screen.run(
        strategy="volume_surge",
        date=None,
        min_bars=30,
        only_buy=True,
        progress=False,
    )
    assert result.matched == 0
    assert any("流通股本" in w for w in result.warnings), result.warnings


def test_screener_applies_cap_filter(services) -> None:
    """写入流通股本后，选股结果必须全部满足流通市值条件。"""
    cache_dir = Path(services.config.get_path("data.cache_dir"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    # 两只小盘 + 两只大盘：大盘股不应出现在结果里
    (cache_dir / "stock_basic.csv").write_text(
        "code,name,float_shares,total_shares,industry\n"
        "600000.SH,浦发银行,1亿,1.2亿,银行\n"
        "000001.SZ,平安银行,1亿,1.2亿,银行\n"
        "300750.SZ,宁德时代,100亿,120亿,电池\n"
        "830000.BJ,云星宇,100亿,120亿,软件服务\n",
        encoding="utf-8-sig",
    )
    assert services.data.basics.available is True
    assert services.data.basics.size == 4

    result = services.screen.run(
        strategy="volume_surge",
        date=None,
        min_bars=30,
        only_buy=True,
        progress=False,
    )
    assert not result.warnings
    codes = {item.code for item in result.results}
    assert codes <= {"600000.SH", "000001.SZ"}
    for item in result.results:
        assert item.factors["float_cap"] <= 150.0
        assert item.factors["volume_r"] >= 2.0


def test_screener_allow_missing_keeps_candidates(services) -> None:
    """放行模式下应给出告警但仍能选出股票。"""
    result = services.screen.run(
        strategy="volume_surge",
        params={"allow_missing_basics": True},
        min_bars=30,
        only_buy=True,
        progress=False,
    )
    assert any("放行" in w for w in result.warnings), result.warnings
    for item in result.results:
        assert np.isnan(item.factors["float_cap"])


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
def test_basics_api(client) -> None:
    """基础信息模板 / 上传 / 查询三个端点。"""
    resp = client.post("/api/data/basics/template")
    assert resp.status_code == 200 and resp.json()["ok"] is True

    resp = client.post(
        "/api/data/basics",
        json={"basics": {"600000.SH": {"name": "浦发银行", "float_shares": "65.7亿"}}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] >= 1

    resp = client.get("/api/data/basics", params={"keyword": "600000"})
    body = resp.json()
    assert body["ok"] is True
    codes = {row["code"] for row in body["data"]["items"]}
    assert "600000.SH" in codes
    row = next(r for r in body["data"]["items"] if r["code"] == "600000.SH")
    assert row["float_shares"] == pytest.approx(6.57e9)
    assert row["float_shares_yi"] == pytest.approx(65.7)

    # 空请求应报错
    resp = client.post("/api/data/basics", json={"basics": {}})
    assert resp.status_code >= 400 or resp.json()["ok"] is False


def test_status_exposes_basics(client) -> None:
    """数据源状态应包含基础信息区块，便于前端提示。"""
    body = client.get("/api/data/status").json()
    assert body["ok"] is True
    basics = body["data"]["basics"]
    assert "available" in basics
    assert "volume_surge" in basics["strategies_requiring_basics"]


def test_screen_strategies_api_lists_volume_surge(client) -> None:
    """策略列表接口应返回新策略的元信息（前端据此渲染参数表单）。"""
    body = client.get("/api/data/strategies").json()
    assert body["ok"] is True
    names = {item["name"] for item in body["data"]}
    assert "volume_surge" in names
    item = next(i for i in body["data"] if i["name"] == "volume_surge")
    assert item["requires_basics"] is True
    assert item["param_schema"]["max_float_market_cap"]["label"]
