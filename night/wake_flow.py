"""普通回复和主动发送共用醒后流程，阶段成功发送后才推进。

发送异常可能表示平台已收到消息：落盘 sending/unknown 时不自动重发，
也不允许主动正文越过它。生成失败则可重试，没有聊天副作用。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from ..core.memory_guard import memory_work

KEY = "night.wake_flow:"


class WakeFlow:
    def __init__(self, night: Any) -> None:
        self.night = night
        self.plugin = night.plugin
        self.store = night.store
        self._locks: dict[str, asyncio.Lock] = {}

    async def epoch(self) -> tuple[int, int]:
        since = await self.night._sleep_since()
        wake = int(self.night.state.state.last_wake_at or 0)
        return since, wake

    async def check(self, epoch: tuple[int, int], identity: dict[str, Any], umo: str) -> None:
        if self.night.state.state.is_asleep() or await self.epoch() != epoch:
            raise RuntimeError("睡眠状态已变化，暂停发送")
        if (await self.plugin.persona.resolve(umo))["token"] != identity["token"]:
            raise RuntimeError("人格已切换，暂停旧角色发送")

    @memory_work
    async def prepare(self, session_id: str, umo: str,
                      send: Callable[[str, str, dict[str, Any]], Awaitable[None]],
                      *, exclude_msg_id: str = "") -> None:
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            if self.night.state.state.is_asleep():
                raise RuntimeError("还在睡觉，暂不能主动发送")
            epoch = await self.epoch()
            since, wake = epoch
            if not since or not wake or wake < since:
                return
            key = KEY + session_id
            receipt = await self.store.kv_get(key, {}) or {}
            if receipt.get("epoch") != list(epoch):
                receipt = {"epoch": list(epoch), "crawl": False, "done": False}
            if receipt.get("sending") or receipt.get("unknown"):
                raise RuntimeError("醒后消息发送结果未知，停止自动重发")
            if receipt.get("done"):
                return
            identity = await self.plugin.persona.resolve(umo)
            await self.check(epoch, identity, umo)

            async def emit(text: str, phase: str) -> None:
                await self.check(epoch, identity, umo)
                receipt["sending"] = phase
                await self.store.kv_set(key, receipt)
                try:
                    await send(text, phase, identity)
                except BaseException:
                    receipt["unknown"] = phase
                    await self.store.kv_set(key, receipt)
                    raise
                receipt.pop("sending", None)
                # 成功阶段立即落盘；后续失败不能重新发送已经成功的阶段。
                receipt[phase] = True
                await self.store.kv_set(key, receipt)

            if not receipt.get("crawl"):
                announce = self.plugin.cfg.str("night.crawl_announce", "once") != "off"
                need = (self.plugin.cfg.bool("night.crawl_enabled", True)
                        and await self.night.crawler.unread_count(session_id, since)
                        and await self.night.crawler.last_crawled(session_id) < wake)
                if need and announce:
                    line = await self.night.crawler.crawl_line(umo=umo, identity=identity)
                    await emit(line, "crawl")
                else:
                    receipt["crawl"] = True
                    await self.store.kv_set(key, receipt)

            chunks = []
            if self.night.rollup.enabled:
                chunks = await self.night.rollup.rollup_session(
                    session_id, since, until_ts=wake, exclude_msg_id=exclude_msg_id, commit=False
                )
                replies = []
                for chunk in chunks:
                    plan = await self.night.rollup.triage(chunk)
                    replies.extend(plan.get("to_reply") or [])
                # 每次醒来的回复上限对整次补课生效，而不是每块各发一轮。
                plan = {"to_reply": replies[:self.night.rollup._max_replies()]}
                if plan["to_reply"] and not receipt.get("reply"):
                    text = await self.night.rollup.compose_reply(
                        plan, umo=umo, identity=identity, strict=True
                    )
                    await emit(text, "reply")
                    receipt["reply_persona"] = identity["token"]
                    await self.store.kv_set(key, receipt)
            await self.check(epoch, identity, umo)
            if chunks:
                await self.night.rollup._save_cursor(session_id, chunks[-1].chunk_end_id)
            await self.night.crawler.mark_read(session_id, strict=True)
            await self.night.crawler.mark_crawled(session_id, strict=True)
            await self.night.state.crawl_done(session_id)
            await self.night.wake_idle.mark_wake(session_id)
            receipt["done"] = True
            receipt["completed_at"] = int(time.time())
            await self.store.kv_set(key, receipt)
            logger.info(f"[congling] 醒后流程完成 session={session_id} wake={wake} "
                        f"persona={identity['mode']}:{identity['id']}")
