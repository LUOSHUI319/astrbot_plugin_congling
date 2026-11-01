"""Focused P0/P1 regressions. Run via run_round1_p0p1.py (no live services)."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_plugin_congling.tests import run_diagnosis_regressions as base
from astrbot_plugin_congling.tests.check_encoding import scan, SUSPECT
from astrbot_plugin_congling.core.contracts import MemoryHit, NightState, PerceivedMessage, RhythmPlan
from astrbot_plugin_congling.core.rhythm import RhythmController
from astrbot_plugin_congling.core.transient_hints import TransientHints
from astrbot_plugin_congling.humanize.persona import PersonaService
from astrbot_plugin_congling.night import NightModule

ROOT = Path(__file__).resolve().parents[1]
NOW = 1793500000


class Round1(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="congling-round1-")
        self.store = base.Store(Path(self.tmp.name) / "test.sqlite")
        await self.store.open()
        self.cfg = base.Config({"night": {"enabled": True, "crawl_enabled": False},
                                "memory": {"cross_session_recall": False}})
        self.p = base.ConglingPlugin.__new__(base.ConglingPlugin)
        self.p.cfg, self.p.store = self.cfg, self.store
        self.p._background_tasks = set()
        self.p._stopping = False
        self.p.bus = base.EventBus(spawn_background=self.p.spawn_background)
        self.p.budget = base.Budget(self.cfg)
        self.p.context = types.SimpleNamespace(send_message=AsyncMock())
        self.p.probe = types.SimpleNamespace(should_yield=lambda _: False, matrix=lambda: {})
        self.p.is_admin = lambda _: False
        self.p.enabled_for = lambda _: True
        self.p._started_at, self.p._boot_errors, self.p.config_upgrade = NOW, [], {}
        for name in ("night", "persona", "models", "diary", "qqext", "perception",
                     "capabilities", "page_api", "diag", "proactive", "composer"):
            setattr(self.p, name, None)
        self.mem = self.p.memory = base.MemoryService(self.p)

    async def asyncTearDown(self):
        tasks = list(self.p._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.close()
        self.tmp.cleanup()

    @unittest.skip("P5 已把人格改为跟随 AstrBot：不再有 builtin 改名/分区模型。等价断言见 tests/test_round5_p5.py 的 Persona 类")
    async def test_persona_rename_preserves_id_partition_and_memory_count(self):
        persona = PersonaService(self.p)
        old_id, key = persona.get().persona_id, persona.key
        await self.mem.record_self_event("diary", "keep", "测试记忆")
        before = await self.store.scalar("SELECT COUNT(*) FROM mem_atom WHERE persona_id=?", (old_id,))
        result = await persona.save({"name": "新名字"})
        self.assertEqual(result["persona_id"], old_id)
        self.assertEqual(persona.key, key)
        self.assertEqual((await self.store.kv_get(key))["name"], "新名字")
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom WHERE persona_id=?", (old_id,)), before)
        self.assertGreater(before, 0)
        restored = PersonaService(self.p)
        await restored.load()
        self.assertEqual(restored.get().persona_id, old_id)
        self.assertEqual(restored.get().name, "新名字")

    async def test_persona_same_id_backward_compatible(self):
        persona = PersonaService(self.p)
        result = await persona.save({"persona_id": persona.get().persona_id, "name": "同库改名"})
        self.assertEqual(result["name"], "同库改名")
        self.assertEqual(len(await persona.versions()), 1)

    async def test_persona_different_or_empty_id_rejected_atomically(self):
        persona = PersonaService(self.p)
        before = persona.as_dict()
        for value in ("other", "", None, 0, {}, []):
            with self.subTest(value=value), patch.object(self.store, "kv_set", new_callable=AsyncMock) as write:
                with self.assertLogs("congling-offline-test", level="WARNING") as logs:
                    with self.assertRaisesRegex(ValueError, "persona_id 不可修改"):
                        await persona.save({"persona_id": value, "name": "不能落盘"})
                self.assertIn("拒绝修改 persona_id", "".join(logs.output))
                self.assertEqual(persona.as_dict(), before)
                write.assert_not_awaited()
        self.assertEqual(await persona.versions(), [])

    def page_api(self):
        web = types.ModuleType("astrbot.api.web")
        web.request = types.SimpleNamespace()
        with patch.dict("sys.modules", {"astrbot.api.web": web}):
            module = importlib.import_module("astrbot_plugin_congling.page_api")
        api = module.PageApi.__new__(module.PageApi)
        api.plugin = self.p
        return module, api

    @unittest.skip("P5 已把人格改为跟随 AstrBot：不再有 builtin 改名/分区模型。等价断言见 tests/test_round5_p5.py 的 Persona 类")
    async def test_persona_api_returns_error_without_saving_mode(self):
        self.p.persona = PersonaService(self.p)
        module, api = self.page_api()
        api._persist = AsyncMock()
        with patch.object(module, "_body", new=AsyncMock(return_value={
                "persona_id": "other", "mode": "none", "astrbot_persona_id": "other"})), \
                self.assertLogs("congling-offline-test", level="WARNING"):
            result = await api.save_persona()
        self.assertFalse(result["ok"])
        self.assertIn("persona_id 不可修改", result["error"])
        api._persist.assert_not_awaited()

    async def test_scheduler_owned_and_shutdown_waits_before_flush_and_close(self):
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        async def sweep():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                order.append("cancel")
                await release.wait()
                order.append("gather")
        self.mem._sweep = sweep
        await self.mem.start()
        await entered.wait()
        self.assertIn(self.mem._task, self.p._background_tasks)
        task = self.mem._task
        original_flush = self.mem.flush
        async def flush():
            order.append("flush")
            self.assertTrue(task.done())
            await original_flush()
        async def close():
            order.append("close")
        self.mem.flush = flush
        self.p.store = types.SimpleNamespace(close=close)
        shutdown = asyncio.create_task(self.p.terminate())
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertNotIn("flush", order)
            release.set()
            await shutdown
        finally:
            release.set()
            await asyncio.gather(shutdown, return_exceptions=True)
        self.assertEqual(order, ["cancel", "gather", "flush", "close"])
        self.assertFalse(self.p._background_tasks)
        self.assertEqual(self.p.bus.subscriber_count(self.p.bus.ON_PERCEIVED), 0)
        with patch.object(self.store, "execute", new_callable=AsyncMock) as query:
            # Fast-forward both scheduler periods: no surviving coroutine may query.
            with patch("astrbot_plugin_congling.memory.service.time.time", return_value=NOW + 120):
                for _ in range(3):
                    await asyncio.sleep(0)
            query.assert_not_awaited()
        with self.assertRaises(RuntimeError):
            await self.mem.start()
        self.assertEqual(self.p.bus.subscriber_count(self.p.bus.ON_PERCEIVED), 0)

    async def test_diary_backfill_is_owned_and_cancelled(self):
        entered = asyncio.Event()
        async def wait():
            entered.set()
            await asyncio.Event().wait()
        with patch.object(base.Diary, "backfill", new=wait_for_self(wait)):
            await self.p._boot_diary()
            await entered.wait()
            tasks = list(self.p._background_tasks)
            self.assertEqual(len(tasks), 1)
            await self.p.terminate()
            self.assertTrue(tasks[0].cancelled())

    async def test_proactive_send_child_is_owned_and_stopping_blocks_spawn(self):
        self.cfg._raw["proactive"] = {"enabled": True}
        proactive = base.Proactive(self.p)
        proactive._boot_at = 0
        proactive.load = AsyncMock()
        proactive.settle_replies = AsyncMock()
        proactive.candidates = AsyncMock(return_value=[{"session_id": "A"}])
        proactive._count_unanswered = AsyncMock(return_value=0)
        proactive.should = AsyncMock(return_value=(True, "test", 80))
        entered = asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        proactive.send_one = send
        await proactive.tick(now=NOW)
        await entered.wait()
        tasks = list(self.p._background_tasks)
        self.assertEqual(len(tasks), 1)
        await self.p.terminate()
        self.assertTrue(tasks[0].cancelled())
        proactive.candidates.reset_mock()
        await proactive.tick(now=NOW + 61)
        proactive.candidates.assert_not_awaited()

    async def test_bus_and_watchdog_follow_plugin_lifecycle(self):
        started = asyncio.Event()
        async def handler(_):
            started.set()
            await asyncio.Event().wait()
        self.p.bus.subscribe("owned", handler)
        self.p.bus.emit_soon("owned")
        await started.wait()
        rhythm = RhythmController(self.cfg, None, None, spawn_background=self.p.spawn_background)
        sender = AsyncMock()
        event = types.SimpleNamespace(send=sender)
        with patch.object(rhythm, "_make_chain", side_effect=lambda parts: parts):
            self.assertTrue(rhythm.attach(event, RhythmPlan(bubbles=["离线"])))
        tasks = list(self.p._background_tasks)
        self.assertEqual(len(tasks), 2)
        await self.p.terminate()
        self.assertTrue(all(t.cancelled() for t in tasks))
        sender.assert_not_awaited()
        self.p.bus.emit_soon("owned")
        self.assertFalse(self.p._background_tasks)

    async def test_background_error_is_warning_and_task_removed(self):
        async def broken():
            raise RuntimeError("injected failure")
        with self.assertLogs("congling-offline-test", level="WARNING") as logs:
            task = self.p.spawn_background(broken())
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
        self.assertIn("后台任务退出", "".join(logs.output))
        self.assertFalse(self.p._background_tasks)

    async def test_shutdown_flush_failure_still_closes(self):
        self.mem.flush = AsyncMock(side_effect=RuntimeError("flush"))
        close = AsyncMock()
        self.p.store = types.SimpleNamespace(close=close)
        self.p.night = types.SimpleNamespace(shutdown=AsyncMock(side_effect=RuntimeError("night")))
        with self.assertLogs("congling-offline-test", level="WARNING"):
            await self.p.terminate()
        close.assert_awaited_once()

    async def test_disabled_night_tick_updates_and_persists_without_events(self):
        self.cfg._raw["night"]["enabled"] = False
        night = base.NightService(self.p)
        for now in (NOW, NOW + 60):
            self.assertEqual(await night.tick(now=now), [])
            self.assertEqual((await self.store.kv_get("night.state"))["last_tick"], now)

    async def test_every_heartbeat_mode_persists(self):
        for mode in ("awake", "asleep", "waking"):
            with self.subTest(mode=mode):
                night = base.NightService(self.p)
                night.state = NightState(mode=mode, last_tick=NOW, sleep_at=NOW - 600,
                    wake_at=NOW + 3600, enter_waking_at=NOW, pending_crawl=["A"])
                with patch("astrbot_plugin_congling.night.energy.fatigue_kind", return_value=""), \
                        patch.object(night, "_past_latest_sleep", return_value=False):
                    await night.tick(now=NOW + 60)
                self.assertEqual((await self.store.kv_get("night.state"))["last_tick"], NOW + 60)

    async def test_message_fatigue_count_survives_reload(self):
        night = base.NightService(self.p)
        night.state = NightState(last_tick=NOW, energy=100)
        with patch("astrbot_plugin_congling.night.state.time.time", return_value=NOW), \
                patch("astrbot_plugin_congling.night.energy.fatigue_kind", return_value="night"), \
                patch("astrbot_plugin_congling.night.energy.in_fatigue", return_value=True), \
                patch.object(base.NightService, "_past_latest_sleep", return_value=False):
            await night.on_message(PerceivedMessage(text="test"))
            saved = await self.store.kv_get("night.state")
            self.assertEqual(saved["fatigue_turns"], 1)
            self.assertEqual(saved["fatigue_entered"], NOW)
            restored = base.NightService(self.p)
            await restored.load()
            self.assertEqual(restored._fatigue_turns, 1)
            await restored.tick(now=NOW + 60)
            self.assertEqual(restored._fatigue_turns, 1)
            self.assertEqual((await self.store.kv_get("night.state"))["fatigue_turns"], 1)

    async def wake(self, stage="rem"):
        self.p.night = NightModule(self.p)
        self.p.night.state.state = NightState(mode="asleep", sleep_at=NOW - 7200, wake_at=NOW + 3600)
        with patch("astrbot_plugin_congling.night.state.time.time", return_value=NOW), \
                patch("astrbot_plugin_congling.night.state.stages_mod.at", return_value={"key": stage}):
            await self.p.night.state._do_wake(reason="message")
        return self.p.night

    async def test_woke_shares_event_but_has_independent_scope_kind_budget(self):
        night = await self.wake()
        event_id = night.state.wake_hints.event_id
        with patch("astrbot_plugin_congling.night.time.time", return_value=NOW):
            for scope in ("group:G", "private:A", ""):
                self.assertEqual(sum("REM" in night.prompt_hint(session_key=scope) for _ in range(20)), 1)
                self.assertEqual(night.state.wake_hints.uses[("woke", scope)], 1)
        self.assertEqual(night.state.wake_hints.event_id, event_id)
        self.assertFalse(any(k[0] == "dream" for k in night.state.wake_hints.uses))

    async def test_woke_expiry_preserves_usage_event_and_new_scopes_do_not_rearm(self):
        night = await self.wake("n3")
        self.cfg._raw["night"]["woke_hint_ttl_minutes"] = 1
        with patch("astrbot_plugin_congling.night.time.time", return_value=NOW):
            self.assertIn("深度睡眠", night.prompt_hint(session_key="A"))
        before = dict(night.state.wake_hints.uses)
        event_id = night.state.wake_hints.event_id
        with patch("astrbot_plugin_congling.night.time.time", return_value=NOW + 60):
            for scope in ("A", "B", ""):
                self.assertNotIn("深度睡眠", night.prompt_hint(session_key=scope))
        self.assertEqual(night.state.wake_hints.uses, before)
        self.assertEqual(night.state.wake_hints.event_id, event_id)
        self.assertEqual(night.state.wake_hints.started_at, NOW)
        self.cfg._raw["night"]["woke_hint_ttl_minutes"] = 120
        with patch("astrbot_plugin_congling.night.time.time", return_value=NOW + 61):
            self.assertNotIn("深度睡眠", night.prompt_hint(session_key="A"))

    async def test_woke_preview_farewell_and_zero_budget_do_not_spend(self):
        night = await self.wake()
        with patch("astrbot_plugin_congling.night.time.time", return_value=NOW):
            self.assertNotIn("REM", night.prompt_hint(consume=False))
            night.pending_sleep = {"kind": "night"}
            self.assertNotIn("REM", night.prompt_hint())
            night.pending_sleep = None
            for field in ("woke_hint_max_uses", "woke_hint_ttl_minutes"):
                self.cfg._raw["night"][field] = 0
                self.assertNotIn("REM", night.prompt_hint())
                self.cfg._raw["night"].pop(field)
        self.assertEqual(night.state.wake_hints.uses, {})

    async def test_woke_request_reentry_is_temp_once_and_does_not_change_prefix(self):
        night = await self.wake()
        req = types.SimpleNamespace(system_prompt="STATIC", extra_user_content_parts=[], contexts=[])
        event = types.SimpleNamespace(unified_msg_origin="group:G")
        with patch("astrbot_plugin_congling.night.time.time", return_value=NOW):
            await self.p.persona_hook(event, req)
            await self.p.persona_hook(event, req)
        self.assertEqual(req.system_prompt, "STATIC")
        self.assertEqual(req.contexts, [])
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertTrue(req.extra_user_content_parts[0].no_save)
        self.assertEqual(night.state.wake_hints.uses[("woke", "group:G")], 1)

    async def test_woke_crawl_repeated_wake_restart_and_new_sleep(self):
        night = await self.wake()
        with patch("astrbot_plugin_congling.night.time.time", return_value=NOW), \
                patch("astrbot_plugin_congling.night.energy.fatigue_kind", return_value=""), \
                patch.object(base.NightService, "_past_latest_sleep", return_value=False):
            self.assertIn("REM", night.prompt_hint())
            event_id = night.state.wake_hints.event_id
            await night.state.finish_waking()
            await night.state.wake_now()
            self.assertEqual(night.state.wake_hints.event_id, event_id)
            self.assertNotIn("REM", night.prompt_hint())
            await night.shutdown()
            restored = NightModule(self.p)
            await restored.start()
            self.assertFalse(restored.state.wake_hints.event_id)
            self.assertNotIn("REM", restored.prompt_hint())
            await night.state.sleep_now(by_user=True)
            self.assertFalse(night.state.wake_hints.event_id)
            with patch("astrbot_plugin_congling.night.state.stages_mod.at", return_value={"key": "rem"}):
                await night.state.wake_now()
            self.assertNotEqual(night.state.wake_hints.event_id, event_id)
            self.assertIn("REM", night.prompt_hint())

    async def test_dream_pending_tracks_completion_exception_and_cancellation(self):
        self.cfg._raw["night"]["dream_enabled"] = True
        night = self.p.night = NightModule(self.p)
        entered, release = asyncio.Event(), asyncio.Event()
        async def make(**kwargs):
            entered.set()
            await release.wait()
            return None
        night.dreamer._make = make
        task = self.p.spawn_background(night.dreamer.make())
        await entered.wait()
        self.assertEqual(night.snapshot()["dream"]["pending"], 1)
        self.assertEqual(self.p.health()["dream"]["pending"], 1)
        release.set()
        await task
        self.assertEqual(night.dreamer.pending, 0)
        for error in (RuntimeError("failure"), asyncio.CancelledError()):
            night.dreamer._make = AsyncMock(side_effect=error)
            with self.assertRaises(type(error)):
                await night.dreamer.make()
            self.assertEqual(night.dreamer.pending, 0)

    async def test_subject_unresolved_is_readonly_partitioned_and_unknown_on_error(self):
        self.assertIsNone(self.mem.snapshot()["subject"]["unresolved"])
        for pid, uid, source in (("ling", "A", "qq"), ("ling", "name", "inferred"),
                                 ("other", "name", "inferred")):
            await self.store.execute("INSERT INTO mem_subject(persona_id,user_id,source) VALUES(?,?,?)",
                                     (pid, uid, source))
        with patch.object(self.store, "execute", new_callable=AsyncMock) as write:
            await self.mem._refresh_subject_diagnostics()
            write.assert_not_awaited()
        data = self.mem.snapshot()
        self.assertEqual(data["subject"]["unresolved"], 1)
        self.assertGreater(data["subject"]["at"], 0)
        data["subject"]["unresolved"] = 999
        self.assertEqual(self.mem.snapshot()["subject"]["unresolved"], 1)
        with patch.object(self.store, "scalar", new=AsyncMock(side_effect=RuntimeError("private detail"))), \
                self.assertLogs("congling-offline-test", level="WARNING") as logs:
            await self.mem._refresh_subject_diagnostics()
        self.assertIsNone(self.mem.snapshot()["subject"]["unresolved"])
        self.assertEqual(self.mem.snapshot()["subject"]["error"], "RuntimeError")
        self.assertNotIn("private detail", "".join(logs.output))

    async def test_recall_snapshot_hits_empty_failure_and_cache_prefix(self):
        self.assertIsNone(self.mem.snapshot()["recall"]["last_hits"])
        msg = PerceivedMessage(session_id="A", session_kind="private", sender_id="A", text="test")
        event = types.SimpleNamespace(_congling_msg=msg, get_sender_id=lambda: "A", get_group_id=lambda: "")
        for hits in ([MemoryHit(atom_id=1, text="私密正文")], []):
            self.mem.retriever.search = AsyncMock(return_value=hits)
            req = types.SimpleNamespace(system_prompt="STATIC", extra_user_content_parts=[])
            await self.mem.inject(event, req)
            self.assertEqual(self.mem.snapshot()["recall"]["last_hits"], len(hits))
            self.assertEqual(req.system_prompt, "STATIC")
            self.assertNotIn("私密正文", json.dumps(self.mem.snapshot(), ensure_ascii=False))
        self.mem.retriever.search = AsyncMock(side_effect=RuntimeError("test"))
        await self.mem.inject(event, req)
        self.assertIsNone(self.mem.snapshot()["recall"]["last_hits"])
        self.assertEqual(self.mem.snapshot()["recall"]["error"], "RuntimeError")

    async def test_health_endpoint_has_four_diagnostics_without_consuming_hints(self):
        night = await self.wake()
        night.state.state.last_tick = NOW
        await self.mem._refresh_subject_diagnostics()
        module, api = self.page_api()
        with patch.object(self.store, "scalar", new_callable=AsyncMock) as query:
            result = await api.health()
            query.assert_not_awaited()
        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertEqual(data["night"]["state"]["last_tick"], NOW)
        self.assertEqual(data["night"]["last_tick"], NOW)
        self.assertEqual(data["dream"]["pending"], 0)
        self.assertIsNone(data["recall"]["last_hits"])
        self.assertEqual(data["subject"]["unresolved"], 0)
        self.assertEqual(night.state.wake_hints.uses, {})
        self.p.night = self.p.memory = None
        data = self.p.health()
        self.assertIsNone(data["night"]["state"]["last_tick"])
        self.assertIsNone(data["dream"]["pending"])
        self.assertIsNone(data["subject"]["unresolved"])

    async def test_proactive_failure_visible_without_debug_or_sensitive_text(self):
        proactive = base.Proactive(self.p)
        for result in ({"ok": False, "error": "这次没生成出来"},
                       {"ok": False, "error": "私密正文"}):
            proactive.send_one = AsyncMock(return_value=result)
            with self.assertLogs("congling-offline-test", level="WARNING") as logs:
                await proactive._send_guarded({"session_id": "A"}, reason="idle")
            self.assertIn("主动发送未完成", "".join(logs.output))
            self.assertNotIn("私密正文", "".join(logs.output))
        proactive.send_one = AsyncMock(side_effect=RuntimeError("私密正文"))
        with self.assertLogs("congling-offline-test", level="WARNING") as logs:
            await proactive._send_guarded({"session_id": "A"}, reason="idle")
        self.assertIn("RuntimeError", "".join(logs.output))
        self.assertNotIn("私密正文", "".join(logs.output))

    async def test_background_model_and_persistence_failures_log_warning(self):
        self.p.models = types.SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError("PRIVATE")))
        dreamer = base.Dreamer(self.p)
        proactive = base.Proactive(self.p)
        diary = base.Diary(self.p)
        checks = [
            (lambda: dreamer._call_model("test", "test"), "梦境生成失败"),
            (lambda: diary._compose_by_model("2026-11-01", {"materials": "test", "affinity_block": ""}), "日记生成失败"),
        ]
        for call, message in checks:
            with self.subTest(message=message), self.assertLogs("congling-offline-test", level="WARNING") as logs:
                await call()
            self.assertIn(message, "".join(logs.output))
            self.assertNotIn("PRIVATE", "".join(logs.output))
        with patch.object(self.store, "kv_set", new=AsyncMock(side_effect=RuntimeError("PRIVATE"))), \
                self.assertLogs("congling-offline-test", level="WARNING") as logs:
            await proactive.save()
        self.assertIn("主动状态保存失败", "".join(logs.output))
        self.assertNotIn("PRIVATE", "".join(logs.output))

    async def test_scheduler_warning_and_heartbeat_isolation_on_failure(self):
        self.mem._sweep = AsyncMock(side_effect=RuntimeError("PRIVATE"))
        with patch("astrbot_plugin_congling.memory.service.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)), \
                self.assertLogs("congling-offline-test", level="WARNING") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await self.mem._scheduler()
        self.assertIn("记忆调度异常", "".join(logs.output))
        self.assertNotIn("PRIVATE", "".join(logs.output))
        self.p.night = types.SimpleNamespace(state=types.SimpleNamespace(tick=AsyncMock()))
        self.p.proactive = types.SimpleNamespace(tick=AsyncMock(side_effect=RuntimeError("test")))
        sleep_calls = 0
        async def sleep(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 2:
                raise asyncio.CancelledError
        with patch("astrbot_plugin_congling.main.asyncio.sleep", side_effect=sleep), \
                self.assertLogs("congling-offline-test", level="WARNING"):
            with self.assertRaises(asyncio.CancelledError):
                await self.p._beat_loop()
        self.assertEqual(self.p.night.state.tick.await_count, 2)
        self.assertEqual(self.p.proactive.tick.await_count, 2)

    async def test_qq_daily_failures_do_not_skip_avatar_and_are_warning(self):
        self.p.qqext = types.SimpleNamespace(
            qzone_daily=types.SimpleNamespace(tick=AsyncMock(side_effect=RuntimeError("PRIVATE"))),
            avatar=object(),
            avatar_pool=types.SimpleNamespace(auto_change=AsyncMock(side_effect=RuntimeError("PRIVATE"))))
        with self.assertLogs("congling-offline-test", level="WARNING") as logs:
            await self.p._qq_daily_tick()
        self.p.qqext.avatar_pool.auto_change.assert_awaited_once_with(self.p.qqext.avatar)
        self.assertIn("每日说说任务出错", "".join(logs.output))
        self.assertIn("自动换头像失败", "".join(logs.output))
        self.assertNotIn("PRIVATE", "".join(logs.output))

    async def test_shutdown_remains_query_free_for_real_61_seconds(self):
        self.p.night = NightModule(self.p)
        self.p.proactive = base.Proactive(self.p)
        await self.mem.start()
        self.p.spawn_background(self.p._beat_loop())
        self.p.spawn_background(self.p._dream_loop())
        self.p.spawn_background(self.p._housekeeping())
        await asyncio.sleep(0)
        tasks = list(self.p._background_tasks)
        self.assertGreaterEqual(len(tasks), 4)
        await self.p.terminate()
        self.assertTrue(all(task.done() for task in tasks))
        self.assertFalse(self.p._background_tasks)
        # Real event-loop time, not patched wall-clock time. These sentinels
        # detect even swallowed post-close queries against the temporary store.
        names = ("fetch_all", "fetch_one", "scalar", "execute", "kv_get", "kv_set")
        patches = [patch.object(self.store, name, new=AsyncMock(side_effect=AssertionError("query after close")))
                   for name in names]
        mocks = [p.start() for p in patches]
        try:
            with self.assertNoLogs("congling-offline-test", level="WARNING"):
                await asyncio.sleep(61)
            for query in mocks:
                query.assert_not_awaited()
        finally:
            for p in reversed(patches):
                p.stop()


def wait_for_self(callback):
    async def call(_):
        return await callback()
    return call


class SourceGuards(unittest.TestCase):
    def test_night_schema_original_47_plus_p4_and_hint_keys_once(self):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                self.assertNotIn(key, result, "duplicate JSON field: " + key)
                result[key] = value
            return result
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"), object_pairs_hook=unique)
        night = schema["night"]["items"]
        added = {"dream_mode", "dream_mention_min_level", "dream_hint_scope",
                 "dream_recall_guard", "dream_recall_k"}
        self.assertTrue(added <= night.keys())
        self.assertEqual(len(night.keys() - added), 47)
        for key in ("woke_hint_max_uses", "woke_hint_ttl_minutes", "dream_hint_max_uses", "dream_hint_ttl_minutes"):
            self.assertEqual(night[key]["default"], base.Config({}).get("night." + key))

    def test_repository_encoding_and_preexisting_user_fix(self):
        result = scan()
        self.assertEqual(result["hits"], [])
        # 开发副本里会带施工文档（其中有一处故意引用的乱码示例）；部署到运行时，
        # docs 里只保留原有三份说明，示例数自然为 0。两种都算正常。
        self.assertLessEqual(len(result["documented_examples"]), 1)
        self.assertGreater(result["files"], 100)
        source = (ROOT / "memory/service.py").read_text(encoding="utf-8")
        self.assertIn("session_ref=where_ref, actor_ref=who_ref", source)
        self.assertIn("who_ref = actor_ref(platform or \"qq\", ev_sender)", source)

    def test_encoding_detector_recognizes_explicit_corruption(self):
        for text in ("\u7481\u677f\u7e42", "\u93c3\u5815\u68ff", "\u6d93\u5d86\u69f8", "\ufffd"):
            self.assertIsNotNone(SUSPECT.search(text))
        self.assertIsNone(SUSPECT.search("群聊个人记忆补充失败"))

    def test_background_failures_not_downgraded_and_normal_debug_retained(self):
        selected = {
            "main.py": {"initialize", "terminate", "_housekeeping", "_beat_loop", "_dream_loop", "_qq_daily_tick"},
            "memory/service.py": {"start", "flush", "_scheduler", "_sweep", "_extract_session", "_prepare_drafts", "daily_tick", "build_profiles"},
            "humanize/proactive.py": None,
            "night/state.py": None, "night/dream.py": None, "night/__init__.py": None,
            "memory/diary.py": {"tick", "backfill", "_compose_by_model", "_gather"},
            "memory/affinity.py": {"add", "settle_daily"},
            "memory/atom.py": {"add_many"},
            "memory/extractor.py": {"extract", "_parse"},
            "memory/maintenance.py": {"reflect", "backup"},
            "memory/profile.py": {"build"},
            "memory/subject.py": {"ensure", "link_atom"},
            "qqext/qzone_daily.py": {"materials", "compose"},
        }
        for rel, functions in selected.items():
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8-sig"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or (functions is not None and fn.name not in functions):
                    continue
                for handler in (node for node in ast.walk(fn) if isinstance(node, ast.ExceptHandler)):
                    for call in (node for node in ast.walk(handler) if isinstance(node, ast.Call)):
                        self.assertNotEqual(ast.unparse(call.func), "logger.debug", f"{rel}:{call.lineno}")
        self.assertIn('logger.debug("[congling] 主动消息与上次重复，放弃")', (ROOT / "humanize/proactive.py").read_text(encoding="utf-8"))
        self.assertIn("logger.debug(f\"[congling] 写入", (ROOT / "memory/service.py").read_text(encoding="utf-8"))

    def test_no_dynamic_system_prompt_or_history_fallback_calls(self):
        for path in ROOT.rglob("*.py"):
            if "tests" in path.relative_to(ROOT).parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    self.assertNotIn(ast.unparse(node.func).split(".")[-1], {"append_user_pair"}, str(path))
                if path.name != "persona.py" and isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                    self.assertNotEqual(node.attr, "system_prompt", str(path))

    def test_metadata_and_plugin_version_unchanged(self):
        self.assertEqual(hashlib.sha256((ROOT / "metadata.yaml").read_bytes()).hexdigest(),
                         "79dbdd355b377cb6359d36d19594a6f3f17471c283f67b7df7ad477fb7b280a3")
        self.assertEqual(importlib.import_module("astrbot_plugin_congling.main").PLUGIN_VERSION, "1.1.0")

    def test_all_19_page_assets_present(self):
        """P6 之外唯一允许改前端的一轮（P5 加了删除记忆按钮），
        所以这条从"哈希必须完全不变"放宽为"19 个页面资产都在且非空"。
        哈希基线等前端稳定后另行固定。"""
        assets = sorted(
            p for p in (base.ROOT / "pages" / "fromling").rglob("*")
            if p.is_file() and p.suffix in {".js", ".css", ".html", ".png", ".webp"}
        )
        self.assertGreaterEqual(len(assets), 19)
        for path in assets:
            self.assertGreater(path.stat().st_size, 0, str(path))
