"""消息组件 → 人话。

这是整个感知层的地基：所有组件都先转成可读文本，再决定要不要增强（图片描述、
转发展开、卡片解析）。这样即使某个增强不可用，也至少不会丢信息。
"""

from __future__ import annotations

from typing import Any

from ..core.contracts import Block


def _get(comp: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = getattr(comp, name, None)
        if value not in (None, ""):
            return value
    return default


def component_to_block(comp: Any) -> Block | None:
    """单个组件 → Block。未知组件返回 None。"""
    type_name = comp.__class__.__name__

    if type_name == "Plain":
        text = str(_get(comp, "text", default="") or "")
        return Block(kind="text", text=text) if text.strip() else None

    if type_name == "At":
        name = str(_get(comp, "name", default="") or "")
        qq = str(_get(comp, "qq", default="") or "")
        label = name or qq
        return Block(kind="at", text=f"@{label}" if label else "@某人", meta={"qq": qq, "name": name})

    if type_name == "AtAll":
        return Block(kind="at", text="@全体成员")

    if type_name == "Face":
        face_id = str(_get(comp, "id", default="") or "")
        return Block(kind="face", text="[表情]", meta={"id": face_id})

    if type_name == "Image":
        url = str(_get(comp, "url", "file", "path", default="") or "")
        return Block(kind="image", text="[图片]", meta={"url": url})

    if type_name == "Record":
        url = str(_get(comp, "url", "file", "path", default="") or "")
        return Block(kind="voice", text="[语音]", meta={"url": url})

    if type_name == "Video":
        url = str(_get(comp, "url", "file", default="") or "")
        return Block(kind="video", text="[视频]", meta={"url": url})

    if type_name == "File":
        name = str(_get(comp, "name", "file", default="") or "")
        return Block(kind="file", text=f"[文件 {name}]" if name else "[文件]", meta={"name": name})

    if type_name == "Json":
        return Block(kind="card", text="[卡片]", meta={"raw": _get(comp, "data")})

    if type_name == "Music":
        return Block(kind="card", text="[音乐卡片]", meta={"raw": _get(comp, "data", "url")})

    if type_name == "Poke":
        # Poke 组件只带 id（被戳的人），没有昵称；把 target 留在 meta 里，
        # 归档时就能分清是「戳我」还是「戳别人」
        target = str(_get(comp, "id", default="") or "")
        return Block(kind="poke", text="[戳一戳]", meta={"target": target})

    if type_name == "Location":
        title = str(_get(comp, "title", "content", default="") or "")
        return Block(kind="location", text=f"[位置：{title}]" if title else "[位置]")

    if type_name == "Share":
        title = str(_get(comp, "title", default="") or "")
        return Block(kind="card", text=f"[分享：{title}]" if title else "[分享]")

    if type_name == "RPS":
        return Block(kind="face", text="[猜拳]")

    if type_name == "Dice":
        return Block(kind="face", text="[骰子]")

    if type_name in {"Forward", "Node", "Nodes"}:
        return Block(kind="forward", text="[合并转发]", meta={"id": _get(comp, "id", default="")})

    if type_name == "Reply":
        nickname = str(_get(comp, "sender_nickname", default="") or "")
        return Block(
            kind="quote",
            text="[引用了一条消息]",
            meta={
                "id": str(_get(comp, "id", default="") or ""),
                "sender": nickname,
                "chain": getattr(comp, "chain", None) or [],
                "message_str": str(_get(comp, "message_str", default="") or ""),
            },
        )

    text = str(_get(comp, "text", "content", default="") or "").strip()
    if text:
        return Block(kind="unknown", text=text)
    return None


def block_line(block: Block, speaker: str = "") -> str:
    prefix = f"{speaker}: " if speaker else ""
    return f"{prefix}{block.text}"


def blocks_to_text(blocks: list[Block], speaker: str = "") -> str:
    parts: list[str] = []
    for block in blocks:
        text = (block.text or "").strip()
        if not text:
            continue
        if parts and block.kind == "text" and not parts[-1].endswith((" ", "\n")):
            parts[-1] = parts[-1] + text
        else:
            parts.append(block_line(block, speaker if not parts else ""))
    return " ".join(part for part in parts if part).strip()
