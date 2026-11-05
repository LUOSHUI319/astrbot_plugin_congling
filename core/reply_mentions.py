"""补课回复的提及元数据；正文仍兼容普通字符串，发送时再生成平台 At。"""
from __future__ import annotations

import re
from typing import Iterator

TOKEN = re.compile(r"\[\[\s*congling_at\s*:\s*([0-9]+)\s*\]\]", re.IGNORECASE)


class MentionReply(str):
    def __new__(cls, text: str, mentions: dict[str, str]):
        # QQ 号只接受来自原始归档的数字 ID，不允许模型自行创建提及对象。
        verified = {str(qq): str(name or qq) for qq, name in mentions.items()
                    if re.fullmatch(r"[1-9][0-9]*", str(qq))}
        seen: set[str] = set()

        def normalize(match: re.Match) -> str:
            qq = match.group(1)
            if qq not in verified or qq in seen:
                return ""
            seen.add(qq)
            return f"[[congling_at:{qq}]]"

        body = TOKEN.sub(normalize, text).strip()
        missing = [f"[[congling_at:{qq}]]" for qq in verified if qq not in seen]
        if missing and body:
            body = " ".join(missing) + " " + body
        result = super().__new__(cls, body)
        result.mentions = verified
        return result

    def segments(self, text: str) -> Iterator[tuple[str, str]]:
        offset = 0
        for match in TOKEN.finditer(text):
            if match.start() > offset:
                yield "text", text[offset:match.start()]
            qq = match.group(1)
            if qq in self.mentions:
                yield "at", qq
            offset = match.end()
        if offset < len(text):
            yield "text", text[offset:]

    def readable(self, text: str) -> str:
        """归档或非群聊展示使用可读文本，不落盘内部占位符。"""
        return "".join(f"@{self.mentions[value]}" if kind == "at" else value
                       for kind, value in self.segments(text))
