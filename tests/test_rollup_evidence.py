"""来源造假、摘要改写、更正跨块及长文本尾部的有效回归。"""
import importlib
import json
import unittest
from unittest.mock import AsyncMock

import test_wake_persona_flow as base

evidence = importlib.import_module("cg.night.rollup_evidence")


class EvidenceTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.FlowTest.asyncSetUp
    asyncTearDown = base.FlowTest.asyncTearDown
    prepare = base.FlowTest.prepare
    archive = base.FlowTest.archive

    async def rows(self):
        return await self.night.crawler.pending("s")

    async def read_chunk(self):
        return await self.night.rollup._summarize_chunk(await self.rows())

    async def test_model_paraphrase_and_fake_refs_are_replaced_by_source(self):
        await self.store.execute(
            "UPDATE msg_archive SET text='不是1200元，是1280元；明天18:30再确认。' WHERE msg_id='sleep-question'")
        self.night.rollup._llm_summarize = AsyncMock(return_value=(
            [base.rollup_mod.RollupItem(msg_id="sleep-question", who="假作者", what="1200元",
                                        refs=["伪造ID", "old"])], "1200元"))
        chunk = await self.read_chunk()
        self.assertIn("1280元", chunk.items[0].what)
        self.assertEqual(chunk.items[0].who, "AKI")
        self.assertEqual(chunk.items[0].refs, ["old"])
        self.assertTrue(evidence.facts_match(chunk.facts, await self.rows()))
        self.assertIn("[msg_id=sleep-question]", chunk.context_text())

    async def test_compose_reads_full_source_instead_of_model_what(self):
        text = "长消息正文" * 700 + "。尾部更正：金额1280元，不是1200元。"
        await self.store.execute("UPDATE msg_archive SET text=? WHERE msg_id='sleep-question'", (text,))
        self.plugin.models.character_chat = AsyncMock(return_value="按1280元确认")
        item = base.rollup_mod.RollupItem(msg_id="sleep-question", session_id="s", what="错误金额1200")
        await self.night.rollup.compose_reply({"to_reply": [{"item": item}]}, umo=self.row["umo"], strict=True)
        prompt = self.plugin.models.character_chat.await_args.args[0]
        self.assertIn(text, prompt)
        self.assertNotIn("错误金额1200", prompt)
        self.assertTrue(any("尾部更正" in fact["quote"] for fact in evidence.extract_facts(await self.rows())))

    async def test_source_deleted_or_changed_after_summarizing_blocks_model(self):
        for action in ("UPDATE msg_archive SET text='已改动' WHERE msg_id='sleep-question'",
                       "DELETE FROM msg_archive WHERE msg_id='sleep-question'"):
            chunk = await self.read_chunk()
            self.plugin.models.character_chat = AsyncMock(return_value="不应生成")
            await self.store.execute(action)
            with self.assertRaisesRegex(RuntimeError, "原消息缺失"):
                await self.night.rollup.compose_reply(
                    {"to_reply": [{"item": chunk.items[0]}]}, umo=self.row["umo"], strict=True)
            self.plugin.models.character_chat.assert_not_awaited()

    async def test_fabricated_quote_or_foreign_evidence_blocks_composition(self):
        chunk = await self.read_chunk()
        self.plugin.models.character_chat = AsyncMock(return_value="不应生成")
        for bad in ({**chunk.facts[0], "quote": "不存在的更正"}, {**chunk.facts[0], "session_id": "other"}):
            with self.assertRaisesRegex(RuntimeError, "关键原句无法核实"):
                await self.night.rollup.compose_reply({
                    "to_reply": [{"item": chunk.items[0]}], "facts": [bad]},
                    umo=self.row["umo"], strict=True)
        self.plugin.models.character_chat.assert_not_awaited()

    async def test_cross_chunk_correction_is_verified_in_reply_prompt(self):
        await self.store.execute(
            "UPDATE msg_archive SET text='更正：视频改到11月8日20:15，不是7日。' WHERE msg_id='new'")
        self.plugin.models.character_chat = AsyncMock(return_value="补课答复")
        await self.prepare()
        prompt = self.plugin.models.character_chat.await_args.args[0]
        self.assertIn("[msg_id=new]", prompt)
        self.assertIn("11月8日20:15", prompt)
        self.assertIn("后续明确更正", prompt)

    async def test_non_strict_missing_source_cannot_fall_back_to_unverified_text(self):
        self.plugin.models.character_chat = AsyncMock(return_value="")
        item = base.rollup_mod.RollupItem(msg_id="missing", session_id="s", what="错误摘要")
        self.assertEqual(await self.night.rollup.compose_reply({"to_reply": [{"item": item}]}), "")
        self.plugin.models.character_chat.assert_not_awaited()

    async def test_failed_verification_preserves_unread_and_cursor(self):
        original = self.night.rollup.triage
        async def deleting(chunk):
            plan = await original(chunk)
            await self.store.execute("DELETE FROM msg_archive WHERE msg_id='sleep-question'")
            return plan
        self.night.rollup.triage = deleting
        with self.assertRaisesRegex(RuntimeError, "无法核实|原消息缺失"):
            await self.prepare()
        self.assertEqual(await self.night.rollup._cursor("s"), 0)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM msg_archive WHERE handled=0"), 2)
        self.assertEqual(self.sent, ["爬楼语"])

    async def test_cached_facts_cannot_be_changed_even_with_recomputed_checksum(self):
        await self.night.rollup.rollup_session("s", rows=await self.rows(), include_items=False, umo=self.row["umo"])
        row = await self.store.fetch_one("SELECT k,v FROM kv WHERE k LIKE 'night.rollup.cache:%' LIMIT 1")
        entry = json.loads(row["v"])
        entry["payload"]["facts"][0]["quote"] = "伪造原句"
        entry["checksum"] = evidence.digest(entry["payload"])
        await self.store.kv_set(row["k"], entry)
        self.model_calls.clear()
        chunks = await self.night.rollup.rollup_session(
            "s", rows=await self.rows(), include_items=False, umo=self.row["umo"])
        self.assertEqual(len(self.model_calls), 1)
        rows = await self.rows()
        self.assertTrue(all(evidence.facts_match(c.facts, rows) for c in chunks))

    def test_repeated_daily_chatter_cannot_displace_negative_authorization(self):
        rows = [{"id": i, "msg_id": str(i), "session_id": "s", "sender_id": "u",
                 "text": ("我没有同意公开照片，只允许内部预览；也没有授权转发到朋友圈。"
                          if i == 1 else "刚才那只猫很可爱。今天路上有点堵。")}
                for i in range(1, 46)]
        facts = evidence.extract_facts(rows)
        self.assertEqual(len(facts), 2)
        self.assertTrue(all(f["msg_id"] == "1" for f in facts))
        self.assertTrue(evidence.facts_match(facts, rows))

    async def test_ref_without_keyword_uses_actual_chunk_end(self):
        self.night.rollup._llm_summarize = AsyncMock(return_value=(
            [base.rollup_mod.RollupItem(msg_id="sleep-question", refs=["new"])], "问题摘要"))
        await self.store.execute("UPDATE msg_archive SET text='上下文内容' WHERE msg_id='new'")
        chunk = await self.read_chunk()
        self.plugin.models.character_chat = AsyncMock(return_value="补课答复")
        await self.night.rollup.compose_reply(
            {"to_reply": [{"item": chunk.items[0]}]}, umo=self.row["umo"], strict=True)
        self.assertIn("关联原文 [msg_id=new]：上下文内容",
                      self.plugin.models.character_chat.await_args.args[0])
