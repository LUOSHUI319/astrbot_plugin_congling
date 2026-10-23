"""SQLite 存储层。

设计取舍：
- 用标准库 sqlite3（零依赖），所有阻塞操作丢进 asyncio.to_thread；
- 单连接 + 线程锁（WAL 模式下单写多读足够，插件流量很小）；
- schema 版本走 PRAGMA user_version，升级只往后加，不删列。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from astrbot.api import logger

SCHEMA_VERSION = 2

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS msg_archive(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id TEXT,
        session_id TEXT NOT NULL,
        session_kind TEXT DEFAULT 'group',
        sender_id TEXT,
        sender_name TEXT,
        ts INTEGER NOT NULL,
        text TEXT,
        blocks_json TEXT,
        expanded INTEGER DEFAULT 0,
        forward_id TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_msg_session_ts ON msg_archive(session_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_msg_forward ON msg_archive(forward_id)",
    """
    CREATE TABLE IF NOT EXISTS forward_cache(
        forward_id TEXT PRIMARY KEY,
        digest TEXT,
        full_text TEXT,
        node_count INTEGER DEFAULT 0,
        created_at INTEGER,
        expires_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mem_atom(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT DEFAULT 'ling',
        scope TEXT DEFAULT 'user',
        subject_id TEXT DEFAULT '',
        kind TEXT DEFAULT 'fact',
        text TEXT NOT NULL,
        norm_hash TEXT,
        keywords TEXT,
        importance REAL DEFAULT 0.5,
        confidence REAL DEFAULT 0.8,
        embedding BLOB,
        source_msg_ids TEXT,
        created_at INTEGER,
        last_hit_at INTEGER,
        hit_count INTEGER DEFAULT 0,
        decay REAL DEFAULT 1.0,
        status TEXT DEFAULT 'active',
        protect INTEGER DEFAULT 0,
        expire_at INTEGER,
        supersedes INTEGER,
        session_id TEXT DEFAULT '',
        session_kind TEXT DEFAULT '',
        session_label TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_atom_scope ON mem_atom(persona_id, scope, subject_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_atom_hash ON mem_atom(norm_hash)",
    """
    CREATE TABLE IF NOT EXISTS mem_episode(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT DEFAULT 'ling',
        session_id TEXT,
        started_at INTEGER,
        ended_at INTEGER,
        summary TEXT,
        mood TEXT,
        participants TEXT,
        msg_count INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mem_entity(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT DEFAULT 'ling',
        name TEXT NOT NULL,
        etype TEXT DEFAULT 'person',
        aliases TEXT,
        attrs TEXT,
        first_seen INTEGER,
        last_seen INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mem_edge(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT DEFAULT 'ling',
        src INTEGER,
        dst INTEGER,
        rel TEXT,
        weight REAL DEFAULT 1.0,
        evidence TEXT,
        updated_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_state(
        persona_id TEXT DEFAULT 'ling',
        scope TEXT DEFAULT 'user',
        subject_id TEXT DEFAULT '',
        intimacy REAL DEFAULT 0.1,
        mood TEXT DEFAULT 'calm',
        energy REAL DEFAULT 0.7,
        busy INTEGER DEFAULT 0,
        agenda_json TEXT,
        last_active INTEGER,
        PRIMARY KEY(persona_id, scope, subject_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inject_cursor(
        session_id TEXT PRIMARY KEY,
        last_msg_row INTEGER DEFAULT 0,
        last_atom_ids TEXT,
        updated_at INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,
        session_id TEXT,
        msg_id TEXT,
        decided TEXT,
        reason TEXT,
        plan_json TEXT,
        cost_tokens INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_decision_ts ON decision_log(ts)",
    """
    CREATE TABLE IF NOT EXISTS kv(
        k TEXT PRIMARY KEY,
        v TEXT,
        updated_at INTEGER
    )
    """,
]


class Store:
    """线程安全的极简异步 SQLite 封装。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self.ready = False

    # ------------------------------------------------------------ 生命周期
    def _open_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        self._migrate()

    def _migrate(self) -> None:
        assert self._conn is not None
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        for statement in _SCHEMA:
            self._conn.execute(statement)
        # 老库补列：只在缺的时候加，不重建表
        try:
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(mem_atom)")}
            for name, stmt in (
                ("session_id", "ALTER TABLE mem_atom ADD COLUMN session_id TEXT DEFAULT ''"),
                ("session_kind", "ALTER TABLE mem_atom ADD COLUMN session_kind TEXT DEFAULT ''"),
                ("session_label", "ALTER TABLE mem_atom ADD COLUMN session_label TEXT DEFAULT ''"),
            ):
                if name not in columns:
                    self._conn.execute(stmt)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_atom_session ON mem_atom(session_id, created_at)"
            )
        except Exception as exc:
            logger.debug(f"[congling] mem_atom 补列失败: {exc!r}")
        if current < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.commit()
            logger.info(f"[congling] 数据库结构就绪（v{current} → v{SCHEMA_VERSION}）")
        else:
            self._conn.commit()

    async def open(self) -> None:
        await asyncio.to_thread(self._open_sync)
        self.ready = True

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                if self._conn is not None:
                    try:
                        self._conn.commit()
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
            self.ready = False

        await asyncio.to_thread(_close)

    # ------------------------------------------------------------ 基础操作
    def _execute_sync(self, sql: str, params: Sequence[Any]) -> sqlite3.Cursor:
        with self._lock:
            if self._conn is None:
                raise RuntimeError("数据库未就绪")
            return self._conn.execute(sql, tuple(params))

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        def _run() -> int:
            with self._lock:
                cursor = self._execute_sync(sql, params)
                if self._conn is not None:
                    self._conn.commit()
                return int(cursor.lastrowid or 0)

        return await asyncio.to_thread(_run)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        data = [tuple(row) for row in rows]

        def _run() -> None:
            with self._lock:
                if self._conn is None:
                    raise RuntimeError("数据库未就绪")
                self._conn.executemany(sql, data)
                self._conn.commit()

        if data:
            await asyncio.to_thread(_run)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        def _run() -> list[dict[str, Any]]:
            cursor = self._execute_sync(sql, params)
            return [dict(row) for row in cursor.fetchall()]

        return await asyncio.to_thread(_run)

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetch_one(sql, params)
        if not row:
            return default
        return next(iter(row.values()), default)

    # ------------------------------------------------------------ KV
    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetch_one("SELECT v FROM kv WHERE k=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["v"] or "null")
        except (ValueError, TypeError):
            return default

    async def kv_set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, default=str)
        await self.execute(
            "INSERT INTO kv(k, v, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (key, payload, int(time.time())),
        )

    # ------------------------------------------------------------ 维护
    async def purge(self, table: str, before_ts: int) -> int:
        allowed = {"msg_archive", "decision_log", "forward_cache"}
        if table not in allowed:
            raise ValueError(f"不允许清理表 {table}")
        column = "expires_at" if table == "forward_cache" else "ts"
        await self.execute(f"DELETE FROM {table} WHERE {column} < ?", (before_ts,))
        return 0

    async def stats(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for table in (
            "msg_archive",
            "mem_atom",
            "mem_episode",
            "mem_entity",
            "mem_edge",
            "decision_log",
        ):
            try:
                value = await self.scalar(f"SELECT COUNT(*) FROM {table}", (), 0)
                result[table] = int(value or 0)
            except Exception:
                result[table] = 0
        return result
