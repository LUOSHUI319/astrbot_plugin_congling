"""QQ 空间工具：把空间能力交给模型自己用。

切三个粗粒度工具（read / post / interact），不是二十个——工具越多模型越容易
选错，上下文也扛不住。内部用 action 分派，边界在各自 handler 里。

**两条硬规矩：**

1. **返回一律自然语言。** fid / cursor / topicId / unikey 这些内部字段不许出现在
   给模型的文本里，它会照着念出来。qzone_ultra 的 README 专门提过这个坑。
2. **写操作只给管理员。** 群里任何人说「发条说说」，模型都不该真的发出去。
   读是公开信息，可以放开；写和互动是公开行为，收在 admin 这层。

工具注册不受 `capability.groups` 白名单影响——空间工具不属于「实用查询」，
它有自己的开关 `qq_extra.qzone_tool_enabled`。
"""

from __future__ import annotations

import time
from typing import Any

from ..core import clock
from ..core.contracts import Capability, CapabilityResult

GROUP = "qzone"

_READ_ACTIONS = ("feed", "timeline", "user", "detail", "comments", "visitors")
_POST_ACTIONS = ("publish", "delete", "preview")
_INTERACT_ACTIONS = ("like", "comment", "reply")


# --------------------------------------------------------------------- 工具函数
def _qzone(plugin: Any) -> Any:
    qqext = getattr(plugin, "qqext", None)
    return getattr(qqext, "qzone", None) if qqext is not None else None


def _bridge_missing() -> CapabilityResult:
    return CapabilityResult(ok=False, text="空间还没准备好（模块没加载或没登录）", error="no_bridge")


def _ago(ts: Any) -> str:
    try:
        stamp = int(ts or 0)
    except (TypeError, ValueError):
        stamp = 0
    if stamp <= 0:
        return "时间不明"
    delta = int(time.time()) - stamp
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{delta // 60} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    if delta < 172800:
        return "昨天"
    return clock.fmt("%m-%d", stamp)


def _who(label: str) -> str:
    """把解析器给的标签换成模型不会误读的说法。

    resolve() 对「我」返回的是「你自己」，这个「你」是说话人视角；
    但模型会把「你自己的空间」读成「bot 自己的空间」。所以换掉。
    """
    text = str(label or "").strip()
    if text in ("你自己", "你的"):
        return "当前说话人"
    return text or ""


def _clip(text: str, limit: int = 60) -> str:
    body = str(text or "").strip().replace("\n", " ")
    if len(body) <= limit:
        return body
    return body[:limit] + "…"


def _line(index: int, item: dict[str, Any]) -> str:
    """一条说说的人话描述。不出现任何内部字段。"""
    nick = str(item.get("nickname") or "").strip() or "某人"
    body = _clip(item.get("content") or "")
    if not body:
        images = len(item.get("images") or [])
        body = f"（一组图，{images} 张）" if images else "（没写什么）"
    tail: list[str] = []
    if item.get("images"):
        tail.append(f"{len(item['images'])} 图")
    if int(item.get("like_count") or 0):
        tail.append(f"{item['like_count']} 赞")
    if int(item.get("comment_count") or 0):
        tail.append(f"{item['comment_count']} 评")
    suffix = f"（{'、'.join(tail)}）" if tail else ""
    return f"{index}. {nick} · {_ago(item.get('created_at'))}：{body}{suffix}"


def _pick(items: list[dict[str, Any]], index: int) -> tuple[dict[str, Any] | None, int]:
    """按序号取一条。1 = 最新，负数从末尾数。返回 (条目, 实际序号)。"""
    if not items:
        return None, 0
    try:
        wanted = int(index)
    except (TypeError, ValueError):
        return None, 0
    if wanted == 0:
        wanted = 1
    if wanted < 0:
        wanted = len(items) + wanted + 1
    if wanted < 1 or wanted > len(items):
        return None, wanted
    return items[wanted - 1], wanted


# --------------------------------------------------------------------- read
async def qzone_read(
    plugin: Any,
    *,
    action: str = "feed",
    target: str = "",
    index: int = 0,
    limit: int = 5,
    _event: Any = None,
) -> CapabilityResult:
    qz = _qzone(plugin)
    if qz is None or not qz.available():
        return _bridge_missing()

    act = str(action or "feed").strip().lower()
    if act not in _READ_ACTIONS:
        return CapabilityResult(ok=False, text=f"不认识的 action：{act}", error="bad_action")

    try:
        want = max(1, min(10, int(limit or 5)))
        pos = int(index or 0)
    except (TypeError, ValueError):
        want, pos = 5, 0

    # 访客不用先读说说列表，单独走
    if act == "visitors":
        data = await qz.visitors(limit=want)
        if not data.get("ok"):
            return CapabilityResult(
                ok=False, text=str(data.get("text") or "读不到"), error="visitors_failed"
            )
        items = list(data.get("items") or [])
        if not items:
            return CapabilityResult(ok=True, text="最近没人来过", data={"count": 0})
        lines = [
            f"{i + 1}. {str(v.get('nickname') or v.get('uin') or '某人')} · {_ago(v.get('at'))}"
            for i, v in enumerate(items)
        ]
        return CapabilityResult(
            ok=True, text="最近访客：\n" + "\n".join(lines), data={"count": len(items)}
        )

    # timeline 走好友动态流，feed/user 走「某人的空间」。
    # 两套接口返回的字段不一样：只有动态流带 unikey / curkey / topic_id（互动要用）。
    fetch = max(want, pos if pos > 0 else 1)
    if act == "timeline":
        result = await qz.timeline(limit=fetch)
    else:
        result = await qz.feed(limit=fetch, target=str(target or ""), event=_event)
    if not result.get("ok"):
        return CapabilityResult(ok=False, text=str(result.get("text") or "没读到"), error="read_failed")

    items = list(result.get("items") or [])
    if not items:
        return CapabilityResult(ok=True, text=str(result.get("text") or "最近没动静"), data={"count": 0})

    if act in ("detail", "comments"):
        item, real = _pick(items, pos or 1)
        if item is None:
            return CapabilityResult(
                ok=False, text=f"那边只有 {len(items)} 条，没有第 {pos or 1} 条", error="out_of_range"
            )
        detail = await qz.detail_of(item)
        if not detail.get("ok"):
            return CapabilityResult(
                ok=False, text=str(detail.get("text") or "详情没读出来"), error="detail_failed"
            )
        comments = list(detail.get("comments") or [])
        nick = str(item.get("nickname") or "").strip() or "这条"
        if act == "comments":
            if not comments:
                return CapabilityResult(ok=True, text=f"{nick} 这条下面还没人评论", data={"count": 0})
            lines = []
            for i, c in enumerate(comments[:20]):
                who = str(c.get("nickname") or c.get("uin") or "某人")
                body = _clip(c.get("content") or "", 40)
                lines.append(f"{i + 1}. {who}：{body}")
            return CapabilityResult(
                ok=True,
                text=f"{nick} 这条下的 {len(comments)} 条评论：\n" + "\n".join(lines),
                data={"count": len(comments)},
            )
        body = _clip(detail.get("content") or item.get("content") or "", 120)
        return CapabilityResult(
            ok=True,
            text=f"{nick} 的第 {real} 条：{body}（{len(comments)} 条评论）",
            data={"index": real, "comments": len(comments)},
        )

    if pos:
        item, real = _pick(items, pos)
        if item is None:
            return CapabilityResult(
                ok=False, text=f"那边只有 {len(items)} 条，没有第 {pos} 条", error="out_of_range"
            )
        return CapabilityResult(ok=True, text=_line(real, item), data={"index": real})

    shown = items[:want]
    who = "好友动态" if act == "timeline" else _who(str((result.get("target") or {}).get("label") or ""))
    head = f"{who}，最新 {len(shown)} 条：" if who else f"最新 {len(shown)} 条："
    body = "\n".join(_line(i + 1, item) for i, item in enumerate(shown))
    return CapabilityResult(ok=True, text=f"{head}\n{body}", data={"count": len(shown)})


# --------------------------------------------------------------------- post
async def qzone_post(
    plugin: Any,
    *,
    action: str = "publish",
    text: str = "",
    index: int = 0,
    _event: Any = None,
) -> CapabilityResult:
    qz = _qzone(plugin)
    if qz is None or not qz.available():
        return _bridge_missing()

    act = str(action or "publish").strip().lower()
    if act not in _POST_ACTIONS:
        return CapabilityResult(ok=False, text=f"不认识的 action：{act}", error="bad_action")
    if act == "preview":
        return CapabilityResult(ok=False, text="preview 还没接，这一版只能 publish / delete", error="not_implemented")

    if act == "delete":
        try:
            pos = int(index or 1)
        except (TypeError, ValueError):
            pos = 1
        if pos < 1:
            return CapabilityResult(ok=False, text="要删第几条？从 1 开始数", error="bad_index")
        # 删除必须落在自己的号上：feed 的默认目标可能是 owner_qq（你），
        # 但删的是 bot 自己发的说说，不能被默认目标带偏。
        result = await qz.own_feed(limit=max(5, pos))
        if not result.get("ok"):
            return CapabilityResult(ok=False, text=str(result.get("text") or "没读到"), error="read_failed")
        items = list(result.get("items") or [])
        item, real = _pick(items, pos)
        if item is None:
            return CapabilityResult(
                ok=False, text=f"那边只有 {len(items)} 条，没有第 {pos} 条", error="out_of_range"
            )
        outcome = await qz.delete_item(item)
        if not outcome.get("ok"):
            return CapabilityResult(
                ok=False, text=str(outcome.get("text") or "没删掉"), error="delete_failed"
            )
        return CapabilityResult(
            ok=True,
            text=f"{outcome.get('text')}（第 {real} 条：{_clip(item.get('content') or '', 20)}）",
            data={"index": real},
        )

    content = str(text or "").strip()
    if not content:
        return CapabilityResult(ok=False, text="要发什么？内容还空着", error="empty")
    if len(content) > 1000:
        return CapabilityResult(
            ok=False, text=f"说说最多 1000 字，现在 {len(content)} 字", error="too_long"
        )

    result = await qz.publish(content)
    if not result.get("ok"):
        return CapabilityResult(
            ok=False, text=str(result.get("text") or "没发出去"), error="publish_failed"
        )
    shots = int(result.get("images") or 0)
    tail = f"，配了 {shots} 张图" if shots else ""
    return CapabilityResult(ok=True, text=f"说说发出去了{tail}", data={"images": shots})


# --------------------------------------------------------------------- interact
async def qzone_interact(
    plugin: Any,
    *,
    action: str = "like",
    target: str = "",
    index: int = 1,
    comment_index: int = 1,
    content: str = "",
    _event: Any = None,
) -> CapabilityResult:
    qz = _qzone(plugin)
    if qz is None or not qz.available():
        return _bridge_missing()

    act = str(action or "like").strip().lower()
    if act not in _INTERACT_ACTIONS:
        return CapabilityResult(ok=False, text=f"不认识的 action：{act}", error="bad_action")

    try:
        pos = int(index or 1)
        cpos = int(comment_index or 1)
    except (TypeError, ValueError):
        pos, cpos = 1, 1

    # 互动只能对「好友动态流」里的条目：点赞要的 unikey / curkey、评论要的 topic_id，
    # 只有这一套接口给；「看某人空间」那套（msglist）一个都不返回。
    if str(target or "").strip():
        return CapabilityResult(
            ok=False,
            text="互动只能对好友动态里的条目，不能指定某人的空间——那套接口不给点赞/评论要的参数",
            error="unsupported_target",
        )
    fetch = max(5, pos if pos > 0 else 5)
    result = await qz.timeline(limit=fetch)
    if not result.get("ok"):
        return CapabilityResult(ok=False, text=str(result.get("text") or "没读到"), error="read_failed")
    items = list(result.get("items") or [])
    item, real = _pick(items, pos)
    if item is None:
        return CapabilityResult(
            ok=False, text=f"那边只有 {len(items)} 条，没有第 {pos} 条", error="out_of_range"
        )
    nick = str(item.get("nickname") or "").strip() or "那条"

    if act == "like":
        outcome = await qz.like_item(item)
        if not outcome.get("ok"):
            return CapabilityResult(
                ok=False, text=str(outcome.get("text") or "没成功"), error="interact_failed"
            )
        return CapabilityResult(
            ok=True, text=f"{outcome.get('text')}（{nick} 的第 {real} 条）", data={"index": real}
        )

    if act == "comment":
        if not str(content or "").strip():
            return CapabilityResult(ok=False, text="要评什么？内容还空着", error="empty")
        outcome = await qz.comment_item(item, str(content))
        if not outcome.get("ok"):
            return CapabilityResult(
                ok=False, text=str(outcome.get("text") or "没成功"), error="interact_failed"
            )
        return CapabilityResult(
            ok=True, text=f"{outcome.get('text')}（{nick} 的第 {real} 条）", data={"index": real}
        )

    # reply：先读评论，再挑一条回
    if not str(content or "").strip():
        return CapabilityResult(ok=False, text="要回什么？内容还空着", error="empty")
    detail = await qz.detail_of(item)
    if not detail.get("ok"):
        return CapabilityResult(
            ok=False, text=str(detail.get("text") or "评论没读出来"), error="detail_failed"
        )
    comments = list(detail.get("comments") or [])
    if not comments:
        return CapabilityResult(ok=False, text=f"{nick} 这条下面还没人评论", error="no_comments")
    comment, cidx = _pick(comments, cpos)
    if comment is None:
        return CapabilityResult(
            ok=False,
            text=f"那条说说的评论只有 {len(comments)} 条，没有第 {cpos} 条",
            error="out_of_range",
        )
    outcome = await qz.reply_comment_item(item, comment, str(content))
    if not outcome.get("ok"):
        return CapabilityResult(
            ok=False, text=str(outcome.get("text") or "没成功"), error="interact_failed"
        )
    who = str(comment.get("nickname") or comment.get("uin") or "某人")
    return CapabilityResult(
        ok=True,
        text=f"{outcome.get('text')}（回了 {who} 的第 {cidx} 条评论）",
        data={"index": real, "comment_index": cidx},
    )


# --------------------------------------------------------------------- 注册
def build_caps(plugin: Any) -> list[Capability]:
    if not plugin.cfg.bool("qq_extra.qzone_tool_enabled", True):
        return []

    def wrap(fn: Any) -> Any:
        async def call(**kwargs: Any) -> CapabilityResult:
            return await fn(plugin, **kwargs)

        return call

    return [
        Capability(
            name="qzone.read",
            description=(
                "看 QQ 空间。action=feed 看默认对象的空间（不填 target 就是配置里指定的人）；"
                "action=timeline 看好友动态流（所有人最近发的）；action=user 看某个人的空间"
                "（target 填 QQ 号、昵称或「我」）；action=detail / comments 读某一条的全文或评论；"
                "action=visitors 看最近访客。结果都带序号。"
            ),
            group=GROUP,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_READ_ACTIONS),
                        "description": (
                            "feed 默认对象的空间 / timeline 好友动态流 / user 某个人的空间 / "
                            "detail 某一条的全文 / comments 某一条下的评论 / visitors 最近访客"
                        ),
                    },
                    "target": {
                        "type": "string",
                        "description": "看谁的空间：QQ 号、昵称，或者「我」。不填就是默认对象",
                    },
                    "index": {
                        "type": "integer",
                        "description": "只看第几条，1 表示最新。不填或 0 表示列出来",
                    },
                    "limit": {"type": "integer", "description": "列几条，默认 5，最多 10"},
                },
                "required": ["action"],
            },
            handler=wrap(qzone_read),
            timeout=25.0,
            takes_event=True,
            auth="all",
            fallback="空间没读出来",
        ),
        Capability(
            name="qzone.post",
            description=(
                "发一条 QQ 空间说说，或删掉自己刚发的。action=publish 时 text 是正文（最多 1000 字）；"
                "action=delete 时 index 指第几条（1 表示最新），只能删 6 小时内的。"
                "这是公开动作，发出去所有人都看得见，只有用户明确要求时才用。"
                "配图由她自己决定，不用传图片参数。"
            ),
            group=GROUP,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["publish", "delete"],
                        "description": "publish 发一条 / delete 删掉自己 6 小时内发的一条",
                    },
                    "text": {"type": "string", "description": "说说正文，最多 1000 字"},
                    "index": {
                        "type": "integer",
                        "description": "delete 时用：删第几条，1 表示最新",
                    },
                },
                "required": ["action"],
            },
            handler=wrap(qzone_post),
            timeout=40.0,
            takes_event=True,
            auth="admin",
            fallback="没发出去",
        ),
        Capability(
            name="qzone.interact",
            description=(
                "在 QQ 空间里互动。**只能对好友动态流里的条目**（那两个接口不给点赞评论要的参数）："
                "先 qzone_read(action=timeline) 拿到序号，再 action=like 点赞、"
                "action=comment 评论这条说说（content 是评论内容），或 action=reply 回复某条评论"
                "（comment_index 指第几条评论）。index 填第几条，1 表示最新。"
                "这是公开行为，按用户明确要求做，别自己找话说。"
            ),
            group=GROUP,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_INTERACT_ACTIONS),
                        "description": "like 点赞 / comment 评论这条说说 / reply 回复底下某条评论",
                    },
                    "index": {"type": "integer", "description": "好友动态里第几条，1 表示最新"},
                    "comment_index": {
                        "type": "integer",
                        "description": "reply 时用：回复第几条评论，1 表示第一条",
                    },
                    "content": {"type": "string", "description": "评论或回复的内容，最多 200 字"},
                },
                "required": ["action", "index"],
            },
            handler=wrap(qzone_interact),
            timeout=40.0,
            takes_event=True,
            auth="admin",
            fallback="没成功",
        ),
    ]
