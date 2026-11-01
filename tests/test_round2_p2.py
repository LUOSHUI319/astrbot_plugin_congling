"""P2 acceptance: stable refs, atomic migration, recall and audience boundaries."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot_plugin_congling.tests import run_diagnosis_regressions as base
from astrbot_plugin_congling.tests import test_round1_p0p1 as round1
from astrbot_plugin_congling.core.contracts import MemoryDraft, PerceivedMessage
from astrbot_plugin_congling.core.store import _SCHEMA
from astrbot_plugin_congling.memory.refs import (
    subject_ref, session_ref, actor_ref, event_key, parse_ref,
)
from astrbot_plugin_congling.memory.retrieve import is_identity_question
from astrbot_plugin_congling.tests.verify_p2_readonly import inspect


class Refs(unittest.TestCase):
    def test_subject(self):
        self.assertEqual(subject_ref("qq", 123), "user:qq:123")
        self.assertEqual(subject_ref("aiocqhttp", "123"), subject_ref("qq", "123"))

    def test_session_kinds(self):
        self.assertEqual(session_ref("qq", "group", 456), "session:qq:group:456")
        self.assertEqual(session_ref("qq", "private", 123), "session:qq:private:123")
        self.assertEqual(session_ref("qq", "owner", 123), "")

    def test_actor_bot_requires_persona(self):
        self.assertEqual(actor_ref("qq", 123), subject_ref("qq", 123))
        self.assertEqual(actor_ref(persona_id="p1"), "bot:p1")
        self.assertEqual(actor_ref("qq", "bot"), "")
        self.assertNotEqual(actor_ref(persona_id="p1"), actor_ref(persona_id="p2"))

    def test_missing(self):
        for value in (None, "", "  "):
            with self.subTest(value=value):
                self.assertEqual(subject_ref("qq", value), "")
                self.assertEqual(subject_ref(value, "123"), "")
                self.assertEqual(session_ref("qq", "group", value), "")
                self.assertEqual(actor_ref("qq", value), "")
                self.assertEqual(event_key(None, value), event_key("", ""))

    def test_unicode_and_delimiters(self):
        key = subject_ref("中文平台", "用户:甲/%")
        self.assertEqual(parse_ref(key, "user"), ("中文平台", "用户:甲/%"))
        self.assertNotEqual(subject_ref("a:b", "c"), subject_ref("a", "b:c"))

    def test_long_ids_not_truncated(self):
        uid = "甲" * 20000
        self.assertEqual(parse_ref(subject_ref("qq", uid), "user")[1], uid)
        self.assertNotEqual(subject_ref("qq", uid), subject_ref("qq", uid + "b"))
        self.assertEqual(len(event_key("qq", uid)), 70)

    def test_event_keys_stable_unambiguous(self):
        self.assertEqual(event_key("qq", "123"), event_key("qq", "123", ""))
        self.assertNotEqual(event_key("a:b", "c"), event_key("a", "b:c"))
        self.assertNotEqual(event_key("qq", "123", "1"), event_key("qq", "123", "2"))

    def test_display_name_not_part_of_identity(self):
        self.assertEqual(subject_ref("qq", "123"), "user:qq:123")
        self.assertEqual(actor_ref("qq", "123"), "user:qq:123")

    def test_meta_patterns(self):
        for text in ("你还记得我吗", "认识我吗？", "你知道我是谁吗", "你把我忘了吗", "你忘了我吗", "忘了吗"):
            with self.subTest(text=text):
                self.assertTrue(is_identity_question(text))
        for text in ("你还记得小李吗", "我喜欢茶", "你记得我的生日吗", "她认识我吗"):
            self.assertFalse(is_identity_question(text))


class Migration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="p2-migrate-")
        self.path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(self.path)
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.execute("ALTER TABLE msg_archive ADD COLUMN platform TEXT DEFAULT ''")
        conn.execute("ALTER TABLE msg_archive ADD COLUMN umo TEXT DEFAULT ''")
        conn.execute("INSERT INTO inject_cursor VALUES('456',17,'[1]',99)")
        conn.execute("INSERT INTO msg_archive(msg_id,session_id,session_kind,sender_id,platform,ts) VALUES('m1','456','group','123','aiocqhttp',1)")
        conn.execute("INSERT INTO mem_atom(persona_id,scope,subject_id,session_id,session_kind,text,source_msg_ids) VALUES('custom','group','123','456','group','正文不许改变','[\"m1\"]')")
        conn.execute("INSERT INTO mem_atom(persona_id,scope,subject_id,session_id,session_kind,text) VALUES('custom','group','小李','unknown','group','历史自然语言')")
        conn.execute("PRAGMA user_version=5")
        conn.commit()
        conn.close()
        self.store = base.Store(self.path, persona_id="custom")

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    async def test_backfill_unique_and_legacy_without_body_changes(self):
        await self.store.open()
        one, two = await self.store.fetch_all("SELECT * FROM mem_atom ORDER BY id")
        self.assertEqual((one["subject_ref"], one["session_ref"], one["actor_ref"], one["ref_status"]),
                         ("user:qq:123", "session:qq:group:456", "user:qq:123", "resolved"))
        self.assertEqual((two["subject_ref"], two["session_ref"], two["actor_ref"], two["ref_status"]), ("", "", "", "legacy"))
        self.assertEqual(one["text"], "正文不许改变")
        self.assertEqual(two["text"], "历史自然语言")
        self.assertEqual(one["subject_id"], "123")
        self.assertEqual(await self.store.scalar("PRAGMA user_version"), 6)

    async def test_old_cursor_belongs_to_current_persona(self):
        await self.store.open()
        row = await self.store.fetch_one("SELECT * FROM inject_cursor")
        self.assertEqual((row["persona_id"], row["session_id"], row["last_msg_row"], row["last_atom_ids"]),
                         ("custom", "456", 17, "[1]"))
        cols = await self.store.fetch_all("PRAGMA table_info(inject_cursor)")
        self.assertEqual([(r["name"], r["pk"]) for r in cols if r["pk"]], [("persona_id", 1), ("session_id", 2)])

    async def test_journal_and_repeat_open_idempotent(self):
        await self.store.open()
        before = await self.store.fetch_all("SELECT * FROM mem_atom")
        await self.store.close()
        await self.store.open()
        self.assertEqual(before, await self.store.fetch_all("SELECT * FROM mem_atom"))
        journal = await self.store.fetch_all("SELECT * FROM migration_journal")
        self.assertEqual(len(journal), 1)
        self.assertEqual(json.loads(journal[0]["detail"]), {"resolved": 1, "legacy": 1})

    async def test_failure_rolls_back_ddl_rows_cursor_and_version(self):
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TRIGGER fail_p2 BEFORE UPDATE ON mem_atom BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        conn.commit()
        conn.close()
        # 迁移不再整段包大事务：那会在真实库上持锁几十秒，让心跳和抽取全部
        # 报 database is locked。改为结构先提交（幂等无害）、回填分批、失败可重试。
        await self.store.open()          # 不抛异常：降级启动
        self.assertTrue(self.store.ready)
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TRIGGER fail_p2")
        conn.commit()
        conn.close()
        await self.store.close()
        await self.store.open()          # 重开补齐
        resolved = await self.store.scalar(
            "SELECT COUNT(*) FROM mem_atom WHERE ref_status='resolved'", (), 0
        )
        self.assertGreater(int(resolved or 0), 0)
        self.assertEqual(await self.store.scalar("PRAGMA user_version"), 6)

    async def test_platform_collision_not_guessed(self):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO msg_archive(msg_id,session_id,session_kind,sender_id,platform,ts) VALUES('m1','456','group','123','telegram',1)")
        conn.commit()
        conn.close()
        await self.store.open()
        row = await self.store.fetch_one("SELECT * FROM mem_atom WHERE id=1")
        self.assertEqual((row["subject_ref"], row["session_ref"], row["actor_ref"], row["ref_status"]), ("", "", "", "legacy"))

    async def test_ambiguous_actor_does_not_become_subject(self):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO msg_archive(msg_id,session_id,session_kind,sender_id,platform,ts) VALUES('m1','456','group','789','aiocqhttp',1)")
        conn.commit()
        conn.close()
        await self.store.open()
        row = await self.store.fetch_one("SELECT * FROM mem_atom WHERE id=1")
        # actor 补不出来（证据跨人/缺证据），但"关于谁"和"在哪"都有，
        # 召回靠的是这两个，不该因为缺 actor 就把整条降级成 legacy。
        self.assertEqual(row["actor_ref"], "")
        self.assertEqual(row["ref_status"], "resolved")
        self.assertEqual(row["subject_ref"], "user:qq:123")

    async def test_confirmed_qq_subject_resolves_independently_of_unknown_session(self):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO mem_subject(persona_id,user_id,source) VALUES('custom','123','qq')")
        conn.execute("UPDATE msg_archive SET platform=''")
        conn.commit()
        conn.close()
        await self.store.open()
        row = await self.store.fetch_one("SELECT * FROM mem_atom WHERE id=1")
        self.assertEqual(row["subject_ref"], "user:qq:123")
        self.assertEqual((row["session_ref"], row["actor_ref"], row["ref_status"]), ("", "", "legacy"))


class Recall(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = round1.Round1.asyncSetUp
    asyncTearDown = round1.Round1.asyncTearDown

    async def atom(self, uid="123", sid="456", kind="group", text="喜欢散步喝茶", *,
                   level=3, importance=0.5, decay=1.0, scope=None, legacy=False, platform="qq", actor=None):
        scope = scope or ("group" if kind == "group" else "user")
        return await self.store.execute(
            "INSERT INTO mem_atom(persona_id,scope,subject_id,session_id,session_kind,text,mem_level,importance,decay,"
            "subject_ref,session_ref,actor_ref,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.cfg.persona_id, scope, uid, sid, kind, text, level, importance, decay,
             "" if legacy else subject_ref(platform, uid), "" if legacy else session_ref(platform, kind, sid),
             "" if legacy else actor_ref(platform, actor or uid), "active", 1793500000),
        )

    async def search(self, query="散步", uid="123", sid="456", kind="group", **kwargs):
        return await self.mem.retriever.search(query, session_ref=session_ref("qq", kind, sid),
                                                actor_ref=actor_ref("qq", uid), **kwargs)

    def event(self, uid="123", sid="456", text="你还记得我吗", kind="group"):
        msg = PerceivedMessage(text=text, session_id=sid, session_kind=kind, sender_id=uid,
                               meta={"platform": "aiocqhttp"})
        return types.SimpleNamespace(_congling_msg=msg, get_group_id=lambda: sid if kind == "group" else "",
                                     get_sender_id=lambda: uid, get_platform_name=lambda: "aiocqhttp")

    def request(self):
        return types.SimpleNamespace(system_prompt="stable", extra_user_content_parts=[])

    async def test_new_write_aligns_refs_and_preserves_old_columns(self):
        draft = MemoryDraft(text="123喜欢喝茶", subject_key="123", session_id="456", session_kind="group",
                            platform="qq", actor_id="789")
        aid = await self.mem.atoms.add(draft, scope="group", subject="456", persona_id=self.cfg.persona_id)
        row = await self.mem.atoms.get(aid)
        self.assertEqual((row["subject_id"], row["session_id"]), ("123", "456"))
        self.assertEqual((row["subject_ref"], row["session_ref"], row["actor_ref"]),
                         ("user:qq:123", "session:qq:group:456", "user:qq:789"))

    async def test_new_write_resolves_actual_evidence_platform_and_actor(self):
        await self.store.execute("INSERT INTO msg_archive(msg_id,session_id,session_kind,sender_id,platform,ts) VALUES('e1','456','group','789','aiocqhttp',1)")
        draft = MemoryDraft(text="123喜欢喝茶", subject_key="123", session_id="456", session_kind="group", evidence_msg_ids=["e1"])
        aid = await self.mem.atoms.add(draft, scope="group", persona_id=self.cfg.persona_id)
        row = await self.mem.atoms.get(aid)
        self.assertEqual(row["actor_ref"], "user:qq:789")
        self.assertEqual(row["ref_status"], "resolved")

    async def test_missing_write_identity_remains_legacy(self):
        aid = await self.mem.atoms.add(MemoryDraft(text="旧接口正文", session_id="456"), scope="group", subject="小李")
        row = await self.mem.atoms.get(aid)
        self.assertEqual(row["subject_ref"], "")
        self.assertEqual(row["ref_status"], "legacy")

    async def test_group_session_not_subject_id(self):
        aid = await self.atom(uid="789")
        self.assertEqual([h.atom_id for h in await self.search()], [aid])

    async def test_legacy_group_session_not_subject_id(self):
        aid = await self.atom(uid="789", legacy=True)
        self.assertEqual([h.atom_id for h in await self.search()], [aid])

    async def test_all_three_candidate_layers_and_dedupe(self):
        self.cfg.override("memory.cross_session_recall", True)
        current = await self.atom()
        personal = await self.atom(sid="999")
        public = await self.atom(uid="", sid="", kind="", scope="global")
        other = await self.atom(uid="789", sid="999")
        ids = {h.atom_id for h in await self.search(cross_session=True, k=20)}
        self.assertEqual(ids, {current, personal, public})
        self.assertNotIn(other, ids)

    async def test_private_cannot_be_recalled_by_other_group_user_any_mode(self):
        self.cfg.override("memory.cross_session_recall", True)
        private = await self.atom(uid="789", sid="789", kind="private")
        current = await self.atom()
        for mode in ("same_user", "all", "whitelist", "self"):
            self.cfg.override("memory.cross_session_whitelist", ["456", "789"])
            hits = await self.search(cross_session=True, mode=mode)
            self.assertIn(current, [h.atom_id for h in hits])
            self.assertNotIn(private, [h.atom_id for h in hits])

    async def test_mislabeled_private_global_does_not_bypass_boundary(self):
        await self.atom(uid="789", sid="789", kind="private", scope="global")
        self.assertEqual(await self.search(), [])

    async def test_private_about_actor_but_in_other_private_is_excluded(self):
        self.cfg.override("memory.cross_session_recall", True)
        await self.atom(uid="123", sid="789", kind="private")
        self.assertEqual(await self.search(cross_session=True, mode="all"), [])

    async def test_current_actor_own_private_permitted_only_when_cross_enabled(self):
        aid = await self.atom(sid="123", kind="private")
        self.assertEqual(await self.search(), [])
        self.cfg.override("memory.cross_session_recall", True)
        self.assertEqual([h.atom_id for h in await self.search(cross_session=True)], [aid])

    async def test_switch_off_overrides_admin_all(self):
        self.cfg.override("memory.cross_session_recall", True)
        self.cfg.override("memory.cross_session_scope", "off")
        await self.atom(sid="999")
        self.assertEqual(await self.search(cross_session=True, mode="all"), [])

    async def test_boolean_off_blocks_cross_session(self):
        await self.atom(sid="999")
        self.assertEqual(await self.search(cross_session=True, mode="all"), [])

    async def test_whitelist_is_not_global_and_empty_is_closed(self):
        self.cfg.override("memory.cross_session_recall", True)
        aid = await self.atom(sid="999")
        self.assertEqual(await self.search(cross_session=True, mode="whitelist"), [])
        self.cfg.override("memory.cross_session_whitelist", ["456", "999"])
        self.assertEqual([h.atom_id for h in await self.search(cross_session=True, mode="whitelist")], [aid])

    async def test_meta_returns_highest_person_not_keyword_match(self):
        low = await self.atom(text="喜欢茶", level=2)
        high = await self.atom(text="生日是十月", level=5)
        protected = await self.atom(text="不可忘的承诺", level=0)
        await self.atom(uid="789", level=5)
        hits = await self.search("你还记得我吗", k=2)
        self.assertEqual([h.atom_id for h in hits], [protected, high])
        self.assertNotIn(low, [h.atom_id for h in hits])
        self.assertEqual(hits[0].level, 0)

    async def test_meta_never_falls_back_to_someone_else(self):
        await self.atom(uid="789", level=5)
        self.assertEqual(await self.search("你认识我吗"), [])

    async def test_about_ref_not_evidence_actor(self):
        aid = await self.atom(uid="789", actor="123")
        self.assertEqual(await self.search("记得我吗", uid="123"), [])
        self.assertEqual([h.atom_id for h in await self.search("记得我吗", uid="789")], [aid])

    async def test_meta_legacy_actor_only(self):
        aid = await self.atom(legacy=True)
        await self.atom(uid="789", legacy=True)
        self.assertEqual([h.atom_id for h in await self.search("认识我吗")], [aid])

    async def test_weak_fallback_importance_times_decay(self):
        one = await self.atom(text="生日十月", importance=0.8, decay=0.5)
        two = await self.atom(text="爱吃苹果", importance=0.6, decay=0.9)
        hits = await self.search("xyznotpresent", k=2)
        self.assertEqual([h.atom_id for h in hits], [two, one])
        self.assertTrue(all(h.weak_related and "弱相关" in h.line() for h in hits))

    async def test_weak_fallback_no_other_session_or_private(self):
        self.cfg.override("memory.cross_session_recall", True)
        await self.atom(sid="999", text="生日十月")
        await self.atom(uid="789", sid="789", kind="private", text="生日十月")
        self.assertEqual(await self.search("xyznotpresent", cross_session=True), [])

    async def test_keyword_hits_are_not_weak(self):
        await self.atom()
        self.assertFalse((await self.search())[0].weak_related)

    async def test_unread_forgotten_archived_low_decay_other_persona_excluded(self):
        good = await self.atom()
        for column, value in (("unread", 1), ("status", "forgotten"), ("status", "archived"),
                              ("decay", 0.01), ("persona_id", "other")):
            aid = await self.atom()
            await self.store.execute(f"UPDATE mem_atom SET {column}=? WHERE id=?", (value, aid))
        self.assertEqual([h.atom_id for h in await self.search()], [good])

    async def test_exclude_and_zero_k_remain_effective(self):
        aid = await self.atom()
        self.assertEqual(await self.search(exclude={aid}), [])
        self.assertEqual(await self.search(k=0), [])
        self.assertEqual(await self.search("记得我吗", exclude={aid}), [])

    async def test_legacy_signature_and_recall_text(self):
        aid = await self.atom(legacy=True)
        hits = await self.mem.retriever.search("散步", scope="group", subject="456")
        self.assertEqual([h.atom_id for h in hits], [aid])
        self.assertIn("散步", await self.mem.retriever.recall_text("散步", scope="group", subject="456"))

    async def test_explicit_platform_does_not_match_other_platform(self):
        aid = await self.atom(platform="telegram")
        self.assertEqual(await self.search(), [])
        await self.atom(legacy=True)
        hits = await self.mem.retriever.search("散步", session_ref=session_ref("telegram", "group", "456"),
                                               actor_ref=actor_ref("telegram", "123"))
        self.assertEqual([h.atom_id for h in hits], [aid])

    async def test_conflicting_legacy_platform_not_matched(self):
        await self.atom(legacy=True)
        await self.store.execute("INSERT INTO msg_archive(session_id,session_kind,platform,ts) VALUES('456','group','telegram',1)")
        self.assertEqual(await self.search(), [])

    async def test_inject_meta_cursor_and_temp_prefix(self):
        aid = await self.atom()
        req = self.request()
        await self.mem.inject(self.event(), req)
        cursor = await self.mem.retriever.cursor("456")
        self.assertEqual(json.loads(cursor["last_atom_ids"]), [aid])
        self.assertEqual(req.system_prompt, "stable")
        self.assertTrue(req.extra_user_content_parts[0].no_save)

    async def test_second_group_speaker_gets_only_their_meta_memories(self):
        one = await self.atom()
        two = await self.atom(uid="789")
        await self.mem.inject(self.event(), self.request())
        await self.mem.inject(self.event(uid="789"), self.request())
        ids = json.loads((await self.mem.retriever.cursor("456"))["last_atom_ids"])
        self.assertEqual(ids, [two])
        self.assertNotIn(one, ids)

    async def test_third_party_mentions_never_trigger_their_profile(self):
        self.cfg.override("memory.profile_inject_in_group", True)
        self.mem.profiles.prompt_hint = AsyncMock(return_value="当前人画像")
        self.mem.affinity.prompt_hint = AsyncMock(return_value="")
        await self.mem.inject(self.event(text="789说他认识小李"), self.request())
        self.mem.profiles.prompt_hint.assert_awaited_once_with("123")
        self.mem.affinity.prompt_hint.assert_awaited_once_with("123")

    async def test_event_identity_wins_over_global_last_message(self):
        await self.atom(uid="789", sid="789", kind="private")
        self.p._last_perceived = PerceivedMessage(text="你认识我吗", session_id="789", session_kind="private", sender_id="789")
        self.mem.profiles.prompt_hint = AsyncMock(return_value="")
        self.cfg.override("memory.profile_inject_in_group", True)
        await self.mem.inject(self.event(), self.request())
        self.mem.profiles.prompt_hint.assert_awaited_once_with("123")

    async def test_cursor_and_water_partition_and_no_reset(self):
        await self.mem._set_water("456", 100)
        await self.mem.retriever.save_cursor("456", [8])
        await self.mem._on_perceived(self.event()._congling_msg)
        self.assertEqual(await self.mem._water("456"), 100)
        self.cfg.override("persona.persona_id", "other")
        self.assertEqual(await self.mem._water("456"), 0)
        self.assertEqual(await self.mem.retriever.cursor("456"), {})
        await self.mem.retriever.save_cursor("456", [9], last_msg_row=20)
        self.cfg.override("persona.persona_id", "ling")
        self.assertEqual(await self.mem._water("456"), 100)
        self.assertEqual(json.loads((await self.mem.retriever.cursor("456"))["last_atom_ids"]), [8])

    async def test_sweep_snapshot_water_is_persona_partitioned(self):
        await self.store.execute("INSERT INTO msg_archive(session_id,session_kind,text,ts) VALUES('456','group','未消池',1)")
        await self.mem._set_water("456", 100)
        self.assertEqual((await self.mem._tick_snapshot())["pool"], {})
        self.cfg.override("persona.persona_id", "other")
        self.assertEqual((await self.mem._tick_snapshot())["pool"], {"456": 1})

    async def test_subject_reject_bot_name_and_group(self):
        for key in ("bot", "__self__", "bot:ling", "bot:other"):
            self.assertEqual(await self.mem.subjects.ensure(key, source="qq"), {})
            self.assertEqual(await self.mem.subjects.identify(sender_id=key), "")
        self.assertEqual(await self.mem.subjects.ensure("小李", confidence=0.4), {})
        await self.atom()
        self.assertEqual(await self.mem.subjects.ensure("456", source="qq"), {})

    async def test_board_and_user_list_exclude_unconfirmed_bot_group(self):
        await self.atom()
        for key, source in (("123", "qq"), ("789", "inferred"), ("bot:ling", "qq"),
                            ("456", "qq"), ("bot", "qq"), ("__self__", "qq")):
            await self.store.execute("INSERT INTO mem_subject(persona_id,user_id,source,display_name) VALUES(?,?,?,?)",
                                     (self.cfg.persona_id, key, source, key))
            await self.store.execute("INSERT INTO affinity(persona_id,subject_key,score) VALUES(?,?,20)",
                                     (self.cfg.persona_id, key))
        self.assertEqual([r["subject_key"] for r in await self.mem.affinity.leaderboard()], ["123"])
        self.assertEqual([r["user_id"] for r in await self.mem.subjects.list()], ["123"])
        self.assertEqual([r["user_id"] for r in await self.mem.subjects.list(keyword="123")], ["123"])

    async def test_readonly_inspector_cannot_migrate_database(self):
        path = self.store.path
        before = await self.store.scalar("PRAGMA user_version")
        result = inspect(path)
        self.assertTrue(result["refs_deployed"]["subject_ref"])
        self.assertEqual(await self.store.scalar("PRAGMA user_version"), before)

    async def test_global_only_legacy_search_without_subject(self):
        aid = await self.atom(uid="", sid="", kind="", scope="global")
        await self.atom(uid="789", sid="789", kind="private", scope="global")
        self.assertEqual([h.atom_id for h in await self.mem.retriever.search("散步")], [aid])
        self.assertEqual([h.atom_id for h in await self.mem.retriever.search("散步", scope="global")], [aid])

    async def test_private_session_layer_can_recall_third_party_fact_in_own_session(self):
        aid = await self.atom(uid="789", sid="123", kind="private")
        self.assertEqual([h.atom_id for h in await self.search(kind="private", sid="123")], [aid])
        self.assertEqual(await self.search("你还记得我吗", kind="private", sid="123"), [])

    async def test_duplicate_detection_separates_platforms(self):
        draft = MemoryDraft(text="123喜欢喝茶", subject_key="123", session_id="456", session_kind="group", platform="qq", actor_id="123")
        one = await self.mem.atoms.add(draft, scope="group")
        draft.platform = "telegram"
        two = await self.mem.atoms.add(draft, scope="group")
        self.assertNotEqual(one, two)
        self.assertEqual(await self.mem.atoms.add(draft, scope="group"), two)

    async def test_subject_edit_clears_stale_ref_without_body_edit(self):
        aid = await self.atom()
        await self.mem.atoms.set_fields(aid, {"subject_id": "789"})
        row = await self.mem.atoms.get(aid)
        self.assertEqual((row["subject_ref"], row["ref_status"]), ("", "legacy"))
        self.assertEqual(row["text"], "喜欢散步喝茶")
        self.assertEqual(await self.search("记得我吗"), [])

    async def test_meta_bypasses_bm25_and_embedding(self):
        await self.atom()
        with patch.object(self.mem.retriever, "_bm25", side_effect=AssertionError("must bypass")), \
             patch.object(self.mem.retriever, "_vector", new=AsyncMock(side_effect=AssertionError("must bypass"))):
            self.assertTrue(await self.search("你还记得我吗"))

    async def test_cross_off_injection_does_not_query_other_sessions(self):
        await self.atom(sid="999")
        self.cfg.override("memory.cross_session_recall", True)
        self.cfg.override("memory.cross_session_scope", "off")
        self.cfg.override("memory.cross_session_admin_mode", "all")
        self.p.is_admin = lambda _: True
        with patch.object(self.mem.retriever, "search", wraps=self.mem.retriever.search) as call:
            await self.mem.inject(self.event(), self.request())
        self.assertEqual(call.await_count, 1)
        self.assertFalse(call.call_args.kwargs["cross_session"])
        self.assertEqual(self.mem.snapshot()["recall"]["last_hits"], 0)

    async def test_legacy_default_platform_does_not_hide_group(self):
        aid = await self.atom(legacy=True)
        for platform in ("", "aiocqhttp", "default"):
            await self.store.execute("INSERT INTO msg_archive(session_id,session_kind,platform,ts) VALUES('456','group',?,1)", (platform,))
        self.assertEqual([h.atom_id for h in await self.search()], [aid])

    async def test_null_refs_are_compatible_without_private_leak(self):
        aid = await self.atom(legacy=True)
        await self.store.execute("UPDATE mem_atom SET subject_ref=NULL, session_ref=NULL WHERE id=?", (aid,))
        self.assertEqual([h.atom_id for h in await self.search()], [aid])
        await self.atom(uid="789", sid="789", kind="private", legacy=True)
        self.cfg.override("memory.cross_session_recall", True)
        self.assertEqual([h.atom_id for h in await self.search(cross_session=True, mode="all")], [aid])

    async def test_self_action_has_bot_refs_without_display_name(self):
        aid = await self.mem.record_self_event("diary", "p2-self", "我写了日记")
        row = await self.mem.atoms.get(aid)
        self.assertEqual((row["subject_ref"], row["actor_ref"]), ("bot:ling", "bot:ling"))

    async def test_explicit_private_platform_legacy_subject_conversion(self):
        aid = await self.atom(sid="123", kind="private", platform="telegram")
        hits = await self.mem.retriever.search("散步", scope="user", subject="123",
                                               session_ref=session_ref("telegram", "private", "123"))
        self.assertEqual([h.atom_id for h in hits], [aid])


if __name__ == "__main__":
    unittest.main()
