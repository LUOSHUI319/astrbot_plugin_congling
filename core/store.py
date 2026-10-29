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

SCHEMA_VERSION = 5

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
        session_label TEXT DEFAULT '',
        unread INTEGER DEFAULT 0,
        unread_since INTEGER DEFAULT 0
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
    """
    CREATE TABLE IF NOT EXISTS mem_subject(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT DEFAULT 'ling',
        user_id TEXT DEFAULT '',
        display_name TEXT DEFAULT '',
        aliases TEXT DEFAULT '[]',
        confidence REAL DEFAULT 0.5,
        source TEXT DEFAULT 'inferred',
        note TEXT DEFAULT '',
        first_seen_at INTEGER,
        last_seen_at INTEGER,
        msg_count INTEGER DEFAULT 0
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_subject_user ON mem_subject(persona_id, user_id)",
    "CREATE INDEX IF NOT EXISTS idx_subject_name ON mem_subject(persona_id, display_name)",
    """
    CREATE TABLE IF NOT EXISTS mem_atom_tag(
        atom_id INTEGER NOT NULL,
        subject_key TEXT NOT NULL,
        role TEXT DEFAULT 'subject',
        created_at INTEGER,
        PRIMARY KEY(atom_id, subject_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tag_subject ON mem_atom_tag(subject_key)",
    """
    CREATE TABLE IF NOT EXISTS mem_profile(
        persona_id TEXT DEFAULT 'ling',
        subject_key TEXT NOT NULL,
        fields TEXT DEFAULT '{}',
        keywords TEXT DEFAULT '[]',
        summary TEXT DEFAULT '',
        source_atom_ids TEXT DEFAULT '[]',
        updated_at INTEGER,
        PRIMARY KEY(persona_id, subject_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS affinity(
        persona_id TEXT DEFAULT 'ling',
        subject_key TEXT NOT NULL,
        score REAL DEFAULT 0,
        stage INTEGER DEFAULT 0,
        delta_today REAL DEFAULT 0,
        floor REAL DEFAULT 0,
        last_talk_at INTEGER,
        talk_days_30 INTEGER DEFAULT 0,
        evidence_atom_ids TEXT DEFAULT '[]',
        manual INTEGER DEFAULT 0,
        updated_at INTEGER,
        PRIMARY KEY(persona_id, subject_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qq_avatar_pool(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT DEFAULT 'ling',
        path TEXT NOT NULL,
        sha1 TEXT DEFAULT '',
        source TEXT DEFAULT 'bot_pending',
        trusted INTEGER DEFAULT 0,
        added_by TEXT DEFAULT '',
        added_at INTEGER,
        last_used_at INTEGER DEFAULT 0,
        use_count INTEGER DEFAULT 0,
        tags TEXT DEFAULT '[]',
        note TEXT DEFAULT ''
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_avatar_sha ON qq_avatar_pool(persona_id, sha1)",
    # ---- 0.7.0 日记（第 6 条）----
    """
    CREATE TABLE IF NOT EXISTS mem_diary(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT DEFAULT 'ling',
        day_key TEXT NOT NULL,
        created_at INTEGER,
        title TEXT DEFAULT '',
        text TEXT DEFAULT '',
        mood TEXT DEFAULT '',
        tags TEXT DEFAULT '[]',
        refs TEXT DEFAULT '[]',
        affinity_subject TEXT DEFAULT '',
        affinity_score REAL DEFAULT 0,
        pinned INTEGER DEFAULT 0,
        hidden INTEGER DEFAULT 0
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_diary_day ON mem_diary(persona_id, day_key)",
    "CREATE INDEX IF NOT EXISTS idx_diary_time ON mem_diary(persona_id, created_at)",
    # ---- 0.7.0 主动找人（第 11 条）----
    """
    CREATE TABLE IF NOT EXISTS proactive_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT DEFAULT 'ling',
        session_id TEXT NOT NULL,
        umo TEXT DEFAULT '',
        at INTEGER,
        reason TEXT DEFAULT '',
        text TEXT DEFAULT '',
        kind TEXT DEFAULT 'text',
        replied_at INTEGER DEFAULT 0,
        replied_delta REAL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_proactive_at ON proactive_log(persona_id, at)",
    # 未爬记忆查询走这条索引，别让记忆库翻全表
    "CREATE INDEX IF NOT EXISTS idx_atom_unread ON mem_atom(persona_id, unread, status)",
]


class Store:
    """线程安全的极简异步 SQLite 封装。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self.ready = False
        # 显式事务期间由调用方控制提交，execute 不再自己 commit。
        # 主体合并这类"多表必须一起成功"的操作要靠它，否则中途失败会写一半。
        self._in_tx = False

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
                ("unread", "ALTER TABLE mem_atom ADD COLUMN unread INTEGER DEFAULT 0"),
                ("unread_since", "ALTER TABLE mem_atom ADD COLUMN unread_since INTEGER DEFAULT 0"),
                ("mem_level", "ALTER TABLE mem_atom ADD COLUMN mem_level INTEGER DEFAULT 3"),
                ("level_reason", "ALTER TABLE mem_atom ADD COLUMN level_reason TEXT DEFAULT ''"),
                ("level_tags", "ALTER TABLE mem_atom ADD COLUMN level_tags TEXT DEFAULT '[]'"),
                ("strength", "ALTER TABLE mem_atom ADD COLUMN strength REAL DEFAULT 0.65"),
                ("next_review_at", "ALTER TABLE mem_atom ADD COLUMN next_review_at INTEGER DEFAULT 0"),
                ("level_source", "ALTER TABLE mem_atom ADD COLUMN level_source TEXT DEFAULT 'llm'"),
            ):
                if name not in columns:
                    self._conn.execute(stmt)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_atom_session ON mem_atom(session_id, created_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_atom_level ON mem_atom(persona_id, mem_level, status)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_atom_review ON mem_atom(next_review_at)"
            )
        except Exception as exc:
            logger.debug(f"[congling] mem_atom 补列失败: {exc!r}")
        # msg_archive 补 umo / platform：主动发送要按 umo 发，光有群号/QQ 号不够
        try:
            msg_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(msg_archive)")}
            for name, stmt in (
                ("umo", "ALTER TABLE msg_archive ADD COLUMN umo TEXT DEFAULT ''"),
                ("platform", "ALTER TABLE msg_archive ADD COLUMN platform TEXT DEFAULT ''"),
            ):
                if name not in msg_cols:
                    self._conn.execute(stmt)
        except Exception as exc:
            logger.debug(f"[congling] msg_archive 补列失败: {exc!r}")
        if current < 4:
            self._backfill_levels()
        if current < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.commit()
            logger.info(f"[congling] 数据库结构就绪（v{current} → v{SCHEMA_VERSION}）")
        else:
            self._conn.commit()

    def _backfill_levels(self) -> None:
        """0.4.x → 0.5.0 一次性回填：老记忆没有等级，按 importance 粗分。

        保护过的（protect=1）直接进 0 级不可忘；其余按重要度切四段。
        只跑一次，靠 PRAGMA user_version 记录。
        """
        assert self._conn is not None
        try:
            self._conn.execute(
                "UPDATE mem_atom SET mem_level=0, level_source='migrated' WHERE protect=1"
            )
            self._conn.execute(
                "UPDATE mem_atom SET mem_level=CASE "
                "WHEN importance<0.4 THEN 2 WHEN importance<0.65 THEN 3 "
                "WHEN importance<0.85 THEN 4 ELSE 5 END, level_source='migrated' "
                "WHERE protect=0"
            )
            self._conn.execute(
                "UPDATE mem_atom SET strength=COALESCE(decay,0.65) "
                "WHERE strength IS NULL OR strength<=0"
            )
            logger.info("[congling] 记忆等级回填完成（0.4.x → 0.5.0）")
        except Exception as exc:
            logger.warning(f"[congling] 记忆等级回填失败: {exc!r}")

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
                if self._conn is not None and not self._in_tx:
                    self._conn.commit()
                return int(cursor.lastrowid or 0)

        return await asyncio.to_thread(_run)

    async def begin(self) -> None:
        """开一个显式事务。这之后的 execute 不再自动提交，直到 commit/rollback。"""

        def _run() -> None:
            with self._lock:
                if self._conn is None:
                    raise RuntimeError("数据库未就绪")
                self._conn.execute("BEGIN IMMEDIATE")
                self._in_tx = True

        await asyncio.to_thread(_run)

    async def commit(self) -> None:
        def _run() -> None:
            with self._lock:
                if self._conn is not None:
                    self._conn.commit()
                self._in_tx = False

        await asyncio.to_thread(_run)

    async def rollback(self) -> None:
        def _run() -> None:
            with self._lock:
                if self._conn is not None:
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                self._in_tx = False

        await asyncio.to_thread(_run)

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
