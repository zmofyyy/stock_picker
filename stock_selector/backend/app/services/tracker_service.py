"""追踪服务：关注池、追踪更新、回放、报告、通知的统一入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..core.config import Config
from ..core.logging import get_logger
from ..tracker.notifier import Notification, Notifier
from ..tracker.report import Report, ReportBuilder
from ..tracker.state_machine import DEFAULT_STATES, StateMachine
from ..tracker.storage import TrackerStorage
from ..tracker.tracker import StockTracker, UpdateResult
from ..tracker.watchlist import Watchlist
from .data_service import DataService

logger = get_logger("tracker_service")


class TrackerService:
    """追踪服务。

    :param config: 全局配置
    :param data_service: 数据服务
    """

    def __init__(self, config: Config, data_service: DataService) -> None:
        self.config = config
        self.data = data_service

        url = config.sqlalchemy_url("tracker.storage")
        self.storage = TrackerStorage(url)
        self.watchlist = Watchlist(self.storage, self.data.names)
        self.notifier = self._build_notifier()
        self.reports = ReportBuilder(self.storage, self.data.names)

        tracker_cfg = config.get("tracker", {}) or {}
        states = list(tracker_cfg.get("states") or DEFAULT_STATES)
        self.state_machine = StateMachine(states)
        self._tracker: Optional[StockTracker] = None
        self._tracker_states = tuple(states)

    # ------------------------------------------------------------------
    def _build_notifier(self) -> Notifier:
        """根据配置构建提醒发送器。"""
        cfg = dict(self.config.get("tracker.notify", {}) or {})
        base_dir = self.config.path.parent
        return Notifier(cfg, base_dir=base_dir)

    @property
    def tracker(self) -> StockTracker:
        """追踪器（惰性创建；状态集合变化时自动重建）。"""
        tracker_cfg = self.config.get("tracker", {}) or {}
        states = tuple(tracker_cfg.get("states") or DEFAULT_STATES)
        if self._tracker is None or states != self._tracker_states:
            self._tracker = StockTracker(
                reader=self.data.reader,
                storage=self.storage,
                config=dict(tracker_cfg),
                name_resolver=self.data.names,
                notifier=self.notifier,
                basics=self.data.basics,
            )
            self._tracker_states = states
        return self._tracker

    def refresh(self) -> None:
        """配置变更后刷新内部对象。"""
        self.notifier = self._build_notifier()
        self.watchlist = Watchlist(self.storage, self.data.names)
        self.reports = ReportBuilder(self.storage, self.data.names)
        self._tracker = None

    # ==================================================================
    # 关注池
    # ==================================================================
    def add_watch(self, **kwargs: Any) -> Dict[str, Any]:
        """添加关注项。"""
        return self.watchlist.add(**kwargs)

    def add_watch_batch(
        self,
        items: Sequence[Dict[str, Any]],
        group: str = "默认分组",
        source: str = "manual",
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """批量添加关注项。"""
        return self.watchlist.add_many(items, group=group, source=source, overwrite=overwrite)

    def remove_watch(self, code: str, keep_state: bool = False) -> bool:
        """删除关注项。"""
        return self.watchlist.remove(code, keep_state=keep_state)

    def update_watch(self, code: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """更新关注项。"""
        return self.watchlist.update(code, **fields)

    def get_watch(self, code: str) -> Optional[Dict[str, Any]]:
        """获取关注项。"""
        return self.watchlist.get(code)

    def list_watch(
        self,
        group: Optional[str] = None,
        enabled_only: bool = False,
        keyword: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """列出关注池。"""
        return self.watchlist.list(
            group=group, enabled_only=enabled_only, keyword=keyword, tags=tags
        )

    def groups(self) -> List[Dict[str, Any]]:
        """列出分组。"""
        return self.watchlist.groups()

    # ==================================================================
    # 追踪
    # ==================================================================
    def update(
        self,
        codes: Optional[Sequence[str]] = None,
        date: Optional[str] = None,
        group: Optional[str] = None,
        strategy: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        notify: bool = True,
    ) -> UpdateResult:
        """执行一次追踪更新。"""
        return self.tracker.update(
            codes=codes, date=date, group=group, strategy=strategy,
            params=params, notify=notify, trigger_type="auto",
        )

    def replay(
        self,
        codes: Optional[Sequence[str]] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        group: Optional[str] = None,
        strategy: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        reset: bool = True,
        notify: bool = False,
    ) -> UpdateResult:
        """历史回放。"""
        return self.tracker.rebuild(
            codes=codes, start=start, end=end, group=group, strategy=strategy,
            params=params, notify=notify, reset=reset,
        )

    def set_state(self, code: str, state: str, reason: str = "", note: str = "") -> Dict[str, Any]:
        """手动设置状态。"""
        return self.tracker.set_state(code, state, reason=reason, note=note)

    def pause(self, code: str, paused: bool = True) -> Optional[Dict[str, Any]]:
        """暂停 / 恢复追踪。"""
        return self.tracker.pause(code, paused=paused)

    # ==================================================================
    # 查询
    # ==================================================================
    def get_state(self, code: str) -> Optional[Dict[str, Any]]:
        """获取当前状态。"""
        return self.storage.get_state(code)

    def list_states(
        self,
        states: Optional[Sequence[str]] = None,
        group: Optional[str] = None,
        risk_level: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """列出当前状态。"""
        return self.storage.list_states(
            states=states, group=group, risk_level=risk_level, keyword=keyword
        )

    def get_history(
        self,
        code: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """查询状态历史。"""
        return self.storage.list_history(code=code, start=start, end=end, limit=limit)

    def get_timeline(self, code: str, limit: int = 100) -> Dict[str, Any]:
        """查询状态时间线。"""
        return self.tracker.timeline(code, limit=limit)

    def get_signals(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """查询信号记录。"""
        return self.storage.list_signals(**kwargs)

    def get_alerts(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """查询提醒记录。"""
        return self.storage.list_alerts(**kwargs)

    def ack_alert(self, alert_id: int) -> bool:
        """标记提醒已读。"""
        return self.storage.ack_alert(alert_id)

    def dashboard(self, date: Optional[str] = None) -> Dict[str, Any]:
        """仪表盘聚合数据。"""
        return self.tracker.dashboard(date=date)

    def add_note(self, code: str, content: str, author: str = "user") -> Dict[str, Any]:
        """添加备注。"""
        return self.storage.add_note(code, content, author=author)

    def list_notes(self, code: str, limit: int = 100) -> List[Dict[str, Any]]:
        """列出备注。"""
        return self.storage.list_notes(code, limit=limit)

    # ==================================================================
    # 报告
    # ==================================================================
    def daily_report(self, date: Optional[str] = None, group: Optional[str] = None) -> Report:
        """每日追踪报告。"""
        return self.reports.daily(date=date, group=group)

    def range_report(
        self,
        code: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        group: Optional[str] = None,
    ) -> Report:
        """区间追踪报告。"""
        return self.reports.range_report(code=code, start=start, end=end, group=group)

    def summary_report(
        self, group: Optional[str] = None, states: Optional[Sequence[str]] = None
    ) -> Report:
        """汇总报告。"""
        return self.reports.summary(group=group, states=states)

    # ==================================================================
    # 导入导出与通知
    # ==================================================================
    def import_from_screen(self, result: Any, group: str = "选股结果", strategy: Optional[str] = None) -> Dict[str, Any]:
        """从选股结果导入关注池。"""
        return self.watchlist.import_from_screen(result, group=group, strategy=strategy)

    def import_from_backtest(self, result: Any, group: str = "回测持仓", only_open: bool = True) -> Dict[str, Any]:
        """从回测持仓导入关注池。"""
        return self.watchlist.import_from_backtest(result, group=group, only_open=only_open)

    def export(self, table: str, path: Path | str, **filters: Any) -> Path:
        """导出某张表为 CSV。"""
        return self.storage.export_csv(table, path, **filters)

    def test_notify(self) -> Dict[str, Any]:
        """发送测试通知。"""
        return self.notifier.test()

    def notify_summary(self) -> Dict[str, Any]:
        """返回通知配置概览（隐藏敏感信息）。"""
        cfg = dict(self.config.get("tracker.notify", {}) or {})
        return {
            "channels": self.notifier.channels,
            "console": bool(cfg.get("console")),
            "csv": bool(cfg.get("csv")),
            "csv_path": cfg.get("csv_path", ""),
            "webhook_configured": bool(cfg.get("webhook")),
            "webhook_type": cfg.get("webhook_type", ""),
            "email_configured": bool(cfg.get("email")),
            "smtp_host": cfg.get("smtp_host", ""),
        }

    def storage_stats(self) -> Dict[str, Any]:
        """存储统计。"""
        return self.storage.stats()
