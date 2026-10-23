"""回不回。

这套打分不是为了「拒答」，而是为了不做那种每条消息都秒回的机器。人在群里
大部分时候是看着的，偶尔插一句；私聊则会一直聊下去。这个差别就靠窗口和阈值。

用法：
- 私聊：框架已经叫到模型了，我们用它决定「回长还是回短」，以及要不要敷衍一下；
- 群聊：用它决定要不要主动插话（插话由 proactive 流程执行）。
"""

from __future__ import annotations

import random
import re
import time
from collections import defaultdict
from typing import Any

from ..core.contracts import PerceivedMessage, ReplyDecision

QUESTION_HINTS = ("?", "？", "吗", "呢", "怎么", "为什么", "为啥", "能不能", "是不是", "有没有", "多少")
NAME_HINTS = ("绫",)


class Decider:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self._last_reply: dict[str, float] = {}
        self._last_msg: dict[str, float] = {}
        self._recent_msgs: dict[str, list[tuple[float, str]]] = defaultdict(list)
        self._self_id = ""

    # ================================================================ 观察
    def observe(self, msg: PerceivedMessage) -> None:
        now = time.time()
        self._last_msg[msg.session_id] = now
        bucket = self._recent_msgs[msg.session_id]
        bucket.append((now, msg.text))
        # 只留最近 60 秒
        cutoff = now - 60
        while bucket and bucket[0][0] < cutoff:
            bucket.pop(0)

    def mark_replied(self, session_id: str) -> None:
        self._last_reply[session_id] = time.time()

    # ================================================================ 判断
    async def decide(self, event: Any, msg: PerceivedMessage) -> ReplyDecision:
        signals: dict[str, float] = {}
        is_group = msg.session_kind == "group"

        if not is_group:
            signals["私聊"] = 100.0
        else:
            if self._is_addressed(event, msg):
                signals["被点名"] = 100.0
            elif self._contains_name(msg.text):
                signals["叫了名字"] = 60.0
            if self._is_question(msg.text):
                signals["问句"] = 25.0
            related = self._topic_overlap(msg)
            if related:
                signals["话题相关"] = related

        # 密度惩罚：群里太吵就别挤进去
        density = len(self._recent_msgs.get(msg.session_id, []))
        if is_group and density > 8:
            signals["群太吵"] = -min(30.0, (density - 8) * 5.0)

        # 刚回过且没人接话：别自嗨
        last_reply = self._last_reply.get(msg.session_id, 0.0)
        if last_reply and time.time() - last_reply < 20 and not self._is_addressed(event, msg):
            signals["刚回过"] = -40.0

        # 静默时段（被点名时不惩罚）
        if not self._in_active_hours() and not self._is_addressed(event, msg):
            signals["静默时段"] = -999.0

        threshold = float(self.config.int("humanize.reply_threshold", 35))
        window = float(self.config.int("humanize.reply_window_seconds", 300))
        in_window = bool(last_reply and (time.time() - last_reply) < window)
        if in_window:
            threshold = max(5.0, threshold - 15.0)

        score = sum(signals.values())
        should = score >= threshold
        roll = random.random()
        if not should and score > 0:
            probability = min(1.0, score / max(threshold, 1.0))
            should = roll < probability

        reason = "、".join(f"{key}{value:+.0f}" for key, value in signals.items()) or "无信号"
        return ReplyDecision(
            should_reply=should,
            score=round(score, 1),
            threshold=threshold,
            signals=signals,
            reason=f"{reason} → {score:.0f}/{threshold:.0f}",
            in_window=in_window,
            roll=round(roll, 3),
        )

    # ---------------------------------------------------------------- 细项
    def _is_addressed(self, event: Any, msg: PerceivedMessage) -> bool:
        self_id = self._resolve_self_id(event)
        for mention in msg.mentions:
            if self_id and self_id in mention:
                return True
            if "@" in mention and self.plugin.cfg.persona_name in mention:
                return True
        if msg.quoted and self_id and self_id in (msg.quoted or ""):
            return True
        raw = " ".join(msg.mentions) + " " + (msg.quoted or "")
        return bool(self_id and self_id in raw)

    def _resolve_self_id(self, event: Any) -> str:
        if self._self_id:
            return self._self_id
        for getter in ("get_self_id", "get_bot_id"):
            try:
                func = getattr(event, getter, None)
                if callable(func):
                    value = func()
                    if value:
                        self._self_id = str(value)
                        return self._self_id
            except Exception:
                continue
        bot = getattr(event, "bot", None)
        for attr in ("self_id", "uin"):
            value = getattr(bot, attr, None) if bot is not None else None
            if value:
                self._self_id = str(value)
                return self._self_id
        return ""

    def _contains_name(self, text: str) -> bool:
        name = self.config.persona_name or "绫"
        hints = set(NAME_HINTS) | {name}
        return any(hint and hint in (text or "") for hint in hints)

    @staticmethod
    def _is_question(text: str) -> bool:
        return any(hint in (text or "") for hint in QUESTION_HINTS)

    def _topic_overlap(self, msg: PerceivedMessage) -> float:
        """和最近几条消息的字面重叠，够用且不烧模型。"""
        recent = [text for _, text in self._recent_msgs.get(msg.session_id, [])[-6:-1]]
        if not recent or not msg.text:
            return 0.0
        target = set(msg.text)
        best = 0.0
        for item in recent:
            other = set(item)
            if not other:
                continue
            ratio = len(target & other) / max(1, len(target | other))
            best = max(best, ratio)
        if best >= 0.72:
            return 20.0
        if best >= 0.5:
            return 10.0
        return 0.0

    def _in_active_hours(self) -> bool:
        spec = self.config.str("persona.active_hours", "09:00-03:30")
        try:
            start_text, end_text = spec.split("-", 1)
            start_h, start_m = [int(x) for x in start_text.strip().split(":")]
            end_h, end_m = [int(x) for x in end_text.strip().split(":")]
        except (ValueError, AttributeError):
            return True
        now = time.localtime()
        current = now.tm_hour * 60 + now.tm_min
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start <= end:
            return start <= current < end
        # 跨午夜
        return current >= start or current < end

    # ================================================================ 长度
    def prefer_brief(self, decision: ReplyDecision, msg: PerceivedMessage) -> bool:
        """短回的场景：群里不熟、深夜、对方只说了几个字。"""
        if decision.score < decision.threshold + 10:
            return True
        if msg.session_kind == "group" and len(msg.text) <= 6:
            return True
        hour = time.localtime().tm_hour
        if 0 <= hour < 5 and len(msg.text) < 20:
            return True
        return False


_BRACKET_RE = re.compile(r"\[[^\]]{0,40}\]")
