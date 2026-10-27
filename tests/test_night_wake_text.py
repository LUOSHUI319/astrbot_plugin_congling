"""醒来文案（0.7.0 · F1）。

午睡睡下去，状态栏不能报「明天早上 07:00-09:00 醒」——那是夜里那一觉的起床点。
日志记的是 wake_at（真实时间），面板必须跟日志一致。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.core.contracts import NIGHT_ASLEEP, NIGHT_AWAKE, NightState  # noqa: E402
from astrbot_plugin_congling.night.state import NightService  # noqa: E402


class FakeCfg:
    """只实现 NightService 用到的那几个读法。"""

    def __init__(self, data: dict | None = None) -> None:
        self.data = {
            "night.enabled": True,
            "night.wake_time": "08:00",
            "night.wake_jitter_minutes": 0,
            "night.energy_max": 100,
            "night.energy_after_wake": 35,
            "night.crawl_enabled": True,
            "night.waking_timeout_minutes": 5,
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


class FakeStore:
    def __init__(self) -> None:
        self.kv: dict[str, object] = {}
        self.rows: list[str] = []

    async def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key, value):
        self.kv[key] = value

    async def fetch_all(self, sql, params=()):
        return [{"session_id": sid} for sid in self.rows]

    async def execute(self, sql, params=()):
        return 1

    async def scalar(self, sql, params=(), default=0):
        return 0


class FakePlugin:
    def __init__(self) -> None:
        self.cfg = FakeCfg()
        self.store = FakeStore()


def make_service() -> NightService:
    service = NightService(FakePlugin())
    service.config = service.plugin.cfg
    return service


class WakeLabelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()

    def test_noon_nap_reports_real_clock_not_morning_window(self) -> None:
        now = time.time()
        self.service.state.mode = NIGHT_ASLEEP
        self.service.state.sleep_kind = "noon"
        self.service.state.sleep_at = int(now - 20 * 60)
        self.service.state.wake_at = int(now + 40 * 60)

        data = self.service.snapshot()
        expected = time.strftime("%H:%M", time.localtime(self.service.state.wake_at))

        self.assertEqual(data["wake_kind"], "noon")
        self.assertEqual(data["wake_label"], expected)
        self.assertEqual(data["wake_window"], "")
        self.assertEqual(data["wake_at_clock"], expected)
        # 午睡最多一小时，日期只可能是今天或（跨零点时）明天，绝不该出现早上窗口
        self.assertIn(data["wake_day_label"], ("今天", "明天"))

    def test_day_label(self) -> None:
        base = time.mktime((2026, 10, 26, 13, 0, 0, 0, 0, -1))
        self.assertEqual(NightService._day_label(int(base + 3600), base), "今天")
        self.assertEqual(NightService._day_label(int(base + 86400 + 3600), base), "明天")
        self.assertEqual(NightService._day_label(int(base + 2 * 86400 + 3600), base), "后天")

    def test_night_sleep_keeps_window(self) -> None:
        now = time.time()
        self.service.state.mode = NIGHT_ASLEEP
        self.service.state.sleep_kind = "night"
        self.service.state.sleep_at = int(now - 60)
        self.service.state.wake_at = int(now + 6 * 3600)

        data = self.service.snapshot()
        self.assertEqual(data["wake_kind"], "night")
        # 夜睡仍然给起床点文案（这次测试里 jitter=0，所以是一个点）
        self.assertEqual(data["wake_label"], "08:00")

    def test_status_text_matches_label(self) -> None:
        now = time.time()
        self.service.state.mode = NIGHT_ASLEEP
        self.service.state.sleep_kind = "noon"
        self.service.state.sleep_at = int(now - 10 * 60)
        self.service.state.wake_at = int(now + 50 * 60)
        text = self.service.status_text()
        self.assertNotIn("07:00-09:00", text)
        self.assertIn(time.strftime("%H:%M", time.localtime(self.service.state.wake_at)), text)

    def test_sleep_kind_survives_restart(self) -> None:
        state = NightState(mode=NIGHT_ASLEEP, sleep_kind="noon", sleep_at=100, wake_at=200)
        state.pending_crawl = ["111", "222"]
        again = NightState.from_dict(state.to_dict())
        self.assertEqual(again.sleep_kind, "noon")
        self.assertEqual(again.pending_crawl, ["111", "222"])

    def test_awake_has_no_stale_kind(self) -> None:
        self.service.state.mode = NIGHT_AWAKE
        self.service.state.sleep_kind = ""
        data = self.service.snapshot()
        self.assertEqual(data["wake_kind"], "night")
        self.assertEqual(data["pending_crawl"], 0)


if __name__ == "__main__":
    unittest.main()
