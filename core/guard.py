"""输出守卫。

用户定的硬规矩：不带 emoji、句末不带句号、不换行、不空行。
这里做成规则表而不是散落的 replace，理由：
1. 规则要能在 UI 里一条条开关；
2. 与 no_newline / quote_probability 同时存在时能按条让位，不会互相覆盖；
3. 幂等——重复跑结果一致，避免多次 hook 叠加出乱码。
"""

from __future__ import annotations

import re
from typing import Any

# emoji 与常见符号（含变体选择符、肤色修饰、零宽连接）
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U0001f000-\U0001f2ff"
    "\U00002600-\U000027bf"
    "\U0001f1e6-\U0001f1ff"
    "\U0000fe0f"
    "\U0000200d"
    "\U00002b00-\U00002bff"
    "\U00002190-\U000021ff"
    "\U0000fe00-\U0000fe0f"
    "]+",
    flags=re.UNICODE,
)

# 颜文字：(=・ω・=) 这类
_KAOMOJI_RE = re.compile(r"[（(][^（）()]{0,12}[ω・∀▽｀´°￣][^（）()]{0,12}[)）]")

_EMPTY_LINES_RE = re.compile(r"\n{2,}")
_MULTI_SPACE_RE = re.compile(r"[ \t\u3000]{2,}")


class OutputGuard:
    def __init__(self, config: Any, probe: Any = None) -> None:
        self.config = config
        self.probe = probe

    # ------------------------------------------------------------ 单条文本
    def _rule_enabled(self, key: str, default: bool = True) -> bool:
        return self.config.bool(f"guard.{key}", default)

    def clean(self, text: str) -> str:
        if not text:
            return ""
        result = text

        if self._rule_enabled("strip_emoji") and not self._external("guard_newline", "emoji"):
            result = _EMOJI_RE.sub("", result)
            result = _KAOMOJI_RE.sub("", result)

        # 禁用语：直接删，不替换成占位符
        for phrase in self.config.list("guard.forbid_phrases", []) or []:
            if phrase and phrase in result:
                result = result.replace(str(phrase), "")

        if self._rule_enabled("flatten_newline") and not self._external("guard_newline", "newline"):
            result = result.replace("\r\n", "\n").replace("\r", "\n")
            result = _EMPTY_LINES_RE.sub("\n", result)
            result = result.replace("\n", " ")

        result = _MULTI_SPACE_RE.sub(" ", result).strip()

        if self._rule_enabled("strip_trailing_period"):
            result = self._strip_period(result)

        return result

    @staticmethod
    def _strip_period(text: str) -> str:
        """去掉句末句号，但保留问号/感叹号/省略号结尾。"""
        result = text
        while result and result[-1] in "。｡":
            result = result[:-1].rstrip()
        return result

    def _external(self, tag: str, rule: str) -> bool:
        if self.probe is None:
            return False
        try:
            return self.probe.guard_owned_externally(tag)
        except Exception:
            return False

    # ------------------------------------------------------------ 分句清理
    def clean_bubbles(self, bubbles: list[str]) -> list[str]:
        max_chars = self.config.int("guard.max_chars_per_bubble", 60)
        result: list[str] = []
        for bubble in bubbles:
            cleaned = self.clean(bubble)
            if not cleaned:
                continue
            for piece in self._hard_split(cleaned, max_chars):
                if piece:
                    result.append(piece)
        return result

    @staticmethod
    def _hard_split(text: str, max_chars: int) -> list[str]:
        if max_chars <= 0 or len(text) <= max_chars:
            return [text]
        pieces: list[str] = []
        remaining = text
        while len(remaining) > max_chars:
            window = remaining[:max_chars]
            cut = max(window.rfind("，"), window.rfind("、"), window.rfind("，"), window.rfind(" "))
            if cut < max_chars // 2:
                cut = max_chars
            pieces.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        if remaining:
            pieces.append(remaining)
        return pieces

    # ------------------------------------------------------------ 重复自检
    @staticmethod
    def too_similar(text: str, recent: list[str], threshold: float = 0.9) -> bool:
        """粗粒度相似度（字符集 Jaccard），够用且零依赖。"""
        if not text or not recent:
            return False
        target = set(text)
        if not target:
            return False
        for item in recent:
            other = set(item or "")
            if not other:
                continue
            inter = len(target & other)
            union = len(target | other)
            if union and inter / union >= threshold:
                return True
        return False

    # ------------------------------------------------------------ 消息链
    def clean_chain(self, chain: list[Any]) -> bool:
        """就地清理消息链里的 Plain 组件，返回是否有改动。"""
        from astrbot.api.message_components import Plain

        changed = False
        for comp in chain or []:
            if not isinstance(comp, Plain):
                continue
            text = getattr(comp, "text", "") or ""
            cleaned = self.clean(text)
            if cleaned != text:
                comp.text = cleaned
                changed = True
        return changed
