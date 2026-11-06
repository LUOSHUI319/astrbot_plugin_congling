"""把好友动态写进记忆。

刷空间不是「看一眼就完」——看到的事得留下痕迹，不然下次谁提起来她一脸茫然。
写入走正常的 AtomStore：主体是发动态的人、scope=user、不带 session
（这不是聊天里说的，是隔着屏幕看到的）。

刻意压低权重：别人的日常对一个不参与的人来说价值不高，
importance 0.42、等级 2（transient），衰减快，不会挤占真正重要的记忆。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ..core.contracts import MemoryDraft

# 太短的内容不值得记：「哈哈哈」「。 」「转发」这种记下来只会稀释记忆
MIN_CONTENT_CHARS = 8
MAX_CONTENT_CHARS = 120
MAX_ITEMS_PER_RUN = 8


class FeedMemory:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store

    async def remember(self, items: list[dict[str, Any]], *, limit: int = MAX_ITEMS_PER_RUN) -> int:
        """把动态条目写进记忆，返回写入（或强化）的条数。"""
        memory = getattr(self.plugin, "memory", None)
        atoms = getattr(memory, "atoms", None) if memory is not None else None
        if atoms is None:
            return 0

        written = 0
        for item in list(items or [])[:limit]:
            draft = self._draft(item)
            if draft is None:
                continue
            try:
                await atoms.add(
                    draft,
                    scope="user",
                    persona_id=self.config.persona_id,
                )
                written += 1
            except Exception as exc:
                logger.debug(f"[congling] 空间动态写记忆失败: {exc!r}")
        if written:
            logger.info(f"[congling] 空间动态已记下 {written} 条")
        return written

    def _draft(self, item: dict[str, Any]) -> MemoryDraft | None:
        uin = item.get("uin") or 0
        nick = str(item.get("nickname") or "").strip()
        content = str(item.get("content") or "").strip()
        images = list(item.get("images") or [])
        if not uin or not nick:
            return None
        if len(content) < MIN_CONTENT_CHARS and not images:
            return None

        if content:
            body = content[:MAX_CONTENT_CHARS]
            text = f"{nick}在空间发了：{body}"
        else:
            text = f"{nick}在空间发了一组图（{len(images)} 张）"

        return MemoryDraft(
            kind="event",
            text=text,
            subject=nick,
            subject_key=str(uin),
            # 隔着屏幕看到的，不是自己经历的；睡着期间刷到的也不该等醒来才「能想起来」
            experienced=False,
            importance=0.42,
            confidence=0.7,
            level=2,
            level_reason="空间动态：看到了但没参与",
            level_tags=["transient"],
            platform="qzone",
        )
