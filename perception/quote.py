"""引用链展开。

关键点不是「拿到被引用的文本」，而是让模型分清三个角色：
当前发言者 / 被引用消息的作者 / 引用里提到的第三方。
搞混这三者，就会出现「用户让甲做事，模型以为是自己要做」这类低级错误。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .render import component_to_block


class QuoteExpander:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg

    async def expand(self, reply_component: Any, depth: int = 0) -> tuple[str, str]:
        """返回 (引用文本, 引用者昵称)。"""
        if reply_component is None:
            return "", ""
        max_depth = self.config.int("perception.quote_depth", 3)
        nickname = str(getattr(reply_component, "sender_nickname", "") or "").strip()

        text = str(getattr(reply_component, "message_str", "") or "").strip()
        chain = getattr(reply_component, "chain", None) or []

        if not text and chain:
            text = self._chain_to_text(chain)
        if not text:
            text = await self._fetch_text(reply_component)

        if not text:
            return "", nickname

        # 引用里还套着引用
        nested = self._find_reply(chain)
        if nested is not None and depth < max_depth:
            nested_text, nested_name = await self.expand(nested, depth + 1)
            if nested_text:
                text = f"{text}（其中又引用了 {nested_name or '某人'} 的话：{nested_text}）"

        return text[:600], nickname

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _chain_to_text(chain: list[Any]) -> str:
        parts: list[str] = []
        for comp in chain:
            block = component_to_block(comp)
            if block is None or block.kind in {"quote"}:
                continue
            parts.append(block.text)
        return " ".join(part for part in parts if part).strip()

    @staticmethod
    def _find_reply(chain: list[Any]) -> Any:
        for comp in chain or []:
            if comp.__class__.__name__ == "Reply":
                return comp
        return None

    async def _fetch_text(self, reply_component: Any) -> str:
        message_id = str(getattr(reply_component, "id", "") or "").strip()
        if not message_id:
            return ""
        bot = getattr(self.plugin, "_current_bot", None)
        if bot is None:
            return ""
        try:
            if hasattr(bot, "call_action"):
                data = await bot.call_action("get_msg", message_id=message_id)
            else:
                data = await bot.api.call_action("get_msg", message_id=message_id)
        except Exception as exc:
            logger.debug(f"[congling] 拉取被引用消息失败: {exc!r}")
            return ""
        payload = data.get("data") if isinstance(data, dict) else {}
        if isinstance(payload, dict):
            raw = payload.get("message") or payload.get("raw_message") or ""
            if isinstance(raw, str):
                return raw
            if isinstance(raw, list):
                return self._chain_to_text(raw)
        return ""
