"""安静：谁能打破、几时能打破、以及它和「睡着」的分界。

安静是「醒着但不接话」，不是「睡了」。所以这里既验打破规则，也验
`NightState` 里 quiet_until 的存取——两者混一起会连带触发做梦、回精力那些事。
"""

from __future__ import annotations

import pathlib
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.core.contracts import NightState, PerceivedMessage  # noqa: E402
from astrbot_plugin_congling.night.quiet import QuietBreaker  # noqa: E402

OWNER = "3190568934"
STRANGER = "10001"


class FakeCfg:
    def __init__(self, **kv):
        self.d = {"close.break_window_minutes": 5, "close.break_count": 2}
        self.d.update(kv)

    def int(self, key, default=0):
        return int(self.d.get(key, default))

    def str(self, key, default=""):
        return str(self.d.get(key, default))

    def bool(self, key, default=False):
        return bool(self.d.get(key, default))


class FakePlugin:
    def __init__(self, **kv):
        self.cfg = FakeCfg(**kv)


def msg(text="在吗", *, kind="private", session="", sender=OWNER, mentions=None, quoted=""):
    return PerceivedMessage(
        session_id=session or sender,
        session_kind=kind,
        sender_id=sender,
        text=text,
        mentions=mentions or [],
        quoted=quoted,
    )


class QuietBreakerTest(unittest.TestCase):
    def setUp(self):
        self.plugin = FakePlugin()
        self.breaker = QuietBreaker(self.plugin)

    def test_stranger_never_counts(self):
        broke, why = self.breaker.check(msg(sender=STRANGER), is_admin=False)
        self.assertFalse(broke)
        self.assertIn("只有管理员", why)

    def test_admin_needs_two_in_private(self):
        first, why1 = self.breaker.check(msg(), is_admin=True)
        self.assertFalse(first)
        self.assertIn("还差 1", why1)
        second, _ = self.breaker.check(msg(), is_admin=True)
        self.assertTrue(second)

    def test_group_without_mention_does_not_count(self):
        for _ in range(3):
            broke, why = self.breaker.check(
                msg(kind="group", session="577084969"), is_admin=True, self_id="3404044656"
            )
            self.assertFalse(broke)
            self.assertIn("没被点名", why)

    def test_group_with_mention_counts(self):
        m = msg(kind="group", session="577084969", mentions=["@3404044656"])
        first, _ = self.breaker.check(m, is_admin=True, self_id="3404044656")
        self.assertFalse(first)
        second, _ = self.breaker.check(m, is_admin=True, self_id="3404044656")
        self.assertTrue(second)

    def test_quote_counts_in_group(self):
        m = msg(kind="group", session="577084969", quoted="3404044656 说：晚安")
        self.breaker.check(m, is_admin=True, self_id="3404044656")
        broke, _ = self.breaker.check(m, is_admin=True, self_id="3404044656")
        self.assertTrue(broke)

    def test_threshold_from_config(self):
        breaker = QuietBreaker(FakePlugin(**{"close.break_count": 1}))
        broke, _ = breaker.check(msg(), is_admin=True)
        self.assertTrue(broke)

    def test_window_expiry_resets(self):
        breaker = QuietBreaker(FakePlugin(**{"close.break_window_minutes": 1}))
        breaker.check(msg(), is_admin=True)
        # 手动把窗口推回过去，模拟超时
        counter = breaker._counters[OWNER]
        counter.window_start = time.time() - 120
        broke, why = breaker.check(msg(), is_admin=True)
        self.assertFalse(broke, "窗口过期后应该重新计")
        self.assertIn("还差 1", why)

    def test_break_resets_counter(self):
        self.breaker.check(msg(), is_admin=True)
        self.assertTrue(self.breaker.check(msg(), is_admin=True)[0])
        # 打破之后重新计数
        broke, why = self.breaker.check(msg(), is_admin=True)
        self.assertFalse(broke)
        self.assertIn("还差 1", why)


class QuietStateTest(unittest.TestCase):
    def test_roundtrip(self):
        state = NightState(mode="awake", quiet_until=int(time.time()) + 600, quiet_word="晚安")
        data = state.to_dict()
        self.assertEqual(data["quiet_until"], state.quiet_until)
        again = NightState.from_dict(data)
        self.assertEqual(again.quiet_until, state.quiet_until)
        self.assertEqual(again.quiet_word, "晚安")

    def test_expired_quiet_is_dropped_on_load(self):
        past = int(time.time()) - 60
        state = NightState.from_dict({"quiet_until": past, "quiet_word": "晚安"})
        self.assertEqual(state.quiet_until, 0)
        self.assertEqual(state.quiet_word, "")

    def test_quiet_does_not_imply_asleep(self):
        state = NightState(mode="awake", quiet_until=int(time.time()) + 600)
        self.assertFalse(state.is_asleep(), "安静被当成睡着，就会连带做梦和回精力")


class QuietServiceTest(unittest.TestCase):
    """真正落库的那一层：进去、读出来、解除。"""

    def build(self):
        from astrbot_plugin_congling.night import state as state_mod

        class Store:
            def __init__(self):
                self.kv = {}

            async def kv_get(self, key, default=None):
                return self.kv.get(key, default)

            async def kv_set(self, key, value):
                self.kv[key] = value

            async def fetch_all(self, *a, **k):
                return []

            async def fetch_one(self, *a, **k):
                return None

        class Plugin:
            def __init__(self):
                self.cfg = FakeCfg(**{
                    "night.enabled": False, "night.allow_sleep": False,
                    "night.wake_time": "08:00", "night.wake_jitter_minutes": 0,
                    "night.night_fatigue": "23:30-07:00", "night.noon_fatigue": "13:00-14:30",
                })
                self.store = Store()
                self.admin_ids = [OWNER]

        plugin = Plugin()
        return state_mod.NightService(plugin)

    def test_start_and_clear(self):
        svc = self.build()

        async def scenario():
            await svc.load()
            until = await svc.start_quiet(minutes=30, word="拜拜")
            self.assertTrue(svc.is_quiet())
            self.assertEqual(until, int(time.time()) + 30 * 60, msg="30 分钟没算对")
            self.assertIn("拜拜", svc.state.quiet_word)
            self.assertFalse(svc.state.is_asleep(), "安静不等于睡着")
            cleared = await svc.clear_quiet(reason="管理员叫醒")
            self.assertTrue(cleared)
            self.assertFalse(svc.is_quiet())

        import asyncio
        asyncio.run(scenario())

    def test_strong_word_runs_until_wake(self):
        svc = self.build()

        async def scenario():
            await svc.load()
            until = await svc.start_quiet(minutes=0, kind="night", word="晚安")
            self.assertTrue(svc.is_quiet())
            # 强档：安静到下一次起床点，不是固定分钟数
            self.assertGreater(until, int(time.time()) + 60)

        import asyncio
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
