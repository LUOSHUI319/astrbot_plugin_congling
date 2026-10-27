"""日记（0.7.0 · 第 6 条）。

盯四件事：
1. 格式坏了也不能丢正文（模型经常不按 JSON 来）；
2. 一天只有一篇（唯一索引 + 预检）；
3. 没素材就不写，别硬编；
4. 好感度不够档位时，正文里不能出现对方名字。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.core.config import Config  # noqa: E402
from astrbot_plugin_congling.memory.diary import Diary, day_key_of  # noqa: E402

SCHEMA = """
CREATE TABLE mem_atom(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  persona_id TEXT DEFAULT 'ling',
  scope TEXT DEFAULT 'user',
  subject_id TEXT DEFAULT '',
  kind TEXT DEFAULT 'fact',
  text TEXT DEFAULT '',
  importance REAL DEFAULT 0.5,
  created_at INTEGER DEFAULT 0,
  decay REAL DEFAULT 1.0,
  status TEXT DEFAULT 'active',
  session_id TEXT DEFAULT '',
  unread INTEGER DEFAULT 0,
  mem_level INTEGER DEFAULT 3
);
CREATE TABLE mem_diary(
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
);
CREATE UNIQUE INDEX idx_diary_day ON mem_diary(persona_id, day_key);
CREATE TABLE affinity(
  persona_id TEXT, subject_key TEXT, score REAL DEFAULT 0, stage INTEGER DEFAULT 0,
  floor REAL DEFAULT 0, delta_today REAL DEFAULT 0, last_talk_at INTEGER DEFAULT 0,
  manual INTEGER DEFAULT 0, updated_at INTEGER DEFAULT 0, evidence_atom_ids TEXT DEFAULT '[]'
);
CREATE TABLE mem_subject(
  persona_id TEXT, user_id TEXT, display_name TEXT DEFAULT '', aliases TEXT DEFAULT '[]',
  last_seen_at INTEGER DEFAULT 0
);
"""


class MiniStore:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.kv: dict[str, object] = {}

    async def fetch_all(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    async def fetch_one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    async def scalar(self, sql, params=(), default=0):
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            return default
        return default if row[0] is None else row[0]

    async def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    async def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key, value):
        self.kv[key] = value


class FakeModels:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0
        self.last_prompt = ""

    async def chat(self, prompt, system="", temperature=1.0, max_tokens=800):
        self.calls += 1
        self.last_prompt = prompt
        return self.payload


class FakeAffinity:
    def __init__(self, store: MiniStore, stage: int, score: float) -> None:
        self.store = store
        self.stage = stage
        self.score = score

    async def get(self, key):
        return {
            "subject_key": key,
            "score": self.score,
            "stage": self.stage,
            "stage_name": "熟人",
        }


class FakeNight:
    dreamer = None
    state = None


class FakePlugin:
    def __init__(self, *, payload: str, stage: int = 4, atoms: int = 3, diary_cfg: dict | None = None) -> None:
        raw = {
            "diary": {
                "enabled": True,
                "hour": 23,
                "jitter_minutes": 0,
                "skip_when_empty": True,
                "affinity_aware": True,
                "min_stage_to_name": 3,
                "use_dreams": False,
                "max_atoms": 12,
            }
        }
        for key, value in (diary_cfg or {}).items():
            raw["diary"][key] = value
        self.cfg = Config(raw)
        self.store = MiniStore()
        self.models = FakeModels(payload)
        self.night = FakeNight()

        class _Mem:
            pass

        mem = _Mem()
        mem.affinity = FakeAffinity(self.store, stage, 62.0 if stage >= 3 else 20.0)
        self.memory = mem

        today = day_key_of()
        start = int(time.mktime(time.strptime(today, "%Y-%m-%d"))) + 3600
        for i in range(atoms):
            self.store.conn.execute(
                "INSERT INTO mem_atom(persona_id, kind, text, subject_id, created_at, status, mem_level) "
                "VALUES('ling', ?, ?, '3190568934', ?, 'active', 4)",
                ("event", f"今天发生的第 {i + 1} 件事", start + i * 60),
            )
        self.store.conn.execute(
            "INSERT INTO mem_subject(persona_id, user_id, display_name) VALUES('ling','3190568934','望山')"
        )
        self.store.conn.commit()


def run(coro):
    return asyncio.run(coro)


GOOD_JSON = json.dumps(
    {"title": "晚上十一点", "text": "今天她一直在写代码，眼睛有点酸。", "mood": "温柔", "tags": ["日常", "夜"], "mention": True},
    ensure_ascii=False,
)


class DiaryTest(unittest.TestCase):
    def test_writes_one_entry_with_refs(self) -> None:
        plugin = FakePlugin(payload=GOOD_JSON)
        diary = Diary(plugin)
        row = run(diary.compose())
        self.assertIsNotNone(row)
        self.assertEqual(row["mood"], "温柔")
        stored = run(diary.get(day_key_of()))
        self.assertEqual(stored["title"], "晚上十一点")
        self.assertTrue(stored["refs"], "必须记下引了哪些记忆")

    def test_only_one_per_day(self) -> None:
        plugin = FakePlugin(payload=GOOD_JSON)
        diary = Diary(plugin)
        run(diary.compose())
        run(diary.compose())
        count = plugin.store.conn.execute("SELECT COUNT(*) FROM mem_diary").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(plugin.models.calls, 1)

    def test_skip_when_empty_does_not_call_model(self) -> None:
        plugin = FakePlugin(payload=GOOD_JSON, atoms=0)
        diary = Diary(plugin)
        self.assertIsNone(run(diary.compose()))
        self.assertEqual(plugin.models.calls, 0)

    def test_broken_json_still_saves_text(self) -> None:
        plugin = FakePlugin(payload="今天有点累，什么都没做成，但还是想写两句。")
        diary = Diary(plugin)
        row = run(diary.compose())
        self.assertIsNotNone(row)
        self.assertIn("有点累", row["text"])
        self.assertTrue(row["mood"])

    def test_no_name_below_stage(self) -> None:
        plugin = FakePlugin(payload=GOOD_JSON, stage=2)
        diary = Diary(plugin)
        run(diary.compose())
        self.assertNotIn("望山", plugin.models.last_prompt)
        self.assertIn("不要写具体名字", plugin.models.last_prompt)

    def test_name_allowed_above_stage(self) -> None:
        plugin = FakePlugin(payload=GOOD_JSON, stage=4)
        diary = Diary(plugin)
        run(diary.compose())
        self.assertIn("望山", plugin.models.last_prompt)

    def test_pin_and_delete(self) -> None:
        plugin = FakePlugin(payload=GOOD_JSON)
        diary = Diary(plugin)
        row = run(diary.compose())
        self.assertTrue(run(diary.pin(row["id"], True)))
        rows = run(diary.list())
        self.assertEqual(rows[0]["pinned"], 1)
        self.assertTrue(run(diary.delete(row["id"])))
        self.assertEqual(run(diary.list()), [])

    def test_due_respects_hour_and_existing(self) -> None:
        plugin = FakePlugin(payload=GOOD_JSON)
        diary = Diary(plugin)
        early = time.mktime(time.strptime(f"{day_key_of()} 09:00", "%Y-%m-%d %H:%M"))
        late = time.mktime(time.strptime(f"{day_key_of()} 23:30", "%Y-%m-%d %H:%M"))
        self.assertFalse(run(diary.due(early)))
        self.assertTrue(run(diary.due(late)))
        run(diary.compose())
        self.assertFalse(run(diary.due(late)))

    def test_disabled_does_nothing(self) -> None:
        plugin = FakePlugin(payload=GOOD_JSON, diary_cfg={"enabled": False})
        diary = Diary(plugin)
        self.assertIsNone(run(diary.compose()))
        self.assertFalse(run(diary.due()))


if __name__ == "__main__":
    unittest.main()
