"""服务器完整未读批次回归；运行时不依赖旁边安装 at_guard 或真实平台。"""
import asyncio
import ast
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wake_persona_flow import FlowTest, main_hook, rollup_mod, flow_mod


class ServerUnreadTest(FlowTest):
    async def test_pending_includes_older_and_awake_unread_excludes_current(self):
        rows = await self.night.crawler.pending("s", exclude_msg_id="new")
        self.assertEqual([r["msg_id"] for r in rows], ["old", "sleep-question"])
        self.assertEqual(await self.night.crawler.unread_count("s", 0), 3)
        await self.store.execute("UPDATE msg_archive SET handled=1 WHERE msg_id='old'")
        self.assertEqual(await self.night.crawler.unread_count("s", self.wake), 2)
        self.assertEqual(await self.night.crawler.pending(""), [])

    async def test_large_batch_keeps_full_text_and_new_arrivals(self):
        await self.store.execute("DELETE FROM msg_archive")
        await self.store.executemany(
            "INSERT INTO msg_archive(msg_id,session_id,ts,text) VALUES(?,?,?,?)",
            [(f"m{i}", "s", self.since - 1000, "长消息" * 200 + f"尾部{i}") for i in range(1001)],
        )
        self.cfg.override("night.rollup.chunk_size", 45)
        self.cfg.override("night.rollup.enabled", False)
        original = self.plugin.models.chat
        calls = []
        async def summarize(prompt, **kw):
            calls.append(prompt)
            if len(calls) == 1:
                await self.archive("incoming", self.wake + 100)
            return await original(prompt, **kw)
        self.plugin.models.chat = summarize
        summaries = await self.night.wake_flow.prepare(
            "s", self.row["umo"],
            lambda text, phase, role: self.plugin.proactive.send_stage(self.row, text, phase, role))
        self.assertEqual(len(summaries), 23)
        self.assertEqual(len(calls), 23)
        self.assertIn("尾部0", calls[0])
        self.assertIn("尾部1000", calls[-1])
        self.assertEqual([r["msg_id"] for r in await self.night.crawler.pending("s")], ["incoming"])
        self.assertEqual(self.sent, ["爬楼语"])

    async def test_read_failure_freezes_batch_across_new_wake_and_reload(self):
        self.plugin.models.chat = AsyncMock(return_value="")
        with self.assertRaises(RuntimeError):
            await self.prepare()
        receipt = await self.store.kv_get("night.wake_flow:s", {})
        upper = receipt["until_id"]
        await self.archive("incoming", self.wake + 100)
        self.night.state.state.last_wake_at += 200
        self.night.wake_flow = flow_mod.WakeFlow(self.night)
        with self.assertRaises(RuntimeError):
            await self.prepare()
        self.assertEqual((await self.store.kv_get("night.wake_flow:s", {}))["until_id"], upper)
        self.assertEqual(await self.store.scalar(
            "SELECT COUNT(*) FROM msg_archive WHERE sender_id<>'bot' AND handled=1"), 0)
        self.assertEqual(len(await self.night.crawler.pending("s")), 4)

    async def test_both_disabled_leave_history_unread(self):
        self.cfg.override("night.rollup.enabled", False)
        self.cfg.override("night.crawl_enabled", False)
        self.assertEqual(await self.night.wake_flow.prepare("s", self.row["umo"], AsyncMock()), [])
        self.assertEqual(len(await self.night.crawler.pending("s")), 3)
        self.assertEqual(self.model_calls, [])

    async def test_awake_or_other_session_silently_reads_without_back_reply(self):
        for mode, pending in (("awake", ["s"]), ("waking", ["other"])):
            await self.store.execute("UPDATE msg_archive SET handled=0 WHERE sender_id<>'bot'")
            self.night.state.state.mode = mode
            self.night.state.state.pending_crawl = pending
            before = len(self.sent)
            await self.prepare()
            self.assertEqual(len(self.sent), before)
            self.assertEqual(await self.night.crawler.pending("s"), [])

    async def test_summary_filters_invented_ids_and_honors_expired(self):
        rows = await self.night.crawler.pending("s", exclude_msg_id="new")
        self.plugin.models.chat = AsyncMock(return_value=json.dumps({
            "summary": "话题已结束",
            "items": [{"msg_id": "old", "expired": True, "priority": 100},
                      {"msg_id": "invented", "priority": 100}],
        }))
        chunk = await self.night.rollup._summarize_chunk(rows)
        self.assertEqual([i.msg_id for i in chunk.items], ["old"])
        self.assertTrue(chunk.items[0].expired)
        self.assertEqual((await self.night.rollup.triage(chunk))["to_reply"], [])

    async def test_archive_and_memory_marks_commit_together_with_evidence(self):
        async def atom(sources, persona=None, session="s"):
            await self.store.execute(
                "INSERT INTO mem_atom(persona_id,session_id,text,source_msg_ids,unread) VALUES(?,?,?,?,1)",
                (persona or self.cfg.persona_id, session, str(sources), json.dumps(sources)))
        await atom(["old", "sleep-question"])
        await atom([])
        await atom(["new"])
        await atom(["old"], persona="other")
        await atom(["old"], session="other")
        first = await self.night.crawler.pending("s", until_id=1)
        self.assertEqual(await self.night.crawler.mark_read("s", strict=True, rows=first), 0)
        second = await self.night.crawler.pending("s", exclude_msg_id="new")
        self.assertEqual(await self.night.crawler.mark_read("s", strict=True, rows=second), 1)
        atoms = await self.store.fetch_all("SELECT unread FROM mem_atom ORDER BY id")
        self.assertEqual([a["unread"] for a in atoms], [0, 1, 1, 1, 1])
        self.assertEqual([r["msg_id"] for r in await self.night.crawler.pending("s")], ["new"])

    async def test_atomic_mark_failure_rolls_back_archive(self):
        await self.store.execute(
            "INSERT INTO mem_atom(persona_id,session_id,text,source_msg_ids,unread) VALUES(?,?,?,?,1)",
            (self.cfg.persona_id, "s", "fact", '["old"]'))
        await self.store.execute(
            "CREATE TRIGGER reject_unread BEFORE UPDATE OF unread ON mem_atom "
            "BEGIN SELECT RAISE(ABORT,'reject'); END")
        with self.assertRaises(Exception):
            await self.night.crawler.mark_read(
                "s", strict=True, rows=await self.night.crawler.pending("s"))
        self.assertEqual(len(await self.night.crawler.pending("s")), 3)
        self.assertEqual(await self.store.scalar("SELECT unread FROM mem_atom"), 1)

    async def test_transaction_cancel_waits_for_commit_and_rejects_nested(self):
        entered, release = threading.Event(), threading.Event()
        def operation(conn):
            conn.execute("UPDATE msg_archive SET handled=1 WHERE msg_id='old'")
            entered.set()
            release.wait(10)
        task = asyncio.create_task(self.store.transaction(operation))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await self.store.scalar("SELECT handled FROM msg_archive WHERE msg_id='old'"), 1)
        await self.store.begin()
        try:
            with self.assertRaisesRegex(RuntimeError, "嵌套"):
                await self.store.transaction(lambda conn: None)
        finally:
            await self.store.rollback()

    async def test_sent_stage_is_not_unread_and_receipt_contains_no_reply_body(self):
        await self.prepare()
        self.assertEqual(await self.night.crawler.pending("s"), [])
        receipt = await self.store.kv_get("night.wake_flow:s", {})
        self.assertTrue(receipt["reply"])
        self.assertNotIn("reply_text", receipt)
        self.assertNotIn("补课答复", json.dumps(receipt, ensure_ascii=False))

    async def test_after_sent_marks_current_only_if_delivery_not_blocked(self):
        self.plugin.enabled_for = lambda _: True
        self.plugin.qqext = self.plugin.memory = None
        event = self.event()
        event.get_result = lambda: type("Result", (), {"chain": ["实际发送正文"]})()
        event.set_extra("_at_guard_delivery_blocked", True)
        await main_hook("after_sent_hook")(self.plugin, event)
        self.assertEqual(await self.store.scalar("SELECT handled FROM msg_archive WHERE msg_id='new'"), 0)
        event.set_extra("_at_guard_delivery_blocked", False)
        await main_hook("after_sent_hook")(self.plugin, event)
        self.assertEqual(await self.store.scalar("SELECT handled FROM msg_archive WHERE msg_id='new'"), 1)

    async def test_manual_crawl_reads_before_acknowledging(self):
        self.cfg.override("night.rollup.enabled", False)
        original = self.plugin.models.chat
        self.plugin.models.chat = AsyncMock(return_value="")
        with self.assertRaises(RuntimeError):
            await self.night.crawl("s")
        self.assertEqual(len(await self.night.crawler.pending("s")), 3)
        self.plugin.models.chat = original
        text = await self.night.crawl("s")
        self.assertIn("有人询问视频", text)
        self.assertEqual(await self.night.crawler.pending("s"), [])

    def test_sending_preparation_runs_after_guard_priority(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8-sig"))
        value = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "PRIO_PERSONA" for t in node.targets))
        self.assertLess(value, 10000)


if __name__ == "__main__":
    # 只运行新增专项，父类既有测试由原入口独立执行。
    suite = unittest.TestSuite(ServerUnreadTest(name) for name in ServerUnreadTest.__dict__
                               if name.startswith("test_"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
