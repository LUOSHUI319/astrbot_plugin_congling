"""唤醒判定。

三档策略 + 概率递增。设计要点：

1. 计数是「窗口内累计」，不是连击——只要还在 x 分钟里就一直攒
2. 概率按条数平分，攒到 y+n 必醒，所以再难叫也有上限
3. 群聊在 private_only 档下完全不累计，等于静默
4. 掷不中不清零，下次继续攒
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.contracts import PerceivedMessage, WakeAttempt

MODE_NEVER = "never"
MODE_PRIVATE = "private_only"
MODE_BOTH = "both"


@dataclass
class _Counter:
    count: int = 0
    window_start: float = 0.0


class Waker:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self._counters: dict[str, _Counter] = {}

    # ------------------------------------------------------------ 配置位
    @property
    def mode(self) -> str:
        raw = str(self.config.str("night.wake_mode", MODE_PRIVATE) or MODE_PRIVATE).strip()
        if raw not in (MODE_NEVER, MODE_PRIVATE, MODE_BOTH):
            return MODE_PRIVATE
        return raw

    def _window_seconds(self) -> float:
        minutes = self.config.int("night.wake_window_minutes", 10)
        return max(1, minutes) * 60.0

    def _start_count(self) -> int:
        return max(1, self.config.int("night.wake_start_count", 2))

    def _step_count(self) -> int:
        return max(0, self.config.int("night.wake_step_count", 4))

    # ------------------------------------------------------------ 主判定
    def check(self, msg: PerceivedMessage, *, self_id: str = "") -> WakeAttempt:
        """msg 是一条睡眠期间收到的消息。返回这次掷骰结果。"""
        mode = self.mode
        if mode == MODE_NEVER:
            return WakeAttempt(allowed=False, reason="档位为不可唤醒")

        is_group = msg.session_kind == "group"

        if is_group and mode == MODE_PRIVATE:
            return WakeAttempt(allowed=False, reason="群聊已被静默")

        # 群聊要算「被 @ 的次数」，没 @ 就不计数
        if is_group:
            if not self._is_at_me(msg, self_id):
                return WakeAttempt(allowed=False, reason="群聊没被点名，不计数")

        counter = self._bump(msg.session_id)

        start = self._start_count()
        step = self._step_count()
        need = start + step

        if counter.count < start:
            return WakeAttempt(
                allowed=False,
                count=counter.count,
                needed=start,
                chance=0.0,
                reason=f"还差 {start - counter.count} 次才开始掷骰",
            )

        # 概率 = (count - start + 1) / (step + 1)
        reached = counter.count - start + 1
        chance = min(1.0, reached / (step + 1))
        roll = random.random()
        woke = roll < chance

        if woke:
            self.reset(msg.session_id)

        return WakeAttempt(
            allowed=True,
            woke=woke,
            count=counter.count,
            needed=need,
            chance=round(chance, 3),
            roll=round(roll, 3),
            reason=f"第 {counter.count} 次，概率 {chance:.0%}" + ("，醒了" if woke else "，没醒"),
        )

    # ------------------------------------------------------------ 计数
    def _bump(self, session_id: str) -> _Counter:
        now = time.time()
        window = self._window_seconds()
        counter = self._counters.get(session_id)
        if counter is None or now - counter.window_start > window:
            counter = _Counter(count=0, window_start=now)
            self._counters[session_id] = counter
        counter.count += 1
        return counter

    def reset(self, session_id: str) -> None:
        self._counters.pop(session_id, None)

    def reset_all(self) -> None:
        self._counters.clear()

    def peek(self, session_id: str) -> tuple[int, float]:
        """面板用：当前计数和窗口剩余秒数。"""
        counter = self._counters.get(session_id)
        if counter is None:
            return 0, 0.0
        left = max(0.0, self._window_seconds() - (time.time() - counter.window_start))
        return counter.count, left

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _is_at_me(msg: PerceivedMessage, self_id: str) -> bool:
        for mention in msg.mentions or []:
            text = str(mention)
            if self_id and self_id in text:
                return True
            if "@" in text:
                return True
        if msg.quoted and self_id and self_id in (msg.quoted or ""):
            return True
        meta = msg.meta or {}
        if meta.get("at_self"):
            return True
        return False

    # ------------------------------------------------------------ 文案
    def status_line(self, session_id: str) -> str:
        count, left = self.peek(session_id)
        if count <= 0:
            return "没有攒唤醒次数"
        start = self._start_count()
        step = self._step_count()
        if count < start:
            return f"攒了 {count} 次，还差 {start - count} 次开始掷骰（窗口剩 {int(left)} 秒）"
        chance = min(1.0, (count - start + 1) / (step + 1))
        return f"攒了 {count} 次，下次唤醒概率 {chance:.0%}（窗口剩 {int(left)} 秒）"
