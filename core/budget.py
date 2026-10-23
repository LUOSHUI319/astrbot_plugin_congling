"""token 预算。

目的不是省到极致，而是永远不要爆窗口、不要一笔账莫名其妙变贵。
估算很粗（中文按字、英文按 4 字符），但足够做裁剪决策。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = max(0, len(text) - cjk)
    return cjk + (rest + 3) // 4


@dataclass
class BudgetSlice:
    name: str
    priority: int
    text: str
    tokens: int = 0

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = estimate_tokens(self.text)


@dataclass
class BudgetResult:
    kept: list[BudgetSlice] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    total_tokens: int = 0
    limit: int = 0

    def render(self, separator: str = "\n") -> str:
        return separator.join(item.text for item in self.kept if item.text)

    def report(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "total": self.total_tokens,
            "kept": [{"name": item.name, "tokens": item.tokens} for item in self.kept],
            "dropped": self.dropped,
        }


class Budget:
    """按优先级装填，超了就从最低优先级开始丢。"""

    def __init__(self, config: Any) -> None:
        self.config = config

    def limit(self, model_window: int = 32000) -> int:
        ratio = self.config.float("memory.inject_budget_ratio", 0.25)
        # 记忆最多占 window * ratio；其余模块另算，这里给整轮一个软上限
        return max(600, int(model_window * max(0.05, min(ratio * 2.5, 0.6))))

    def pack(self, slices: list[BudgetSlice], limit: int) -> BudgetResult:
        result = BudgetResult(limit=limit)
        ordered = sorted(slices, key=lambda item: item.priority)
        for item in ordered:
            if result.total_tokens + item.tokens <= limit:
                result.kept.append(item)
                result.total_tokens += item.tokens
            else:
                result.dropped.append(item.name)
        # 保持原有顺序输出，读起来才连贯
        order = {id(item): index for index, item in enumerate(slices)}
        result.kept.sort(key=lambda item: order.get(id(item), 0))
        return result

    def trim_text(self, text: str, max_tokens: int) -> str:
        if estimate_tokens(text) <= max_tokens:
            return text
        approx_chars = max(1, max_tokens)
        return text[:approx_chars] + "…"
