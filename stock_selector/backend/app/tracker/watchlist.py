"""关注池领域服务。

在 :class:`~app.tracker.storage.TrackerStorage` 之上补充业务规则：
    * 代码标准化与校验；
    * 名称自动补全（若配置了名称映射文件）；
    * 从选股结果 / 回测持仓一键导入；
    * 分组、标签、持仓信息维护；
    * 导出 CSV。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..core.logging import get_logger
from ..data.names import NameResolver
from ..data.tdx_reader import normalize_code
from .storage import TrackerStorage

logger = get_logger("watchlist")

# 一键导入时的默认分组
DEFAULT_GROUP = "默认分组"


class Watchlist:
    """关注池服务。

    :param storage: 追踪存储
    :param name_resolver: 名称解析器（可选）
    """

    def __init__(
        self,
        storage: TrackerStorage,
        name_resolver: Optional[NameResolver] = None,
    ) -> None:
        self.storage = storage
        self.names = name_resolver or NameResolver()

    # ------------------------------------------------------------------
    # 增删改查
    # ------------------------------------------------------------------
    def add(
        self,
        code: str,
        name: str = "",
        group: str = DEFAULT_GROUP,
        tags: Optional[Iterable[str]] = None,
        note: str = "",
        strategy: str = "",
        strategy_params: Optional[Dict[str, Any]] = None,
        cost_price: Optional[float] = None,
        shares: Optional[int] = None,
        target_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        source: str = "manual",
        overwrite: bool = False,
        state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """添加关注项。

        :param code: 股票代码（任意可识别写法）
        :param name: 名称；为空时尝试从名称映射补全
        :param group: 分组
        :param tags: 标签
        :param note: 备注
        :param strategy: 绑定策略
        :param strategy_params: 策略参数
        :param cost_price: 成本价
        :param shares: 持仓股数
        :param target_price: 目标价
        :param stop_price: 止损价
        :param source: 来源标记
        :param overwrite: 已存在时是否覆盖
        :param state: 初始状态
        :return: 关注项字典
        """
        std = normalize_code(code)
        resolved_name = name or self.names.resolve(std)
        return self.storage.add_watch(
            code=std,
            name=resolved_name,
            group=group or DEFAULT_GROUP,
            tags=list(tags or []),
            note=note or "",
            strategy=strategy or "",
            strategy_params=strategy_params,
            cost_price=cost_price,
            shares=shares,
            target_price=target_price,
            stop_price=stop_price,
            source=source,
            overwrite=overwrite,
            state=state,
        )

    def add_many(
        self,
        items: Sequence[Dict[str, Any]],
        group: str = DEFAULT_GROUP,
        source: str = "manual",
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """批量添加。

        :param items: 每项至少包含 ``code``，可含 ``name/tags/note/...``
        :param group: 条目未指定分组时使用的分组
        :param source: 条目未指定来源时使用的来源标记
        :param overwrite: 已存在时是否覆盖
        :return: ``{"added": n, "failed": [...], "items": [...]}``
        """
        added: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for item in items:
            try:
                # 过滤 None，避免把未提供的字段覆盖成空值
                payload = {k: v for k, v in dict(item).items() if v is not None}
                # overwrite 由本方法统一控制，防止与 **payload 展开时关键字重复
                payload.pop("overwrite", None)
                # 分组 / 来源：条目未显式指定（缺失或为默认值）时采用批量参数
                if not payload.get("group") or payload["group"] == DEFAULT_GROUP:
                    payload["group"] = group
                if not payload.get("source") or payload["source"] == "manual":
                    payload["source"] = source
                added.append(self.add(overwrite=overwrite, **payload))
            except Exception as exc:
                logger.warning("批量添加失败：%s -> %s", item, exc)
                failed.append({"item": item, "error": str(exc)})
        return {"added": len(added), "failed": failed, "items": added}

    def remove(self, code: str, keep_state: bool = False) -> bool:
        """删除关注项。"""
        return self.storage.remove_watch(code, keep_state=keep_state)

    def get(self, code: str) -> Optional[Dict[str, Any]]:
        """获取关注项。"""
        return self.storage.get_watch(code)

    def update(self, code: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """更新关注项字段。"""
        if "cost_price" in fields and fields["cost_price"] is not None:
            fields["cost_price"] = float(fields["cost_price"])
        if "shares" in fields and fields["shares"] is not None:
            fields["shares"] = int(fields["shares"])
        return self.storage.update_watch(code, **fields)

    def list(
        self,
        group: Optional[str] = None,
        enabled_only: bool = False,
        keyword: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """列出关注项。"""
        return self.storage.list_watch(
            group=group, enabled_only=enabled_only, keyword=keyword, tags=tags
        )

    def groups(self) -> List[Dict[str, Any]]:
        """列出分组。"""
        return self.storage.list_groups()

    def codes(self, enabled_only: bool = True) -> List[str]:
        """返回关注池代码列表。"""
        return self.storage.watch_codes(enabled_only=enabled_only)

    def set_position(
        self,
        code: str,
        cost_price: Optional[float] = None,
        shares: Optional[int] = None,
        target_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """设置持仓信息。"""
        return self.storage.update_watch(
            code,
            cost_price=cost_price,
            shares=shares,
            target_price=target_price,
            stop_price=stop_price,
        )

    def clear_position(self, code: str) -> Optional[Dict[str, Any]]:
        """清空持仓信息。"""
        return self.storage.update_watch(
            code, cost_price=None, shares=0, target_price=None, stop_price=None
        )

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------
    def import_from_screen(
        self,
        result: Any,
        group: str = "选股结果",
        strategy: Optional[str] = None,
        note: str = "",
    ) -> Dict[str, Any]:
        """从选股结果导入关注池。

        :param result: :class:`~app.selector.screener.ScreeningResult` 或等价字典
        :param group: 目标分组
        :param strategy: 绑定的策略名
        :return: 导入结果统计
        """
        items = getattr(result, "results", None)
        if items is None and isinstance(result, dict):
            items = result.get("results", [])
        items = items or []

        payload: List[Dict[str, Any]] = []
        for item in items:
            data = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            payload.append(
                {
                    "code": data.get("code", ""),
                    "name": data.get("name", ""),
                    "strategy": strategy or data.get("strategy", ""),
                    "note": note or f"选股信号：{data.get('reason_text', '')}",
                    "tags": ["选股"],
                }
            )
        stats = self.add_many(payload, group=group, source="screen")
        logger.info("从选股结果导入关注池：成功 %d，失败 %d", stats["added"], len(stats["failed"]))
        return stats

    def import_from_backtest(
        self,
        result: Any,
        group: str = "回测持仓",
        only_open: bool = True,
    ) -> Dict[str, Any]:
        """从回测持仓记录导入关注池。

        :param result: :class:`~app.backtest.engine.BacktestResult` 或等价字典
        :param group: 目标分组
        :param only_open: 只导入末尾仍持有的标的
        """
        positions = getattr(result, "positions", None)
        if positions is None and isinstance(result, dict):
            positions = result.get("positions", [])
        positions = positions or []
        if not positions:
            return {"added": 0, "failed": [], "items": []}

        latest_date = max((p.get("date") or "" for p in positions), default="")
        selected = [p for p in positions if (p.get("date") == latest_date)] if only_open else positions

        payload: List[Dict[str, Any]] = []
        seen = set()
        for p in selected:
            code = p.get("code", "")
            if not code or code in seen:
                continue
            seen.add(code)
            payload.append(
                {
                    "code": code,
                    "name": p.get("name", ""),
                    "cost_price": p.get("avg_cost"),
                    "shares": p.get("shares"),
                    "note": f"回测持仓导入（{latest_date}）",
                    "tags": ["回测持仓"],
                }
            )
        return self.add_many(payload, group=group, source="backtest")

    def import_positions(
        self,
        positions: Sequence[Dict[str, Any]],
        group: str = "回测持仓",
    ) -> Dict[str, Any]:
        """从持仓字典列表导入。"""
        payload = [
            {
                "code": p.get("code", ""),
                "name": p.get("name", ""),
                "cost_price": p.get("avg_cost") or p.get("cost_price"),
                "shares": p.get("shares"),
                "tags": ["回测持仓"],
            }
            for p in positions
            if p.get("code")
        ]
        return self.add_many(payload, group=group, source="backtest")

    # ------------------------------------------------------------------
    def export_csv(self, path: Path | str, group: Optional[str] = None) -> Path:
        """导出关注池为 CSV。"""
        import pandas as pd

        rows = self.list(group=group)
        df = pd.DataFrame(rows)
        if len(df) > 0 and "tags" in df.columns:
            df["tags"] = df["tags"].apply(lambda v: ",".join(v or []))
        if len(df) > 0 and "strategy_params" in df.columns:
            df["strategy_params"] = df["strategy_params"].apply(lambda v: str(v or {}))
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(p, index=False, encoding="utf-8-sig")
        return p
