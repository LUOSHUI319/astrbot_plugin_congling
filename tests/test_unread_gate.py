"""未曾爬楼的闸门（0.7.0 · F9）。

睡着期间产生的记忆不许直接出现在记忆库：先标 unread=1，展示层全部过滤掉，
等她真爬过楼（take_crawl_line）才放出来。

这里盯的是最要命的那条：抽取是延后批量的，等它跑的时候人早醒了，
所以判定必须按「证据消息的时间 vs 睡眠区间」，而不是「写入这一刻在不在睡」。
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.memory.atom import AtomStore  # noqa: E402

SCHEMA = """
CREATE TABLE mem_atom(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  persona_id TEXT DEFAULT 'ling',
  scope TEXT DEFAULT 'user',
  subject_id TEXT DEFAULT '',
  kind TEXT DEFAULT 'fact',
  text TEXT DEFAULT '',
  norm_hash TEXT DEFAULT '',
  keywords TEXT DEFAULT '[]',
  importance REAL DEFAULT 0.5,
  confidence REAL DEFAULT 0.8,
  source_msg_ids TEXT DEFAULT '[]',
  created_at INTEGER DEFAULT 0,
  last_hit_at INTEGER DEFAULT 0,
  hit_count INTEGER DEFAULT 0,
  decay REAL DEFAULT 1.0,
  status TEXT DEFAULT 'active',
  protect INTEGER DEFAULT 0,
  supersedes INTEGER DEFAULT 0,
  session_id TEXT DEFAULT '',
  session_kind TEXT DEFAULT '',
  session_label TEXT DEFAULT '',
  unread INTEGER DEFAULT 0,
  unread_since INTEGER DEFAULT 0,
  mem_level INTEGER DEFAULT 3,
  level_reason TEXT DEFAULT '',
  level_tags TEXT DEFAULT '[]',
  level_source TEXT DEFAULT 'llm',
  strength REAL DEFAULT 0.6,
  next_review_at INTEGER DEFAULT 0
);
CREATE TABLE msg_archive(
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
);
"""

SLEEP_START = 1_800_000_000
SLEEP_END = SLEEP_START + 8 * 3600


class FakeCfg:
    def float(self, key, default=0.0):
        return default

    def int(self, key, default=0):
        return default

    def bool(self, key, default=False):
        return default

    def str(self, key, default=""):
        return default

    def list(self, key, default=None):
        return default or []


class MiniStore:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.kv: dict[str, object] = {}

    async def fetch_all(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    async def fetch_one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    async def scalar(self, sql, params=(), default=0):
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    async def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    async def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key, value):
        self.kv[key] = value


class FakePlugin:
    def __init__(self) -> None:
        self.cfg = FakeCfg()
        self.store = MiniStore()


def build() -> tuple[AtomStore, MiniStore]:
    plugin = FakePlugin()
    return AtomStore(plugin), plugin.store


def add_atom(store: MiniStore, *, text: str, session: str, unread: int, created: int = SLEEP_START + 60) -> int:
    cur = store.conn.execute(
        "INSERT INTO mem_atom(text, session_id, session_kind, session_label, unread, unread_since, "
        "created_at, status, mem_level, importance) VALUES(?,?,?,?,?,?,?, 'active', 3, 0.6)",
        (text, session, "group", f"群 {session}", unread, created if unread else 0, created),
    )
    store.conn.commit()
    return int(cur.lastrowid)


class UnreadGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.atoms, self.store = build()
        add_atom(self.store, text="看过的一条", session="111", unread=0)
        add_atom(self.store, text="没爬过的一条", session="222", unread=1)

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_list_hides_unread_by_default(self) -> None:
        rows = self.run_async(self.atoms.list())
        texts = [r["text"] for r in rows]
        self.assertIn("看过的一条", texts)
        self.assertNotIn("没爬过的一条", texts)

    def test_list_can_include_unread(self) -> None:
        rows = self.run_async(self.atoms.list(include_unread=True))
        self.assertEqual(len(rows), 2)

    def test_count_and_unread_count(self) -> None:
        self.assertEqual(self.run_async(self.atoms.count()), 1)
        self.assertEqual(self.run_async(self.atoms.count_unread()), 1)

    def test_sessions_skip_unread(self) -> None:
        rows = self.run_async(self.atoms.sessions())
        ids = [r["session_id"] for r in rows]
        self.assertEqual(ids, ["111"])

    def test_sleep_hit_matches_window(self) -> None:
        for msg_id, ts in (("m1", SLEEP_START + 120), ("m2", SLEEP_START + 200)):
            self.store.conn.execute(
                "INSERT INTO msg_archive(msg_id, session_id, ts, text) VALUES(?,?,?,?)",
                (msg_id, "222", ts, "睡的时候说的"),
            )
        self.store.conn.commit()
        self.store.kv["night.last_sleep_window"] = [
            {"sleep_at": SLEEP_START, "wake_at": SLEEP_END, "kind": "night"}
        ]
        hit = self.run_async(self.atoms._sleep_hit(["m1", "m2"]))
        self.assertGreaterEqual(hit, SLEEP_START)

    def test_sleep_hit_ignores_daytime_messages(self) -> None:
        self.store.conn.execute(
            "INSERT INTO msg_archive(msg_id, session_id, ts, text) VALUES(?,?,?,?)",
            ("m3", "111", SLEEP_END + 3600, "醒着说的"),
        )
        self.store.conn.commit()
        self.store.kv["night.last_sleep_window"] = [
            {"sleep_at": SLEEP_START, "wake_at": SLEEP_END, "kind": "night"}
        ]
        self.assertEqual(self.run_async(self.atoms._sleep_hit(["m3"])), 0)

    def test_no_window_no_mark(self) -> None:
        self.store.conn.execute(
            "INSERT INTO msg_archive(msg_id, session_id, ts, text) VALUES(?,?,?,?)",
            ("m4", "111", SLEEP_START + 60, "随便一句"),
        )
        self.store.conn.commit()
        self.assertEqual(self.run_async(self.atoms._sleep_hit(["m4"])), 0)


if __name__ == "__main__":
    unittest.main()
