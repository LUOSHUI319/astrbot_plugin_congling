"""指令。不依赖 UI 也能把插件用起来。

/绫 状态 | 记忆 <词> | 记住 <内容> | 忘掉 <词> | 空间 [看|发] | 兼容 | 调试 [开|关]
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

HELP = (
    "/绫 状态 —— 看运行状态\n"
    "/绫 记忆 <词> —— 翻记忆\n"
    "/绫 记住 <内容> —— 手动记一条\n"
    "/绫 忘掉 <词> —— 忘掉相关记忆\n"
    "/绫 空间 看 —— 看空间动态\n"
    "/绫 空间 发 <内容> —— 发一条说说\n"
    "/绫 兼容 —— 看和哪些插件共存\n"
    "/绫 调试 开/关 —— 决策日志"
)


class Commands:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    async def handle(self, event: Any, args: list[str]) -> str:
        if not args:
            return HELP
        action = args[0].strip()
        rest = args[1:]

        if action in {"状态", "status"}:
            return await self._status(event)
        if action in {"记忆", "memory"}:
            return await self._recall(event, " ".join(rest).strip())
        if action in {"记住", "remember"}:
            return await self._remember(event, " ".join(rest).strip())
        if action in {"忘掉", "forget"}:
            return await self._forget(event, " ".join(rest).strip())
        if action in {"空间", "qzone"}:
            return await self._qzone(event, rest)
        if action in {"兼容", "compat"}:
            return self._compat()
        if action in {"调试", "debug"}:
            return self._debug(event, rest)
        return HELP

    # ---------------------------------------------------------------- 状态
    async def _status(self, event: Any) -> str:
        health = self.plugin.health()
        persona = health["persona"]
        uptime = health["uptime"] // 60
        modules = "、".join(name for name, ok in health["modules"].items() if ok) or "无"
        lines = [
            f"{persona['name']}（{persona['id']}）· v{health['version']}",
            f"运行 {uptime} 分钟，模块：{modules}",
        ]
        if self.plugin.memory is not None:
            try:
                count = await self.plugin.memory.atoms.count()
                lines.append(f"记忆 {count} 条")
            except Exception:
                pass
        if self.plugin.capabilities is not None:
            lines.append(f"能力 {len(self.plugin.capabilities.caps)} 项")
        yield_text = health["compat"]["yield"]
        yielding = [key for key, value in yield_text.items() if value]
        if yielding:
            lines.append("让位给：" + "、".join(yielding))
        return "；".join(lines)

    # ---------------------------------------------------------------- 记忆
    async def _recall(self, event: Any, keyword: str) -> str:
        if self.plugin.memory is None:
            return "记忆没启用"
        if not keyword:
            return "要找什么"
        try:
            is_group = bool(event.get_group_id())
        except Exception:
            is_group = False
        scope = "group" if is_group else "user"
        subject = str(event.get_group_id() or event.get_sender_id() or "")
        hits = await self.plugin.memory.recall(keyword, scope=scope, subject=subject, k=5)
        if not hits:
            return "没想起来相关的事"
        return "\n".join(hit.line() for hit in hits)

    async def _remember(self, event: Any, content: str) -> str:
        if self.plugin.memory is None:
            return "记忆没启用"
        if not content:
            return "要记什么"
        if not self.plugin.is_admin(event):
            return "这个要管理员才能写"
        try:
            is_group = bool(event.get_group_id())
        except Exception:
            is_group = False
        scope = "group" if is_group else "user"
        subject = str(event.get_group_id() or event.get_sender_id() or "")
        atom_id = await self.plugin.memory.write_text(
            content, scope=scope, subject=subject, importance=0.9
        )
        return f"记住了（#{atom_id}）" if atom_id else "没写进去"

    async def _forget(self, event: Any, keyword: str) -> str:
        if self.plugin.memory is None:
            return "记忆没启用"
        if not keyword:
            return "要忘掉什么"
        if not self.plugin.is_admin(event):
            return "这个要管理员才能删"
        count = await self.plugin.memory.forget(keyword)
        return f"忘掉了 {count} 条" if count else "没找到相关的记忆"

    # ---------------------------------------------------------------- 空间
    async def _qzone(self, event: Any, rest: list[str]) -> str:
        if self.plugin.qqext is None:
            return "QQ 扩展没就绪"
        sub = rest[0] if rest else "看"
        if sub in {"看", "feed"}:
            result = await self.plugin.qqext.qzone.feed()
            return result.get("text") or "没读到"
        if sub in {"发", "post", "publish"}:
            if not self.plugin.is_admin(event):
                return "这个要管理员才能发"
            content = " ".join(rest[1:]).strip()
            result = await self.plugin.qqext.qzone.publish(content)
            return result.get("text") or "没发出去"
        return "用法：/绫 空间 看 或 /绫 空间 发 <内容>"

    # ---------------------------------------------------------------- 其它
    def _compat(self) -> str:
        matrix = self.plugin.probe.matrix()
        present = "、".join(matrix["present"].keys()) or "无"
        yielded = "、".join(key for key, value in matrix["yield"].items() if value) or "无"
        optional = "、".join(key for key, value in matrix["optional"].items() if value) or "无"
        return f"外部插件：{present}；让位：{yielded}；可借用：{optional}"

    def _debug(self, event: Any, rest: list[str]) -> str:
        if not self.plugin.is_admin(event):
            return "这个要管理员"
        if not rest:
            enabled = self.plugin.cfg.bool("debug.decision_log", True)
            return f"决策日志现在是{'开' if enabled else '关'}的"
        value = rest[0] in {"开", "on", "1", "true"}
        self.plugin.cfg.override("debug.decision_log", value)
        return f"决策日志已{'打开' if value else '关闭'}"
