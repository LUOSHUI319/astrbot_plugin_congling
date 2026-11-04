"""临时上下文注入。

规矩只有一条：**每轮都会变的东西，绝不写进 system_prompt。**

system_prompt 是 prompt 的最前面那一段，几乎所有厂商都按前缀做缓存。
一旦每轮往里拼"这次召回了哪几条记忆""这条消息解析出了什么""现在是几点、累不累"，
前缀就每轮都在变，缓存命中率直接掉到 0，等于每轮全价重算。

所以动态内容统一走 `req.extra_user_content_parts`：
AstrBot 会把它们拼在**用户消息那一段**的末尾，system_prompt 保持不动，
前缀缓存照样命中。再配合 `mark_as_temp()`，这些东西只喂给模型、
不会被写回会话历史（否则历史会越滚越长，下一轮又变成新的前缀）。

用法：

    from ..core.prompt_parts import add_temp_part
    add_temp_part(req, "【长期记忆】...", marker="<!--congling:memory-->")

marker 仅用于正文标记，不是来源证明。受控调用另传请求内来源和刷新版本；
未知内容保留，只有来源、原文、角色及权限全部一致才消除重复装配。
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import hashlib
import json
import threading
from typing import Any

from astrbot.api import logger

try:  # 正常情况走这个
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 兼容旧版本
    TextPart = None  # type: ignore[assignment]

TEMP_ATTR = "_congling_temp"

# Reload retains the commit lock; no lock or extra metadata is attached to req.
if "_COMMIT_LOCK" not in globals():
    _COMMIT_LOCK = threading.RLock()


@dataclass(frozen=True)
class TempSource:
    source_id: str
    occurrence_id: str
    scope: str
    principal: str
    permissions: tuple[str, ...]
    original_time: str
    role: str = "user"
    slot: str = ""
    version: int = 0
    assembly_refs: tuple[str, ...] = ()
    replace_slot: bool = False


@dataclass(frozen=True)
class _Evidence:
    part: Any
    source: TempSource
    raw: str
    raw_sha256: str
    wire: str
    wire_sha256: str
    refs: tuple[str, ...]


class _ControlledParts(list):
    def __init__(self, parts: list[Any], owner: Any, evidence: tuple[_Evidence, ...]):
        super().__init__(parts)
        self.owner = owner
        self.evidence = evidence
        self._issued_evidence = evidence
        self._issued_signature = _evidence_signature(evidence)

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        # 新请求只继承正文，不继承旧请求的删除授权。
        return copy.deepcopy(list(self), memo)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _evidence_signature(records: tuple[_Evidence, ...]) -> str:
    return _sha(json.dumps(
        [(asdict(e.source), e.raw, e.raw_sha256, e.wire, e.wire_sha256, e.refs)
         for e in records], ensure_ascii=True, separators=(",", ":"), sort_keys=True))


def _valid_source(source: Any) -> bool:
    return (type(source) is TempSource and source.role == "user"
            and all(type(x) is str and x for x in (
                source.source_id, source.occurrence_id, source.scope,
                source.principal, source.original_time))
            and type(source.permissions) is tuple and bool(source.permissions)
            and all(type(x) is str and x for x in source.permissions)
            and type(source.version) is int and source.version >= 0
            and type(source.slot) is str
            and type(source.replace_slot) is bool
            and type(source.assembly_refs) is tuple and bool(source.assembly_refs)
            and all(type(x) is str and x for x in source.assembly_refs))


def _origin(source: TempSource) -> tuple[Any, ...]:
    return (source.source_id, source.occurrence_id, source.scope, source.principal,
            source.permissions, source.original_time, source.role, source.slot)


def _records(req: Any, parts: Any) -> tuple[_Evidence, ...]:
    if type(parts) is not _ControlledParts or parts.owner is not req:
        return ()
    # 对象身份加完整原文校验；正文包含 marker 或相同子串都不能冒充来源。
    records = parts.evidence
    if type(records) is not tuple or records is not parts._issued_evidence or any(
        type(e) is not _Evidence or not _valid_source(e.source)
        or type(e.refs) is not tuple or not e.refs
        or any(type(ref) is not str or not ref for ref in e.refs)
        or any(ref not in e.refs for ref in e.source.assembly_refs)
        or _sha(e.raw) != e.raw_sha256 or _sha(e.wire) != e.wire_sha256
        or _part_text(e.part) != e.wire for e in records
    ) or _evidence_signature(records) != parts._issued_signature:
        raise ValueError("invalid_temp_evidence")
    return records


def temp_evidence(req: Any) -> list[dict[str, Any]]:
    """私密请求内引用接口；统计日志不能直接序列化此接口。"""
    try:
        parts = getattr(req, "extra_user_content_parts", None)
        records = _records(req, parts)
        return [{"index": i, "source": e.source, "raw_sha256": e.raw_sha256,
                 "wire_sha256": e.wire_sha256, "assembly_refs": e.refs}
                for i, part in enumerate(parts or [])
                for e in records if e.part is part]
    except Exception:
        return []


def next_temp_version(req: Any, slot: str) -> int:
    try:
        records = _records(req, getattr(req, "extra_user_content_parts", None))
        return 1 + max((e.source.version for e in records if e.source.slot == slot), default=0)
    except Exception:
        return 1


def request_temp_source(req: Any, event: Any, source_id: str, *, version: int) -> TempSource | None:
    """调用点声明当前请求授权范围；不推断其他插件/历史的权限。"""
    try:
        scope = str(event.unified_msg_origin or "")
        principal = str(event.get_sender_id() or "")
        getter = getattr(event, "get_message_id", None)
        occurrence = str(getter() or "") if callable(getter) else ""
        if not occurrence:
            occurrence = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        created_at = getattr(event, "created_at", None)
        if not scope or not principal or not occurrence or created_at is None:
            return None
        return TempSource(source_id, occurrence, scope, principal,
                          (scope,), str(created_at), slot=source_id,
                          version=version, assembly_refs=(f"{source_id}:call:{version}",),
                          replace_slot=True)
    except Exception:
        return None


def _ensure_list(req: Any) -> list[Any]:
    parts = getattr(req, "extra_user_content_parts", None)
    if parts is None:
        parts = []
        try:
            setattr(req, "extra_user_content_parts", parts)
        except Exception:
            return []
    return parts


def _part_text(part: Any) -> str:
    if isinstance(part, dict):
        return str(part.get("text") or "")
    return str(getattr(part, "text", "") or "")


def _build(text: str) -> Any:
    """造一个「只给模型看、不落库」的内容块。"""
    if TextPart is None:  # 老版本兜底成 dict
        return {"type": "text", "text": text}
    try:
        return TextPart(text=text).mark_as_temp()
    except Exception:  # mark_as_temp 不存在时也别炸
        part = TextPart(text=text)
        try:
            setattr(part, TEMP_ATTR, True)
        except Exception:
            pass
        return part


def has_temp_part(req: Any, marker: str) -> bool:
    # 只报告本 helper 已验证的临时块，不认领未知 marker/raw 内容。
    try:
        for e in _records(req, getattr(req, "extra_user_content_parts", None)):
            if marker and e.wire.startswith(marker + "\n"):
                return True
    except Exception:
        pass
    return False


def add_temp_part(req: Any, text: str, marker: str = "", *,
                  source: TempSource | None = None) -> bool:
    """Append or deduplicate controlled user content as one locked transaction."""
    # Serialize the whole read/build/compare/assign transaction, not just assign.
    with _COMMIT_LOCK:
        return _add_temp_part_locked(req, text, marker, source=source)


def _add_temp_part_locked(req: Any, text: str, marker: str = "", *,
                          source: TempSource | None = None) -> bool:
    """把一段动态上下文塞进 extra_user_content_parts。

    返回是否真的写进去了。system_prompt 一个字符都不会被动。
    """
    try:
        if type(text) is not str or not text.strip() or type(marker) is not str:
            return False
        body = f"{marker}\n{text}" if marker and marker not in text else text
        original = getattr(req, "extra_user_content_parts", None)
        if original is not None and not isinstance(original, list):
            return False
        parts = list(original or [])
        evidence_snapshot = (original.evidence
                             if type(original) is _ControlledParts else None)
        try:
            records = _records(req, original)
        except (ValueError, TypeError, AttributeError):
            # 来源损坏只撤销删除授权：保留已有块，按普通注入追加当前原文。
            records = ()
        controlled = _valid_source(source)
        same = []
        refresh = []
        if controlled:
            for i, part in enumerate(parts):
                e = next((e for e in records if e.part is part), None)
                if e is None or _origin(e.source) != _origin(source):
                    continue
                if e.raw == text and e.raw_sha256 == _sha(text) and e.wire == body:
                    same.append((i, e))
                elif (source.replace_slot and e.source.replace_slot and source.slot
                      and source.version > e.source.version):
                    refresh.append((i, e))
        # 先构造完整候选，最后仅赋值一次；任何异常都不会留下半份列表。
        selected = same + refresh
        refs = tuple(dict.fromkeys(
            [ref for _, e in sorted(selected) for ref in e.refs]
            + (list(source.assembly_refs) if controlled else [])))
        if same:
            keep = min(i for i, _ in selected)
            part = parts[min(i for i, _ in same)]
        else:
            keep = min((i for i, _ in refresh), default=len(parts))
            part = _build(body)
            if _part_text(part) != body:
                return False
        removed = {i for i, _ in selected}
        candidate = [p for i, p in enumerate(parts) if i not in removed]
        candidate.insert(keep - sum(i < keep for i in removed), part)
        new_records = tuple(e for e in records if any(e.part is p for p in candidate)
                            and e.part is not part)
        if controlled:
            if same:
                source = max([source] + [e.source for _, e in same],
                             key=lambda s: s.version)
            new_records += (_Evidence(part, source, text, _sha(text), body, _sha(body), refs),)
        replacement = _ControlledParts(candidate, req, new_records)
        if getattr(req, "extra_user_content_parts", None) is not original:
            return False
        namespace = getattr(req, "__dict__", None)
        if type(namespace) is not dict or any(
            hasattr(cls.__dict__.get("extra_user_content_parts"), "__set__")
            for cls in type(req).__mro__
        ):
            return False
        # A reentrant builder or an uncoordinated mutation invalidates the snapshot.
        if original is not None and (
            len(original) != len(parts) or any(a is not b for a, b in zip(original, parts))
        ):
            return False
        if type(original) is _ControlledParts:
            if original.evidence is not evidence_snapshot:
                return False
            try:
                current_records = _records(req, original)
            except (ValueError, TypeError, AttributeError):
                current_records = ()
            if current_records is not records:
                return False
        if namespace.get("extra_user_content_parts") is not original:
            return False
        namespace["extra_user_content_parts"] = replacement
        return True
    except Exception:
        return False


def append_user_pair(req: Any, user_text: str, assistant_text: str = "") -> bool:
    """退路：把内容作为一对 user/assistant 追加到对话历史。

    只有拿不到 extra_user_content_parts 时才用（比如某些自建 provider），
    比改 system_prompt 好，但会进历史，能用上面那条就用上面那条。
    """
    user_text = (user_text or "").strip()
    if not user_text:
        return False
    contexts = getattr(req, "contexts", None)
    if not isinstance(contexts, list):
        return False
    contexts.append({"role": "user", "content": user_text})
    if assistant_text:
        contexts.append({"role": "assistant", "content": assistant_text})
    return True
