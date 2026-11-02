"""醒后静默待机（双控制的第二个时钟）。

``wake_window_minutes``（唤醒计数窗口）是「有新消息就重新计时」的滑动窗，
管「睡着 → 被叫醒」的掷骰期（在 wake.py 里实现）。

本模块实现第二个独立时钟 ``wake_idle_minutes``（醒后静默待机）：
醒着的时候，最后一条消息过去 N 分钟无新动静，就触发「想继续睡」。
触发前 ``idle_merge_seconds`` 内有回复则并入主回复，否则单独发。
同一段静默只触发一次提示，不在此处改变睡眠状态。

两个时钟作用在不同状态阶段，互不重叠：
- wake_window_minutes 只在 asleep 阶段计数（wake.py）
- wake_idle_minutes   只在 awake / waking 阶段计时（本模块）

情绪池：强制唤醒时从 ``wake_mood_pool`` 抽一个情绪，配合人设临时接话。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from ..core.contracts import NIGHT_AWAKE, NIGHT_WAKING

LOG = "[congling:idle]"

# kv：醒后待机的时间标记。session_id -> 最后一条消息的 unix 时间戳
IDLE_MARK_KEY = "night.wake_idle.mark"

# 醒来时刻的标记（session_id -> 醒的时间戳），用于「醒后一直没人说话」也触发
WAKE_MARK_KEY = "night.wake_idle.wake_at"
# 每轮静默仅认领一次；落库后重载也不会把同一轮再触发。
FIRED_KEY = "night.wake_idle.fired"


@dataclass
class IdleDecision:
    """一次待机判定的结果。"""

    trigger: bool = False     # 是否该「想继续睡」
    idle_seconds: int = 0     # 已经静默多久
    idle_minutes: int = 10    # 配置的阈值（分钟）
    reason: str = ""


class WakeIdle:
    """醒后静默待机计时器。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self._pending_line: dict[str, str] = {}  # session_id -> 待并入的「想继续睡」文本
        self._pending_at: dict[str, float] = {}
        self._pending_persona: dict[str, str] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 参数
    def _idle_minutes(self) -> int:
        return max(0, self.config.int("night.rollup.wake_idle_minutes", 10))

    def _merge_seconds(self) -> int:
        return max(0, self.config.int("night.rollup.idle_merge_seconds", 60))

    def _mood_pool(self) -> list[str]:
        return [str(x).strip() for x in self.config.list("night.rollup.wake_mood_pool", []) if str(x).strip()]

    # ------------------------------------------------------------ 时间标记
    async def mark_wake(self, session_id: str) -> None:
        """醒来时标记时间。醒后一直没人说话也按这个起算。"""
        async with self._lock:
            self.discard(session_id)
            await self.store.kv_set(f"{WAKE_MARK_KEY}:{session_id}", int(time.time()))
            await self.store.execute("DELETE FROM kv WHERE k=?", (f"{FIRED_KEY}:{session_id}",))

    async def note_message(self, session_id: str) -> None:
        """醒着时每处理一条消息都刷新「最后消息时间」。"""
        async with self._lock:
            self.discard(session_id)
            await self.store.kv_set(f"{IDLE_MARK_KEY}:{session_id}", int(time.time()))
            await self.store.execute("DELETE FROM kv WHERE k=?", (f"{FIRED_KEY}:{session_id}",))

    async def _last_activity(self, session_id: str) -> int:
        """以消息和本次醒来中较晚的时间起算，避免使用上次睡前的旧消息。"""
        msg_ts = await self.store.kv_get(f"{IDLE_MARK_KEY}:{session_id}", None)
        wake_ts = await self.store.kv_get(f"{WAKE_MARK_KEY}:{session_id}", None)
        return max(int(msg_ts or 0), int(wake_ts or 0))

    async def clear(self, session_id: str) -> None:
        """入睡/重新入睡时清掉标记。"""
        self.discard(session_id)
        try:
            await self.store.execute(
                "DELETE FROM kv WHERE k=? OR k=? OR k=?",
                (f"{IDLE_MARK_KEY}:{session_id}", f"{WAKE_MARK_KEY}:{session_id}",
                 f"{FIRED_KEY}:{session_id}"),
            )
        except Exception:
            pass

    async def _active_sessions(self) -> list[str]:
        """列出有醒时标记、可能还在待机状态的会话（供 idle 心跳扫描）。"""
        try:
            rows = await self.store.fetch_all(
                "SELECT k FROM kv WHERE k LIKE ?",
                (f"{WAKE_MARK_KEY}:%",),
            )
            out: list[str] = []
            prefix = f"{WAKE_MARK_KEY}:"
            for r in rows:
                k = str(r.get("k") or "")
                if k.startswith(prefix):
                    sid = k[len(prefix):]
                    if sid:
                        out.append(sid)
            return out
        except Exception:
            return []

    # ------------------------------------------------------------ 判定
    def eligible(self) -> bool:
        state = getattr(getattr(getattr(self.plugin, "night", None), "state", None), "state", None)
        return bool(state is not None and state.mode in (NIGHT_AWAKE, NIGHT_WAKING)
                    and state.last_wake_reason in ("message", "manual")
                    and float(state.energy) < self.config.float("night.energy_max", 100))

    def discard(self, session_id: str) -> None:
        self._pending_line.pop(session_id, None)
        self._pending_at.pop(session_id, None)
        self._pending_persona.pop(session_id, None)

    def take_line(self, session_id: str, persona_token: str = "") -> str:
        line = self._pending_line.get(session_id, "")
        valid = (self.eligible() and 0 <= time.time() - self._pending_at.get(session_id, 0)
                 <= self._merge_seconds()
                 and self._pending_persona.get(session_id, "") == persona_token)
        self.discard(session_id)
        return line if valid else ""

    async def check(self, session_id: str) -> IdleDecision:
        """醒着时由心跳调用：静默超时则触发「想继续睡」。"""
        idle_minutes = self._idle_minutes()
        if not self.eligible():
            self.discard(session_id)
            return IdleDecision(reason="自然醒、满精力或非醒后状态，不提示继续睡")
        state = self.plugin.night.state.state
        wake_mark = int(await self.store.kv_get(f"{WAKE_MARK_KEY}:{session_id}", 0) or 0)
        if wake_mark < state.last_wake_at:
            self.discard(session_id)
            return IdleDecision(reason="旧醒来周期的待机标记已失效")
        if idle_minutes <= 0:
            return IdleDecision(trigger=False, idle_minutes=0, reason="醒后静默待机已关闭")
        if await self.store.kv_get(f"{FIRED_KEY}:{session_id}", False):
            return IdleDecision(trigger=False, idle_minutes=idle_minutes, reason="本轮静默已经触发")
        last = await self._last_activity(session_id)
        if last <= 0:
            return IdleDecision(trigger=False, idle_seconds=0, idle_minutes=idle_minutes)
        idle_seconds = int(time.time()) - last
        if idle_seconds < idle_minutes * 60:
            return IdleDecision(
                trigger=False, idle_seconds=idle_seconds, idle_minutes=idle_minutes
            )
        return IdleDecision(
            trigger=True,
            idle_seconds=idle_seconds,
            idle_minutes=idle_minutes,
            reason=f"醒后静默 {idle_seconds // 60} 分钟，想继续睡",
        )

    async def claim(self, session_id: str) -> IdleDecision:
        """心跳认领一次提示；锁和持久化标记防并发/重载重复触发。"""
        async with self._lock:
            decision = await self.check(session_id)
            if decision.trigger:
                await self.store.kv_set(f"{FIRED_KEY}:{session_id}", True)
            return decision

    # ------------------------------------------------------------ 情绪池
    def pick_mood(self) -> str:
        """从情绪池抽一个情绪词。空池返回空串。"""
        pool = self._mood_pool()
        if not pool:
            return ""
        return random.choice(pool)

    def compose_sleepy_line(self, mood: str = "") -> str:
        """拼一句「还想再睡会儿」的话。

        情绪词是给下游 LLM 当语气基调用的，**不拼进正文**——直接打「（生气）」
        这种会把内心 OS 露出来。这里只给一句不带情绪词的开场，具体语气交给
        prompt_hint 里那段内部提示去带。
        """
        return "我还想再睡会儿……"

    def prompt_hint(self, mood: str = "") -> str:
        """给 system prompt 加一段「还想睡」的状态提示，供强制唤醒时临时接话。

        情绪词只作**内部语气基调**，明确要求不把情绪词本身写进回复、不写内心 OS。
        """
        if not mood:
            return ""
        return (
            f"你刚被强行叫醒，心里还带着一点「{mood}」的情绪。"
            "接话时把这种情绪**体现在语气里**，但不要把情绪词本身写出来，"
            "也不要写任何内心独白或括号注释，只像平常聊天一样自然说一句，"
            "表达还想再睡会儿、可以晚点再说。"
        )
