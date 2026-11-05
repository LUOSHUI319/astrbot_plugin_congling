"""活跃补上下文、醒后提示去重及发送故障边界，使用真实流程和隔离数据库。"""
import asyncio
import unittest
from unittest.mock import AsyncMock

import test_unread_crawl_flow as unread_support
import test_wake_persona_flow as base


class AnnouncementTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.FlowTest.asyncSetUp
    asyncTearDown = base.FlowTest.asyncTearDown
    archive = base.FlowTest.archive
    prepare = base.FlowTest.prepare
    event = base.FlowTest.event
    unread = unread_support.UnreadFlow.unread
    guard = unread_support.UnreadFlow.guard
    dispatch = unread_support.UnreadFlow.dispatch

    def awake(self):
        self.night.state.state.mode = "awake"
        self.night.state.state.pending_crawl = []
        self.night.state.state.energy = 100

    def waking(self, sessions=None):
        self.night.state.state.mode = "waking"
        self.night.state.state.pending_crawl = sessions or ["s", "other"]

    async def test_active_first_and_later_batches_are_silent_and_read_full_history(self):
        self.awake()
        epoch = await self.night.wake_flow.epoch()
        await self.prepare()
        await self.archive("active-later", self.wake + 500)
        await self.prepare()
        self.assertEqual(self.sent, [])
        self.night.crawler.crawl_line.assert_not_awaited()
        self.assertEqual(await self.unread(), [])
        self.assertEqual(await self.night.wake_flow.epoch(), epoch)
        prompts = "\n".join(c["prompt"] for c in self.model_calls)
        for mid in ("old", "sleep-question", "new", "active-later"):
            self.assertIn("msg_id=" + mid, prompts)

    async def test_same_wake_multiple_batches_and_reload_announce_only_once(self):
        self.waking()
        await self.prepare()
        self.assertEqual(self.night.state.state.mode, "waking")
        await self.archive("later", self.wake + 500)
        self.night.wake_flow = base.flow_mod.WakeFlow(self.night)
        await self.prepare()
        self.assertEqual(self.sent.count("爬楼语"), 1)
        self.night.crawler.crawl_line.assert_awaited_once()
        self.assertEqual(await self.unread(), [])

    async def test_another_real_wake_can_announce_again(self):
        await self.prepare()
        self.night.state.state.last_wake_at += 100
        self.waking()
        await self.archive("next-wake", self.wake + 500)
        await self.prepare()
        self.assertEqual(self.sent.count("爬楼语"), 2)

    async def test_same_wake_two_sessions_each_announce_once(self):
        self.waking()
        await self.archive("other-message", self.since + 20)
        await self.store.execute("UPDATE msg_archive SET session_id='other' WHERE msg_id='other-message'")
        await self.prepare()
        row = dict(self.row, session_id="other", umo="qq:GroupMessage:other")
        await self.night.wake_flow.prepare(
            "other", row["umo"],
            lambda text, phase, role: self.plugin.proactive.send_stage(row, text, phase, role))
        self.assertEqual(self.sent.count("爬楼语"), 2)

    async def test_other_waking_session_does_not_make_active_session_announce(self):
        self.waking(["other"])
        await self.prepare()
        self.assertEqual(self.sent, [])
        self.assertEqual(await self.unread(), [])

    async def test_announce_off_still_reads_and_preserves_wake_reply(self):
        self.cfg.override("night.crawl_announce", "off")
        await self.prepare()
        self.assertEqual(self.sent, ["补课答复"])
        self.assertEqual(await self.unread(), [])

    async def test_legacy_completed_receipt_deduplicates_current_wake(self):
        self.waking()
        epoch = list(await self.night.wake_flow.epoch())
        await self.store.kv_set("night.wake_flow:s", {"epoch": epoch, "crawl": True, "done": True})
        await self.prepare()
        self.night.crawler.crawl_line.assert_not_awaited()
        self.assertEqual(await self.unread(), [])

    async def test_failed_summary_and_reload_do_not_repeat_successful_announcement(self):
        original = self.plugin.models.chat
        self.plugin.models.chat = AsyncMock(return_value="")
        with self.assertRaises(RuntimeError):
            await self.prepare()
        self.assertEqual(self.sent, ["爬楼语"])
        self.assertEqual(await self.unread(), ["old", "sleep-question", "new"])
        self.plugin.models.chat = original
        self.night.wake_flow = base.flow_mod.WakeFlow(self.night)
        await self.prepare()
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])
        self.assertEqual(await self.unread(), [])

    async def test_unknown_or_sending_receipt_blocks_even_active_reentry(self):
        self.awake()
        for flag in ("unknown", "sending"):
            await self.store.kv_set("night.wake_flow:s", {flag: "crawl"})
            with self.assertRaisesRegex(RuntimeError, "结果未知"):
                await self.prepare()
        self.assertEqual(self.sent, [])
        self.assertEqual(await self.unread(), ["old", "sleep-question", "new"])

    async def test_active_gate_allowed_request_reads_then_keeps_persona_and_current_reply(self):
        self.awake()
        guard, event, request = await self.dispatch("reply")
        self.assertFalse(event.extras.get("stopped"))
        self.assertEqual(self.sent, [])
        self.assertEqual(request.prompt, "当前问题")
        self.assertIn("你是大小姐", request.system_prompt)
        self.assertIn("当前问题仍须正常回答", str(request.extra_user_content_parts))
        self.assertEqual(await self.unread(), ["new"])
        self.assertEqual(guard._stale_reason(event, "s"), "")

    async def test_active_gate_silent_keeps_unread_and_sends_nothing(self):
        self.awake()
        _, event, _ = await self.dispatch("silent")
        self.assertTrue(event.extras.get("stopped"))
        self.assertEqual(self.sent, [])
        self.assertEqual(await self.unread(), ["old", "sleep-question", "new"])

    async def test_active_concurrent_preparation_reads_once_without_sends(self):
        self.awake()
        await asyncio.gather(*(self.prepare() for _ in range(8)))
        self.assertEqual(self.sent, [])
        self.assertEqual(await self.unread(), [])
        self.assertEqual(len(self.model_calls), 3)

    async def test_waking_timeout_during_announcement_generation_suppresses_late_line(self):
        async def crawl_line(**kwargs):
            await self.night.state.finish_waking()
            return "已经过时的爬楼语"
        self.night.crawler.crawl_line = crawl_line
        await self.prepare()
        self.assertEqual(self.sent, [])
        self.assertEqual(await self.unread(), [])

    async def test_waking_timeout_during_reply_generation_suppresses_late_supplement(self):
        original = self.night.rollup.compose_reply
        async def compose(*args, **kwargs):
            text = await original(*args, **kwargs)
            await self.night.state.finish_waking()
            return text
        self.night.rollup.compose_reply = compose
        await self.prepare()
        self.assertEqual(self.sent, ["爬楼语"])
        self.assertEqual(await self.unread(), [])
        receipt = await self.store.kv_get("night.wake_flow:s")
        self.assertFalse(receipt.get("reply"))


if __name__ == "__main__":
    unittest.main()
