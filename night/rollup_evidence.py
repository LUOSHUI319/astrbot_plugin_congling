"""本地保留有界原句，避免让模型再次改写时间、金额、否定和归属。"""
from __future__ import annotations

import re
from typing import Any

from .rollup_cache import digest

MAX_FACTS = 12
MAX_QUOTE_CHARS = 240
MAX_FACT_CHARS = 1800
_SENTENCE = re.compile(r"[^。！？!?\n；;]+[。！？!?；;]?")
_IMPORTANT = re.compile(
    r"\d|[一二三四五六七八九十百千万]+(?:元|块|点|月|日|天)|"
    r"(?:今天|明天|后天|昨天|周[一二三四五六日天]).*(?:交付|发布|开会|截止|到账|取消|改期)|"
    r"截止|取消|改为|改成|更正|延期|不是|不能|不要|不用|没有|尚未|不再|还没|"
    r"未(?:结束|批准|付款|通过|完成|授权)|仅限|只(?:允许|限|能|给)|除非|如果|"
    r"审批|审核|授权|负责人|编号|状态|@|[?？]"
)
_CORRECTION = re.compile(r"更正|取消|改为|改成|延期|不是|不再|尚未|除非")
_CONDITION = re.compile(r"不能|不要|不用|没有|还没|仅限|只(?:允许|限|能|给)|审批|授权|如果")


def source_hash(row: dict[str, Any]) -> str:
    return digest({key: row.get(key) for key in
                   ("id", "session_id", "msg_id", "sender_id", "sender_name", "text")})


def extract_facts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只摘完整且逐字存在的短句；不是全文替代，超长句不静默切断。"""
    candidates = []
    for row in rows:
        if not row.get("msg_id"):
            continue
        text = str(row.get("text") or "")
        for match in _SENTENCE.finditer(text):
            quote = match.group().strip()
            if not quote or len(quote) > MAX_QUOTE_CHARS or not _IMPORTANT.search(quote):
                continue
            fact = {
                "row_id": int(row.get("id") or 0),
                "msg_id": str(row["msg_id"]),
                "session_id": str(row.get("session_id") or ""),
                "sender_id": str(row.get("sender_id") or ""),
                "sender_name": str(row.get("sender_name") or row.get("sender_id") or "某人"),
                "ts": int(row.get("ts") or 0),
                "quote": quote,
            }
            # 更正和数字优先，超限时优先保留较新的信息，最终仍按原始顺序展示。
            score = (4 * bool(_CORRECTION.search(quote))
                     + 3 * bool(_CONDITION.search(quote)) + 2 * bool(re.search(r"\d", quote)))
            candidates.append((score, fact["row_id"], match.start(), fact))
    selected = []
    chars = 0
    seen = set()
    for candidate in sorted(candidates, key=lambda x: x[:3], reverse=True):
        fact = candidate[3]
        identity = (fact["sender_id"], fact["quote"])
        if identity in seen:
            continue
        if len(selected) >= MAX_FACTS or chars + len(fact["quote"]) > MAX_FACT_CHARS:
            continue
        selected.append(candidate)
        seen.add(identity)
        chars += len(fact["quote"])
    return [item[3] for item in sorted(selected, key=lambda x: x[1:3])]


def facts_match(facts: list[dict[str, Any]], rows: list[dict[str, Any]]) -> bool:
    sources = {int(row.get("id") or 0): row for row in rows}
    for fact in facts:
        row = sources.get(int(fact.get("row_id") or 0))
        if (row is None or str(row.get("msg_id") or "") != fact.get("msg_id")
                or str(row.get("session_id") or "") != fact.get("session_id")
                or str(row.get("sender_id") or "") != fact.get("sender_id")
                or str(row.get("sender_name") or row.get("sender_id") or "某人") != fact.get("sender_name")
                or int(row.get("ts") or 0) != fact.get("ts")
                or not fact.get("quote") or fact["quote"] not in str(row.get("text") or "")):
            return False
    return True


def render_facts(facts: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[msg_id={f['msg_id']}][{f['sender_name']}][ts={f['ts']}] {f['quote']}"
        for f in facts
    )
