"""请求级匿名组成旁路；不改变装配、模型、工具或输出预算。"""

import copy
import hashlib
import json
from typing import Any

from .prompt_parts import temp_evidence


SOURCE_LABELS = frozenset({
    "congling.crawled_history", "congling.wake_reply", "congling.night",
    "congling.avatar", "congling.diary", "congling.persona_dynamic",
    "congling.perception", "congling.memory",
})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError("unsupported_component")


def _metric(chunks: list[str]) -> dict[str, Any]:
    # SHA 使用带边界的 JSON 数组；chars/bytes 仅统计解码后的字段原文。
    return {"chars": sum(map(len, chunks)),
            "utf8_bytes": sum(len(s.encode("utf-8")) for s in chunks),
            "sha256": hashlib.sha256(_json(chunks).encode("utf-8")).hexdigest(),
            "items": len(chunks), "tokens": None}


def _content(value: Any) -> list[str]:
    if value is None:
        return []
    if type(value) is str:
        return [value]
    if isinstance(value, list):
        return [p["text"] if isinstance(p, dict) and p.get("type") == "text"
                and type(p.get("text")) is str else _json(_plain(p)) for p in value]
    raise TypeError("unsupported_content")


def request_composition(req: Any, *, final_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """可在最终请求旁路复用；未知/不支持项为 null，不冒充零用量。"""
    try:
        contexts = [_plain(m) for m in (getattr(req, "contexts", None) or [])]
        buckets: dict[str, list[str]] = {
            "history": [], "persona_system": [], "tool_messages": [],
            "assistant_reasoning": [], "tool_calls": [], "current": [],
            "uncontrolled_extra": [],
        }
        system = getattr(req, "system_prompt", "") or ""
        buckets["persona_system"].append(system)
        for m in contexts:
            role = m.get("role")
            target = ("tool_messages" if role == "tool" else
                      "persona_system" if role == "system" else "history")
            buckets[target].extend(_content(m.get("content")))
            if role == "assistant":
                reasoning = m.get("reasoning_content")
                if reasoning is not None:
                    if type(reasoning) is not str:
                        raise TypeError("unsupported_reasoning")
                    buckets["assistant_reasoning"].append(reasoning)
            if m.get("tool_calls"):
                buckets["tool_calls"].append(_json(m["tool_calls"]))
        prompt = getattr(req, "prompt", "") or ""
        buckets["current"].extend(_content(prompt))
        by_index = {e["index"]: e for e in temp_evidence(req)}
        controlled: dict[str, list[str]] = {}
        for i, part in enumerate(getattr(req, "extra_user_content_parts", None) or []):
            raw = _plain(part)
            chunks = _content([raw])
            evidence = by_index.get(i)
            if evidence is None:
                buckets["uncontrolled_extra"].extend(chunks)
                continue
            source = evidence["source"].source_id
            label = source if source in SOURCE_LABELS else "other_controlled"
            controlled.setdefault(label, []).extend(chunks)
        # 只统计当前 ToolSet 的无副作用 schema 投影，无法确认格式时保留未知。
        tools = getattr(req, "func_tool", None)
        schema = [] if tools is None else copy.deepcopy(tools).openai_schema()
        result = {
            "version": 1, "stage": "persona_hook", "final_payload_confirmed": False,
            "tokenizer": None, "system_scope": "complete_request_system_not_persona_only",
            "components": {k: _metric(v) for k, v in buckets.items()},
            "controlled_temp_by_source": {k: _metric(v) for k, v in controlled.items()},
            "tool_schema_openai_projection": _metric([_json(schema)]),
        }
        if final_payload is not None:
            wire = _json(final_payload)
            result["stage"] = "final_payload_sidecar"
            result["final_payload_confirmed"] = True
            result["final_payload_json"] = {
                "chars": len(wire), "utf8_bytes": len(wire.encode("utf-8")),
                "sha256": hashlib.sha256(wire.encode("utf-8")).hexdigest(),
                "tokens": None,
            }
            messages = final_payload.get("messages", [])
            result["final_messages"] = [
                {"index": i, "role": m.get("role") if m.get("role") in
                 {"system", "user", "assistant", "tool"} else "unknown",
                 **_metric([_json(m)])} for i, m in enumerate(messages)]
        return result
    except Exception:
        return {"version": 1, "stage": "unavailable", "tokenizer": None,
                "components": None, "error": "unsupported_or_invalid_component"}


def record_request_composition(event: Any, req: Any) -> None:
    try:
        event.set_extra("congling:request_composition", request_composition(req))
    except Exception:
        pass
