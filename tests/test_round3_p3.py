"""写入链断言：真实结构迁移、同事务故障注入、身份、四条链及隐私/缓存。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot_plugin_congling.tests import run_diagnosis_regressions as base
from astrbot_plugin_congling.core.contracts import MemoryDraft, PerceivedMessage, DreamEntry
from astrbot_plugin_congling.core.store import _SCHEMA
from astrbot_plugin_congling.memory.ledger import EventLedger, PRIVATE_AUDIENCE
from astrbot_plugin_congling.memory.refs import event_key
from astrbot_plugin_congling.memory.perspective import material_label, remember_action
from astrbot_plugin_congling.memory.subject import self_names
from astrbot_plugin_congling.qqext.qzone import QzoneBridge
from astrbot_plugin_congling.qqext.qzone_daily import QzoneDaily
from astrbot_plugin_congling.humanize.persona import PersonaService, SELF_ACTION_RULE
from astrbot_plugin_congling.night import NightModule
from astrbot_plugin_congling.capability.providers import memory_search, build_builtin


class Fixture(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.Regression.asyncSetUp
    asyncTearDown = base.Regression.asyncTearDown

    async def counts(self):
        return (await self.store.scalar("SELECT COUNT(*) FROM event_ledger"),
                await self.store.scalar("SELECT COUNT(*) FROM mem_atom WHERE scope='self'"))

    def event(self, *, group="", uid="123", query="你写日记了吗", admin=False):
        msg = PerceivedMessage(text=query, session_id=group or uid,
                               session_kind="group" if group else "private", sender_id=uid,
                               meta={"platform": "qq"})
        return types.SimpleNamespace(_congling_msg=msg, get_group_id=lambda: group,
                                     get_sender_id=lambda: uid, get_platform_name=lambda: "qq",
                                     unified_msg_origin=group or uid, admin=admin)

    async def raw_atom(self, text, *, scope="group", sid="456", uid="123", event_type="", ref="user:qq:123"):
        kind = "private" if scope == "user" else "group"
        return await self.store.execute(
            "INSERT INTO mem_atom(persona_id,scope,subject_id,text,session_id,session_kind,"
            "subject_ref,actor_ref,session_ref,event_type,mem_level,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.cfg.persona_id, scope, uid, text, sid, kind, ref, "user:qq:" + uid,
             f"session:qq:{kind}:{sid}", event_type, 3, int(time.time())))


class Migration(Fixture):
    async def test_real_v7_missing_columns_keeps_14_cursors_and_history(self):
        path = Path(self.tmp.name) / "v7.db"
        with sqlite3.connect(path) as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            for i in range(14):
                conn.execute("INSERT INTO inject_cursor VALUES(?,?,?,?)", (str(i), 99 + i, "[1]", 100))
            conn.execute("INSERT INTO mem_atom(text,subject_id) VALUES('历史正文不许改','大猫')")
            conn.execute("PRAGMA user_version=7")
        store = base.Store(path, persona_id="live")
        try:
            await store.open()
            self.assertEqual(await store.scalar("PRAGMA user_version"), 7)
            self.assertEqual(await store.scalar("SELECT COUNT(*) FROM inject_cursor WHERE persona_id='live'"), 14)
            self.assertEqual(await store.scalar("SELECT last_msg_row FROM inject_cursor WHERE session_id='13'"), 112)
            atom = await store.fetch_one("SELECT * FROM mem_atom")
            self.assertEqual((atom["text"], atom["audience"], atom["ref_status"]), ("历史正文不许改", "legacy", "legacy"))
            cols = {r["name"] for r in await store.fetch_all("PRAGMA table_info(event_ledger)")}
            self.assertTrue({"event_id", "persona_id", "event_key", "event_type", "subject_ref", "actor_ref",
                             "session_ref", "audience", "occurred_at", "recorded_at", "status", "result_text",
                             "result_ref", "result_json", "memory_id"} <= cols)
            before = await store.fetch_all("SELECT * FROM mem_atom")
            await store.close()
            await store.open()
            self.assertEqual(before, await store.fetch_all("SELECT * FROM mem_atom"))
        finally:
            await store.close()

    async def test_high_version_with_new_cursor_but_missing_refs_is_repaired(self):
        path = Path(self.tmp.name) / "partial.db"
        with sqlite3.connect(path) as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute("ALTER TABLE inject_cursor ADD COLUMN persona_id TEXT DEFAULT 'ling'")
            conn.execute("INSERT INTO mem_atom(text) VALUES('不改写')")
            conn.execute("PRAGMA user_version=77")
        store = base.Store(path)
        try:
            await store.open()
            self.assertEqual(await store.scalar("SELECT ref_status FROM mem_atom"), "legacy")
            self.assertEqual(await store.scalar("PRAGMA user_version"), 77)
        finally:
            await store.close()

    async def test_legacy_self_text_not_rewritten_or_promoted(self):
        await self.store.execute("INSERT INTO mem_atom(scope,subject_id,text) VALUES('self','owner:__private__','旧梦正文')")
        await self.store.close()
        await self.store.open()
        self.assertEqual(await self.store.scalar("SELECT audience FROM mem_atom"), "legacy")
        self.assertEqual(await self.store.scalar("SELECT text FROM mem_atom"), "旧梦正文")
        self.assertEqual(await self.mem.self_events("g", "group", include_private=True), [])

    async def test_p3_migration_failure_rolls_back_structure_and_data(self):
        path = Path(self.tmp.name) / "migration-failure.db"
        with sqlite3.connect(path) as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO inject_cursor VALUES('g',14,'[1]',100)")
            conn.execute("INSERT INTO mem_atom(text) VALUES('历史原文')")
            conn.execute("PRAGMA user_version=7")
        store = base.Store(path)
        original = store._migrate_events
        def fail():
            original()
            raise RuntimeError("fault after P3 DDL")
        # 迁移不再整段包大事务（那会在真实库上锁几十秒）。要求变成：
        # 失败不拖垮启动、历史数据不破坏、下次启动能补齐。
        with patch.object(store, "_migrate_events", side_effect=fail):
            await store.open()          # 不抛异常：降级启动
        self.assertTrue(store.ready)
        await store.close()
        await store.open()              # 重开补齐
        with sqlite3.connect(path) as conn:
            self.assertIn("audience", {r[1] for r in conn.execute("PRAGMA table_info(mem_atom)")})
            self.assertEqual(conn.execute("SELECT text FROM mem_atom").fetchone()[0], "历史原文")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='event_ledger'").fetchone()[0], 1)
        await store.close()


class Ledger(Fixture):
    async def test_success_is_one_memory_with_all_refs_and_result(self):
        aid = await self.mem.record_self_event("diary", "12", "我写了日记", private_text="全文秘密",
                                               occurred_at=12345, result={"diary_id": 12})
        self.assertEqual(await self.counts(), (1, 1))
        row = await self.store.fetch_one("SELECT * FROM event_ledger")
        atom = await self.mem.atoms.get(aid)
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["occurred_at"], 12345)
        self.assertGreater(row["recorded_at"], 12345)
        self.assertEqual(row["subject_ref"], row["actor_ref"])
        self.assertEqual(row["actor_ref"], "bot:ling")
        self.assertEqual(row["audience"], PRIVATE_AUDIENCE)
        self.assertEqual(row["memory_id"], aid)
        self.assertEqual(atom["event_id"], row["event_id"])
        self.assertEqual(atom["event_key"], event_key("diary", "12"))
        self.assertEqual((atom["text"], atom["public_text"]), ("全文秘密", "我写了日记"))
        self.assertEqual(json.loads(row["result_json"]), {"diary_id": 12})

    async def test_atom_failure_rolls_back_new_ledger(self):
        await self.store.execute("CREATE TRIGGER fail_atom BEFORE INSERT ON mem_atom BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.mem.record_self_event("dream", "a", "我做了梦")
        self.assertEqual(await self.counts(), (0, 0))

    async def test_result_update_failure_rolls_back_both(self):
        await self.store.execute("CREATE TRIGGER fail_link BEFORE UPDATE OF memory_id ON event_ledger BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.mem.record_self_event("dream", "a", "我做了梦")
        self.assertEqual(await self.counts(), (0, 0))

    async def test_planned_failure_restores_planned_without_memory(self):
        await self.mem.ledger.record("qzone", "a", status="planned")
        await self.store.execute("CREATE TRIGGER fail_atom BEFORE INSERT ON mem_atom BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.mem.record_self_event("qzone", "a", "我发了说说")
        self.assertEqual(await self.store.scalar("SELECT status FROM event_ledger"), "planned")
        self.assertEqual(await self.counts(), (1, 0))

    async def test_state_machine_terminals_never_become_memories(self):
        for status in ("failed", "expired"):
            await self.mem.ledger.record("qzone", status, status="planned")
            await self.mem.ledger.record("qzone", status, status=status)
            with self.assertRaises(ValueError):
                await self.mem.record_self_event("qzone", status, "不能复活")
        self.assertEqual(await self.counts(), (2, 0))
        await self.mem.ledger.record("qzone", "ok", status="planned")
        await self.mem.record_self_event("qzone", "ok", "我发了说说")
        self.assertEqual(await self.counts(), (3, 1))
        with self.assertRaises(ValueError):
            await self.mem.ledger.record("qzone", "ok", status="failed")

    async def test_invalid_status_or_audience_rejected(self):
        for kwargs in ({"status": "fake"}, {"session_id": "g"}, {"session_kind": "group"}):
            with self.assertRaises(ValueError):
                await self.mem.ledger.record("diary", "a", "我写了日记", **kwargs)
        self.assertEqual(await self.counts(), (0, 0))

    async def test_concurrent_independent_services_and_restart_idempotent(self):
        other = base.MemoryService(self.p)
        ids = await asyncio.gather(*[m.record_self_event("diary", "same", "我写了日记")
                                     for m in [self.mem, other] * 6])
        self.assertEqual(len(set(ids)), 1)
        await self.store.close()
        await self.store.open()
        self.assertEqual(await other.record_self_event("diary", "same", "不覆盖"), ids[0])
        self.assertEqual(await self.counts(), (1, 1))
        self.assertEqual(await self.store.scalar("SELECT text FROM mem_atom"), "我写了日记")

    async def test_persona_and_event_type_partition_keys(self):
        await self.mem.record_self_event("diary", "1", "我写了日记")
        await self.mem.record_self_event("dream", "1", "我做了梦")
        cfg = base.Config({"persona": {"persona_id": "other"}})
        other = EventLedger(types.SimpleNamespace(cfg=cfg, store=self.store))
        await other.record("diary", "1", "另一人格的日记")
        self.assertEqual(await self.counts(), (3, 3))
        # P4 后梦不再由无等级账本记录直接注入；事件身份仍须独立保存。
        self.assertEqual(await self.store.scalar(
            "SELECT COUNT(*) FROM event_ledger WHERE persona_id=?", (self.cfg.persona_id,)), 2)
        self.assertEqual(len(await self.mem.self_events("A", "private")), 1)

    async def test_nested_transaction_refused_without_committing_other_work(self):
        await self.store.begin()
        await self.store.execute("INSERT INTO kv(k,v) VALUES('uncommitted','1')")
        with self.assertRaises(RuntimeError):
            await self.mem.record_self_event("diary", "1", "我写了日记")
        await self.store.rollback()
        self.assertIsNone(await self.store.kv_get("uncommitted"))
        self.assertEqual(await self.counts(), (0, 0))

    async def test_ledger_exists_before_atom_insert(self):
        await self.store.execute(
            "CREATE TRIGGER require_event BEFORE INSERT ON mem_atom WHEN NOT EXISTS("
            "SELECT 1 FROM event_ledger WHERE event_id=NEW.event_id AND status='succeeded') "
            "BEGIN SELECT RAISE(ABORT,'missing ledger'); END")
        await self.mem.record_self_event("qzone", "one", "已发布")
        self.assertEqual(await self.counts(), (1, 1))

    async def test_separate_connections_share_unique_idempotency(self):
        other_store = base.Store(self.store.path)
        await other_store.open()
        try:
            ledger = EventLedger(types.SimpleNamespace(cfg=self.cfg, store=other_store))
            ids = await asyncio.gather(self.mem.record_self_event("qzone", "shared", "结果一"),
                                       ledger.record("qzone", "shared", "结果二"))
            self.assertEqual(ids[0], ids[1])
            self.assertEqual(await self.counts(), (1, 1))
        finally:
            await other_store.close()


class Actions(Fixture):
    async def test_diary_positive_id_only_and_full_body(self):
        self.cfg._raw["diary"] = {"enabled": True}
        diary = base.Diary(self.p)
        diary._gather = AsyncMock(return_value={"materials": "今天聊天", "atoms": []})
        body = "私人日记" * 250 + "尾部验收"
        diary._compose_by_model = AsyncMock(return_value={"text": body, "mood": "安静"})
        for bad in (0, -1):
            with patch.object(diary, "_insert", AsyncMock(return_value=bad)):
                self.assertIsNone(await diary.compose("2026-11-01"))
            self.assertEqual(await self.counts(), (0, 0))
        item = await diary.compose("2026-11-01")
        self.assertGreater(item["id"], 0)
        await diary.compose("2026-11-01")
        self.assertEqual(await self.counts(), (1, 1))
        self.assertIn(body, await self.store.scalar("SELECT result_text FROM event_ledger"))

    async def test_dream_save_failure_no_event_then_success_one_event(self):
        self.cfg._raw["night"]["dream_enabled"] = True
        dreamer = base.Dreamer(self.p)
        dreamer._call_model = AsyncMock(return_value=("梦全文" * 250, 3, ["美梦"]))
        with patch.object(dreamer, "save", AsyncMock(return_value=False)):
            self.assertIsNone(await dreamer.make())
        self.assertEqual(dreamer.living(), [])
        self.assertEqual(await self.counts(), (0, 0))
        entry = await dreamer.make()
        self.assertEqual(await self.counts(), (1, 1))
        self.assertIn(entry.text, await self.store.scalar("SELECT text FROM mem_atom"))

    async def test_qzone_false_missing_ok_exception_and_success(self):
        bridge = QzoneBridge(self.p)
        bridge.auth = AsyncMock(return_value=object())
        with patch("astrbot_plugin_congling.qqext.qzone.QzoneClient") as client:
            for result in ({"ok": False}, {"fid": "unconfirmed"}, None):
                client.return_value.publish = AsyncMock(return_value=result)
                self.assertFalse((await bridge._publish_once("不该留下"))["ok"])
            self.assertEqual(await self.counts(), (0, 0))
            client.return_value.publish = AsyncMock(side_effect=RuntimeError())
            with self.assertRaises(RuntimeError):
                await bridge._publish_once("失败")
            client.return_value.publish = AsyncMock(return_value={"ok": True, "fid": "post1"})
            await bridge._publish_once("晴天我看到了花")
            await bridge._publish_once("重复结果")
            self.assertEqual(await self.counts(), (1, 1))

    async def test_proactive_false_response_not_success(self):
        proactive = base.Proactive(self.p)
        proactive.compose = AsyncMock(return_value="一起喝茶吗")
        self.p.context.send_message = AsyncMock(return_value=False)
        with patch("astrbot_plugin_congling.humanize.proactive.asyncio.sleep", AsyncMock()):
            self.assertFalse((await proactive.send_one({"session_id": "123", "session_kind": "private"}))["ok"])
        self.assertEqual(await self.counts(), (0, 0))

    async def test_partial_send_remembers_only_delivered_fragment(self):
        proactive = base.Proactive(self.p)
        proactive.compose = AsyncMock(return_value="第一段。秘密第二段")
        self.p.composer = types.SimpleNamespace(split=lambda *a: ["第一段", "秘密第二段"])
        self.p.context.send_message = AsyncMock(side_effect=[None, False, False])
        with patch("astrbot_plugin_congling.humanize.proactive.asyncio.sleep", AsyncMock()):
            result = await proactive.send_one({"session_id": "123", "session_kind": "private"})
        self.assertFalse(result["ok"])
        text = await self.store.scalar("SELECT text FROM mem_atom")
        self.assertIn("第一段", text)
        self.assertNotIn("秘密第二段", text)
        self.assertEqual(await self.counts(), (1, 1))

    async def test_sent_but_quota_save_failed_still_remembers(self):
        proactive = base.Proactive(self.p)
        proactive.compose = AsyncMock(return_value="已发送的片段")
        self.p.context.send_message = AsyncMock(return_value=None)
        proactive.save = AsyncMock(side_effect=RuntimeError("quota write failed"))
        with self.assertRaises(RuntimeError):
            await proactive.send_one({"session_id": "123", "session_kind": "private"})
        self.assertEqual(await self.counts(), (1, 1))

    async def test_diary_insert_collision_uses_persisted_body(self):
        self.cfg._raw["diary"] = {"enabled": True}
        diary = base.Diary(self.p)
        await self.store.execute("INSERT INTO mem_diary(persona_id,day_key,text) VALUES(?,?,?)",
                                 (self.cfg.persona_id, "2026-11-01", "已经保存的正文"))
        diary.exists = AsyncMock(return_value=False)  # 模拟预检与写入之间另一生成器抢先保存
        diary._gather = AsyncMock(return_value={"materials": "素材", "atoms": []})
        diary._compose_by_model = AsyncMock(return_value={"text": "竞争失败的草稿"})
        await diary.compose("2026-11-01")
        text = await self.store.scalar("SELECT text FROM mem_atom")
        self.assertIn("已经保存的正文", text)
        self.assertNotIn("竞争失败的草稿", text)


class Identity(Fixture):
    def row(self, text, sid="123", name="望山", mid="m1"):
        return {"sender_id": sid, "sender_name": name, "msg_id": mid, "text": text}

    def test_render_actor_id_name_without_generic_bot(self):
        rendered, used = base.MemoryExtractor._render(
            [{"role": "user", "sender_id": "123", "sender_name": "望山", "platform": "qq", "text": "我今天很累", "idx": 0},
             {"role": "assistant", "text": "休息吧", "idx": 1}], bot_name="洛水", persona_id="ling")
        self.assertIn("user 望山 (ID=123, actor_ref=user:qq:123)", rendered)
        self.assertIn("assistant 洛水 (ID=ling, actor_ref=bot:ling", rendered)
        self.assertNotIn("机器人", rendered)
        self.assertEqual(used, [0, 1])

    async def test_pronoun_corrected_to_current_nickname(self):
        drafts = [MemoryDraft(text="我今天很累", subject="我", evidence_msg_ids=["0"])]
        await self.mem._prepare_drafts(drafts, [self.row("我今天很累")])
        self.assertEqual([(d.text, d.subject_key) for d in drafts], [("望山今天很累", "123")])

    async def test_twenty_bot_named_statements_never_use_name_as_self_subject(self):
        rows = [self.row(f"洛水今天看了电影{i}", mid=str(i)) for i in range(20)]
        drafts = [MemoryDraft(text=f"洛水今天看了电影{i}", subject="__self__", evidence_msg_ids=[str(i)]) for i in range(20)]
        await self.mem._prepare_drafts(drafts, rows)
        self.assertEqual(len(drafts), 20)
        for draft in drafts:
            self.assertTrue(draft.text.startswith("我今天"))
            self.assertNotIn("洛水今天", draft.text)

    async def test_assistant_empty_invented_and_question_evidence_rejected(self):
        rows = [self.row("我很累", sid="bot"), self.row("你困吗？"), self.row(""), self.row("如果我喜欢茶")]
        drafts = [MemoryDraft(text="望山很困", subject="123", evidence_msg_ids=[str(i)]) for i in range(5)]
        await self.mem._prepare_drafts(drafts, rows)
        self.assertEqual(drafts, [])

    async def test_human_self_never_becomes_bot(self):
        for text in ("我今天很累", "洛水，我今天很累"):
            drafts = [MemoryDraft(text="我今天很累", subject="__self__", evidence_msg_ids=["0"])]
            await self.mem._prepare_drafts(drafts, [self.row(text)])
            self.assertEqual(drafts, [])

    async def test_unsupported_fact_and_negation_flip_rejected(self):
        for fact, evidence in (("望山会开直升飞机", "我今天很累"), ("望山喜欢喝茶", "我不喜欢喝茶")):
            drafts = [MemoryDraft(text=fact, subject="123", evidence_msg_ids=["0"])]
            await self.mem._prepare_drafts(drafts, [self.row(evidence)])
            self.assertEqual(drafts, [])

    async def test_ambiguous_subject_is_not_last_speaker(self):
        rows = [self.row("我喜欢茶", name="同名"), self.row("我喜欢茶", sid="456", name="同名")]
        drafts = [MemoryDraft(text="同名喜欢茶", subject="同名", evidence_msg_ids=["0", "1"])]
        await self.mem._prepare_drafts(drafts, rows)
        self.assertEqual(drafts, [])

    async def test_four_self_name_forms_persist_and_identify(self):
        for i, text in enumerate(("我叫大猫", "叫我大猫", "我是大猫", "可以叫我大猫")):
            with self.subTest(text=text):
                uid = str(100 + i)
                await self.mem.subjects.observe_names(uid, "昵称" + uid, text)
                aliases = json.loads(await self.store.scalar("SELECT aliases FROM mem_subject WHERE user_id=?", (uid,)))
                self.assertIn("大猫", aliases)
        self.assertEqual(await self.mem.subjects.identify(subject_text="大猫"), "")  # 四人重名不可猜

    def test_alias_false_positive_guards(self):
        for text in ("他说我叫大猫", "我叫大猫吗？", "如果我叫大猫", "我是程序员",
                     "我是大猫的朋友", "我不是大猫", "不要叫我大猫", "“我叫大猫”", "你叫我大猫吗"):
            self.assertEqual(self_names(text), [], text)
        self.assertEqual(self_names("可以叫我「大猫」，很高兴认识你"), ["大猫"])

    async def test_alias_indexes_without_llm_or_history_rewrite(self):
        aid = await self.raw_atom("望山昨天修好了电脑")
        msg = PerceivedMessage(sender_id="123", sender_name="新昵称", text="叫我大猫", session_id="456", session_kind="group")
        await self.mem._on_perceived(msg)
        self.assertEqual(await self.mem.subjects.identify(subject_text="大猫"), "123")
        self.assertEqual(await self.mem.subjects.display_name("123"), "新昵称")
        self.assertEqual((await self.mem.atoms.get(aid))["text"], "望山昨天修好了电脑")
        drafts = [MemoryDraft(text="大猫喜欢喝茶", subject="大猫", evidence_msg_ids=["0"])]
        await self.mem._prepare_drafts(drafts, [self.row("我喜欢喝茶", name="新昵称")])
        self.assertEqual(drafts[0].text, "新昵称喜欢喝茶")
        self.assertEqual(drafts[0].subject_key, "123")

    async def test_archive_replay_indexes_even_empty_extraction(self):
        await self.store.execute("INSERT INTO msg_archive(session_id,sender_id,sender_name,text,ts) VALUES('456','123','望山','我是大猫',1)")
        self.mem.extractor.extract = AsyncMock(return_value=[])
        self.mem.extractor.last_ok = True
        await self.mem._extract_session("456", force=True)
        self.assertEqual(await self.mem.subjects.identify(subject_text="大猫"), "123")

    async def test_alias_search_without_alias_in_text_keeps_private_boundary(self):
        self.cfg._raw["memory"] = {"cross_session_scope": "all", "cross_session_recall": True}
        await self.mem.subjects.observe_names("123", "望山", "叫我大猫")
        aid = await self.raw_atom("望山昨天修好了电脑", sid="oldgroup")
        await self.raw_atom("望山的私聊秘密", scope="user", sid="123")
        for event in (None, self.event(group="newgroup", uid="789")):
            result = await memory_search(self.p, "你还记得大猫吗", _event=event)
            self.assertIsInstance(result["text"], list, result)
            self.assertEqual([h.atom_id for h in result["text"]], [aid])
            self.assertEqual(result["text"][0].text, "望山昨天修好了电脑")
        await self.mem.subjects.observe_names("789", "小李", "叫我大猫")
        result = await memory_search(self.p, "大猫")
        self.assertIsInstance(result["text"], str)  # 同名不能猜主体

    async def test_alias_search_cannot_enable_cross_session_off(self):
        self.cfg._raw["memory"] = {"cross_session_scope": "off", "cross_session_recall": False}
        await self.mem.subjects.observe_names("123", "望山", "叫我大猫")
        await self.raw_atom("望山修好了电脑", sid="oldgroup")
        result = await memory_search(self.p, "大猫", _event=self.event(group="newgroup"))
        self.assertIsInstance(result["text"], str)

    async def test_id_subject_still_replaces_alias_prefix_with_current_name(self):
        await self.mem.subjects.observe_names("123", "望山", "叫我大猫")
        drafts = [MemoryDraft(text="大猫喜欢喝茶", subject="123", evidence_msg_ids=["0"])]
        await self.mem._prepare_drafts(drafts, [self.row("我喜欢喝茶", name="新昵称")])
        self.assertEqual(drafts[0].text, "新昵称喜欢喝茶")

    async def test_anonymous_name_cannot_attach_alias_to_someone_else(self):
        await self.mem.subjects.identify(sender_id="123", sender_name="望山")
        await self.mem.subjects.observe_names("", "望山", "叫我冒名者")
        self.assertNotIn("冒名者", await self.mem.subjects.aliases_of("123"))

    async def test_concurrent_alias_additions_are_not_lost(self):
        await self.mem.subjects.identify(sender_id="123", sender_name="望山")
        await asyncio.gather(*(self.mem.subjects.add_alias("123", f"别名{i}") for i in range(8)))
        aliases = await self.mem.subjects.aliases_of("123")
        self.assertTrue({f"别名{i}" for i in range(8)} <= set(aliases))


class PrivacyMaterials(Fixture):
    async def test_private_fulltext_admin_private_only(self):
        await self.mem.record_self_event("diary", "1", "我写了日记", private_text="私密全文不能外泄")
        for kind, allow in (("group", False), ("group", True), ("private", False)):
            rows = await self.mem.self_events("123", kind, include_private=allow)
            self.assertEqual([r["text"] for r in rows], ["我写了日记"])
        rows = await self.mem.self_events("123", "private", include_private=True)
        self.assertEqual([r["text"] for r in rows], ["私密全文不能外泄"])

    async def test_injection_checks_admin_and_preserves_prefix(self):
        await self.mem.record_self_event("diary", "1", "我写了日记", private_text="秘密正文")
        self.p.is_admin = lambda e: e.admin
        for group, admin, visible in (("456", True, False), ("", False, False), ("", True, True)):
            req = types.SimpleNamespace(system_prompt="STATIC", extra_user_content_parts=[])
            await self.mem.inject(self.event(group=group, admin=admin), req)
            self.assertEqual(req.system_prompt, "STATIC")
            self.assertTrue(all(p.no_save for p in req.extra_user_content_parts))
            self.assertEqual("秘密正文" in "".join(p.text for p in req.extra_user_content_parts), visible)

    async def test_self_event_types_answer_four_questions(self):
        cases = (("diary", "写日记", "写了日记"), ("dream", "做梦", "做了一段梦"),
                 ("qzone", "发说说", "发了空间说说"), ("proactive", "找过我", "主动问你喝茶"))
        for kind, _, text in cases:
            await self.mem.record_self_event(kind, kind, "我" + text, session_id="123", session_kind="private")
        for kind, query, text in cases:
            if kind == "dream":
                # P4 专用工具替代旧关键词召回；不可凭这条无等级自述编梦。
                self.assertEqual(await self.store.scalar(
                    "SELECT COUNT(*) FROM event_ledger WHERE event_type='dream'"), 1)
                continue
            rows = await self.mem.self_events("123", "private", query=query)
            self.assertEqual([r["text"] for r in rows], ["我" + text])

    async def test_proactive_audience_and_platform_not_crossed(self):
        await self.mem.record_self_event("proactive", "1", "私聊片段", session_id="123", session_kind="private")
        self.assertEqual(await self.mem.self_events("456", "group"), [])
        self.assertEqual(await self.mem.self_events("789", "private"), [])
        self.assertEqual(await self.mem.self_events("123", "private", platform="telegram"), [])
        self.assertEqual(len(await self.mem.self_events("123", "private")), 1)

    def test_material_labels_are_shared_and_legacy_not_claimed(self):
        self.assertEqual(material_label("__self__"), "[主体是我]")
        self.assertIn("主体未确认", material_label("旧人名"))
        self.assertIn("他人", material_label("123", subject_ref="user:qq:123"))
        self.assertEqual(material_label("owner:__private__", subject_ref="bot:ling"), "[主体是我]")

    async def test_same_type_excluded_even_nonself_scope(self):
        diary = base.Diary(self.p)
        dream = base.Dreamer(self.p)
        daily = QzoneDaily(self.p)
        for kind in ("diary", "dream", "qzone"):
            await self.raw_atom("素材" + kind, event_type=kind)
        await self.mem.record_self_event("diary", "self", "自述摘要", private_text="私密自述")
        atoms = await diary._atoms_between(0, int(time.time()) + 1)
        self.assertNotIn("素材diary", [r["text"] for r in atoms])
        fragments = await dream._pick_fragments(100)
        self.assertFalse(any("素材dream" in s or "私密自述" in s for s in fragments))
        materials = await daily.materials()
        self.assertFalse(any("素材qzone" in s["text"] or "私密自述" in s["text"] for s in materials))

    async def test_raw_dream_never_in_public_qzone_material(self):
        self.p.night = types.SimpleNamespace(dreamer=types.SimpleNamespace(living=lambda: [
            DreamEntry(dream_id="d1", text="绝密梦全文", level=4)]))
        material = await QzoneDaily(self.p).materials()
        self.assertTrue(any(x["kind"] == "dream" for x in material))
        self.assertNotIn("绝密梦全文", json.dumps(material, ensure_ascii=False))

    async def test_persona_static_rule_does_not_depend_on_dynamic_memories(self):
        persona = PersonaService(self.p)
        prefixes = []
        for i in range(2):
            await self.mem.record_self_event("qzone", str(i), "动态内容" + str(i))
            req = types.SimpleNamespace(system_prompt="STATIC", extra_user_content_parts=[])
            await persona.inject(self.event(), req)
            prefixes.append(req.system_prompt)
        self.assertEqual(prefixes[0], prefixes[1])
        self.assertIn(SELF_ACTION_RULE, prefixes[0])
        self.assertNotIn("动态内容", prefixes[0])

    async def test_persona_hook_diary_snippet_audience(self):
        plugin = base.ConglingPlugin.__new__(base.ConglingPlugin)
        plugin.cfg = self.cfg
        plugin.cfg._raw["diary"] = {"feel_in_prompt": True}
        plugin.enabled_for = lambda _: True
        plugin.is_admin = lambda e: e.admin
        plugin.persona = plugin.qqext = None
        plugin.night = types.SimpleNamespace(prompt_hint=lambda **kw: "秘密梦" if kw["include_private"] else "做过梦")
        plugin.diary = types.SimpleNamespace(latest_snippet=AsyncMock(return_value="秘密日记"))
        for group, admin, visible in (("456", True, False), ("", False, False), ("", True, True)):
            req = types.SimpleNamespace(system_prompt="STATIC", extra_user_content_parts=[])
            await plugin.persona_hook(self.event(group=group, admin=admin), req)
            body = "".join(p.text for p in req.extra_user_content_parts)
            self.assertEqual("秘密梦" in body, visible)
            self.assertEqual("秘密日记" in body, visible)
            self.assertEqual(req.system_prompt, "STATIC")

    async def test_real_wake_hint_default_hides_text_but_keeps_use_limit(self):
        self.cfg._raw["night"]["dream_enabled"] = True
        night = NightModule(self.p)
        now = time.time()
        night.state.state.mode = "awake"
        night.state.wake_hints.begin(now)
        night._dream_event_id = night.state.wake_hints.event_id
        dream = DreamEntry(dream_id="d", created_at=int(now), level=5, text="绝密醒后梦", tags=["私密标签"])
        night.morning_dream = dream
        night.dreamer._dreams = [dream]
        group_hints = [night.prompt_hint(session_key="g") for _ in range(20)]
        self.assertFalse(any("绝密醒后梦" in h or "私密标签" in h for h in group_hints))
        self.assertEqual(sum("做过梦" in h for h in group_hints), 2)
        self.assertIn("绝密醒后梦", night.prompt_hint(session_key="owner", include_private=True))


class PatchGuard(Fixture):
    async def test_all_layer_and_content_word_ranking_retained(self):
        self.cfg._raw["memory"] = {"cross_session_scope": "all", "cross_session_recall": True}
        target = await self.raw_atom("望山说烧货很好吃", sid="other", uid="789")
        for i in range(20):
            await self.raw_atom("望山表示自己记得你还认识他" + str(i), sid="other", uid="789")
        hits = await self.mem.recall("你还记得烧货吗", session_ref="session:qq:group:456",
                                     actor_ref="user:qq:123", cross_session=True, mode="all")
        self.assertEqual(hits[0].atom_id, target)

    async def test_search_tool_event_and_no_event_not_global_only(self):
        self.cfg._raw["memory"] = {"cross_session_scope": "all", "cross_session_recall": True}
        aid = await self.raw_atom("烧货来自群内的经历")
        await self.raw_atom("烧货私聊秘密", scope="user", sid="789", uid="789")
        await self.mem.record_self_event("diary", "1", "我写了日记", private_text="烧货秘密日记")
        for event in (None, self.event(group="456")):
            result = await memory_search(self.p, "烧货", _event=event)
            self.assertEqual([h.atom_id for h in result["text"]], [aid])
        capability = next(x for x in build_builtin(self.p) if x.name == "self.memory")
        self.assertTrue(capability.takes_event)
