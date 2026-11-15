"""图片登记表的 kind 分家（avatar / scene / meme）。

这张表原来只装头像，空间要配生活图之后加了一列 `kind` 复用同一张表。
这里锁住的是那条**硬约束**：

    换脸只能从头像里挑。

不加这条，哪天她拿一张窗外的雨当头像就换上了。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.core import paths  # noqa: E402
from astrbot_plugin_congling.core.store import Store  # noqa: E402
from astrbot_plugin_congling.qqext.avatar_pool import AvatarPool  # noqa: E402

# 只要不是空、不超过 8MB 就行，add_bytes 不校验真实格式
FAKE_JPG = b"\xff\xd8\xff\xe0" + b"congling-test" * 4


class FakeCfg:
    persona_id = "ling"

    def __init__(self, **kv):
        self.d = {"qq_extra.avatar_pool_limit": 30, "qq_extra.gallery_limit": 60}
        self.d.update(kv)

    def int(self, key, default=0):
        try:
            return int(self.d.get(key, default))
        except (TypeError, ValueError):
            return default

    def bool(self, key, default=False):
        return bool(self.d.get(key, default))

    def str(self, key, default=""):
        return str(self.d.get(key, default))


class FakePlugin:
    def __init__(self, conf):
        self.cfg = conf
        self.store = None
        self.context = None


class ImageKindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        paths._cache["root"] = self.tmp
        self.p = FakePlugin(FakeCfg())
        self.store = Store(self.tmp / "t.db")
        self.async_run(self.store.open())
        self.p.store = self.store
        self.pool = AvatarPool(self.p)

    def tearDown(self):
        try:
            self.async_run(self.store.close())
        except Exception:
            pass
        paths._cache.pop("root", None)

    def async_run(self, coro):
        return asyncio.run(coro)

    # ------------------------------------------------------------ 入库
    def test_scene_goes_to_gallery_folder(self):
        async def scenario():
            res = await self.pool.add_bytes(FAKE_JPG, suffix=".jpg", kind="scene")
            self.assertTrue(res.get("ok"), res)
            self.assertEqual(res.get("kind"), "scene")
            path = pathlib.Path(res.get("path"))
            self.assertEqual(path.parent.name, "gallery")
            row = await self.pool.get(int(res["id"]))
            return row

        row = self.async_run(scenario())
        self.assertEqual(row.get("kind"), "scene")

    def test_avatar_defaults_when_kind_omitted(self):
        """老调用点不传 kind，应该还是头像，行为不变。"""

        async def scenario():
            res = await self.pool.add_bytes(FAKE_JPG)
            return await self.pool.get(int(res["id"]))

        row = self.async_run(scenario())
        self.assertEqual(row.get("kind"), "avatar")
        self.assertEqual(pathlib.Path(row.get("path")).parent.name, "avatar")

    def test_bad_kind_falls_back_to_avatar(self):
        async def scenario():
            res = await self.pool.add_bytes(FAKE_JPG, kind="whatever")
            return await self.pool.get(int(res["id"]))

        row = self.async_run(scenario())
        self.assertEqual(row.get("kind"), "avatar")

    # ------------------------------------------------------------ 取图
    def test_pick_does_not_cross_kinds(self):
        async def scenario():
            await self.pool.add_bytes(FAKE_JPG + b"scene", kind="scene")
            avatars = await self.pool.pick(kind="avatar")
            scenes = await self.pool.pick(kind="scene")
            return avatars, scenes

        avatars, scenes = self.async_run(scenario())
        self.assertIsNone(avatars, "没有头像时 pick(avatar) 必须返回 None，不能拿生活图顶")
        self.assertIsNotNone(scenes)
        self.assertEqual(scenes.get("kind"), "scene")

    def test_auto_change_refuses_non_avatar(self):
        """库里只有生活图时，自动换脸必须放弃。"""

        class FakeAvatar:
            def __init__(self):
                self.called = False

            async def set_from_file(self, path, reason=""):
                self.called = True
                return {"ok": True}

        async def scenario():
            await self.pool.add_bytes(FAKE_JPG + b"scene2", kind="scene")
            avatar = FakeAvatar()
            result = await self.pool.auto_change(avatar)
            return avatar, result

        avatar, result = self.async_run(scenario())
        self.assertFalse(avatar.called, "拿生活图去换脸了")
        self.assertFalse(result.get("ok"))

    # ------------------------------------------------------------ 裁剪
    def test_prune_keeps_kinds_apart(self):
        """生活图爆库不该把头像挤掉。"""

        async def scenario():
            # 注意两个下限：头像 max(3,...)、图库 max(5,...)，设得再小也会被顶回去
            self.p.cfg.d["qq_extra.avatar_pool_limit"] = 3
            self.p.cfg.d["qq_extra.gallery_limit"] = 5
            for i in range(6):
                await self.pool.add_bytes(FAKE_JPG + f"a{i}".encode(), kind="avatar")
            for i in range(8):
                await self.pool.add_bytes(FAKE_JPG + f"s{i}".encode(), kind="scene")
            await self.pool.prune()
            counts = {}
            for kind in ("avatar", "scene"):
                rows = await self.store.fetch_all(
                    "SELECT COUNT(*) AS n FROM qq_avatar_pool WHERE kind=?", (kind,)
                )
                counts[kind] = rows[0]["n"]
            return counts

        counts = self.async_run(scenario())
        self.assertEqual(counts["avatar"], 3)
        self.assertEqual(counts["scene"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
