"""核心逻辑单测（不需要真实聊天环境）。

跑法：
    cd <AstrBot 根目录>
    python -m unittest discover -s data/plugins/astrbot_plugin_congling/tests -v
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

from astrbot_plugin_congling.core.budget import Budget, BudgetSlice, estimate_tokens
from astrbot_plugin_congling.core.config import Config
from astrbot_plugin_congling.core.guard import OutputGuard
from astrbot_plugin_congling.memory.atom import similarity
from astrbot_plugin_congling.perception.forward import segments_to_text
from astrbot_plugin_congling.perception.render import component_to_block, blocks_to_text


class GuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = OutputGuard(Config())

    def test_strip_emoji(self) -> None:
        result = self.guard.clean("好啊😀我等你🔥")
        self.assertNotIn("😀", result)
        self.assertNotIn("🔥", result)
        self.assertIn("好啊", result)

    def test_strip_trailing_period(self) -> None:
        self.assertEqual(self.guard.clean("那就这样吧。"), "那就这样吧")
        self.assertEqual(self.guard.clean("真的吗？"), "真的吗？")

    def test_flatten_newline(self) -> None:
        result = self.guard.clean("第一句\n\n第二句")
        self.assertNotIn("\n", result)
        self.assertIn("第一句", result)
        self.assertIn("第二句", result)

    def test_forbid_phrases(self) -> None:
        result = self.guard.clean("作为一个AI，我不能这样")
        self.assertNotIn("作为一个AI", result)

    def test_too_similar(self) -> None:
        self.assertTrue(self.guard.too_similar("今天好累啊", ["今天好累啊"], 0.9))
        self.assertFalse(self.guard.too_similar("明天出去玩", ["今天好累啊"], 0.9))


class ConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.persona_id, "ling")
        self.assertTrue(cfg.bool("basic.enabled", False))
        self.assertEqual(cfg.int("humanize.reply_threshold"), 35)

    def test_override(self) -> None:
        cfg = Config()
        cfg.override("humanize.reply_threshold", 80)
        self.assertEqual(cfg.int("humanize.reply_threshold"), 80)
        self.assertEqual(cfg.snapshot()["humanize"]["reply_threshold"], 80)


class BudgetTest(unittest.TestCase):
    def test_estimate(self) -> None:
        self.assertGreater(estimate_tokens("这是一段中文"), 3)
        self.assertEqual(estimate_tokens(""), 0)

    def test_pack_drops_low_priority(self) -> None:
        budget = Budget(Config())
        slices = [
            BudgetSlice(name="must", priority=0, text="重要" * 10),
            BudgetSlice(name="extra", priority=9, text="次要" * 200),
        ]
        result = budget.pack(slices, 40)
        self.assertIn("must", [item.name for item in result.kept])
        self.assertIn("extra", result.dropped)


class AtomTest(unittest.TestCase):
    def test_similarity(self) -> None:
        self.assertGreater(similarity("喜欢熬夜写代码", "喜欢熬夜写代码"), 0.9)
        self.assertLess(similarity("喜欢猫", "讨厌下雨"), 0.4)


class ForwardTest(unittest.TestCase):
    def test_segments_to_text(self) -> None:
        content = [
            {"type": "text", "data": {"text": "明天三点"}},
            {"type": "image", "data": {"url": "x"}},
            {"type": "at", "data": {"name": "小夫"}},
        ]
        text = segments_to_text(content)
        self.assertIn("明天三点", text)
        self.assertIn("[图片]", text)
        self.assertIn("@小夫", text)

    def test_plain_string(self) -> None:
        self.assertEqual(segments_to_text("直接是字符串"), "直接是字符串")


class RenderTest(unittest.TestCase):
    def test_unknown_component(self) -> None:
        class Fake:
            text = "兜底文本"

        block = component_to_block(Fake())
        self.assertIsNotNone(block)
        self.assertEqual(block.text, "兜底文本")

    def test_blocks_to_text(self) -> None:
        class PlainLike:
            text = "你好"

        block = component_to_block(PlainLike())
        self.assertEqual(blocks_to_text([block]), "你好")


if __name__ == "__main__":
    unittest.main()
