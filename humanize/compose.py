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
_CLAUSE_RE = re.compile(r"(?<=[，,])")
# 换行、分号是作者自己划的段落边界：这是硬分隔，先按它断段，段内再按句末标点分句。
# 以前把它压平成空格再按逗号乱切，模型写好的段落意图全被搅碎。
_BLOCK_RE = re.compile(r"(?:\r?\n)+|[;；]")

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

        # 长文本交给框架的 t2i：本插件只替换 event.send、不改 result.chain，
        # 而 t2i 判定在钩子之后、读的是未拆分的原文。若不撒手，图片会被当成
        # extras 挂到最后一条气泡，文字和图片各发一遍。
        if self._should_yield_to_t2i(original_text):
            logger.debug("[congling] 长文本交给 t2i，跳过分句与发送接管")
            return

        session_id, session_kind = self._session_of(event)
        msg = self._message_for(event, session_id)

        bubbles = self.split(original_text, session_kind)
        bubbles = self._dedupe(session_id, bubbles)
        if not bubbles:
            logger.debug("[congling] 回复与最近内容重复，已丢弃")
            chain.clear()
            return

        quote = self._should_quote(msg)
        delayer = getattr(self.plugin, "delayer", None)
        hold = 0.0
        if delayer is not None:
            try:
                delayer.describe()
                hold = delayer.extra_hold(session_kind) if delayer.should_hold(session_kind) else 0.0
            except Exception:
                hold = 0.0
        plan = self.rhythm.build_plan(
            bubbles,
            input_chars=len(getattr(msg, "text", "") or "") if msg else 0,
            session_kind=session_kind,
            quote=quote,
            reason="分句" + ("+引用" if quote else "") + (f"+犹豫{hold:.1f}s" if hold else ""),
            extra_hold=hold,
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

    # ------------------------------------------------------------ t2i 让位
    def _should_yield_to_t2i(self, text: str) -> bool:
        """超长文本交给框架转图片，本插件不接管发送。

        AstrBot 在 result_decorate 阶段先跑 on_decorating_result、再判 t2i。
        插件只替换 event.send、不改 result.chain，所以 t2i 看到的是未拆分的原文：
        超阈值时整条转图，随后 event.send 把这张图当 extras 挂到最后一条气泡上，
        结果文字和图片各发一遍。这里提前撒手，让框架走自己的 t2i 流程，
        换行也得以保留，图片排版更整齐。
        """
        try:
            cfg = self.plugin.context.get_config() or {}
        except Exception:
            return False
        if not cfg.get("t2i"):
            return False
        try:
            threshold = int(cfg.get("t2i_word_threshold") or 150)
        except (TypeError, ValueError):
            threshold = 150
        # 框架拼 plain_str 时每个 Plain 前会补 "\n\n"，长度按 +2 对齐
        return len(text) + 2 > threshold

    # ================================================================ 分句
    def split(self, text: str, session_kind: str = "group") -> list[str]:
        text = (text or "").strip()
        if not text:
            return []

        # 短回复不切：一两句话本来就该是一口气说完
        if len(text) <= 14:
            return [self.guard.clean(text)]

        # 1) 按换行/分号断段（作者自己划的段落，优先级最高）
        # 2) 段内按句末标点断句 → 3) 合并碎句 → 4) 超长句再按逗号切
        groups: list[list[str]] = []
        for block in _BLOCK_RE.split(text):
            block = block.strip()
            if not block:
                continue
            pieces = [item.strip() for item in _SENTENCE_RE.split(block) if item.strip()]
            pieces = self._merge_short(pieces)
            pieces = self._split_long(pieces)
            # 长句切完再合一次：别留「哦这个，」这种 3~5 字的孤立气泡
            pieces = self._merge_short(pieces)
            if pieces:
                groups.append(pieces)

        if not groups:
            return [self.guard.clean(text)]

        max_bubbles = self.config.int(
            "humanize.max_bubbles_private" if session_kind == "private" else "humanize.max_bubbles_group",
            3 if session_kind == "private" else 2,
        )
        # 按段分桶：段数没超标就一段一条，超标了才按顺序并桶，尽量不把一段拆开
        parts = [self._join_parts(bucket) for bucket in self._bucket(groups, max_bubbles)]
        parts = self._random_regroup(parts)
        # 随机扰动会改变条数，上限得再兜一次
        if len(parts) > max_bubbles:
            parts = parts[: max_bubbles - 1] + [self._join_parts(parts[max_bubbles - 1 :])]

        cleaned = [self.guard.clean(part) for part in parts]
        cleaned = [item for item in cleaned if item]
        return cleaned or [self.guard.clean(text)]

    @staticmethod
    def _bucket(groups: list[list[str]], limit: int) -> list[list[str]]:
        """把若干段合并成不超过 limit 个气泡，顺序不变、按字数尽量均衡。

        以前是按「组数」均分，结果是一桶塞两段、另一桶只剩一小截，
        发出来一条巨长一条极短，很怪。
        """
        limit = max(1, limit)
        if len(groups) <= limit:
            return [list(group) for group in groups]
        total = sum(sum(len(item) for item in group) for group in groups)
        target = total / limit
        buckets: list[list[str]] = []
        current: list[str] = []
        current_len = 0
        remaining = limit
        for group in groups:
            group_len = sum(len(item) for item in group)
            if current and remaining > 1 and current_len + group_len / 2 > target:
                buckets.append(current)
                current = []
                current_len = 0
                remaining -= 1
            current.extend(group)
            current_len += group_len
        if current:
            buckets.append(current)
        return buckets

    @staticmethod
    def _join_parts(parts: list[str]) -> str:
        """拼回一条消息：中文不加空格（中英混排处会挤出怪空格），缺口补个句号。"""
        buffer = ""
        for part in parts:
            part = (part or "").strip()
            if not part:
                continue
            if buffer and buffer[-1] not in "。！？!?…～~，,、；;：:":
                buffer += "。"
            buffer += part
        return buffer

    @staticmethod
    def _merge_short(parts: list[str]) -> list[str]:
        result: list[str] = []
        for part in parts:
            if result and len(part) < 6:
                result[-1] = result[-1] + part
            else:
                result.append(part)
        # 开头就三五字的碎片也别单独占一条气泡，往后并
        if len(result) > 1 and len(result[0]) < 6:
            result[1] = result[0] + result[1]
            result.pop(0)
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
        """人有的时候一口气说完，有的时候一句一句来——轻微扰动就够了。

        只做「合并相邻两条」这一种扰动。以前还带一个「拆长句」分支，
        但兜底会按条数上限再合回去，一拆一合等于把第一条削短、第二条塞长，
        越扰动越难看，索性去掉。
        """
        if len(parts) <= 1:
            return parts
        if random.random() < 0.2:
            index = random.randrange(len(parts) - 1)
            merged = parts[index] + parts[index + 1]
            # 合并完几百字一条，比不合并还假，那就别合
            if len(merged) <= 90:
                parts = parts[:index] + [merged] + parts[index + 2 :]
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
    # ------------------------------------------------------------ 引用谁
    @staticmethod
    def _session_of(event: Any) -> tuple[str, str]:
        try:
            group_id = str(event.get_group_id() or "")
            if group_id:
                return group_id, "group"
            return str(event.get_sender_id() or ""), "private"
        except Exception:
            return "", "private"

    def _message_for(self, event: Any, session_id: str) -> PerceivedMessage | None:
        """这条回复对应的那条消息。

        以前直接拿 `plugin._last_perceived`——那是全局「最后一条被感知的消息」，
        于是三种情况都会引错：
        - 主动消息 / 定时任务发的回复：上下文里根本没有新消息，引用会落到几小时前那条
        - 两条消息挨着进来：先处理完的那条会引用后一条
        - 延时回复：等它发的时候 _last_perceived 早换了
        现在优先取挂在事件上的那一份，并且要求会话对得上、不是太久以前的事。
        """
        msg = getattr(event, "_congling_perceived", None)
        if msg is None:
            return None
        # 会话对不上说明事件被复用或串了，宁可不引用
        msg_session = str(getattr(msg, "session_id", "") or "")
        if session_id and msg_session and msg_session != session_id:
            return None
        # 引用一条几分钟前的话，对方看着莫名其妙
        try:
            age = time.time() - float(getattr(msg, "ts", 0) or 0)
        except (TypeError, ValueError):
            age = 0.0
        limit = self.config.float("humanize.quote_max_age_seconds", 180)
        if limit > 0 and age > limit:
            return None
        return msg

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
        # 没有可引用的 id 就别引：id 是空的或者占位值，QQ 那边会引到别的地方去
        msg_id = str(getattr(msg, "msg_id", "") or "").strip()
        if not msg_id or msg_id in ("0", "None", "null"):
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
                id=str(msg.msg_id).strip(),
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
