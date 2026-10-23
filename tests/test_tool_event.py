"""工具层事件取值与整句自我指代的回归测试。

这一组钉住两个曾经搞错的地方：
1. AstrAgentContext 的消息事件在 event 字段，不在 context 字段
2. 模型把整句（"看我空间"）塞进 target 时，仍应落到说话人自己
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from astrbot_plugin_congling.capability.tool import CapabilityTool
from astrbot_plugin_congling.qqext.targets import TargetResolver

SENDER = 3190568934


class FakeAt:
    def __init__(self, qq: str) -> None:
        self.qq = qq
        self.type = "at"


class FakeEvent:
    """像 AstrMessageEvent：有 get_sender_id / get_group_id / unified_msg_origin。"""

    def __init__(self, sender: int = SENDER, group: str = "999", at: str = "", umo: str = ""):
        self._sender = str(sender)
        self._group = group
        self.unified_msg_origin = umo
        self.message_obj = SimpleNamespace(message=[FakeAt(at)] if at else [])

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group


class FakeStarContext:
    """像 Star Context：没有消息事件的方法。"""


class FakeAgentContext:
    """像 AstrAgentContext：context 是 Star Context，event 才是消息事件。"""

    def __init__(self, event):
        self.context = FakeStarContext()
        self.event = event


class RecordingRegistry:
    def __init__(self, admin_result=True):
        self.plugin = SimpleNamespace(is_admin=lambda ev: admin_result and ev is not None and hasattr(ev, "get_sender_id"))
        self.calls: list[dict] = []

    async def invoke(self, name, *, session_id="", admin=False, event=None, **kwargs):
        self.calls.append({"session_id": session_id, "admin": admin, "event": event})
        return SimpleNamespace(text="ok")


def make_tool(registry) -> CapabilityTool:
    return CapabilityTool(
        registry=registry,
        capability_name="qzone.feed",
        name="congling_qzone_feed",
        description="读空间",
        parameters={"type": "object", "properties": {}},
    )


class TestToolEventResolution(unittest.TestCase):
    def test_uses_event_field_not_context(self):
        registry = RecordingRegistry()
        tool = make_tool(registry)
        event = FakeEvent(group="999")

        asyncio.run(tool.call(FakeAgentContext(event)))

        call = registry.calls[0]
        # session_id 来自真事件的 get_group_id
        self.assertEqual(call["session_id"], "999")
        # 传下去的 event 必须是消息事件，不是 Star Context
        self.assertIs(call["event"], event)
        self.assertTrue(hasattr(call["event"], "get_sender_id"))

    def test_admin_check_receives_real_event(self):
        registry = RecordingRegistry(admin_result=True)
        tool = make_tool(registry)
        asyncio.run(tool.call(FakeAgentContext(FakeEvent())))
        self.assertTrue(registry.calls[0]["admin"])

    def test_falls_back_to_context_when_no_event_field(self):
        registry = RecordingRegistry()
        tool = make_tool(registry)
        star = FakeStarContext()
        asyncio.run(tool.call(star))
        # 没有 event 字段时退回 context 本身，不崩
        self.assertIs(registry.calls[0]["event"], star)
        self.assertEqual(registry.calls[0]["session_id"], "")

    def test_bare_event_object_still_works(self):
        registry = RecordingRegistry()
        tool = make_tool(registry)
        event = FakeEvent(group="888")
        asyncio.run(tool.call(event))
        self.assertIs(registry.calls[0]["event"], event)
        self.assertEqual(registry.calls[0]["session_id"], "888")


class TestSessionId(unittest.TestCase):
    def _sid(self, event) -> str:
        return CapabilityTool._session_id(event)

    def test_group_chat_still_uses_group_id(self):
        # 群聊行为保持不变
        event = FakeEvent(group="123456", umo="aiocqhttp:GroupMessage:123456")
        self.assertEqual(self._sid(event), "123456")

    def test_private_chat_uses_umo(self):
        event = FakeEvent(sender=SENDER, group=None, umo="aiocqhttp:FriendMessage:3190568934")
        self.assertEqual(self._sid(event), "aiocqhttp:FriendMessage:3190568934")

    def test_empty_group_does_not_swallow_umo(self):
        # 私聊时 get_group_id 返回空串/None，不能被当成有效群号
        for empty in ("", None):
            event = FakeEvent(group=empty, umo="aiocqhttp:FriendMessage:42")
            self.assertEqual(self._sid(event), "aiocqhttp:FriendMessage:42", repr(empty))

    def test_falls_back_to_sender_when_no_umo(self):
        event = FakeEvent(sender=SENDER, group=None, umo="")
        self.assertEqual(self._sid(event), str(SENDER))

    def test_empty_when_nothing_available(self):
        self.assertEqual(self._sid(SimpleNamespace()), "")

    def test_umo_as_method_also_supported(self):
        class MethodUmo:
            def get_group_id(self):
                return None

            def unified_msg_origin(self):
                return "aiocqhttp:FriendMessage:7"

        self.assertEqual(self._sid(MethodUmo()), "aiocqhttp:FriendMessage:7")


class FakeBridge:
    def _onebot_bot(self):
        return None

    async def _call_action(self, bot, action, **params):
        return None


class TestSelfPhrases(unittest.TestCase):
    def setUp(self):
        plugin = SimpleNamespace(qqext=SimpleNamespace(qzone=FakeBridge()))
        self.resolver = TargetResolver(plugin)

    def test_whole_sentence_phrases(self):
        for phrase in ("我", "我的", "我的空间", "我空间", "看我空间", "看看我的空间", "我的主页"):
            res = asyncio.run(self.resolver.resolve(phrase, FakeEvent(sender=SENDER)))
            self.assertTrue(res.ok, phrase)
            self.assertEqual(res.uin, SENDER, phrase)

    def test_other_person_phrase_not_treated_as_self(self):
        # 没有 At 也没有昵称时，"他" 落到说话人自己，但绝不能被当成"自我指代"误判成别人
        res = asyncio.run(self.resolver.resolve("他", FakeEvent(sender=SENDER)))
        self.assertTrue(res.ok)
        self.assertEqual(res.uin, SENDER)


if __name__ == "__main__":
    unittest.main()
