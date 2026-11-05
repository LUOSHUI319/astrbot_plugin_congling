"""感知与能力缓存专项；只运行替身模型、内存库，不接触真实平台。"""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

ROOT = Path(os.environ.get("CONGLING_REUSE_SOURCE", str(Path(__file__).resolve().parents[1])))
sys.path.insert(0, str(ROOT))
if "astrbot.api" not in sys.modules:
    import night_selftest
    night_selftest._mock_astrbot()
if "astrbot.api.star" not in sys.modules:
    star = types.ModuleType("astrbot.api.star")
    star.StarTools = SimpleNamespace(get_data_dir=lambda _: (_ for _ in ()).throw(
        AssertionError("禁止访问生产路径")))
    sys.modules[star.__name__] = star
pkg = types.ModuleType("reuse_cg")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg
Service = importlib.import_module("reuse_cg.perception.service").PerceptionService
Media = importlib.import_module("reuse_cg.perception.media").MediaInterpreter
Registry = importlib.import_module("reuse_cg.capability.registry").CapabilityRegistry
contracts = importlib.import_module("reuse_cg.core.contracts")
Config = importlib.import_module("reuse_cg.core.config").Config


class Image:
    url = "https://fixture.invalid/image"


class Event:
    def __init__(self, origin="qq:private:1", sender="1", persona="ling"):
        self.unified_msg_origin = origin
        self.sender = sender
        self.persona = persona
        self.message_obj = SimpleNamespace(message=[Image()], message_id="m1")
        self.bot = None
    def get_sender_id(self): return self.sender
    def get_group_id(self): return ""
    def get_sender_name(self): return "用户"
    def get_self_id(self): return "bot"
    def get_extra(self, _): return self.persona


class Store:
    def __init__(self):
        self.kv = {}
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE msg_archive(id INTEGER PRIMARY KEY, msg_id, session_id, "
                        "session_kind, sender_id, sender_name, ts, text, blocks_json, expanded, "
                        "forward_id, umo, platform, is_at_me, reply_to_msg_id, handled)")
    async def kv_get(self, key): return self.kv.get(key)
    async def kv_set(self, key, value): self.kv[key] = value
    async def execute(self, sql, args): self.db.execute(sql, args)


class ReuseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.db.close)
        self.hub = SimpleNamespace(describe_image=AsyncMock(return_value="截图报错。" * 100))
        self.plugin = SimpleNamespace(cfg=Config({}), store=self.store, models=self.hub,
            bus=SimpleNamespace(ON_PERCEIVED="perceived", emit=AsyncMock()))

    async def test_archive_night_and_request_perceive_once_and_write_observation(self):
        svc = Service(self.plugin)
        event = Event()
        first = await svc.perceive(event, enrich=False)
        await svc.archive(first)
        self.assertEqual(self.hub.describe_image.await_count, 0)
        enriched = await svc.perceive(event)
        self.assertIs(first, enriched)
        await svc.archive(enriched)
        again = await svc.perceive(event)
        await svc.archive(again)
        self.assertEqual(self.hub.describe_image.await_count, 1)
        text = self.store.db.execute("SELECT text FROM msg_archive").fetchone()[0]
        self.assertIn("截图报错", text)
        self.assertGreater(len(text), 200)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM msg_archive").fetchone()[0], 1)
        self.plugin.bus.emit.assert_awaited_once()

    async def test_fresh_enrich_retains_observation_in_text_and_other_events_stay_separate(self):
        svc = Service(self.plugin)
        first = await svc.perceive(Event())
        second = await svc.perceive(Event(origin="qq:group:1"))
        self.assertIsNot(first, second)
        self.assertIn("截图报错", first.text)
        self.assertEqual(self.hub.describe_image.await_count, 2)

    async def test_media_parallel_same_source_calls_once_but_private_scope_isolated(self):
        media = Media(self.plugin)
        result = await asyncio.gather(*[media.describe_image("u", "private:1") for _ in range(8)])
        self.assertEqual(len(set(result)), 1)
        self.assertEqual(self.hub.describe_image.await_count, 1)
        await media.describe_image("u", "group:1")
        self.assertEqual(self.hub.describe_image.await_count, 2)
        self.assertEqual(self.store.kv, {})

    async def test_media_expiry_and_failed_result_are_not_reused(self):
        media = Media(self.plugin)
        self.hub.describe_image.side_effect = ["", "事实", "更新事实"]
        self.assertEqual(await media.describe_image("u", "p"), "[图片]")
        self.assertIn("事实", await media.describe_image("u", "p"))
        media._images = {key: (0, value[1]) for key, value in media._images.items()}
        self.assertIn("更新事实", await media.describe_image("u", "p"))
        self.assertEqual(self.hub.describe_image.await_count, 3)

    def registry(self, handler, ttl=60):
        registry = Registry(self.plugin)
        registry.register(contracts.Capability(name="knowledge.web", description="fixture",
            handler=handler, cache_ttl=ttl, rate_limit=1))
        return registry

    async def test_capability_concurrent_singleflight_and_cached_hits_dont_consume_quota(self):
        handler = AsyncMock(return_value={"text": "网页事实"})
        registry = self.registry(handler)
        results = await asyncio.gather(*[
            registry.invoke("knowledge.web", event=Event(), session_id="s", query="q")
            for _ in range(8)])
        self.assertTrue(all(result.ok for result in results))
        handler.assert_awaited_once()
        self.assertTrue((await registry.invoke("knowledge.web", event=Event(), session_id="s", query="q")).cached)

    async def test_capability_user_persona_scope_and_explicit_errors(self):
        handler = AsyncMock(return_value={"text": "网页事实"})
        registry = self.registry(handler)
        for index, event in enumerate((Event(), Event(sender="2"), Event(persona="new"),
                                       Event(origin="qq:group:1"))):
            await registry.invoke("knowledge.web", event=event, session_id=str(index), query="q")
        self.assertEqual(handler.await_count, 4)
        handler = AsyncMock(return_value={"ok": False, "text": "失败", "error": "offline"})
        registry = self.registry(handler)
        self.assertFalse((await registry.invoke("knowledge.web", event=Event(), query="x")).ok)
        self.assertFalse(any("offline" in json.dumps(row) for row in self.store.kv.values()))

    async def test_expired_capability_and_side_effects_are_not_coalesced(self):
        handler = AsyncMock(return_value="事实")
        registry = self.registry(handler)
        with patch("reuse_cg.capability.registry.time.time", return_value=10):
            await registry.invoke("knowledge.web", event=Event(), query="q", session_id="s")
        with patch("reuse_cg.capability.registry.time.time", return_value=71):
            self.assertFalse((await registry.invoke("knowledge.web", event=Event(), query="q", session_id="s")).cached)
        handler = AsyncMock(return_value="已执行")
        registry = self.registry(handler, ttl=0)
        await registry.invoke("knowledge.web", query="q", session_id="a")
        await registry.invoke("knowledge.web", query="q", session_id="b")
        self.assertEqual(handler.await_count, 2)


if __name__ == "__main__":
    unittest.main()
