"""0.5.0 新增逻辑单测：记忆等级 / 遗忘曲线 / 好感度 / 主体识别。

跑法：
    cd <AstrBot 根目录>
    python -m unittest discover -s data/plugins/astrbot_plugin_congling/tests -p "test_memory_level.py" -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = PLUGIN_ROOT.parent
for path in (str(PLUGIN_ROOT), str(PLUGINS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from astrbot_plugin_congling.core.config import Config
from astrbot_plugin_congling.core.contracts import MemoryDraft
from astrbot_plugin_congling.memory.forgetting import ForgettingCurve
from astrbot_plugin_congling.memory.grade import LevelJudge


class FakePlugin:
    def __init__(self, raw=None) -> None:
        self.cfg = Config(raw or {})


class LevelJudgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.judge = LevelJudge(FakePlugin())

    def _draft(self, text: str, **kwargs) -> MemoryDraft:
        return MemoryDraft(text=text, **kwargs)

    def test_taboo_goes_zero(self) -> None:
        draft = self._draft("别碰这个话题", level_tags=["taboo"], level=3)
        level, reason, tags = self.judge.judge_one(draft, stage=5)
        self.assertEqual(level, 0)
        self.assertIn("taboo", tags)
        self.assertTrue(reason)

    def test_identity_floor(self) -> None:
        draft = self._draft("我叫小林，在做前端", level_tags=["identity"], level=1)
        level, _, _ = self.judge.judge_one(draft, stage=0)
        self.assertGreaterEqual(level, 4)

    def test_routine_capped(self) -> None:
        draft = self._draft("早安", level_tags=["routine"], level=5)
        level, _, _ = self.judge.judge_one(draft, stage=5)
        self.assertLessEqual(level, 2)

    def test_affinity_boost(self) -> None:
        low = self._draft("他养了一只猫", level_tags=[], level=3)
        high = self._draft("他养了一只猫", level_tags=[], level=3)
        l1, _, _ = self.judge.judge_one(low, stage=0)
        l2, _, _ = self.judge.judge_one(high, stage=5)
        self.assertGreater(l2, l1)

    def test_rule_tags_fallback(self) -> None:
        tags = LevelJudge.rule_tags("别忘了周五的约定")
        self.assertIn("promise", tags)
        self.assertIn("user_stress", tags)

    def test_judge_all_keeps_level_in_range(self) -> None:
        drafts = [
            MemoryDraft(text="普通一句", level=3),
            MemoryDraft(text="我喜欢吃辣", level=2),
            MemoryDraft(text="哈哈哈", level=5),
        ]
        out = self.judge.judge_all(drafts, stage=3)
        for item in out:
            self.assertTrue(0 <= item.level <= 5)


class ForgettingCurveTest(unittest.TestCase):
    def _curve(self, mode: str) -> ForgettingCurve:
        return ForgettingCurve(FakePlugin({"memory": {"forgetting": {"mode": mode}}}))

    def test_off_never_decays(self) -> None:
        curve = self._curve("off")
        self.assertEqual(curve.decay_at(3, 999), 1.0)

    def test_zero_level_never_decays(self) -> None:
        curve = self._curve("balanced")
        self.assertEqual(curve.decay_at(0, 99999), 1.0)

    def test_balanced_between_dream_and_ebbinghaus(self) -> None:
        dream = self._curve("dream").decay_at(3, 20)
        balanced = self._curve("balanced").decay_at(3, 20)
        ebbing = self._curve("ebbinghaus").decay_at(3, 20)
        self.assertLessEqual(dream, balanced)
        self.assertLessEqual(balanced, ebbing)

    def test_level_one_dies_first(self) -> None:
        curve = self._curve("balanced")
        self.assertLess(curve.decay_at(1, 10), curve.decay_at(5, 10))

    def test_hits_extend_life(self) -> None:
        curve = self._curve("ebbinghaus")
        self.assertLess(curve.decay_at(3, 20, 0), curve.decay_at(3, 20, 3))

    def test_status_thresholds(self) -> None:
        curve = self._curve("balanced")
        self.assertEqual(curve.status_for(0.9), "active")
        self.assertEqual(curve.status_for(0.3), "archived")
        self.assertEqual(curve.status_for(0.05), "forgotten")

    def test_preview_shape(self) -> None:
        data = self._curve("balanced").preview(10)
        self.assertEqual(data["mode"], "balanced")
        self.assertEqual(len(data["series"]["3"]), 11)


if __name__ == "__main__":
    unittest.main()
