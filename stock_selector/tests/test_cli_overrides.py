"""``--set`` 命令行覆盖项解析的回归测试。

``parse_cli_overrides`` 必须把 ``data.tdx_dir=./demo_tdx`` 这类点号路径
还原成**嵌套字典**，否则 ``Config.update`` 会把它当成一个名为
``"data.tdx_dir"`` 的新顶级字段，覆盖静默失效。
"""

from __future__ import annotations

from backend.app.core.config import Config, parse_cli_overrides


def test_parse_cli_overrides_nested() -> None:
    """点号路径应还原为嵌套字典。"""
    result = parse_cli_overrides(
        ["data.tdx_dir=./demo_tdx", "backtest.max_positions=5", "screen.min_bars=30"]
    )
    assert result == {
        "data": {"tdx_dir": "./demo_tdx"},
        "backtest": {"max_positions": 5},
        "screen": {"min_bars": 30},
    }


def test_parse_cli_overrides_types_and_edge_cases() -> None:
    """类型推断与非法输入。"""
    result = parse_cli_overrides(
        ["a.b=true", "a.c=1.5", "a.d=null", "nodot", "=x", "e.f=", ""]
    )
    assert result["a"] == {"b": True, "c": 1.5, "d": None}
    assert result["e"] == {"f": None}
    assert "nodot" not in result


def test_parse_cli_overrides_applied_to_config(tmp_path) -> None:
    """覆盖项经 ``Config.update`` 后应真正改到目标配置项。"""
    cfg = Config(path=tmp_path / "config.yaml", auto_create=True)
    cfg.update(parse_cli_overrides(["data.tdx_dir=./demo_tdx"]), persist=False)
    assert cfg.get("data.tdx_dir") == "./demo_tdx"
    # 不应污染其它字段
    assert cfg.get("data.cache_dir")
    assert "data.tdx_dir" not in cfg.as_dict()
