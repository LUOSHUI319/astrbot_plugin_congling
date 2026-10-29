"""统一诊断轨迹。

为什么要有这个：以前排查全靠零散的 `night:diag` / `express:diag`，出问题得满地找键。
这里收成一个环形缓冲，保留最近 N 条，谁都能往里写一条。

铁规矩：
- **不记原文**。要记文本只记长度和前 40 字，避免把聊天内容沉淀到诊断里
- **异常只记类型**，不记 traceback（可能带路径和参数）
- 写失败绝不影响业务
"""

from __future__ import annotations

import time
from typing import Any

KEY = "diag:trace"
LIMIT = 60


class Diagnostics:
    def __init__(self, plugin: Any, limit: int = LIMIT) -> None:
        self.plugin = plugin
        self.store = getattr(plugin, "store", None)
        self.limit = limit
        self.enabled = True

    @staticmethod
    def _shrink(value: Any) -> Any:
        if value is None or isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, str):
            return value[:40] + ("…" if len(value) > 40 else "")
        if isinstance(value, (list, tuple, set)):
            return f"<{type(value).__name__} {len(value)}>"
        if isinstance(value, dict):
            return f"<dict {len(value)}>"
        return f"<{type(value).__name__}>"

    async def trace(self, stage: str, **fields: Any) -> None:
        """记一条。永远不抛异常。"""
        if not self.enabled or self.store is None:
            return
        try:
            item: dict[str, Any] = {"at": int(time.time()), "stage": str(stage)[:32]}
            for key, value in fields.items():
                item[str(key)[:24]] = self._shrink(value)
            raw = await self.store.kv_get(KEY, []) or []
            if not isinstance(raw, list):
                raw = []
            raw.append(item)
            await self.store.kv_set(KEY, raw[-self.limit :])
        except Exception:
            pass

    async def tail(self, count: int = 20) -> list[dict[str, Any]]:
        try:
            raw = await self.store.kv_get(KEY, []) or []
            return list(raw)[-count:] if isinstance(raw, list) else []
        except Exception:
            return []
