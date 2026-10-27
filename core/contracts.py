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
    level: int = 3
    strength: float = 0.65
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
    # 0.5.0：等级由模型给基础档 + 标签压上下限 + 好感度微调，理由要留痕
    level: int = 3
    level_reason: str = ""
    level_tags: list[str] = field(default_factory=list)
    level_source: str = "llm"  # llm|rule|manual
    subject_key: str = ""  # 识别库里的主体键（通常是 QQ 号），入 tag 表用


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


# --------------------------------------------------------------------- 长夜如梦
# /*congling:night*/
NIGHT_AWAKE = "awake"
NIGHT_ASLEEP = "asleep"
NIGHT_WAKING = "waking"


@dataclass
class NightState:
    """全局作息状态。精力是全局一份，不按会话分。

    mode       awake / asleep / waking
    energy     0~100，疲劳期内消耗
    sleep_at   入睡时间戳，0 表示没睡
    wake_at    计划醒来时间戳（固定点 ± 浮动算出来的）
    reason     入睡原因：talk / energy / latest / manual
    """

    mode: str = NIGHT_AWAKE
    energy: float = 100.0
    sleep_at: int = 0
    wake_at: int = 0
    reason: str = ""
    last_tick: int = 0
    dream_count_today: int = 0
    dream_skip: bool = False
    version: int = 1
    # 这一觉是夜里睡还是午睡：决定状态栏文案（午睡不能报「明天早上醒」）
    sleep_kind: str = ""
    # 进 waking 的时刻，用来做超时兜底（卡在 waking 会让状态机僵住）
    enter_waking_at: int = 0
    # 醒来后还等着爬楼的会话
    pending_crawl: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "energy": round(self.energy, 2),
            "sleep_at": self.sleep_at,
            "wake_at": self.wake_at,
            "reason": self.reason,
            "last_tick": self.last_tick,
            "dream_count_today": self.dream_count_today,
            "dream_skip": self.dream_skip,
            "sleep_kind": self.sleep_kind,
            "enter_waking_at": self.enter_waking_at,
            "pending_crawl": list(self.pending_crawl or []),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> NightState:
        if not data:
            return cls()
        state = cls()
        state.mode = str(data.get("mode") or NIGHT_AWAKE)
        try:
            state.energy = float(data.get("energy", 100.0))
        except (TypeError, ValueError):
            state.energy = 100.0
        state.sleep_at = int(data.get("sleep_at") or 0)
        state.wake_at = int(data.get("wake_at") or 0)
        state.reason = str(data.get("reason") or "")
        state.last_tick = int(data.get("last_tick") or 0)
        state.dream_count_today = int(data.get("dream_count_today") or 0)
        state.dream_skip = bool(data.get("dream_skip"))
        state.sleep_kind = str(data.get("sleep_kind") or "")
        state.enter_waking_at = int(data.get("enter_waking_at") or 0)
        raw_pending = data.get("pending_crawl") or []
        state.pending_crawl = [str(x) for x in raw_pending if str(x).strip()] if isinstance(raw_pending, list) else []
        return state

    def is_asleep(self) -> bool:
        return self.mode in (NIGHT_ASLEEP, NIGHT_WAKING)


@dataclass
class WakeAttempt:
    """一次唤醒掷骰的结果，便于面板展示和日志排查。"""

    allowed: bool = False
    woke: bool = False
    count: int = 0
    needed: int = 0
    chance: float = 0.0
    roll: float = 0.0
    reason: str = ""


def night_key_of(ts: int) -> str:
    """一段梦属于哪一夜。

    中午之前的梦算前一晚：凌晨三点做的梦，人是归到"昨晚"的。
    中午之后算当天，方便白天午睡也不串夜。
    """
    import datetime as _dt

    if ts <= 0:
        return ""
    moment = _dt.datetime.fromtimestamp(ts)
    if moment.hour < 12:
        moment = moment - _dt.timedelta(days=1)
    return moment.strftime("%Y-%m-%d")


@dataclass
class DreamEntry:
    """一段梦。独立于普通记忆，按天衰减。"""

    dream_id: str = ""
    session_id: str = ""
    created_at: int = 0
    level: int = 3
    tags: list[str] = field(default_factory=list)
    text: str = ""
    importance: float = 0.6
    decay_per_day: float = 0.15
    strength: float = 1.0
    expired: bool = False
    mentioned: bool = False
    night_key: str = ""
    pinned: bool = False

    def remaining_days(self) -> int:
        if self.decay_per_day <= 0:
            return 999
        import math

        if self.strength <= 0.15:
            return 0
        return max(0, int(math.log(0.15 / max(self.strength, 0.0001)) / math.log(1 - self.decay_per_day)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "dream_id": self.dream_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "level": self.level,
            "tags": list(self.tags),
            "text": self.text,
            "importance": self.importance,
            "decay_per_day": self.decay_per_day,
            "strength": round(self.strength, 4),
            "expired": self.expired,
            "mentioned": self.mentioned,
            "night_key": self.night_key,
            "pinned": self.pinned,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DreamEntry:
        return cls(
            dream_id=str(data.get("dream_id") or ""),
            session_id=str(data.get("session_id") or ""),
            created_at=int(data.get("created_at") or 0),
            level=int(data.get("level") or 3),
            tags=list(data.get("tags") or []),
            text=str(data.get("text") or ""),
            importance=float(data.get("importance") or 0.6),
            decay_per_day=float(data.get("decay_per_day") or 0.15),
            strength=float(data.get("strength", 1.0)),
            expired=bool(data.get("expired")),
            mentioned=bool(data.get("mentioned")),
            night_key=str(data.get("night_key") or ""),
            pinned=bool(data.get("pinned")),
        )
# /*congling:night:end*/
