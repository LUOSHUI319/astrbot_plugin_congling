"""Offline regression entrypoint; framework substitutes are test-only.

Run with Python directly. Uses temporary SQLite, never plugin data_dir().
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.dont_write_bytecode = True


def module(name):
    obj = types.ModuleType(name)
    obj.__path__ = []
    sys.modules[name] = obj
    return obj


module("astrbot")
api = module("astrbot.api")
api.logger = logging.getLogger("congling-offline-test")
api.AstrBotConfig = dict
events = module("astrbot.api.event")
events.AstrMessageEvent = object
events.MessageChain = list


class Filters:
    EventMessageType = types.SimpleNamespace(ALL="all")

    def __getattr__(self, name):
        return lambda *args, **kwargs: lambda fn: fn


events.filter = Filters()
star = module("astrbot.api.star")
star.Context = object
star.Star = object
star.StarTools = types.SimpleNamespace(get_data_dir=lambda *a: (_ for _ in ()).throw(
    AssertionError("test must not access data directory")))
star.register = lambda *a, **kw: lambda cls: cls
components = module("astrbot.api.message_components")


class Plain:
    def __init__(self, text="", **kwargs):
        self.text = text


components.Plain = components.Reply = Plain
module("astrbot.core")
module("astrbot.core.agent")
agent_message = module("astrbot.core.agent.message")


class TextPart(Plain):
    def mark_as_temp(self):
        self.no_save = True
        return self


agent_message.TextPart = TextPart

from astrbot_plugin_congling.core.config import Config
from astrbot_plugin_congling.core.contracts import MemoryDraft
from astrbot_plugin_congling.core.store import Store
from astrbot_plugin_congling.core.bus import EventBus
from astrbot_plugin_congling.core.budget import Budget
from astrbot_plugin_congling.memory.service import MemoryService
from astrbot_plugin_congling.memory.diary import Diary
from astrbot_plugin_congling.memory.extractor import MemoryExtractor
from astrbot_plugin_congling.night.state import NightService
from astrbot_plugin_congling.night.dream import Dreamer
from astrbot_plugin_congling.night import stages
from astrbot_plugin_congling.humanize.proactive import Proactive
from astrbot_plugin_congling.main import ConglingPlugin


class Regression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="congling-diagnosis-")
        self.store = Store(Path(self.tmp.name) / "test.sqlite")
        await self.store.open()
        self.cfg = Config({"persona": {"name": "洛水"}, "night": {"enabled": True}})
        self.p = types.SimpleNamespace(cfg=self.cfg, store=self.store, night=None,
                                       bus=EventBus(), persona=None, models=None,
                                       context=types.SimpleNamespace(), probe=types.SimpleNamespace(
                                           should_yield=lambda _: False),
                                       is_admin=lambda e: False, diag=None)
        self.p.budget = Budget(self.cfg)
        self.mem = self.p.memory = MemoryService(self.p)

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    async def test_tick_persists_without_transition_and_restores_turns(self):
        night = NightService(self.p)
        night.state.last_tick = 100000
        night.state.energy = 50
        night._fatigue_turns = 7
        night._fatigue_entered = 99000
        with patch("astrbot_plugin_congling.night.energy.fatigue_kind", return_value="night"), \
             patch.object(night, "_past_latest_sleep", return_value=False):
            self.assertEqual(await night.tick(now=100060), [])
            raw = await self.store.kv_get("night.state")
            self.assertEqual(raw["last_tick"], 100060)
            self.assertAlmostEqual(raw["energy"], 49.2)
            restored = NightService(self.p)
            with patch("astrbot_plugin_congling.night.state.time.time", return_value=100060), \
                 patch.object(restored, "_past_latest_sleep", return_value=False):
                await restored.load()
            self.assertEqual(restored._fatigue_turns, 7)
            self.assertEqual(restored.state.energy, night.state.energy)

    async def test_load_error_does_not_overwrite_state(self):
        self.store.kv_get = AsyncMock(side_effect=RuntimeError("数据库未就绪"))
        self.store.kv_set = AsyncMock()
        with self.assertRaises(RuntimeError):
            await NightService(self.p).load()
        self.store.kv_set.assert_not_called()

    async def test_unload_cancels_children_before_close(self):
        plugin = ConglingPlugin.__new__(ConglingPlugin)
        plugin._background_tasks = set()
        plugin._stopping = False
        order = []

        async def child():
            try:
                await asyncio.Event().wait()
            finally:
                order.append("cancelled")

        async def note(what):
            order.append(what)

        plugin.memory = types.SimpleNamespace(flush=lambda: note("flush"))
        plugin.night = types.SimpleNamespace(shutdown=lambda: note("night"))
        plugin.store = types.SimpleNamespace(close=lambda: note("close"))
        task = plugin.spawn_background(child())
        await asyncio.sleep(0)
        await plugin.terminate()
        self.assertTrue(task.done())
        self.assertEqual(order, ["cancelled", "flush", "night", "close"])
        self.assertFalse(plugin._background_tasks)
        with self.assertRaises(RuntimeError):
            plugin.spawn_background(child())

    async def test_affinity_read_is_not_write_and_text_is_not_user(self):
        await self.mem.affinity.get("洛水")
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM affinity"), 0)
        self.assertEqual(await self.mem.subjects.identify(subject_text="路过的小李"), "")
        await self.mem.subjects.identify(sender_id="123", sender_name="洛水")
        await self.mem.affinity.add("123", 4)
        await self.mem.affinity.add("历史文本名", 50)
        board = await self.mem.affinity.leaderboard()
        self.assertEqual([x["subject_key"] for x in board], ["123"])
        self.assertEqual([x["user_id"] for x in await self.mem.subjects.list()], ["123"])

    async def test_evidence_roles_and_subjects(self):
        rows = [{"id": 1, "msg_id": "real-a", "sender_id": "123", "sender_name": "小明",
                 "text": "我喜欢茶。洛水今天没睡"},
                {"id": 2, "msg_id": "real-b", "sender_id": "bot", "sender_name": "洛水"}]
        drafts = [MemoryDraft(text="用户喜欢茶", subject="用户", evidence_msg_ids=["0"]),
                  MemoryDraft(text="洛水今天没睡", subject="__self__", evidence_msg_ids=["0"]),
                  MemoryDraft(text="小李爱跑步", subject="小李", evidence_msg_ids=["0"]),
                  MemoryDraft(text="我去月球了", subject="__self__", evidence_msg_ids=["1"]),
                  MemoryDraft(text="小明爱喝水", subject="小明", evidence_msg_ids=["99"])]
        await self.mem._prepare_drafts(drafts, rows)
        self.assertEqual(len(drafts), 2)
        self.assertEqual(drafts[0].subject_key, "123")
        self.assertEqual(drafts[0].text, "小明喜欢茶")
        self.assertEqual(drafts[0].evidence_msg_ids, ["real-a"])
        self.assertEqual(drafts[1].text, "我今天没睡")
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM affinity"), 0)
        await self.store.execute(
            "INSERT INTO msg_archive(session_id,session_kind,sender_id,text,ts,msg_id) VALUES('123','private','bot','我想你',1,'b')")
        self.mem.extractor.extract = AsyncMock(return_value=[])
        self.mem.extractor.last_ok = True
        await self.mem._extract_session("123", force=True)
        turns = self.mem.extractor.extract.call_args.args[0]
        self.assertEqual(turns[0]["role"], "assistant")

    async def test_actions_idempotent_private_and_sleep_readable(self):
        self.p.night = types.SimpleNamespace(state=types.SimpleNamespace(
            state=types.SimpleNamespace(is_asleep=lambda: True)))
        a = await self.mem.record_self_event("proactive", "p1", "我找过A：喝茶吗", session_id="A", session_kind="private")
        b = await self.mem.record_self_event("proactive", "p1", "重复", session_id="A", session_kind="private")
        self.assertEqual(a, b)
        await self.mem.record_self_event("dream", "d1", "我做梦了")
        await self.mem.record_self_event("dream", "d2", "私人梦正文", session_id="__private__", session_kind="owner")
        a_rows = await self.mem.self_events("A", "private")
        b_rows = await self.mem.self_events("B", "private")
        # P4：没有可核验梦等级的旧自述不主动注入；梦改走专用检索。
        self.assertEqual(len(a_rows), 1)
        self.assertEqual(len(b_rows), 0)
        self.assertEqual(len(await self.mem.self_events("B", "private", include_private=True)), 0)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 3)
        self.assertEqual(await self.store.scalar("SELECT SUM(unread) FROM mem_atom"), 0)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM affinity"), 0)
        self.assertEqual(await self.mem.retriever._candidates("user", "B", self.cfg.persona_id, True, "all"), [])

    async def test_action_injection_preserves_cached_system(self):
        await self.mem.record_self_event("diary", "1", "我昨天写日记了")
        msg = types.SimpleNamespace(text="昨天写日记了吗", session_id="B", session_kind="private",
                                    sender_id="B", sender_name="B", msg_id="b1", ts=1)
        event = types.SimpleNamespace(_congling_msg=msg, get_group_id=lambda: "", get_sender_id=lambda: "B")
        req = types.SimpleNamespace(system_prompt="stable prefix", extra_user_content_parts=[])
        await self.mem.inject(event, req)
        await self.mem.inject(event, req)
        self.assertEqual(req.system_prompt, "stable prefix")
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertTrue(req.extra_user_content_parts[0].no_save)
        self.assertIn("我昨天写日记了", req.extra_user_content_parts[0].text)

    async def test_concurrent_action_idempotence_and_type_recall(self):
        ids = await asyncio.gather(*[
            self.mem.record_self_event("diary", "one", "我昨天写了日记") for _ in range(4)])
        self.assertEqual(len(set(ids)), 1)
        for n in range(10):
            await self.mem.record_self_event("dream", str(n), "我做了梦")
        rows = await self.mem.self_events("B", "private", query="你昨天写日记了吗")
        self.assertEqual([r["text"] for r in rows], ["我昨天写了日记"])

    async def test_heartbeat_failure_does_not_skip_proactive(self):
        plugin = ConglingPlugin.__new__(ConglingPlugin)
        plugin.cfg = self.cfg
        plugin.night = types.SimpleNamespace(state=types.SimpleNamespace(
            tick=AsyncMock(side_effect=RuntimeError("tick failed"))))
        plugin.proactive = types.SimpleNamespace(tick=AsyncMock())
        calls = 0

        async def sleep(_):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise asyncio.CancelledError

        with patch("astrbot_plugin_congling.main.asyncio.sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await plugin._beat_loop()
        plugin.proactive.tick.assert_awaited_once()

    async def test_group_self_fact_recall_and_no_cross_audience_dedupe(self):
        draft = MemoryDraft(text="我今天没睡", subject_key="__self__", session_id="G", session_kind="group")
        one = await self.mem.atoms.add(draft, scope="group", persona_id=self.cfg.persona_id)
        draft.session_id = "H"
        two = await self.mem.atoms.add(draft, scope="group", persona_id=self.cfg.persona_id)
        self.assertNotEqual(one, two)
        rows = await self.mem.retriever._candidates("group", "G", self.cfg.persona_id)
        self.assertEqual([r["id"] for r in rows], [one])

    async def test_dream_success_and_failed_persistence(self):
        self.cfg._raw.setdefault("night", {})["dream_enabled"] = True
        dreamer = Dreamer(self.p)
        dreamer._call_model = AsyncMock(return_value=("我看见一朵花", 3, ["美梦"]))
        entry = await dreamer.make()
        self.assertIsNotNone(entry)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom WHERE scope='self'"), 1)
        restored = Dreamer(self.p)
        await restored.load()
        self.assertEqual(restored._last_dream_at, entry.created_at)
        dreamer.save = AsyncMock(return_value=False)
        self.assertIsNone(await dreamer.make())
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom WHERE scope='self'"), 1)

    async def test_diary_success_and_failed_insert(self):
        self.cfg._raw.setdefault("diary", {})["enabled"] = True
        diary = Diary(self.p)
        diary._gather = AsyncMock(return_value={"materials": "今天聊天", "atoms": []})
        diary._compose_by_model = AsyncMock(return_value={"text": "我今天很开心", "mood": "安静"})
        self.assertIsNotNone(await diary.compose("2026-10-30"))
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom WHERE scope='self'"), 1)
        diary._insert = AsyncMock(return_value=0)
        self.assertIsNone(await diary.compose("2026-10-31"))
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom WHERE scope='self'"), 1)

    async def test_proactive_success_and_empty_does_not_remember(self):
        proactive = Proactive(self.p)
        self.p.context.send_message = AsyncMock()
        proactive.compose = AsyncMock(return_value="想喝茶吗")
        row = {"session_id": "A", "session_kind": "private"}
        self.assertTrue((await proactive.send_one(row))["ok"])
        self.assertEqual(len(await self.mem.self_events("A", "private")), 1)
        self.assertEqual(len(await self.mem.self_events("B", "private")), 0)
        proactive.compose = AsyncMock(return_value="")
        self.assertFalse((await proactive.send_one(row))["ok"])
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom WHERE scope='self'"), 1)

    async def test_qzone_remembers_only_successful_publish(self):
        from astrbot_plugin_congling.qqext.qzone import QzoneBridge

        bridge = QzoneBridge(self.p)
        bridge.auth = AsyncMock(return_value=object())
        with patch("astrbot_plugin_congling.qqext.qzone.QzoneClient") as client:
            client.return_value.publish = AsyncMock(return_value={"ok": True, "fid": "post-1"})
            self.assertTrue((await bridge._publish_once("今天看见花开了"))["ok"])
            self.assertEqual(len(await self.mem.self_events("B", "private")), 1)
            client.return_value.publish = AsyncMock(side_effect=RuntimeError("publish failed"))
            with self.assertRaises(RuntimeError):
                await bridge._publish_once("未发送")
            self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom WHERE scope='self'"), 1)

    async def test_idle_30_is_only_candidate_gate(self):
        self.cfg._raw["proactive"] = {"quiet_hours": "00:00-00:00", "active_hours": "00:00-23:59",
                                     "idle_min_minutes": 30, "promise_nudge": False, "allow_group": True}
        proactive = Proactive(self.p)
        proactive._promise_warm = AsyncMock(return_value=False)
        self.mem.affinity.get = AsyncMock(return_value={"stage": 1})
        now = 1793502000
        with patch("astrbot_plugin_congling.humanize.proactive.random.uniform", return_value=14.9):
            ok, _, score = await proactive.should({"session_id": "A", "session_kind": "private", "idle_seconds": 210*60}, now)
            self.assertFalse(ok)
            self.assertEqual(score, 59.9)
        with patch("astrbot_plugin_congling.humanize.proactive.random.uniform", return_value=15):
            ok, _, score = await proactive.should({"session_id": "G", "session_kind": "group", "idle_seconds": 500*60}, now)
            self.assertFalse(ok)
            self.assertEqual(score, 50)

    async def test_15_minutes_does_not_force_rem(self):
        self.cfg._raw["night"].update(dream_enabled=True, dream_start_after_minutes=15)
        dreamer = Dreamer(self.p)
        self.assertTrue(dreamer.due(sleep_at=1000, already=0, now=1900))
        stage = stages.at(self.cfg, sleep_at=1000, wake_at=1000+8*3600, now=1900)
        self.assertNotEqual(stage["key"], "rem")
        plan = stages.build_plan(self.cfg, sleep_at=1000, wake_at=1000+8*3600)
        self.assertEqual(next(s["start"] for s in plan if s["key"] == "rem"), 1000+80*60)

    def test_all_python_parses(self):
        for source in ROOT.rglob("*.py"):
            ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Regression)
    for name in ("test_sleep_stages", "test_unread_gate", "test_diary", "test_proactive",
                 "test_memory_level", "test_night_wake_dream", "test_dream_hint"):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(
            importlib.import_module("astrbot_plugin_congling.tests." + name)))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(not result.wasSuccessful())
