"""Wake-event hint regressions. Offline entrypoint: run_diagnosis_regressions.py."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_plugin_congling.core.config import Config
from astrbot_plugin_congling.core.contracts import DreamEntry, NightState
from astrbot_plugin_congling.core.transient_hints import TransientHints
from astrbot_plugin_congling.main import ConglingPlugin
from astrbot_plugin_congling.night import NightModule
from astrbot_plugin_congling.qqext.qzone_daily import QzoneDaily
from astrbot_plugin_congling.tests.test_night_wake_dream import DictRowStore

NOW = 1793500000.0
DREAM = "UNIQUE_DREAM_FRAGMENT"


class BudgetTests(unittest.TestCase):
    def test_scope_kind_reset_and_boundary(self):
        hints = TransientHints()
        take = lambda kind="dream", scope="g": hints.take(
            kind, scope, limit=2, ttl_minutes=120, now=NOW)
        self.assertFalse(take())
        hints.begin(NOW)
        first = hints.event_id
        self.assertTrue(take())
        self.assertTrue(take())
        self.assertFalse(take())
        self.assertTrue(take(scope="p"))
        self.assertTrue(take(kind="woke"))
        self.assertFalse(hints.valid(now=NOW+7200, ttl_minutes=120))
        self.assertFalse(hints.valid(now=NOW-1, ttl_minutes=120))
        hints.begin(NOW)
        self.assertNotEqual(first, hints.event_id)
        self.assertTrue(take())
        hints.clear()
        self.assertFalse(take())


class DreamHintTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.addCleanup(patch.stopall)
        patch("astrbot_plugin_congling.night.time.time", return_value=NOW).start()
        patch("astrbot_plugin_congling.night.energy.fatigue_kind", return_value="").start()
        # P4 的五级梦也只有 0.9；预算测试固定命中，概率另有千次分桶断言。
        patch("astrbot_plugin_congling.night.dream.random.random", return_value=0).start()
        self.cfg = Config({"night": {"enabled": True, "dream_enabled": True,
                                      "crawl_enabled": False, "dream_mode": "live"}})
        self.store = DictRowStore()
        self.plugin = SimpleNamespace(cfg=self.cfg, store=self.store, models=None)
        self.night = self.plugin.night = NightModule(self.plugin)
        self.dream = DreamEntry(dream_id="dream-1", created_at=int(NOW-600),
                                level=5, text=DREAM, tags=["test"])
        self.night.dreamer._dreams = [self.dream]
        self.night.dreamer._loaded = True
        await self.wake()

    async def wake(self, stage="rem", reason="message", wake_at=None):
        self.night.state.state = NightState(
            mode="asleep", sleep_at=int(NOW-7200),
            wake_at=int(wake_at if wake_at is not None else NOW+3600))
        with patch("astrbot_plugin_congling.night.state.stages_mod.at",
                   return_value={"key": stage} if stage else None):
            await self.night.state._do_wake(reason=reason)

    def hint(self, key="g", **kwargs):
        return self.night.prompt_hint(session_key=key, include_private=True, **kwargs)

    async def test_twenty_turns_and_separate_sessions(self):
        self.assertEqual(sum(DREAM in self.hint() for _ in range(20)), 2)
        self.assertEqual(sum(DREAM in self.hint("private") for _ in range(20)), 2)
        self.assertNotIn(DREAM, self.hint())

    async def test_missing_key_is_bounded_and_does_not_clear_other_sessions(self):
        self.assertEqual(sum(DREAM in self.hint("") for _ in range(20)), 2)
        self.assertIn(DREAM, self.hint("private"))

    async def test_first_chat_four_hours_late_never_rolls(self):
        with patch("astrbot_plugin_congling.night.time.time", return_value=NOW+4*3600), \
             patch.object(self.night.dreamer, "pick_to_mention") as pick:
            for key in ("g", "private", "g"):
                text = self.hint(key)
                self.assertNotIn(DREAM, text)
                self.assertNotIn("REM", text)
            pick.assert_not_called()

    async def test_exact_ttl_expiry_does_not_rearm(self):
        self.assertIn(DREAM, self.hint())
        with patch("astrbot_plugin_congling.night.time.time", return_value=NOW+7200):
            self.assertNotIn(DREAM, self.hint("private"))
            self.assertIsNone(self.night.morning_dream)
            self.assertNotIn(DREAM, self.hint("new"))

    async def test_delayed_natural_wake_uses_scheduled_time(self):
        self.night.state.state.sleep_at = int(NOW-12*3600)
        self.night.state.state.wake_at = int(NOW-4*3600)
        await self.night.state._do_wake(reason="natural")
        self.assertEqual(self.night.state.wake_hints.started_at, NOW-4*3600)
        self.assertNotIn(DREAM, self.hint())

    async def test_non_rem_and_missing_stage_still_recall(self):
        for stage in ("n1", "n2", "n3", ""):
            await self.wake(stage)
            self.assertIn(DREAM, self.hint())

    async def test_preview_neither_reveals_nor_rolls_nor_spends(self):
        with patch.object(self.night.dreamer, "pick_to_mention",
                          wraps=self.night.dreamer.pick_to_mention) as pick:
            for _ in range(20):
                self.assertNotIn(DREAM, self.hint(consume=False))
            pick.assert_not_called()
            self.assertEqual(self.night.state.wake_hints.uses, {})
            self.assertIn(DREAM, self.hint())
            self.assertNotIn(DREAM, self.hint(consume=False))
            self.assertEqual(pick.call_count, 1)

    async def test_failed_recall_roll_is_not_retried(self):
        with patch.object(self.night.dreamer, "pick_to_mention", return_value=None) as pick:
            for _ in range(20):
                self.assertNotIn(DREAM, self.hint())
            self.assertEqual(pick.call_count, 1)

    async def test_old_high_level_dream_does_not_mask_this_sleep(self):
        old = DreamEntry(created_at=int(NOW-86400), level=5, text="OLD", strength=1)
        self.dream.level = 4
        self.night.dreamer._dreams.insert(0, old)
        with patch("astrbot_plugin_congling.night.dream.random.random", return_value=0):
            self.assertIn(DREAM, self.hint())
        self.assertIs(self.night.morning_dream, self.dream)

    async def test_old_or_future_or_expired_dreams_are_not_selected(self):
        for created, expired in ((NOW-86400, False), (NOW+1, False), (NOW-60, True)):
            await self.wake()
            self.dream.created_at, self.dream.expired = int(created), expired
            self.assertNotIn(DREAM, self.hint())

    async def test_forgetting_or_expiring_selected_dream_stops_injection(self):
        self.assertIn(DREAM, self.hint())
        self.night.dreamer.forget(self.dream.dream_id)
        self.assertNotIn(DREAM, self.hint())
        await self.wake()
        self.night.dreamer._dreams = [self.dream]
        self.assertIn(DREAM, self.hint())
        self.dream.expired = True
        self.assertNotIn(DREAM, self.hint())

    async def test_independent_ttls_and_budgets_and_no_rem_backdoor(self):
        self.cfg._raw["night"].update(dream_hint_max_uses=0, woke_hint_max_uses=2)
        text = self.hint()
        self.assertIn("REM", text)
        self.assertNotIn("提一句", text)
        self.assertNotIn(DREAM, text)
        self.cfg._raw["night"].update(dream_hint_max_uses=2, woke_hint_ttl_minutes=1)
        with patch("astrbot_plugin_congling.night.time.time", return_value=NOW+61):
            text = self.hint()
            self.assertIn(DREAM, text)
            self.assertNotIn("REM", text)

    async def test_zero_ttl_and_disabled_dream(self):
        self.cfg._raw["night"]["dream_hint_ttl_minutes"] = 0
        self.assertNotIn(DREAM, self.hint())
        self.cfg._raw["night"].update(dream_hint_ttl_minutes=120, dream_enabled=False)
        self.assertNotIn(DREAM, self.hint())

    async def test_waking_waits_for_awake_without_extending_ttl(self):
        self.night.state.state.mode = "waking"
        for _ in range(3):
            self.assertNotIn(DREAM, self.hint())
        await self.night.state.finish_waking()
        self.assertIn(DREAM, self.hint())
        self.assertEqual(self.night.state.wake_hints.started_at, NOW)

    async def test_crawl_does_not_reroll_and_awake_manual_wake_does_not_reset(self):
        self.assertIn(DREAM, self.hint())
        self.assertIn(DREAM, self.hint())
        self.night.crawler.mark_read = AsyncMock(return_value=1)
        self.night.crawler.mark_crawled = AsyncMock()
        self.night.need_crawl = AsyncMock(return_value=1)
        self.cfg._raw["night"]["crawl_announce"] = "off"
        await self.night.take_crawl_line("g")
        await self.night.state.wake_now()
        self.assertNotIn(DREAM, self.hint())

    async def test_sleep_cancels_and_next_wake_gets_fresh_budget(self):
        self.assertIn(DREAM, self.hint())
        self.assertIn(DREAM, self.hint())
        await self.night.state.sleep_now(by_user=True)
        self.assertNotIn(DREAM, self.hint())
        self.assertIsNone(self.night.morning_dream)
        await self.wake()
        self.assertIn(DREAM, self.hint())

    async def test_farewell_does_not_spend_or_inject_dream(self):
        self.night.pending_sleep = {"kind": "night"}
        self.assertNotIn(DREAM, self.hint())
        self.assertEqual(self.night.state.wake_hints.uses, {})

    async def test_restart_keeps_dream_archive_but_does_not_rearm(self):
        self.assertIn(DREAM, self.hint())
        await self.night.shutdown()
        restored = NightModule(self.plugin)
        await restored.start()
        self.assertEqual(restored.dreamer.living()[0].text, DREAM)
        self.assertIsNone(restored.morning_dream)
        self.assertFalse(restored.state.wake_hints.event_id)
        self.assertNotIn(DREAM, restored.prompt_hint(session_key="g"))

    async def test_main_injects_temp_user_part_once_per_request(self):
        plugin = ConglingPlugin.__new__(ConglingPlugin)
        plugin.enabled_for = lambda event: True
        plugin.persona = plugin.qqext = plugin.diary = plugin.capabilities = None
        plugin.night = self.night
        plugin.is_admin = lambda _: True
        event = SimpleNamespace(unified_msg_origin="g", get_group_id=lambda: "")
        req = SimpleNamespace(system_prompt="STATIC", extra_user_content_parts=[])
        await plugin.persona_hook(event, req)
        await plugin.persona_hook(event, req)
        self.assertEqual(req.system_prompt, "STATIC")
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn(DREAM, req.extra_user_content_parts[0].text)
        self.assertTrue(req.extra_user_content_parts[0].no_save)
        self.assertEqual(self.night.state.wake_hints.uses[("dream", "g")], 1)

    async def test_qzone_dream_has_only_dedicated_material_path(self):
        daily = QzoneDaily(self.plugin)
        self.assertIn(DREAM, self.hint())
        for _ in range(3):
            items = await daily.materials()
            self.assertFalse(any(DREAM in item["text"] for item in items))
            matches = [item for item in items if item["kind"] == "dream"]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["kind"], "dream")
        self.assertEqual(self.night.state.wake_hints.uses[("dream", "g")], 1)
        await daily._remember_material(items)
        restarted = QzoneDaily(self.plugin)
        await restarted.load_recent()
        self.assertFalse(any(DREAM in item["text"] for item in await restarted.materials()))

    def test_schema_and_code_defaults_match(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json")
                            .read_text(encoding="utf-8"))
        for key in ("dream_hint_max_uses", "dream_hint_ttl_minutes",
                    "woke_hint_max_uses", "woke_hint_ttl_minutes"):
            self.assertEqual(Config({}).get(f"night.{key}"), schema["night"]["items"][key]["default"])
