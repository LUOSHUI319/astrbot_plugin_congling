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
    # 让人去睡的说法。对 bot 说这些等同于「你该歇了」，所以和「我睡了」同档。
    # 叙述句（「他说睡觉吧」）和问句（「睡觉吧？」）仍被下面的句式过滤挡住。
    "睡觉吧", "去睡觉", "睡吧", "我去睡了", "该睡了",
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
    """默认词 + 配置词合并，而不是配置整表覆盖。

    原来是 `config.list(...) or list(STRONG)`：配置里只要写了非空列表，
    代码里的默认词就整个作废——之前「睡觉去」就是这么无声失效的。
    现在改成合并，想删默认词就在 `close.removed_words` 里写。
    """
    try:
        removed = {
            str(item).strip().lower()
            for item in (config.list("close.removed_words", []) or [])
            if str(item).strip()
        }
    except Exception:
        removed = set()

    def merge(defaults: tuple[str, ...], key: str) -> list[str]:
        try:
            extra = config.list(key, []) or []
        except Exception:
            extra = []
        merged: list[str] = []
        seen: set[str] = set()
        for raw in list(defaults) + [str(item) for item in extra]:
            text = str(raw).strip()
            if not text:
                continue
            lowered = text.lower()
            if lowered in seen or lowered in removed:
                continue
            seen.add(lowered)
            merged.append(text)
        return merged

    return (
        merge(STRONG, "close.strong_words"),
        merge(MEDIUM, "close.medium_words"),
        merge(WEAK, "close.weak_words"),
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


# ---------------------------------------------------------------- 触发权限
# 「谁能让它停下」和「什么话算结束语」是两件事：前者管人，后者管字。
# 群里随便一个路人都能把整只 bot 说睡过去，就是把这两件事混在一起的下场。
TRIGGER_MODES = ("whitelist", "admin", "all")


def trigger_allowed(
    sender_id: str,
    config: Any,
    admins: Any = (),
    session_id: str = "",
) -> tuple[bool, str]:
    """这句话有没有资格触发收线静默。

    返回 (是否允许, 依据)。依据会落进日志与诊断，方便回头查「为什么没停」。

    - whitelist：白名单里的人（管理员始终在内，避免把自己锁死）
    - admin：只认管理员
    - all：谁都能触发，旧行为
    """
    mode = str(config.str("close.trigger_mode", "whitelist") or "whitelist").strip().lower()
    if mode not in TRIGGER_MODES:
        mode = "whitelist"
    if mode == "all":
        return True, "all"

    uid = str(sender_id or "").strip()
    admin_set = {str(item).strip() for item in (admins or []) if str(item).strip()}
    if uid and uid in admin_set:
        return True, "admin"
    if mode == "admin":
        return False, "admin_only"

    allow: set[str] = set()
    try:
        allow = {
            str(item).strip()
            for item in (config.list("close.trigger_whitelist", []) or [])
            if str(item).strip()
        }
    except Exception:
        allow = set()
    if not allow:
        # 名单空着：退回「只有管理员能停」，比「谁都能停」安全得多
        return False, "whitelist_empty"
    if uid and uid in allow:
        return True, "whitelist"
    # 群号也认：群聊时 session_id 就是群号，放行一整群比一个个加人省事
    sid = str(session_id or "")
    if sid and any(item.isdigit() and len(item) >= 5 and item in sid for item in allow):
        return True, "group"
    return False, "not_whitelisted"


def close_minutes(config: Any, strength: str, word: str) -> int:
    """这次收线该静默多久（分钟）。0 表示要睡到起床时间。"""
    if strength == "strong":
        return 0
    if "午" in word or "眯" in word or "盹" in word or "躺" in word:
        return max(1, config.int("close.noon_mute_minutes", 75))
    return max(1, config.int("close.bye_mute_minutes", 30))
