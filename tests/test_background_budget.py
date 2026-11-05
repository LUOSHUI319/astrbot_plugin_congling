"""后台预算、日去重及失败水位；模型/平台离线，使用真实临时 SQLite。"""
import asyncio
import importlib
import json
import sys
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_wake_persona_flow as base

if "astrbot.api.star" not in sys.modules:
    star = types.ModuleType("astrbot.api.star")
    star.StarTools = SimpleNamespace(get_data_dir=lambda *a: None)
    sys.modules[star.__name__] = star
budget_mod = importlib.import_module("cg.core.modelhub")
extractor_mod = importlib.import_module("cg.memory.extractor")
maintenance_mod = importlib.import_module("cg.memory.maintenance")
profile_mod = importlib.import_module("cg.memory.profile")


class Provider:
    """与原生适配器一样丢弃 kwargs，然后合入共享的配置请求体。"""
    def __init__(self, response="[]"):
        self.body = {"max_tokens": 8192, "thinking": {"type": "enabled"}}
        self.provider_config = {"model": "deepseek-flash", "custom_extra_body": self.body}
        self.calls = []
        self.response = response
        self.failure = None
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))

    async def create(self, **kwargs):
        await asyncio.sleep(0)
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        return SimpleNamespace(completion_text=self.response)

    async def text_chat(self, **kwargs):
        return await self.client.chat.completions.create(
            prompt=kwargs["prompt"], model=self.provider_config["model"], extra_body=self.body)


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_final_request_and_concurrent_main_are_isolated(self):
        provider = Provider()
        model = budget_mod.ModelHub(SimpleNamespace(
            cfg=object(), context=SimpleNamespace(get_using_provider=lambda: provider)))
        await asyncio.gather(model.chat("summary", max_tokens=512, background=True),
                             model.chat("main", max_tokens=1024))
        calls = {c["prompt"]: c for c in provider.calls}
        self.assertEqual(calls["summary"]["extra_body"]["thinking"], {"type": "disabled"})
        self.assertEqual(calls["summary"]["max_tokens"], 512)
        self.assertEqual(calls["main"]["extra_body"]["thinking"], {"type": "enabled"})
        self.assertEqual(calls["main"]["max_tokens"], 1024)
        self.assertEqual(provider.body, {"max_tokens": 8192, "thinking": {"type": "enabled"}})

    async def test_failure_and_cancellation_restore_all_scopes(self):
        for error in (RuntimeError("synthetic"), asyncio.CancelledError()):
            provider = Provider()
            provider.failure = error
            with self.assertRaises(type(error)):
                with budget_mod.provider_request_budget(provider, 512) as options:
                    await provider.text_chat(prompt="failure", **options)
            provider.failure = None
            await provider.text_chat(prompt="after")
            self.assertEqual(len(provider.calls), 2)
            self.assertNotIn("max_tokens", provider.calls[-1])
            self.assertEqual(provider.calls[-1]["extra_body"]["thinking"], {"type": "enabled"})

    async def test_hot_reload_reuses_wrapper(self):
        provider = Provider()
        with budget_mod.provider_request_budget(provider, 512) as options:
            await provider.text_chat(prompt="first", **options)
        wrapper = provider.client.chat.completions.create
        reloaded = importlib.reload(budget_mod)
        with reloaded.provider_request_budget(provider, 2048) as options:
            await provider.text_chat(prompt="second", **options)
        self.assertIs(wrapper, provider.client.chat.completions.create)
        self.assertEqual(provider.calls[-1]["max_tokens"], 2048)

    async def test_unknown_model_does_not_receive_vendor_parameter(self):
        provider = Provider()
        provider.provider_config["model"] = "some-other-model"
        provider.body = {}
        with budget_mod.provider_request_budget(provider, 512) as options:
            await provider.text_chat(prompt="other", **options)
        self.assertNotIn("thinking", provider.calls[-1]["extra_body"])

    async def test_router_without_sdk_receives_request_options(self):
        provider = SimpleNamespace(provider_config={"model": "deepseek-flash"})
        with budget_mod.provider_request_budget(provider, 512) as options:
            self.assertEqual(options, {"max_tokens": 512, "extra_body": {"thinking": {"type": "disabled"}}})


class MemoryBudgetTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.FlowTest.asyncSetUp
    asyncTearDown = base.FlowTest.asyncTearDown
    archive = base.FlowTest.archive

    async def setup_memory(self):
        self.provider = Provider("我知道测试用户下周要出差。")
        self.plugin.context.get_using_provider = lambda: self.provider
        self.plugin.bus = SimpleNamespace(ON_REFLECT="reflect", emit=AsyncMock())
        self.cfg.override("memory.reflect_hour", 0)
        for number in range(4):
            await self.store.execute(
                "INSERT INTO mem_atom(persona_id,subject_id,scope,text,importance,status) "
                "VALUES(?,?,'user',?,0.8,'active')",
                (self.cfg.persona_id, "test-user", f"测试用户的事实 {number}"))

    async def test_two_instances_generate_once_and_database_reopen_skips(self):
        await self.setup_memory()
        first = maintenance_mod.Maintenance(self.plugin, None, None)
        second = maintenance_mod.Maintenance(self.plugin, None, None)
        await asyncio.gather(first.reflect(), second.reflect())
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.provider.calls[0]["max_tokens"], 512)
        self.assertEqual(self.provider.calls[0]["extra_body"]["thinking"], {"type": "disabled"})
        await self.store.close()
        await self.store.open()
        await maintenance_mod.Maintenance(self.plugin, None, None).reflect()
        self.assertEqual(len(self.provider.calls), 1)
        self.plugin.bus.emit.assert_awaited_once()

    async def test_next_business_day_generates_again(self):
        await self.setup_memory()
        await self.store.kv_set(f"reflect:{self.cfg.persona_id}:test-user",
                                {"text": "旧的认识", "ts": int(time.time()) - 86400})
        await maintenance_mod.Maintenance(self.plugin, None, None).reflect()
        self.assertEqual(len(self.provider.calls), 1)

    async def test_existing_today_summary_is_used_on_first_upgrade(self):
        await self.setup_memory()
        await self.store.kv_set(f"reflect:{self.cfg.persona_id}:test-user",
                                {"text": "今天已有认识", "ts": int(time.time())})
        await maintenance_mod.Maintenance(self.plugin, None, None).reflect()
        self.assertEqual(self.provider.calls, [])

    async def test_empty_or_failed_reflection_can_retry(self):
        await self.setup_memory()
        self.provider.response = ""
        maintenance = maintenance_mod.Maintenance(self.plugin, None, None)
        await maintenance.reflect()
        self.assertIsNone(await self.store.kv_get(f"reflect:{self.cfg.persona_id}:test-user"))
        self.provider.response = "测试用户已改期，不是周五。"
        await maintenance.reflect()
        self.assertEqual(len(self.provider.calls), 2)

    async def test_before_scheduled_hour_does_not_call_model(self):
        await self.setup_memory()
        self.cfg.override("memory.reflect_hour", 4)
        with patch.object(maintenance_mod.time, "localtime", return_value=time.struct_time(
                (2026, 11, 4, 3, 0, 0, 2, 308, -1))):
            await maintenance_mod.Maintenance(self.plugin, None, None).reflect()
        self.assertEqual(self.provider.calls, [])

    async def test_persona_isolation(self):
        await self.setup_memory()
        await self.store.kv_set("reflect:other-persona:test-user",
                                {"text": "其他人格的认识", "ts": int(time.time())})
        await maintenance_mod.Maintenance(self.plugin, None, None).reflect()
        self.assertEqual(len(self.provider.calls), 1)

    async def test_extract_budget_and_truncated_json_does_not_advance_watermark(self):
        provider = Provider('[{"text":"测试用户欠款不是1200元而是800元"')
        self.plugin.context.get_using_provider = lambda: provider
        extractor = extractor_mod.MemoryExtractor(self.plugin)
        turns = [{"idx": 1, "role": "user", "text": "测试用户欠款不是1200元而是800元"}]
        self.assertEqual(await extractor.extract(turns), [])
        self.assertFalse(extractor.last_ok)
        self.assertEqual(provider.calls[0]["max_tokens"], 2560)
        self.assertEqual(provider.calls[0]["extra_body"]["thinking"], {"type": "disabled"})
        provider.response = "[]"
        await extractor.extract(turns)
        self.assertTrue(extractor.last_ok)

    async def test_large_atom_configuration_has_room_for_complete_json(self):
        provider = Provider("[]")
        self.plugin.context.get_using_provider = lambda: provider
        self.cfg.override("memory.max_atoms_per_extract", 30)
        await extractor_mod.MemoryExtractor(self.plugin).extract(
            [{"idx": 1, "role": "user", "text": "测试用户下周需要出差"}])
        self.assertEqual(provider.calls[0]["max_tokens"], 7680)

    async def test_extraction_rejects_assistant_evidence_but_keeps_user_fact(self):
        provider = Provider(json.dumps([
            {"text": "测试用户已经还了800元", "evidence": ["1"]},
            {"text": "机器人认为测试用户花生过敏", "evidence": ["1", "2"]}]))
        self.plugin.context.get_using_provider = lambda: provider
        extractor = extractor_mod.MemoryExtractor(self.plugin)
        drafts = await extractor.extract([
            {"idx": 1, "role": "user", "text": "我已经还了800元。"},
            {"idx": 2, "role": "assistant", "text": "测试用户花生过敏。"}])
        self.assertEqual([d.text for d in drafts], ["测试用户已经还了800元"])
        self.assertTrue(extractor.last_ok)

    async def test_extraction_rejects_unknown_evidence_without_another_model_call(self):
        provider = Provider(json.dumps([
            {"text": "测试用户的会议在周五", "evidence": ["999"]}]))
        self.plugin.context.get_using_provider = lambda: provider
        extractor = extractor_mod.MemoryExtractor(self.plugin)
        drafts = await extractor.extract([{"idx": 1, "role": "user", "text": "会议改到周一。"}])
        self.assertEqual(drafts, [])
        self.assertEqual(len(provider.calls), 1)

    async def test_profile_has_budget_and_invalid_json_preserves_existing_profile(self):
        provider = Provider("not json")
        self.plugin.context.get_using_provider = lambda: provider
        subjects = SimpleNamespace(atoms_of=AsyncMock(return_value=[
            {"id": i, "text": "测试用户喜欢清淡的饭菜"} for i in range(4)]),
            display_name=AsyncMock(return_value="测试用户"))
        affinity = SimpleNamespace(stage=AsyncMock(return_value=5))
        book = profile_mod.ProfileBook(self.plugin, subjects, affinity)
        result = await book.build("test-user", force=True)
        self.assertFalse(result["ok"])
        self.assertEqual(provider.calls[0]["max_tokens"], 1024)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_profile"), 0)


if __name__ == "__main__":
    unittest.main()
