"""把 Capability 包装成框架认识的 LLM 工具。

照 livingmemory 的 MemorySearchTool 写法：pydantic dataclass 子类 + call 方法。
call 只返回字符串（框架的 ToolExecResult 就接受 str），失败也返回人话。
"""

from __future__ import annotations

from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext


@dataclass
class CapabilityTool(FunctionTool[AstrAgentContext]):
    __pydantic_config__ = {"arbitrary_types_allowed": True}

    registry: Any = None
    capability_name: str = ""
    name: str = "congling_tool"
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def _resolve_event(context: Any) -> Any:
        """向下钻进事件对象。

        工具 call 拿到的 context 是 ContextWrapper[AstrAgentContext]，
        真正的 AstrMessageEvent 在 context.context.event；不同框架版本包裹层数
        不一定一样，所以不写死层级，直接钻到带 get_sender_id 的那一层。
        """
        node = context
        for _ in range(4):
            if node is None:
                break
            if callable(getattr(node, "get_sender_id", None)):
                return node
            nxt = getattr(node, "event", None)
            if nxt is None:
                nxt = getattr(node, "context", None)
            if nxt is None or nxt is node:
                break
            node = nxt
        return node if node is not None else context


    @staticmethod
    def _session_id(event: Any) -> str:
        """群聊仍用群号（保持原行为），其余会话用 unified_msg_origin。

        unified_msg_origin 是 property，格式 platform:message_type:session_id；
        拿不到时退回发送者 ID，再拿不到就是空串（限流会退化成一个共享桶）。
        """
        group_getter = getattr(event, "get_group_id", None)
        if callable(group_getter):
            try:
                group_id = str(group_getter() or "")
            except Exception:
                group_id = ""
            if group_id:
                return group_id

        umo = getattr(event, "unified_msg_origin", None)
        if callable(umo):
            try:
                umo = umo()
            except Exception:
                umo = None
        if isinstance(umo, str) and umo:
            return umo

        sender_getter = getattr(event, "get_sender_id", None)
        if callable(sender_getter):
            try:
                sender = str(sender_getter() or "")
            except Exception:
                sender = ""
            if sender:
                return sender
        return ""

    async def call(self, context: Any, **kwargs: Any) -> str:
        if self.registry is None:
            return "这个功能暂时不可用"

        event = self._resolve_event(context)
        session_id = self._session_id(event)

        admin = False
        try:
            plugin = getattr(self.registry, "plugin", None)
            if plugin is not None and hasattr(plugin, "is_admin"):
                admin = bool(plugin.is_admin(event))
        except Exception:
            admin = False

        result = await self.registry.invoke(
            self.capability_name,
            session_id=session_id,
            admin=admin,
            event=event,
            **kwargs,
        )
        return result.text or "没查到"
