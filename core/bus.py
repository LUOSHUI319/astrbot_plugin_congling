"""进程内事件总线。

特点：
- 同步注册、异步派发；
- 单个订阅者抛错不影响其他订阅者（插件里最怕一个模块炸掉整条链）；
- 支持 once 与取消订阅。
"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger

Handler = Callable[..., Awaitable[Any] | Any]


class EventBus:
    # 事件名常量，避免拼错
    ON_PERCEIVED = "on_perceived"
    ON_MEMORY_WRITTEN = "on_memory_written"
    ON_REPLY_PLANNED = "on_reply_planned"
    ON_REPLY_SENT = "on_reply_sent"
    ON_REFLECT = "on_reflect"
    ON_ERROR = "on_error"

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def subscribe(self, event: str, handler: Handler) -> Handler:
        self._handlers[event].append(handler)
        return handler

    def once(self, event: str, handler: Handler) -> Handler:
        async def wrapper(payload: Any) -> None:
            self.unsubscribe(event, wrapper)
            await _maybe_await(handler(payload))

        self._handlers[event].append(wrapper)
        return wrapper

    def unsubscribe(self, event: str, handler: Handler) -> None:
        handlers = self._handlers.get(event)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            pass

    def clear(self, event: str | None = None) -> None:
        if event is None:
            self._handlers.clear()
        else:
            self._handlers.pop(event, None)

    def subscriber_count(self, event: str) -> int:
        return len(self._handlers.get(event, []))

    async def emit(self, event: str, payload: Any = None) -> None:
        handlers = list(self._handlers.get(event, ()))
        for handler in handlers:
            try:
                result = handler(payload)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[congling] 事件 {event} 的订阅者出错: {exc!r}")

    def emit_soon(self, event: str, payload: Any = None) -> None:
        """不阻塞当前链路地派发（用于记忆回写这类后台工作）。"""
        try:
            asyncio.get_running_loop().create_task(self.emit(event, payload))
        except RuntimeError:
            logger.debug(f"[congling] 无事件循环，跳过事件 {event}")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
