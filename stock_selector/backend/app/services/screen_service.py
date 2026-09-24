"""选股服务：把 HTTP 请求参数翻译成 :class:`Screener` 调用。"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from ..core.config import Config
from ..core.logging import get_logger
from ..selector.screener import Screener, ScreeningResult
from ..strategies.registry import get_strategy, list_strategies
from .data_service import DataService

logger = get_logger("screen_service")


class ScreenService:
    """选股服务。

    :param config: 全局配置
    :param data_service: 数据服务
    """

    def __init__(self, config: Config, data_service: DataService) -> None:
        self.config = config
        self.data = data_service
        self._lock = threading.RLock()
        self._results: Dict[str, ScreeningResult] = {}

    # ------------------------------------------------------------------
    @property
    def screener(self) -> Screener:
        """构造选股器（每次使用最新配置，保证设置修改后立即生效）。"""
        return Screener(self.data.reader, self.data.names, self.data.basics)

    def strategies(self) -> List[Dict[str, Any]]:
        """返回策略列表与当前配置的默认参数。"""
        items = list_strategies()
        for item in items:
            cfg_params = self.config.get(f"strategy.{item['name']}", {})
            if isinstance(cfg_params, dict):
                item["config_params"] = cfg_params
                merged = dict(item["default_params"])
                merged.update(cfg_params)
                item["effective_params"] = merged
            else:  # pragma: no cover
                item["config_params"] = {}
                item["effective_params"] = dict(item["default_params"])
        return items

    # ------------------------------------------------------------------
    def run(
        self,
        strategy: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        date: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        scan_all: bool = False,
        universe: Optional[List[str]] = None,
        min_bars: Optional[int] = None,
        max_results: Optional[int] = None,
        only_buy: bool = True,
        limit_universe: Optional[int] = None,
        progress: bool = False,
    ) -> ScreeningResult:
        """执行选股。

        参数缺省值来自 ``config.yaml`` 的 ``screen`` 段。
        """
        strat = get_strategy(strategy, params or None)
        codes = self.data.resolve_universe(universe, limit=limit_universe)

        min_bars = int(min_bars if min_bars is not None else self.config.get("screen.min_bars", 60))
        max_results = int(
            max_results if max_results is not None else self.config.get("screen.max_results", 200)
        )

        result = self.screener.run(
            strategy=strat,
            date=date,
            universe=codes,
            params=params or None,
            min_bars=min_bars,
            max_results=max_results,
            only_buy=only_buy,
            limit_universe=limit_universe,
            progress=progress,
            start=start,
            end=end,
            scan_all=scan_all,
        )
        self._store(result)
        return result

    # ------------------------------------------------------------------
    def _store(self, result: ScreeningResult) -> str:
        """把结果保存在内存中，便于后续导出。"""
        rid = uuid.uuid4().hex[:12]
        with self._lock:
            self._results[rid] = result
            # 只保留最近 20 份，避免内存膨胀
            if len(self._results) > 20:
                for key in list(self._results.keys())[:-20]:
                    self._results.pop(key, None)
        return rid

    def get_result(self, result_id: str) -> Optional[ScreeningResult]:
        """按 ID 获取选股结果。"""
        with self._lock:
            return self._results.get(result_id)

    def last_result(self) -> Optional[ScreeningResult]:
        """返回最近一次选股结果。"""
        with self._lock:
            if not self._results:
                return None
            return list(self._results.values())[-1]
