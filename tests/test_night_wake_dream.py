"""醒来卡在「睡着」+ 梦一段都不出（2026-10-27）。

两个都是同一类事故：`store.fetch_all` 返回的是 dict 行，
代码却按元组取 `row[0]`，一进 _do_wake / _pick_fragments 就 KeyError：

- `_pending_sessions` 抛错 → 醒来流程中断 → 面板一直显示睡着中
- `_pick_fragments` 抛错 → 做梦取不到碎片 → 梦根本生成不出来

这里把两条路径钉住，顺带证明梦能落库。
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.core.contracts import (  # noqa: E402
    NIGHT_ASLEEP,
    NIGHT_AWAKE,
    NIGHT_WAKING,
    NightState,
)
from astrbot_plugin_congling.night.dream import Dreamer  # noqa: E402
from astrbot_plugin_congling.night.state import NightService  # noqa: E402


class FakeCfg:
    def __init__(self, data: dict | None = None) -> None:
        self.data = {
            "night.enabled": True,
            "night.wake_time": "08:00",
            "night.wake_jitter_minutes": 0,
            "night.energy_max": 100,
            "night.energy_after_wake": 35,
            "night.energy_per_minute": 0.8,
            "night.energy_recover_per_minute": 1.5,
            "night.min_turns_before_sleep": 8,
            "night.latest_sleep_time": "02:30",
            "night.crawl_enabled": True,
            "night.waking_timeout_minutes": 5,
            "night.night_fatigue": "23:30-07:00",
            "night.noon_fatigue": "13:00-14:30",
            "night.dream_enabled": True,
            "night.dream_start_after_minutes": 60,
            "night.dream_interval_minutes": 60,
            "night.dream_max_per_sleep": 8,
            "night.dream_tags": ["美梦"],
            "night.dream_extra_tags": [],
        }
        self.data.update(data or {})

    def _get(self, key, default):
        return self.data.get(key, default)

    def str(self, key, default=""):
        return str(self._get(key, default))

    def int(self, key, default=0):
        try:
            return int(self._get(key, default))
        except (TypeError, ValueError):
            return default

    def float(self, key, default=0.0):
        try:
            return float(self._get(key, default))
        except (TypeError, ValueError):
            return default

    def bool(self, key, default=False):
        value = self._get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def list(self, key, default=None):
        value = self._get(key, default if default is not None else [])
        return list(value) if isinstance(value, (list, tuple)) else []


class DictRowStore:
    """和真 store 一样：fetch_all 给 dict 行。"""

    def __init__(self, sessions=None, texts=None) -> None:
        self.kv: dict[str, object] = {}
        self.sessions = sessions or []
        self.texts = texts or []

    async def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key, value):
        self.kv[key] = value

    async def fetch_all(self, sql, params=()):
        if "msg_archive" in sql:
            return [{"session_id": sid} for sid in self.sessions]
        if "mem_atom" in sql:
            return [{"text": text} for text in self.texts]
        return []

    async def fetch_one(self, sql, params=()):
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql, params=()):
        return 1

    async def scalar(self, sql, params=(), default=0):
        return 0


class FakeModels:
    def __init__(self, payload: str = '{"text":"梦见楼下的便利店。","level":3,"tags":["美梦"]}') -> None:
        self.payload = payload

    async def chat(self, prompt, system="", temperature=1.0, max_tokens=800):
        return self.payload


class FakePlugin:
    def __init__(self, store, cfg=None) -> None:
        self.cfg = cfg or FakeCfg()
        self.store = store
        self.models = FakeModels()


def make_service(**cfg) -> tuple[NightService, DictRowStore]:
    store = DictRowStore(sessions=["3190568934", "577084969"], texts=["望山说不喜欢被催睡觉"])
    service = NightService(FakePlugin(store, FakeCfg(cfg)))
    service.config = service.plugin.cfg
    return service, store


class PendingSessionsTest(unittest.TestCase):
    def test_dict_rows_do_not_crash(self) -> None:
        service, _ = make_service()
        got = asyncio.run(service._pending_sessions(int(time.time()) - 600))
        self.assertEqual(got, ["3190568934", "577084969"])


class WakeFromAsleepTest(unittest.TestCase):
    def test_wake_finishes_and_state_leaves_asleep(self) -> None:
        service, store = make_service()
        now = int(time.time())
        service.state = NightState(
            mode=NIGHT_ASLEEP,
            sleep_at=now - 3600,
            wake_at=now - 60,
            reason="manual",
            last_tick=now - 3600,
        )

        async def run():
            await service.tick(now=now)

        asyncio.run(run())
        # 有未读会话 → 进 waking；关键是不能还停在 asleep
        self.assertIn(service.state.mode, (NIGHT_WAKING, NIGHT_AWAKE))
        self.assertEqual(service.state.wake_at, 0)
        self.assertEqual(service.state.sleep_at, 0)

    def test_wake_without_pending_goes_awake(self) -> None:
        service, _ = make_service()
        service.store.sessions = []
        now = int(time.time())
        service.state = NightState(
            mode=NIGHT_ASLEEP,
            sleep_at=now - 3600,
            wake_at=now - 60,
            reason="manual",
            last_tick=now - 3600,
        )

        async def run():
            await service.tick(now=now)

        asyncio.run(run())
        self.assertEqual(service.state.mode, NIGHT_AWAKE)


class DreamFragmentsTest(unittest.TestCase):
    def test_fragments_from_dict_rows(self) -> None:
        store = DictRowStore(texts=["那家便利店", "三点钟的路灯"])
        dreamer = Dreamer(FakePlugin(store))

        async def run():
            return await dreamer._pick_fragments()

        self.assertEqual(asyncio.run(run()), ["那家便利店", "三点钟的路灯"])

    def test_make_saves_dream(self) -> None:
        store = DictRowStore(texts=["那家便利店"])
        dreamer = Dreamer(FakePlugin(store))

        async def run():
            await dreamer.load()
            return await dreamer.make(mood="美梦")

        entry = asyncio.run(run())
        self.assertIsNotNone(entry)
        self.assertIn("night.dreams", store.kv)
        self.assertEqual(len(store.kv["night.dreams"]), 1)
        self.assertEqual(dreamer.stats()["total"], 1)
        self.assertTrue(dreamer.list_recent(1))


if __name__ == "__main__":
    unittest.main()
