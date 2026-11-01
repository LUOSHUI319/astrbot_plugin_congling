"""Explicit narrator identity and provenance labels; never rewrite historical prose."""

from typing import Any

from astrbot.api import logger
from ..core.exc import exc_brief

SELF_SUBJECT = "__self__"
GENERIC_SUBJECTS = {"", "用户", "聊天对象", "对方", "他", "她", "我"}


async def narrator_name(plugin: Any) -> str:
    name = plugin.cfg.str("persona.name", "AstrBot 当前人格") or "AstrBot 当前人格"
    book = getattr(plugin, "persona", None)
    if book is not None and hasattr(book, "display_name"):
        name = await book.display_name() or name
    return name


def material_label(subject: str, *, subject_ref: str = "", ref_status: str = "") -> str:
    if subject == SELF_SUBJECT or subject_ref.startswith("bot:"):
        return "[主体是我]"
    if subject_ref.startswith("user:"):
        return f"[他人：{subject}；不是我的经历]"
    return "[主体未确认；不能直接认领为我的经历]"


async def remember_action(plugin: Any, event_type: str, event_id: str, text: str,
                          *, session_id: str = "", session_kind: str = "",
                          private_text: str = "", platform: str = "qq",
                          occurred_at: int | None = None, result: dict | None = None) -> None:
    memory = getattr(plugin, "memory", None)
    record = getattr(memory, "record_self_event", None)
    if record is None:
        return
    try:
        await record(event_type, event_id, text, session_id=session_id, session_kind=session_kind,
                     private_text=private_text, platform=platform, occurred_at=occurred_at, result=result)
    except Exception as exc:
        logger.warning(f"[congling] 自述行为记忆写入失败({event_type}): {exc_brief(exc)}")
