"""股票名称解析模块。

背景：
    通达信 ``.day`` 文件名只包含代码，不包含股票名称；名称通常存放在
    ``T0002/hq_cache`` 下的私有格式文件中，各版本差异较大、解析不可靠。

策略：
    1. 优先读取用户提供的映射文件（CSV：``code,name``），默认路径
       ``<cache_dir>/names.csv``；
    2. 内置常见指数名称；
    3. 以上都缺失时，退化为「代码即名称」，并在 Web UI 中提示用户补充映射文件。

这样既不伪造数据，也保证程序随时可用。
"""

from __future__ import annotations

import csv
import threading
from pathlib import Path
from typing import Dict, Optional

from ..core.logging import get_logger
from .tdx_reader import normalize_code

logger = get_logger("names")

# 常见指数名称（用于回测基准显示）
BUILTIN_INDEX_NAMES: Dict[str, str] = {
    "000001.SH": "上证指数",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000016.SH": "上证50",
    "000852.SH": "中证1000",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "399005.SZ": "中小100",
    "899050.BJ": "北证50",
}

# 内置映射文件的表头
_TEMPLATE_HEADER = ["code", "name"]


class NameResolver:
    """股票名称解析器（线程安全，带内存缓存）。

    :param mapping_file: 名称映射 CSV 路径
    """

    def __init__(self, mapping_file: Optional[Path | str] = None) -> None:
        self.mapping_file = Path(mapping_file) if mapping_file else None
        self._lock = threading.RLock()
        self._mapping: Dict[str, str] = {}
        self._loaded_mtime: Optional[float] = None
        self._loaded = False

    # ------------------------------------------------------------------
    def load(self, force: bool = False) -> Dict[str, str]:
        """加载映射文件（带 mtime 变更检测）。

        :param force: 强制重新读取
        :return: 名称映射字典
        """
        with self._lock:
            if self.mapping_file is None:
                self._loaded = True
                return self._mapping
            if not self.mapping_file.exists():
                self._loaded = True
                if not self._mapping:
                    logger.info(
                        "未找到股票名称映射文件 %s，将使用代码作为名称。"
                        "可调用 /api/data/names/template 生成模板后用 /api/data/names 上传。",
                        self.mapping_file,
                    )
                return self._mapping

            mtime = self.mapping_file.stat().st_mtime
            if self._loaded and not force and self._loaded_mtime == mtime:
                return self._mapping

            mapping: Dict[str, str] = {}
            try:
                with open(self.mapping_file, "r", encoding="utf-8-sig", newline="") as fp:
                    reader = csv.DictReader(fp)
                    if reader.fieldnames is None:
                        raise ValueError("空文件")
                    fields = [f.strip().lower() for f in reader.fieldnames]
                    if "code" not in fields or "name" not in fields:
                        raise ValueError("缺少 code/name 表头")
                    idx = {f: i for i, f in enumerate(fields)}
                    for row in reader:
                        values = list(row.values())
                        try:
                            code = str(values[idx["code"]]).strip()
                            name = str(values[idx["name"]]).strip()
                        except IndexError:
                            continue
                        if not code:
                            continue
                        try:
                            std = normalize_code(code)
                        except ValueError:
                            continue
                        mapping[std] = name or std
            except Exception as exc:
                logger.error("读取名称映射文件失败：%s", exc)
                self._loaded = True
                return self._mapping

            self._mapping = mapping
            self._loaded_mtime = mtime
            self._loaded = True
            logger.info("已加载 %d 条股票名称映射", len(mapping))
            return self._mapping

    # ------------------------------------------------------------------
    def resolve(self, code: str) -> str:
        """返回股票名称；找不到映射时返回代码本身。

        :param code: 股票代码
        """
        try:
            std = normalize_code(code)
        except ValueError:
            return str(code)
        if not self._loaded:
            self.load()
        name = self._mapping.get(std)
        if name:
            return name
        return BUILTIN_INDEX_NAMES.get(std, std)

    def resolve_many(self, codes) -> Dict[str, str]:
        """批量解析名称。"""
        return {normalize_code(c): self.resolve(c) for c in codes}

    @property
    def size(self) -> int:
        """已加载的映射条数。"""
        if not self._loaded:
            self.load()
        return len(self._mapping)

    @property
    def available(self) -> bool:
        """映射文件是否存在。"""
        return bool(self.mapping_file and self.mapping_file.exists())

    # ------------------------------------------------------------------
    def write_template(self, path: Optional[Path | str] = None) -> Path:
        """生成名称映射模板文件（含表头与常用指数示例）。

        :param path: 目标路径，默认使用 ``self.mapping_file``
        :return: 实际写入路径
        """
        target = Path(path) if path else self.mapping_file
        if target is None:
            raise ValueError("未指定映射文件路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with open(target, "w", encoding="utf-8-sig", newline="") as fp:
                writer = csv.writer(fp)
                writer.writerow(_TEMPLATE_HEADER)
                for code, name in BUILTIN_INDEX_NAMES.items():
                    writer.writerow([code, name])
        return target

    def upsert(self, items: Dict[str, str]) -> int:
        """新增或更新名称映射并写回 CSV。

        :param items: ``{code: name}``
        :return: 更新后的总条数
        """
        if self.mapping_file is None:
            raise ValueError("未配置名称映射文件路径")
        with self._lock:
            self.load(force=True)
            merged = dict(self._mapping)
            for code, name in (items or {}).items():
                try:
                    merged[normalize_code(code)] = str(name).strip()
                except ValueError:
                    continue
            self.mapping_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.mapping_file, "w", encoding="utf-8-sig", newline="") as fp:
                writer = csv.writer(fp)
                writer.writerow(_TEMPLATE_HEADER)
                for code in sorted(merged):
                    writer.writerow([code, merged[code]])
            self._mapping = merged
            self._loaded_mtime = self.mapping_file.stat().st_mtime
            self._loaded = True
            return len(merged)
