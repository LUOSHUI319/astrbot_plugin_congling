"""1.1.0 适配中的可选桥隔离、发送确认和清除闸门；只使用离线替身。"""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_congling.tests import run_diagnosis_regressions as base
base.module("astrbot.api.web").request = SimpleNamespace()
from astrbot_plugin_congling.core.contracts import MemoryDraft
from astrbot_plugin_congling.memory.bridge_livingmemory import LivingMemoryBridge
from astrbot_plugin_congling.qqext.qzone import QzoneBridge
from astrbot_plugin_congling.night import NightModule


class Adaptation(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.Regression.asyncSetUp
    asyncTearDown = base.Regression.asyncTearDown

    def bridge(self, enabled=True):
        self.cfg.override("bridge_livingmemory.enabled", enabled)
        bridge = LivingMemoryBridge(self.p)
        self.engine = SimpleNamespace(add_memory=AsyncMock(), search_memories=AsyncMock())
        bridge._ensure_engine = AsyncMock(return_value=self.engine)
        return bridge

    async def test_bridge_is_opt_in_by_default(self):
        bridge = LivingMemoryBridge(self.p)
        self.assertFalse(bridge._enabled)
        with patch("astrbot_plugin_congling.memory.bridge_livingmemory._get_lm_engine") as lookup:
            self.assertEqual(await bridge.search("秘密", where_ref="session:qq:private:A"), [])
            lookup.assert_not_called()

    async def test_bridge_filters_persona_platform_and_private_session(self):
        bridge = self.bridge()
        def item(ref=None, pid="ling", origin="congling"):
            return SimpleNamespace(content="事实", metadata={
                "congling_session_ref": ref, "congling_persona_id": pid, "memory_origin": origin})
        self.engine.search_memories.return_value = [
            item("session:qq:private:A"), item("session:qq:private:B"),
            item("session:telegram:private:A"), item("session:qq:private:A", "other"),
            item(), item("session:qq:private:A", origin="external"),
        ]
        self.assertEqual(len(await bridge.search("事实", where_ref="session:qq:private:A")), 1)
        self.assertEqual(await bridge.search("事实", where_ref=""), [])

    async def test_bridge_dedupe_includes_audience_and_does_not_copy_self(self):
        bridge = self.bridge()
        drafts = [MemoryDraft(text="喜欢茶", platform="qq", session_kind="private",
                              session_id=sid, subject_key=sid) for sid in ("A", "A", "B")]
        drafts += [MemoryDraft(text="私人梦", platform="qq", session_kind="private",
                               session_id="A", subject_key="__self__"),
                   MemoryDraft(text="身份不明")]
        await bridge._on_memory_written(drafts)
        self.assertEqual(self.engine.add_memory.await_count, 2)
        refs = [c.kwargs["metadata"]["congling_session_ref"]
                for c in self.engine.add_memory.await_args_list]
        self.assertEqual(refs, ["session:qq:private:A", "session:qq:private:B"])

    async def test_bridge_late_registration_is_retried(self):
        self.cfg.override("bridge_livingmemory.enabled", True)
        bridge = LivingMemoryBridge(self.p)
        engine = object()
        with patch("astrbot_plugin_congling.memory.bridge_livingmemory._get_lm_engine",
                   side_effect=[None, engine]):
            self.assertIsNone(await bridge._ensure_engine())
            self.assertIs(await bridge._ensure_engine(), engine)

    async def test_mirror_inflight_blocks_reset_without_cancelling_writer(self):
        bridge = self.bridge()
        entered, release = asyncio.Event(), asyncio.Event()
        async def write(**kwargs):
            entered.set()
            await release.wait()
        self.engine.add_memory.side_effect = write
        task = asyncio.create_task(bridge._on_memory_written([
            MemoryDraft(text="事实", platform="qq", session_kind="private",
                        session_id="A", subject_key="A")]))
        await entered.wait()
        try:
            with self.assertRaises(ValueError):
                async with self.store.memory_reset_guard():
                    pass
            self.assertFalse(task.done())
        finally:
            release.set()
            await task

    async def test_missing_selected_persona_rejected_without_config_change(self):
        self.p.raw_config = {}
        from astrbot_plugin_congling.page_api import PageApi
        self.p.context.persona_manager.get_persona.side_effect = ValueError("不存在")
        api = PageApi(self.p)
        before = self.cfg.snapshot()
        with self.assertRaises(ValueError):
            await api._persist({"persona": {"astrbot_persona_id": "missing"}})
        self.assertEqual(self.cfg.snapshot(), before)
        self.assertEqual(self.p.raw_config, {})

    async def test_wake_receipt_contains_progress_without_private_reply_text(self):
        night = self.p.night = NightModule(self.p)
        now = 1793500000
        night.state.state.last_wake_at = now
        await self.store.kv_set("night.last_sleep_window",
                                [{"sleep_at": now - 10, "wake_at": now, "kind": "night"}])
        night.crawler.mark_read = AsyncMock()
        night.crawler.mark_crawled = AsyncMock()
        await night.wake_flow.prepare("A", "qq:FriendMessage:A", AsyncMock())
        receipt = await self.store.kv_get("night.wake_flow:A")
        self.assertTrue(receipt["done"])
        self.assertNotIn("reply_text", receipt)

    async def test_native_qzone_publish_records_only_confirmed_success(self):
        bridge = QzoneBridge(self.p)
        bridge.available = bridge.write_allowed = lambda: True
        bridge._native_action = AsyncMock(return_value={"tid": "native-post"})
        bridge._publish_once = AsyncMock()
        self.assertTrue((await bridge.publish("已发布"))["ok"])
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 1)
        bridge._native_action.return_value = {}
        self.assertFalse((await bridge.publish("未知"))["ok"])
        bridge._publish_once.assert_not_called()
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 1)

    async def test_native_qzone_timeout_does_not_retry_http(self):
        bridge = QzoneBridge(self.p)
        bridge.available = bridge.write_allowed = lambda: True
        bridge._onebot_bot = lambda: object()
        bridge._call_action = AsyncMock(side_effect=TimeoutError())
        bridge._publish_once = AsyncMock()
        self.assertFalse((await bridge.publish("可能已发布"))["ok"])
        bridge._call_action.assert_awaited_once()
        bridge._publish_once.assert_not_called()
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 0)


if __name__ == "__main__":
    unittest.main()
