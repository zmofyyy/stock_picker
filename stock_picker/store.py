"""计划（买入/卖出价）与追踪列表的持久化（SQLite）。

持久化保障
----------
1. **WAL 日志模式 + ``synchronous=FULL``**：提交即落盘，进程被杀、机器掉电
   都不会丢掉已确认的写入。
2. **在线备份**：启动时、以及每次写入后（距上次超过 ``AUTO_BACKUP_MIN_INTERVAL``）
   用 SQLite 官方 backup API 生成一致性快照，存到 ``<db>/../backups``，
   滚动保留最近 ``backup_keep`` 份。
3. **可下载**：备份文件与当前库都能通过 ``/api/backup/*`` 取回本地。
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

BACKUP_DIRNAME = "backups"
#: 连续重启时不必反复备份
STARTUP_BACKUP_MIN_INTERVAL = 300.0
#: 写入触发的自动备份最小间隔（6 小时）
AUTO_BACKUP_MIN_INTERVAL = 6 * 3600.0
DEFAULT_BACKUP_KEEP = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL,
    name          TEXT DEFAULT '',
    signal_date   TEXT NOT NULL,
    base_close    REAL,          -- 放量日收盘价（卖出价的基准）
    volume        REAL,
    ma_volume     REAL,
    vol_ratio     REAL,
    buy_price     REAL,          -- 计划买入价
    sell_price    REAL,          -- 计划卖出价
    qty           INTEGER,       -- 计划数量（股）
    status        TEXT DEFAULT '待买入',
    note          TEXT DEFAULT '',
    created_at    TEXT,
    updated_at    TEXT,
    UNIQUE (code, signal_date)
);

CREATE TABLE IF NOT EXISTS watchlist (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       INTEGER,
    code          TEXT NOT NULL,
    name          TEXT DEFAULT '',
    signal_date   TEXT NOT NULL,
    base_close    REAL,
    buy_price     REAL,
    sell_price    REAL,
    qty           INTEGER,
    entry_date    TEXT,          -- 实际买入日期，空=未买入
    entry_price   REAL,          -- 实际买入价
    status        TEXT DEFAULT '关注中',
    note          TEXT DEFAULT '',
    created_at    TEXT,
    updated_at    TEXT,
    UNIQUE (code, signal_date)
);

CREATE INDEX IF NOT EXISTS idx_plans_code ON plans(code);
CREATE INDEX IF NOT EXISTS idx_watch_code ON watchlist(code);
"""

PLAN_STATUS = ["待买入", "已买入", "已卖出", "已过期", "放弃"]
WATCH_STATUS = ["关注中", "已买入", "已止盈", "已止损", "已过期", "放弃"]

PLAN_FIELDS = [
    "code", "name", "signal_date", "base_close", "volume", "ma_volume", "vol_ratio",
    "buy_price", "sell_price", "qty", "status", "note",
]
WATCH_FIELDS = [
    "plan_id", "code", "name", "signal_date", "base_close", "buy_price", "sell_price",
    "qty", "entry_date", "entry_price", "status", "note",
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """线程安全的轻量 SQLite 封装。"""

    def __init__(
        self,
        db_path: Path | str,
        *,
        backup_keep: int = DEFAULT_BACKUP_KEEP,
        auto_backup: bool = True,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.db_path.parent / BACKUP_DIRNAME
        self.backup_keep = max(1, int(backup_keep))
        self._auto_backup = bool(auto_backup)
        self._last_backup_ts = 0.0
        self._lock = threading.RLock()

        # 备份前先看清：全新库不值得备份
        pre_existing = self.db_path.is_file() and self.db_path.stat().st_size > 0

        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

        if self._auto_backup and pre_existing:
            self._maybe_backup("startup", STARTUP_BACKUP_MIN_INTERVAL)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
        # 写后按需备份：放在锁外，避免长时间持锁做文件拷贝
        if self._auto_backup:
            self._maybe_backup("auto", AUTO_BACKUP_MIN_INTERVAL)
        return cur

    # ------------------------------------------------------------------
    # 备份
    # ------------------------------------------------------------------
    def _maybe_backup(self, reason: str, min_interval: float) -> Optional[Dict[str, Any]]:
        if not self.db_path.is_file():
            return None
        if time.time() - self._last_backup_ts < min_interval:
            return None
        return self.create_backup(reason)

    def create_backup(self, reason: str = "manual") -> Optional[Dict[str, Any]]:
        """用 SQLite 在线备份 API 生成一份一致性快照。"""
        if not self.db_path.is_file():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        stamp = now.strftime("%Y%m%d-%H%M%S")
        safe_reason = "".join(c for c in str(reason) if c.isalnum() or c in "-_") or "manual"
        dst = self.backup_dir / f"{stamp}-{safe_reason}.db"
        if dst.exists():
            dst = self.backup_dir / f"{stamp}-{safe_reason}-{uuid.uuid4().hex[:4]}.db"
        with self._lock:
            target = sqlite3.connect(str(dst))
            try:
                self._conn.backup(target)
            finally:
                target.close()
        self._last_backup_ts = time.time()
        self._prune_backups()
        return {
            "file": dst.name,
            "path": str(dst),
            "size": dst.stat().st_size,
            "at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _prune_backups(self) -> int:
        if not self.backup_dir.is_dir():
            return 0
        files = sorted(
            self.backup_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        removed = 0
        for old in files[self.backup_keep :]:
            try:
                old.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def list_backups(self) -> List[Dict[str, Any]]:
        if not self.backup_dir.is_dir():
            return []
        out: List[Dict[str, Any]] = []
        for p in sorted(
            self.backup_dir.glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True
        ):
            st = p.stat()
            out.append(
                {
                    "file": p.name,
                    "size": st.st_size,
                    "at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        return out

    def backup_info(self) -> Dict[str, Any]:
        items = self.list_backups()
        return {
            "db_path": str(self.db_path),
            "db_size": self.db_path.stat().st_size if self.db_path.is_file() else 0,
            "dir": str(self.backup_dir),
            "keep": self.backup_keep,
            "count": len(items),
            "latest": items[0] if items else None,
            "backups": items,
        }

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------
    def list_plans(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            return self._query(
                "SELECT * FROM plans WHERE status=? ORDER BY signal_date DESC, id DESC",
                (status,),
            )
        return self._query("SELECT * FROM plans ORDER BY signal_date DESC, id DESC")

    def get_plan(self, plan_id: int) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM plans WHERE id=?", (plan_id,))
        return rows[0] if rows else None

    def upsert_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """按 (code, signal_date) 新增或覆盖计划。"""
        data = {k: payload.get(k) for k in PLAN_FIELDS}
        if not data.get("code") or not data.get("signal_date"):
            raise ValueError("code 与 signal_date 必填")
        data.setdefault("status", "待买入")
        if not data.get("status"):
            data["status"] = "待买入"
        existing = self._query(
            "SELECT * FROM plans WHERE code=? AND signal_date=?",
            (data["code"], data["signal_date"]),
        )
        if existing:
            sets = ", ".join(f"{k}=?" for k in PLAN_FIELDS)
            self._exec(
                f"UPDATE plans SET {sets}, updated_at=? WHERE id=?",
                [data[k] for k in PLAN_FIELDS] + [_now(), existing[0]["id"]],
            )
            return self.get_plan(existing[0]["id"])  # type: ignore[return-value]
        cols = ", ".join(PLAN_FIELDS + ["created_at", "updated_at"])
        marks = ", ".join(["?"] * (len(PLAN_FIELDS) + 2))
        cur = self._exec(
            f"INSERT INTO plans ({cols}) VALUES ({marks})",
            [data[k] for k in PLAN_FIELDS] + [_now(), _now()],
        )
        return self.get_plan(int(cur.lastrowid))  # type: ignore[return-value]

    def update_plan(self, plan_id: int, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        allowed = {k: v for k, v in patch.items() if k in PLAN_FIELDS}
        if not allowed:
            return self.get_plan(plan_id)
        sets = ", ".join(f"{k}=?" for k in allowed)
        self._exec(
            f"UPDATE plans SET {sets}, updated_at=? WHERE id=?",
            list(allowed.values()) + [_now(), plan_id],
        )
        return self.get_plan(plan_id)

    def delete_plan(self, plan_id: int) -> bool:
        cur = self._exec("DELETE FROM plans WHERE id=?", (plan_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 追踪
    # ------------------------------------------------------------------
    def list_watch(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            return self._query(
                "SELECT * FROM watchlist WHERE status=? ORDER BY signal_date DESC, id DESC",
                (status,),
            )
        return self._query("SELECT * FROM watchlist ORDER BY signal_date DESC, id DESC")

    def get_watch(self, watch_id: int) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM watchlist WHERE id=?", (watch_id,))
        return rows[0] if rows else None

    def upsert_watch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = {k: payload.get(k) for k in WATCH_FIELDS}
        if not data.get("code") or not data.get("signal_date"):
            raise ValueError("code 与 signal_date 必填")
        if not data.get("status"):
            data["status"] = "关注中"
        existing = self._query(
            "SELECT * FROM watchlist WHERE code=? AND signal_date=?",
            (data["code"], data["signal_date"]),
        )
        if existing:
            sets = ", ".join(f"{k}=?" for k in WATCH_FIELDS)
            self._exec(
                f"UPDATE watchlist SET {sets}, updated_at=? WHERE id=?",
                [data[k] for k in WATCH_FIELDS] + [_now(), existing[0]["id"]],
            )
            return self.get_watch(existing[0]["id"])  # type: ignore[return-value]
        cols = ", ".join(WATCH_FIELDS + ["created_at", "updated_at"])
        marks = ", ".join(["?"] * (len(WATCH_FIELDS) + 2))
        cur = self._exec(
            f"INSERT INTO watchlist ({cols}) VALUES ({marks})",
            [data[k] for k in WATCH_FIELDS] + [_now(), _now()],
        )
        return self.get_watch(int(cur.lastrowid))  # type: ignore[return-value]

    def update_watch(self, watch_id: int, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        allowed = {k: v for k, v in patch.items() if k in WATCH_FIELDS}
        if not allowed:
            return self.get_watch(watch_id)
        sets = ", ".join(f"{k}=?" for k in allowed)
        self._exec(
            f"UPDATE watchlist SET {sets}, updated_at=? WHERE id=?",
            list(allowed.values()) + [_now(), watch_id],
        )
        return self.get_watch(watch_id)

    def delete_watch(self, watch_id: int) -> bool:
        cur = self._exec("DELETE FROM watchlist WHERE id=?", (watch_id,))
        return cur.rowcount > 0

    def stats(self) -> Dict[str, Any]:
        return {
            "plans": self._query("SELECT COUNT(*) AS n FROM plans")[0]["n"],
            "watch": self._query("SELECT COUNT(*) AS n FROM watchlist")[0]["n"],
            "plan_status": self._query(
                "SELECT status, COUNT(*) AS n FROM plans GROUP BY status"
            ),
            "watch_status": self._query(
                "SELECT status, COUNT(*) AS n FROM watchlist GROUP BY status"
            ),
            "storage": self.backup_info(),
        }

    def export(self) -> Dict[str, Any]:
        """导出全部计划与追踪（用于人工存档）。"""
        return {
            "exported_at": _now(),
            "schema": 1,
            "plans": self.list_plans(),
            "watchlist": self.list_watch(),
        }
