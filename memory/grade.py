"""记忆等级判定。

一把尺子是标签，一把尺子是关系。两把尺子都不能单独用：
- 只看标签，模型一句话说"这很重要"就能把寒暄顶成五级；
- 只看好感度，交心的人随便一句话都成了关键记忆，等级失去区分度。

所以流程是：模型给个基础档 → 标签压上下限 → 好感度微调 → 硬标签直接定档。
"""

from __future__ import annotations

import re
from typing import Any

from ..core.contracts import MemoryDraft

# 等级 → 初始强度。0 级不可忘，强度直接顶满
STRENGTH = {0: 1.0, 1: 0.35, 2: 0.5, 3: 0.65, 4: 0.82, 5: 0.95}

# 等级 → 人话，给 UI 和日志用
LEVEL_NAMES = {0: "不可忘", 1: "随口", 2: "细节", 3: "平常事实", 4: "在意的事", 5: "关键"}

# 标签白名单：模型给了不认识的标签一律丢掉，避免污染判定
KNOWN_TAGS = {
    "identity",
    "promise",
    "user_stress",
    "taboo",
    "emotion_high",
    "routine",
    "transient",
    "preference",
    "milestone",
}

TAG_LABELS = {
    "identity": "身份信息",
    "promise": "约定承诺",
    "user_stress": "用户强调",
    "taboo": "禁区底线",
    "emotion_high": "情绪浓度高",
    "routine": "日常寒暄",
    "transient": "一次性时效",
    "preference": "明确喜好",
    "milestone": "关系里程碑",
}

# 标签 → 上下限 / 加成
FLOOR = {"identity": 4, "preference": 4}
CAP = {"routine": 2, "transient": 1}
BOOST = {"promise": 1, "user_stress": 1, "emotion_high": 1}
HARD = {"taboo": 0, "milestone": 5}

# 规则兜底：模型不可用、或返回里没带标签时用关键词猜
RULES: list[tuple[str, tuple[str, ...]]] = [
    ("taboo", ("不许", "别碰", "不能提", "别提", "禁区", "底线")),
    ("milestone", ("第一次", "纪念日", "一周年", "刚认识你那天")),
    ("identity", ("我叫", "我的名字", "我是做", "我住在", "我生日", "我今年")),
    ("preference", ("我最喜欢", "我最讨厌", "受不了", "我超爱", "我根本不吃")),
    ("promise", ("约定", "答应", "说好", "欠你", "下次一起", "约好")),
    ("user_stress", ("记住", "别忘了", "很重要", "一定要", "千万")),
    ("emotion_high", ("崩溃", "哭", "难受死了", "气死", "开心死", "睡不着")),
    ("transient", ("今天", "刚才", "现在", "路上", "等下", "马上到", "这周")),
    ("routine", ("早安", "晚安", "在吗", "吃了吗", "哈哈", "在干嘛", "好的", "嗯嗯")),
]


def level_name(level: int) -> str:
    return LEVEL_NAMES.get(int(level), "平常事实")


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class LevelJudge:
    """把「这条记忆多重要」翻译成一个 0~5 的等级。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg if plugin is not None else None

    # ------------------------------------------------------------ 对外
    def judge_one(
        self,
        draft: MemoryDraft,
        *,
        stage: int | None = None,
        tag_weights: dict[str, Any] | None = None,
    ) -> tuple[int, str, list[str]]:
        """返回 (等级, 理由, 标签)。"""
        tags = [tag for tag in (draft.level_tags or []) if tag in KNOWN_TAGS]
        source = "llm"
        if not tags:
            tags = self.rule_tags(draft.text)
            source = "rule" if tags else "llm"
        base = draft.level if 1 <= int(draft.level or 0) <= 5 else self.guess_base(draft, tags)

        level, why = self.resolve(base, tags, stage, tag_weights)

        # 理由优先用模型给的；模型没给就自己拼一句
        reason = (draft.level_reason or "").strip()
        if not reason:
            reason = why
        elif why not in reason:
            reason = f"{reason}；{why}"
        draft.level_source = source
        return level, reason[:200], tags

    def judge_all(
        self,
        drafts: list[MemoryDraft],
        *,
        stage: int | None = None,
    ) -> list[MemoryDraft]:
        if not self.enabled():
            return drafts
        weights = None
        if self.config is not None:
            raw = self.config.get("memory.levels.tag_weights", None)
            if isinstance(raw, dict):
                weights = raw
        for draft in drafts:
            try:
                level, reason, tags = self.judge_one(draft, stage=stage, tag_weights=weights)
            except Exception:
                continue
            draft.level = level
            draft.level_reason = reason
            draft.level_tags = tags
        return drafts

    def enabled(self) -> bool:
        if self.config is None:
            return True
        try:
            return bool(self.config.bool("memory.levels.enabled", True))
        except Exception:
            return True

    # ------------------------------------------------------------ 核心计算
    def resolve(
        self,
        base: int,
        tags: list[str],
        stage: int | None,
        tag_weights: dict[str, Any] | None = None,
    ) -> tuple[int, str]:
        tag_set = set(tags)

        # 硬标签：不给好感度留余地
        for tag, fixed in HARD.items():
            if tag in tag_set:
                return fixed, f"{TAG_LABELS[tag]}直接定档 {fixed} 级"

        floor = 1
        cap = 5
        level = clamp(int(base), 1, 5)
        notes: list[str] = []

        for tag in tag_set:
            if tag in FLOOR:
                floor = max(floor, FLOOR[tag])
                notes.append(f"{TAG_LABELS[tag]}至少 {FLOOR[tag]} 级")
            if tag in CAP:
                cap = min(cap, CAP[tag])
                notes.append(f"{TAG_LABELS[tag]}封顶 {CAP[tag]} 级")
            if tag in BOOST:
                level += BOOST[tag]
                notes.append(f"{TAG_LABELS[tag]} +1")

        level = clamp(level, floor, cap)

        if stage is not None:
            adj = int(round((int(stage) - 2) * 0.6))
            if adj:
                level = clamp(level + adj, 1, 5)
                notes.append(f"好感度 {adj:+d}")
            level = clamp(level, floor, cap)

        why = "、".join(notes) if notes else f"按内容判定 {level} 级"
        return level, why

    def guess_base(self, draft: MemoryDraft, tags: list[str]) -> int:
        """模型没给等级时，用重要度和标签猜一个基础档。"""
        try:
            importance = float(draft.importance or 0.5)
        except (TypeError, ValueError):
            importance = 0.5
        if importance >= 0.85:
            base = 5
        elif importance >= 0.65:
            base = 4
        elif importance >= 0.45:
            base = 3
        elif importance >= 0.3:
            base = 2
        else:
            base = 1
        if "identity" in tags or "preference" in tags:
            base = max(base, 4)
        if "promise" in tags:
            base = max(base, 4)
        if "routine" in tags:
            base = min(base, 2)
        return base

    # ------------------------------------------------------------ 规则兜底
    @staticmethod
    def rule_tags(text: str) -> list[str]:
        body = str(text or "")
        if not body:
            return []
        hits: list[str] = []
        for tag, words in RULES:
            if any(word in body for word in words):
                hits.append(tag)
        # 一句话又像寒暄又像约定时，约定压过寒暄（别把重要的话当日常忘掉）
        if "routine" in hits and any(tag in hits for tag in ("promise", "taboo", "identity", "milestone")):
            hits.remove("routine")
        return hits

    @staticmethod
    def init_strength(level: int) -> float:
        return STRENGTH.get(int(level), 0.65)


def next_review_at(level: int, now: int, factor: float = 1.0) -> int:
    """下次该复习（重算衰减）的时间。1 级最快，5 级最慢。"""
    days = {0: 0, 1: 1, 2: 3, 3: 7, 4: 21, 5: 60}.get(int(level), 7)
    if days <= 0:
        return 0
    return int(now + days * 86400 * max(0.2, factor))


def extract_tag_hint(text: str) -> str:
    """给抽取提示词用的补充说明，别塞太多字。"""
    return re.sub(r"\s+", " ", text or "")[:80]
