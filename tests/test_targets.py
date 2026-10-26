"""目标识别与管理员判定的单测。

这两块都是"权限/指向搞错就出事"的地方，必须用断言钉住。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from astrbot_plugin_congling.core.config import Config
from astrbot_plugin_congling.main import ConglingPlugin
from astrbot_plugin_congling.qqext.targets import Resolution, TargetResolver

SENDER = 10001
FRIEND = 10086
OTHER = 10001
STRANGER = 10099  # 既不是管理员也不是发言人，用来测「普通用户被拒」


class FakeAt:
    def __init__(self, qq: str) -> None:
        self.qq = qq
        self.type = "at"


class FakeEvent:
    def __init__(self, sender: int = SENDER, group: str = "", at: str = ""):
        self._sender = str(sender)
        self._group = group
        chain = [FakeAt(at)] if at else []
        self.message_obj = SimpleNamespace(message=chain)

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group


class FakeBridge:
    """假的空间桥，只提供取名单的能力。"""

    def __init__(self, members=None, friends=None):
        self.members = members or []
        self.friends = friends or []

    def _onebot_bot(self):
        return object()

    async def _call_action(self, bot, action, **params):
        if action == "get_group_member_list":
            return self.members
        if action == "get_friend_list":
            return self.friends
        return None


def make_resolver(members=None, friends=None) -> TargetResolver:
    qzone = FakeBridge(members, friends)
    plugin = SimpleNamespace(qqext=SimpleNamespace(qzone=qzone))
    return TargetResolver(plugin)


class TestTargetResolver(unittest.TestCase):
    def test_explicit_qq_number(self):
        res = asyncio.run(make_resolver().resolve("10001", None))
        self.assertTrue(res.ok)
        self.assertEqual(res.uin, OTHER)

    def test_qq_number_with_at_prefix(self):
        res = asyncio.run(make_resolver().resolve("@10001", None))
        self.assertEqual(res.uin, OTHER)

    def test_empty_defaults_to_sender(self):
        res = asyncio.run(make_resolver().resolve("", FakeEvent(sender=SENDER)))
        self.assertTrue(res.ok)
        self.assertEqual(res.uin, SENDER)

    def test_self_words(self):
        for word in ("我的空间", "我", "自己"):
            res = asyncio.run(make_resolver().resolve(word, FakeEvent(sender=SENDER)))
            self.assertEqual(res.uin, SENDER, word)

    def test_at_takes_precedence_when_no_explicit_target(self):
        event = FakeEvent(sender=SENDER, group="999", at=str(FRIEND))
        res = asyncio.run(make_resolver().resolve("", event))
        self.assertEqual(res.uin, FRIEND)

    def test_self_beats_at(self):
        event = FakeEvent(sender=SENDER, group="999", at=str(FRIEND))
        res = asyncio.run(make_resolver().resolve("我的空间", event))
        self.assertEqual(res.uin, SENDER)

    def test_nickname_in_group(self):
        members = [
            {"user_id": OTHER, "card": "小山", "nickname": "望山"},
            {"user_id": FRIEND, "card": "洛水", "nickname": "洛水"},
        ]
        res = asyncio.run(make_resolver(members=members).resolve("小山", FakeEvent(group="999")))
        self.assertTrue(res.ok)
        self.assertEqual(res.uin, OTHER)
        self.assertIn("小山", res.label)

    def test_nickname_from_friends(self):
        friends = [{"user_id": FRIEND, "remark": "阿水", "nickname": "洛水"}]
        res = asyncio.run(make_resolver(friends=friends).resolve("阿水", FakeEvent(group="")))
        self.assertEqual(res.uin, FRIEND)

    def test_ambiguous_nickname_reports(self):
        members = [
            {"user_id": OTHER, "card": "小王", "nickname": "王"},
            {"user_id": FRIEND, "card": "小王", "nickname": "王二"},
        ]
        res = asyncio.run(make_resolver(members=members).resolve("小王", FakeEvent(group="999")))
        self.assertFalse(res.ok)
        self.assertIn("好几个", res.error)

    def test_exact_match_beats_partial(self):
        members = [
            {"user_id": OTHER, "card": "小王", "nickname": "王"},
            {"user_id": FRIEND, "card": "小王二号", "nickname": "王二"},
        ]
        res = asyncio.run(make_resolver(members=members).resolve("小王", FakeEvent(group="999")))
        self.assertTrue(res.ok)
        self.assertEqual(res.uin, OTHER)

    def test_unknown_nickname_reports(self):
        res = asyncio.run(make_resolver().resolve("查无此人", FakeEvent(group="999")))
        self.assertFalse(res.ok)
        self.assertIn("没找到", res.error)

    def test_page_context_without_event_falls_back_later(self):
        # 没有事件上下文时，"我" 解析不出来，由 bridge 用登录账号兜底
        res = asyncio.run(make_resolver().resolve("我", None))
        self.assertFalse(res.ok)

    def test_resolution_ok_property(self):
        self.assertTrue(Resolution(uin=1).ok)
        self.assertFalse(Resolution().ok)
        self.assertFalse(Resolution(uin=1, error="x").ok)


class FakeContext:
    def __init__(self, admins):
        self._admins = admins

    def get_config(self):
        return {"admins_id": self._admins}


class FakePlugin(ConglingPlugin):
    """不跑初始化，只带上判定所需的两样东西。"""

    def __init__(self, astrbot_admins, extra=(), include_role=False):
        self.context = FakeContext(list(astrbot_admins))
        self.cfg = Config(
            {
                "basic": {
                    "extra_admins": list(extra),
                    "include_group_role": include_role,
                }
            }
        )


def make_plugin_self(astrbot_admins, extra=(), include_role=False):
    return FakePlugin(astrbot_admins, extra, include_role)


class FakeRoleEvent(FakeEvent):
    def __init__(self, sender: int = SENDER, role: str = "member"):
        super().__init__(sender=sender)
        self.role = role
        self.message_obj = SimpleNamespace(message=[], sender=SimpleNamespace(role=role))


class TestAdminRules(unittest.TestCase):
    def test_astrbot_admin_recognized(self):
        fake_self = make_plugin_self(["10001"])
        self.assertTrue(ConglingPlugin.is_admin(fake_self, FakeRoleEvent(SENDER)))

    def test_extra_admin_recognized(self):
        fake_self = make_plugin_self([], extra=["10001"])
        self.assertTrue(ConglingPlugin.is_admin(fake_self, FakeRoleEvent(OTHER)))

    def test_regular_user_rejected(self):
        # 这里必须用第三个号：OTHER 恰好等于管理员，拿它测「拒绝」永远测不出来
        fake_self = make_plugin_self(["10001"])
        self.assertFalse(ConglingPlugin.is_admin(fake_self, FakeRoleEvent(STRANGER)))

    def test_group_admin_ignored_by_default(self):
        fake_self = make_plugin_self([], include_role=False)
        self.assertFalse(ConglingPlugin.is_admin(fake_self, FakeRoleEvent(OTHER, role="admin")))

    def test_group_admin_recognized_when_enabled(self):
        fake_self = make_plugin_self([], include_role=True)
        self.assertTrue(ConglingPlugin.is_admin(fake_self, FakeRoleEvent(OTHER, role="admin")))

    def test_owner_recognized_when_enabled(self):
        fake_self = make_plugin_self([], include_role=True)
        self.assertTrue(ConglingPlugin.is_admin(fake_self, FakeRoleEvent(OTHER, role="owner")))

    def test_astrbot_placeholder_ignored(self):
        fake_self = make_plugin_self(["astrbot"])
        self.assertEqual(ConglingPlugin.admin_ids(fake_self), [])
        self.assertFalse(ConglingPlugin.is_admin(fake_self, FakeRoleEvent(OTHER)))

    def test_union_dedup(self):
        fake_self = make_plugin_self(["10001"], extra=["10001", "10001"])
        self.assertEqual(ConglingPlugin.admin_ids(fake_self), ["10001"])


if __name__ == "__main__":
    unittest.main()
