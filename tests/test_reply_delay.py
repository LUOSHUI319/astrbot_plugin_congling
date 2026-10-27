"""延时回复（0.7.0 · 第 10 条）。

重点是「永远不叠加」：插件里本来就有 rhythm 在接管 event.send，
外面再包一层会让两个 watchdog 互相以为对方没发、把同一条消息发两遍。
所以这里把三种模式钉死。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.core.rhythm import RhythmController  # noqa: E402
from astrbot_plugin_congling.humanize.delay import (  # noqa: E402
    MODE_OFF,
    MODE_RHYTHM,
    MODE_STANDALONE,
    ReplyDelayer,
)


class FakeCfg:
    def __init__(self, data: dict | None = None) -> None:
        self.data = {
            "humanize.reply_delay.enabled": True,
            "humanize.reply_delay.min_seconds": 2.0,
            "humanize.reply_delay.max_seconds": 8.0,
            "humanize.reply_delay.probability": 1.0,
            "humanize.reply_delay.apply_private": True,
            "humanize.reply_delay.apply_group": False,
            "humanize.reply_delay.night_scale": 1.0,
            "humanize.max_bubbles_private": 3,
            "humanize.max_bubbles_group": 2,
            "humanize.think_delay_base": 1.2,
            "humanize.bubble_delay_factor": 0.09,
        }
        self.data.update(data or {})

    def _get(self, k, d):
        return self.data.get(k, d)

    def bool(self, k, d=False):
        return bool(self._get(k, d))

    def int(self, k, d=0):
        return int(self._get(k, d))

    def float(self, k, d=0.0):
        return float(self._get(k, d))

    def str(self, k, d=""):
        return str(self._get(k, d))

    def list(self, k, d=None):
        return d or []


class FakeProbe:
    def __init__(self, yield_rhythm: bool = False) -> None:
        self.yield_rhythm = yield_rhythm

    def should_yield(self, name: str) -> bool:
        return name == "rhythm" and self.yield_rhythm


class FakeEvent:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message=None):
        text = ""
        for comp in getattr(message, "chain", []) or []:
            text += getattr(comp, "text", "") or ""
        self.sent.append(text)


class FakeGuard:
    def clean_bubbles(self, bubbles):
        return [b for b in bubbles if b is not None]


class ModeTest(unittest.TestCase):
    def test_off_when_disabled(self) -> None:
        cfg = FakeCfg({"humanize.reply_delay.enabled": False})
        self.assertEqual(ReplyDelayer(cfg, FakeProbe()).mode(), MODE_OFF)

    def test_rhythm_when_rhythm_alive(self) -> None:
        self.assertEqual(ReplyDelayer(FakeCfg(), FakeProbe(False)).mode(), MODE_RHYTHM)

    def test_standalone_when_rhythm_yields(self) -> None:
        self.assertEqual(ReplyDelayer(FakeCfg(), FakeProbe(True)).mode(), MODE_STANDALONE)


class HoldTest(unittest.TestCase):
    def test_hold_within_range(self) -> None:
        delayer = ReplyDelayer(FakeCfg(), FakeProbe())
        for _ in range(50):
            hold = delayer.extra_hold("private")
            self.assertGreaterEqual(hold, 2.0)
            self.assertLessEqual(hold, 8.0)

    def test_group_off_by_default(self) -> None:
        delayer = ReplyDelayer(FakeCfg(), FakeProbe())
        self.assertEqual(delayer.extra_hold("group"), 0.0)

    def test_off_returns_zero(self) -> None:
        cfg = FakeCfg({"humanize.reply_delay.enabled": False})
        self.assertEqual(ReplyDelayer(cfg, FakeProbe()).extra_hold("private"), 0.0)

    def test_patch_does_nothing_in_rhythm_mode(self) -> None:
        delayer = ReplyDelayer(FakeCfg(), FakeProbe(False))
        event = FakeEvent()
        self.assertFalse(delayer.patch(event, session_kind="private"))
        self.assertFalse(getattr(event, "_congling_delay_patched", False))

    def test_patch_once_in_standalone_mode(self) -> None:
        # 不能用 0~0.01：round 之后可能正好是 0.0，patch 会当成「不用等」直接返回 False
        cfg = FakeCfg({"humanize.reply_delay.min_seconds": 0.5, "humanize.reply_delay.max_seconds": 0.6})
        delayer = ReplyDelayer(cfg, FakeProbe(True))
        event = FakeEvent()
        self.assertTrue(delayer.patch(event, session_kind="private"))
        self.assertFalse(delayer.patch(event, session_kind="private"))
        self.assertTrue(getattr(event, "_congling_delay_patched", False))


class RhythmMergeTest(unittest.TestCase):
    def test_extra_hold_goes_into_first_bubble(self) -> None:
        cfg = FakeCfg()
        rhythm = RhythmController(cfg, FakeGuard(), None)
        plan = rhythm.build_plan(["第一句", "第二句"], session_kind="private", extra_hold=5.0)
        self.assertGreaterEqual(plan.delays[0], 5.0)
        self.assertLess(plan.delays[1], 5.0)

    def test_no_hold_keeps_original_delay(self) -> None:
        cfg = FakeCfg()
        rhythm = RhythmController(cfg, FakeGuard(), None)
        plan = rhythm.build_plan(["一句"], session_kind="private")
        self.assertLess(plan.delays[0], 5.0)

    def test_hold_is_clamped(self) -> None:
        cfg = FakeCfg()
        rhythm = RhythmController(cfg, FakeGuard(), None)
        plan = rhythm.build_plan(["一句"], session_kind="private", extra_hold=999.0)
        self.assertLessEqual(plan.delays[0], 25.0)


if __name__ == "__main__":
    unittest.main()
