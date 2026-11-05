"""补课正文中的原生 QQ @；模型、发送通道与数据库均为离线替身。"""
import importlib
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_wake_persona_flow as base

MentionReply = importlib.import_module("cg.core.reply_mentions").MentionReply
RollupItem = importlib.import_module("cg.night.rollup").RollupItem


class RollupMentions(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.FlowTest.asyncSetUp
    asyncTearDown = base.FlowTest.asyncTearDown
    archive = base.FlowTest.archive
    prepare = base.FlowTest.prepare

    async def reply(self, text="[[congling_at:123456]] 你问的视频明天做。", items=None, umo=None):
        await self.store.execute(
            "UPDATE msg_archive SET sender_id='123456',sender_name='真实昵称' "
            "WHERE session_id='s' AND msg_id='sleep-question'")
        self.plugin.models.character_chat = AsyncMock(return_value=text)
        identity = await self.plugin.persona.resolve(self.row["umo"])
        items = items or [RollupItem(msg_id="sleep-question", session_id="s",
                                     who="模型编造昵称", what="视频什么时候做？")]
        body = await self.night.rollup.compose_reply(
            {"to_reply": [{"item": item} for item in items]},
            umo=self.row["umo"] if umo is None else umo, identity=identity, strict=True,
        )
        return body, identity

    async def send(self, body, identity, row=None, phase="reply"):
        self.context.send_message = AsyncMock(return_value=True)
        module = sys.modules["astrbot.api.message_components"]
        with patch.object(module, "At", new=lambda qq: SimpleNamespace(qq=qq), create=True):
            result = await self.plugin.proactive.send_stage(row or self.row, body, phase, identity)
        chains = [call.args[1].chain for call in self.context.send_message.await_args_list]
        return result, chains

    async def test_verified_author_replaces_model_nickname_in_prompt(self):
        body, _ = await self.reply()
        prompt = self.plugin.models.character_chat.await_args.args[0]
        self.assertNotIn("模型编造昵称", prompt)
        self.assertNotIn("真实昵称", prompt)
        self.assertIn("[[congling_at:123456]]", prompt)
        self.assertEqual(body.mentions, {"123456": "真实昵称"})

    async def test_native_at_is_inside_same_reply_and_archive_has_no_tokens(self):
        body, identity = await self.reply("我翻完了，[[congling_at:123456]] 你问的视频明天做。")
        result, chains = await self.send(body, identity)
        self.assertEqual(len(chains), 1)
        self.assertEqual([getattr(c, "qq", None) for c in chains[0]], [None, "123456", None])
        self.assertEqual(result, ["我翻完了，@真实昵称 你问的视频明天做。"])
        row = await self.store.fetch_one("SELECT text FROM msg_archive WHERE sender_id='bot'")
        self.assertNotIn("congling_at", row["text"])
        self.assertIn("@真实昵称", row["text"])

    async def test_multiple_users_and_questions_are_one_message(self):
        await self.store.execute(
            "UPDATE msg_archive SET sender_id='654321',sender_name='另一位' WHERE msg_id='old'")
        items = [RollupItem(msg_id="sleep-question", session_id="s", what="视频？"),
                 RollupItem(msg_id="old", session_id="s", what="图片？")]
        body, identity = await self.reply(
            "[[congling_at:123456]] 视频明天做；[[congling_at:654321]] 图片晚上发。", items)
        self.plugin.composer = SimpleNamespace(split=Mock(return_value=["不该分句"]))
        _, chains = await self.send(body, identity)
        self.plugin.composer.split.assert_not_called()
        self.assertEqual(len(chains), 1)
        self.assertEqual([c.qq for c in chains[0] if hasattr(c, "qq")], ["123456", "654321"])

    async def test_missing_token_still_mentions_selected_user(self):
        body, identity = await self.reply("你问的视频明天做。")
        _, chains = await self.send(body, identity)
        self.assertEqual(chains[0][0].qq, "123456")
        self.assertIn("视频明天做", chains[0][1].text)

    async def test_duplicate_and_invented_tokens_do_not_ping_extra_users(self):
        body, identity = await self.reply(
            "[[congling_at:999999]] [[congling_at:123456]] 明天做。[[congling_at:123456]]")
        _, chains = await self.send(body, identity)
        self.assertEqual([c.qq for c in chains[0] if hasattr(c, "qq")], ["123456"])
        self.assertNotIn("999999", str(body))

    async def test_whitespace_and_case_in_token_are_supported(self):
        body = MentionReply("[[ CONGLING_AT : 123456 ]] 你问的明天做。", {"123456": "昵称"})
        self.assertEqual(list(body.segments(str(body)))[0], ("at", "123456"))

    async def test_private_reply_does_not_create_group_ping(self):
        await self.store.execute(
            "INSERT INTO msg_archive(msg_id,session_id,session_kind,sender_id,ts,text) "
            "VALUES('private','123456','private','123456',1,'视频什么时候做？')")
        body, identity = await self.reply("你问的明天做。", [
            RollupItem(msg_id="private", session_id="123456")], umo="qq:FriendMessage:123456")
        row = {"session_id": "123456", "umo": "qq:FriendMessage:123456", "session_kind": "private"}
        _, chains = await self.send(body, identity, row)
        self.assertFalse(any(hasattr(c, "qq") for c in chains[0]))
        self.assertEqual(chains[0][0].text, "你问的明天做。")

    async def test_legacy_plain_text_is_not_interpreted_as_native_at(self):
        identity = await self.plugin.persona.resolve(self.row["umo"])
        _, chains = await self.send("[[congling_at:123456]] 普通文字", identity)
        self.assertFalse(any(hasattr(c, "qq") for c in chains[0]))

    async def test_other_session_cannot_supply_mention_identity(self):
        await self.store.execute(
            "INSERT INTO msg_archive(msg_id,session_id,session_kind,sender_id,sender_name,ts,text) "
            "VALUES('foreign','other','group','987654','外群用户',1,'问题')")
        with self.assertRaisesRegex(RuntimeError, "来源会话不匹配"):
            await self.reply("回应。", [
                RollupItem(msg_id="foreign", session_id="other", who="外群用户", what="问题")])
        self.plugin.models.character_chat.assert_not_awaited()

    async def test_missing_author_does_not_guess_qq_from_name(self):
        with self.assertRaisesRegex(RuntimeError, "原消息缺失"):
            await self.reply("回应。", [
                RollupItem(msg_id="missing", session_id="s", who="123456", what="问题")])
        self.plugin.models.character_chat.assert_not_awaited()

    async def test_empty_model_result_remains_failure_instead_of_at_only_message(self):
        with self.assertRaisesRegex(RuntimeError, "补课回复生成失败"):
            await self.reply("")

    async def test_actual_wake_flow_sends_one_reply_with_native_at(self):
        await self.store.execute(
            "UPDATE msg_archive SET sender_id='123456',sender_name='真实昵称' WHERE msg_id='sleep-question'")
        self.context.send_message = AsyncMock(return_value=True)
        module = sys.modules["astrbot.api.message_components"]
        with patch.object(module, "At", new=lambda qq: SimpleNamespace(qq=qq), create=True):
            await self.prepare()
        self.assertEqual(self.context.send_message.await_count, 2)  # 爬楼提示 + 一条补课回复
        reply_chain = self.context.send_message.await_args_list[1].args[1].chain
        self.assertEqual([c.qq for c in reply_chain if hasattr(c, "qq")], ["123456"])
        self.assertEqual(await self.store.scalar(
            "SELECT COUNT(*) FROM msg_archive WHERE sender_id<>'bot' AND handled=0"), 0)


if __name__ == "__main__":
    unittest.main()
