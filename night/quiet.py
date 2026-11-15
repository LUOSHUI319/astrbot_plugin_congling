"""安静的打破规则。

比正常唤醒（`night/wake.py`）简单得多：那边要掷骰、按睡眠阶段打折、攒够
6 次必醒；安静这里**到数就醒**。因为「安静」本来就只是「别接话」，
不是「睡着了」——用睡觉那套概率去解除它，属于用力过猛。

三条规矩：

1. **只有管理员的消息计数。** 别人刷屏不算，否则群里随便热闹几句就解除了。
2. **群聊要 @ 或引用她才计数。** 管理员在群里聊别的事，不该把她叫醒。
3. 窗口内累计到 N 条立刻解除，不掷骰；窗口过了重新算。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..core.contracts import PerceivedMessage

DEFAULT_WINDOW_MINUTES = 5
DEFAULT_COUNT = 2


@dataclass
class _Counter:
    count: int = 0
    window_start: float = 0.0


class QuietBreaker:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self._counters: dict[str, _Counter] = {}

    # ------------------------------------------------------------ 配置
    def window_minutes(self) -> int:
        try:
            return max(1, min(60, self.config.int("close.break_window_minutes", DEFAULT_WINDOW_MINUTES)))
        except (TypeError, ValueError):
            return DEFAULT_WINDOW_MINUTES

    def need(self) -> int:
        try:
            return max(1, min(10, self.config.int("close.break_count", DEFAULT_COUNT)))
        except (TypeError, ValueError):
            return DEFAULT_COUNT

    def _window_seconds(self) -> float:
        return self.window_minutes() * 60.0

    # ------------------------------------------------------------ 主判定
    def check(
        self, msg: PerceivedMessage, *, is_admin: bool, self_id: str = ""
    ) -> tuple[bool, str]:
        """返回 (是否该解除安静, 说明)。说明会落进日志和诊断。"""
        if not is_admin:
            return False, "只有管理员能叫醒"
        if msg.session_kind == "group" and not self._addressed(msg, self_id):
            return False, "群里没被点名，不计数"

        counter = self._bump(msg.session_id)
        need = self.need()
        if counter.count >= need:
            self.reset(msg.session_id)
            return True, f"{self.window_minutes()} 分钟内第 {counter.count} 条，解除"
        return False, f"还差 {need - counter.count} 条（{self.window_minutes()} 分钟内）"

    def _bump(self, session_id: str) -> _Counter:
        now = time.time()
        window = self._window_seconds()
        counter = self._counters.get(session_id)
        if counter is None or now - counter.window_start > window:
            counter = _Counter(count=0, window_start=now)
            self._counters[session_id] = counter
        counter.count += 1
        return counter

    def reset(self, session_id: str = "") -> None:
        if session_id:
            self._counters.pop(session_id, None)
        else:
            self._counters.clear()

    def status_line(self, session_id: str) -> str:
        counter = self._counters.get(session_id)
        if counter is None or counter.count <= 0:
            return "还没攒"
        left = max(0.0, self._window_seconds() - (time.time() - counter.window_start))
        return f"攒了 {counter.count}/{self.need()} 条（窗口剩 {int(left)} 秒）"

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _addressed(msg: PerceivedMessage, self_id: str) -> bool:
        for mention in msg.mentions or []:
            text = str(mention)
            if self_id and self_id in text:
                return True
            if "@" in text:
                return True
        if msg.quoted and self_id and self_id in (msg.quoted or ""):
            return True
        meta = msg.meta or {}
        return bool(meta.get("at_self"))
