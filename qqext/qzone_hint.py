"""空间提示：告诉她「有这些手」和「现在能不能自己动手」。

工具注册只让模型看见函数签名，它不会知道自己**该不该**用。所以这里补一段话，
两个模式两套说法：

- `off`    手在不动：能力都在，但只有对方明确要求时才用
- `intent` 可以有念头：值得说给所有人听的可以发；没什么想说的就不发

`intent` 那段还带上「最近发过什么」——这是自知的最小实现。没有它，她会反复说
同一件事，而这正是「像人」和「像定时任务」的分界线。
"""

from __future__ import annotations

from ..core import clock

import time
from typing import Any

from astrbot.api import logger

MIND_KEY = "qzone:mind"
MARKER = "<!--congling:qzone-->"
RECENT_KEEP = 5
COOLDOWN_SECONDS = 6 * 3600


def behavior_mode(plugin: Any) -> str:
    mode = str(plugin.cfg.str("qq_extra.qzone_behavior_mode", "off") or "off").strip().lower()
    return mode if mode in ("off", "intent") else "off"


def _ago(ts: Any) -> str:
    try:
        stamp = int(ts or 0)
    except (TypeError, ValueError):
        return "刚刚"
    delta = max(0, int(time.time()) - stamp)
    if delta < 3600:
        return f"{max(1, delta // 60)}分钟前"
    if delta < 86400:
        return f"{delta // 3600}小时前"
    return f"{delta // 86400}天前"


async def load_mind(plugin: Any) -> dict[str, Any]:
    try:
        data = await plugin.store.kv_get(MIND_KEY, {}) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def remember_post(
    plugin: Any, text: str, *, fid: str = "", source: str = "manual"
) -> None:
    """发完记一笔：最近说了什么、什么时候说的。

    `recent` 是后来防重复的依据，`last_post_at` 用来做冷却判断。
    写失败只是丢一条记录，不能影响发布本身。
    """
    mind = await load_mind(plugin)
    now = int(time.time())
    recent = list(mind.get("recent") or [])
    recent.append(
        {
            "text": str(text or "")[:240],
            "at": now,
            "fid": str(fid or ""),
            "source": str(source or ""),
        }
    )
    mind["recent"] = recent[-RECENT_KEEP:]
    mind["last_post_at"] = now
    day = clock.fmt("%Y-%m-%d")
    if mind.get("day") != day:
        mind["day"] = day
        mind["today_count"] = 0
    mind["today_count"] = int(mind.get("today_count") or 0) + 1
    try:
        await plugin.store.kv_set(MIND_KEY, mind)
    except Exception as exc:
        logger.debug(f"[congling] 记录空间发布失败: {exc!r}")


def recent_block(mind: dict[str, Any], limit: int = 3) -> str:
    items = [item for item in (mind.get("recent") or []) if isinstance(item, dict)]
    lines: list[str] = []
    for item in items[-limit:]:
        text = str(item.get("text") or "").strip().replace("\n", " ")
        if not text:
            continue
        lines.append(f"- 「{text[:60]}」（{_ago(item.get('at'))}）")
    return "\n".join(lines)


async def build_hint(plugin: Any) -> str:
    """给模型的一段空间说明。取不到该说什么就返回空串，不占上下文。"""
    if not plugin.cfg.bool("qq_extra.qzone_tool_enabled", True):
        return ""
    mode = behavior_mode(plugin)

    if mode == "off":
        # 越短越好：这段话每轮都在，off 模式下它只是「别乱动」的提醒。
        return (
            "【空间】空间相关的能力都在（看动态、看某个人的空间、读评论、发说说、"
            "点赞评论回评），但现在主动行为是关着的：只在对方明确要求时用，"
            "不要自己主动提起，也不要自己发。"
        )

    mind = await load_mind(plugin)
    parts = [
        "【空间】你有空间相关的能力，也可以自己想说点什么。",
        "一个念头如果是说给所有人听的（而不是只跟眼前这个人说），可以发到空间去，用 qzone_post。",
        "分寸：别为了发而发，今天没什么想说的就不发；一条说清楚一件事，别写成小作文；配图你自己挑，挑不到就不配。",
    ]
    last = int(mind.get("last_post_at") or 0)
    if last and (int(time.time()) - last) < COOLDOWN_SECONDS:
        parts.append(f"你刚在{_ago(last)}发过一条，别急着再发。")
    block = recent_block(mind)
    if block:
        parts.append("最近发过这几条，别重复：\n" + block)
    return "\n".join(parts)
