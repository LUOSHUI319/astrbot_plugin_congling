"""跨模块契约。

铁律：
1. 模块之间只通过这里的数据结构通信，不互相 import 实现。
2. 每个契约带 version，外部插件实现同一份契约即可替换本模块。
3. 所有字段给默认值，缺一半也能跑（降级优先）。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

CONTRACT_VERSION = 1


# --------------------------------------------------------------------- 人格
@dataclass
class Persona:
    """人格档案。所有模块都带 persona_id，一套代码可跑多个角色。"""

    persona_id: str = "ling"
    name: str = "绫"
    version: int = CONTRACT_VERSION
    identity: str = ""
    style: dict[str, Any] = field(default_factory=dict)
    taboos: list[str] = field(default_factory=list)
    active_hours: str = "09:00-03:30"
    state: dict[str, Any] = field(default_factory=dict)

    def style_line(self) -> str:
        parts: list[str] = []
        max_chars = self.style.get("max_chars")
        if max_chars:
            parts.append(f"单句不超过 {max_chars} 字")
        habits = self.style.get("habits") or []
        if habits:
            parts.append("口头习惯：" + "、".join(str(item) for item in habits))
        return "；".join(parts)


# --------------------------------------------------------------------- 感知
@dataclass
class Block:
    """消息里的一个可读块。"""

    kind: str = "text"  # text/image/voice/video/file/forward/quote/card/at/face/poke/location/redpacket
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    truncated: bool = False

    def render(self) -> str:
        return self.text or ""


@dataclass
class PerceivedMessage:
    """归一化后的消息，感知→记忆→拟人→UI 都吃这个。"""

    msg_id: str = ""
    session_id: str = ""
    session_kind: str = "group"  # group/private
    sender_id: str = ""
    sender_name: str = ""
    ts: int = field(default_factory=lambda: int(time.time()))
    text: str = ""
    blocks: list[Block] = field(default_factory=list)
    raw_components: list[Any] = field(default_factory=list)
    is_forward_expanded: bool = False
    quoted: str = ""
    mentions: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def prompt_text(self, limit: int = 4000) -> str:
        text = self.text.strip()
        if len(text) > limit:
            return text[:limit] + "…（后文略）"
        return text

    def has(self, kind: str) -> bool:
        return any(block.kind == kind for block in self.blocks)


# --------------------------------------------------------------------- 记忆
@dataclass
class MemoryHit:
    atom_id: int = 0
    text: str = ""
    kind: str = "fact"
    importance: float = 0.5
    score: float = 0.0
    created_at: int = 0
    decay: float = 1.0
    evidence: list[str] = field(default_factory=list)
    subject_id: str = ""
    scope: str = "user"
    session_id: str = ""
    session_kind: str = ""
    session_label: str = ""

    def line(self) -> str:
        stamp = ""
        if self.created_at:
            stamp = time.strftime("%Y-%m-%d", time.localtime(self.created_at))
        mark = " 【重要】" if self.importance >= 0.85 else ""
        prefix = f"({stamp}) " if stamp else ""
        where = f"[{self.session_label}] " if self.session_label else ""
        return f"- {prefix}{where}{self.text}{mark}"


@dataclass
class MemoryDraft:
    """抽取阶段的中间产物，尚未入库。"""

    kind: str = "fact"
    text: str = ""
    subject: str = ""
    keywords: list[str] = field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.8
    time_hint: str = ""
    evidence_msg_ids: list[str] = field(default_factory=list)
    supersedes: int | None = None
    session_id: str = ""
    session_kind: str = ""
    session_label: str = ""


# --------------------------------------------------------------------- 节奏
@dataclass
class RhythmPlan:
    bubbles: list[str] = field(default_factory=list)
    delays: list[float] = field(default_factory=list)
    quote: bool = False
    mention: bool = False
    as_voice: bool = False
    sticker_tag: str | None = None
    typo_fix: str | None = None
    reason: str = ""

    def total_chars(self) -> int:
        return sum(len(item) for item in self.bubbles)


# --------------------------------------------------------------------- 能力
@dataclass
class Capability:
    """一个可被 LLM 或 UI 调用的能力。"""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    handler: Callable[..., Awaitable[Any]] | None = None
    group: str = "life"
    timeout: float = 8.0
    cache_ttl: int = 0
    rate_limit: int = 0  # 每会话每分钟，0 表示继承全局
    auth: str = "all"  # all/admin
    fallback: str = "这个我查不到欸"
    enabled: bool = True
    takes_event: bool = False  # True 时调用会把当前消息事件以 _event 传入


@dataclass
class CapabilityResult:
    ok: bool = False
    text: str = ""
    data: Any = None
    source: str = ""
    elapsed: float = 0.0
    cached: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "data": self.data,
            "source": self.source,
            "elapsed": round(self.elapsed, 3),
            "cached": self.cached,
            "error": self.error,
        }


# --------------------------------------------------------------------- 判断
@dataclass
class ReplyDecision:
    should_reply: bool = False
    score: float = 0.0
    threshold: float = 35.0
    signals: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    in_window: bool = False
    roll: float = 0.0
