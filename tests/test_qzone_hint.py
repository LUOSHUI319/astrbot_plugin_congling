"""空间提示：两个模式各说什么、自知表记的什么。

这段文字是每轮请求都要塞的，所以「短」和「准」都重要：
off 只有一句提醒别乱动，intent 才带分寸和最近发过什么。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.qqext import qzone_hint as hint_mod  # noqa: E402
from astrbot_plugin_congling.qqext.qzone_hint import (  # noqa: E402
    build_hint,
    load_mind,
    recent_block,
    remember_post,
)


class FakeStore:
    def __init__(self):
        self.kv = {}

    async def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key, value):
        self.kv[key] = value


class FakeCfg:
    def __init__(self, **kv):
        self.d = {"qq_extra.qzone_behavior_mode": "off", "qq_extra.qzone_tool_enabled": True}
        self.d.update(kv)

    def bool(self, key, default=False):
        return bool(self.d.get(key, default))

    def str(self, key, default=""):
        return str(self.d.get(key, default))


class FakePlugin:
    def __init__(self, **kv):
        self.cfg = FakeCfg(**kv)
        self.store = FakeStore()


def run(coro):
    return asyncio.run(coro)


class ModeHintTest(unittest.TestCase):
    def test_off_says_hands_are_down(self):
        text = run(build_hint(FakePlugin()))
        self.assertIn("关着", text)
        self.assertIn("不要自己主动", text)
        # off 的话要短，它是每轮都塞的
        self.assertLess(len(text), 160, f"off 提示太长了：{len(text)}")

    def test_off_does_not_mention_posting_allowed(self):
        text = run(build_hint(FakePlugin()))
        self.assertNotIn("可以发到空间", text)

    def test_intent_grants_intent(self):
        text = run(build_hint(FakePlugin(**{"qq_extra.qzone_behavior_mode": "intent"})))
        self.assertIn("可以自己想说点什么", text)
        self.assertIn("别为了发而发", text)

    def test_tool_disabled_says_nothing(self):
        plugin = FakePlugin(**{"qq_extra.qzone_tool_enabled": False})
        self.assertEqual(run(build_hint(plugin)), "")

    def test_unknown_mode_treated_as_off(self):
        text = run(build_hint(FakePlugin(**{"qq_extra.qzone_behavior_mode": "whatever"})))
        self.assertIn("关着", text)


class SelfAwarenessTest(unittest.TestCase):
    def test_cooldown_mentioned_after_posting(self):
        plugin = FakePlugin(**{"qq_extra.qzone_behavior_mode": "intent"})
        run(remember_post(plugin, "今天下了很大的雨"))
        text = run(build_hint(plugin))
        self.assertIn("别急着再发", text)

    def test_recent_posts_listed(self):
        plugin = FakePlugin(**{"qq_extra.qzone_behavior_mode": "intent"})
        run(remember_post(plugin, "今天下了很大的雨"))
        run(remember_post(plugin, "楼下的猫又开始叫了"))
        text = run(build_hint(plugin))
        self.assertIn("别重复", text)
        self.assertIn("楼下的猫", text)

    def test_old_post_does_not_trigger_cooldown(self):
        plugin = FakePlugin(**{"qq_extra.qzone_behavior_mode": "intent"})
        old = int(time.time()) - 12 * 3600
        run(plugin.store.kv_set(
            "qzone:mind",
            {"last_post_at": old, "recent": [{"text": "旧的一条", "at": old}], "day": "", "today_count": 1},
        ))
        text = run(build_hint(plugin))
        self.assertNotIn("别急着再发", text)
        self.assertIn("旧的一条", text)


class MindBookkeepingTest(unittest.TestCase):
    def test_post_appends_and_keeps_tail(self):
        plugin = FakePlugin()
        for i in range(hint_mod.RECENT_KEEP + 3):
            run(remember_post(plugin, f"第 {i} 条"))
        mind = run(load_mind(plugin))
        self.assertEqual(len(mind["recent"]), hint_mod.RECENT_KEEP)
        self.assertEqual(mind["recent"][-1]["text"], f"第 {hint_mod.RECENT_KEEP + 2} 条")

    def test_today_count_resets_on_new_day(self):
        plugin = FakePlugin()
        run(plugin.store.kv_set("qzone:mind", {"day": "2000-01-01", "today_count": 9, "recent": []}))
        run(remember_post(plugin, "新的一天"))
        mind = run(load_mind(plugin))
        self.assertEqual(mind["today_count"], 1)
        self.assertEqual(mind["day"], time.strftime("%Y-%m-%d"))

    def test_same_day_count_accumulates(self):
        plugin = FakePlugin()
        run(remember_post(plugin, "一"))
        run(remember_post(plugin, "二"))
        mind = run(load_mind(plugin))
        self.assertEqual(mind["today_count"], 2)

    def test_recent_block_skips_blank(self):
        block = recent_block({"recent": [{"text": "  ", "at": 0}, {"text": "有内容", "at": 0}]})
        self.assertIn("有内容", block)
        self.assertEqual(block.count("- "), 1)

    def test_store_failure_does_not_raise(self):
        class BrokenStore:
            async def kv_get(self, key, default=None):
                raise RuntimeError("boom")

            async def kv_set(self, key, value):
                raise RuntimeError("boom")

        plugin = FakePlugin()
        plugin.store = BrokenStore()
        # 记住失败不能把发布流程带崩
        run(remember_post(plugin, "写不进去"))
        self.assertEqual(run(load_mind(plugin)), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
