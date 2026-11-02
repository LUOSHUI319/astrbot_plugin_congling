"""P4 七项断言；断电通过关闭 SQLite / 丢弃模块 / 重建启动链模拟，不直接造 delivered。"""
from __future__ import annotations

import asyncio
import ast
from datetime import datetime
import json
import sqlite3
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot_plugin_congling.tests import run_diagnosis_regressions as base
from astrbot_plugin_congling.capability.providers import build_builtin, dream_recall, dream_recall_intent
from astrbot_plugin_congling.capability.registry import CapabilityRegistry
from astrbot_plugin_congling.core.contracts import DreamEntry, NightState, PerceivedMessage
from astrbot_plugin_congling.memory.service import MemoryService
from astrbot_plugin_congling.night import NightModule, stages
from astrbot_plugin_congling.night.dream import LEVEL_DECAY

ROOT = Path(__file__).resolve().parents[1]
T0 = int(datetime(2026, 11, 1, 23).timestamp())


class Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.Regression.asyncSetUp(self)
        self.now = T0
        self.addCleanup(patch.stopall)
        patch("astrbot_plugin_congling.night.plans.time.time", side_effect=lambda: self.now).start()
        patch("astrbot_plugin_congling.night.energy.fatigue_kind", return_value="").start()
        patch("astrbot_plugin_congling.night.state.NightService._past_latest_sleep", return_value=False).start()
        self.cfg._raw["night"].update(dream_enabled=True, crawl_enabled=False, noon_dream_chance=1)
        self.p._background_tasks, self.p._stopping = set(), False
        self.p.spawn_background = types.MethodType(base.ConglingPlugin.spawn_background, self.p)
        self.p._background_done = types.MethodType(base.ConglingPlugin._background_done, self.p)
        self.p.models = types.SimpleNamespace(chat=AsyncMock(
            return_value=json.dumps({"text": "我梦见星河与蓝色鲸鱼", "level": 5, "tags": ["美梦"]})))
        self.night = self.p.night = NightModule(self.p)
        await self.night.start()
        self.p.is_admin = lambda e: e.admin

    async def asyncTearDown(self):
        await self.cancel_tasks()
        await base.Regression.asyncTearDown(self)

    async def cancel_tasks(self):
        tasks = list(self.p._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.p._background_tasks.clear()

    async def drain(self):
        while self.p._background_tasks:
            await asyncio.gather(*list(self.p._background_tasks))

    async def sleep(self, minutes=480, kind="night", drain=True):
        wake = self.now + minutes * 60
        with patch("astrbot_plugin_congling.night.energy.next_wake_time", return_value=wake):
            await self.night.state._do_sleep(reason="test", kind=kind, force=True)
        if drain:
            await self.drain()
        return await self.rows()

    async def rows(self):
        return await self.store.fetch_all("SELECT * FROM night_dream_plan ORDER BY id")

    async def restart(self, now):
        # 模拟 kill：取消任务、不调用 night.shutdown（不额外保存内存）；关闭旧连接。
        await self.cancel_tasks()
        path = self.store.path
        await self.store.close()
        self.now = now
        self.store = self.p.store = base.Store(path)
        await self.store.open()
        self.mem = self.p.memory = MemoryService(self.p)
        self.p.night = None
        # 实际生产启动函数：NightModule.start -> reconcile_dream_plans。
        await base.ConglingPlugin._boot_night(self.p)
        self.night = self.p.night
        await self.drain()

    def event(self, text="你昨晚梦见什么", *, group="", admin=True):
        return types.SimpleNamespace(
            _congling_msg=PerceivedMessage(text=text, session_id=group or "123",
                                           session_kind="group" if group else "private", sender_id="123"),
            get_group_id=lambda: group, get_sender_id=lambda: "123", admin=admin,
            get_platform_name=lambda: "qq", unified_msg_origin="123")

    def set_dreams(self, levels):
        self.night.dreamer._dreams = [
            DreamEntry(dream_id=f"d{i}", level=level, text=f"星河鲸鱼{i}",
                       created_at=self.now - 600 + i, decay_at=self.now - 600 + i)
            for i, level in enumerate(levels)]
        self.night.dreamer._loaded = True

    def wake_hints(self):
        self.night.state.state.mode = "awake"
        self.night.state._wake_sleep_at = self.now - 3600
        self.night.state.wake_hints.begin(self.now)


class Admission(Fixture):
    async def test_fixed_probability_and_minimum(self):
        d = self.night.dreamer
        self.assertEqual([d.remember_chance(n) for n in range(1, 6)], [0, 0, 0, .6, .9])
        self.cfg._raw["night"].update(dream_remember_by_level=False, dream_mention_min_level=1)
        self.assertEqual(d.remember_chance(3), 0)
        self.assertEqual(d.remember_chance(4), .6)
        self.cfg._raw["night"]["dream_mention_min_level"] = 5
        self.assertEqual(d.remember_chance(4), 0)

    async def test_thousand_wakes_match_sixty_and_ninety_percent(self):
        for level, expected in ((3, 0), (4, 600), (5, 900)):
            self.set_dreams([level])
            with patch("astrbot_plugin_congling.night.dream.random.random", side_effect=[i / 1000 for i in range(1000)]):
                hits = sum(self.night.dreamer.pick_to_mention() is not None for _ in range(1000))
            self.assertEqual(hits, expected)

    async def test_low_levels_twenty_turns_no_prompt_but_searchable(self):
        for level in (1, 2, 3):
            self.set_dreams([level])
            self.wake_hints()
            for _ in range(20):
                self.assertNotIn("梦", self.night.prompt_hint(session_key="a", include_private=True))
            result = await dream_recall(self.p, "鲸鱼", self.event())
            self.assertEqual(result["data"][0]["level"], level)
            self.assertIn("记不清", result["text"])
            self.assertNotIn("星河", result["text"])

    async def test_session_and_global_budgets(self):
        for scope, expected in (("session", (2, 2)), ("global", (2, 0))):
            self.cfg._raw["night"]["dream_hint_scope"] = scope
            self.set_dreams([5])
            self.wake_hints()
            with patch("astrbot_plugin_congling.night.dream.random.random", return_value=0):
                counts = tuple(sum("星河" in self.night.prompt_hint(session_key=key, include_private=True)
                                   for _ in range(20)) for key in ("a", "b"))
            self.assertEqual(counts, expected)

    async def test_self_events_filter_and_inject_cannot_bypass_budget(self):
        self.set_dreams([3, 4, 5])
        for dream in self.night.dreamer._dreams:
            await self.mem.record_self_event("dream", dream.dream_id, f"梦摘要{dream.level}",
                                            private_text=dream.text)
        summaries = await self.mem.self_events("123", "private", include_private=True)
        self.assertEqual({r["text"] for r in summaries}, {"梦摘要4", "梦摘要5"})
        req = types.SimpleNamespace(system_prompt="STATIC", extra_user_content_parts=[])
        await self.mem.inject(self.event(), req)
        text = "".join(p.text for p in req.extra_user_content_parts)
        self.assertNotIn("星河", text)
        self.assertNotIn("梦摘要", text)
        self.assertEqual(req.system_prompt, "STATIC")
        self.assertEqual(await self.mem.recall("星河", mode="all", cross_session=True), [])

    async def test_minimum_five_also_filters_behavior_summary(self):
        self.set_dreams([4])
        await self.mem.record_self_event("dream", "d0", "我做过梦")
        self.cfg._raw["night"]["dream_mention_min_level"] = 5
        self.assertEqual(await self.mem.self_events("123", "private"), [])

    async def test_low_dreams_do_not_reenter_diary_or_qzone_prompt(self):
        from astrbot_plugin_congling.core.contracts import night_key_of
        from astrbot_plugin_congling.memory.diary import Diary
        from astrbot_plugin_congling.qqext.qzone_daily import QzoneDaily
        self.set_dreams([1, 2, 3])
        for dream in self.night.dreamer._dreams:
            dream.night_key = night_key_of(self.now)
        self.night.state.state.dream_count_today = 3
        diary = Diary(self.p)
        self.assertEqual(await diary._dreams_for(night_key_of(self.now)), [])
        self.assertNotIn("梦", diary._night_state())
        self.assertFalse(any(i["kind"] == "dream" for i in await QzoneDaily(self.p).materials()))


class Recall(Fixture):
    async def test_tool_name_description_parameters_and_event(self):
        registry = CapabilityRegistry(self.p)
        await registry.start()
        cap = registry.caps["dream.recall"]
        self.assertEqual("congling_" + cap.name.replace(".", "_"), "congling_dream_recall")
        for word in ("梦想", "白日梦", "别做梦了", "中国梦", "不要调用"):
            self.assertIn(word, cap.description)
        self.assertTrue(cap.takes_event)
        self.assertFalse(cap.parameters.get("required"))
        self.assertEqual(cap.cache_ttl, 0)
        self.set_dreams([5])
        result = await registry.invoke("dream.recall", event=self.event(), query="")
        self.assertIn("星河", result.text)

    async def test_order_limit_and_literal_query_only_dreams(self):
        self.set_dreams([3, 5, 4, 5, 1, 2])
        self.cfg._raw["night"]["dream_recall_k"] = 3
        await self.mem.write_text("普通记忆专用词", scope="global")
        result = await dream_recall(self.p, _event=self.event())
        self.assertEqual([r["dream_id"] for r in result["data"]], ["d3", "d1", "d2"])
        self.assertEqual((await dream_recall(self.p, "普通记忆专用词", self.event()))["data"], [])
        self.assertEqual((await dream_recall(self.p, "鲸鱼0", self.event()))["data"][0]["level"], 3)
        self.assertEqual((await dream_recall(self.p, "%", self.event()))["data"], [])

    async def test_guard_default_off_does_not_keyword_trigger(self):
        self.set_dreams([5])
        # 默认依赖模型的语义调用；无需任何固定关键词才允许使用工具。
        result = await dream_recall(self.p, _event=self.event("睡着后脑海里出现了什么"))
        self.assertIn("星河", result["text"])
        self.cfg._raw["night"]["dream_recall_guard"] = True
        result = await dream_recall(self.p, _event=self.event("睡着后脑海里出现了什么"))
        self.assertEqual(result["data"], [])

    async def test_guard_blacklist_subject_and_missing_event(self):
        self.set_dreams([5])
        self.cfg._raw["night"]["dream_recall_guard"] = True
        for text in ("我的梦想是当程序员", "你别做梦了", "你做白日梦", "中国梦",
                     "你知道他做梦吗", "他梦见你", "我昨晚梦见你", "美梦成真"):
            self.assertFalse(dream_recall_intent(text), text)
            self.assertEqual((await dream_recall(self.p, _event=self.event(text)))["data"], [])
        for text in ("你做梦了吗", "你昨晚梦见什么", "你有没有做过梦", "你的梦境是什么"):
            self.assertTrue(dream_recall_intent(text), text)
            self.assertIn("星河", (await dream_recall(self.p, _event=self.event(text)))["text"])
        self.assertEqual((await dream_recall(self.p))["data"], [])

    async def test_private_body_not_leaked_by_tool_result_or_data(self):
        self.set_dreams([5])
        for event in (None, self.event(group="g"), self.event(admin=False)):
            result = await dream_recall(self.p, _event=event)
            self.assertNotIn("星河", json.dumps(result, ensure_ascii=False))
        self.assertIn("星河", (await dream_recall(self.p, _event=self.event()))["text"])


class Scheduling(Fixture):
    async def test_planned_committed_before_model_and_hybrid_only_three(self):
        async def model(*a, **kw):
            rows = await self.rows()
            self.assertGreaterEqual(len(rows), 4)
            self.assertTrue(all(r["status"] in ("planned", "generated") for r in rows))
            self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 0)
            self.assertEqual(self.night.dreamer.list_recent(), [])
            return '{"text":"预生成星河","level":4,"tags":["美梦"]}'
        self.p.models.chat.side_effect = model
        rows = await self.sleep()
        self.assertLessEqual(len(rows), 8)
        self.assertEqual(self.p.models.chat.await_count, 3)
        self.assertEqual([r["status"] for r in rows[:3]], ["generated"] * 3)
        self.assertTrue(all(r["status"] == "planned" for r in rows[3:]))
        self.assertEqual(await self.store.kv_get("night.dreams", []), [])
        self.assertEqual((await dream_recall(self.p, _event=self.event()))["data"], [])

    async def test_precomputed_and_maximum(self):
        self.cfg._raw["night"].update(dream_mode="precomputed", dream_max_per_sleep=4)
        rows = await self.sleep()
        self.assertEqual(len(rows), 4)
        self.assertEqual(self.p.models.chat.await_count, 4)
        self.assertTrue(all(r["status"] == "generated" for r in rows))

    async def test_noon_forty_zero_hundred_has_rem(self):
        self.assertEqual(await self.sleep(40, "noon"), [])
        self.now += 86400
        rows = await self.sleep(100, "noon")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage_key"], "rem")

    async def test_live_keeps_existing_generation_path(self):
        self.cfg._raw["night"]["dream_mode"] = "live"
        self.assertEqual(await self.sleep(), [])
        self.now += 81 * 60
        await self.night.dream_tick()
        self.assertEqual(len(self.night.dreamer._dreams), 1)
        self.assertEqual(self.night.state.state.dream_count_today, 1)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 1)

    async def test_model_failure_keeps_planned_then_one_retry_failed(self):
        self.p.models.chat.side_effect = RuntimeError("model down")
        rows = await self.sleep()
        self.assertTrue(all(r["status"] == "planned" for r in rows))
        self.assertEqual([r["attempts"] for r in rows[:3]], [1, 1, 1])
        self.now = rows[0]["scheduled_at"]
        await self.night.dream_tick()
        rows = await self.rows()
        self.assertEqual((rows[0]["status"], rows[0]["attempts"]), ("failed", 2))
        count = self.p.models.chat.await_count
        await self.night.dream_tick()
        self.assertEqual(self.p.models.chat.await_count, count)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 0)

    async def test_hybrid_later_stage_live_and_context_cutoff(self):
        rows = await self.sleep()
        self.now = rows[3]["scheduled_at"]
        await self.night.dream_tick()
        fourth = (await self.rows())[3]
        self.assertEqual(fourth["status"], "generated")
        self.assertEqual(fourth["context_cutoff"], self.now)
        self.assertTrue(all(r["context_cutoff"] == T0 for r in rows[:3]))
        self.assertEqual(self.p.models.chat.await_count, 4)

    async def test_fragments_respect_cutoff(self):
        for ts, text in ((T0 - 1, "入睡前的雨"), (T0 + 1, "入睡后的秘密")):
            await self.store.execute("INSERT INTO mem_atom(persona_id,scope,text,created_at) VALUES(?,?,?,?)",
                                     (self.cfg.persona_id, "global", text, ts))
        before = await self.night.dreamer._pick_fragments(100, context_cutoff=T0)
        after = await self.night.dreamer._pick_fragments(100, context_cutoff=T0 + 2)
        self.assertTrue(any("雨" in t for t in before))
        self.assertFalse(any("秘密" in t for t in before))
        self.assertTrue(any("秘密" in t for t in after))

    async def test_duplicate_planning_and_persona_partition(self):
        rows = await self.sleep()
        await self.night.plans.plan_sleep()
        await self.drain()
        self.assertEqual(await self.rows(), rows)
        self.assertEqual(self.p.models.chat.await_count, 3)
        self.cfg._raw.setdefault("persona", {})["persona_id"] = "other"
        await self.night.plans.plan_sleep()
        await self.drain()
        other = (await self.rows())[len(rows):]
        self.assertEqual(len(other), len(rows))
        self.assertTrue(all(r["persona_id"] == "other" for r in other))
        self.assertNotEqual(other[0]["plan_key"], rows[0]["plan_key"])

    async def test_jitter_bounds_and_indexes(self):
        rows = await self.sleep()
        for row in rows:
            self.assertLessEqual(row["stage_start"], row["scheduled_at"])
            self.assertLessEqual(row["scheduled_at"] - row["stage_start"],
                                 min(120, (row["stage_end"] - row["stage_start"]) // 3))
        index = await self.store.fetch_all("PRAGMA index_info(idx_dream_plan_due)")
        self.assertEqual([r["name"] for r in index], ["persona_id", "status", "scheduled_at"])
        with self.assertRaises(sqlite3.IntegrityError):
            await self.store.execute(
                "INSERT INTO night_dream_plan(persona_id,plan_key,stage_index) VALUES(?,?,?)",
                (rows[0]["persona_id"], rows[0]["plan_key"], rows[0]["stage_index"]))

    async def test_early_wake_expires_unexperienced_stages(self):
        await self.sleep()
        self.now += 600
        await self.night.state._do_wake(reason="message")
        self.assertTrue(all(r["status"] == "expired" for r in await self.rows()))
        self.assertEqual(await self.night.deliver_dream_plans(), 0)

    async def test_terminate_cancels_generation_before_store_close(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def blocked(*a, **kw):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.assertTrue(self.store.ready)
                cancelled.set()
                raise
        self.p.models.chat.side_effect = blocked
        await self.sleep(drain=False)
        await started.wait()
        self.assertTrue(self.p._background_tasks)
        await base.ConglingPlugin.terminate(self.p)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.store.ready)
        self.assertFalse(self.p._background_tasks)
        self.assertFalse(self.night.plans._tasks)
        await self.store.open()
        self.assertEqual((await self.rows())[0]["status"], "planned")


class Delivery(Fixture):
    async def test_cancel_during_sqlite_commit_then_shutdown_keeps_delivered_dream(self):
        rows = await self.sleep(100, "noon")
        self.now = rows[0]["scheduled_at"]
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        transaction = self.store.transaction
        async def delayed(operation):
            def write(conn):
                result = operation(conn)
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise AssertionError("test barrier timeout")
                return result
            return await transaction(write)
        with patch.object(self.store, "transaction", side_effect=delayed):
            task = self.p.spawn_background(self.night.deliver_dream_plans())
            await asyncio.wait_for(entered.wait(), timeout=3)
            task.cancel()  # SQLite 线程已写入但尚未 commit，取消 asyncio 等待。
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await base.ConglingPlugin.terminate(self.p)
        await self.store.open()
        self.assertEqual((await self.rows())[0]["status"], "delivered")
        self.assertEqual(len(await self.store.kv_get("night.dreams", [])), 1)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 1)

    async def test_restart_after_state_already_awake_keeps_count(self):
        rows = await self.sleep(100, "noon")
        self.now = rows[0]["wake_at"]
        await self.night.state._do_wake(reason="natural")
        self.assertEqual(self.night.state.state.sleep_at, 0)
        await self.restart(self.now + 600)
        self.assertEqual(self.night.state.state.dream_count_today, 1)
        self.assertEqual((await self.rows())[0]["status"], "delivered")

    async def test_model_call_crossing_backfill_deadline_expires(self):
        self.p.models.chat.side_effect = RuntimeError("first failed")
        rows = await self.sleep(100, "noon")
        self.now = rows[0]["wake_at"] + 86400 - 1
        async def slow(*a, **kw):
            self.now += 2
            return '{"text":"不应交付的梦","level":5}'
        self.p.models.chat.side_effect = slow
        await self.night.reconcile_dream_plans()
        self.assertEqual((await self.rows())[0]["status"], "expired")
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 0)

    async def test_online_one_per_round_and_atomic_outputs(self):
        rows = await self.sleep()
        self.now = rows[2]["scheduled_at"]
        self.assertEqual(await self.night.deliver_dream_plans(), 1)
        self.assertEqual(await self.night.deliver_dream_plans(), 1)
        self.assertEqual(len(self.night.dreamer._dreams), 2)
        self.assertEqual(self.night.state.state.dream_count_today, 2)
        self.assertEqual([r["status"] for r in await self.rows()][:3], ["delivered", "delivered", "generated"])
        events = await self.store.fetch_all("SELECT * FROM event_ledger")
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["status"] == "succeeded" for e in events))
        self.assertEqual(events[0]["occurred_at"], rows[0]["stage_start"])
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom WHERE event_type='dream'"), 2)

    async def test_transaction_failure_rolls_back_all_outputs(self):
        rows = await self.sleep()
        self.now = rows[0]["scheduled_at"]
        await self.store.execute("CREATE TRIGGER fail_delivery BEFORE UPDATE OF status ON night_dream_plan "
                                 "WHEN NEW.status='delivered' BEGIN SELECT RAISE(ABORT,'fault'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.night.deliver_dream_plans()
        self.assertEqual((await self.rows())[0]["status"], "generated")
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 0)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom"), 0)
        self.assertEqual(await self.store.kv_get("night.dreams", []), [])
        await self.store.execute("DROP TRIGGER fail_delivery")
        self.assertEqual(await self.night.deliver_dream_plans(), 1)

    async def test_concurrent_delivery_is_idempotent(self):
        rows = await self.sleep()
        self.now = rows[0]["scheduled_at"]
        counts = await asyncio.gather(*(self.night.deliver_dream_plans() for _ in range(5)))
        self.assertEqual(sum(counts), 1)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 1)
        self.assertEqual(self.night.state.state.dream_count_today, 1)

    async def test_shutdown_three_hours_crosses_wake_and_five_restarts(self):
        # 100 分钟午睡，入睡 10 分钟断电，再离线 3 小时；明确跨过 wake_at。
        rows = await self.sleep(100, "noon")
        off_at = T0 + 10 * 60
        restart_at = off_at + 3 * 3600
        self.now = off_at
        for _ in range(5):
            await self.restart(restart_at)
        delivered = await self.rows()
        self.assertEqual(delivered[0]["status"], "delivered")
        self.assertEqual(delivered[0]["backfilled"], 1)
        self.assertEqual(delivered[0]["delivered_at"], rows[0]["wake_at"])
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 1)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom WHERE event_type='dream'"), 1)
        self.assertEqual(len(self.night.dreamer._dreams), 1)
        self.assertEqual(self.night.state.state.dream_count_today, 1)
        self.assertTrue(self.night.dreamer._dreams[0].backfilled)

    async def test_shutdown_three_hours_still_sleeping_keeps_until_loop(self):
        # 本用例验证睡中排产投递；避免新引入的满精力自然醒提前结束这一觉。
        self.cfg.override("night.energy_recover_per_minute", 0)
        self.night.state.state.energy = 10
        rows = await self.sleep()
        off_at = T0 + 600
        await self.restart(off_at + 3 * 3600)
        self.assertEqual(self.night.state.state.mode, "asleep")
        self.assertTrue(all(r["status"] == "generated" for r in (await self.rows())[:3]))
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 0)
        self.assertEqual(await self.night.deliver_dream_plans(), 1)
        self.assertFalse(self.night.dreamer._dreams[0].backfilled)

    async def test_shutdown_three_days_expires_generated_and_planned(self):
        rows = await self.sleep()
        self.assertTrue(any(r["status"] == "planned" for r in rows))
        off_at = T0 + 600
        await self.restart(off_at + 3 * 86400)
        self.assertTrue(all(r["status"] == "expired" for r in await self.rows()))
        self.assertEqual(await self.store.kv_get("night.dreams", []), [])
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 0)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom"), 0)
        self.assertEqual(self.p.models.chat.await_count, 3)

    async def test_overnight_backfills_three_not_offline_live_stages(self):
        rows = await self.sleep()
        await self.restart(rows[0]["wake_at"] + 1800)
        rows = await self.rows()
        self.assertEqual([r["status"] for r in rows[:3]], ["delivered"] * 3)
        self.assertTrue(all(r["backfilled"] for r in rows[:3]))
        self.assertTrue(all(r["status"] == "expired" for r in rows[3:]))
        self.assertEqual(self.p.models.chat.await_count, 3)

    async def test_pending_generation_recovers_after_process_killed(self):
        started = asyncio.Event()
        async def blocked(*a, **kw):
            started.set()
            await asyncio.Event().wait()
        self.p.models.chat.side_effect = blocked
        rows = await self.sleep(100, "noon", drain=False)
        await started.wait()
        self.assertEqual((await self.rows())[0]["status"], "planned")
        self.p.models.chat.side_effect = None
        await self.restart(T0 + 600 + 3 * 3600)
        self.assertEqual((await self.rows())[0]["status"], "delivered")
        self.assertEqual((await self.rows())[0]["attempts"], 2)
        self.assertEqual(len(self.night.dreamer._dreams), 1)

    async def test_exact_twenty_four_hour_boundary(self):
        rows = await self.sleep(100, "noon")
        await self.restart(rows[0]["wake_at"] + 86400)
        self.assertEqual((await self.rows())[0]["status"], "delivered")

    async def test_twenty_four_hours_plus_one_expires(self):
        rows = await self.sleep(100, "noon")
        await self.restart(rows[0]["wake_at"] + 86401)
        self.assertEqual((await self.rows())[0]["status"], "expired")
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM event_ledger"), 0)

    async def test_backfill_decay_uses_stage_start_and_no_double_decay(self):
        rows = await self.sleep(100, "noon")
        at = rows[0]["wake_at"] + 23 * 3600
        await self.restart(at)
        dream = self.night.dreamer._dreams[0]
        expected = (1 - LEVEL_DECAY[5]) ** ((at - rows[0]["stage_start"]) / 86400)
        self.assertEqual(dream.created_at, rows[0]["stage_start"])
        self.assertAlmostEqual(dream.strength, expected, places=4)
        fresh = DreamEntry(level=5, decay_per_day=LEVEL_DECAY[5])
        self.assertLess(dream.remaining_days(), fresh.remaining_days())
        before = dream.strength
        await self.night.dreamer.daily_tick()
        self.assertAlmostEqual(dream.strength, before, places=7)
        await self.restart(at)
        self.assertAlmostEqual(self.night.dreamer._dreams[0].strength, expected, places=4)

    async def test_disabled_generation_does_not_backfill_but_still_expires(self):
        rows = await self.sleep(100, "noon")
        self.cfg._raw["night"]["dream_enabled"] = False
        await self.restart(rows[0]["wake_at"] + 10)
        self.assertEqual((await self.rows())[0]["status"], "generated")
        await self.restart(rows[0]["wake_at"] + 86401)
        self.assertEqual((await self.rows())[0]["status"], "expired")

    async def test_backfill_has_no_per_dream_messages(self):
        rows = await self.sleep()
        self.p.context.send_message = AsyncMock()
        self.night._pick_morning_dream = AsyncMock()
        self.now = rows[0]["wake_at"] + 10
        await self.night.reconcile_dream_plans()
        self.p.context.send_message.assert_not_called()
        self.night._pick_morning_dream.assert_not_called()


class MigrationAndAPI(Fixture):
    async def test_pinned_time_is_not_charged_when_unpinned(self):
        self.set_dreams([5])
        dream = self.night.dreamer._dreams[0]
        self.night.dreamer.set_pinned(dream.dream_id, True)
        strength = dream.strength
        self.now += 3 * 86400
        self.night.dreamer.set_pinned(dream.dream_id, False)
        self.assertEqual(dream.strength, strength)
        dream.age_to(self.now + 86400)
        self.assertAlmostEqual(dream.strength, strength * (1 - dream.decay_per_day))

    async def test_v7_without_plan_table_and_partial_structure_repaired(self):
        await self.store.close()
        with sqlite3.connect(self.store.path) as conn:
            conn.execute("DROP TABLE night_dream_plan")
            conn.execute("PRAGMA user_version=7")
        await self.store.open()
        self.assertEqual(await self.store.scalar("PRAGMA user_version"), 7)
        await self.store.close()
        with sqlite3.connect(self.store.path) as conn:
            conn.execute("DROP TABLE night_dream_plan")
            conn.execute("CREATE TABLE night_dream_plan(id INTEGER PRIMARY KEY, persona_id TEXT, "
                         "plan_key TEXT, stage_index INTEGER)")
            conn.execute("INSERT INTO night_dream_plan VALUES(1,'ling','old',1)")
            conn.execute("PRAGMA user_version=77")
        await self.store.open()
        self.assertEqual(await self.store.scalar("PRAGMA user_version"), 77)
        fields = {r["name"] for r in await self.store.fetch_all("PRAGMA table_info(night_dream_plan)")}
        self.assertTrue({"stage_start", "wake_at", "dream_json", "scheduled_at", "attempts"} <= fields)
        self.assertEqual((await self.rows())[0]["plan_key"], "old")
        before = await self.rows()
        await self.store.close()
        await self.store.open()
        self.assertEqual(await self.rows(), before)

    async def test_plan_migration_failure_rolls_back(self):
        await self.store.close()
        with sqlite3.connect(self.store.path) as conn:
            conn.execute("DROP TABLE night_dream_plan")
            conn.execute("PRAGMA user_version=7")
        migrate = self.store._migrate_dream_plans
        def fail():
            migrate()
            raise RuntimeError("DDL fault")
        # 同上：失败要可恢复，不要求整体回滚。
        with patch.object(self.store, "_migrate_dream_plans", side_effect=fail):
            await self.store.open()      # 不抛异常：降级启动
        self.assertTrue(self.store.ready)
        await self.store.close()
        await self.store.open()          # 重开补齐
        with sqlite3.connect(self.store.path) as conn:
            self.assertIsNotNone(
                conn.execute("SELECT name FROM sqlite_master WHERE name='night_dream_plan'").fetchone()
            )

    async def test_fields_backward_compatible_and_api_roundtrip(self):
        old = DreamEntry.from_dict({"dream_id": "old"})
        self.assertEqual((old.planned_at, old.stage_key, old.cycle, old.backfilled), (0, "", 0, False))
        rows = await self.sleep(100, "noon")
        await self.restart(rows[0]["wake_at"] + 1800)
        import sys
        if "astrbot.api.web" not in sys.modules:
            base.module("astrbot.api.web").request = types.SimpleNamespace()
        from astrbot_plugin_congling.page_api import PageApi
        api = PageApi(self.p)
        with patch("astrbot_plugin_congling.page_api._params", AsyncMock(return_value={})):
            result = await api.night_dreams()
        item = result["data"]["items"][0]
        self.assertEqual(item["planned_at"], T0)
        self.assertEqual((item["stage_key"], item["cycle"], item["backfilled"]), ("rem", 1, True))
        self.assertEqual(item["created_at"], rows[0]["stage_start"])
        self.assertIn("remaining_days", item)

    async def test_config_defaults_and_no_version_bump(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        items = schema["night"]["items"]
        self.assertEqual(items["dream_mode"]["default"], "hybrid")
        self.assertEqual(items["dream_mention_min_level"]["default"], 4)
        self.assertEqual(items["dream_hint_scope"]["default"], "session")
        self.assertFalse(items["dream_recall_guard"]["default"])
        for key in ("dream_mode", "dream_mention_min_level", "dream_hint_scope",
                    "dream_recall_guard", "dream_recall_k"):
            self.assertEqual(base.Config({}).get(f"night.{key}"), items[key]["default"])
        from astrbot_plugin_congling.core.store import SCHEMA_VERSION
        self.assertEqual(SCHEMA_VERSION, 6)

    async def test_static_prompt_rule_and_production_call_order(self):
        from astrbot_plugin_congling.humanize.persona import DREAM_RECALL_RULE, PersonaService
        persona = PersonaService(self.p)
        prefixes = []
        for levels in ([3], [5]):
            self.set_dreams(levels)
            req = types.SimpleNamespace(system_prompt="STATIC", extra_user_content_parts=[])
            await persona.inject(self.event(), req)
            prefixes.append(req.system_prompt)
        self.assertEqual(prefixes[0], prefixes[1])
        self.assertIn(DREAM_RECALL_RULE, prefixes[0])
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
        loop = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_dream_loop")
        calls = [n.func.attr for n in ast.walk(loop) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        self.assertLess(calls.index("deliver_dream_plans"), calls.index("dream_tick"))
