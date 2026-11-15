"""发说说时的自动配图。

这一段的原则：**配图是情绪的标点，不是装饰。**
正文里没有情绪词就不配；表情包库没连上、没匹到、上传失败，一律退回纯文本。

整条链路尽力而为——配不上图绝不能把整条说说卡死。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.qqext import qzone as qzone_mod  # noqa: E402
from astrbot_plugin_congling.qqext.qzone import QzoneBridge, mood_words  # noqa: E402


class FakeCfg:
    def __init__(self, **kv):
        self.d = {"qq_extra.qzone_image_from_meme": True}
        self.d.update(kv)

    def bool(self, key, default=False):
        return bool(self.d.get(key, default))

    def str(self, key, default=""):
        return str(self.d.get(key, default))

    def int(self, key, default=0):
        return int(self.d.get(key, default))

    def list(self, key, default=None):
        return list(self.d.get(key, default if default is not None else []))


class FakeSticker:
    def __init__(self, pick=("GIF89a", ".gif")):
        self.pick = pick
        self.calls = []

    async def pick_bytes(self, moods):
        self.calls.append(list(moods))
        return self.pick


class FakeQQExt:
    def __init__(self, sticker):
        self.sticker = sticker


class FakePlugin:
    def __init__(self, sticker=None, **kv):
        self.cfg = FakeCfg(**kv)
        self.qqext = FakeQQExt(sticker)


class FakeAuth:
    uin = 3404044656
    gtk = "123"
    usable = True

    def header(self):
        return ""


class FakeClient:
    """上传成功的替身。真实上传会在空间图床留图，测试里不能干这事。"""
    uploaded = []

    def __init__(self, auth, **kw):
        pass

    async def upload_image(self, data, filename="image.jpg"):
        FakeClient.uploaded.append((data, filename))
        return {"richval": "RV", "pic_bo": "PB", "type": "1"}


class FakeClientFail(FakeClient):
    async def upload_image(self, data, filename="image.jpg"):
        return None


def make_bridge(plugin, client_cls=FakeClient):
    bridge = QzoneBridge.__new__(QzoneBridge)
    bridge.plugin = plugin
    bridge.config = plugin.cfg
    bridge._last_error = ""
    bridge._auth = FakeAuth()

    async def fake_auth(force: bool = False):
        return FakeAuth()

    bridge.auth = fake_auth  # type: ignore[method-assign]
    return bridge


class MoodWordsTest(unittest.TestCase):
    def test_finds_chinese_word_and_category(self):
        words = mood_words("今天上班累死了")
        self.assertIn("累", words)
        self.assertIn("tired", words)

    def test_no_emotion_returns_empty(self):
        self.assertEqual(mood_words("明早八点开会"), [])
        self.assertEqual(mood_words(""), [])

    def test_multiple_moods(self):
        words = mood_words("又累又有点无语")
        self.assertIn("tired", words)
        self.assertIn("dumb", words)

    def test_dedup(self):
        words = mood_words("累，累，真的累")
        self.assertEqual(words.count("累"), 1)
        self.assertEqual(words.count("tired"), 1)


class MemeImageTest(unittest.TestCase):
    def setUp(self):
        FakeClient.uploaded = []

    def run_pick(self, text, bridge, client_cls=FakeClient):
        async def scenario():
            with patch.object(qzone_mod, "QzoneClient", client_cls):
                return await bridge._meme_images(text)

        return asyncio.run(scenario())

    def test_picks_and_uploads_when_mood_matches(self):
        sticker = FakeSticker()
        bridge = make_bridge(FakePlugin(sticker))
        images = self.run_pick("今天好累", bridge)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["richval"], "RV")
        self.assertEqual(images[0]["pic_bo"], "PB")
        self.assertTrue(sticker.calls, "没去表情包库挑")

    def test_skips_when_no_mood(self):
        sticker = FakeSticker()
        bridge = make_bridge(FakePlugin(sticker))
        images = self.run_pick("明早八点开会", bridge)
        self.assertEqual(images, [])
        self.assertEqual(sticker.calls, [], "没情绪词却去挑图了")

    def test_skips_when_switch_off(self):
        sticker = FakeSticker()
        bridge = make_bridge(FakePlugin(sticker, **{"qq_extra.qzone_image_from_meme": False}))
        images = self.run_pick("今天好累", bridge)
        self.assertEqual(images, [])
        self.assertEqual(sticker.calls, [])

    def test_skips_when_sticker_missing(self):
        bridge = make_bridge(FakePlugin(None))
        self.assertEqual(self.run_pick("今天好累", bridge), [])

    def test_skips_when_pick_returns_none(self):
        sticker = FakeSticker(pick=None)
        bridge = make_bridge(FakePlugin(sticker))
        self.assertEqual(self.run_pick("今天好累", bridge), [])

    def test_upload_failure_falls_back_to_text(self):
        sticker = FakeSticker()
        bridge = make_bridge(FakePlugin(sticker))
        images = self.run_pick("今天好累", bridge, client_cls=FakeClientFail)
        self.assertEqual(images, [], "上传失败应该退回纯文本，不能返回半条数据")


if __name__ == "__main__":
    unittest.main(verbosity=2)
