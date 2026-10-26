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

marker 用来做幂等：同一个 marker 已经在里面了，就替换而不是再追加一条，
免得 tool loop 里重入几次就拼出三份。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

try:  # 正常情况走这个
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 兼容旧版本
    TextPart = None  # type: ignore[assignment]

TEMP_ATTR = "_congling_temp"


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
    if not marker:
        return False
    for part in _ensure_list(req):
        if marker in _part_text(part):
            return True
    return False


def add_temp_part(req: Any, text: str, marker: str = "") -> bool:
    """把一段动态上下文塞进 extra_user_content_parts。

    返回是否真的写进去了。system_prompt 一个字符都不会被动。
    """
    body = (text or "").strip()
    if not body:
        return False
    # marker 要真正写进内容里，否则同一条上下文既查不到也去不了重
    if marker and marker not in body:
        body = f"{marker}\n{body}"
    parts = _ensure_list(req)
    if not parts and getattr(req, "extra_user_content_parts", None) is None:
        logger.debug("[congling] 当前请求不支持 extra_user_content_parts，跳过临时注入")
        return False

    if marker:
        for index, part in enumerate(parts):
            if marker in _part_text(part):
                parts[index] = _build(body)
                return True
    parts.append(_build(body))
    return True


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
