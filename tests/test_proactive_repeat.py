"""主动消息防复读。

盯四件事：
1. 换个说法重问同一件事，能被认出来（原来只比完全相等，改个措辞就漏）；
2. 真换话题不会误杀；
3. 撞车先换一件重来，还撞就不发；
4. 提示词里确实带上了「我最近说过的话」。
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.humanize.proactive import (  # noqa: E402
    Proactive,
    _bigrams,
    _repeat_topic,
)

SCHEMA = """
CREATE TABLE msg_archive(
  id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id TEXT, session_id TEXT NOT NULL,
  session_kind TEXT DEFAULT 'private', sender_id TEXT, sender_name TEXT,
  ts INTEGER NOT NULL, text TEXT, blocks_json TEXT, expanded INTEGER DEFAULT 0,
  forward_id TEXT, umo TEXT DEFAULT '', platform TEXT DEFAULT ''
);
CREATE TABLE mem_atom(
  id INTEGER PRIMARY KEY AUTOINCREMENT, persona_id TEXT DEFAULT 'ling',
  kind TEXT DEFAULT 'event', text TEXT DEFAULT '', importance REAL DEFAULT 0.5,
  status TEXT DEFAULT 'active', unread INTEGER DEFAULT 0, created_at INTEGER DEFAULT 0,
  scope TEXT DEFAULT 'user', session_id TEXT DEFAULT ''
);
"""

SESSION = "307062828"
IMG_Q1 = "那张图我到现在还是没看清写的啥"
IMG_Q2 = "白天上班那张图，你到底说的是哪张啊"
NEW_TOPIC = "楼下那只橘猫又蹲在门口了"


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

    async def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    async def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key, value):
        self.kv[key] = value

    def add_msg(self, text: str, *, sender: str = "user", ts: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO msg_archive(msg_id, session_id, session_kind, sender_id, sender_name, ts, text, blocks_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (f"m{ts}-{sender}", SESSION, "private", "bot" if sender == "bot" else SESSION,
             "我" if sender == "bot" else "Sense", ts, text, "[]"),
        )
        self.conn.commit()


class MiniCfg:
    persona_id = "ling"

    def int(self, key, default=0):
        return default

    def str(self, key, default=""):
        return default

    def bool(self, key, default=False):
        return default


class ScriptedModels:
    """按顺序回话，并把 prompt 留下来给断言用。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def chat(self, prompt, system="", temperature=1.0, max_tokens=800):
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self.replies) - 1)
        return self.replies[index]


class MiniPlugin:
    def __init__(self, store: MiniStore, models: ScriptedModels) -> None:
        self.store = store
        self.cfg = MiniCfg()
        self.models = models


class BigramTest(unittest.TestCase):
    def test_bigram_cuts_chinese_pairs(self):
        self.assertIn("那张", _bigrams("那张图"))
        self.assertEqual(_bigrams(""), set())

    def test_same_topic_by_different_wording(self):
        # 同一张图：一条问「写的啥」，一条问「是哪张」
        self.assertTrue(_repeat_topic(IMG_Q2, [IMG_Q1]))

    def test_same_topic_rewrite(self):
        # 把「是啥」改成「写的啥」也要算撞车
        self.assertTrue(_repeat_topic("你上班那张图到底写的啥", ["你上班发的那张图是啥呀"]))

    def test_different_topic_is_fine(self):
        self.assertFalse(_repeat_topic("今天风好大", [IMG_Q1]))

    def test_short_text_never_hits(self):
        self.assertFalse(_repeat_topic("嗯", [IMG_Q1]))
        self.assertFalse(_repeat_topic("", [IMG_Q1]))


class ComposeRepeatTest(unittest.TestCase):
    def _plugin(self, replies: list[str]) -> MiniPlugin:
        store = MiniStore()
        store.add_msg("在吗", ts=1)
        store.add_msg(IMG_Q1, sender="bot", ts=2)
        return MiniPlugin(store, ScriptedModels(replies))

    def _compose(self, plugin: MiniPlugin) -> str:
        proactive = Proactive(plugin)
        return asyncio.run(
            proactive.compose({"session_id": SESSION, "session_kind": "private"})
        )

    def test_retry_once_then_take_new_topic(self):
        plugin = self._plugin([IMG_Q2, NEW_TOPIC])
        self.assertEqual(self._compose(plugin), NEW_TOPIC)
        self.assertEqual(len(plugin.models.prompts), 2)

    def test_give_up_when_both_attempts_repeat(self):
        plugin = self._plugin([IMG_Q2, "那张图我到现在还是没看清写的啥"])
        self.assertEqual(self._compose(plugin), "")
        self.assertEqual(len(plugin.models.prompts), 2)

    def test_first_attempt_ok(self):
        plugin = self._plugin([NEW_TOPIC])
        self.assertEqual(self._compose(plugin), NEW_TOPIC)
        self.assertEqual(len(plugin.models.prompts), 1)

    def test_prompt_lists_recent_said(self):
        plugin = self._plugin([NEW_TOPIC])
        self._compose(plugin)
        prompt = plugin.models.prompts[0]
        self.assertIn("不许再说同一件事", prompt)
        self.assertIn(IMG_Q1, prompt)


if __name__ == "__main__":
    unittest.main()
