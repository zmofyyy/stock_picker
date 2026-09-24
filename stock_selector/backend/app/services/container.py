"""服务容器。

把配置、数据服务、选股服务、回测服务、追踪服务组装到一起，
作为进程内单例供 FastAPI 路由与 CLI 复用。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from ..core.config import Config, get_config
from ..core.logging import get_logger, setup_from_config
from .backtest_service import BacktestService
from .data_service import DataService
from .screen_service import ScreenService
from .tracker_service import TrackerService

logger = get_logger("container")


@dataclass
class Services:
    """服务集合。"""

    config: Config
    data: DataService
    screen: ScreenService
    backtest: BacktestService
    tracker: TrackerService

    def refresh_after_settings_change(self) -> None:
        """配置变更后刷新依赖配置的服务实例。"""
        self.data.refresh_reader()
        self.tracker.refresh()
        logger.info("服务已按最新配置刷新")


class Scheduler:
    """定时任务封装（基于 APScheduler）。

    用途：每个交易日收盘后自动执行一次追踪更新。
    """

    def __init__(self, services: Services) -> None:
        self.services = services
        self._scheduler: Optional[Any] = None
        self._last_run: Optional[str] = None
        self._last_result: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    def start(self) -> Dict[str, Any]:
        """按配置启动定时任务。"""
        cfg = self.services.config.get("tracker", {}) or {}
        if not cfg.get("enabled", True) or not cfg.get("auto_update", False):
            logger.info("追踪定时任务未启用（tracker.auto_update=false）")
            return self.status()

        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
        except Exception as exc:  # pragma: no cover - 依赖缺失
            logger.warning("APScheduler 不可用，定时任务未启动：%s", exc)
            return self.status()

        update_time = str(cfg.get("update_time", "15:30") or "15:30")
        try:
            hour, minute = [int(x) for x in update_time.split(":")[:2]]
        except Exception:
            hour, minute = 15, 30

        if self._scheduler is None:
            self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        # 先移除同名任务，避免重复
        try:
            self._scheduler.remove_job("daily_tracking")
        except Exception:
            pass
        self._scheduler.add_job(
            self.run_once,
            trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
            id="daily_tracking",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        if not self._scheduler.running:
            self._scheduler.start()
        logger.info("追踪定时任务已启动：每周一至周五 %02d:%02d", hour, minute)
        return self.status()

    def stop(self) -> Dict[str, Any]:
        """停止定时任务。"""
        if self._scheduler is not None and self._scheduler.running:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception as exc:  # pragma: no cover
                logger.warning("关闭定时任务失败：%s", exc)
        self._scheduler = None
        logger.info("追踪定时任务已停止")
        return self.status()

    # ------------------------------------------------------------------
    def run_once(self, notify: bool = True) -> Dict[str, Any]:
        """立即执行一次追踪更新（供定时任务与手动触发）。"""
        self._last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = self.services.tracker.update(notify=notify)
            self._last_result = result.to_dict()
            logger.info(
                "定时追踪完成：处理 %d 只，状态变更 %d，提醒 %d",
                result.processed, result.changed, result.alerts,
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("定时追踪执行失败：%s", exc)
            self._last_result = {"error": str(exc)}
        return self._last_result or {}

    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        """返回定时任务状态。"""
        cfg = self.services.config.get("tracker", {}) or {}
        jobs = []
        if self._scheduler is not None:
            try:
                for job in self._scheduler.get_jobs():
                    jobs.append(
                        {
                            "id": job.id,
                            "next_run": job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
                            if job.next_run_time
                            else None,
                        }
                    )
            except Exception:  # pragma: no cover
                jobs = []
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "auto_update": bool(cfg.get("auto_update", False)),
            "update_time": str(cfg.get("update_time", "15:30")),
            "running": bool(self._scheduler is not None and self._scheduler.running),
            "jobs": jobs,
            "last_run": self._last_run,
            "last_result": self._last_result,
        }


# ----------------------------------------------------------------------
# 单例
# ----------------------------------------------------------------------
_services: Optional[Services] = None
_scheduler: Optional[Scheduler] = None
_lock = threading.RLock()


def build_services(config_path: Optional[str] = None, force: bool = False) -> Services:
    """构建（或返回已有的）服务集合。

    :param config_path: 配置文件路径
    :param force: 强制重新构建
    """
    global _services
    with _lock:
        if _services is not None and not force and config_path is None:
            return _services

        cfg = get_config(config_path)
        cfg.ensure_dirs()
        setup_from_config(force=True)

        data_service = DataService(cfg)
        services = Services(
            config=cfg,
            data=data_service,
            screen=ScreenService(cfg, data_service),
            backtest=BacktestService(cfg, data_service),
            tracker=TrackerService(cfg, data_service),
        )
        _services = services
        logger.info("服务容器初始化完成，配置文件：%s", cfg.path)
        return services


def get_services() -> Services:
    """获取服务集合单例。"""
    if _services is None:
        return build_services()
    return _services


def get_scheduler() -> Scheduler:
    """获取定时任务单例。"""
    global _scheduler
    with _lock:
        if _scheduler is None:
            _scheduler = Scheduler(get_services())
        return _scheduler


def reset_services() -> None:
    """重置单例（主要供测试使用）。"""
    global _services, _scheduler
    with _lock:
        if _scheduler is not None:
            try:
                _scheduler.stop()
            except Exception:  # pragma: no cover
                pass
        _services = None
        _scheduler = None
