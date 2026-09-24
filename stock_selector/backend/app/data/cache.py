"""本地数据缓存模块。

设计目标：
    * 缓存以「代码 + 周期」为粒度保存 **完整历史**，按日期区间过滤在内存中完成，
      避免为不同区间重复落盘；
    * 通过源文件 ``mtime`` 判断缓存是否刷新，通达信数据更新后可自动失效；
    * 优先使用 parquet（体积小、读取快），缺少 pyarrow 时自动降级为 pickle。
"""

from __future__ import annotations

import json
import pickle
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..core.logging import get_logger
from .tdx_reader import normalize_code, split_code

logger = get_logger("cache")


def _parquet_available() -> bool:
    """检测 parquet 引擎是否可用。"""
    try:
        import pyarrow  # noqa: F401

        return True
    except Exception:
        try:
            import fastparquet  # noqa: F401

            return True
        except Exception:
            return False


class BarCache:
    """K 线磁盘缓存。

    :param cache_dir: 缓存根目录
    :param fmt: ``parquet`` 或 ``pickle``；``parquet`` 不可用时自动降级
    """

    def __init__(self, cache_dir: Path | str, fmt: str = "parquet") -> None:
        self.root = Path(cache_dir)
        self.bars_dir = self.root / "bars"
        self.bars_dir.mkdir(parents=True, exist_ok=True)
        requested = (fmt or "parquet").lower()
        if requested == "parquet" and not _parquet_available():
            logger.warning("未检测到 pyarrow/fastparquet，缓存格式自动降级为 pickle")
            requested = "pickle"
        self.fmt = requested
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    @property
    def ext(self) -> str:
        """缓存文件扩展名。"""
        return ".parquet" if self.fmt == "parquet" else ".pkl"

    def _paths(self, code: str, freq: str) -> Tuple[Path, Path]:
        """返回 ``(数据文件, 元信息文件)`` 路径。"""
        std = normalize_code(code)
        symbol, market = split_code(std)
        d = self.bars_dir / market
        d.mkdir(parents=True, exist_ok=True)
        stem = f"{market}{symbol}_{freq}"
        return d / f"{stem}{self.ext}", d / f"{stem}.meta.json"

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def save(
        self,
        code: str,
        freq: str,
        df: pd.DataFrame,
        source_mtime: Optional[float] = None,
        adjust: str = "none",
    ) -> None:
        """写入缓存。

        :param code: 股票代码
        :param freq: 周期，``daily`` 或 ``5min`` 等
        :param df: K 线数据
        :param source_mtime: 源文件修改时间，用于新鲜度判断
        :param adjust: 复权模式（作为缓存元信息之一）
        """
        if df is None or len(df) == 0:
            return
        data_path, meta_path = self._paths(code, freq)
        with self._lock:
            try:
                if self.fmt == "parquet":
                    df.to_parquet(data_path, index=False)
                else:
                    with open(data_path, "wb") as fp:
                        pickle.dump(df, fp, protocol=pickle.HIGHEST_PROTOCOL)
            except Exception as exc:
                logger.warning("缓存写入失败（%s/%s）：%s", code, freq, exc)
                # 尝试降级为 pickle，保证缓存功能可用
                if self.fmt == "parquet":
                    try:
                        data_path = data_path.with_suffix(".pkl")
                        with open(data_path, "wb") as fp:
                            pickle.dump(df, fp, protocol=pickle.HIGHEST_PROTOCOL)
                        self.fmt = "pickle"
                    except Exception:  # pragma: no cover
                        return
                else:
                    return

            meta = {
                "code": normalize_code(code),
                "freq": freq,
                "adjust": adjust,
                "rows": int(len(df)),
                "start": str(pd.Timestamp(df["datetime"].iloc[0])),
                "end": str(pd.Timestamp(df["datetime"].iloc[-1])),
                "source_mtime": float(source_mtime or 0.0),
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "format": self.fmt,
            }
            try:
                with open(meta_path, "w", encoding="utf-8") as fp:
                    json.dump(meta, fp, ensure_ascii=False, indent=2)
            except Exception as exc:  # pragma: no cover
                logger.debug("缓存元信息写入失败：%s", exc)

    def load(self, code: str, freq: str) -> Optional[pd.DataFrame]:
        """读取缓存，不存在或损坏时返回 None。"""
        data_path, _ = self._paths(code, freq)
        if not data_path.exists():
            # 尝试另一种格式
            alt = data_path.with_suffix(".pkl" if self.ext == ".parquet" else ".parquet")
            if alt.exists():
                data_path = alt
            else:
                return None
        with self._lock:
            try:
                if data_path.suffix == ".parquet":
                    df = pd.read_parquet(data_path)
                else:
                    with open(data_path, "rb") as fp:
                        df = pickle.load(fp)
                if not isinstance(df, pd.DataFrame) or len(df) == 0:
                    return None
                df["datetime"] = pd.to_datetime(df["datetime"])
                return df
            except Exception as exc:
                logger.warning("缓存读取失败（%s/%s）：%s", code, freq, exc)
                return None

    def meta(self, code: str, freq: str) -> Optional[Dict[str, Any]]:
        """读取缓存元信息。"""
        _, meta_path = self._paths(code, freq)
        if not meta_path.exists():
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:  # pragma: no cover
            return None

    def is_fresh(
        self,
        code: str,
        freq: str,
        source_mtime: Optional[float] = None,
        adjust: Optional[str] = None,
    ) -> bool:
        """判断缓存是否仍然有效。

        :param code: 股票代码
        :param freq: 周期
        :param source_mtime: 源文件当前 mtime
        :param adjust: 复权模式，与缓存记录不一致时视为失效
        :return: 是否可直接使用缓存
        """
        m = self.meta(code, freq)
        if not m:
            return False
        if adjust is not None and m.get("adjust") != adjust:
            return False
        if source_mtime is not None:
            if float(m.get("source_mtime", 0.0)) < float(source_mtime) - 1e-6:
                return False
        data_path, _ = self._paths(code, freq)
        if not data_path.exists():
            alt = data_path.with_suffix(".pkl" if self.ext == ".parquet" else ".parquet")
            if not alt.exists():
                return False
        return True

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def remove(self, code: str, freq: Optional[str] = None) -> int:
        """删除指定股票的缓存。

        :param code: 股票代码
        :param freq: 为 None 时删除该股票的所有周期缓存
        :return: 删除的文件数
        """
        std = normalize_code(code)
        symbol, market = split_code(std)
        d = self.bars_dir / market
        if not d.is_dir():
            return 0
        count = 0
        pattern = f"{market}{symbol}_{freq}.*" if freq else f"{market}{symbol}_*"
        for f in d.glob(pattern):
            try:
                f.unlink()
                count += 1
            except Exception:  # pragma: no cover
                pass
        return count

    def clear(self) -> int:
        """清空全部缓存。

        :return: 删除的文件数
        """
        count = sum(1 for _ in self.bars_dir.rglob("*") if _.is_file())
        if self.bars_dir.exists():
            shutil.rmtree(self.bars_dir, ignore_errors=True)
        self.bars_dir.mkdir(parents=True, exist_ok=True)
        logger.info("已清空缓存，共删除 %d 个文件", count)
        return count

    def info(self, limit: int = 200) -> Dict[str, Any]:
        """返回缓存概览信息。

        :param limit: 最多列出多少条明细
        :return: 含总量、体积、明细列表的字典
        """
        entries: List[Dict[str, Any]] = []
        total_size = 0
        if self.bars_dir.exists():
            for meta_file in self.bars_dir.rglob("*.meta.json"):
                # 元信息文件名形如 sz000001_daily.meta.json
                stem = meta_file.name.replace(".meta.json", "")
                # 数据文件可能是 parquet 或 pickle，两种都探测
                size = 0
                for suffix in (".parquet", ".pkl"):
                    data_file = meta_file.with_name(stem + suffix)
                    if data_file.exists():
                        size += data_file.stat().st_size
                total_size += size
                try:
                    with open(meta_file, "r", encoding="utf-8") as fp:
                        m = json.load(fp)
                except Exception:  # pragma: no cover
                    continue
                m["size"] = size
                entries.append(m)

        entries.sort(key=lambda e: (e.get("code", ""), e.get("freq", "")))
        return {
            "cache_dir": str(self.root),
            "format": self.fmt,
            "count": len(entries),
            "total_size": total_size,
            "total_size_mb": round(total_size / 1024 / 1024, 2),
            "entries": entries[:limit],
        }
