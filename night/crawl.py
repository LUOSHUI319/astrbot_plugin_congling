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

_ACTIONS = ["翻翻", "看看", "翻一下", "追一下", "瞅瞅", "瞄一眼", "扫一眼", "捋一遍"]
_TARGETS = [
    "昨晚的消息", "刚才的消息", "我不在的时候", "睡前那段",
    "这段", "你们说的", "前面的", "漏掉的那些",
    "你们聊到哪了", "这会儿都发生啥了",
]

# 这些动作能直接接宾语（翻翻昨晚的消息）；其余不行（扫一眼漏掉的那些 ✗）
_TRANSITIVE = {"翻翻", "看看", "翻一下", "追一下", "瞅瞅", "瞄一眼", "扫一眼", "捋一遍"}
_TAILS = [
    "", "等我一下", "稍等", "马上", "我看看", "给我几秒",
    "别急", "我翻完就来", "让我瞅瞅", "缓一缓", "先缓缓神",
]

_FALLBACK = [
    "等我翻翻消息",
    "我看看之前聊了啥",
    "稍等，我看一下你们前面说的",
    "睡过去了，我看看昨天发生了什么",
    "还没睡醒，等我捋捋你们都聊了啥",
    "先别催，我把前面的消息翻一翻",
]


class Crawler:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self._recent: list[str] = []

    # ------------------------------------------------------------ 爬楼语
    async def crawl_line(self, *, umo: str = "", identity: dict[str, Any] | None = None) -> str:
        """现拼一句爬楼语。开了 crawl_use_llm 就让模型现写。"""
        if self.config.bool("night.crawl_use_llm", False):
            line = await self._llm_line(umo=umo, identity=identity)
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

    async def _llm_line(self, *, umo: str = "", identity: dict[str, Any] | None = None) -> str:
        """让模型现写一句爬楼语。失败就返回空，走词池。"""
        model = getattr(self.plugin, "models", None)
        if model is None:
            return ""
        try:
            raw = await model.character_chat(
                "你要去翻一下之前没看的聊天记录。"
                "用一句话说这件事，像人在群里随口说的，不超过 15 个字，"
                "不要问句，不要解释，不要引号。只输出这句话。",
                system="你是一个在群里聊天的人，只说一句话。",
                umo=umo, identity=identity,
                temperature=1.0,
                max_tokens=60,
            )
        except Exception:
            return ""
        line = (raw or "").strip().strip('"“”')
        line = line.splitlines()[0] if line else ""
        return line[:24] if line else ""

    def _assemble(self) -> str:
        # 三种句式随机挑一种，避免全是「动作+消息」一个模子：
        #   0 动作+目标（翻翻昨晚的消息）
        #   1 带迷糊/情绪的开场（刚睡醒，还没缓过来，先看看你们说啥）
        #   2 更口语的完整短句
        style = random.random()
        if style < 0.55:
            body = self._verb_target()
        elif style < 0.8:
            body = self._groggy_open()
        else:
            body = self._casual_line()

        body = body.strip().rstrip("。")
        # 开头偶尔再加个语气词，但别跟 groggy 那类重复了
        if random.random() < 0.2:
            body = random.choice(["等一下，", "诶，", "嗯…", "我看看，"]) + body
        return body

    def _verb_target(self) -> str:
        """动作 + 目标：翻翻昨晚的消息。"""
        action = random.choice(_ACTIONS)
        target = random.choice(_TARGETS) if random.random() < 0.8 else ""
        tail = random.choice(_TAILS)
        if target and action in _TRANSITIVE:
            body = f"{action}{target}"
        elif target:
            body = f"{action}，{target}也看看" if random.random() < 0.5 else f"{action}把{target}看看"
        else:
            body = action
        if tail and random.random() < 0.75:
            body = f"{body}，{tail}"
        return body

    def _groggy_open(self) -> str:
        """带「刚醒还没缓过来」的开场，更有人味。"""
        target = random.choice(_TARGETS)
        opens = [
            f"刚醒，脑子还有点木，我先{random.choice(_ACTIONS)}{target}",
            f"还没睡透，等我{random.choice(_ACTIONS)}{target}",
            f"眯着呢，你们先聊，我{random.choice(_ACTIONS)}{target}",
            f"才缓过来，{random.choice(_ACTIONS)}{target}",
            f"眼皮还沉，先{random.choice(_ACTIONS)}{target}",
        ]
        return random.choice(opens)

    def _casual_line(self) -> str:
        """更口语的完整短句，不拆动作/目标。"""
        return random.choice([
            "你们聊到哪了，我瞅一眼",
            "等等，我把前面的补一补",
            "刚醒，发生啥了",
            "我先翻翻你们都说了啥",
            "醒是醒了，消息还没看，等我",
            "谁喊我呢，我看看",
        ])

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
    async def mark_read(self, session_id: str = "", *, strict: bool = False) -> int:
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
            if strict:
                raise
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
    async def mark_crawled(self, session_id: str, until: int | None = None, *, strict: bool = False) -> None:
        """记下这次爬到了哪里。"""
        stamp = int(until or time.time())
        try:
            raw = await self.store.kv_get(KV_KEY, {}) or {}
            if not isinstance(raw, dict):
                raw = {}
            raw[session_id] = stamp
            await self.store.kv_set(KV_KEY, raw)
        except Exception:
            if strict:
                raise
            pass

    async def last_crawled(self, session_id: str) -> int:
        try:
            raw = await self.store.kv_get(KV_KEY, {}) or {}
            if isinstance(raw, dict):
                return int(raw.get(session_id) or 0)
        except Exception:
            pass
        return 0
