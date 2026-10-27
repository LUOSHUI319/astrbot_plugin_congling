"""延时回复（0.7.0 · 第 10 条）。

技法来自 reply_delay：把 event.send 换成自己的包装函数，延迟一会儿再发，
期间用户又说话了就立刻放行（"看到你在敲，就不端着了"）。

**但这个插件里已经有一套发送接管了**：core/rhythm.py 的 RhythmController
本来就把 event.send 换成逐条发送，还带 8 秒 watchdog 兜底。
在外面再套一层，两个 watchdog 会互相以为对方没发，兜底路径可能重复发一遍。
所以这里只提供两种互斥模式：

    rhythm      默认。不 patch，只给 RhythmController 一个「额外多等几秒」，
                由它并进第一条气泡的延时里。
    standalone  rhythm 因为外部插件让位（probe.should_yield("rhythm")）时才启用，
                自己 patch event.send。
    off         配置关掉，或平台不在白名单。

判定统一走 mode()，启动日志和面板都读它，免得用户以为"调了没反应"。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from astrbot.api import logger

PATCH_FLAG = "congling_delay_patched"

MODE_OFF = "off"
MODE_RHYTHM = "rhythm"
MODE_STANDALONE = "standalone"


class ReplyDelayer:
    def __init__(self, config: Any, probe: Any) -> None:
        self.config = config
        self.probe = probe
        self._announced: str | None = None

    # ------------------------------------------------------------ 模式
    def mode(self) -> str:
        if not self.config.bool("humanize.reply_delay.enabled", False):
            return MODE_OFF
        try:
            if self.probe is not None and self.probe.should_yield("rhythm"):
                return MODE_STANDALONE
        except Exception:
            pass
        return MODE_RHYTHM

    def describe(self) -> str:
        mode = self.mode()
        if mode != self._announced:
            self._announced = mode
            logger.info(
                "[congling] 延时回复："
                + {
                    MODE_OFF: "关闭",
                    MODE_RHYTHM: f"接在节奏控制里（{self._lo()}-{self._hi()} 秒额外等待）",
                    MODE_STANDALONE: f"独立接管发送（{self._lo()}-{self._hi()} 秒）",
                }[mode]
            )
        return mode

    # ------------------------------------------------------------ 数值
    def _lo(self) -> float:
        value = self.config.float("humanize.reply_delay.min_seconds", 2.0)
        return max(0.0, min(30.0, value))

    def _hi(self) -> float:
        value = self.config.float("humanize.reply_delay.max_seconds", 8.0)
        return max(self._lo(), min(45.0, value))

    def _night_scale(self) -> float:
        """深夜少拖：人困了回得慢，但不能慢到像掉线。"""
        try:
            import time

            hour = time.localtime().tm_hour
        except Exception:
            return 1.0
        if 1 <= hour <= 5:
            return float(self.config.float("humanize.reply_delay.night_scale", 0.7))
        return 1.0

    def _session_allowed(self, session_kind: str) -> bool:
        if session_kind == "private":
            return self.config.bool("humanize.reply_delay.apply_private", True)
        return self.config.bool("humanize.reply_delay.apply_group", False)

    def extra_hold(self, session_kind: str = "private") -> float:
        """这次要多等几秒（给 RhythmController 用，rhythm 模式下唯一入口）。"""
        if self.mode() == MODE_OFF:
            return 0.0
        if not self._session_allowed(session_kind):
            return 0.0
        hold = random.uniform(self._lo(), self._hi()) * self._night_scale()
        # 上限保护：思考延时最长 4.5 秒，两边加起来别超过 25 秒
        return round(max(0.0, min(20.0, hold)), 2)

    def should_hold(self, session_kind: str = "private") -> bool:
        """要不要抽一次延时。概率可调，默认 100%。"""
        if self.mode() == MODE_OFF or not self._session_allowed(session_kind):
            return False
        try:
            chance = self.config.float("humanize.reply_delay.probability", 1.0)
        except Exception:
            chance = 1.0
        return random.random() < max(0.0, min(1.0, chance))

    # ------------------------------------------------------------ 独立接管
    def patch(self, event: Any, *, session_kind: str = "private") -> bool:
        """standalone 模式才真正干活：把 event.send 换成"先睡一会儿再发"。"""
        if self.mode() != MODE_STANDALONE:
            return False
        if not self.should_hold(session_kind):
            return False
        if getattr(event, "_congling_delay_patched", False):
            return False
        original_send = getattr(event, "send", None)
        if not callable(original_send):
            return False
        try:
            delay = self.extra_hold(session_kind)
            if delay <= 0:
                return False
        except Exception:
            return False

        state = {"done": False}

        async def _sleep(*_args, **_kwargs) -> None:
            await asyncio.sleep(delay)

        async def wrapped_send(message: Any = None) -> None:
            if state["done"]:
                await original_send(message)
                return
            state["done"] = True
            try:
                await asyncio.sleep(delay)
                await original_send(message)
            except asyncio.CancelledError:
                # 用户在等的时候又说话了 → 框架取消，立刻把消息放出去
                try:
                    await original_send(message)
                except Exception:
                    pass
                raise
            except Exception as exc:
                logger.debug(f"[congling] 延时发送失败，直接发: {exc!r}")
                try:
                    await original_send(message)
                except Exception:
                    pass

        try:
            event.send = wrapped_send  # type: ignore[assignment]
            setattr(event, "_congling_delay_patched", True)
            try:
                event.get_extra(PATCH_FLAG, True)
            except Exception:
                pass
            return True
        except Exception as exc:
            logger.debug(f"[congling] 延时接管失败: {exc!r}")
            return False
