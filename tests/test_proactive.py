"""主动找人（0.7.0 · 第 11 条）。

盯六条：
1. 免打扰 / 睡着 / 精力低 → 不发（靠硬否决，不靠分数）；
2. 每天额度、单会话额度、冷却 → 不发；
3. 连着两条没被回 → 该会话退避；被回之后计数清零；
4. 生成失败或空 → 不发、不写行动记录；
5. 老数据 umo 为空 → 能拼出合法 umo；
6. 发出去的主动消息要进归档（否则她下一轮不知道自己说过话）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.core.config import Config  # noqa: E402
from astrbot_plugin_congling.humanize.proactive import Proactive, _in_spec  # noqa: E402

SCHEMA = """
CREATE TABLE msg_archive(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  msg_id TEXT, session_id TEXT NOT NULL, session_kind TEXT DEFAULT 'private',
  sender_id TEXT, sender_name TEXT, ts INTEGER NOT NULL, text TEXT,
  blocks_json TEXT, expanded INTEGER DEFAULT 0, forward_id TEXT,
  umo TEXT DEFAULT '', platform TEXT DEFAULT ''
);
CREATE TABLE proactive_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, persona_id TEXT DEFAULT 'ling',
  session_id TEXT NOT NULL, umo TEXT DEFAULT '', at INTEGER, reason TEXT DEFAULT '',
  text TEXT DEFAULT '', kind TEXT DEFAULT 'text', replied_at INTEGER DEFAULT 0,
  replied_delta REAL DEFAULT 0
);
CREATE TABLE mem_atom(
  id INTEGER PRIMARY KEY AUTOINCREMENT, persona_id TEXT DEFAULT 'ling', kind TEXT DEFAULT 'event',
  text TEXT DEFAULT '', importance REAL DEFAULT 0.5, status TEXT DEFAULT 'active',
  unread INTEGER DEFAULT 0, created_at INTEGER DEFAULT 0
);
"""

SESSION = "3190568934"


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
        if row is None or row[0] is None:
            return default
        return row[0]

    async def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    async def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key, value):
        self.kv[key] = value


class FakeModels:
    def __init__(self, text: str = "那个插件你弄完了吗") -> None:
        self.text = text
        self.calls = 0

    async def chat(self, prompt, system="", temperature=1.0, max_tokens=800):
        self.calls += 1
        return self.text


class FakeAffinity:
    def __init__(self, stage: int = 4) -> None:
        self.stage = stage
        self.added: list[tuple[str, float]] = []

    async def get(self, key):
        return {"subject_key": key, "score": 80.0, "stage": self.stage}

    async def add(self, key, delta, **_kwargs):
        self.added.append((key, delta))
        return {"subject_key": key, "score": 82.0, "stage": self.stage}


class FakeState:
    def __init__(self, mode: str = "awake", energy: float = 90.0) -> None:
        self.mode = mode
        self.energy = energy
        self.enter_waking_at = 0


class FakeNight:
    def __init__(self, state: FakeState) -> None:
        self.state = type("S", (), {"state": state})()


class FakeComposer:
    def split(self, text, session_kind="private"):
        return [text]


class FakeContext:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    async def send_message(self, umo, chain):
        if self.fail:
            raise RuntimeError("通道坏了")
        # MessageChain 本身不可迭代，要取 .chain
        parts = getattr(chain, "chain", chain) or []
        text = "".join(str(getattr(comp, "text", "")) for comp in parts)
        self.sent.append((umo, text))


class FakeMemory:
    def __init__(self, stage: int = 4) -> None:
        self.affinity = FakeAffinity(stage)


class FakePlugin:
    def __init__(self, *, stage: int = 4, mode: str = "awake", energy: float = 90.0,
                 reply: str = "那个插件你弄完了吗", cfg: dict | None = None,
                 send_fail: bool = False) -> None:
        raw = {
            "proactive": {
                "enabled": True,
                "session_mode": "recent",
                "idle_min_minutes": 90,
                "idle_max_hours": 48,
                "daily_cap": 6,
                "per_session_cap": 2,
                "cooldown_minutes": 120,
                "unanswered_limit": 2,
                "min_affinity_stage": 1,
                "energy_min": 40,
                "respect_night": True,
                "quiet_hours": "01:00-08:00",
                "active_hours": "",
                "allow_group": False,
                "group_silence_minutes": 30,
                "promise_nudge": False,
            }
        }
        for key, value in (cfg or {}).items():
            raw["proactive"][key] = value
        self.cfg = Config(raw)
        self.store = MiniStore()
        self.models = FakeModels(reply)
        self.memory = FakeMemory(stage)
        self.night = FakeNight(FakeState(mode, energy))
        self.composer = FakeComposer()
        self.context = FakeContext(send_fail)
        self.context.plugin = self


def run(coro):
    return asyncio.run(coro)


def seed_message(plugin: FakePlugin, ts: int, *, umo: str = "", sender_id: str = SESSION,
                 kind: str = "private", session_id: str = SESSION, text: str = "我先睡了") -> None:
    plugin.store.conn.execute(
        "INSERT INTO msg_archive(msg_id, session_id, session_kind, sender_id, sender_name, ts, text, umo, platform) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (f"m{ts}", session_id, kind, sender_id, "望山", ts, text, umo, "aiocqhttp"),
    )
    plugin.store.conn.commit()


def at_today(hour: int, minute: int = 0) -> float:
    base = time.strftime("%Y-%m-%d", time.localtime())
    return time.mktime(time.strptime(f"{base} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M"))


class ProactiveTest(unittest.TestCase):
    def build(self, **kwargs) -> tuple[FakePlugin, Proactive, dict]:
        plugin = FakePlugin(**kwargs)
        now = at_today(15, 0)
        seed_message(plugin, int(now) - 4 * 3600)
        proactive = Proactive(plugin)
        run(proactive.load())
        rows = run(proactive.candidates(now))
        assert rows, "应该有一个候选"
        return plugin, proactive, rows[0]

    # ---------------------------------------------------------------- 硬否决
    def test_quiet_hours_blocks(self) -> None:
        plugin, proactive, row = self.build()
        self.assertFalse(run(proactive.should(row, at_today(3, 0)))[0])

    def test_asleep_blocks(self) -> None:
        plugin, proactive, row = self.build(mode="asleep")
        ok, reason, _ = run(proactive.should(row, at_today(15, 0)))
        self.assertFalse(ok)
        self.assertIn("睡", reason)

    def test_low_energy_blocks(self) -> None:
        plugin, proactive, row = self.build(energy=12.0)
        self.assertFalse(run(proactive.should(row, at_today(15, 0)))[0])

    def test_low_affinity_blocks(self) -> None:
        plugin, proactive, row = self.build(stage=0)
        ok, reason, _ = run(proactive.should(row, at_today(15, 0)))
        self.assertFalse(ok)
        self.assertIn("熟", reason)

    def test_group_off_by_default(self) -> None:
        plugin = FakePlugin()
        now = at_today(15, 0)
        seed_message(plugin, int(now) - 7200, kind="group", session_id="577084969", sender_id="1001")
        proactive = Proactive(plugin)
        rows = run(proactive.candidates(now))
        ok, reason, _ = run(proactive.should(rows[0], now))
        self.assertFalse(ok)

    # ---------------------------------------------------------------- 额度
    def test_daily_cap_blocks(self) -> None:
        plugin, proactive, row = self.build(cfg={"daily_cap": 1})
        proactive._global["sent_today"] = 1
        proactive._global["day"] = time.strftime("%Y-%m-%d")
        ok, reason, _ = run(proactive.should(row, at_today(15, 0)))
        self.assertFalse(ok)
        self.assertIn("今天", reason)

    def test_cooldown_blocks(self) -> None:
        plugin, proactive, row = self.build()
        now = at_today(15, 0)
        proactive._sess(SESSION)["next_ok_at"] = int(now + 600)
        self.assertFalse(run(proactive.should(row, now))[0])

    def test_unanswered_limit_blocks(self) -> None:
        plugin, proactive, row = self.build()
        now = at_today(15, 0)
        proactive._sess(SESSION)["unanswered"] = 2
        ok, reason, _ = run(proactive.should(row, now))
        self.assertFalse(ok)
        self.assertIn("没被回", reason)

    # ---------------------------------------------------------------- 发送
    def test_send_success_records(self) -> None:
        plugin, proactive, row = self.build()
        result = run(proactive.send_one(row, reason="idle"))
        self.assertTrue(result["ok"])
        self.assertEqual(len(plugin.context.sent), 1)
        self.assertIn("弄完", plugin.context.sent[0][1])
        log = plugin.store.conn.execute("SELECT COUNT(*) FROM proactive_log").fetchone()[0]
        self.assertEqual(log, 1)
        archived = plugin.store.conn.execute(
            "SELECT COUNT(*) FROM msg_archive WHERE sender_id='bot'"
        ).fetchone()[0]
        self.assertEqual(archived, 1, "主动消息必须进归档")
        self.assertTrue(plugin.memory.affinity.added)

    def test_empty_reply_sends_nothing(self) -> None:
        plugin, proactive, row = self.build(reply="")
        result = run(proactive.send_one(row, reason="idle"))
        self.assertFalse(result["ok"])
        self.assertEqual(plugin.context.sent, [])
        log = plugin.store.conn.execute("SELECT COUNT(*) FROM proactive_log").fetchone()[0]
        self.assertEqual(log, 0, "没生成出来就不该留记录")

    def test_send_failure_does_not_log(self) -> None:
        plugin, proactive, row = self.build(send_fail=True)
        result = run(proactive.send_one(row, reason="idle"))
        self.assertFalse(result["ok"])
        log = plugin.store.conn.execute("SELECT COUNT(*) FROM proactive_log").fetchone()[0]
        self.assertEqual(log, 0)

    def test_umo_rebuilt_from_old_rows(self) -> None:
        plugin, proactive, row = self.build()
        self.assertEqual(row["umo"], "")
        umo = proactive._umo_of(row)
        self.assertEqual(umo, f"aiocqhttp:FriendMessage:{SESSION}")
        group_row = {"session_id": "577084969", "session_kind": "group", "umo": "", "platform": ""}
        self.assertEqual(proactive._umo_of(group_row), "aiocqhttp:GroupMessage:577084969")

    def test_umo_preferred_from_row(self) -> None:
        plugin, proactive, _row = self.build()
        row = {"session_id": SESSION, "session_kind": "private",
               "umo": "aiocqhttp:FriendMessage:3190568934", "platform": "aiocqhttp"}
        self.assertEqual(proactive._umo_of(row), "aiocqhttp:FriendMessage:3190568934")

    # ---------------------------------------------------------------- 结算
    def test_reply_settles_unanswered(self) -> None:
        plugin, proactive, row = self.build()
        run(proactive.send_one(row, reason="idle"))
        # 行动记录写的是真实时钟，所以结算也按真实时钟算
        real = time.time()
        proactive._sess(SESSION)["unanswered"] = 2
        seed_message(plugin, int(real) + 60, sender_id=SESSION, text="刚看到")
        settled = run(proactive.settle_replies(real + 120))
        self.assertEqual(settled, 1)
        self.assertEqual(proactive._sess(SESSION)["unanswered"], 0)
        self.assertIn((SESSION, 3.0), plugin.memory.affinity.added)

    def test_old_log_not_settled(self) -> None:
        plugin, proactive, row = self.build()
        real = time.time()
        run(proactive.send_one(row, reason="idle"))
        plugin.store.conn.execute("UPDATE proactive_log SET at=?", (int(real - 20 * 3600),))
        plugin.store.conn.commit()
        seed_message(plugin, int(real), sender_id=SESSION, text="嗯")
        self.assertEqual(run(proactive.settle_replies(real + 10)), 0)

    # ---------------------------------------------------------------- 时段工具
    def test_spec_cross_midnight(self) -> None:
        self.assertTrue(_in_spec("01:00-08:00", at_today(3, 0)))
        self.assertFalse(_in_spec("01:00-08:00", at_today(12, 0)))
        self.assertTrue(_in_spec("23:00-02:00", at_today(0, 30)))
        self.assertFalse(_in_spec("23:00-02:00", at_today(12, 0)))
        self.assertTrue(_in_spec("", at_today(12, 0)))


if __name__ == "__main__":
    unittest.main()
