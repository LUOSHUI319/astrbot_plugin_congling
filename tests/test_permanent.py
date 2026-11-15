"""永久记忆判定的单测。

跑法：
    cd <AstrBot 根目录>
    python -m unittest discover -s data/plugins/astrbot_plugin_congling/tests -p "test_permanent.py" -v

这一版把「永久记忆」从「taboo 标签一票通过」改成四条判据，
这个文件锁住每条判据的边界，尤其是**不该进永久档**的那些。
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
from astrbot_plugin_congling.memory.affinity import Affinity
from astrbot_plugin_congling.memory.grade import LevelJudge
from astrbot_plugin_congling.memory.permanent import (
    has_clear_target,
    hard_reason,
    judge,
    kin_reason,
)


class FakePlugin:
    def __init__(self, raw=None) -> None:
        self.cfg = Config(raw or {})


# ---------------------------------------------------------------- 明确对象
class ClearTargetTest(unittest.TestCase):
    """「有没有明确对象」是挡住误判的第一道关。"""

    def test_explicit_prohibition_ok(self) -> None:
        for text in (
            "不要动 aaa 备份文件夹",
            "群里不许发可能涉及擦边的信息",
            "别碰这个话题",
            "不准用我的号发东西",
            "禁止在群里发广告",
        ):
            with self.subTest(text=text):
                self.assertTrue(has_clear_target(text))

    def test_vague_words_rejected(self) -> None:
        # 这些在中文里出现频率极高，全都不是「禁令」
        for text in (
            "别提了，那天累死我",
            "别管我",
            "算了算了",
            "没事，不说了",
            "别急，马上到",
        ):
            with self.subTest(text=text):
                self.assertFalse(has_clear_target(text))

    def test_too_short_rejected(self) -> None:
        for text in ("别走", "好的", "", None):
            with self.subTest(text=text):
                self.assertFalse(has_clear_target(text))

    def test_explicit_zone_ok(self) -> None:
        for text in ("这是我的底线，不能退", "那块是禁区", "群里有个规矩要守"):
            with self.subTest(text=text):
                self.assertTrue(has_clear_target(text))


# ---------------------------------------------------------------- 硬判据
class HardReasonTest(unittest.TestCase):
    def test_taboo_with_target(self) -> None:
        ok, why = hard_reason({"taboo"}, "不要动 aaa 备份文件夹")
        self.assertTrue(ok)
        self.assertIn("禁令", why)

    def test_taboo_without_target(self) -> None:
        ok, why = hard_reason({"taboo"}, "别提了")
        self.assertFalse(ok)
        self.assertIn("明确对象", why)

    def test_identity_not_permanent(self) -> None:
        # 身份锚点走 4 级，不占永久档
        ok, _ = hard_reason({"identity"}, "我叫望山")
        self.assertFalse(ok)

    def test_promise_not_permanent(self) -> None:
        ok, _ = hard_reason({"promise"}, "答应周六下午三点等你")
        self.assertFalse(ok)


# ---------------------------------------------------------------- 核心关系人
class KinReasonTest(unittest.TestCase):
    CORE = {"3190568934": (1000.0, "望山"), "1824843202": (615.6, "林潇潇")}

    def test_core_with_strong_kind(self) -> None:
        ok, why = kin_reason("3190568934", "promise", 0.92, self.CORE)
        self.assertTrue(ok)
        self.assertIn("望山", why)

    def test_weak_kind_rejected(self) -> None:
        # 事实类太多，不能都进永久档
        for kind in ("fact", "event", "emotion"):
            with self.subTest(kind=kind):
                ok, _ = kin_reason("3190568934", kind, 0.95, self.CORE)
                self.assertFalse(ok)

    def test_low_importance_rejected(self) -> None:
        ok, _ = kin_reason("3190568934", "promise", 0.7, self.CORE)
        self.assertFalse(ok)

    def test_outsider_rejected(self) -> None:
        ok, _ = kin_reason("999999", "promise", 0.99, self.CORE)
        self.assertFalse(ok)

    def test_empty_subject_rejected(self) -> None:
        ok, _ = kin_reason("", "promise", 0.99, self.CORE)
        self.assertFalse(ok)

    def test_no_core_list(self) -> None:
        ok, _ = kin_reason("3190568934", "promise", 0.99, None)
        self.assertFalse(ok)


# ---------------------------------------------------------------- 组合判定
class JudgeTest(unittest.TestCase):
    CORE = {"3190568934": (1000.0, "望山"), "1824843202": (615.6, "林潇潇")}

    def test_prohibition_wins(self) -> None:
        ok, why = judge(tags={"taboo"}, text="不要动 aaa 文件夹", importance=0.9, core=self.CORE)
        self.assertTrue(ok)
        self.assertIn("禁令", why)

    def test_kin_path(self) -> None:
        ok, why = judge(
            tags=[], text="望山说想周末一起去看展", subject_id="3190568934",
            kind="promise", importance=0.92, core=self.CORE,
        )
        self.assertTrue(ok)
        self.assertIn("望山", why)

    def test_neither(self) -> None:
        ok, _ = judge(
            tags=[], text="今天天气不错", subject_id="999999",
            kind="fact", importance=0.5, core=self.CORE,
        )
        self.assertFalse(ok)

    def test_vague_taboo_falls_through(self) -> None:
        # 有 taboo 标签但没明确对象，且主体不是核心关系人 → 不永久
        ok, _ = judge(
            tags={"taboo"}, text="别提了，累死我", subject_id="999999",
            kind="fact", importance=0.9, core=self.CORE,
        )
        self.assertFalse(ok)


# ---------------------------------------------------------------- 等级落地
class JudgeOneTest(unittest.TestCase):
    """判据接进 LevelJudge 之后，最终等级对不对。"""

    CORE = {"3190568934": (1000.0, "望山")}

    def setUp(self) -> None:
        self.judge = LevelJudge(FakePlugin())

    def _one(self, text, tags, *, subject="3190568934", kind="fact", importance=0.9, stage=3):
        draft = MemoryDraft(text=text, kind=kind, importance=importance, confidence=0.9, level_tags=tags)
        draft.subject_key = subject
        return self.judge.judge_one(draft, stage=stage, core=self.CORE)

    def test_clear_prohibition_is_zero(self) -> None:
        level, why, _ = self._one("不要动 aaa 备份文件夹", ["taboo"])
        self.assertEqual(level, 0)
        self.assertIn("禁令", why)

    def test_vague_taboo_not_zero(self) -> None:
        # 这是这一版修掉的主要误判
        level, _, _ = self._one("别提了，那天累死我", ["taboo"])
        self.assertNotEqual(level, 0)

    def test_identity_floor(self) -> None:
        level, _, _ = self._one("我叫望山", ["identity"])
        self.assertGreaterEqual(level, 4)
        self.assertNotEqual(level, 0)

    def test_kin_promise_zero(self) -> None:
        level, why, _ = self._one("望山说想周末去看展", [], kind="promise", importance=0.92)
        self.assertEqual(level, 0)
        self.assertIn("核心关系人", why)

    def test_kin_fact_not_zero(self) -> None:
        level, _, _ = self._one("望山今天吃了面", [], kind="fact", importance=0.92)
        self.assertNotEqual(level, 0)

    def test_no_core_falls_back(self) -> None:
        # 不传 core 时行为退回纯标签判定
        draft = MemoryDraft(text="望山说想周末去看展", kind="promise", importance=0.95, confidence=0.9, level_tags=[])
        draft.subject_key = "3190568934"
        level, _, _ = self.judge.judge_one(draft, stage=3)
        self.assertNotEqual(level, 0)


# ---------------------------------------------------------------- 核心关系人名单
class CorePeopleTest(unittest.IsolatedAsyncioTestCase):
    """满 600 的全算；不足三位取排行榜前三。"""

    def _service(self, board):
        svc = Affinity.__new__(Affinity)

        async def fake_leaderboard(limit=10):
            return board[:limit]

        svc.leaderboard = fake_leaderboard
        return svc

    def _row(self, key, score, name=None):
        return {"subject_key": key, "score": score, "stage": 3, "stage_name": "朋友", "name": name or key}

    async def test_three_above_threshold(self) -> None:
        board = [self._row("a", 1000), self._row("b", 900), self._row("c", 615), self._row("d", 300)]
        core = await self._service(board).core_people()
        self.assertEqual(set(core), {"a", "b", "c"})

    async def test_more_than_three_above(self) -> None:
        board = [self._row("a", 1000), self._row("b", 900), self._row("c", 800), self._row("d", 700)]
        core = await self._service(board).core_people()
        self.assertEqual(set(core), {"a", "b", "c", "d"})

    async def test_pad_when_short(self) -> None:
        # 只有一个人满 600，补到前三位
        board = [self._row("a", 1000), self._row("b", 300), self._row("c", 200), self._row("d", 100)]
        core = await self._service(board).core_people()
        self.assertEqual(set(core), {"a", "b", "c"})

    async def test_nobody_above(self) -> None:
        # 新装的机器谁都没到 600，仍然取前三
        board = [self._row("a", 300), self._row("b", 200), self._row("c", 100)]
        core = await self._service(board).core_people()
        self.assertEqual(set(core), {"a", "b", "c"})

    async def test_empty_board(self) -> None:
        core = await self._service([]).core_people()
        self.assertEqual(core, {})

    async def test_custom_threshold(self) -> None:
        board = [self._row("a", 500), self._row("b", 400), self._row("c", 300), self._row("d", 200)]
        core = await self._service(board).core_people(threshold=450, at_least=2)
        self.assertEqual(set(core), {"a", "b"})

    async def test_name_kept(self) -> None:
        board = [self._row("3190568934", 1000, "望山")]
        core = await self._service(board).core_people()
        self.assertIn("3190568934", core)
        score, name = core["3190568934"]
        self.assertEqual(name, "望山")
        self.assertAlmostEqual(score, 1000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
