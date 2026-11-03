"""未读归档、失败重入、读取快照和实际 at_guard 钩子联动，平台均为离线替身。"""
import ast
import asyncio
from collections import defaultdict, deque
from dataclasses import replace
import importlib
import importlib.util
import json
import os
from pathlib import Path
import random
import sys
import time
import types
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_wake_persona_flow as base

ROOT = Path(base.bootstrap.BASE)
GUARD_ROOT = Path(os.environ.get("CONGLING_TEST_GUARD_ROOT",
                                 str(ROOT.parent / "astrbot_plugin_at_guard")))


def load_guard():
    """编译真实 Main 类；只替换框架类型，不替换闸门逻辑。"""
    sys.path.insert(0, str(GUARD_ROOT))
    class Plain:
        def __init__(self, text=""):
            self.text = text
    class TextPart(Plain):
        def mark_as_temp(self):
            self.no_save = True
            return self
    components = sys.modules["astrbot.api.message_components"]
    components.Plain = Plain
    components.Reply = type("Reply", (), {})
    components.At = type("At", (), {})  # 新版 at_guard 的发送模块需要此框架类型。
    message = types.ModuleType("astrbot.core.agent.message")
    message.TextPart = TextPart
    sys.modules[message.__name__] = message
    from guard_engine import Decision, GateEngine, POLICY_VERSION
    from guard_media import image_target
    from guard_image_cache import ImageObservationCache
    from guard_reply_delivery import ReplyDelivery, number
    from guard_expression import Expression, congling_for, takeover
    from guard_fragments import FragmentWait
    from guard_understanding import Understanding
    tree = ast.parse((GUARD_ROOT / "main.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Main")
    cls.bases = [ast.Name(id="object", ctx=ast.Load())]
    for method in cls.body:
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method.decorator_list = [d for d in method.decorator_list
                                     if not isinstance(d, ast.Call)]
    namespace = {
        "asyncio": asyncio, "time": time, "json": json, "re": __import__("re"),
        "html": __import__("html"), "math": __import__("math"), "importlib": importlib,
        "Path": Path, "replace": replace,
        "defaultdict": defaultdict, "deque": deque, "maxsize": sys.maxsize,
        "AstrMessageEvent": object, "ProviderRequest": object, "LLMResponse": object,
        "Context": object, "GateEngine": GateEngine, "image_target": image_target,
        "ImageObservationCache": ImageObservationCache,
        "ReplyDelivery": ReplyDelivery, "congling_for": congling_for,
        "number": number, "Expression": Expression, "takeover": takeover,
        "FragmentWait": FragmentWait, "Understanding": Understanding,
        "Decision": Decision, "POLICY_VERSION": POLICY_VERSION,
        "Plain": Plain, "At": type("At", (), {}), "Image": type("Image", (), {}),
        "Reply": type("Reply", (), {}), "TextPart": TextPart,
        "MessageType": SimpleNamespace(GROUP_MESSAGE="group", FRIEND_MESSAGE="private"),
        "NO_REPLY_TOKEN": "[[ASTRBOT_NO_REPLY]]", "BOT_LINE_PREFIX": "[我]: ",
        "MEANINGFUL_TEXT_RE": __import__("re").compile(r"[\w\u4e00-\u9fff]"),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 "actual_at_guard", "exec"), namespace)
    return namespace["Main"]


class UnreadFlow(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.FlowTest.asyncSetUp
    asyncTearDown = base.FlowTest.asyncTearDown
    archive = base.FlowTest.archive
    prepare = base.FlowTest.prepare
    event = base.FlowTest.event

    async def unread(self):
        return [r["msg_id"] for r in await self.store.fetch_all(
            "SELECT msg_id FROM msg_archive WHERE session_id='s' AND handled=0 ORDER BY id")]

    async def test_all_earlier_unread_including_previous_sleep_and_after_wake(self):
        await self.prepare()
        self.assertEqual(await self.unread(), [])
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM msg_archive WHERE handled=1"), 5)
        transcripts = "\n".join(c["prompt"] for c in self.model_calls if "只输出一个 JSON" in c["prompt"])
        for mid in ("old", "sleep-question", "new"):
            self.assertIn(f"msg_id={mid}", transcripts)

    async def test_no_200_character_truncation(self):
        text = "完整消息" * 200 + "尾部必须读到"
        await self.store.execute("UPDATE msg_archive SET text=? WHERE msg_id='old'", (text,))
        await self.prepare()
        self.assertTrue(any("尾部必须读到" in call["prompt"] for call in self.model_calls))

    async def test_completed_wake_does_not_skip_new_unread(self):
        await self.prepare()
        await self.archive("later", self.wake + 500)
        await self.prepare()
        self.assertEqual(await self.unread(), [])
        self.assertTrue(any("msg_id=later" in call["prompt"] for call in self.model_calls))

    async def test_old_cursor_does_not_hide_unread_holes(self):
        await self.night.rollup._save_cursor("s", 999999)
        await self.prepare()
        self.assertEqual(await self.unread(), [])

    async def test_generation_failure_retains_rows_across_another_wake(self):
        original = self.plugin.models.chat
        self.plugin.models.chat = AsyncMock(return_value="")
        with self.assertRaises(RuntimeError):
            await self.prepare()
        self.assertEqual(await self.unread(), ["old", "sleep-question", "new"])
        self.night.state.state.last_wake_at += 100
        self.plugin.models.chat = original
        await self.prepare()
        self.assertEqual(await self.unread(), [])
        self.assertEqual(self.sent.count("爬楼语"), 1)

    async def test_reading_does_not_consume_messages_arriving_mid_model_call(self):
        original = self.plugin.models.chat
        added = False
        async def chat(*args, **kwargs):
            nonlocal added
            if not added:
                added = True
                await self.archive("arriving", self.wake + 500)
            return await original(*args, **kwargs)
        self.plugin.models.chat = chat
        await self.prepare()
        self.assertEqual(await self.unread(), ["arriving"])
        await self.prepare()
        self.assertEqual(await self.unread(), [])

    async def test_read_flags_and_memory_flags_roll_back_together(self):
        await self.store.execute(
            "INSERT INTO mem_atom(persona_id,session_id,text,unread,source_msg_ids) "
            "VALUES('ling','s','旧事实',1,'[\"old\"]')")
        await self.store.execute(
            "CREATE TRIGGER deny_read BEFORE UPDATE OF unread ON mem_atom "
            "BEGIN SELECT RAISE(ABORT,'read flag fault'); END")
        with self.assertRaises(Exception):
            await self.prepare()
        self.assertEqual(await self.unread(), ["old", "sleep-question", "new"])
        self.assertEqual(await self.store.scalar("SELECT unread FROM mem_atom"), 1)
        await self.store.execute("DROP TRIGGER deny_read")
        await self.prepare()
        self.assertEqual(await self.unread(), [])
        self.assertEqual(await self.store.scalar("SELECT unread FROM mem_atom"), 0)
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])

    async def test_memory_sources_in_separate_batches_clear_only_after_both_are_read(self):
        await self.store.execute(
            "INSERT INTO mem_atom(persona_id,session_id,text,unread,source_msg_ids) "
            "VALUES('ling','s','两条证据',1,'[\"old\",\"future\"]')")
        await self.prepare()
        self.assertEqual(await self.store.scalar("SELECT unread FROM mem_atom"), 1)
        await self.archive("future", self.wake + 500)
        await self.prepare()
        self.assertEqual(await self.store.scalar("SELECT unread FROM mem_atom"), 0)

    async def test_manual_crawl_reads_old_and_new_history_instead_of_only_clearing_flags(self):
        result = await self.night.crawl("s")
        self.assertEqual(await self.unread(), [])
        self.assertIn("有人询问视频", result)
        self.assertEqual(self.sent, [])
        self.assertTrue(any("msg_id=old" in c["prompt"] for c in self.model_calls))

    async def test_unknown_send_survives_new_wake_without_duplicate(self):
        self.context.send_message = AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            await self.prepare()
        self.night.state.state.last_wake_at += 100
        with self.assertRaisesRegex(RuntimeError, "结果未知"):
            await self.prepare()
        self.context.send_message.assert_awaited_once()
        self.assertEqual(await self.unread(), ["old", "sleep-question", "new"])

    async def test_crawl_with_supplement_disabled_still_reads_history(self):
        self.cfg.override("night.rollup.enabled", False)
        await self.prepare()
        self.assertEqual(await self.unread(), [])
        self.assertEqual(self.sent, ["爬楼语"])
        self.assertTrue(self.model_calls)

    async def test_reply_limit_does_not_reset_for_later_unread_in_same_wake(self):
        self.cfg.override("night.rollup.max_replies_per_wake", 1)
        await self.prepare()
        self.assertEqual(self.sent.count("补课答复"), 1)
        await self.archive("later-question", self.wake + 500)
        self.night.rollup._llm_summarize = AsyncMock(return_value=(
            [base.rollup_mod.RollupItem(msg_id="later-question", what="新问题",
                                        targets_me=True, priority=100)], "新问题摘要"))
        await self.prepare()
        self.assertEqual(self.sent.count("补课答复"), 1)
        self.assertEqual(await self.unread(), [])

    async def test_current_question_is_excluded_and_only_marked_after_delivery(self):
        self.plugin.enabled_for = lambda _: True
        self.plugin.qqext = self.plugin.diary = self.plugin.capabilities = None
        event = self.event()
        req = SimpleNamespace(prompt="当前问题", system_prompt="", contexts=[])
        await base.main_hook("persona_hook")(self.plugin, event, req)
        self.assertEqual(await self.unread(), ["new"])
        self.assertEqual(req.prompt, "当前问题")
        self.assertTrue(any("历史" in str(p) for p in req.extra_user_content_parts))
        event.get_result = lambda: SimpleNamespace(chain=[])
        await base.main_hook("after_sent_hook")(self.plugin, event)
        self.assertEqual(await self.unread(), ["new"])
        event.get_result = lambda: SimpleNamespace(chain=[SimpleNamespace(text="当前答复")])
        await base.main_hook("after_sent_hook")(self.plugin, event)
        self.assertEqual(await self.unread(), [])

    async def test_astrbot_message_object_id_keeps_current_message_out_of_history(self):
        self.plugin.enabled_for = lambda _: True
        self.plugin.qqext = self.plugin.diary = self.plugin.capabilities = None
        event = self.event()
        del event.get_message_id
        event.message_obj = SimpleNamespace(message_id="new")
        req = SimpleNamespace(prompt="当前问题", system_prompt="", contexts=[])
        await base.main_hook("persona_hook")(self.plugin, event, req)
        self.assertFalse(event.extras.get("stopped"))
        self.assertEqual(await self.unread(), ["new"])
        event.get_result = lambda: SimpleNamespace(chain=[SimpleNamespace(text="当前答复")])
        await base.main_hook("after_sent_hook")(self.plugin, event)
        self.assertEqual(await self.unread(), [])

    async def test_large_history_is_read_in_order_without_total_count_cap(self):
        await self.store.execute("DELETE FROM msg_archive")
        self.cfg.override("night.rollup.chunk_size", 45)
        # 一千零一条超过常见的 100/500/1000 上限；实际全文必须逐块送入模型。
        for index in range(1001):
            await self.archive(f"m{index}", self.since - 500 + index)
        await self.prepare()
        transcripts = [c["prompt"] for c in self.model_calls if "只输出一个 JSON" in c["prompt"]]
        self.assertEqual(len(transcripts), 23)
        text = "\n".join(transcripts)
        self.assertEqual(text.count("[msg_id="), 1001)
        self.assertLess(text.index("[msg_id=m0]"), text.index("[msg_id=m1000]"))
        self.assertEqual(await self.unread(), [])

    async def test_seeded_arrival_schedules_never_consume_future_rows(self):
        rng = random.Random(20261103)
        original = self.plugin.models.chat
        for schedule in range(12):
            await self.store.execute("DELETE FROM msg_archive")
            await self.store.execute("DELETE FROM kv WHERE k LIKE 'night.wake_flow:%'")
            for index in range(4):
                await self.archive(f"initial-{index}", self.since + index)
            arrival = rng.randrange(4)
            calls = 0
            async def chat(*args, **kwargs):
                nonlocal calls
                if calls == arrival:
                    await self.archive(f"future-{schedule}", self.wake + 500)
                calls += 1
                return await original(*args, **kwargs)
            self.plugin.models.chat = chat
            await self.prepare()
            self.assertEqual(await self.unread(), [f"future-{schedule}"])

    def guard(self, action):
        Main = load_guard()
        from guard_engine import GateEngine
        guard = Main.__new__(Main)
        guard.config = {"gate_mode": "enforce", "fragment_wait_seconds": 0,
                        "proactive_cooldown_seconds": 0, "duplicate_window_seconds": 0,
                        "gate_hourly_budget": 0}
        guard.logger = SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, debug=lambda *a: None)
        guard.engine = GateEngine(guard.config, None, guard.logger)
        guard.recent_messages = defaultdict(lambda: deque(maxlen=30))
        guard.recent_images = defaultdict(lambda: deque(maxlen=30))
        guard.context_revision = defaultdict(int)
        guard.replied_fingerprints = {}
        guard.last_bot_line = guard.last_bot_line_at = {}
        guard.last_proactive_reply = guard.proactive_inflight_until = {}
        guard.participation_stats = guard._empty_participation_stats()
        guard.participation_stats_path = None
        provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(
            completion_text=json.dumps({"act": action, "conf": 85 if action == "reply" else 20,
                                        "understood": True, "reason": "continuation" if action == "reply"
                                        else "others_talking", "ground": "none"}))))
        guard.context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        return guard

    async def dispatch(self, action):
        guard = self.guard(action)
        event = self.event()
        event.message_str = "当前问题你怎么看"
        event.is_at_or_wake_command = False
        event.get_message_type = lambda: "group"
        event.get_messages = lambda: []
        event.get_self_id = lambda: "bot"
        event.get_sender_name = lambda: "测试用户"
        event.get_extra = lambda key, default=None: event.extras.get(key, default)
        self.plugin.enabled_for = lambda _: True
        self.plugin.qqext = self.plugin.diary = self.plugin.capabilities = None
        req = SimpleNamespace(prompt="当前问题", system_prompt="", contexts=[], extra_user_content_parts=[])
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        priority = next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                        and any(getattr(t, "id", "") == "PRIO_PERSONA" for t in n.targets))
        # 使用真实钩子的优先级和真实处理函数，模拟框架的降序执行。
        for _, hook in sorted([(priority, lambda: base.main_hook("persona_hook")(self.plugin, event, req)),
                               (10000, lambda: guard.guard_llm_request(event, req))], reverse=True):
            await hook()
            if event.extras.get("stopped"):
                break
        return guard, event, req

    @unittest.skipUnless((GUARD_ROOT / "main.py").exists(), "跨插件验证需要同级 at_guard 源码")
    async def test_actual_gate_silence_preserves_all_unread_without_crawl_send(self):
        _, event, _ = await self.dispatch("silent")
        self.assertTrue(event.extras.get("stopped"))
        self.assertEqual(self.sent, [])
        self.assertEqual(await self.unread(), ["old", "sleep-question", "new"])
        self.assertIsNone(await self.store.kv_get("night.wake_flow:s"))

    @unittest.skipUnless((GUARD_ROOT / "main.py").exists(), "跨插件验证需要同级 at_guard 源码")
    async def test_actual_gate_allows_crawl_then_keeps_current_reply_request(self):
        guard, event, req = await self.dispatch("reply")
        self.assertFalse(event.extras.get("stopped"))
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])
        self.assertEqual(req.prompt, "当前问题")
        self.assertEqual(await self.unread(), ["new"])
        self.assertEqual(guard._stale_reason(event, "s"), "")
        response = SimpleNamespace(completion_text="当前问题的正常答复", result_chain=None,
                                   tools_call_args=[], tools_call_name=[])
        await guard.suppress_no_reply_and_track(event, response)
        result = SimpleNamespace(chain=[SimpleNamespace(text=response.completion_text)])
        event.get_result = lambda: result
        await guard.recheck_before_send(event)
        self.assertTrue(result.chain)
        self.assertFalse(event.extras.get("stopped"))

    @unittest.skipUnless((GUARD_ROOT / "main.py").exists(), "跨插件验证需要同级 at_guard 源码")
    async def test_gate_timeout_excludes_only_preparation_and_keeps_revision_check(self):
        guard = self.guard("reply")
        event = self.event()
        event.get_extra = lambda key, default=None: event.extras.get(key, default)
        event.set_extra("_at_guard_recorded_at", 100)
        event.set_extra("_at_guard_judged_revision", 0)
        event.set_extra("congling:wake_prepare_seconds", 45)
        with patch("time.monotonic", return_value=160):
            self.assertEqual(guard._stale_reason(event, "s"), "")
            guard.context_revision["s"] = 1
            self.assertEqual(guard._stale_reason(event, "s"), "context_changed")
        guard.context_revision["s"] = 0
        with patch("time.monotonic", return_value=180):
            self.assertEqual(guard._stale_reason(event, "s"), "message_expired")


if __name__ == "__main__":
    unittest.main()
