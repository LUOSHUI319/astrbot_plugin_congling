"""表达层：把模型的一大段话，变成人发的几条消息。

这一步是「像不像人」最直观的部分。做对了，同样一句回复，观感完全不同：
- 分句：一段话切成 1~3 条，不是每次都切；
- 轻清理：去 emoji、去句末句号、压平换行；
- 引用：偶尔引一下，不是每条都引；
- 节奏：交给 RhythmController 逐条发、带停顿。

关键取舍：分句不是越碎越好。字数少的回复就该一条，硬切反而像机器人。
"""

from __future__ import annotations

import random
import re
import time
from collections import defaultdict
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Plain, Reply

from ..core.contracts import PerceivedMessage, RhythmPlan

_SENTENCE_RE = re.compile(r"(?<=[。！？!?…～~])")
_CLAUSE_RE = re.compile(r"(?<=[，,、])")

# 配置缺失时的兜底值
QUOTE_COOLDOWN = 30.0
QUOTE_PROBABILITY_MAX = 0.35


class Composer:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.guard = plugin.guard
        self.rhythm = plugin.rhythm
        self._recent_out: dict[str, list[str]] = defaultdict(list)
        self._last_quote: dict[str, float] = {}

    # ================================================================ 入口
    async def decorate(self, event: Any, result: Any) -> None:
        # result.chain 是列表，下面所有操作（插引用、重写分句）都作用在这份拷贝上，
        # 不写回去等于白干——之前引用一直是「判定 True 但发出来没有」就是这个原因。
        chain = list(getattr(result, "chain", None) or [])
        if not chain:
            return
        try:
            await self._compose(event, result, chain)
        finally:
            result.chain = chain

    async def _compose(self, event: Any, result: Any, chain: list[Any]) -> None:
        original_text = self._join_plain(chain)
        if not original_text.strip():
            self.guard.clean_chain(chain)
            return

        msg = getattr(self.plugin, "_last_perceived", None)
        session_id = getattr(msg, "session_id", "") if msg else ""
        session_kind = getattr(msg, "session_kind", "group") if msg else "group"

        bubbles = self.split(original_text, session_kind)
        bubbles = self._dedupe(session_id, bubbles)
        if not bubbles:
            logger.debug("[congling] 回复与最近内容重复，已丢弃")
            chain.clear()
            return

        quote = self._should_quote(msg)
        plan = self.rhythm.build_plan(
            bubbles,
            input_chars=len(getattr(msg, "text", "") or "") if msg else 0,
            session_kind=session_kind,
            quote=quote,
            reason="分句" + ("+引用" if quote else ""),
        )

        if quote and msg is not None:
            self._insert_quote(chain, msg, event)

        attached = self.rhythm.attach(event, plan)
        if not attached:
            # 外部延时插件在场（或接管失败）：把分句结果写回消息链，让框架自己发
            self._rewrite_chain(chain, plan)
            self.guard.clean_chain(chain)

        self._remember(session_id, plan)
        try:
            await self.plugin.decision.planned(
                plan, session_id=session_id, msg_id=getattr(msg, "msg_id", "") if msg else ""
            )
        except Exception:
            pass

    # ================================================================ 分句
    def split(self, text: str, session_kind: str = "group") -> list[str]:
        text = (text or "").strip()
        if not text:
            return []

        # 短回复不切：一两句话本来就该是一口气说完
        if len(text) <= 14:
            return [self.guard.clean(text)]

        raw_parts = [part.strip() for part in _SENTENCE_RE.split(text) if part.strip()]
        parts = self._merge_short(raw_parts)
        parts = self._split_long(parts)
        parts = self._random_regroup(parts)

        max_bubbles = self.config.int(
            "humanize.max_bubbles_private" if session_kind == "private" else "humanize.max_bubbles_group",
            3 if session_kind == "private" else 2,
        )
        if len(parts) > max_bubbles:
            head = parts[: max_bubbles - 1]
            tail = " ".join(parts[max_bubbles - 1 :])
            parts = head + [tail]

        cleaned = [self.guard.clean(part) for part in parts]
        cleaned = [item for item in cleaned if item]
        return cleaned or [self.guard.clean(text)]

    @staticmethod
    def _merge_short(parts: list[str]) -> list[str]:
        result: list[str] = []
        for part in parts:
            if result and len(part) < 6:
                result[-1] = result[-1] + part
            else:
                result.append(part)
        return result

    @staticmethod
    def _split_long(parts: list[str], limit: int = 32) -> list[str]:
        result: list[str] = []
        for part in parts:
            if len(part) <= limit:
                result.append(part)
                continue
            clauses = [item for item in _CLAUSE_RE.split(part) if item.strip()]
            if len(clauses) <= 1:
                result.append(part)
                continue
            buffer = ""
            for clause in clauses:
                if buffer and len(buffer) + len(clause) > limit:
                    result.append(buffer.strip())
                    buffer = clause
                else:
                    buffer += clause
            if buffer.strip():
                result.append(buffer.strip())
        return result

    @staticmethod
    def _random_regroup(parts: list[str]) -> list[str]:
        """人有的时候一口气说完，有的时候一句一句来——轻微扰动就够了。"""
        if len(parts) <= 1:
            return parts
        if random.random() < 0.2:
            # 合并相邻两条
            index = random.randrange(len(parts) - 1)
            parts = parts[:index] + [parts[index] + parts[index + 1]] + parts[index + 2 :]
        elif random.random() < 0.15 and len(parts) >= 2:
            # 把一个长句拆成两截
            index = max(range(len(parts)), key=lambda i: len(parts[i]))
            target = parts[index]
            if len(target) > 18:
                cut = target.rfind("，", 0, len(target) // 2 + 6)
                if cut > 4:
                    parts = parts[:index] + [target[: cut + 1], target[cut + 1 :]] + parts[index + 1 :]
        return parts

    # ================================================================ 去重
    def _dedupe(self, session_id: str, bubbles: list[str]) -> list[str]:
        if not self.config.bool("guard.self_repeat_guard", True):
            return bubbles
        recent = self._recent_out.get(session_id, [])
        result: list[str] = []
        for bubble in bubbles:
            if self.guard.too_similar(bubble, recent):
                continue
            result.append(bubble)
        return result

    def _remember(self, session_id: str, plan: RhythmPlan) -> None:
        if not session_id:
            return
        bucket = self._recent_out[session_id]
        bucket.extend(plan.bubbles)
        while len(bucket) > 5:
            bucket.pop(0)

    # ================================================================ 引用
    def _quote_cooldown(self) -> float:
        try:
            return max(0.0, float(self.config.float("humanize.quote_cooldown_seconds", QUOTE_COOLDOWN)))
        except Exception:
            return QUOTE_COOLDOWN

    def _quote_ceiling(self) -> float:
        try:
            return max(0.0, min(1.0, float(self.config.float("humanize.quote_probability_max", QUOTE_PROBABILITY_MAX))))
        except Exception:
            return QUOTE_PROBABILITY_MAX

    def _should_quote(self, msg: PerceivedMessage | None) -> bool:
        if msg is None:
            return False
        session_id = msg.session_id or ""
        now = time.time()
        cooldown = self._quote_cooldown()
        if cooldown > 0 and now - self._last_quote.get(session_id, 0.0) < cooldown:
            return False
        if not msg.msg_id:
            return False

        probability = self.config.float("humanize.quote_probability", 0.08)
        if getattr(msg, "quoted", ""):
            probability += 0.15
        if len(getattr(msg, "text", "") or "") > 80:
            probability += 0.10
        if getattr(msg, "session_kind", "group") == "private":
            probability -= 0.03
        probability = min(self._quote_ceiling(), max(0.0, probability))

        if random.random() < probability:
            self._last_quote[session_id] = now
            return True
        return False

    @staticmethod
    def _insert_quote(chain: list[Any], msg: PerceivedMessage, event: Any) -> None:
        try:
            reply = Reply(
                id=msg.msg_id,
                sender_id=int(msg.sender_id) if str(msg.sender_id).isdigit() else 0,
                sender_nickname=msg.sender_name,
                time=int(msg.ts),
                message_str=(msg.text or "")[:100],
            )
            chain.insert(0, reply)
        except Exception as exc:
            logger.debug(f"[congling] 构造引用失败: {exc!r}")

    # ================================================================ 工具
    @staticmethod
    def _join_plain(chain: list[Any]) -> str:
        parts: list[str] = []
        for comp in chain:
            if isinstance(comp, Plain):
                parts.append(getattr(comp, "text", "") or "")
        return "".join(parts).strip()

    @staticmethod
    def _rewrite_chain(chain: list[Any], plan: RhythmPlan) -> None:
        """把消息链的文本换成计划好的分句（框架分段回复会据此发送）。"""
        kept: list[Any] = []
        inserted = False
        for comp in chain:
            if isinstance(comp, Plain):
                if not inserted:
                    for index, bubble in enumerate(plan.bubbles):
                        if not bubble:
                            continue
                        kept.append(Plain(text=bubble))
                        inserted = True
                continue
            kept.append(comp)
        chain[:] = kept
