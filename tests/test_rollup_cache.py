"""分块缓存、低输出阅读与补答隔离；真实 SQLite，模型与平台离线。"""
import asyncio
import importlib
import json
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_wake_persona_flow as base

cache_mod = importlib.import_module("cg.night.rollup_cache")
if "astrbot.api.star" not in sys.modules:
    star = types.ModuleType("astrbot.api.star")
    star.StarTools = SimpleNamespace(get_data_dir=lambda *a: (_ for _ in ()).throw(
        AssertionError("离线测试不能访问生产数据目录")))
    sys.modules[star.__name__] = star
reset_mod = importlib.import_module("cg.memory.reset")


class CacheTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.FlowTest.asyncSetUp
    asyncTearDown = base.FlowTest.asyncTearDown
    archive = base.FlowTest.archive
    prepare = base.FlowTest.prepare

    async def read(self, *, items=False, rows=None, umo=None):
        rows = rows if rows is not None else await self.night.crawler.pending("s")
        return await self.night.rollup.rollup_session(
            "s", rows=rows, commit=False, read_only=True, include_items=items,
            umo=self.row["umo"] if umo is None else umo)

    async def cache_count(self):
        return await self.store.scalar("SELECT COUNT(*) FROM kv WHERE k LIKE ?",
                                       (cache_mod.PREFIX + "%",))

    async def unread(self):
        return await self.store.scalar("SELECT COUNT(*) FROM msg_archive WHERE handled=0")

    async def test_later_chunk_failure_reuses_first_after_reopening_database(self):
        original = self.plugin.models.chat
        async def fail_second(prompt, **kwargs):
            if "[msg_id=sleep-question]" in prompt:
                return ""
            return await original(prompt, **kwargs)
        self.plugin.models.chat = fail_second
        with self.assertRaises(RuntimeError):
            await self.prepare()
        self.assertEqual(await self.cache_count(), 2)
        self.assertEqual(await self.unread(), 3)
        self.assertEqual(await self.night.rollup._cursor("s"), 0)
        self.assertEqual(self.sent, ["爬楼语"])
        await self.store.close()
        await self.store.open()
        self.night.rollup = base.rollup_mod.Rollup(self.plugin)
        self.night.wake_flow = base.flow_mod.WakeFlow(self.night)
        self.plugin.models.chat = original
        self.model_calls.clear()
        await self.prepare()
        transcripts = [call["prompt"] for call in self.model_calls if "只输出一个 JSON" in call["prompt"]]
        self.assertEqual(len(transcripts), 2)
        self.assertFalse(any("[msg_id=old]" in text for text in transcripts))
        self.assertEqual(await self.unread(), 0)
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])

    async def test_read_only_does_not_supply_reply_items(self):
        read = await self.read()
        self.assertTrue(all(not chunk.items for chunk in read))
        self.model_calls.clear()
        full = await self.read(items=True)
        self.assertEqual(len(self.model_calls), 3)
        self.assertTrue(any(chunk.items for chunk in full))
        self.assertTrue(all(call["max_tokens"] == 2000 for call in self.model_calls))

    async def test_full_result_also_supplies_read_only_summary(self):
        await self.read(items=True)
        self.model_calls.clear()
        chunks = await self.read()
        self.assertEqual(self.model_calls, [])
        self.assertTrue(all(chunk.summary and not chunk.items for chunk in chunks))
        self.assertEqual(await self.unread(), 3)

    async def test_read_only_prompt_omits_priority_and_uses_512_output_limit(self):
        self.night.state.state.mode = "awake"
        self.night.rollup.triage = AsyncMock()
        self.night.rollup.compose_reply = AsyncMock()
        await self.prepare()
        self.assertEqual(len(self.model_calls), 3)
        for call in self.model_calls:
            self.assertEqual(call["max_tokens"], 512)
            self.assertNotIn('"items"', call["prompt"])
            self.assertNotIn("priority", call["prompt"])
        self.night.rollup.triage.assert_not_awaited()
        self.night.rollup.compose_reply.assert_not_awaited()
        self.assertEqual(self.sent, [])
        self.assertEqual(await self.unread(), 0)

    async def test_reply_disabled_still_reads_without_scoring(self):
        self.cfg.override("night.rollup.enabled", False)
        self.night.rollup.triage = AsyncMock()
        await self.prepare()
        self.assertTrue(all(call["max_tokens"] == 512 for call in self.model_calls))
        self.night.rollup.triage.assert_not_awaited()
        self.assertEqual(self.sent, ["爬楼语"])

    async def test_exhausted_reply_budget_does_not_generate_items_or_call_guard(self):
        epoch = list(await self.night.wake_flow.epoch())
        await self.store.kv_set("night.wake_flow:s", {
            "epoch": epoch, "done": True, "reply_count": self.night.rollup._max_replies(),
            "announced_epoch": epoch,
        })
        self.night.rollup.triage = AsyncMock()
        self.night.rollup.compose_reply = AsyncMock()
        await self.prepare()
        self.assertTrue(all(call["max_tokens"] == 512 for call in self.model_calls))
        self.night.rollup.triage.assert_not_awaited()
        self.night.rollup.compose_reply.assert_not_awaited()
        self.assertEqual(self.sent, [])

    async def test_delivered_reply_then_read_commit_failure_reuses_summary_without_scoring(self):
        original = self.night.crawler.mark_read
        self.night.crawler.mark_read = AsyncMock(side_effect=RuntimeError("写已读失败"))
        with self.assertRaisesRegex(RuntimeError, "写已读失败"):
            await self.prepare()
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])
        self.night.crawler.mark_read = original
        self.night.rollup.triage = AsyncMock()
        self.model_calls.clear()
        await self.prepare()
        self.assertEqual(self.model_calls, [])
        self.night.rollup.triage.assert_not_awaited()
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])
        self.assertEqual(await self.unread(), 0)

    async def test_manual_crawl_reads_only(self):
        result = await self.night.crawl("s")
        self.assertIn("有人询问视频", result)
        self.assertTrue(all(call["max_tokens"] == 512 for call in self.model_calls))
        self.assertEqual(await self.unread(), 0)

    async def test_changed_model_provider_and_endpoint_invalidate(self):
        await self.read()
        for key, value in (("model", "another-model"), ("id", "another-provider"),
                           ("api_base", "https://offline.invalid/v2")):
            self.provider.provider_config[key] = value
            self.model_calls.clear()
            await self.read()
            self.assertEqual(len(self.model_calls), 3, key)

    async def test_unknown_provider_identity_disables_cache(self):
        self.provider.provider_config = {}
        await self.read()
        self.model_calls.clear()
        await self.read()
        self.assertEqual(len(self.model_calls), 3)
        self.assertEqual(await self.cache_count(), 0)

    async def test_provider_meta_and_get_model_are_supported(self):
        self.provider.provider_config = {}
        self.provider.meta = lambda: SimpleNamespace(id="meta-provider")
        self.provider.get_model = lambda: "meta-model"
        await self.read()
        self.model_calls.clear()
        await self.read()
        self.assertEqual(self.model_calls, [])

    async def test_model_changes_mid_call_do_not_save_under_old_identity(self):
        original = self.plugin.models.chat
        async def change(*args, **kwargs):
            self.provider.provider_config["model"] = "new-model"
            return await original(*args, **kwargs)
        self.plugin.models.chat = change
        await self.read()
        self.assertEqual(await self.cache_count(), 0)
        self.plugin.models.chat = original
        self.model_calls.clear()
        await self.read()
        self.assertEqual(len(self.model_calls), 3)

    async def test_persona_selection_and_persona_prompt_changes_invalidate(self):
        await self.read()
        self.cfg.override("persona.astrbot_persona_id", "另一个")
        self.model_calls.clear()
        await self.read()
        self.assertEqual(len(self.model_calls), 3)
        self.context.persona_manager.items["另一个"].system_prompt += "新设定"
        self.model_calls.clear()
        await self.read()
        self.assertEqual(len(self.model_calls), 3)

    async def test_umo_and_session_do_not_share_private_or_cross_platform_results(self):
        await self.read()
        self.model_calls.clear()
        await self.read(umo="other-platform:GroupMessage:s")
        self.assertEqual(len(self.model_calls), 3)
        rows = await self.night.crawler.pending("s")
        foreign = [{**row, "session_id": "private-session"} for row in rows]
        self.model_calls.clear()
        await self.night.rollup.rollup_session(
            "private-session", rows=foreign, commit=False, read_only=True, include_items=False,
            umo="qq:FriendMessage:private-session")
        self.assertEqual(len(self.model_calls), 3)

    async def test_changed_message_text_invalidates_only_changed_block(self):
        await self.read()
        await self.store.execute("UPDATE msg_archive SET text='修改后的全文' WHERE msg_id='old'")
        self.model_calls.clear()
        await self.read()
        self.assertEqual(len(self.model_calls), 1)
        self.assertIn("修改后的全文", self.model_calls[0]["prompt"])

    async def test_new_wake_reuses_summary_but_refreshes_full_items(self):
        await self.read(items=True)
        self.night.state.state.last_wake_at += 1
        self.model_calls.clear()
        await self.read()
        self.assertEqual(self.model_calls, [])
        await self.read(items=True)
        self.assertEqual(len(self.model_calls), 3)

    async def test_ttl_and_global_limit_prune_without_deleting_other_kv(self):
        await self.store.kv_set("unrelated", "保留")
        with patch.object(cache_mod.time, "time", return_value=100):
            await self.night.rollup.cache.put({"first": {"summary": "摘要"}})
        with patch.object(cache_mod.time, "time", return_value=100 + cache_mod.TTL_SECONDS):
            self.assertIsNone(await self.night.rollup.cache.get("first"))
            await self.night.rollup.cache.put({
                f"{i:04d}": {"summary": str(i)} for i in range(cache_mod.MAX_ENTRIES + 9)})
        self.assertEqual(await self.cache_count(), cache_mod.MAX_ENTRIES)
        self.assertIsNone(await self.store.kv_get(cache_mod.PREFIX + "first"))
        self.assertEqual(await self.store.kv_get("unrelated"), "保留")

    async def test_bad_checksum_and_invalid_json_recompute_only_bad_blocks(self):
        await self.read()
        row = await self.store.fetch_one("SELECT k,v FROM kv WHERE k LIKE ? ORDER BY k LIMIT 1",
                                         (cache_mod.PREFIX + "%",))
        entry = json.loads(row["v"])
        entry["payload"]["summary"] = "被修改"
        await self.store.kv_set(row["k"], entry)
        self.model_calls.clear()
        await self.read()
        self.assertEqual(len(self.model_calls), 1)
        await self.store.execute("UPDATE kv SET v='broken json' WHERE k=?", (row["k"],))
        self.model_calls.clear()
        await self.read()
        self.assertEqual(len(self.model_calls), 1)

    async def test_cache_write_failure_leaves_all_unread_and_rolls_back(self):
        await self.store.execute(
            "CREATE TRIGGER deny_cache BEFORE INSERT ON kv WHEN NEW.k LIKE 'night.rollup.cache:%' "
            "BEGIN SELECT RAISE(ABORT,'cache fault'); END")
        with self.assertRaises(Exception):
            await self.prepare()
        self.assertEqual(await self.cache_count(), 0)
        self.assertEqual(await self.unread(), 3)
        self.assertEqual(await self.night.rollup._cursor("s"), 0)

    async def test_parallel_readers_reuse_each_block_once(self):
        await asyncio.gather(*(self.read() for _ in range(5)))
        self.assertEqual(len(self.model_calls), 3)
        self.assertEqual(await self.unread(), 3)

    async def test_full_long_text_is_read_and_not_copied_into_cache(self):
        text = "完整消息" * 500 + "尾部证据"
        await self.store.execute("UPDATE msg_archive SET text=? WHERE msg_id='old'", (text,))
        await self.read()
        self.assertTrue(any(text in call["prompt"] for call in self.model_calls))
        values = await self.store.fetch_all("SELECT v FROM kv WHERE k LIKE ?",
                                            (cache_mod.PREFIX + "%",))
        self.assertFalse(any(text in row["v"] for row in values))

    async def test_all_memory_reset_clears_cache_and_keeps_old_rows_from_reappearing(self):
        await self.read(items=True)
        data = await self.store.transaction(reset_mod.snapshot)
        self.assertEqual(data["counts"]["rollup_cache_entries"], 6)
        self.assertEqual(data["counts"]["memory_kv_cursors"], 0)
        path = Path(self.temp.name) / "backup.json"
        checksum = reset_mod.write_backup(path, data)
        state = {"digest": reset_mod._digest(data), "owner": "offline-admin",
                 "backup_path": str(path), "backup_sha256": checksum}
        reset = reset_mod.MemoryReset(self.plugin)
        with patch.object(reset_mod, "_backup_path", return_value=Path(self.temp.name) / "final.json"):
            await reset._execute(state)
        self.assertEqual(await self.cache_count(), 0)
        self.assertEqual(await self.unread(), 0)
        self.model_calls.clear()
        await self.prepare()
        self.assertEqual(self.model_calls, [])
        # 兼容旧的增量入口也不能重读清除时保留的原文。
        self.assertEqual(await self.night.rollup.rollup_session("s"), [])
        self.assertEqual(self.model_calls, [])
        await self.archive("fresh", self.wake + 500)
        await self.prepare()
        self.assertEqual(len(self.model_calls), 1)
        self.assertIn("[msg_id=fresh]", self.model_calls[0]["prompt"])

    async def test_reset_guard_rejects_during_model_call_for_manual_read(self):
        started, finish = asyncio.Event(), asyncio.Event()
        original = self.plugin.models.chat
        async def slow(*args, **kwargs):
            started.set()
            await finish.wait()
            return await original(*args, **kwargs)
        self.plugin.models.chat = slow
        task = asyncio.create_task(self.night.crawl("s"))
        try:
            await started.wait()
            with self.assertRaisesRegex(ValueError, "正在运行"):
                async with self.store.memory_reset_guard():
                    self.fail("不能在阅读期间清除")
        finally:
            finish.set()
            await task
        self.assertEqual(await self.unread(), 0)

    async def test_cache_backup_restores_exact_entries_and_rejects_overwrite(self):
        await self.read(items=True)
        data = await self.store.transaction(reset_mod.snapshot)
        backup = Path(self.temp.name) / "cache-backup.json"
        checksum = reset_mod.write_backup(backup, data)
        target = base.Store(Path(self.temp.name) / "restore.db")
        await target.open()
        await target.close()
        with sqlite3.connect(target.path) as conn:
            reset_mod.restore_backup(conn, backup, checksum)
            rows = conn.execute(
                "SELECT k,v,updated_at FROM kv WHERE k LIKE ? ORDER BY k",
                (cache_mod.PREFIX + "%",)).fetchall()
            self.assertEqual([list(row) for row in rows], data["kv"])
            with self.assertRaisesRegex(ValueError, "非空"):
                reset_mod.restore_backup(conn, backup, checksum)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM kv WHERE k LIKE ?", (cache_mod.PREFIX + "%",)
            ).fetchone()[0], len(rows))

    async def test_failed_batch_keeps_upper_bound_when_new_messages_arrive_before_retry(self):
        original = self.plugin.models.chat
        async def fail_second(prompt, **kwargs):
            return "" if "[msg_id=sleep-question]" in prompt else await original(prompt, **kwargs)
        self.plugin.models.chat = fail_second
        with self.assertRaises(RuntimeError):
            await self.prepare()
        await self.archive("arriving-after-failure", self.wake + 500)
        self.plugin.models.chat = original
        self.model_calls.clear()
        await self.prepare()
        self.assertEqual(await self.unread(), 1)
        self.assertFalse(any("[msg_id=arriving-after-failure]" in call["prompt"]
                             for call in self.model_calls))
        self.model_calls.clear()
        await self.prepare()
        self.assertEqual(await self.unread(), 0)
        self.assertEqual(len(self.model_calls), 1)
        self.assertIn("[msg_id=arriving-after-failure]", self.model_calls[0]["prompt"])

    async def test_stop_scoring_later_chunks_once_reply_budget_is_filled(self):
        self.cfg.override("night.rollup.max_replies_per_wake", 1)
        original = self.plugin.models.chat
        async def items_for_each_row(prompt, **kwargs):
            msg_id = prompt.split("[msg_id=", 1)[1].split("]", 1)[0]
            return json.dumps({"summary": "本块摘要", "items": [
                {"msg_id": msg_id, "what": "请回答我", "targets_me": True, "priority": 100}]})
        self.plugin.models.chat = items_for_each_row
        triage = self.night.rollup.triage
        self.night.rollup.triage = AsyncMock(wraps=triage)
        await self.prepare()
        self.night.rollup.triage.assert_awaited_once()
        self.assertEqual(await self.unread(), 0)
        self.assertEqual(self.sent.count("补课答复"), 1)
        self.plugin.models.chat = original


if __name__ == "__main__":
    unittest.main()
