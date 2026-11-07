"""图片描述回填归档。

视觉模型只有 6 秒预算，超时那一次图片块还停在 "[图片]"，直接归档的话
她事后回忆只看到占位符，就会反复追问同一张图。这里盯归档前的缓存回填：

1. 缓存里有描述 → 补进块和顶层 text，并打上 described；
2. 缓存里没有 → 保持原样，不触发模型、不报错；
3. 已经有描述 → 不覆盖；
4. 多图消息 → 只补块，不动顶层 text。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.core.contracts import Block, PerceivedMessage  # noqa: E402
from astrbot_plugin_congling.perception.media import MediaInterpreter  # noqa: E402
from astrbot_plugin_congling.perception.service import PerceptionService  # noqa: E402

URL = "https://example.com/crocodile.jpg"
KEY = "img:" + hashlib.sha1(URL.encode("utf-8", "ignore")).hexdigest()[:20]
DESC = "[图片：绿皮小鳄鱼握拳，下方文字「可恶！」]"


class MiniStore:
    def __init__(self, kv: dict | None = None) -> None:
        self.kv = dict(kv or {})

    async def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key, value):
        self.kv[key] = value


class MiniCfg:
    def bool(self, key, default=False):
        return default

    def int(self, key, default=0):
        return default

    def str(self, key, default=""):
        return default


class NoCallModels:
    """归档路径只许读缓存，模型一次都不该被叫。"""

    def __init__(self) -> None:
        self.calls = 0

    async def describe_image(self, image, prompt):
        self.calls += 1
        raise AssertionError("归档路径不该调用视觉模型")


class MiniPlugin:
    def __init__(self, store: MiniStore) -> None:
        self.cfg = MiniCfg()
        self.store = store
        self.models = NoCallModels()


def make_media(kv: dict | None = None) -> tuple[MediaInterpreter, MiniPlugin]:
    plugin = MiniPlugin(MiniStore(kv))
    return MediaInterpreter(plugin), plugin


class CachedImageTest(unittest.TestCase):
    def test_hit(self):
        media, _ = make_media({KEY: DESC})
        self.assertEqual(asyncio.run(media.cached_image(URL)), DESC)

    def test_miss(self):
        media, _ = make_media()
        self.assertEqual(asyncio.run(media.cached_image(URL)), "")

    def test_empty_url(self):
        media, _ = make_media({KEY: DESC})
        self.assertEqual(asyncio.run(media.cached_image("")), "")


class FillArchiveTest(unittest.TestCase):
    def _service(self, kv: dict | None = None):
        media, plugin = make_media(kv)
        # 只测这一个方法，绕开 __init__ 里那一串运行时依赖
        service = PerceptionService.__new__(PerceptionService)
        service.media = media
        return service, plugin

    def test_placeholder_gets_description(self):
        service, plugin = self._service({KEY: DESC})
        msg = PerceivedMessage(
            text="[图片]",
            blocks=[Block(kind="image", text="[图片]", meta={"url": URL})],
        )
        asyncio.run(service._fill_cached_image_text(msg))
        self.assertEqual(msg.blocks[0].text, DESC)
        self.assertEqual(msg.text, DESC)
        self.assertTrue(msg.blocks[0].meta.get("described"))
        self.assertEqual(plugin.models.calls, 0)

    def test_miss_keeps_placeholder(self):
        service, plugin = self._service()
        msg = PerceivedMessage(
            text="[图片]",
            blocks=[Block(kind="image", text="[图片]", meta={"url": URL})],
        )
        asyncio.run(service._fill_cached_image_text(msg))
        self.assertEqual(msg.blocks[0].text, "[图片]")
        self.assertEqual(msg.text, "[图片]")
        self.assertEqual(plugin.models.calls, 0)

    def test_existing_description_not_overwritten(self):
        service, _ = self._service({KEY: DESC})
        msg = PerceivedMessage(
            text="[图片：她原本就看到了]",
            blocks=[Block(kind="image", text="[图片：她原本就看到了]", meta={"url": URL})],
        )
        asyncio.run(service._fill_cached_image_text(msg))
        self.assertEqual(msg.blocks[0].text, "[图片：她原本就看到了]")
        self.assertEqual(msg.text, "[图片：她原本就看到了]")

    def test_multi_image_keeps_top_text(self):
        service, _ = self._service({KEY: DESC})
        second = "https://example.com/second.jpg"
        msg = PerceivedMessage(
            text="[图片][图片]",
            blocks=[
                Block(kind="image", text="[图片]", meta={"url": URL}),
                Block(kind="image", text="[图片]", meta={"url": second}),
            ],
        )
        asyncio.run(service._fill_cached_image_text(msg))
        self.assertEqual(msg.blocks[0].text, DESC)
        # 第二张没缓存，保持占位符
        self.assertEqual(msg.blocks[1].text, "[图片]")
        # 多图时顶层 text 不整体替换，免得丢图
        self.assertEqual(msg.text, "[图片][图片]")


if __name__ == "__main__":
    unittest.main()
