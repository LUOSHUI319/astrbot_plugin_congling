"""判定断线恢复补读：窗口、人格、安全文案和发送结果。"""
import asyncio
import importlib
import json
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

try:
    from . import test_wake_persona_flow as base
except ImportError:
    import test_wake_persona_flow as base


components = sys.modules["astrbot.api.message_components"]
components.At = lambda qq: SimpleNamespace(qq=qq, text=f"@{qq}")
ClassifierRecovery = importlib.import_module(
    "cg.humanize.recovery"
).ClassifierRecovery


class ClassifierRecoveryTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.FlowTest.asyncSetUp
    asyncTearDown = base.FlowTest.asyncTearDown
    archive = base.FlowTest.archive

    def work(self):
        return SimpleNamespace(
            event_id="a" * 32,
            window_start=self.since,
            window_end=time.time(),
            sessions=(
                SimpleNamespace(
                    session_id="qq:GroupMessage:s",
                    first_affected_at=self.since,
                    last_affected_at=self.wake,
                    backfill_status="pending",
                ),
            ),
        )

    async def test_window_freezes_row_upper_bound_and_preserves_night_state(self):
        now = int(time.time())
        await self.store.execute(
            "INSERT INTO msg_archive(msg_id,session_id,session_kind,sender_id,"
            "sender_name,ts,text,umo) VALUES(?,?,?,?,?,?,?,?)",
            (
                "wrong-audience",
                "s",
                "group",
                "u2",
                "旁人",
                now - 2,
                "PRIVATE LEAK",
                "other:GroupMessage:s",
            ),
        )
        original_scalar = self.store.scalar
        injected = False

        async def scalar(sql, params=(), default=None):
            nonlocal injected
            value = await original_scalar(sql, params, default)
            if "SELECT MAX(id) FROM msg_archive" in sql and not injected:
                injected = True
                await self.store.execute(
                    "INSERT INTO msg_archive(msg_id,session_id,session_kind,"
                    "sender_id,sender_name,ts,text,umo) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "late-backfill",
                        "s",
                        "group",
                        "u3",
                        "迟到写入",
                        now - 1,
                        "LATE BACKDATED",
                        "qq:GroupMessage:s",
                    ),
                )
            return value

        self.store.scalar = scalar
        state = self.night.state.state
        before = (
            state.mode,
            state.energy,
            state.last_wake_at,
            state.last_wake_reason,
            list(state.pending_crawl),
        )

        result = await ClassifierRecovery(self.plugin).recover(self.work())

        self.assertEqual(result["contract"], "congling.classifier-recovery.v1")
        self.assertEqual(result["sessions"][0]["status"], "completed")
        self.assertEqual(len(result["sessions"][0]["dedupe_key"]), 64)
        self.assertEqual(len(self.sent), 1)
        prompts = "\n".join(str(call.get("prompt") or "") for call in self.model_calls)
        self.assertNotIn("PRIVATE LEAK", prompts)
        self.assertNotIn("LATE BACKDATED", prompts)
        self.assertIn("你是大小姐", next(
            str(call.get("system_prompt") or "")
            for call in self.model_calls
            if str(call.get("prompt") or "").startswith("请只回答")
        ))
        self.assertEqual(
            (
                state.mode,
                state.energy,
                state.last_wake_at,
                state.last_wake_reason,
                list(state.pending_crawl),
            ),
            before,
        )

    async def test_success_is_persisted_and_reload_does_not_send_twice(self):
        first = await ClassifierRecovery(self.plugin).recover(self.work())
        self.assertEqual(first["sessions"][0]["status"], "completed")
        self.assertEqual(len(self.sent), 1)

        self.plugin.proactive = base.Proactive(self.plugin)
        second = await ClassifierRecovery(self.plugin).recover(self.work())

        self.assertEqual(second["sessions"][0]["status"], "completed")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(
            await self.store.scalar(
                "SELECT COUNT(*) FROM classifier_recovery_delivery "
                "WHERE status='sent'"
            ),
            1,
        )

    async def test_no_reply_completes_silently(self):
        self.night.rollup._llm_summarize = AsyncMock(
            return_value=([], "只是闲聊，已经结束")
        )

        result = await ClassifierRecovery(self.plugin).recover(self.work())

        self.assertEqual(result["sessions"][0]["status"], "no_reply")
        self.assertEqual(self.sent, [])

    async def test_two_invalid_explanations_use_fixed_safe_apology(self):
        prefix_outputs = iter(("抱歉，API故障了", "抱歉，我刚去医院了"))
        calls = []

        async def text_chat(**kwargs):
            calls.append(kwargs)
            prompt = str(kwargs.get("prompt") or "")
            if "只输出一个 JSON" in prompt:
                return SimpleNamespace(completion_text=json.dumps({
                    "summary": "有人问视频安排",
                    "items": [{
                        "msg_id": "sleep-question",
                        "targets_me": True,
                        "answered": False,
                        "expired": False,
                        "priority": 100,
                    }],
                }))
            if prompt.startswith("请只回答"):
                return SimpleNamespace(completion_text="实际回答放在这里")
            if prompt.startswith("按你此刻的人格"):
                return SimpleNamespace(completion_text=next(prefix_outputs))
            return SimpleNamespace(completion_text="")

        provider = SimpleNamespace(
            text_chat=text_chat,
            provider_config={"id": "recovery-test", "model": "offline"},
        )
        self.context.get_using_provider = lambda: provider
        self.context.get_using_provider_async = AsyncMock(return_value=provider)
        self.plugin.models = base.ModelHub(self.plugin)
        self.night.rollup.cache = importlib.import_module(
            "cg.night.rollup_cache"
        ).RollupCache(self.store)

        result = await ClassifierRecovery(self.plugin).recover(self.work())

        self.assertEqual(result["sessions"][0]["status"], "completed")
        self.assertEqual(
            self.sent,
            ["抱歉，刚才没及时跟上。\n实际回答放在这里"],
        )
        visible = self.sent[0].lower()
        for forbidden in (
            "服务器", "api", "模型", "网络", "系统", "插件", "数据库",
            "故障", "异常", "超时", "后台", "运维", "医院",
        ):
            self.assertNotIn(forbidden, visible)
        self.assertEqual(
            sum(str(call.get("prompt") or "").startswith("按你此刻的人格")
                for call in calls),
            2,
        )

    async def test_unknown_send_result_is_not_retried(self):
        self.context.send_message = AsyncMock(side_effect=TimeoutError())

        result = await ClassifierRecovery(self.plugin).recover(self.work())

        self.assertEqual(result["sessions"][0]["status"], "send_unknown")
        self.assertEqual(self.context.send_message.await_count, 1)
        self.assertEqual(
            await self.store.scalar(
                "SELECT COUNT(*) FROM msg_archive "
                "WHERE msg_id LIKE 'classifier-recovery-%'"
            ),
            0,
        )
        first_sender = self.context.send_message
        self.plugin.proactive = base.Proactive(self.plugin)
        self.context.send_message = AsyncMock(return_value=True)

        reloaded = await ClassifierRecovery(self.plugin).recover(self.work())

        self.assertEqual(reloaded["sessions"][0]["status"], "send_unknown")
        self.assertEqual(first_sender.await_count, 1)
        self.context.send_message.assert_not_awaited()
        self.assertEqual(
            await self.store.scalar(
                "SELECT COUNT(*) FROM classifier_recovery_delivery "
                "WHERE status='unknown'"
            ),
            1,
        )

    async def test_explicit_send_failure_is_retryable_without_partial_archive(self):
        self.context.send_message = AsyncMock(return_value=False)

        result = await ClassifierRecovery(self.plugin).recover(self.work())

        self.assertEqual(result["sessions"][0]["status"], "retry_pending")
        self.assertEqual(self.context.send_message.await_count, 1)
        self.assertEqual(
            await self.store.scalar(
                "SELECT COUNT(*) FROM classifier_recovery_delivery "
                "WHERE status='failed'"
            ),
            1,
        )
        self.assertEqual(
            await self.store.scalar(
                "SELECT COUNT(*) FROM msg_archive "
                "WHERE sender_id='bot' AND text LIKE '抱歉%'"
            ),
            0,
        )

    async def test_cancelled_send_is_persisted_unknown_across_reload(self):
        self.context.send_message = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
        with self.assertRaises(asyncio.CancelledError):
            await ClassifierRecovery(self.plugin).recover(self.work())

        self.plugin.proactive = base.Proactive(self.plugin)
        self.context.send_message = AsyncMock(return_value=True)
        result = await ClassifierRecovery(self.plugin).recover(self.work())

        self.assertEqual(result["sessions"][0]["status"], "send_unknown")
        self.context.send_message.assert_not_awaited()
        self.assertEqual(
            await self.store.scalar(
                "SELECT COUNT(*) FROM classifier_recovery_delivery "
                "WHERE status='unknown'"
            ),
            1,
        )

    async def test_concurrent_duplicate_recovery_sends_once(self):
        calls = 0

        async def delayed_send(*_args):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return True

        self.context.send_message = delayed_send
        results = await asyncio.gather(
            ClassifierRecovery(self.plugin).recover(self.work()),
            ClassifierRecovery(self.plugin).recover(self.work()),
        )

        statuses = [result["sessions"][0]["status"] for result in results]
        self.assertEqual(calls, 1)
        self.assertIn("completed", statuses)
        self.assertTrue(
            all(status in {"completed", "send_unknown"} for status in statuses)
        )
        self.assertEqual(
            await self.store.scalar(
                "SELECT COUNT(*) FROM classifier_recovery_delivery"
            ),
            1,
        )

    async def test_answer_generation_failure_never_sends_apology_only(self):
        self.night.rollup._llm_compose = AsyncMock(return_value="")

        result = await ClassifierRecovery(self.plugin).recover(self.work())

        self.assertEqual(result["sessions"][0]["status"], "retry_pending")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.night.rollup._llm_compose.await_count, 2)


if __name__ == "__main__":
    unittest.main()
