#!/usr/bin/env python
"""stock_selector 命令行入口。

示例::

    python cli.py read-data --tdx-dir C:/new_tdx
    python cli.py screen --strategy ma_cross --date 2024-01-02
    python cli.py backtest --strategy ma_cross --start 2020-01-01 --end 2024-01-01
    python cli.py track add --code 600000.SH --strategy ma_cross --note "观察"
    python cli.py track remove --code 600000.SH
    python cli.py track list
    python cli.py track update --date 2024-01-02
    python cli.py track report --start 2024-01-01 --end 2024-01-31
    python cli.py track export --output tracking.csv
    python cli.py serve --port 8000

设计说明：
    CLI 与 Web UI 共用同一套服务层（:mod:`app.services`），
    因此命令行与网页端的行为、结果完全一致。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# 保证可以直接以 `python cli.py` 运行（把项目根加入 sys.path）
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.core.config import get_config, parse_cli_overrides  # noqa: E402
from backend.app.core.logging import get_logger, setup_from_config  # noqa: E402
from backend.app.services.container import build_services, get_scheduler, reset_services  # noqa: E402

logger = get_logger("cli")


# ======================================================================
# 输出工具
# ======================================================================
def echo(message: str = "") -> None:
    """打印到标准输出。"""
    print(message)


def banner(title: str) -> None:
    """打印分节标题。"""
    echo("")
    echo("=" * 68)
    echo(f"  {title}")
    echo("=" * 68)


def print_table(rows: List[Dict[str, Any]], columns: List[str], max_rows: int = 50) -> None:
    """以等宽文本表格打印数据。

    :param rows: 行字典列表
    :param columns: 需要显示的列
    :param max_rows: 最多显示行数
    """
    if not rows:
        echo("(空)")
        return
    widths = {c: len(str(c)) for c in columns}
    for row in rows[:max_rows]:
        for c in columns:
            widths[c] = max(widths[c], len(_cell(row.get(c))))

    header = " | ".join(str(c).ljust(widths[c]) for c in columns)
    echo(header)
    echo("-" * len(header))
    for row in rows[:max_rows]:
        echo(" | ".join(_cell(row.get(c)).ljust(widths[c]) for c in columns))
    if len(rows) > max_rows:
        echo(f"... 共 {len(rows)} 行，仅显示前 {max_rows} 行")


def _cell(value: Any) -> str:
    """单元格格式化（处理浮点数与 None）。"""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def write_text(path: str, content: str) -> None:
    """写入文本文件并提示。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    echo(f"已写入：{p.resolve()}")


# ======================================================================
# 命令实现
# ======================================================================
def cmd_read_data(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """读取通达信本地数据并写入缓存。"""
    svc = ctx["services"]
    if args.tdx_dir:
        svc.data.set_tdx_dir(args.tdx_dir, persist=args.persist)

    banner("读取本地通达信数据")
    echo(f"通达信目录：{svc.config.get('data.tdx_dir')}")

    status = svc.data.status()
    if not status.get("ready"):
        echo("❌ 未找到 vipdoc 目录，请检查 --tdx-dir 参数。")
        return 2
    echo(f"vipdoc 目录：{status.get('vipdoc')}")
    echo(f"本地文件数：{status.get('total_files')}　明细：{status.get('markets')}")
    echo(f"pytdx 可用：{status.get('pytdx', {}).get('available')}")

    codes = args.codes.split(",") if args.codes else None
    result = svc.data.load(
        codes=codes,
        freq=args.freq,
        start=args.start,
        end=args.end,
        limit=args.limit,
        force=args.force,
    )
    echo("")
    echo(f"股票池：{result['total']}　成功：{result['loaded']}　失败：{result['failed']}　"
         f"总记录：{result['rows']}")
    return 0 if result["loaded"] > 0 else 1


def cmd_data_status(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """查看数据源状态。"""
    svc = ctx["services"]
    banner("数据源状态")
    status = svc.data.status()
    echo(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def cmd_data_quality(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """数据质量检查。"""
    svc = ctx["services"]
    banner("数据质量检查")
    result = svc.data.quality_check(max_codes=args.max_codes)
    echo(f"检查数量：{result['total']}　正常：{result['ok']}　有问题：{result['problem_count']}")
    print_table(result["issues"], ["code", "level", "message"], max_rows=args.max_rows)
    return 0


def cmd_cache(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """缓存管理。"""
    svc = ctx["services"]
    banner("缓存管理")
    if args.action == "clear":
        result = svc.data.clear_cache(code=args.code)
        echo(f"已删除 {result['removed']} 个缓存文件")
        return 0
    info = svc.data.cache_info(limit=args.limit)
    echo(f"缓存目录：{info['cache_dir']}　格式：{info['format']}")
    echo(f"条目数：{info['count']}　体积：{info['total_size_mb']} MB")
    print_table(
        info["entries"],
        ["code", "freq", "rows", "start", "end", "saved_at"],
        max_rows=args.max_rows,
    )
    return 0


def cmd_screen(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """执行选股。"""
    svc = ctx["services"]
    banner("选股")
    universe = args.universe.split(",") if args.universe else None
    result = svc.screen.run(
        strategy=args.strategy,
        params=ctx["strategy_params"],
        date=args.date,
        start=args.start,
        end=args.end,
        scan_all=args.scan_all,
        universe=universe,
        min_bars=args.min_bars,
        max_results=args.max_results,
        only_buy=not args.include_sell,
        limit_universe=args.limit_universe,
        progress=not args.quiet,
    )
    echo("")
    echo(f"策略：{result.strategy_name}（{result.strategy}）　参数：{result.params}")
    echo(f"扫描：{result.total_scanned} 只　命中：{result.matched} 只　耗时：{result.elapsed:.2f}s")
    if result.warnings:
        for w in result.warnings:
            echo(f"提示：{w}")
    echo("")
    print_table(result.to_rows(), ["code", "name", "date", "signal", "close", "pct_change(%)", "reason"])

    if args.output:
        if args.format == "csv":
            path = result.to_csv(args.output)
        else:
            write_text(args.output, result.to_markdown())
            path = Path(args.output)
        echo(f"结果已导出：{path.resolve()}")
    return 0


def cmd_backtest(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """执行回测。"""
    svc = ctx["services"]
    banner("回测")
    universe = args.universe.split(",") if args.universe else None

    override: Dict[str, Any] = {}
    if args.cash is not None:
        override["initial_cash"] = args.cash
    if args.max_positions is not None:
        override["max_positions"] = args.max_positions
    if args.commission is not None:
        override["commission"] = args.commission
    if args.stamp_tax is not None:
        override["stamp_tax"] = args.stamp_tax
    if args.slippage is not None:
        override["slippage"] = args.slippage
    if args.exec_price:
        override["exec_price"] = args.exec_price
    if args.benchmark is not None:
        override["benchmark"] = args.benchmark

    result = svc.backtest.run(
        start=args.start,
        end=args.end,
        strategy=args.strategy,
        params=ctx["strategy_params"],
        universe=universe,
        max_universe=args.max_universe,
        config_override=override,
        progress=not args.quiet,
    )

    echo("")
    echo(f"策略：{result.strategy_name}　区间：{result.start} ~ {result.end}　"
         f"股票池：{result.universe_size} 只")
    for w in result.warnings:
        echo(f"提示：{w}")

    perf = result.performance
    if perf and perf.metrics:
        metrics = perf.metrics
        echo("")
        echo("绩效指标：")
        labels = perf.to_dict().get("metric_labels", {})
        percent_keys = {
            "total_return", "annual_return", "max_drawdown", "annual_volatility",
            "win_rate", "turnover", "benchmark_return", "benchmark_annual_return",
            "excess_return", "alpha",
        }
        for key, value in metrics.items():
            label = labels.get(key, key)
            if value is None:
                text = "-"
            elif key in percent_keys:
                text = f"{float(value) * 100:.2f}%"
            else:
                text = f"{float(value):.4f}"
            echo(f"  {label:<14}: {text}")
        echo("")
        echo(f"交易明细：{len(result.trades)} 笔")
        print_table(
            result.trades[-20:],
            ["date", "code", "name", "direction_text", "price", "shares", "pnl", "pnl_pct", "reason"],
            max_rows=20,
        )

    if args.output:
        if args.format == "csv":
            import pandas as pd

            pd.DataFrame(result.trades).to_csv(args.output, index=False, encoding="utf-8-sig")
            echo(f"交易明细已导出：{Path(args.output).resolve()}")
        elif args.format == "html":
            write_text(args.output, result.to_html())
        else:
            write_text(args.output, result.to_markdown())
    return 0


def cmd_track(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """追踪相关子命令。"""
    svc = ctx["services"]
    action = args.action

    if action == "add":
        codes = [c for c in (args.code or "").split(",") if c]
        if not codes:
            echo("请通过 --code 指定股票代码")
            return 2
        added = 0
        for code in codes:
            try:
                item = svc.tracker.add_watch(
                    code=code,
                    name=args.name or "",
                    group=args.group,
                    note=args.note or "",
                    strategy=args.strategy or "",
                    tags=[t for t in (args.tags or "").split(",") if t],
                    cost_price=args.cost,
                    shares=args.shares,
                    target_price=args.target,
                    stop_price=args.stop,
                    source="cli",
                )
                echo(f"已加入关注池：{item['code']} {item['name']}（{item['group']}）")
                added += 1
            except Exception as exc:
                echo(f"添加 {code} 失败：{exc}")
        return 0 if added else 1

    if action == "remove":
        codes = [c for c in (args.code or "").split(",") if c]
        if not codes:
            echo("请通过 --code 指定股票代码")
            return 2
        for code in codes:
            ok_flag = svc.tracker.remove_watch(code, keep_state=args.keep_state)
            echo(f"{'已移除' if ok_flag else '未找到'}：{code}")
        return 0

    if action == "list":
        rows = svc.tracker.list_watch(group=args.group, keyword=args.keyword)
        states = {s["code"]: s for s in svc.tracker.list_states()}
        for r in rows:
            st = states.get(r["code"], {})
            r["state"] = st.get("state", "-")
            r["signal"] = st.get("signal_text", "-")
            r["price"] = st.get("price")
            r["risk"] = st.get("risk_level", "-")
        banner(f"关注池（{len(rows)} 只）")
        print_table(
            rows,
            ["code", "name", "group", "state", "signal", "price", "risk",
             "cost_price", "target_price", "stop_price", "note"],
            max_rows=args.max_rows,
        )
        return 0

    if action == "update":
        banner("追踪更新")
        result = svc.tracker.update(
            codes=[c for c in (args.code or "").split(",") if c] or None,
            date=args.date,
            group=args.group,
            strategy=args.strategy,
            notify=not args.no_notify,
        )
        echo(f"数据日期：{result.trade_date}　处理：{result.processed} 只　"
             f"状态变更：{result.changed}　新信号：{result.new_signals}　"
             f"提醒：{result.alerts}　耗时：{result.elapsed:.2f}s")
        if result.state_changes:
            echo("")
            echo("状态变更：")
            print_table(
                result.state_changes,
                ["trade_date", "code", "name", "old_state", "new_state", "reason"],
            )
        if result.errors:
            echo("")
            echo("错误：")
            for err in result.errors[:20]:
                echo(f"  {err}")
        return 0

    if action == "replay":
        banner("历史回放")
        result = svc.tracker.replay(
            codes=[c for c in (args.code or "").split(",") if c] or None,
            start=args.start,
            end=args.end,
            group=args.group,
            strategy=args.strategy,
            reset=not args.no_reset,
            notify=not args.no_notify,
        )
        echo(f"区间：{args.start} ~ {args.end}　处理：{result.processed} 条日次记录　"
             f"状态变更：{result.changed}　耗时：{result.elapsed:.2f}s")
        print_table(
            result.state_changes,
            ["trade_date", "code", "name", "old_state", "new_state", "reason"],
            max_rows=args.max_rows,
        )
        return 0

    if action == "state":
        banner("当前状态")
        rows = svc.tracker.list_states(
            states=[s for s in (args.state or "").split(",") if s] or None,
            group=args.group,
            risk_level=args.risk,
            keyword=args.keyword,
        )
        print_table(
            rows,
            ["code", "name", "group", "state", "prev_state", "state_changed_at",
             "data_date", "signal_text", "price", "pct_change", "risk_level",
             "unrealized_pct"],
            max_rows=args.max_rows,
        )
        return 0

    if action == "set-state":
        if not args.code or not args.state:
            echo("需要 --code 与 --state")
            return 2
        record = svc.tracker.set_state(args.code, args.state, reason=args.note or "")
        echo(f"{args.code} 状态已设置为 {args.state}")
        echo(json.dumps(record, ensure_ascii=False, indent=2))
        return 0

    if action == "report":
        return _track_report(args, ctx)

    if action == "export":
        return _track_export(args, ctx)

    echo(f"未知子命令：{action}")
    return 2


def _track_report(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """生成追踪报告。"""
    svc = ctx["services"]
    kind = args.kind or "daily"
    banner(f"追踪报告（{kind}）")

    if kind == "daily":
        report = svc.tracker.daily_report(date=args.date, group=args.group)
    elif kind == "range":
        report = svc.tracker.range_report(
            code=args.code, start=args.start, end=args.end, group=args.group
        )
    else:
        report = svc.tracker.summary_report(group=args.group)

    if args.output:
        path = Path(args.output)
        if path.suffix.lower() in (".html", ".htm"):
            write_text(str(path), report.to_html())
        elif path.suffix.lower() == ".csv":
            import pandas as pd

            pd.DataFrame(report.csv_rows).to_csv(path, index=False, encoding="utf-8-sig")
            echo(f"已写入：{path.resolve()}")
        else:
            write_text(str(path), report.markdown)
    else:
        echo(report.markdown)
    return 0


def _track_export(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """导出追踪数据。"""
    svc = ctx["services"]
    banner("导出追踪数据")
    output = args.output or "tracking.csv"
    table = args.table or "tracking_state"

    if table == "all":
        base = Path(output)
        for name in ("watchlist", "tracking_state", "tracking_history", "signals", "alerts"):
            target = base.with_name(f"{base.stem}_{name}{base.suffix or '.csv'}")
            svc.tracker.export(name, target)
            echo(f"已导出 {name} → {target.resolve()}")
        return 0

    filters: Dict[str, Any] = {}
    if args.start:
        filters["start"] = args.start
    if args.end:
        filters["end"] = args.end
    if args.code and table in ("tracking_history", "signals", "alerts"):
        filters["code"] = args.code

    path = svc.tracker.export(table, output, **filters)
    echo(f"已导出 {table} → {Path(path).resolve()}")
    return 0


def cmd_serve(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """启动 Web 服务。"""
    import uvicorn

    svc = ctx["services"]
    host = args.host or str(svc.config.get("web.host", "0.0.0.0"))
    port = args.port or int(svc.config.get("web.port", 8000))
    banner(f"启动 Web 服务 http://{host}:{port}")
    echo("接口文档：/docs　接口索引：/api")
    if not (PROJECT_ROOT / "frontend" / "dist" / "index.html").exists():
        echo("提示：前端尚未构建，请执行 cd frontend && npm install && npm run dev")
    uvicorn.run(
        "backend.app.main:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level=str(svc.config.get("logging.level", "info")).lower(),
    )
    return 0


def cmd_settings(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """查看 / 修改配置。"""
    svc = ctx["services"]
    banner("配置")
    if args.action == "get":
        key = args.key
        if key:
            echo(json.dumps(svc.config.get(key), ensure_ascii=False, indent=2))
        else:
            echo(json.dumps(svc.config.as_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.action == "set":
        if not args.key or args.value is None:
            echo("需要 --key 与 --value")
            return 2
        from backend.app.core.config import _coerce  # 复用类型推断

        svc.config.set(args.key, _coerce(str(args.value)), persist=True)
        svc.refresh_after_settings_change()
        echo(f"已设置 {args.key} = {svc.config.get(args.key)}")
        return 0
    if args.action == "path":
        echo(str(svc.config.path))
        return 0
    return 2


def cmd_scheduler(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """定时任务管理。"""
    scheduler = get_scheduler()
    banner("定时任务")
    if args.action == "start":
        echo(json.dumps(scheduler.start(), ensure_ascii=False, indent=2))
    elif args.action == "stop":
        echo(json.dumps(scheduler.stop(), ensure_ascii=False, indent=2))
    elif args.action == "run":
        result = scheduler.run_once(notify=not args.no_notify)
        echo(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        echo(json.dumps(scheduler.status(), ensure_ascii=False, indent=2))
    return 0


def cmd_strategies(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """列出可用策略。"""
    banner("可用策略")
    for item in ctx["services"].screen.strategies():
        echo(f"* {item['name']} —— {item['display_name']}")
        echo(f"  说明：{item['description']}")
        echo(f"  默认参数：{item['default_params']}")
        echo(f"  当前配置：{item.get('effective_params')}")
        echo("")
    return 0


def cmd_basics(args: argparse.Namespace, ctx: Dict[str, Any]) -> int:
    """股票基础信息（流通股本 / 总股本 / 行业）管理。

    流通盘类策略（如 ``volume_surge``「小盘放量」）依赖该文件判断流通市值，
    因为通达信 ``.day`` 文件本身不包含股本信息。
    """
    svc = ctx["services"]
    banner("股票基础信息")
    action = args.action

    if action == "template":
        path = svc.data.basics.write_template()
        echo(f"模板已生成：{path}")
        echo("请填写 float_shares 列（单位：股，也支持 65.7亿 / 3500万 这类写法），然后执行：")
        echo(f"  python cli.py basics import --file {path}")
        return 0

    if action == "import":
        if not args.file:
            echo("请用 --file 指定要导入的 CSV 路径")
            return 2
        source = Path(args.file)
        if not source.exists():
            echo(f"文件不存在：{source}")
            return 2
        from backend.app.data.basics import StockBasicResolver

        loaded = StockBasicResolver(source).load()
        if not loaded:
            echo(f"未能从 {source} 解析出任何记录，请检查表头是否包含 code 与 float_shares。")
            return 2
        items = {
            code: {
                "name": item.name,
                "float_shares": item.float_shares,
                "total_shares": item.total_shares,
                "industry": item.industry,
            }
            for code, item in loaded.items()
        }
        result = svc.data.update_basics(items)
        echo(f"已导入 {len(items)} 条记录 → {result['file']}（当前共 {result['count']} 条）")
        return 0

    if action == "list":
        data = svc.data.basics_list(keyword=args.keyword or "", limit=args.limit)
        echo(f"文件：{data['file']}")
        echo(f"共 {data['count']} 条，显示 {data['returned']} 条")
        for row in data["items"]:
            shares = row["float_shares_yi"]
            echo(
                f"  {row['code']:<12} {row['name']:<10} "
                f"流通股本 {shares if shares is not None else '未填写'} 亿股"
            )
        return 0

    # status
    status = svc.data.status().get("basics", {})
    echo(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


# ======================================================================
# 参数解析
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="stock_selector",
        description="A 股选股、回测与持续追踪系统（本地通达信数据）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", help="配置文件路径（默认 ./config.yaml）")
    parser.add_argument(
        "--set", dest="overrides", action="append", default=[],
        metavar="KEY=VALUE", help="临时覆盖配置，可重复，如 --set backtest.max_positions=5",
    )
    parser.add_argument("--quiet", action="store_true", help="关闭进度条")

    sub = parser.add_subparsers(dest="command", required=True)

    # ---------------- 数据 ----------------
    p = sub.add_parser("read-data", help="读取本地通达信数据并写入缓存")
    p.add_argument("--tdx-dir", help="通达信安装目录（含 vipdoc）")
    p.add_argument("--codes", help="逗号分隔的股票代码；默认扫描全部")
    p.add_argument("--freq", default="daily", help="daily / 1min / 5min / 15min / 30min / 60min")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--limit", type=int, help="最多读取多少只")
    p.add_argument("--force", action="store_true", help="忽略缓存强制重读")
    p.add_argument("--persist", action="store_true", help="把 tdx-dir 写回配置文件")
    p.set_defaults(func=cmd_read_data)

    p = sub.add_parser("data-status", help="查看数据源状态")
    p.set_defaults(func=cmd_data_status)

    p = sub.add_parser("data-quality", help="数据质量检查")
    p.add_argument("--max-codes", type=int, default=200)
    p.add_argument("--max-rows", type=int, default=50)
    p.set_defaults(func=cmd_data_quality)

    p = sub.add_parser("cache", help="缓存管理")
    p.add_argument("action", choices=["info", "clear"])
    p.add_argument("--code", help="只清理指定股票")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--max-rows", type=int, default=50)
    p.set_defaults(func=cmd_cache)

    p = sub.add_parser("strategies", help="列出可用策略")
    p.set_defaults(func=cmd_strategies)

    p = sub.add_parser("basics", help="股票基础信息（流通股本）管理")
    p.add_argument(
        "action", choices=["status", "template", "import", "list"],
        help="status=查看状态 / template=生成模板 / import=从 CSV 导入 / list=列出已配置",
    )
    p.add_argument("--file", help="import：待导入的 CSV 路径（表头 code,name,float_shares,...）")
    p.add_argument("--keyword", help="list：代码或名称模糊过滤")
    p.add_argument("--limit", type=int, default=50, help="list：最多显示多少条")
    p.set_defaults(func=cmd_basics)

    # ---------------- 选股 ----------------
    p = sub.add_parser("screen", help="执行选股")
    p.add_argument(
        "--strategy",
        help="策略名：ma_cross / rsi / volume_breakout / volume_surge",
    )
    p.add_argument("--date", help="选股日期 YYYY-MM-DD")
    p.add_argument("--start", help="区间选股起始日")
    p.add_argument("--end", help="区间选股结束日")
    p.add_argument("--scan-all", action="store_true", help="在区间内逐日扫描")
    p.add_argument("--universe", help="逗号分隔的股票池")
    p.add_argument("--limit-universe", type=int, help="限制股票池大小（调试用）")
    p.add_argument("--min-bars", type=int, default=60)
    p.add_argument("--max-results", type=int, default=200)
    p.add_argument("--include-sell", action="store_true", help="同时输出卖出信号")
    p.add_argument("--output", help="导出文件路径")
    p.add_argument("--format", choices=["csv", "markdown", "md"], default="csv")
    p.add_argument("--param", action="append", default=[], metavar="K=V", help="策略参数，可重复")
    p.set_defaults(func=cmd_screen)

    # ---------------- 回测 ----------------
    p = sub.add_parser("backtest", help="执行回测")
    p.add_argument("--strategy", help="策略名")
    p.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    p.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    p.add_argument("--universe", help="逗号分隔的股票池")
    p.add_argument("--max-universe", type=int, help="限制股票池大小")
    p.add_argument("--cash", type=float, help="初始资金")
    p.add_argument("--max-positions", type=int, help="最大持仓数")
    p.add_argument("--commission", type=float, help="佣金费率")
    p.add_argument("--stamp-tax", type=float, help="印花税率")
    p.add_argument("--slippage", type=float, help="滑点率")
    p.add_argument("--exec-price", choices=["next_open", "next_close", "close"])
    p.add_argument("--benchmark", help="基准指数代码，如 000300.SH；传空字符串可关闭")
    p.add_argument("--output", help="导出文件路径")
    p.add_argument("--format", choices=["csv", "markdown", "md", "html"], default="markdown")
    p.add_argument("--param", action="append", default=[], metavar="K=V", help="策略参数，可重复")
    p.set_defaults(func=cmd_backtest)

    # ---------------- 追踪 ----------------
    p = sub.add_parser("track", help="持续追踪")
    tsub = p.add_subparsers(dest="action", required=True)

    t = tsub.add_parser("add", help="加入关注池")
    t.add_argument("--code", required=True, help="股票代码，多个用逗号分隔")
    t.add_argument("--name", help="股票名称")
    t.add_argument("--group", default="默认分组")
    t.add_argument("--tags", help="标签，逗号分隔")
    t.add_argument("--note", help="备注")
    t.add_argument("--strategy", help="绑定策略")
    t.add_argument("--cost", type=float, help="成本价")
    t.add_argument("--shares", type=int, help="持股数")
    t.add_argument("--target", type=float, help="目标价")
    t.add_argument("--stop", type=float, help="止损价")

    t = tsub.add_parser("remove", help="移出关注池")
    t.add_argument("--code", required=True)
    t.add_argument("--keep-state", action="store_true", help="保留状态记录")

    t = tsub.add_parser("list", help="列出关注池")
    t.add_argument("--group")
    t.add_argument("--keyword")
    t.add_argument("--max-rows", type=int, default=100)

    t = tsub.add_parser("update", help="执行追踪更新")
    t.add_argument("--code", help="指定股票，逗号分隔")
    t.add_argument("--date", help="目标日期 YYYY-MM-DD（按日期回放）")
    t.add_argument("--group")
    t.add_argument("--strategy")
    t.add_argument("--no-notify", action="store_true")

    t = tsub.add_parser("replay", help="历史回放")
    t.add_argument("--code")
    t.add_argument("--start", required=True)
    t.add_argument("--end", required=True)
    t.add_argument("--group")
    t.add_argument("--strategy")
    t.add_argument("--no-reset", action="store_true", help="保留既有历史，只追加")
    t.add_argument("--no-notify", action="store_true")
    t.add_argument("--max-rows", type=int, default=50)

    t = tsub.add_parser("state", help="查看当前状态")
    t.add_argument("--state", help="按状态过滤，逗号分隔")
    t.add_argument("--group")
    t.add_argument("--risk", choices=["normal", "warning", "danger"])
    t.add_argument("--keyword")
    t.add_argument("--max-rows", type=int, default=100)

    t = tsub.add_parser("set-state", help="手动设置状态")
    t.add_argument("--code", required=True)
    t.add_argument("--state", required=True)
    t.add_argument("--note")

    t = tsub.add_parser("report", help="生成追踪报告")
    t.add_argument("--kind", choices=["daily", "range", "summary"], default="daily")
    t.add_argument("--date")
    t.add_argument("--code")
    t.add_argument("--start")
    t.add_argument("--end")
    t.add_argument("--group")
    t.add_argument("--output", help="导出文件（.md/.html/.csv）")

    t = tsub.add_parser("export", help="导出追踪数据")
    t.add_argument("--output", default="tracking.csv")
    t.add_argument(
        "--table", default="tracking_state",
        choices=["watchlist", "tracking_state", "tracking_history", "signals", "alerts", "all"],
    )
    t.add_argument("--code")
    t.add_argument("--start")
    t.add_argument("--end")

    p.set_defaults(func=cmd_track, code=None, group=None, strategy=None, date=None,
                   start=None, end=None, output=None, note=None, keyword=None,
                   risk=None, state=None, no_notify=False, no_reset=False,
                   keep_state=False, max_rows=50, table=None)

    # ---------------- 服务 ----------------
    p = sub.add_parser("serve", help="启动 Web 服务")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("settings", help="查看/修改配置")
    p.add_argument("action", choices=["get", "set", "path"], default="get", nargs="?")
    p.add_argument("--key")
    p.add_argument("--value")
    p.set_defaults(func=cmd_settings)

    p = sub.add_parser("scheduler", help="定时任务管理")
    p.add_argument("action", choices=["status", "start", "stop", "run"], default="status", nargs="?")
    p.add_argument("--no-notify", action="store_true")
    p.set_defaults(func=cmd_scheduler)

    return parser


def parse_params(items: List[str]) -> Dict[str, Any]:
    """把 ``--param k=v`` 解析为策略参数字典。"""
    from backend.app.core.config import _coerce

    out: Dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            continue
        key, _, value = item.partition("=")
        out[key.strip()] = _coerce(value.strip())
    return out


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 主入口。

    :param argv: 参数列表（默认取 ``sys.argv[1:]``）
    :return: 进程退出码
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # 未指定时补齐默认属性，避免子命令缺失属性导致 AttributeError
    for name, default in (
        ("code", None), ("group", None), ("strategy", None), ("date", None),
        ("start", None), ("end", None), ("output", None), ("note", None),
        ("keyword", None), ("risk", None), ("state", None),
        ("no_notify", False), ("no_reset", False), ("keep_state", False),
        ("max_rows", 50), ("table", None), ("param", []), ("format", "csv"),
        ("include_sell", False), ("scan_all", False), ("quiet", False),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)

    # ---------------- 初始化配置与服务 ----------------
    try:
        config_path = args.config or os.environ.get("STOCK_SELECTOR_CONFIG")
        config = get_config(config_path)
        overrides = parse_cli_overrides(args.overrides)
        if overrides:
            config.update(overrides, persist=False)
        config.ensure_dirs()
        setup_from_config(force=True)

        reset_services()
        services = build_services(str(config.path))
        ctx: Dict[str, Any] = {
            "services": services,
            "config": config,
            "strategy_params": parse_params(getattr(args, "param", []) or []),
        }
    except Exception as exc:
        echo(f"初始化失败：{exc}")
        return 3

    try:
        return int(args.func(args, ctx) or 0)
    except KeyboardInterrupt:  # pragma: no cover
        echo("已中断")
        return 130
    except Exception as exc:
        logger.exception("命令执行失败")
        echo(f"执行失败：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
