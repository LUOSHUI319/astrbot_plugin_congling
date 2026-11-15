"""空间工具层：三个工具的行为与边界。

重点验三件事：
1. 返回是人话，**不含 fid / unikey / topicId 这类内部字段**——模型会照着念
2. 写操作收在 admin 这层（群里谁都能让它发说说就太危险了）
3. 空间工具不吃 capability.groups 白名单
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.capability.registry import CapabilityRegistry  # noqa: E402
from astrbot_plugin_congling.core.contracts import Capability  # noqa: E402
from astrbot_plugin_congling.qqext import qzone_tools as tools  # noqa: E402

NOW = int(time.time())
ITEMS = [
    {
        "nickname": "阿山", "content": "今天下雨了，没出门", "created_at": NOW - 3600,
        "images": ["http://x/1.jpg"], "like_count": 3, "comment_count": 1,
        "fid": "FID_SECRET_1", "unikey": "UK_SECRET_1", "curkey": "CK_SECRET_1",
        "topic_id": "TOPIC_SECRET_1", "uin": 10001, "appid": "311",
    },
    {
        "nickname": "小洛", "content": "", "created_at": NOW - 7200,
        "images": ["http://x/2.jpg", "http://x/3.jpg"], "like_count": 0, "comment_count": 0,
        "fid": "FID_SECRET_2", "unikey": "UK_SECRET_2", "curkey": "CK_SECRET_2",
        "topic_id": "TOPIC_SECRET_2", "uin": 10002, "appid": "311",
    },
]

SECRETS = ("FID_SECRET", "UK_SECRET", "CK_SECRET", "TOPIC_SECRET")


class FakeCfg:
    def __init__(self, **kv):
        self.d = {"qq_extra.qzone_tool_enabled": True}
        self.d.update(kv)

    def bool(self, key, default=False):
        return bool(self.d.get(key, default))

    def str(self, key, default=""):
        return str(self.d.get(key, default))

    def int(self, key, default=0):
        return int(self.d.get(key, default))

    def list(self, key, default=None):
        return list(self.d.get(key, default if default is not None else []))


class FakeQzone:
    def __init__(self):
        self.published = []
        self.liked = []
        self.commented = []
        self.deleted = []
        self.replied = []
        self.comments = {}
        self.visitors_used = False
        self.feed_targets = []
        self.timeline_calls = 0
        self.own_feed_calls = 0
        self.visitor_items = [{"uin": 20001, "nickname": "路过的人", "at": NOW - 7200}]

    def available(self):
        return True

    async def feed(self, *, limit=10, page=1, target="", target_uin=0, event=None, **_):
        self.feed_targets.append(target)
        items = ITEMS[: max(1, min(int(limit), len(ITEMS)))]
        return {"ok": True, "text": "好友的空间", "items": items, "target": {"label": "好友动态"}}

    async def timeline(self, *, limit=10, page=1):
        self.timeline_calls += 1
        items = ITEMS[: max(1, min(int(limit), len(ITEMS)))]
        return {"ok": True, "items": items, "text": "好友动态"}

    async def own_feed(self, *, limit=5, page=1):
        self.own_feed_calls += 1
        items = ITEMS[: max(1, min(int(limit), len(ITEMS)))]
        return {"ok": True, "text": "你自己的空间", "items": items, "target": {"label": "你自己"}}

    async def publish(self, text, *, images=None):
        self.published.append(text)
        return {"ok": True, "images": 0}

    async def like_item(self, item):
        self.liked.append(item.get("fid"))
        return {"ok": True, "text": "点了赞"}

    async def comment_item(self, item, text):
        self.commented.append((item.get("topic_id"), text))
        return {"ok": True, "text": "评论发出去了"}

    async def detail_of(self, item):
        return {
            "ok": True,
            "content": item.get("content") or "",
            "comments": self.comments.get(item.get("fid"), []),
        }

    async def delete_item(self, item):
        self.deleted.append(item.get("fid"))
        return {"ok": True, "text": "删掉了"}

    async def reply_comment_item(self, item, comment, text):
        self.replied.append((item.get("fid"), comment.get("comment_id"), text))
        return {"ok": True, "text": "回复发出去了"}

    async def visitors(self, *, limit=10):
        if self.visitors_used:
            return {"ok": False, "text": "今天已经看过一次了", "items": []}
        return {"ok": True, "items": self.visitor_items[:limit], "text": "最近有人来过"}


class FakeQQExt:
    def __init__(self, qz):
        self.qzone = qz


class FakePlugin:
    def __init__(self, **kv):
        self.cfg = FakeCfg(**kv)
        self.qqext = FakeQQExt(FakeQzone())


def run(coro):
    return asyncio.run(coro)


class QzoneReadTest(unittest.TestCase):
    def setUp(self):
        self.plugin = FakePlugin()

    def test_feed_is_human_readable_and_hides_internals(self):
        res = run(tools.qzone_read(self.plugin, action="feed", limit=5))
        self.assertTrue(res.ok)
        self.assertIn("阿山", res.text)
        self.assertIn("今天下雨了", res.text)
        self.assertIn("1 图", res.text)
        for secret in SECRETS:
            self.assertNotIn(secret, res.text, "内部字段漏到给模型的文本里了")

    def test_index_picks_one(self):
        res = run(tools.qzone_read(self.plugin, action="feed", index=2))
        self.assertTrue(res.ok)
        self.assertIn("小洛", res.text)
        self.assertNotIn("阿山", res.text)
        # 纯图说说要有可读的描述，不能是空串
        self.assertIn("组图", res.text)

    def test_index_out_of_range(self):
        res = run(tools.qzone_read(self.plugin, action="feed", index=9))
        self.assertFalse(res.ok)
        self.assertIn("没有第 9 条", res.text)

    def test_detail_returns_one_post(self):
        res = run(tools.qzone_read(self.plugin, action="detail", index=1))
        self.assertTrue(res.ok)
        self.assertIn("阿山", res.text)
        self.assertIn("今天下雨了", res.text)
        for secret in SECRETS:
            self.assertNotIn(secret, res.text)

    def test_comments_lists_people_and_text(self):
        self.plugin.qqext.qzone.comments = {
            "FID_SECRET_1": [
                {"comment_id": "CID1", "uin": 30001, "nickname": "小洛", "content": "下雨就别出门了"},
                {"comment_id": "CID2", "uin": 30002, "nickname": "阿山", "content": "我带伞了"},
            ]
        }
        res = run(tools.qzone_read(self.plugin, action="comments", index=1))
        self.assertTrue(res.ok)
        self.assertIn("小洛：下雨就别出门了", res.text)
        self.assertIn("阿山：我带伞了", res.text)

    def test_comments_hides_comment_ids(self):
        self.plugin.qqext.qzone.comments = {
            "FID_SECRET_1": [
                {"comment_id": "CID1", "uin": 30001, "nickname": "小洛", "content": "下雨就别出门了"}
            ]
        }
        res = run(tools.qzone_read(self.plugin, action="comments", index=1))
        self.assertNotIn("CID1", res.text, "评论 id 不该出现在给模型的文本里")

    def test_comments_empty(self):
        res = run(tools.qzone_read(self.plugin, action="comments", index=1))
        self.assertTrue(res.ok)
        self.assertIn("还没人评论", res.text)

    def test_timeline_reads_feed_stream(self):
        res = run(tools.qzone_read(self.plugin, action="timeline", limit=2))
        self.assertTrue(res.ok)
        self.assertIn("好友动态", res.text)
        self.assertEqual(self.plugin.qqext.qzone.timeline_calls, 1)

    def test_visitors_ok(self):
        res = run(tools.qzone_read(self.plugin, action="visitors"))
        self.assertTrue(res.ok)
        self.assertIn("路过的人", res.text)

    def test_visitors_daily_limit_passes_through(self):
        self.plugin.qqext.qzone.visitors_used = True
        res = run(tools.qzone_read(self.plugin, action="visitors"))
        self.assertFalse(res.ok)
        self.assertIn("已经看过一次", res.text)

    def test_unknown_action_rejected(self):
        res = run(tools.qzone_read(self.plugin, action="whatever"))
        self.assertFalse(res.ok)


class QzonePostTest(unittest.TestCase):
    def setUp(self):
        self.plugin = FakePlugin()

    def test_publish_ok(self):
        res = run(tools.qzone_post(self.plugin, action="publish", text="今天下雨了"))
        self.assertTrue(res.ok)
        self.assertEqual(self.plugin.qqext.qzone.published, ["今天下雨了"])
        self.assertIn("发出去了", res.text)

    def test_empty_text_rejected(self):
        res = run(tools.qzone_post(self.plugin, action="publish", text="   "))
        self.assertFalse(res.ok)
        self.assertEqual(self.plugin.qqext.qzone.published, [])

    def test_too_long_rejected(self):
        res = run(tools.qzone_post(self.plugin, action="publish", text="字" * 1001))
        self.assertFalse(res.ok)
        self.assertEqual(self.plugin.qqext.qzone.published, [])

    def test_preview_not_implemented(self):
        res = run(tools.qzone_post(self.plugin, action="preview", text="x"))
        self.assertFalse(res.ok)
        self.assertIn("还没接", res.text)

    def test_delete_by_index(self):
        res = run(tools.qzone_post(self.plugin, action="delete", index=1))
        self.assertTrue(res.ok)
        self.assertEqual(self.plugin.qqext.qzone.deleted, ["FID_SECRET_1"])
        # 删除必须落在自己号上，不能被默认目标（owner_qq）带偏
        self.assertEqual(self.plugin.qqext.qzone.own_feed_calls, 1)
        self.assertNotIn("FID_SECRET", res.text)

    def test_delete_rejects_out_of_range(self):
        res = run(tools.qzone_post(self.plugin, action="delete", index=9))
        self.assertFalse(res.ok)
        self.assertEqual(self.plugin.qqext.qzone.deleted, [])

    def test_delete_surfaces_refusal_reason(self):
        """6 小时窗口之类的拒绝，要原样说给模型听，不能吞成通用失败。"""

        async def refuse(item):
            return {"ok": False, "text": "这条发出去超过 6 小时了，删它得你自己去手机上操作"}

        self.plugin.qqext.qzone.delete_item = refuse
        res = run(tools.qzone_post(self.plugin, action="delete", index=1))
        self.assertFalse(res.ok)
        self.assertIn("超过 6 小时", res.text)


class QzoneInteractTest(unittest.TestCase):
    def setUp(self):
        self.plugin = FakePlugin()

    def test_like_by_index(self):
        res = run(tools.qzone_interact(self.plugin, action="like", index=1))
        self.assertTrue(res.ok)
        self.assertEqual(self.plugin.qqext.qzone.liked, ["FID_SECRET_1"])
        self.assertNotIn("FID_SECRET", res.text)

    def test_interact_uses_timeline_not_user_feeds(self):
        """点赞/评论要的参数只有动态流给，所以必须走 timeline。"""
        run(tools.qzone_interact(self.plugin, action="like", index=1))
        self.assertEqual(self.plugin.qqext.qzone.timeline_calls, 1)

    def test_interact_rejects_target(self):
        res = run(tools.qzone_interact(self.plugin, action="like", index=1, target="3190568934"))
        self.assertFalse(res.ok)
        self.assertIn("好友动态", res.text)

    def test_comment_needs_content(self):
        res = run(tools.qzone_interact(self.plugin, action="comment", index=1, content="  "))
        self.assertFalse(res.ok)
        self.assertEqual(self.plugin.qqext.qzone.commented, [])

    def test_comment_ok(self):
        res = run(tools.qzone_interact(self.plugin, action="comment", index=2, content="图不错"))
        self.assertTrue(res.ok)
        self.assertEqual(self.plugin.qqext.qzone.commented, [("TOPIC_SECRET_2", "图不错")])

    def test_reply_without_comments(self):
        res = run(tools.qzone_interact(self.plugin, action="reply", index=1, content="嗯"))
        self.assertFalse(res.ok)
        self.assertIn("还没人评论", res.text)

    def test_reply_needs_content(self):
        self.plugin.qqext.qzone.comments = {
            "FID_SECRET_1": [{"comment_id": "CID1", "uin": 30001, "nickname": "小洛", "content": "下雨了"}]
        }
        res = run(tools.qzone_interact(self.plugin, action="reply", index=1, content="  "))
        self.assertFalse(res.ok)
        self.assertEqual(self.plugin.qqext.qzone.replied, [])

    def test_reply_ok(self):
        self.plugin.qqext.qzone.comments = {
            "FID_SECRET_1": [
                {"comment_id": "CID1", "uin": 30001, "nickname": "小洛", "content": "下雨了"},
                {"comment_id": "CID2", "uin": 30002, "nickname": "阿山", "content": "带伞"},
            ]
        }
        res = run(tools.qzone_interact(self.plugin, action="reply", index=1, comment_index=2, content="知道了"))
        self.assertTrue(res.ok)
        self.assertEqual(self.plugin.qqext.qzone.replied, [("FID_SECRET_1", "CID2", "知道了")])
        self.assertNotIn("CID2", res.text)


class BuildCapsTest(unittest.TestCase):
    def test_three_caps_with_admin_on_writes(self):
        caps = tools.build_caps(FakePlugin())
        self.assertEqual(len(caps), 3)
        by_name = {cap.name: cap for cap in caps}
        self.assertEqual(set(by_name), {"qzone.read", "qzone.post", "qzone.interact"})
        self.assertEqual(by_name["qzone.read"].auth, "all")
        self.assertEqual(by_name["qzone.post"].auth, "admin")
        self.assertEqual(by_name["qzone.interact"].auth, "admin")
        for cap in caps:
            self.assertEqual(cap.group, "qzone")
            self.assertTrue(cap.takes_event)

    def test_disabled_returns_empty(self):
        self.assertEqual(tools.build_caps(FakePlugin(**{"qq_extra.qzone_tool_enabled": False})), [])


class RegistryGroupBypassTest(unittest.TestCase):
    """空间工具不该被 capability.groups 白名单挡住。"""

    def test_ignore_groups(self):
        plugin = FakePlugin(**{"capability.enabled": True, "capability.groups": ["life"]})
        reg = CapabilityRegistry(plugin)
        cap = Capability(name="qzone.read", description="x", group="qzone")
        reg.register(cap)
        self.assertNotIn("qzone.read", reg.caps)
        reg.register(cap, ignore_groups=True)
        self.assertIn("qzone.read", reg.caps)


if __name__ == "__main__":
    unittest.main(verbosity=2)
