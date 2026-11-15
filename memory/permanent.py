"""永久记忆判定。

**这一版的改动（1.3.0）**

以前 `taboo` 是硬标签，一票定 0 级。加上老数据迁移把 `protect=1` 的全刷成 0 级，
结果 157 条「永久记忆」里 77 条是普通对话记录（「某人说自己忘记高考报名了」）。

现在的判据分四条，任意一条成立即可，但每条都有明确条件：

| 来源 | 条件 |
| --- | --- |
| `taboo` 明确禁令 | 有强禁止词 + 明确对象；语气词不算 |
| `identity` 身份锚点 | 名字、生日、称呼、所在地这类不可错的事实（走 4 级，不占永久档） |
| `promise` 硬承诺 | 有具体时间或内容，不是随口应声（走 +1 提升，不占永久档） |
| `kin` 核心关系人 | 好感度 ≥600 的人（不足三位取排行前三）的关键记忆 |

最后一条是本版新加的：**亲近的人的重要记忆值得永存**。但要限数量——
望山一个人就有 2081 条记忆，全升永久会把注入名额挤爆，所以每人有配额。
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------- 硬判据
# 只有「禁令」够格直接进永久档：它是唯一「一破就出事」的类型。
# identity（名字、生日）走原来的「至少 4 级」；promise 走「+1 提升」——
# 这两类记错可以纠正，性质上没到「绝不能错」。
HARD_KINDS: dict[str, str] = {
    "taboo": "明确禁令",
}

# 强禁止词：只认「动词组合」，不认单独的「别」
# 「别提了」「别走」都是语气，没有明确对象
PROHIBIT_RE = re.compile(r"(不要|不许|不准|不能|禁止|勿|别碰|别动|别发|别用|别问|别让)")
# 显式声明禁区/底线/规矩，也算
EXPLICIT_RE = re.compile(r"(禁区|底线|规矩|原则|雷区)")
# 语气词：出现这些基本可以判定「没有明确对象」
VAGUE_WORDS = (
    "别提了", "别说这个", "别管我", "别问了", "算了", "没事",
    "不说了", "别提", "别急", "别慌", "别怕",
)

# 核心关系人这条判据，只认这几种类型的记忆
KIN_KINDS = {"promise", "relation", "pref"}
KIN_MIN_IMPORTANCE = 0.9      # 核心关系人也要够重要
DEFAULT_KIN_QUOTA = 30        # 每人最多几条（防止一个人撑满永久档）


def has_clear_target(text: str) -> bool:
    """这段话有没有「明确的、可以被违反的对象」。

    「不要动 aaa 文件夹」→ 有（文件夹 + 动）
    「别提了，累死我」  → 没有（语气词）
    """
    body = str(text or "").strip()
    if not body:
        return False
    if any(word in body for word in VAGUE_WORDS):
        return False
    # 显式声明（禁区/底线/规矩）本身就有明确含义，短也算。
    # 「那块是禁区」只有五个字，但比「别提了」明确得多。
    if EXPLICIT_RE.search(body):
        return True
    if len(body) < 6:
        return False
    return bool(PROHIBIT_RE.search(body))


def hard_reason(tags: set[str], text: str) -> tuple[bool, str]:
    """三条硬判据。返回 (是否够格, 理由)。"""
    for tag in tags & set(HARD_KINDS):
        label = HARD_KINDS[tag]
        if tag == "taboo" and not has_clear_target(text):
            return False, f"{label}但缺少明确对象"
        return True, label
    return False, ""


def kin_reason(
    subject_id: str,
    kind: str,
    importance: float,
    core: dict[str, tuple[float, str]],
) -> tuple[bool, str]:
    """核心关系人这条判据。

    `core` 是 {subject_id: (好感度, 显示名)}，由 AffinityService 算出来。
    """
    sid = str(subject_id or "").strip()
    core = core or {}
    if not sid or sid not in core:
        return False, ""
    score, name = core[sid]
    if str(kind or "") not in KIN_KINDS:
        return False, ""
    try:
        imp = float(importance)
    except (TypeError, ValueError):
        return False, ""
    if imp < KIN_MIN_IMPORTANCE:
        return False, ""
    return True, f"核心关系人「{name}」（好感度 {score:.0f}）的{_kind_label(kind)}"


def _kind_label(kind: str) -> str:
    return {
        "promise": "约定", "relation": "关系", "pref": "偏好",
        "fact": "事实", "event": "事件", "emotion": "情绪",
    }.get(str(kind or ""), "记忆")


def judge(
    *,
    tags: list[str] | set[str],
    text: str,
    subject_id: str = "",
    kind: str = "",
    importance: float = 0.0,
    core: dict[str, tuple[float, str]] | None = None,
) -> tuple[bool, str]:
    """一条记忆该不该进永久档。返回 (是否永久, 理由)。"""
    tag_set = set(tags or [])
    ok, why = hard_reason(tag_set, text)
    if ok:
        return True, why
    if core:
        ok, why2 = kin_reason(subject_id, kind, importance, core)
        if ok:
            return True, why2
    return False, why or "不属于永久档"
