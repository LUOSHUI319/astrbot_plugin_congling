"""长夜如梦。

一个板块，四件事：作息（state）、道别（lullaby）、唤醒（wake）、做梦（dream）。
爬楼和结束语识别是配套的零件，不单独对外。

对外只暴露 NightModule，其余都是内部件。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from ..core.contracts import NIGHT_ASLEEP, NIGHT_AWAKE, PerceivedMessage
from .close import close_minutes, is_closing, match_strength
from .crawl import Crawler
from .dream import Dreamer
from .state import NightService

__all__ = [
    "NightModule",
    "NightService",
    "Dreamer",
    "Crawler",
    "is_closing",
    "match_strength",
    "close_minutes",
]


class NightModule:
    """把作息、做梦、爬楼装在一起，给 main 一个门面。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.state = NightService(plugin)
        self.dreamer = Dreamer(plugin)
        self.crawler = Crawler(plugin)
        self._waking_started: dict[str, int] = {}
        self.pending_sleep: dict[str, Any] | None = None
        self.morning_dream: Any = None

    # ================================================================ 生命周期
    async def start(self) -> None:
        await self.state.start()
        if self.dreamer.enabled:
            await self.dreamer.load()

    async def shutdown(self) -> None:
        await self.state.save()
        if self.dreamer.enabled:
            await self.dreamer.save()

    # ================================================================ 消息入口
    async def on_message(self, msg: PerceivedMessage, *, self_id: str = "") -> dict[str, Any]:
        """睡眠期拦截 + 唤醒判定 + 收线判定。返回 state.on_message 的结果。"""
        result = await self.state.on_message(msg, self_id=self_id)

        # 正在醒：先爬楼再说
        if self.state.state.mode == "waking":
            started = self._waking_started.get(msg.session_id)
            if not started:
                self._waking_started[msg.session_id] = int(time.time())

        # 醒着的时候判断这句话是不是在收线
        if not self.state.state.is_asleep() and self.config.bool("close.enabled", True):
            closing, strength, word = is_closing(msg.text, self.config)
            if closing and strength in ("strong", "medium"):
                kind = "noon" if ("午" in word or "眯" in word or "盹" in word) else "night"
                self.pending_sleep = {
                    "kind": kind,
                    "reason": "talk",
                    "word": word,
                    "strength": strength,
                    "session_id": msg.session_id,
                }
                result["closing"] = True
                result["hint"] = f"对方收线（{word}），回完这句就睡"

        # 精力耗尽触发的入睡，也要给一次道别的机会
        if result.get("sleep_triggered"):
            kind = "night"
            try:
                from . import energy as energy_mod

                kind = energy_mod.fatigue_kind(self.config) or "night"
            except Exception:
                pass
            self.pending_sleep = {"kind": kind, "reason": "energy", "session_id": msg.session_id}

        return result

    async def after_reply(self, msg: PerceivedMessage | None = None, *, session_id: str = "") -> None:
        """回完一句话之后：正在醒就先爬楼，否则执行收线入睡。"""
        # 1) 爬楼：醒来后的第一句回复发完，就算翻完了
        if self.state.state.mode == "waking":
            try:
                await self.crawl(session_id)
                self._pick_morning_dream()
            except Exception as exc:
                logger.debug(f"[congling] 爬楼失败: {exc!r}")
            return

        # 2) 收线：刚才那句已经是最后一句，现在真的去睡
        pending = self.pending_sleep
        if not pending:
            return
        self.pending_sleep = None
        self.morning_dream = None
        try:
            await self.state.sleep_now(reason=pending.get("reason", "talk"), kind=pending.get("kind") or None)
            logger.info(
                f"[congling] 入睡：{pending.get('reason')} "
                f"（{pending.get('word') or '精力耗尽'}）"
            )
        except Exception as exc:
            logger.debug(f"[congling] 入睡失败: {exc!r}")

    def _pick_morning_dream(self) -> None:
        """醒来时掷一次：这段梦记不记得，记得就等着找机会说。"""
        try:
            dream = self.dreamer.pick_to_mention()
        except Exception:
            dream = None
        self.morning_dream = dream
        if dream is not None:
            logger.info(f"[congling] 醒来记得那个梦：{dream.level} 级 / {'、'.join(dream.tags)}")

    # ================================================================ 爬楼
    async def need_crawl(self, session_id: str) -> int:
        if not self.config.bool("night.crawl_enabled", True):
            return 0
        sleep_at = self.state.state.sleep_at
        if sleep_at <= 0:
            return 0
        return await self.crawler.unread_count(session_id, sleep_at)

    async def crawl(self, session_id: str) -> str:
        """执行爬楼，返回要说的话。"""
        line = await self.crawler.crawl_line()
        removed = await self.crawler.mark_read(session_id)
        await self.crawler.mark_crawled(session_id)
        await self.state.finish_waking()
        self._waking_started.pop(session_id, None)
        if removed:
            logger.info(f"[congling] 爬楼完成：撤销 {removed} 条未读（{session_id}）")
        return line

    # ================================================================ 做梦
    async def dream_tick(self) -> None:
        """由 housekeeping 定时调用：该做梦就做一段。"""
        if not self.dreamer.enabled:
            return
        state = self.state.state
        if state.mode != NIGHT_ASLEEP:
            return
        if state.dream_skip:
            return
        if not self.dreamer.due(
            sleep_at=state.sleep_at,
            already=state.dream_count_today,
        ):
            return
        last = self.dreamer.list_recent(1)
        continuation = ""
        if last:
            continuation = (last[0].text or "")[-200:]
        entry = await self.dreamer.make(continuation=continuation)
        if entry is not None:
            state.dream_count_today += 1
            await self.state.save()

    # ================================================================ 面板
    def prompt_hint(self) -> str:
        """给 system prompt 加一段作息状态。空串表示不用加。"""
        state = self.state.state
        if state.mode == "asleep":
            return ""

        from . import energy as energy_mod

        lines: list[str] = []
        if state.mode == "waking":
            lines.append("你刚从睡眠里被叫醒，昨晚的消息还没看。不要直接回答具体内容，先说一句你要去翻消息。")

        if self.pending_sleep:
            limit = max(4, self.config.int("close.max_closing_chars", 12))
            if self.config.bool("night.farewell_before_sleep", True):
                lines.append(
                    f"这句话说完你就要去睡了。回复里带上道别，控制在 {limit} 字以内，"
                    "不要抛问题、不要开新话题、不要约明天的事。"
                )
            else:
                lines.append(f"这句话说完你就去睡了，回复控制在 {limit} 字以内，简短收尾就行。")

        if self.morning_dream is not None:
            dream = self.morning_dream
            excerpt = (dream.text or "")[:120]
            lines.append(
                f"你昨晚做了一个梦（{'、'.join(dream.tags) or '说不清'}，强度 {dream.level} 级）：{excerpt}"
                "\n如果这轮对话有自然的缝隙，可以顺口提一句这个梦；没有就别说，别硬插。"
            )

        kind = energy_mod.fatigue_kind(self.config)
        if kind:
            percent = max(0, int(state.energy))
            if percent <= 25:
                tone = "已经很困了，脑子转不太动，说话会短、会卡"
            elif percent <= 55:
                tone = "有点犯困，反应慢半拍"
            else:
                tone = "开始犯困了，但还能撑"
            lines.append(f"现在是疲劳时段，你的精力还剩 {percent}%，{tone}。")

        if not lines:
            return ""
        return "【作息状态】\n" + "\n".join(f"- {line}" for line in lines)

    def snapshot(self) -> dict[str, Any]:
        data = self.state.snapshot()
        data["dream"] = {
            "enabled": self.dreamer.enabled,
            "stats": self.dreamer.stats() if self.dreamer.enabled else {},
        }
        return data

    def status_text(self) -> str:
        return self.state.status_text()
