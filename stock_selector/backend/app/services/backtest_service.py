"""回测服务：封装 :class:`BacktestEngine` 的调用与结果缓存。"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Dict, List, Optional

from ..backtest.engine import BacktestEngine, BacktestResult
from ..core.config import Config
from ..core.logging import get_logger
from ..strategies.registry import get_strategy
from .data_service import DataService

logger = get_logger("backtest_service")


class BacktestService:
    """回测服务。

    :param config: 全局配置
    :param data_service: 数据服务
    """

    def __init__(self, config: Config, data_service: DataService) -> None:
        self.config = config
        self.data = data_service
        self._lock = threading.RLock()
        self._results: Dict[str, BacktestResult] = {}
        self._last_id: Optional[str] = None

    # ------------------------------------------------------------------
    def base_config(self) -> Dict[str, Any]:
        """返回 ``config.yaml`` 的 ``backtest`` 段。"""
        cfg = self.config.get("backtest", {}) or {}
        return dict(cfg)

    def run(
        self,
        start: str,
        end: str,
        strategy: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        universe: Optional[List[str]] = None,
        max_universe: Optional[int] = None,
        config_override: Optional[Dict[str, Any]] = None,
        progress: bool = False,
    ) -> BacktestResult:
        """执行回测。

        :param start: 起始日期
        :param end: 结束日期
        :param strategy: 策略名
        :param params: 策略参数
        :param universe: 股票池；为空则使用本地全部股票
        :param max_universe: 限制股票池大小
        :param config_override: 覆盖回测配置（资金、费率、仓位等）
        :param progress: 是否显示进度条
        """
        strat = get_strategy(strategy, params or None)
        codes = self.data.resolve_universe(universe)
        if max_universe:
            codes = codes[: int(max_universe)]

        cfg = self.base_config()
        cfg.update({k: v for k, v in (config_override or {}).items() if v is not None})

        engine = BacktestEngine(
            reader=self.data.reader,
            strategy=strat,
            config=cfg,
            name_resolver=self.data.names,
            basics=self.data.basics,
        )
        result = engine.run(
            codes=codes,
            start=start,
            end=end,
            params=params or None,
            progress=progress,
            max_universe=max_universe,
        )
        self._store(result)
        return result

    # ------------------------------------------------------------------
    def _store(self, result: BacktestResult) -> str:
        """缓存回测结果。"""
        rid = uuid.uuid4().hex[:12]
        with self._lock:
            self._results[rid] = result
            self._last_id = rid
            if len(self._results) > 10:
                for key in list(self._results.keys())[:-10]:
                    self._results.pop(key, None)
        return rid

    def get_result(self, result_id: Optional[str] = None) -> Optional[BacktestResult]:
        """按 ID 获取回测结果；ID 为空时返回最近一次。"""
        with self._lock:
            if result_id:
                return self._results.get(result_id)
            if self._last_id:
                return self._results.get(self._last_id)
            return None

    @property
    def last_id(self) -> Optional[str]:
        """最近一次回测的 ID。"""
        return self._last_id

    def list_results(self) -> List[Dict[str, Any]]:
        """列出已缓存的回测摘要。"""
        with self._lock:
            out = []
            for rid, res in self._results.items():
                metrics = res.performance.metrics if res.performance else {}
                out.append(
                    {
                        "id": rid,
                        "strategy": res.strategy,
                        "strategy_name": res.strategy_name,
                        "start": res.start,
                        "end": res.end,
                        "universe_size": res.universe_size,
                        "total_return": metrics.get("total_return"),
                        "max_drawdown": metrics.get("max_drawdown"),
                        "sharpe": metrics.get("sharpe"),
                    }
                )
            return out
