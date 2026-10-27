"""睡眠阶段的边界测试（docs/sleep_stages_plan.md 第 1 步）。

重点是「首尾对齐、不重不漏」——阶段是算出来的，一旦中间漏一分钟或者多算一段，
面板上的甘特条就会出现空档，或者「现在」落在两段之间。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.night import stages  # noqa: E402

HOUR = 3600


class Cfg:
    def __init__(self, data: dict | None = None) -> None:
        self.data = {
            "night.stage_enabled": True,
            "night.stage_cycle_minutes": 90,
            "night.stage_n1_minutes": 8,
            "night.stage_deep_max_minutes": 24,
            "night.stage_deep_decay": 0.32,
            "night.stage_rem_base_minutes": 10,
            "night.stage_rem_grow_minutes": 6,
            "night.stage_rem_max_minutes": 28,
            "night.stage_noon_light_only": True,
        }
        self.data.update(data or {})

    def _get(self, key, default):
        return self.data.get(key, default)

    def bool(self, key, default=False):
        return bool(self._get(key, default))

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

    def str(self, key, default=""):
        return str(self._get(key, default))


SLEEP_AT = 1793029013  # 10-26 23:36:53
NIGHT_WAKE = SLEEP_AT + 8 * HOUR


def plan(**cfg):
    return stages.build_plan(Cfg(cfg), sleep_at=SLEEP_AT, wake_at=NIGHT_WAKE, kind="night")


class PlanShapeTest(unittest.TestCase):
    def test_covers_every_second(self) -> None:
        rows = plan()
        self.assertTrue(rows)
        self.assertEqual(rows[0]["start"], SLEEP_AT)
        self.assertEqual(rows[-1]["end"], NIGHT_WAKE)
        for prev, nxt in zip(rows, rows[1:]):
            self.assertEqual(prev["end"], nxt["start"], "段与段之间不能有空档也不能重叠")

    def test_every_stage_is_known(self) -> None:
        for seg in plan():
            self.assertIn(seg["key"], stages.STAGE_KEYS)
            self.assertEqual(seg["label"], stages.STAGE_CN[seg["key"]])

    def test_first_segment_is_falling_asleep(self) -> None:
        first = plan()[0]
        self.assertEqual(first["key"], "n1")
        self.assertEqual(first["cycle"], 0)
        self.assertEqual(first["end"] - first["start"], 8 * 60)

    def test_deep_sleep_shrinks_and_rem_grows(self) -> None:
        rows = plan()
        deep = {}
        rem = {}
        for seg in rows:
            if seg["key"] == "n3":
                deep[seg["cycle"]] = deep.get(seg["cycle"], 0) + (seg["end"] - seg["start"])
            if seg["key"] == "rem":
                rem[seg["cycle"]] = rem.get(seg["cycle"], 0) + (seg["end"] - seg["start"])
        self.assertGreater(deep.get(0, 0), deep.get(1, 0))
        self.assertLess(rem.get(0, 0), rem.get(2, 0))
        self.assertGreaterEqual(len(deep), 2)
        self.assertGreaterEqual(len(rem), 3)

    def test_stats_add_up_to_total(self) -> None:
        rows = plan()
        total = sum(stages.stats(rows).values())
        self.assertEqual(total, 8 * 60)

    def test_noon_only_light_sleep(self) -> None:
        rows = stages.build_plan(
            Cfg(), sleep_at=SLEEP_AT, wake_at=SLEEP_AT + 60 * 60, kind="noon"
        )
        keys = {seg["key"] for seg in rows}
        self.assertNotIn("n3", keys)
        self.assertIn("n2", keys)
        self.assertEqual(rows[0]["start"], SLEEP_AT)
        self.assertEqual(rows[-1]["end"], SLEEP_AT + 60 * 60)

    def test_short_noon_has_no_rem(self) -> None:
        rows = stages.build_plan(
            Cfg(), sleep_at=SLEEP_AT, wake_at=SLEEP_AT + 15 * 60, kind="noon"
        )
        self.assertNotIn("rem", {seg["key"] for seg in rows})

    def test_disabled_returns_nothing(self) -> None:
        rows = stages.build_plan(
            Cfg({"night.stage_enabled": False}), sleep_at=SLEEP_AT, wake_at=NIGHT_WAKE
        )
        self.assertEqual(rows, [])

    def test_degenerate_config_still_covers(self) -> None:
        rows = stages.build_plan(
            Cfg(
                {
                    "night.stage_cycle_minutes": 20,
                    "night.stage_deep_max_minutes": 60,
                    "night.stage_rem_base_minutes": 30,
                }
            ),
            sleep_at=SLEEP_AT,
            wake_at=SLEEP_AT + 90 * 60,
        )
        self.assertTrue(rows)
        self.assertEqual(rows[0]["start"], SLEEP_AT)
        self.assertEqual(rows[-1]["end"], SLEEP_AT + 90 * 60)
        for prev, nxt in zip(rows, rows[1:]):
            self.assertEqual(prev["end"], nxt["start"])


class QueryTest(unittest.TestCase):
    def test_at_returns_matching_segment(self) -> None:
        rows = plan()
        target = rows[1]
        got = stages.at(Cfg(), sleep_at=SLEEP_AT, wake_at=NIGHT_WAKE, now=target["start"] + 30)
        self.assertIsNotNone(got)
        self.assertEqual(got["key"], target["key"])
        self.assertEqual(got["since"], target["start"])
        self.assertEqual(got["until"], target["end"])
        self.assertAlmostEqual(got["progress"], 30 / (target["end"] - target["start"]), places=2)

    def test_before_sleep_falls_back_to_first(self) -> None:
        got = stages.at(Cfg(), sleep_at=SLEEP_AT, wake_at=NIGHT_WAKE, now=SLEEP_AT - 600)
        self.assertEqual(got["key"], "n1")
        self.assertEqual(got["progress"], 0.0)

    def test_after_wake_falls_back_to_last(self) -> None:
        got = stages.at(Cfg(), sleep_at=SLEEP_AT, wake_at=NIGHT_WAKE, now=NIGHT_WAKE + 600)
        self.assertEqual(got["until"], NIGHT_WAKE)
        self.assertEqual(got["progress"], 1.0)

    def test_no_sleep_no_stage(self) -> None:
        self.assertIsNone(stages.at(Cfg(), sleep_at=0, wake_at=0, now=SLEEP_AT))

    def test_describe(self) -> None:
        got = stages.at(Cfg(), sleep_at=SLEEP_AT, wake_at=NIGHT_WAKE, now=SLEEP_AT + 30 * 60)
        text = stages.describe(got)
        self.assertIn("个周期", text)

    def test_seq_starts_from_now(self) -> None:
        now = SLEEP_AT + 3 * HOUR
        rows = stages.seq(plan(), now=now)
        self.assertTrue(rows)
        self.assertGreater(rows[0]["end"], now)
        self.assertEqual(rows[-1]["end"], NIGHT_WAKE)


class FactorTest(unittest.TestCase):
    def test_factors(self) -> None:
        self.assertAlmostEqual(stages.energy_factor({"key": "n3"}), 2.2)
        self.assertLess(stages.wake_factor({"key": "n3"}), stages.wake_factor({"key": "n1"}))
        self.assertAlmostEqual(stages.energy_factor(None), 1.0)
        self.assertAlmostEqual(stages.wake_factor(None), 1.0)


if __name__ == "__main__":
    unittest.main()
