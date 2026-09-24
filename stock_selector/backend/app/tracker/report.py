"""追踪报告生成模块。

提供三类报告（全部以 Markdown 为主格式，可在 Web UI 中直接渲染）：
    * **每日追踪报告**：当日新增信号、状态变更、风险提醒、持仓盈亏；
    * **区间追踪报告**：某只股票或整个关注池在指定区间的时间线与统计；
    * **汇总报告**：关注池所有股票的当前状态、上次状态、变更时间、关键指标。

同时提供选股报告与回测报告的包装，统一由 ``/api/report`` 输出。
"""

from __future__ import annotations

import html as _html
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from ..core.logging import get_logger
from ..data.names import NameResolver
from .state_machine import RISK_LABELS
from .storage import TrackerStorage

logger = get_logger("report")

# 状态 → 图标（纯文本，避免依赖 emoji 字体）
STATE_MARKS = {
    "候选": "○", "观察": "◔", "触发": "◑", "买入": "◕", "持仓": "●",
    "减仓": "◒", "清仓": "◯", "止盈": "★", "止损": "✖", "失效": "×", "暂停": "‖",
}


@dataclass
class Report:
    """报告容器。"""

    kind: str
    title: str
    markdown: str
    generated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    period: Dict[str, Any] = field(default_factory=dict)
    summary: Dict[str, Any] = field(default_factory=dict)
    csv_rows: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return {
            "kind": self.kind,
            "title": self.title,
            "markdown": self.markdown,
            "html": self.to_html(),
            "generated_at": self.generated_at,
            "period": self.period,
            "summary": self.summary,
            "rows": self.csv_rows,
        }

    def to_html(self) -> str:
        """把 Markdown 转换为独立 HTML（不依赖第三方库）。"""
        body = markdown_to_html(self.markdown)
        return (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            f"<title>{_html.escape(self.title)}</title>"
            "<style>body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
            "margin:32px auto;max-width:1100px;line-height:1.75;color:#1f1f1f;padding:0 16px}"
            "h1{border-bottom:2px solid #e8e8e8;padding-bottom:10px}"
            "h2{margin-top:28px;color:#1677ff}"
            "table{border-collapse:collapse;width:100%;margin:14px 0;font-size:13px}"
            "th,td{border:1px solid #e0e0e0;padding:7px 10px;text-align:left}"
            "th{background:#fafafa;font-weight:600}"
            "tr:nth-child(even){background:#fcfcfc}"
            "code{background:#f5f5f5;padding:1px 5px;border-radius:3px;font-size:12px}"
            "blockquote{border-left:4px solid #faad14;background:#fffbe6;margin:10px 0;padding:10px 14px}"
            "ul{padding-left:22px}</style></head><body>" + body + "</body></html>"
        )

    def to_csv(self, path: str) -> str:
        """导出报告明细为 CSV。"""
        df = pd.DataFrame(self.csv_rows)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path


def markdown_to_html(md: str) -> str:
    """极简 Markdown → HTML（支持标题/表格/列表/引用/粗体/代码）。"""
    lines: List[str] = []
    in_table = False
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            lines.append("</ul>")
            in_list = False

    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("|") and line.endswith("|") and len(line) > 1:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(c and set(c) <= set("-: ") for c in cells):
                continue
            close_list()
            if not in_table:
                lines.append("<table>")
                in_table = True
                lines.append("<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells) + "</tr>")
            else:
                lines.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            lines.append("</table>")
            in_table = False

        if not line.strip():
            close_list()
            continue
        if line.startswith("#"):
            close_list()
            level = len(line) - len(line.lstrip("#"))
            level = min(max(level, 1), 6)
            lines.append(f"<h{level}>{_inline(line[level:].strip())}</h{level}>")
        elif line.startswith(">"):
            close_list()
            lines.append(f"<blockquote>{_inline(line[1:].strip())}</blockquote>")
        elif line.lstrip().startswith(("- ", "* ")):
            if not in_list:
                lines.append("<ul>")
                in_list = True
            lines.append(f"<li>{_inline(line.lstrip()[2:])}</li>")
        elif line.strip() == "---":
            close_list()
            lines.append("<hr/>")
        else:
            close_list()
            lines.append(f"<p>{_inline(line)}</p>")

    if in_table:
        lines.append("</table>")
    close_list()
    return "\n".join(lines)


def _inline(text: str) -> str:
    """处理行内粗体与代码。"""
    import re

    escaped = _html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
    return escaped


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    """数值格式化，None/NaN 显示为 ``-``。"""
    if value is None:
        return "-"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f != f or f in (float("inf"), float("-inf")):
        return "-"
    return f"{f:.{digits}f}{suffix}"


def _state_cell(state: str) -> str:
    """状态单元格（带符号）。"""
    return f"{STATE_MARKS.get(state, '·')} {state}"


class ReportBuilder:
    """报告生成器。

    :param storage: 追踪存储
    :param name_resolver: 名称解析器
    """

    def __init__(
        self,
        storage: TrackerStorage,
        name_resolver: Optional[NameResolver] = None,
    ) -> None:
        self.storage = storage
        self.names = name_resolver or NameResolver()

    # ==================================================================
    # 每日追踪报告
    # ==================================================================
    def daily(
        self,
        date: Optional[str] = None,
        group: Optional[str] = None,
        include_holdings: bool = True,
    ) -> Report:
        """生成每日追踪报告。

        :param date: 报告日期（默认取状态表中的最新数据日期）
        :param group: 只统计指定分组
        :param include_holdings: 是否包含持仓盈亏章节
        """
        target = self._resolve_date(date)
        states = self.storage.list_states(group=group)
        if not states:
            md = self._empty_markdown("每日追踪报告", target, "关注池为空，请先添加关注股票。")
            return Report(
                kind="daily", title=f"每日追踪报告 · {target}", markdown=md,
                period={"date": target}, summary={},
            )

        history = self.storage.list_history(start=target, end=target, limit=1000)
        signals = self.storage.list_signals(start=target, end=target, limit=1000)
        alerts = self.storage.list_alerts(start=target, end=target, limit=1000)

        if group:
            codes = {s["code"] for s in states}
            history = [h for h in history if h["code"] in codes]
            signals = [s for s in signals if s["code"] in codes]
            alerts = [a for a in alerts if a["code"] in codes]

        new_signals = [s for s in signals if s.get("is_new")]
        buy_signals = [s for s in new_signals if s.get("signal", 0) > 0]
        sell_signals = [s for s in new_signals if s.get("signal", 0) < 0]
        risk_alerts = [a for a in alerts if a.get("category") == "risk"]

        lines: List[str] = [f"# 每日追踪报告 · {target}", ""]
        lines.append(
            f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}　|　"
            f"关注股票：{len(states)} 只　|　"
            f"状态变更：{len(history)} 次　|　"
            f"新增信号：{len(new_signals)} 个　|　"
            f"风险提醒：{len(risk_alerts)} 条"
        )
        lines.append("")

        # ---------------- 今日概览 ----------------
        lines.append("## 一、今日概览")
        lines.append("")
        lines.append("| 指标 | 数量 |")
        lines.append("|---|---|")
        lines.append(f"| 关注股票总数 | {len(states)} |")
        lines.append(f"| 状态变更次数 | {len(history)} |")
        lines.append(f"| 新增买入信号 | {len(buy_signals)} |")
        lines.append(f"| 新增卖出信号 | {len(sell_signals)} |")
        lines.append(f"| 风险提醒 | {len(risk_alerts)} |")
        pos_states = [s for s in states if s["state"] in ("买入", "持仓", "减仓")]
        lines.append(f"| 当前持仓中 | {len(pos_states)} |")
        lines.append("")

        # ---------------- 新增信号 ----------------
        lines.append("## 二、今日新增信号")
        lines.append("")
        if new_signals:
            lines.append("| 代码 | 名称 | 策略 | 信号 | 价格 | 涨跌幅 | 触发原因 |")
            lines.append("|---|---|---|---|---|---|---|")
            for s in new_signals:
                lines.append(
                    f"| {s['code']} | {s['name']} | {s['strategy']} | {s['signal_text']} | "
                    f"{_fmt(s.get('price'))} | {_fmt((s.get('pct_change') or 0) * 100, 2, '%')} | "
                    f"{s.get('reason', '')} |"
                )
        else:
            lines.append("> 今日没有新的策略信号。")
        lines.append("")

        # ---------------- 状态变更 ----------------
        lines.append("## 三、今日状态变更")
        lines.append("")
        if history:
            lines.append("| 代码 | 名称 | 原状态 | 新状态 | 价格 | 变更原因 |")
            lines.append("|---|---|---|---|---|---|")
            for h in history:
                lines.append(
                    f"| {h['code']} | {h['name']} | {_state_cell(h['old_state'])} | "
                    f"{_state_cell(h['new_state'])} | {_fmt(h.get('price'))} | {h.get('reason', '')} |"
                )
        else:
            lines.append("> 今日没有状态变更。")
        lines.append("")

        # ---------------- 风险提醒 ----------------
        lines.append("## 四、风险提醒")
        lines.append("")
        if risk_alerts:
            lines.append("| 代码 | 名称 | 级别 | 类型 | 内容 |")
            lines.append("|---|---|---|---|---|")
            for a in risk_alerts:
                lines.append(
                    f"| {a['code']} | {a['name']} | {RISK_LABELS.get(a['level'], a['level'])} | "
                    f"{a['title']} | {a['message']} |"
                )
        else:
            lines.append("> 今日没有风险提醒。")
        lines.append("")

        # ---------------- 持仓盈亏 ----------------
        if include_holdings:
            lines.append("## 五、持仓盈亏")
            lines.append("")
            holdings = [s for s in states if s.get("cost_price")]
            if holdings:
                lines.append("| 代码 | 名称 | 成本价 | 现价 | 持股数 | 浮动盈亏 | 盈亏比例 | 持仓天数 | 状态 |")
                lines.append("|---|---|---|---|---|---|---|---|---|")
                for s in sorted(holdings, key=lambda x: (x.get("unrealized_pnl") or 0)):
                    lines.append(
                        f"| {s['code']} | {s['name']} | {_fmt(s.get('cost_price'))} | "
                        f"{_fmt(s.get('price'))} | {s.get('shares') or '-'} | "
                        f"{_fmt(s.get('unrealized_pnl'))} | "
                        f"{_fmt((s.get('unrealized_pct') or 0), 2, '%')} | "
                        f"{s.get('hold_days') if s.get('hold_days') is not None else '-'} | "
                        f"{_state_cell(s['state'])} |"
                    )
                total_pnl = sum(float(s.get("unrealized_pnl") or 0) for s in holdings)
                lines.append("")
                lines.append(f"**合计浮动盈亏：{_fmt(total_pnl)} 元**")
            else:
                lines.append("> 关注池中没有录入成本价的持仓。")
            lines.append("")

        summary = {
            "date": target,
            "watch_count": len(states),
            "state_changes": len(history),
            "new_signals": len(new_signals),
            "buy_signals": len(buy_signals),
            "sell_signals": len(sell_signals),
            "risk_alerts": len(risk_alerts),
            "holding_count": len(pos_states),
        }
        return Report(
            kind="daily",
            title=f"每日追踪报告 · {target}",
            markdown="\n".join(lines),
            period={"date": target},
            summary=summary,
            csv_rows=history + new_signals,
        )

    # ==================================================================
    # 区间追踪报告
    # ==================================================================
    def range_report(
        self,
        code: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        group: Optional[str] = None,
    ) -> Report:
        """生成区间追踪报告。

        :param code: 指定股票代码；为空则统计整个关注池
        :param start: 起始日期
        :param end: 结束日期
        :param group: 分组过滤（code 为空时生效）
        """
        today = datetime.now().strftime("%Y-%m-%d")
        start = start or (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        end = end or today

        history = self.storage.list_history(code=code, start=start, end=end, limit=2000, order="asc")
        signals = self.storage.list_signals(code=code, start=start, end=end, limit=2000, order="asc")

        if group and not code:
            codes = {w["code"] for w in self.storage.list_watch(group=group)}
            history = [h for h in history if h["code"] in codes]
            signals = [s for s in signals if s["code"] in codes]

        title = f"区间追踪报告 · {code or '全部关注池'}"
        lines: List[str] = [f"# {title}", ""]
        lines.append(f"- **区间**：{start} ~ {end}")
        lines.append(f"- **状态变更次数**：{len(history)}")
        lines.append(f"- **信号数量**：{len(signals)}")
        lines.append("")

        # ---------------- 状态时间线 ----------------
        lines.append("## 一、状态时间线")
        lines.append("")
        if history:
            for h in history:
                lines.append(
                    f"- **{h.get('trade_date') or '-'}**　{h['code']} {h['name']}　"
                    f"{_state_cell(h['old_state'])} → {_state_cell(h['new_state'])}　"
                    f"（{h.get('reason', '')}）"
                )
        else:
            lines.append("> 区间内没有状态变更记录。")
        lines.append("")

        # ---------------- 信号列表 ----------------
        lines.append("## 二、信号明细")
        lines.append("")
        if signals:
            lines.append("| 日期 | 代码 | 名称 | 策略 | 信号 | 价格 | 涨跌幅 | 是否新信号 | 原因 |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
            for s in signals:
                lines.append(
                    f"| {s.get('trade_date')} | {s['code']} | {s['name']} | {s['strategy']} | "
                    f"{s['signal_text']} | {_fmt(s.get('price'))} | "
                    f"{_fmt((s.get('pct_change') or 0) * 100, 2, '%')} | "
                    f"{'是' if s.get('is_new') else '否'} | {s.get('reason', '')} |"
                )
        else:
            lines.append("> 区间内没有策略信号。")
        lines.append("")

        # ---------------- 状态分布 ----------------
        lines.append("## 三、状态分布")
        lines.append("")
        dist = self._state_distribution(history)
        if dist:
            lines.append("| 状态 | 出现次数 |")
            lines.append("|---|---|")
            for state, count in sorted(dist.items(), key=lambda kv: -kv[1]):
                lines.append(f"| {_state_cell(state)} | {count} |")
        else:
            lines.append("> 无数据。")
        lines.append("")

        summary = {
            "start": start, "end": end, "code": code or "",
            "state_changes": len(history), "signals": len(signals),
        }
        return Report(
            kind="range",
            title=title,
            markdown="\n".join(lines),
            period={"start": start, "end": end, "code": code or ""},
            summary=summary,
            csv_rows=history,
        )

    # ==================================================================
    # 汇总报告
    # ==================================================================
    def summary(self, group: Optional[str] = None, states: Optional[Sequence[str]] = None) -> Report:
        """生成关注池汇总报告。

        :param group: 分组过滤
        :param states: 只包含指定状态
        """
        rows = self.storage.list_states(group=group, states=states)
        watch = {w["code"]: w for w in self.storage.list_watch(group=group)}

        lines: List[str] = ["# 关注池汇总报告", ""]
        lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}　|　共 {len(rows)} 只")
        lines.append("")

        # ---------------- 状态分布 ----------------
        dist: Dict[str, int] = {}
        risk_dist: Dict[str, int] = {}
        for r in rows:
            dist[r["state"]] = dist.get(r["state"], 0) + 1
            risk_dist[r.get("risk_level", "normal")] = risk_dist.get(r.get("risk_level", "normal"), 0) + 1

        lines.append("## 一、状态分布")
        lines.append("")
        lines.append("| 状态 | 数量 |")
        lines.append("|---|---|")
        for state, count in sorted(dist.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {_state_cell(state)} | {count} |")
        lines.append("")
        lines.append("| 风险等级 | 数量 |")
        lines.append("|---|---|")
        for level, count in sorted(risk_dist.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {RISK_LABELS.get(level, level)} | {count} |")
        lines.append("")

        # ---------------- 明细 ----------------
        lines.append("## 二、关注池明细")
        lines.append("")
        if rows:
            lines.append(
                "| 代码 | 名称 | 分组 | 当前状态 | 上次状态 | 状态变更时间 | 数据日期 | "
                "策略 | 信号 | 现价 | 涨跌幅 | 风险 | 成本价 | 浮盈% |"
            )
            lines.append("|" + "---|" * 15)
            for r in rows:
                lines.append(
                    f"| {r['code']} | {r['name']} | {r['group']} | {_state_cell(r['state'])} | "
                    f"{r.get('prev_state') or '-'} | {r.get('state_changed_at') or '-'} | "
                    f"{r.get('data_date') or '-'} | {r.get('strategy') or '-'} | "
                    f"{r.get('signal_text') or '-'} | {_fmt(r.get('price'))} | "
                    f"{_fmt((r.get('pct_change') or 0) * 100, 2, '%')} | "
                    f"{RISK_LABELS.get(r.get('risk_level'), r.get('risk_level'))} | "
                    f"{_fmt(r.get('cost_price'))} | "
                    f"{_fmt((r.get('unrealized_pct') or 0), 2, '%')} |"
                )
        else:
            lines.append("> 关注池为空。")
        lines.append("")

        # ---------------- 关键指标 ----------------
        lines.append("## 三、关键指标")
        lines.append("")
        if rows:
            lines.append("| 代码 | 名称 | 状态 | 关键因子 |")
            lines.append("|---|---|---|---|")
            for r in rows:
                factors = r.get("factors") or {}
                text = "，".join(f"{k}={_fmt(v, 4)}" for k, v in list(factors.items())[:6]) or "-"
                lines.append(f"| {r['code']} | {r['name']} | {_state_cell(r['state'])} | {text} |")
        lines.append("")

        summary = {
            "total": len(rows),
            "state_distribution": dist,
            "risk_distribution": risk_dist,
            "watch_total": len(watch),
        }
        return Report(
            kind="summary",
            title="关注池汇总报告",
            markdown="\n".join(lines),
            summary=summary,
            csv_rows=rows,
        )

    # ==================================================================
    # 选股 / 回测报告包装
    # ==================================================================
    def screening(self, result: Any) -> Report:
        """把选股结果包装成报告对象。"""
        md = result.to_markdown() if hasattr(result, "to_markdown") else str(result)
        rows = result.to_rows() if hasattr(result, "to_rows") else []
        data = result.to_dict() if hasattr(result, "to_dict") else {}
        return Report(
            kind="screening",
            title=f"选股报告 · {data.get('strategy_name', '')}",
            markdown=md,
            period={"date": data.get("end") or data.get("start")},
            summary={
                "matched": data.get("matched", 0),
                "total_scanned": data.get("total_scanned", 0),
            },
            csv_rows=rows,
        )

    def backtest(self, result: Any) -> Report:
        """把回测结果包装成报告对象。"""
        md = result.to_markdown() if hasattr(result, "to_markdown") else str(result)
        data = result.to_dict(include_curve=False) if hasattr(result, "to_dict") else {}
        return Report(
            kind="backtest",
            title=f"回测报告 · {data.get('strategy_name', '')}",
            markdown=md,
            period={"start": data.get("start"), "end": data.get("end")},
            summary=data.get("metrics", {}),
            csv_rows=data.get("trades", []),
        )

    # ==================================================================
    # 内部工具
    # ==================================================================
    def _resolve_date(self, date: Optional[str]) -> str:
        """确定报告日期：未指定时取状态表中最新数据日期。"""
        if date:
            return pd.Timestamp(date).strftime("%Y-%m-%d")
        rows = self.storage.list_states()
        dates = [r.get("data_date") for r in rows if r.get("data_date")]
        if dates:
            return max(dates)
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _state_distribution(history: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        """统计历史记录中的状态分布。"""
        dist: Dict[str, int] = {}
        for h in history:
            key = h.get("new_state", "")
            if key:
                dist[key] = dist.get(key, 0) + 1
        return dist

    @staticmethod
    def _empty_markdown(title: str, date: str, message: str) -> str:
        """生成空报告。"""
        return "\n".join([f"# {title} · {date}", "", f"> {message}", ""])
