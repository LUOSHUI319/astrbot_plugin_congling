"""结束语识别。

只靠关键词会误判——「他昨天跟我说晚安了」不是要睡，是在叙述。
所以先过一遍句式，句式过了才认。

另外还负责「对方是不是在收线」这条判断，和 wake 是相反方向的同一件事。
"""

from __future__ import annotations

import re
from typing import Any

STRONG = (
    "晚安", "我睡了", "睡了", "去睡了", "先睡一步", "我下了", "下线了",
    "关灯了", "撑不住了", "困死了", "睡觉去", "睡啦", "good night", "night night",
)

MEDIUM = (
    "先这样", "回头聊", "有空再聊", "去忙了", "忙去了", "拜拜", "bye",
    "886", "午安", "午休了", "眯一会儿", "打个盹", "躺会儿", "先撤",
)

WEAK = ("嗯", "好", "行", "收到", "ok", "知道了", "就这样", "没别的事了")

# 第一人称线索
_FIRST_PERSON = ("我", "咱", "俺", "在下")

# 第三人称（出现这些就不算自己在收线）
_THIRD_PERSON = ("他说", "她说", "他说过", "她说过", "跟我说", "跟你说了", "他昨天", "她昨天")

# 疑问尾（问号结尾或这些词，视为不是收线）
_QUESTION_TAIL = ("吗", "？", "?", "呢", "什么时候", "几点了", "在哪")

# 命中词后面紧跟这些字，多半是在说某个名词（晚安曲、拜拜歌）
_NOUN_AFTER = ("曲", "歌", "片", "剧", "书", "会", "餐", "班", "店", "场", "服", "舞")


def _strength_words(config: Any) -> tuple[list[str], list[str], list[str]]:
    strong = config.list("close.strong_words", []) or list(STRONG)
    medium = config.list("close.medium_words", []) or list(MEDIUM)
    weak = config.list("close.weak_words", []) or list(WEAK)
    return (
        [str(item) for item in strong if str(item).strip()],
        [str(item) for item in medium if str(item).strip()],
        [str(item) for item in weak if str(item).strip()],
    )


def match_strength(text: str, config: Any) -> tuple[str, str]:
    """返回 (strength, 命中的词)。strength: strong / medium / weak / '' """
    body = (text or "").strip()
    if not body:
        return "", ""
    lowered = body.lower()
    strong, medium, weak = _strength_words(config)
    for word in strong:
        if word.lower() in lowered:
            return "strong", word
    for word in medium:
        if word.lower() in lowered:
            return "medium", word
    for word in weak:
        if word.lower() in lowered:
            return "weak", word
    return "", ""


def _looks_like_narration(text: str) -> bool:
    return any(marker in text for marker in _THIRD_PERSON)


def _looks_like_question(text: str) -> bool:
    body = (text or "").strip()
    if not body:
        return False
    if body.endswith(("?", "？", "吗", "呢")):
        return True
    return any(body.endswith(tail) for tail in _QUESTION_TAIL)


def _is_short_and_bare(text: str, word: str) -> bool:
    """整句几乎只有这个词 + 不超过 6 个字的修饰。"""
    body = (text or "").strip()
    core = body.replace(word, "", 1)
    core = re.sub(r"[\s，,。.!！~～、]+", "", core)
    return len(core) <= 6


def _is_noun_usage(text: str, word: str) -> bool:
    """「晚安曲」「拜拜歌」这种是在说名词，不是要睡。"""
    index = text.find(word)
    if index < 0:
        return False
    after = text[index + len(word): index + len(word) + 1]
    if not after:
        return False
    return after in _NOUN_AFTER


def is_closing(text: str, config: Any) -> tuple[bool, str, str]:
    """判断这句话是不是在收线。

    返回 (是否收线, 强度, 命中的词)。
    """
    body = (text or "").strip()
    if not body:
        return False, "", ""

    strength, word = match_strength(body, config)
    if not strength:
        return False, "", ""

    # 1. 叙述别人说的话，不算
    if _looks_like_narration(body):
        return False, "", ""

    # 2. 疑问结尾，不算（「你睡了吗」是在问，不是自己要睡）
    if _looks_like_question(body):
        return False, "", ""

    # 2.5 紧跟名词后缀（晚安曲、拜拜歌），不算
    if _is_noun_usage(body, word):
        return False, "", ""

    # 3. 弱档只在「整句就是这个词」时才算，避免把「好」这种语气词当收线
    if strength == "weak":
        if _is_short_and_bare(body, word):
            return True, strength, word
        return False, "", ""

    # 4. 强/中档：整句很短，或者带第一人称，或者就在句末
    if _is_short_and_bare(body, word):
        return True, strength, word
    if any(marker in body for marker in _FIRST_PERSON):
        return True, strength, word
    if body.rstrip(" \t，,。.!！~～").endswith(word):
        return True, strength, word

    # 5. 兜底：句子不长且只有这一个结束信号，也算
    if len(body) <= 12:
        return True, strength, word

    return False, "", ""


def close_minutes(config: Any, strength: str, word: str) -> int:
    """这次收线该静默多久（分钟）。0 表示要睡到起床时间。"""
    if strength == "strong":
        return 0
    if "午" in word or "眯" in word or "盹" in word or "躺" in word:
        return max(1, config.int("close.noon_mute_minutes", 75))
    return max(1, config.int("close.bye_mute_minutes", 30))
