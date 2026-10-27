"""引用回复引错人（2026-10-27）。

现象：有时候 bot 引用的是很久以前的一条消息，而不是刚触发它的那条。

原因在 `Composer._compose`：它拿的是 `plugin._last_perceived`——全局「最后一条被感知的消息」。
三种情况都会错位：

1. 主动消息 / 定时任务：发回复时上下文里根本没有新消息，`_last_perceived`
   还是几小时前那条，于是引用落到那句老话上
2. 两条消息挨着进来：先算完的那条会引用后一条
3. 延时回复：等它真发的时候 `_last_perceived` 早换了

现在改成从事件上取（`_congling_perceived`），并且要求会话对得上、不是太久以前。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from astrbot_plugin_congling.core.contracts import PerceivedMessage  # noqa: E402
from astrbot_plugin_congling.humanize.compose import Composer  # noqa: E402


class Cfg:
    def __init__(self, data: dict | None = None) -> None:
        self.data = {"humanize.quote_max_age_seconds": 180, "humanize.quote_cooldown_seconds": 30}
        self.data.update(data or {})

    def float(self, key, default=0.0):
        try:
            return float(self.data.get(key, default))
        except (TypeError, ValueError):
            return default

    def int(self, key, default=0):
        try:
            return int(self.data.get(key, default))
        except (TypeError, ValueError):
            return default

    def bool(self, key, default=False):
        return bool(self.data.get(key, default))

    def str(self, key, default=""):
        return str(self.data.get(key, default))


class FakeEvent:
    def __init__(self, *, group: str = "", sender: str = "3190568934") -> None:
        self._group = group
        self._sender = sender

    def get_group_id(self):
        return self._group or None

    def get_sender_id(self):
        return self._sender


class FakePlugin:
    def __init__(self, cfg: Cfg | None = None) -> None:
        self.cfg = cfg or Cfg()
        self.guard = None
        self.rhythm = None
        self._last_perceived = None


def make(msg=None, cfg=None):
    plugin = FakePlugin(cfg)
    return Composer(plugin), plugin


def note(ts=None, session="3190568934", kind="private", msg_id="777"):
    return PerceivedMessage(
        msg_id=msg_id,
        session_id=session,
        session_kind=kind,
        sender_id="3190568934",
        sender_name="望山",
        ts=int(ts if ts is not None else time.time()),
        text="睡了没",
    )


class QuoteTargetTest(unittest.TestCase):
    def test_uses_message_bound_to_event(self) -> None:
        composer, plugin = make()
        fresh = note()
        stale = note(ts=time.time() - 7200, msg_id="111")
        plugin._last_perceived = stale  # 全局缓存是老的
        event = FakeEvent()
        event._congling_perceived = fresh

        got = composer._message_for(event, "3190568934")
        self.assertIs(got, fresh, "应该引用事件上那条，而不是全局缓存里的老消息")

    def test_no_message_bound_means_no_quote(self) -> None:
        composer, plugin = make()
        plugin._last_perceived = note()
        got = composer._message_for(FakeEvent(), "3190568934")
        self.assertIsNone(got, "主动消息这种没有绑定消息的场景，宁可不引用")

    def test_too_old_is_rejected(self) -> None:
        composer, _ = make()
        event = FakeEvent()
        event._congling_perceived = note(ts=time.time() - 600)
        self.assertIsNone(composer._message_for(event, "3190568934"))

    def test_age_limit_can_be_disabled(self) -> None:
        composer, _ = make(cfg=Cfg({"humanize.quote_max_age_seconds": 0}))
        event = FakeEvent()
        event._congling_perceived = note(ts=time.time() - 7200)
        self.assertIsNotNone(composer._message_for(event, "3190568934"))

    def test_session_mismatch_is_rejected(self) -> None:
        composer, _ = make()
        event = FakeEvent(group="577084969")
        event._congling_perceived = note(session="3190568934")
        self.assertIsNone(composer._message_for(event, "577084969"))

    def test_session_of_event(self) -> None:
        composer, _ = make()
        self.assertEqual(composer._session_of(FakeEvent(group="577084969")), ("577084969", "group"))
        self.assertEqual(composer._session_of(FakeEvent()), ("3190568934", "private"))


class ShouldQuoteTest(unittest.TestCase):
    def test_no_message_no_quote(self) -> None:
        composer, _ = make()
        self.assertFalse(composer._should_quote(None))

    def test_empty_msg_id_no_quote(self) -> None:
        composer, _ = make()
        msg = note(msg_id="")
        self.assertFalse(composer._should_quote(msg))


if __name__ == "__main__":
    unittest.main()
