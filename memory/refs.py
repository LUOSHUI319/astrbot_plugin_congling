"""Stable identity keys. Names never participate; missing identity stays empty."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote, unquote


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def platform_key(platform: Any) -> str:
    value = _text(platform).lower()
    return {"aiocqhttp": "qq", "onebot": "qq", "onebot_v11": "qq"}.get(value, value)


def _part(value: Any) -> str:
    return quote(_text(value), safe="", encoding="utf-8", errors="replace")


def subject_ref(platform: Any, user_id: Any) -> str:
    platform = platform_key(platform)
    return f"user:{_part(platform)}:{_part(user_id)}" if platform and _text(user_id) else ""


def session_ref(platform: Any, kind: Any, id: Any) -> str:
    platform, kind = platform_key(platform), _text(kind).lower()
    if not platform or kind not in {"group", "private"} or not _text(id):
        return ""
    return f"session:{_part(platform)}:{kind}:{_part(id)}"


def actor_ref(platform: Any = None, user_id: Any = None, *, persona_id: Any = None) -> str:
    if _text(persona_id):
        return f"bot:{_part(persona_id)}"
    if _text(user_id) in {"bot", "__self__"} or _text(user_id).startswith("bot:"):
        return ""  # A bot identity requires an explicit persona, never a display name.
    return subject_ref(platform, user_id)


def event_key(source_type: Any, source_id: Any, sub_id: Any = "") -> str:
    parts = [_text(source_type), _text(source_id), _text(sub_id)]
    payload = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return "event:" + hashlib.sha256(payload.encode("ascii")).hexdigest()


def parse_ref(value: str, prefix: str) -> tuple[str, ...]:
    parts = str(value or "").split(":")
    size = 4 if prefix == "session" else 3
    if len(parts) != size or parts[0] != prefix or not all(parts):
        return ()
    return tuple(unquote(part) for part in parts[1:])


def resolved_refs(row: dict, platforms: set[str], senders: set[str]) -> tuple[str, str, str, str]:
    """Resolve only a unique platform and evidence actor, shared by backfill/writes."""
    # 空串是"没记录"，default 是 AstrBot 的默认平台名，两者都不算真实平台。
    # 真实库里同一会话会同时出现 `qq` / `default` / 空值，不剔掉就永远算不出唯一平台。
    known = {p for p in platforms if _text(p) and p != "default"}
    platform = next(iter(known)) if len(known) == 1 else ""
    subject = _text(row.get("subject_id"))
    if subject == "__self__" or row.get("scope") == "self":
        about = actor_ref(persona_id=row.get("persona_id"))
    elif subject and not (row.get("session_kind") == "group" and subject == _text(row.get("session_id"))) and (
        subject.isascii() and subject.isdigit() or subject in senders
    ):
        about = subject_ref(platform, subject)
    else:
        about = ""
    where = session_ref(platform, row.get("session_kind"), row.get("session_id"))
    known_senders = {x for x in senders if _text(x)}
    sender = next(iter(known_senders)) if len(known_senders) == 1 else ""
    who = actor_ref(platform, sender, persona_id=row.get("persona_id") if sender == "bot" else None)
    return about, where, who, "resolved" if about and where else "legacy"
