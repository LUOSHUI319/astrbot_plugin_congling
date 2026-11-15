"""表情包桥：六种状态 + 按情绪取图。

状态拆分是这次的重点——「不可用」这三个字在面板上等于什么都没说。
没装插件、装了停用、库文件没生成、库里没图、读坏了，要修的地方完全不同。
"""

from __future__ import annotations

import asyncio
import pathlib
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.core import paths  # noqa: E402
from astrbot_plugin_congling.qqext.sticker_bridge import StickerBridge  # noqa: E402

SCHEMA = (
    "CREATE TABLE emoji ("
    "path TEXT PRIMARY KEY, category TEXT, desc TEXT, overlay_text TEXT, "
    "emotions_json TEXT, character TEXT, is_favorite INTEGER, "
    "use_count INTEGER, last_used_at INTEGER)"
)


def make_db(path: pathlib.Path, rows=()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    if rows:
        conn.executemany("INSERT INTO emoji VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


class FakeStore:
    def __init__(self):
        self.kv = {}

    async def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key, value):
        self.kv[key] = value


class FakeMeta:
    def __init__(self, activated=True):
        self.name = "astrbot_plugin_stealer"
        self.root_dir_name = "astrbot_plugin_stealer"
        self.activated = activated
        self.star_cls = None


class FakeCtx:
    def __init__(self, metas=None):
        self.metas = metas or []

    def get_registered_star(self, name):
        return None

    def get_all_stars(self):
        return self.metas


class FakePlugin:
    def __init__(self, metas=None):
        self.context = FakeCtx(metas)
        self.store = FakeStore()


class StickerStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.data = self.tmp / "astrbot_plugin_congling"
        self.data.mkdir(parents=True, exist_ok=True)
        paths._cache["root"] = self.data
        self.db = self.tmp / "astrbot_plugin_stealer" / "cache" / "emoji.db"

    def tearDown(self):
        paths._cache.pop("root", None)

    def bridge(self, metas=None):
        return StickerBridge(FakePlugin(metas))

    def scan(self, metas=None):
        return asyncio.run(self.bridge(metas).scan())

    # ------------------------------------------------------------ 六种状态
    def test_no_plugin(self):
        st = self.scan()
        self.assertEqual(st["state"], "no_plugin")
        self.assertFalse(st["connected"])
        self.assertIn("没装", st["error"])

    def test_disabled(self):
        st = self.scan([FakeMeta(activated=False)])
        self.assertEqual(st["state"], "disabled")
        self.assertFalse(st["connected"])

    def test_no_db(self):
        st = self.scan([FakeMeta(activated=True)])
        self.assertEqual(st["state"], "no_db")

    def test_empty(self):
        make_db(self.db)
        st = self.scan([FakeMeta(activated=True)])
        self.assertEqual(st["state"], "empty")
        self.assertTrue(st["connected"])
        self.assertEqual(st["count"], 0)

    def test_ok(self):
        make_db(self.db, [
            (str(self.tmp / "a.gif"), "tired", "累瘫趴着", "", "", "", 0, 0, 0),
            (str(self.tmp / "b.gif"), "happy", "开心大笑", "", "", "", 0, 0, 0),
        ])
        st = self.scan()
        self.assertEqual(st["state"], "ok")
        self.assertTrue(st["connected"])
        self.assertEqual(st["count"], 2)
        self.assertEqual(st["categories"], 2)
        self.assertEqual(st["via"], "db_file")

    def test_error_on_broken_db(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.db.write_bytes(b"this is not a sqlite database at all")
        st = self.scan()
        self.assertEqual(st["state"], "error")
        self.assertFalse(st["connected"])
        self.assertTrue(st["error"])

    def test_describe_covers_every_state(self):
        samples = {
            "ok": "已连接",
            "empty": "空库",
            "no_plugin": "没装",
            "disabled": "已停用",
            "no_db": "还没生成",
        }
        for state, needle in samples.items():
            text = StickerBridge.describe({"state": state, "count": 1, "categories": 1})
            self.assertIn(needle, text, f"{state} 的文案不对：{text}")
        self.assertIn("读取失败", StickerBridge.describe({"state": "error", "error": "x"}))


class StickerPickTest(unittest.TestCase):
    NAMES = [
        ("p_tired.gif", "tired", "累瘫一样一动不动", "", "", "", 0, 0, 0),
        ("p_happy.gif", "happy", "开心大笑", "", "", "", 0, 0, 0),
        ("p_dumb.gif", "dumb", "无语又无奈地吐槽", "", "", "", 0, 0, 0),
    ]

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.data = self.tmp / "astrbot_plugin_congling"
        self.data.mkdir(parents=True, exist_ok=True)
        paths._cache["root"] = self.data
        # path 必须是绝对路径，而且文件要真存在：pick 会验存在性，
        # 库里登记了却没文件的会被跳过（真实环境里清过目录就会这样）
        rows = [(str(self.tmp / name),) + tuple(rest) for name, *rest in self.NAMES]
        for name, *_ in self.NAMES:
            (self.tmp / name).write_bytes(b"GIF89a-fake")
        make_db(self.tmp / "astrbot_plugin_stealer" / "cache" / "emoji.db", rows)
        self.plugin = FakePlugin()
        self.bridge_obj = StickerBridge(self.plugin)

    def tearDown(self):
        paths._cache.pop("root", None)

    def test_pick_matches_mood(self):
        item = asyncio.run(self.bridge_obj.pick(["累"]))
        self.assertIsNotNone(item)
        self.assertEqual(item["category"], "tired")

    def test_pick_returns_none_when_no_match(self):
        self.assertIsNone(asyncio.run(self.bridge_obj.pick(["完全无关的词"])))

    def test_recent_is_excluded(self):
        async def scenario():
            first = await self.bridge_obj.pick(["累"])
            second = await self.bridge_obj.pick(["累"])
            return first, second

        first, second = asyncio.run(scenario())
        self.assertIsNotNone(first)
        # 只有一张 tired，取过就不该再取到同一张
        self.assertIsNone(second)

    def test_pick_bytes_reads_file(self):
        item = asyncio.run(self.bridge_obj.pick_bytes(["累"]))
        self.assertIsNotNone(item)
        data, suffix = item
        self.assertEqual(data, b"GIF89a-fake")
        self.assertEqual(suffix, ".gif")


if __name__ == "__main__":
    unittest.main(verbosity=2)
