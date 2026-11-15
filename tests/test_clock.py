"""时间来源：北京时间 vs 本机时间。

这块最怕的是「看着对、其实偏了」——所以断言不用字符串比，而是拿一个确定的
UTC 时间戳，看它落在当天第几分钟、以及是否被判进夜间疲劳窗口。
"""

from __future__ import annotations

import pathlib
import sys
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.core import clock  # noqa: E402
from astrbot_plugin_congling.night import energy  # noqa: E402

# 2026-11-14 15:30 UTC = 北京 23:30
TS = datetime(2026, 11, 14, 15, 30, tzinfo=timezone.utc).timestamp()


class FakeCfg:
    def __init__(self, **kv):
        self.d = {"night.night_fatigue": "23:30-07:00", "night.noon_fatigue": "13:00-14:30",
                  "night.wake_time": "08:00", "night.wake_jitter_minutes": 0}
        self.d.update(kv)

    def str(self, key, default=""):
        return str(self.d.get(key, default))

    def int(self, key, default=0):
        return int(self.d.get(key, default))

    def bool(self, key, default=False):
        return bool(self.d.get(key, default))

    def float(self, key, default=0.0):
        return float(self.d.get(key, default))


class ClockBeijingTest(unittest.TestCase):
    def setUp(self):
        clock.set_source("beijing")

    def tearDown(self):
        clock.set_source("beijing")

    def test_minute_of_day_is_beijing(self):
        self.assertEqual(clock.minute_of_day(TS), 23 * 60 + 30)

    def test_hour_and_day_key(self):
        self.assertEqual(clock.hour_of(TS), 23)
        self.assertEqual(clock.day_key(TS), "2026-11-14")

    def test_late_night_belongs_to_next_beijing_day(self):
        # UTC 16:30 = 北京次日 00:30，日期必须跨过去
        late = datetime(2026, 11, 14, 16, 30, tzinfo=timezone.utc).timestamp()
        self.assertEqual(clock.day_key(late), "2026-11-15")
        self.assertEqual(clock.hour_of(late), 0)

    def test_day_start_is_beijing_midnight(self):
        start = clock.day_start(TS)
        self.assertEqual(clock.fmt("%H:%M", start), "00:00")
        self.assertEqual(clock.day_key(start), "2026-11-14")
        # 北京 0 点 = UTC 前一天 16:00
        self.assertEqual(int(start), int(datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc).timestamp()))

    def test_23_30_falls_in_night_fatigue(self):
        cfg = FakeCfg()
        self.assertEqual(energy.fatigue_kind(cfg, TS), "night")
        self.assertEqual(energy.now_minute(TS), 1410)

    def test_08_00_wake_lands_on_beijing_clock(self):
        cfg = FakeCfg(**{"night.wake_time": "08:00", "night.wake_jitter_minutes": 0})
        # 北京 23:30 睡下 → 次日 08:00 醒
        wake = energy.next_wake_time(cfg, TS)
        self.assertEqual(clock.fmt("%Y-%m-%d %H:%M", wake), "2026-11-15 08:00")

    def test_at_hhmm(self):
        self.assertEqual(clock.fmt("%H:%M", clock.at_hhmm("07:05", TS)), "07:05")
        self.assertEqual(clock.at_hhmm("99:99", TS), 0)


class ClockLocalTest(unittest.TestCase):
    def tearDown(self):
        clock.set_source("beijing")

    def test_local_matches_system(self):
        clock.set_source("local")
        stamp = time.localtime(TS)
        self.assertEqual(clock.minute_of_day(TS), stamp.tm_hour * 60 + stamp.tm_min)
        self.assertEqual(clock.day_key(TS), time.strftime("%Y-%m-%d", stamp))

    def test_unknown_source_falls_back_to_beijing(self):
        self.assertEqual(clock.set_source("whatever"), "beijing")

    def test_configure_reads_config(self):
        cfg = FakeCfg()
        cfg.d["clock.source"] = "local"
        self.assertEqual(clock.configure(cfg), "local")
        clock.set_source("beijing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
