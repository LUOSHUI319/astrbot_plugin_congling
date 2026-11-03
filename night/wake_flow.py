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
                      *, exclude_msg_id: str = "") -> list[str]:
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            if self.night.state.state.is_asleep():
                raise RuntimeError("还在睡觉，暂不能主动发送")
            epoch = await self.epoch()
            _, wake = epoch
            key = KEY + session_id
            receipt = await self.store.kv_get(key, {}) or {}
            if receipt.get("sending") or receipt.get("unknown"):
                raise RuntimeError("醒后消息发送结果未知，停止自动重发")
            # 提示按会话和醒来事件去重，不能随未读批次重置。
            # 兼容旧回执：已完成 crawl 阶段保守视为本次提示已处理。
            announced_epoch = receipt.get("announced_epoch")
            if announced_epoch is None and receipt.get("crawl"):
                announced_epoch = receipt.get("epoch")
            # 未完成批次冻结上界；再次睡醒也不能丢弃这批还没有读完的历史。
            until_id = receipt.get("until_id") if not receipt.get("done") else None
            rows = await self.night.crawler.pending(
                session_id, exclude_msg_id=exclude_msg_id, until_id=until_id,
            )
            if not rows:
                return []
            if receipt.get("done") or "until_id" not in receipt:
                count = receipt.get("reply_count", 0) if receipt.get("epoch") == list(epoch) else 0
                receipt = {"epoch": list(epoch), "crawl": False, "done": False,
                           "until_id": int(rows[-1]["id"]), "reply_count": count,
                           "announced_epoch": announced_epoch}
            else:
                receipt["epoch"] = list(epoch)
                receipt["announced_epoch"] = announced_epoch
            # 爬楼负责阅读，补课负责筛选回复；两者都关闭才跳过，并保留未读。
            if not (self.night.rollup.enabled or self.plugin.cfg.bool("night.crawl_enabled", True)):
                return []
            identity = await self.plugin.persona.resolve(umo)
            await self.check(epoch, identity, umo)

            def waking_here() -> bool:
                state = self.night.state.state
                return (wake > 0 and state.mode == "waking"
                        and session_id in (state.pending_crawl or []))

            async def emit(text: str, phase: str) -> bool:
                await self.check(epoch, identity, umo)
                # 文案生成期间可能已超时转为 awake，发送前再次确认醒后需求。
                if not waking_here():
                    return False
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
                if phase == "crawl":
                    receipt["announced_epoch"] = list(epoch)
                await self.store.kv_set(key, receipt)
                return True

            # at_guard 放行只表示可以回复；普通活跃补上下文必须静默。
            if not receipt.get("crawl"):
                announce = self.plugin.cfg.str("night.crawl_announce", "once").strip().lower() != "off"
                need = self.plugin.cfg.bool("night.crawl_enabled", True)
                if (waking_here() and need and announce
                        and receipt.get("announced_epoch") != list(epoch)):
                    line = await self.night.crawler.crawl_line(umo=umo, identity=identity)
                    await emit(line, "crawl")
                else:
                    receipt["crawl"] = True
                    await self.store.kv_set(key, receipt)

            chunks = await self.night.rollup.rollup_session(
                session_id, commit=False, rows=rows, read_only=True,
            )
            replies = {}
            if self.night.rollup.enabled and waking_here():
                for chunk in chunks:
                    plan = await self.night.rollup.triage(chunk)
                    for item in plan.get("to_reply") or []:
                        replies.setdefault(item["item"].msg_id, item)
            if chunks:
                # 每次醒来的回复上限对整次补课生效，而不是每块各发一轮。
                limit = max(0, self.night.rollup._max_replies() - int(receipt.get("reply_count") or 0))
                plan = {"to_reply": list(replies.values())[:limit]}
                if plan["to_reply"] and not receipt.get("reply"):
                    text = await self.night.rollup.compose_reply(
                        plan, umo=umo, identity=identity, strict=True
                    )
                    if await emit(text, "reply"):
                        receipt["reply_persona"] = identity["token"]
                        receipt["reply_count"] = int(receipt.get("reply_count") or 0) + len(plan["to_reply"])
                        await self.store.kv_set(key, receipt)
            await self.check(epoch, identity, umo)
            if chunks:
                await self.night.rollup._save_cursor(session_id, chunks[-1].chunk_end_id)
            await self.night.crawler.mark_read(session_id, strict=True, rows=rows)
            await self.night.crawler.mark_crawled(session_id, strict=True)
            await self.night.state.crawl_done(session_id)
            await self.night.wake_idle.mark_wake(session_id)
            receipt["done"] = True
            receipt["completed_at"] = int(time.time())
            await self.store.kv_set(key, receipt)
            logger.info(f"[congling] 醒后流程完成 session={session_id} wake={wake} "
                        f"persona={identity['mode']}:{identity['id']} read={len(rows)}")
            return [chunk.summary for chunk in chunks if chunk.summary]
