"""睡了就是睡了：静默必须是全局的。

踩过的坑：`state.on_message` 开头原本是

    if not self.enabled:
        return {"block": False, "hint": "未启用"}

它在状态判断之前就返回了，而 `_do_sleep` 并不看 `night.enabled` ——
于是「长夜没开、只开收线静默」的机器上，私聊里说了晚安它真躺下了，
群聊那边却因为提前 return 而继续有说有笑。这里把这条钉死：
只要状态是睡着，哪一个会话、走路哪条分支进来，都得拦。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.core.contracts import PerceivedMessage  # noqa: E402
from astrbot_plugin_congling.night import state as state_mod  # noqa: E402


class FakeCfg:
    def __init__(self, data: dict) -> None:
        self.d = data

    def str(self, key, default=""):
        return str(self.d.get(key, default))

    def bool(self, key, default=False):
        return bool(self.d.get(key, default))

    def int(self, key, default=0):
        try:
            return int(self.d.get(key, default))
        except (TypeError, ValueError):
            return default

    def float(self, key, default=0.0):
        try:
            return float(self.d.get(key, default))
        except (TypeError, ValueError):
            return default

    def list(self, key, default=None):
        return list(self.d.get(key, default if default is not None else []))


class FakeStore:
    def __init__(self) -> None:
        self.kv: dict = {}

    async def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key, value):
        self.kv[key] = value

    async def scalar(self, sql, params=()):
        return 0

    async def fetch_all(self, sql, params=()):
        return []


class FakePlugin:
    def __init__(self, conf) -> None:
        self.cfg = conf
        self.store = FakeStore()
        self.models = None
        self.admin_ids = ["3190568934"]


def make_cfg(**over) -> FakeCfg:
    data = {
        "night.enabled": True,
        "night.allow_sleep": True,
        "night.night_fatigue": "23:30-07:00",
        "night.noon_fatigue": "13:00-14:30",
        "night.energy_max": 100,
        "night.energy_per_message": 4.0,
        "night.energy_per_minute": 0.8,
        "night.energy_recover_per_minute": 1.5,
        "night.min_turns_before_sleep": 8,
        "night.wake_time": "08:00",
        "night.wake_jitter_minutes": 0,
        "night.latest_sleep_time": "02:30",
        "night.wake_mode": "private_only",
        "night.wake_window_minutes": 10,
        "night.wake_start_count": 2,
        "night.wake_step_count": 4,
        "night.dream_enabled": False,
    }
    data.update(over)
    return FakeCfg(data)


def pm(session_id: str, kind: str, text: str = "在吗") -> PerceivedMessage:
    return PerceivedMessage(
        session_id=session_id, session_kind=kind, sender_id="10001", text=text
    )


class GlobalSilenceTest(unittest.TestCase):
    def test_sleep_blocks_every_session_when_night_switch_off(self):
        """长夜没开、只靠收线静默躺下的情况：群聊也得停。"""

        async def scenario():
            svc = state_mod.NightService(FakePlugin(make_cfg(**{"night.enabled": False})))
            await svc.load()
            await svc._do_sleep(reason="talk", kind="night")
            self.assertTrue(svc.state.is_asleep())
            results = {}
            for kind, sid in (("private", "p1"), ("group", "g1"), ("group", "g2")):
                results[sid] = await svc.on_message(pm(sid, kind))
            return results

        results = asyncio.run(scenario())
        for sid, result in results.items():
            self.assertTrue(result.get("block"), f"{sid} 在它睡着了的时候还被放行了")

    def test_sleep_blocks_every_session_when_night_switch_on(self):
        async def scenario():
            svc = state_mod.NightService(FakePlugin(make_cfg()))
            await svc.load()
            await svc._do_sleep(reason="talk", kind="night")
            return [
                await svc.on_message(pm("p1", "private")),
                await svc.on_message(pm("g1", "group")),
            ]

        for result in asyncio.run(scenario()):
            self.assertTrue(result.get("block"))

    def test_awake_with_night_switch_off_is_not_blocked(self):
        """没在睡的时候，开关关着就应该彻底不管事。"""

        async def scenario():
            svc = state_mod.NightService(FakePlugin(make_cfg(**{"night.enabled": False})))
            await svc.load()
            return await svc.on_message(pm("g1", "group"))

        result = asyncio.run(scenario())
        self.assertFalse(result.get("block"))
        self.assertEqual(result.get("hint"), "未启用")

    def test_asleep_survives_restart(self):
        """睡下去这件事要落盘：重启之后别的会话进来仍然是睡的。"""

        async def scenario():
            plugin = FakePlugin(make_cfg(**{"night.enabled": False}))
            svc = state_mod.NightService(plugin)
            await svc.load()
            await svc._do_sleep(reason="talk", kind="night")
            # 同一个 store 上重建一个 service，模拟重启
            again = state_mod.NightService(plugin)
            await again.load()
            self.assertTrue(again.state.is_asleep())
            return await again.on_message(pm("g9", "group"))

        result = asyncio.run(scenario())
        self.assertTrue(result.get("block"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
