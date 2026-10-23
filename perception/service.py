"""感知服务：把一条事件变成「看得懂的消息」。

流程：
  组件遍历 → 基础文本块 → 增强（转发展开 / 引用展开 / 卡片解析 / 媒体理解）
  → PerceivedMessage → 注入 prompt + 入库归档

增强全部带超时。宁可这轮少看见一点，也不能让首响被拖到十几秒。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from astrbot.api import logger

from ..core.contracts import Block, PerceivedMessage
from .card import parse_card
from .forward import ForwardExpander, segments_to_text
from .media import MediaInterpreter
from .quote import QuoteExpander
from .render import blocks_to_text, component_to_block

MEDIA_TIMEOUT = 6.0
MARKER_PREFIX = "<!--congling:perceived:"


class PerceptionService:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self.forward = ForwardExpander(plugin)
        self.quote = QuoteExpander(plugin)
        self.media = MediaInterpreter(plugin)

    # ================================================================ 主流程
    async def perceive(self, event: Any, enrich: bool = True) -> PerceivedMessage:
        # 先挂上当前事件与 bot：转发展开、引用拉取都要用
        self.plugin._current_event = event
        self.plugin._current_bot = getattr(event, "bot", None)

        message_obj = getattr(event, "message_obj", None)
        chain = list(getattr(message_obj, "message", None) or [])

        msg = PerceivedMessage(
            msg_id=str(self._safe(event, "get_message_id") or getattr(message_obj, "message_id", "") or ""),
            session_id=str(self._safe(event, "get_group_id") or self._safe(event, "get_sender_id") or ""),
            session_kind="group" if self._safe(event, "get_group_id") else "private",
            sender_id=str(self._safe(event, "get_sender_id") or ""),
            sender_name=str(self._safe(event, "get_sender_name") or ""),
            ts=int(time.time()),
            raw_components=chain,
        )

        blocks: list[Block] = []
        for comp in chain:
            block = component_to_block(comp)
            if block is None:
                continue
            if block.kind == "forward":
                await self._handle_forward(block, comp)
            elif block.kind == "quote":
                await self._handle_quote(block, comp)
            elif block.kind == "card":
                await self._handle_card(block, comp)
            blocks.append(block)

        msg.blocks = blocks
        msg.text = blocks_to_text(blocks)
        msg.quoted = next((b.text for b in blocks if b.kind == "quote"), "")
        msg.mentions = [b.text for b in blocks if b.kind == "at"]
        msg.is_forward_expanded = any(b.kind == "forward" and b.meta.get("expanded") for b in blocks)

        if enrich:
            await self._enrich_with_timeout(msg)

        # 缓存当前事件与消息，供记忆、拟人、QQ 扩展复用
        self.plugin._current_event = event
        self.plugin._current_bot = getattr(event, "bot", None)
        self.plugin._last_perceived = msg
        return msg

    # ================================================================ 注入
    async def _recent_context(self, msg: PerceivedMessage) -> str:
        """把这个会话最近几条归档消息捞出来当上下文。

        群聊里 bot 只在被 @ 时才醒，中间那些没触发它的消息不进对话历史。
        所以有人先转发一大段、再 @ 它问「看得到吗」，它是真的看不到——
        转发内容虽然入库了，但没进这一轮的 prompt。这里补上。
        """
        limit = self.config.int("perception.recent_context_count", 6)
        if limit <= 0:
            return ""
        try:
            rows = await self.store.fetch_all(
                "SELECT sender_name, text FROM msg_archive "
                "WHERE session_id=? AND ts<=? AND msg_id<>? AND text IS NOT NULL AND text<>'' "
                "ORDER BY id DESC LIMIT ?",
                (msg.session_id, msg.ts, msg.msg_id, int(limit)),
            )
        except Exception as exc:
            logger.debug(f"[congling] 最近消息读取失败: {exc!r}")
            return ""
        if not rows:
            return ""

        budget = self.config.int("perception.recent_context_chars", 1500)
        lines: list[str] = []
        total = 0
        for row in reversed(rows):
            text = " ".join(str(row.get("text") or "").split())
            if not text:
                continue
            if len(text) > 600:
                text = text[:600] + "…"
            name = str(row.get("sender_name") or "")
            line = f"{name}: {text}" if name else text
            if total + len(line) > budget:
                lines.insert(0, "…（更早的略）")
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)

    async def inject(self, event: Any, req: Any) -> None:
        """把特殊消息展开结果注入 prompt（独立区块 + marker 去重）。"""
        msg = await self.perceive(event)
        # 每条被感知到的消息都归档，抽取器才有原料；
        # 之前只有在有转发/引用/图片时才归档，纯文字消息全丢了。
        await self.archive(msg)

        marker = f"{MARKER_PREFIX}{msg.msg_id}-->"
        existing = getattr(req, "system_prompt", "") or ""
        if marker in existing:
            return

        sections = self._prompt_sections(msg)
        recent = await self._recent_context(msg)
        if recent:
            sections.append(
                "以下是这个会话里最近的几条消息（转发已展开），只作上下文参考，"
                "不是当前用户这一句的内容：\n" + recent
            )
        if not sections:
            return

        block = (
            f"\n\n{marker}\n"
            "【消息解析补充 · 以下内容由系统自动展开，不是用户原话】\n"
            + "\n".join(sections)
            + "\n【补充结束】"
        )
        req.system_prompt = existing + block

    def _prompt_sections(self, msg: PerceivedMessage) -> list[str]:
        sections: list[str] = []
        for block in msg.blocks:
            if block.kind == "forward" and block.meta.get("expanded"):
                count = block.meta.get("count", 0)
                sections.append(
                    f"用户转发了一段聊天记录（共 {count} 条）。记录里的说话人是记录中的人，"
                    f"不是当前用户：\n{block.text}"
                )
            elif block.kind == "quote" and block.meta.get("quote_text"):
                sender = block.meta.get("sender") or "某人"
                sections.append(
                    f"当前用户引用了 {sender} 的一条消息：\n「{block.meta.get('quote_text')}」\n"
                    "（被引用内容属于原作者，当前对话里的要求属于当前用户）"
                )
            elif block.kind == "card" and block.meta.get("site"):
                sections.append(f"用户分享了一条{block.meta.get('site')}内容：{block.text}")
            elif block.kind == "image" and block.meta.get("described"):
                sections.append(f"用户发来一张图片：{block.text}")
            elif block.kind == "voice" and block.meta.get("transcribed"):
                sections.append(f"用户发来一条语音，转写成：{block.text}")
        return sections

    # ================================================================ 增强
    async def _handle_forward(self, block: Block, comp: Any) -> None:
        if not self.config.bool("perception.expand_forward", True):
            return
        # Nodes 组件自带内容，不消耗 API
        nodes = getattr(comp, "nodes", None)
        if nodes:
            text, count = self._render_nodes(nodes)
            if text:
                block.text = text
                block.meta.update({"expanded": True, "count": count})
                return
        forward_id = str(block.meta.get("id") or getattr(comp, "id", "") or "")
        if not forward_id:
            return
        text, count = await self.forward.expand(forward_id)
        if not text:
            return
        text = await self.forward.summarize(text)
        block.text = text
        block.meta.update({"expanded": True, "count": count, "forward_id": forward_id})

    @staticmethod
    def _render_nodes(nodes: Any) -> tuple[str, int]:
        lines: list[str] = []
        count = 0
        for node in nodes or []:
            name = str(getattr(node, "name", "") or "某人")
            content = getattr(node, "content", None)
            text = segments_to_text(content)
            if not text:
                continue
            lines.append(f"{name}: {text}")
            count += 1
        return "\n".join(lines), count

    async def _handle_quote(self, block: Block, comp: Any) -> None:
        text, nickname = await self.quote.expand(comp)
        if not text:
            return
        block.text = f"[引用 {nickname or '某人'}：{text[:120]}]"
        block.meta.update({"quote_text": text, "sender": nickname})

    async def _handle_card(self, block: Block, comp: Any) -> None:
        if not self.config.bool("perception.card_parse", True):
            return
        try:
            text, meta = parse_card(comp)
        except Exception as exc:
            logger.debug(f"[congling] 卡片解析失败: {exc!r}")
            return
        if text:
            block.text = text
            block.meta.update(meta)

    async def _enrich_with_timeout(self, msg: PerceivedMessage) -> None:
        tasks: list[asyncio.Task] = []
        for block in msg.blocks:
            if block.kind == "image" and block.meta.get("url"):
                tasks.append(asyncio.create_task(self._describe(block)))
            elif block.kind == "voice" and block.meta.get("url"):
                tasks.append(asyncio.create_task(self._transcribe(block)))
        if not tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=MEDIA_TIMEOUT)
        except asyncio.TimeoutError:
            logger.debug("[congling] 媒体理解超时，本轮先用占位符")
        except Exception as exc:
            logger.debug(f"[congling] 媒体理解异常: {exc!r}")

    async def _describe(self, block: Block) -> None:
        text = await self.media.describe_image(str(block.meta.get("url") or ""))
        if text and text != "[图片]":
            block.text = text
            block.meta["described"] = True

    async def _transcribe(self, block: Block) -> None:
        text = await self.media.transcribe(str(block.meta.get("url") or ""))
        if text and text != "[语音]":
            block.text = text
            block.meta["transcribed"] = True

    # ================================================================ 归档
    async def archive(self, msg: PerceivedMessage) -> None:
        if not msg.text and not msg.blocks:
            return
        try:
            await self.store.execute(
                "INSERT INTO msg_archive(msg_id, session_id, session_kind, sender_id, sender_name, ts, text, blocks_json, expanded, forward_id) "
                "SELECT ?,?,?,?,?,?,?,?,?,? "
                "WHERE NOT EXISTS(SELECT 1 FROM msg_archive WHERE msg_id IS ? AND session_id = ?)",
                (
                    msg.msg_id,
                    msg.session_id,
                    msg.session_kind,
                    msg.sender_id,
                    msg.sender_name,
                    msg.ts,
                    msg.text,
                    json.dumps(
                        [
                            {"kind": b.kind, "text": b.text, "meta": {k: v for k, v in b.meta.items() if k != "chain"}}
                            for b in msg.blocks
                        ],
                        ensure_ascii=False,
                        default=str,
                    )[:20000],
                    1 if msg.is_forward_expanded else 0,
                    next(
                        (
                            b.meta.get("forward_id")
                            for b in msg.blocks
                            if b.kind == "forward" and b.meta.get("forward_id")
                        ),
                        None,
                    ),
                    msg.msg_id,
                    msg.session_id,
                ),
            )
            await self.plugin.bus.emit(self.plugin.bus.ON_PERCEIVED, msg)
        except Exception as exc:
            logger.debug(f"[congling] 消息归档失败: {exc!r}")

    # ================================================================ 工具
    @staticmethod
    def _safe(event: Any, method: str) -> Any:
        try:
            func = getattr(event, method, None)
            return func() if callable(func) else None
        except Exception:
            return None
