"""空间接口的纯解析与边界。

这里不碰网络，只把「接口返回什么样、我们怎么认」和「什么情况下不许动」锁住。
真实接口的字段名一直很飘（commentid / commentId / tid / id 都见过），
所以解析要宽容；但删说说的 6 小时窗口是硬边界，不能宽容。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.qqext import qzone as qzone_mod  # noqa: E402
from astrbot_plugin_congling.qqext.qzone_client import (  # noqa: E402
    parse_comment_list,
    parse_visitors,
)


class CommentParseTest(unittest.TestCase):
    def test_commentlist_at_top_level(self):
        payload = {
            "commentlist": [
                {
                    "commentid": "C1",
                    "uin": 30001,
                    "nickname": "小洛",
                    "content": "下雨了就别出门",
                    "date": 1762800000,
                    "user": {"uin": 30001, "nickname": "小洛"},
                }
            ]
        }
        rows = parse_comment_list(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["comment_id"], "C1")
        self.assertEqual(rows[0]["uin"], 30001)
        self.assertEqual(rows[0]["content"], "下雨了就别出门")

    def test_comment_nested_under_comment_block(self):
        payload = {"comment": {"commentlist": [{"commentId": "C2", "content": "带伞"}]}}
        rows = parse_comment_list(payload)
        self.assertEqual([r["comment_id"] for r in rows], ["C2"])

    def test_comment_under_data(self):
        payload = {"data": {"comments": [{"tid": "C3", "content": "嗯"}]}}
        self.assertEqual([r["comment_id"] for r in parse_comment_list(payload)], ["C3"])

    def test_nested_replies_are_flattened(self):
        payload = {
            "commentlist": [
                {
                    "commentid": "C1",
                    "content": "主评论",
                    "replyList": [{"commentid": "C1R", "content": "楼中楼"}],
                }
            ]
        }
        rows = parse_comment_list(payload)
        ids = [r["comment_id"] for r in rows]
        self.assertIn("C1", ids)
        self.assertIn("C1R", ids)
        reply = [r for r in rows if r["comment_id"] == "C1R"][0]
        self.assertEqual(reply["parent_id"], "C1")

    def test_duplicates_are_dropped(self):
        payload = {
            "commentlist": [{"commentid": "C1", "uin": 1, "content": "同一条"}],
            "comments": [{"commentid": "C1", "uin": 1, "content": "同一条"}],
        }
        self.assertEqual(len(parse_comment_list(payload)), 1)

    def test_html_is_stripped(self):
        payload = {"commentlist": [{"commentid": "C1", "content": "<b>粗</b>体"}]}
        self.assertEqual(parse_comment_list(payload)[0]["content"], "粗体")

    def test_garbage_returns_empty(self):
        for payload in ({}, {"commentlist": "not a list"}, {"comment": None}):
            self.assertEqual(parse_comment_list(payload), [])


class VisitorParseTest(unittest.TestCase):
    def test_items(self):
        payload = {"items": [{"uin": 20001, "name": "路过的人", "time": 1762800000}]}
        rows = parse_visitors(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["nickname"], "路过的人")

    def test_nested_data(self):
        payload = {"data": {"items": [{"frienduin": 20002, "nickname": "小洛"}]}}
        rows = parse_visitors(payload)
        self.assertEqual(rows[0]["uin"], 20002)

    def test_empty(self):
        self.assertEqual(parse_visitors({}), [])
        self.assertEqual(parse_visitors({"items": "x"}), [])


class DefaultTargetTest(unittest.TestCase):
    """没指定看谁的时候看谁。有 owner 就不跟说话人走。"""

    OWNER = "3190568934"

    def test_empty_falls_back_to_owner(self):
        self.assertEqual(qzone_mod.pick_default_target("", self.OWNER), self.OWNER)

    def test_blank_falls_back_to_owner(self):
        self.assertEqual(qzone_mod.pick_default_target("   ", self.OWNER), self.OWNER)

    def test_explicit_wins(self):
        self.assertEqual(qzone_mod.pick_default_target("10001", self.OWNER), "10001")

    def test_me_wins(self):
        self.assertEqual(qzone_mod.pick_default_target("我", self.OWNER), "我")

    def test_no_owner_returns_empty(self):
        self.assertEqual(qzone_mod.pick_default_target("", ""), "")

    def test_non_digit_owner_ignored(self):
        self.assertEqual(qzone_mod.pick_default_target("", "not-a-qq"), "")


class DeleteWindowTest(unittest.TestCase):
    NOW = 1_760_000_000

    def test_inside_window(self):
        self.assertTrue(qzone_mod.within_delete_window(self.NOW - 3600, self.NOW))

    def test_just_outside(self):
        self.assertFalse(qzone_mod.within_delete_window(self.NOW - 6 * 3600 - 1, self.NOW))

    def test_edge_is_allowed(self):
        self.assertTrue(qzone_mod.within_delete_window(self.NOW - 6 * 3600, self.NOW))

    def test_missing_stamp_allows(self):
        # 没有时间戳时放行：外层还有写权限 + 只能删自己的两道
        self.assertTrue(qzone_mod.within_delete_window(0, self.NOW))
        self.assertTrue(qzone_mod.within_delete_window(None, self.NOW))


if __name__ == "__main__":
    unittest.main(verbosity=2)
