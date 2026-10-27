"""爬楼。

睡着期间的消息照样入库，但都被打上「未曾爬楼」——意思是「我知道有这回事，
但还没看」。被叫醒后要先说一句去翻消息，翻完才能答。

爬楼语必须是现拼的，不能是固定几条轮着来，否则一眼假。
这里用「动作 + 时间指代 + 语气尾」三段随机拼，24 小时内不重复。
"""

from __future__ import annotations

import random
import time
from typing import Any

KV_KEY = "night.crawl"

_ACTIONS = ["翻翻", "爬个楼", "补补课", "看看", "翻一下", "追一下", "补一下"]
_TARGETS = [
    "昨晚的消息", "刚才的消息", "我不在的时候", "睡前那段",
    "这段", "你们说的", "前面的", "漏掉的那些",
]

# 这些动作能直接接宾语（翻翻昨晚的消息）；其余不行（爬个楼漏掉的那些 ✗）
_TRANSITIVE = {"翻翻", "看看", "翻一下", "追一下", "补一下"}
_TAILS = [
    "", "等我一下", "稍等", "马上", "我看看", "给我几秒",
    "别急", "我翻完就来", "让我瞅瞅",
]

_FALLBACK = [
    "等我翻翻消息",
    "我爬个楼看看",
    "稍等，我补一下课",
    "睡过去了，我看看昨天发生了什么",
]


class Crawler:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self._recent: list[str] = []

    # ------------------------------------------------------------ 爬楼语
    async def crawl_line(self) -> str:
        """现拼一句爬楼语。开了 crawl_use_llm 就让模型现写。"""
        if self.config.bool("night.crawl_use_llm", False):
            line = await self._llm_line()
            if line:
                return line
        for _ in range(8):
            text = self._assemble()
            if text and text not in self._recent:
                self._recent.append(text)
                if len(self._recent) > 10:
                    del self._recent[:-10]
                return text
        return random.choice(_FALLBACK)

    async def _llm_line(self) -> str:
        """让模型现写一句爬楼语。失败就返回空，走词池。"""
        model = getattr(self.plugin, "models", None)
        if model is None:
            return ""
        try:
            raw = await model.chat(
                "你要去翻一下之前没看的聊天记录。"
                "用一句话说这件事，像人在群里随口说的，不超过 15 个字，"
                "不要问句，不要解释，不要引号。只输出这句话。",
                system="你是一个在群里聊天的人，只说一句话。",
                temperature=1.0,
                max_tokens=60,
            )
        except Exception:
            return ""
        line = (raw or "").strip().strip('"“”')
        line = line.splitlines()[0] if line else ""
        return line[:24] if line else ""

    def _assemble(self) -> str:
        action = random.choice(_ACTIONS)
        target = random.choice(_TARGETS) if random.random() < 0.8 else ""
        tail = random.choice(_TAILS)

        if target and action in _TRANSITIVE:
            body = f"{action}{target}"
        elif target:
            # 不及物动作不能直接带宾语，中间垫一个"看看"
            body = f"{action}，{target}也看看" if random.random() < 0.5 else f"{action}把{target}看看"
        else:
            body = action

        if tail and random.random() < 0.75:
            body = f"{body}，{tail}"
        body = body.strip().rstrip("。")
        # 开头偶尔加个语气
        if random.random() < 0.3:
            body = random.choice(["等一下，", "诶，", "嗯…", "我看看，"]) + body
        return body

    # ------------------------------------------------------------ 未读统计
    async def unread_count(self, session_id: str, since: int) -> int:
        """统计这个会话从 since 到现在有多少条消息没爬。"""
        if not session_id or since <= 0:
            return 0
        try:
            value = await self.store.scalar(
                "SELECT COUNT(*) FROM msg_archive WHERE session_id = ? AND ts >= ?",
                (session_id, since),
            )
            return int(value or 0)
        except Exception:
            return 0

    async def unread_total(self, sessions: list[tuple[str, int]]) -> int:
        total = 0
        for session_id, since in sessions:
            total += await self.unread_count(session_id, since)
        return total

    # ------------------------------------------------------------ 撤销未读
    async def mark_read(self, session_id: str = "") -> int:
        """把「未曾爬楼」标记撤掉，返回撤了几条。"""
        try:
            if session_id:
                value = await self.store.scalar(
                    "SELECT COUNT(*) FROM mem_atom WHERE status='active' AND unread=1 AND session_id=?",
                    (session_id,),
                )
                await self.store.execute(
                    "UPDATE mem_atom SET unread=0 WHERE unread=1 AND session_id=?",
                    (session_id,),
                )
            else:
                value = await self.store.scalar(
                    "SELECT COUNT(*) FROM mem_atom WHERE status='active' AND unread=1", ())
                await self.store.execute("UPDATE mem_atom SET unread=0 WHERE unread=1", ())
            return int(value or 0)
        except Exception:
            return 0

    async def unread_atoms(self, limit: int = 20) -> list[dict[str, Any]]:
        """面板用：列出还没爬的记忆。"""
        try:
            rows = await self.store.fetch_all(
                "SELECT id, text, kind, session_id, session_label, unread_since, created_at FROM mem_atom "
                "WHERE status='active' AND unread=1 ORDER BY unread_since DESC LIMIT ?",
                (max(1, limit),),
            )
            return [dict(row) for row in rows]
        except Exception:
            return []

    # ------------------------------------------------------------ 记录
    async def mark_crawled(self, session_id: str, until: int | None = None) -> None:
        """记下这次爬到了哪里。"""
        stamp = int(until or time.time())
        try:
            raw = await self.store.kv_get(KV_KEY, {}) or {}
            if not isinstance(raw, dict):
                raw = {}
            raw[session_id] = stamp
            await self.store.kv_set(KV_KEY, raw)
        except Exception:
            pass

    async def last_crawled(self, session_id: str) -> int:
        try:
            raw = await self.store.kv_get(KV_KEY, {}) or {}
            if isinstance(raw, dict):
                return int(raw.get(session_id) or 0)
        except Exception:
            pass
        return 0
