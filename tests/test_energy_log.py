"""精力流水与状态一致性测试，使用临时 SQLite，不调用模型或聊天平台。"""
import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import night_selftest as bootstrap

state_mod = bootstrap._prepare()
Store = importlib.import_module("cg.core.store").Store
energy = importlib.import_module("cg.night.energy")
contracts = importlib.import_module("cg.core.contracts")
idle_mod = importlib.import_module("cg.night.wake_idle")


class EnergyLogTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")
        await self.store.open()
        self.cfg = bootstrap.base_cfg(**{
            "night.night_fatigue": "", "night.noon_fatigue": "",
            "night.latest_sleep_time": "", "night.energy_per_message": 2.0,
            "night.energy_recover_per_minute": 3.0,
        })
        self.service = state_mod.NightService(SimpleNamespace(cfg=self.cfg, store=self.store, models=None))
        self.service.state.last_tick = int(time.time())
        self.msg = SimpleNamespace(session_id="test-session", msg_id="m1", session_kind="group",
                                   text="测试", is_at_me=False, reply_to_msg_id="", sender_id="u1")
        if self._testMethodName.startswith("test_idle_"):
            # 静默测试的前提是本次被叫醒且没睡够；满格自然醒另有反例测试。
            self.service.state.last_wake_reason = "message"
            self.service.state.energy = 40
            self.service.plugin.night = SimpleNamespace(state=self.service)

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    async def rows(self):
        return (await self.service.events.list())["items"]

    async def test_message_records_actual_cost_and_identity(self):
        result = await self.service.on_message(self.msg)
        self.assertFalse(result["block"])
        rows = [r for r in await self.rows() if r["event"] == "message_drain"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delta"], -2)
        self.assertEqual(rows[0]["energy_before"], 100)
        self.assertEqual(rows[0]["energy_after"], self.service.state.energy)
        self.assertEqual(rows[0]["msg_id"], "m1")

    async def test_time_and_message_changes_reconcile(self):
        self.service.state.energy = 40
        self.service.state.last_tick = int(time.time()) - 60
        await self.service.on_message(self.msg)
        rows = list(reversed(await self.rows()))
        self.assertEqual([r["event"] for r in rows], ["awake_recover", "message_drain"])
        self.assertAlmostEqual(40 + sum(r["delta"] for r in rows), self.service.state.energy)
        self.assertEqual(rows[0]["energy_after"], rows[1]["energy_before"])

    async def test_recovery_at_cap_does_not_fill_log(self):
        now = time.time()
        self.service.state.last_tick = int(now) - 600
        await self.service.tick(now)
        self.assertEqual(await self.rows(), [])

    async def test_sleep_and_wake_are_not_energy_exhaustion(self):
        self.service.state.energy = 93.26
        await self.service.sleep_now(reason="talk", kind="noon")
        self.assertEqual(self.service.snapshot()["sleep_reason_label"], "结束语收线")
        sleep = (await self.rows())[0]
        self.assertEqual(sleep["reason"], "talk")
        self.assertEqual(sleep["energy_after"], 93.26)
        self.assertEqual(sleep["delta"], 0)
        await self.service.wake_now(reason="manual")
        wake = (await self.rows())[0]
        self.assertEqual(wake["event"], "wake")
        self.assertEqual(wake["energy_after"], 35)

    async def test_sleep_recovery_and_natural_wake(self):
        await self.service.sleep_now(kind="noon")
        self.service.state.energy = 10
        self.service.state.last_tick = int(time.time()) - 60
        await self.service.tick()
        self.assertTrue(any(r["event"] == "sleep_recover" for r in await self.rows()))
        await self.service._do_wake(reason="natural")
        self.assertEqual(self.service.state.energy, 100)
        self.assertEqual((await self.rows())[0]["reason"], "natural")

    async def test_sleeping_message_not_charged(self):
        self.service.state.energy = 50
        await self.service.sleep_now(kind="noon")
        self.service.waker.check = lambda *a, **k: contracts.WakeAttempt(reason="未被点名")
        result = await self.service.on_message(self.msg)
        self.assertTrue(result["block"])
        self.assertEqual((await self.rows())[0]["event"], "wake_check")
        self.assertFalse(any(r["event"] == "message_drain" for r in await self.rows()))

    async def test_fatigue_guard_is_logged_and_reconciles(self):
        self.service.state.energy = 1
        with patch.object(energy, "fatigue_kind", return_value="night"), patch.object(energy, "in_fatigue", return_value=True):
            await self.service.on_message(self.msg)
        rows = list(reversed(await self.rows()))
        self.assertTrue(any(r["event"] == "sleep_guard" for r in rows))
        self.assertEqual(self.service.state.energy, 0.5)
        self.assertAlmostEqual(1 + sum(r["delta"] for r in rows), 0.5)
        self.assertEqual(self.service.state.mode, "awake")

    async def test_switch_records_prevented_sleep(self):
        self.cfg.d["night.allow_sleep"] = False
        self.service.state.energy = 0
        await self.service.sleep_now(reason="energy")
        self.assertEqual(self.service.state.mode, "awake")
        self.assertEqual((await self.rows())[0]["event"], "sleep_skipped")

    async def test_concurrent_append_and_stable_pagination(self):
        await asyncio.gather(*(self.service.events.add("message_drain", 100, self.service.state, msg_id=str(i)) for i in range(40)))
        first = await self.service.events.list(limit=15, category="drain")
        second = await self.service.events.list(limit=15, before_id=first["next_before_id"], category="drain")
        self.assertTrue(first["has_more"])
        self.assertEqual(len(first["items"]), 15)
        self.assertFalse(set(r["id"] for r in first["items"]) & set(r["id"] for r in second["items"]))
        self.assertEqual((await self.service.events.list(category="recover"))["items"], [])

    async def test_database_failure_does_not_block_reply(self):
        self.store.execute = unittest.mock.AsyncMock(side_effect=RuntimeError("模拟写失败"))
        result = await self.service.on_message(self.msg)
        self.assertFalse(result["block"])
        self.assertEqual(self.service.state.energy, 98)

    async def test_retention_only_removes_observation_logs(self):
        await self.service.events.add("snapshot", 100, self.service.state)
        await self.store.execute("UPDATE night_energy_log SET ts=?", (time.time() - 8 * 86400,))
        await self.store.kv_set("unrelated", {"preserved": True})
        self.service.events._last_prune = 0
        await self.service.events.add("snapshot", 100, self.service.state)
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(await self.store.kv_get("unrelated"), {"preserved": True})

    async def test_read_does_not_change_sleep_or_energy(self):
        await self.service.sleep_now(reason="talk", kind="noon")
        before = self.service.state.to_dict()
        await self.service.events.list(since=time.time() - 3600)
        self.assertEqual(before, self.service.state.to_dict())

    async def test_full_energy_wakes_before_scheduled_time(self):
        self.service.state.energy = 99
        await self.service.sleep_now(reason="talk", kind="noon")
        original_wake = self.service.state.wake_at
        self.service.state.last_tick = int(time.time()) - 60
        result = await self.service.tick()
        self.assertIn("wake_energy_full", result)
        self.assertEqual(self.service.state.mode, "awake")
        self.assertEqual(self.service.state.energy, 100)
        self.assertFalse(self.service._forced_wake)
        row = (await self.rows())[0]
        self.assertEqual(row["reason"], "energy_full")
        self.assertEqual(row["reason_label"], "精力满格自动醒")
        windows = await self.store.kv_get(state_mod.SLEEP_WINDOW_KEY)
        self.assertLess(windows[-1]["wake_at"], original_wake)
        self.assertLessEqual(windows[-1]["wake_at"], int(time.time()))

    async def test_already_full_checks_even_with_no_elapsed_time(self):
        await self.service.sleep_now(reason="manual", kind="noon")
        now = self.service.state.last_tick
        self.assertIn("wake_energy_full", await self.service.tick(now))
        self.assertEqual(self.service.state.mode, "awake")

    async def test_first_heartbeat_wakes_full_energy(self):
        await self.service.sleep_now(reason="talk", kind="noon")
        self.service.state.last_tick = 0
        self.assertIn("wake_energy_full", await self.service.tick())
        self.assertEqual(self.service.state.mode, "awake")

    async def test_under_cap_does_not_wake_from_display_rounding(self):
        self.service.state.energy = 99.96
        await self.service.sleep_now(kind="noon")
        now = self.service.state.last_tick
        self.assertEqual(await self.service.tick(now), [])
        self.assertEqual(self.service.state.mode, "asleep")

    async def test_custom_cap_and_crawl_preserve_full_energy(self):
        self.cfg.d["night.energy_max"] = 80
        self.cfg.d["night.crawl_enabled"] = True
        self.service.state.energy = 80
        await self.service.sleep_now(kind="noon")
        self.service._pending_sessions = unittest.mock.AsyncMock(return_value=["test-session"])
        self.assertIn("wake_energy_full", await self.service.tick(self.service.state.last_tick))
        self.assertEqual(self.service.state.mode, "waking")
        self.assertEqual(self.service.state.energy, 80)
        await self.service.finish_waking()
        self.assertEqual(self.service.state.mode, "awake")
        self.assertEqual(self.service.state.energy, 80)

    async def test_scheduled_time_still_wakes_without_full_energy(self):
        self.service.state.energy = 20
        await self.service.sleep_now(kind="noon")
        self.cfg.d["night.energy_recover_per_minute"] = 0
        self.service.state.wake_at = int(time.time()) - 1
        self.assertIn("wake_natural", await self.service.tick())
        self.assertEqual((await self.rows())[0]["reason"], "natural")
        self.assertEqual(self.service.state.energy, 100)

    async def test_disabled_night_does_not_use_energy_wake(self):
        await self.service.sleep_now(kind="noon")
        self.cfg.d["night.enabled"] = False
        self.assertEqual(await self.service.tick(), [])
        self.assertEqual(self.service.state.mode, "asleep")

    async def test_waking_timeout_obeys_config_and_preserves_energy(self):
        self.cfg.d["night.waking_timeout_minutes"] = 2
        now = int(time.time())
        self.service.state.mode = "waking"
        self.service.state.enter_waking_at = now - 119
        self.service.state.pending_crawl = ["test-session"]
        self.service.state.energy = 50
        self.assertNotIn("wake_timeout", await self.service.tick(now))
        self.assertEqual(self.service.state.mode, "waking")
        self.assertIn("wake_timeout", await self.service.tick(now + 1))
        self.assertEqual(self.service.state.mode, "awake")
        self.assertGreaterEqual(self.service.state.energy, 50)
        self.assertEqual(self.service.snapshot()["waking_timeout_minutes"], 2)

    async def test_timeout_defaults_and_minimum(self):
        self.assertEqual(self.service.snapshot()["waking_timeout_minutes"], 5)
        self.cfg.d["night.waking_timeout_minutes"] = 0
        self.assertEqual(self.service.snapshot()["waking_timeout_minutes"], 1)

    async def test_idle_fires_once_across_heartbeats_and_reload(self):
        idle = idle_mod.WakeIdle(self.service.plugin)
        await self.store.kv_set(f"{idle_mod.WAKE_MARK_KEY}:s", int(time.time()) - 601)
        self.assertTrue((await idle.claim("s")).trigger)
        self.assertFalse((await idle.claim("s")).trigger)
        reloaded = idle_mod.WakeIdle(self.service.plugin)
        self.assertFalse((await reloaded.claim("s")).trigger)

    async def test_idle_concurrent_claim_only_one_triggers(self):
        idle = idle_mod.WakeIdle(self.service.plugin)
        await self.store.kv_set(f"{idle_mod.WAKE_MARK_KEY}:s", int(time.time()) - 601)
        results = await asyncio.gather(*(idle.claim("s") for _ in range(10)))
        self.assertEqual(sum(r.trigger for r in results), 1)

    async def test_idle_new_message_rearms_after_full_wait(self):
        idle = idle_mod.WakeIdle(self.service.plugin)
        now = int(time.time())
        await self.store.kv_set(f"{idle_mod.WAKE_MARK_KEY}:s", now - 601)
        self.assertTrue((await idle.claim("s")).trigger)
        await idle.note_message("s")
        self.assertFalse((await idle.claim("s")).trigger)
        with patch.object(idle_mod.time, "time", return_value=now + 601):
            self.assertTrue((await idle.claim("s")).trigger)
            self.assertFalse((await idle.claim("s")).trigger)

    async def test_idle_new_wake_ignores_previous_sleep_message(self):
        idle = idle_mod.WakeIdle(self.service.plugin)
        now = int(time.time())
        await self.store.kv_set(f"{idle_mod.IDLE_MARK_KEY}:s", now - 900)
        await idle.mark_wake("s")
        self.assertFalse((await idle.claim("s")).trigger)
        with patch.object(idle_mod.time, "time", return_value=now + 601):
            self.assertTrue((await idle.claim("s")).trigger)
        with patch.object(idle_mod.time, "time", return_value=now + 602):
            await idle.mark_wake("s")
            self.assertFalse((await idle.claim("s")).trigger)

    async def test_idle_disabled_by_zero_and_session_isolation(self):
        idle = idle_mod.WakeIdle(self.service.plugin)
        for sid in ("a", "b"):
            await self.store.kv_set(f"{idle_mod.WAKE_MARK_KEY}:{sid}", int(time.time()) - 601)
        self.cfg.d["night.rollup.wake_idle_minutes"] = 0
        self.assertFalse((await idle.claim("a")).trigger)
        self.cfg.d["night.rollup.wake_idle_minutes"] = 10
        self.assertTrue((await idle.claim("a")).trigger)
        self.assertTrue((await idle.claim("b")).trigger)
        self.assertFalse((await idle.claim("a")).trigger)

    async def test_idle_clear_removes_trigger_and_marks(self):
        idle = idle_mod.WakeIdle(self.service.plugin)
        await self.store.kv_set(f"{idle_mod.WAKE_MARK_KEY}:s", int(time.time()) - 601)
        self.assertTrue((await idle.claim("s")).trigger)
        await idle.clear("s")
        self.assertFalse((await idle.claim("s")).trigger)
        self.assertIsNone(await self.store.kv_get(f"{idle_mod.FIRED_KEY}:s"))

    async def test_idle_tick_generates_one_hint_without_sleeping(self):
        # 加载真实门面，检查心跳接线、日志和待发文案消费，而非仅检查计时器。
        module = bootstrap._load("cg.night", "night/__init__.py")
        self.cfg.d["night.rollup.enabled"] = True
        night = module.NightModule(self.service.plugin)
        night.state.state.last_wake_reason = "message"
        night.state.state.energy = 40
        self.service.plugin.night = night
        async def identity():
            return {"token": "test-role"}
        self.service.plugin.persona = SimpleNamespace(resolve=identity)
        await self.store.kv_set(f"{idle_mod.WAKE_MARK_KEY}:s", int(time.time()) - 601)
        await night.idle_tick()
        self.assertEqual(night.state.state.mode, "awake")
        self.assertTrue(night.take_sleepy_line("s", "test-role"))
        await night.idle_tick()
        self.assertEqual(night.take_sleepy_line("s"), "")
        self.assertEqual(night.state.state.mode, "awake")

    def make_closing_module(self):
        module = bootstrap._load("cg.night", "night/__init__.py")
        self.cfg.d.update({
            "close.enabled": True, "close.strong_enabled": False,
            "close.strong_words": ["晚安", "我睡了", "去睡了"],
            "close.medium_words": ["拜拜", "停", "闭嘴", "安静"],
        })
        return module.NightModule(self.service.plugin)

    async def test_historical_stop_description_does_not_close(self):
        night = self.make_closing_module()
        self.msg.text = "我的直接甩一句，不接这句话，停止"
        result = await night.on_message(self.msg)
        self.assertFalse(result.get("closing", False))
        self.assertIsNone(night.pending_sleep)
        self.assertNotIn("这句话说完你", night.prompt_hint(session_id="test-session", msg_id="m1"))
        await night.after_reply(self.msg)
        self.assertEqual(night.state.state.mode, "awake")

    async def test_disabled_strong_does_not_create_sleep_task(self):
        night = self.make_closing_module()
        self.msg.text = "那我睡了，晚安。"
        result = await night.on_message(self.msg)
        self.assertFalse(result.get("closing", False))
        self.assertIsNone(night.pending_sleep)
        row = (await night.state.events.list())["items"][0]
        self.assertEqual(row["reason"], "strong_off")

    async def test_direct_medium_command_still_allowed_with_strong_off(self):
        night = self.make_closing_module()
        self.msg.text = "停一下"
        self.assertTrue((await night.on_message(self.msg))["closing"])
        self.assertEqual(night.pending_sleep["strength"], "medium")
        self.assertEqual(night.pending_sleep["msg_id"], "m1")
        await night.after_reply(self.msg)
        self.assertEqual(night.state.state.mode, "asleep")

    async def test_continued_conversation_cancels_pending_before_prompt(self):
        night = self.make_closing_module()
        self.msg.text = "拜拜"
        await night.on_message(self.msg)
        self.msg.msg_id, self.msg.text = "m2", "因为看图我换成了其他模型啊"
        await night.on_message(self.msg)
        self.assertIsNone(night.pending_sleep)
        self.assertNotIn("这句话说完你", night.prompt_hint(session_id="test-session", msg_id="m2"))
        await night.after_reply(self.msg)
        self.assertEqual(night.state.state.mode, "awake")

    async def test_pending_expires_and_cannot_sleep_on_later_reply(self):
        night = self.make_closing_module()
        self.msg.text = "拜拜"
        await night.on_message(self.msg)
        night.pending_sleep["created_at"] = time.time() - 121
        self.assertNotIn("这句话说完你", night.prompt_hint(session_id="test-session", msg_id="m1"))
        await night.after_reply(self.msg)
        self.assertIsNone(night.pending_sleep)
        self.assertEqual(night.state.state.mode, "awake")

    async def test_other_session_cannot_use_farewell_or_consume_task(self):
        night = self.make_closing_module()
        self.msg.text = "拜拜"
        await night.on_message(self.msg)
        self.assertNotIn("这句话说完你", night.prompt_hint(session_id="other-session", msg_id="m2"))
        await night.after_reply(None, session_id="other-session", msg_id="m2")
        self.assertIsNotNone(night.pending_sleep)
        self.assertEqual(night.state.state.mode, "awake")
        await night.after_reply(self.msg)
        self.assertEqual(night.state.state.mode, "asleep")

    async def test_old_reply_in_same_session_cannot_consume_new_task(self):
        night = self.make_closing_module()
        self.msg.text = "拜拜"
        await night.on_message(self.msg)
        await night.after_reply(None, session_id="test-session", msg_id="earlier-message")
        self.assertIsNotNone(night.pending_sleep)
        self.assertEqual(night.state.state.mode, "awake")

    async def test_disable_strong_after_detection_blocks_pending(self):
        night = self.make_closing_module()
        self.cfg.d["close.strong_enabled"] = True
        self.msg.text = "我睡了，晚安"
        await night.on_message(self.msg)
        self.assertIsNotNone(night.pending_sleep)
        self.cfg.d["close.strong_enabled"] = False
        await night.after_reply(self.msg)
        self.assertIsNone(night.pending_sleep)
        self.assertEqual(night.state.state.mode, "awake")

    async def test_disable_closing_before_reply_cancels_task(self):
        night = self.make_closing_module()
        self.msg.text = "拜拜"
        await night.on_message(self.msg)
        self.cfg.d["close.enabled"] = False
        await night.after_reply(self.msg)
        self.assertIsNone(night.pending_sleep)
        self.assertEqual(night.state.state.mode, "awake")


if __name__ == "__main__":
    unittest.main()
