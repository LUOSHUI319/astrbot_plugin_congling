"""记忆召回。

默认跑 BM25（中文用 bigram 分词，装了 jieba 就用 jieba），配了 Embedding 模型
再叠一路向量，两路用 RRF 融合。这不是为了炫技：短句记忆里关键词命中很准，
但「上次说的那个事」这种指代只有向量救得回来。

硬约束（防止复读记忆）：
- 已归档 / 遗忘的不召回
- decay 太低的不召回
- 同一原子 24 小时内已注入过就跳过
"""

from __future__ import annotations

import math
import re
import struct
import time
from typing import Any

from astrbot.api import logger

from ..core.contracts import MemoryHit
from .atom import AtomStore

_WORD_RE = re.compile(r"[a-zA-Z0-9_]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

try:  # 可选增强
    import jieba  # type: ignore

    jieba.setLogLevel(60)
    _HAS_JIEBA = True
except Exception:  # pragma: no cover - 没装就降级
    _HAS_JIEBA = False


def tokenize(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    tokens: list[str] = []
    if _HAS_JIEBA:
        try:
            tokens.extend([word for word in jieba.cut(text) if len(word) >= 1 and word.strip()])
        except Exception:
            pass
    if not tokens:
        chars = _CJK_RE.findall(text)
        tokens.extend(chars)
        tokens.extend(f"{chars[i]}{chars[i + 1]}" for i in range(len(chars) - 1))
    tokens.extend(_WORD_RE.findall(text))
    return [token for token in tokens if token.strip()]


class Retriever:
    def __init__(self, plugin: Any, atoms: AtomStore) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self.atoms = atoms

    # ================================================================ 检索
    async def search(
        self,
        query: str,
        *,
        scope: str = "user",
        subject: str = "",
        persona_id: str | None = None,
        k: int | None = None,
        exclude: set[int] | None = None,
        cross_session: bool = False,
        mode: str = "",
    ) -> list[MemoryHit]:
        # 不许再退回写死的 "ling"：多分区环境下那会把所有人的记忆串到一起
        persona_id = persona_id or self.config.persona_id
        query = (query or "").strip()
        top_k = int(k or self.config.int("memory.inject_top_k", 5))
        if not query or top_k <= 0:
            return []

        rows = await self._candidates(scope, subject, persona_id, cross_session=cross_session, mode=mode)
        if not rows:
            return []

        terms = tokenize(query)
        bm25_scores = self._bm25(rows, terms)
        vector_scores = await self._vector(rows, query)

        fused: dict[int, float] = {}
        for ranking in (bm25_scores, vector_scores):
            ordered = sorted(ranking.items(), key=lambda item: item[1], reverse=True)
            for rank, (atom_id, _score) in enumerate(ordered):
                fused[atom_id] = fused.get(atom_id, 0.0) + 1.0 / (60 + rank + 1)

        excluded = exclude or set()
        hits: list[MemoryHit] = []
        by_id = {int(row["id"]): row for row in rows}
        for atom_id, base in fused.items():
            if atom_id in excluded:
                continue
            row = by_id.get(atom_id)
            if not row:
                continue
            importance = float(row.get("importance") or 0.5)
            decay = float(row.get("decay") or 1.0)
            created = int(row.get("created_at") or 0)
            days = max(0.0, (time.time() - created) / 86400) if created else 0.0
            recency = 0.7 + 0.3 * math.exp(-days / 180.0)
            score = base * (0.55 + 0.45 * importance) * (0.5 + 0.5 * decay) * recency
            hits.append(AtomStore.to_hit(row, score))

        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:top_k]

    async def recall_text(
        self, query: str, *, scope: str, subject: str, k: int = 5, cross_session: bool = False
    ) -> str:
        hits = await self.search(
            query, scope=scope, subject=subject, k=k, cross_session=cross_session
        )
        return "\n".join(hit.line() for hit in hits)

    # ---------------------------------------------------------------- 候选
    async def _candidates(
        self,
        scope: str,
        subject: str,
        persona_id: str,
        cross_session: bool = False,
        mode: str = "",
    ) -> list[dict[str, Any]]:
        if cross_session:
            # 跨会话补齐的受众边界。四档：off / same_user / whitelist / all。
            # 以前是全库捞，私聊里能翻出群聊别人的秘密，这是真事故。
            mode = str(mode or self.config.str("memory.cross_session_scope", "same_user") or "same_user").strip().lower()
            if mode in ("self",):
                mode = "same_user"
            if mode == "off":
                return []
            if mode == "whitelist":
                # 只在指定会话之间互通。名单为空等于关闭 —— 空名单不能退化成全库
                allow = [
                    str(x).strip() for x in (self.config.list("memory.cross_session_whitelist", []) or [])
                    if str(x).strip()
                ]
                if not allow:
                    return []
                marks = ",".join("?" * len(allow))
                rows = await self.store.fetch_all(
                    "SELECT id, text, kind, keywords, importance, decay, created_at, source_msg_ids, "
                    "subject_id, scope, last_hit_at, session_id, session_kind, session_label FROM mem_atom "
                    "WHERE persona_id=? AND status='active' AND decay>=0.15 AND (unread IS NULL OR unread=0) "
                    f"AND session_id IN ({marks}) "
                    "ORDER BY importance DESC, id DESC LIMIT 800",
                    (persona_id, *allow),
                )
                return rows
            if mode == "all":
                rows = await self.store.fetch_all(
                    "SELECT id, text, kind, keywords, importance, decay, created_at, source_msg_ids, "
                    "subject_id, scope, last_hit_at, session_id, session_kind, session_label FROM mem_atom "
                    "WHERE persona_id=? AND status='active' AND decay>=0.15 AND (unread IS NULL OR unread=0) "
                    "ORDER BY importance DESC, id DESC LIMIT 800",
                    (persona_id,),
                )
                return rows
            rows = await self.store.fetch_all(
                "SELECT id, text, kind, keywords, importance, decay, created_at, source_msg_ids, "
                "subject_id, scope, last_hit_at, session_id, session_kind, session_label FROM mem_atom "
                "WHERE persona_id=? AND status='active' AND decay>=0.15 AND (unread IS NULL OR unread=0) "
                "AND (subject_id=? OR scope='global') "
                "ORDER BY importance DESC, id DESC LIMIT 800",
                (persona_id, subject),
            )
            return rows
        rows = await self.store.fetch_all(
            "SELECT id, text, kind, keywords, importance, decay, created_at, source_msg_ids, "
            "subject_id, scope, last_hit_at, session_id, session_kind, session_label FROM mem_atom "
            "WHERE persona_id=? AND status='active' AND decay>=0.15 AND (unread IS NULL OR unread=0) "
            "AND (scope='global' OR (scope=? AND subject_id=?) OR (scope=? AND subject_id='')) "
            "ORDER BY importance DESC, id DESC LIMIT 800",
            (persona_id, scope, subject, "user"),
        )
        return rows

    # ---------------------------------------------------------------- BM25
    @staticmethod
    def _bm25(rows: list[dict[str, Any]], terms: list[str]) -> dict[int, float]:
        if not terms:
            return {}
        docs: list[tuple[int, dict[str, int], int]] = []
        for row in rows:
            text = f"{row.get('text') or ''} {row.get('keywords') or ''}"
            tokens = tokenize(text)
            freq: dict[str, int] = {}
            for token in tokens:
                freq[token] = freq.get(token, 0) + 1
            docs.append((int(row["id"]), freq, len(tokens)))

        total = len(docs)
        avg_len = sum(item[2] for item in docs) / max(1, total) or 1.0
        df: dict[str, int] = {}
        for term in set(terms):
            df[term] = sum(1 for _, freq, _ in docs if term in freq)

        k1, b = 1.4, 0.72
        scores: dict[int, float] = {}
        for atom_id, freq, length in docs:
            score = 0.0
            for term in terms:
                tf = freq.get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (total - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
                denom = tf + k1 * (1 - b + b * length / avg_len)
                score += idf * (tf * (k1 + 1)) / max(denom, 1e-6)
            if score > 0:
                scores[atom_id] = score
        return scores

    # ---------------------------------------------------------------- 向量
    async def _vector(self, rows: list[dict[str, Any]], query: str) -> dict[int, float]:
        provider = self._embedding_provider()
        if provider is None:
            return {}
        try:
            query_vec = await provider.get_embedding(query)
        except Exception as exc:
            logger.debug(f"[congling] 查询向量生成失败: {exc!r}")
            return {}
        if not query_vec:
            return {}

        scored: dict[int, float] = {}
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        stored = await self.store.fetch_all(
            f"SELECT id, embedding FROM mem_atom WHERE id IN ({placeholders})", ids
        )
        blobs = {int(row["id"]): row.get("embedding") for row in stored}
        for atom_id, blob in blobs.items():
            if not blob:
                continue
            try:
                vec = struct.unpack(f"{len(blob) // 4}f", blob)
            except Exception:
                continue
            score = self._cosine(query_vec, vec)
            if score > 0.2:
                scored[atom_id] = score
        return scored

    def embedding_available(self) -> bool:
        """是否真的能用向量检索：配了 embedding provider 且拿得到实例。"""
        return self._embedding_provider() is not None

    def _embedding_provider(self) -> Any:
        provider_id = self.config.str("memory.embedding_provider_id", "")
        if not provider_id:
            return None
        try:
            return self.plugin.context.get_provider_by_id(provider_id)
        except Exception:
            return None

    @staticmethod
    def _cosine(a: Any, b: Any) -> float:
        try:
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(y * y for y in b))
            if not na or not nb:
                return 0.0
            return dot / (na * nb)
        except Exception:
            return 0.0

    # ---------------------------------------------------------------- 补向量
    async def embed_pending(self, limit: int = 20) -> int:
        provider = self._embedding_provider()
        if provider is None:
            return 0
        rows = await self.store.fetch_all(
            "SELECT id, text FROM mem_atom WHERE status='active' AND embedding IS NULL "
            "ORDER BY id DESC LIMIT ?",
            (max(1, limit),),
        )
        done = 0
        for row in rows:
            try:
                vec = await provider.get_embedding(str(row.get("text") or ""))
            except Exception:
                continue
            if not vec:
                continue
            blob = struct.pack(f"{len(vec)}f", *[float(x) for x in vec])
            await self.store.execute("UPDATE mem_atom SET embedding=? WHERE id=?", (blob, int(row["id"])))
            done += 1
        return done

    # ---------------------------------------------------------------- 游标
    async def cursor(self, session_id: str) -> dict[str, Any]:
        row = await self.store.fetch_one("SELECT * FROM inject_cursor WHERE session_id=?", (session_id,))
        return row or {}

    async def save_cursor(
        self, session_id: str, last_ids: list[int], last_msg_row: int | None = None
    ) -> None:
        """记录注入过的原子。

        last_msg_row 是消池水位，只有传了值才写——否则注入会顺手把水位清零，
        池子就会反复重抽同一批消息。
        """
        import json

        if last_msg_row is None:
            await self.store.execute(
                "INSERT INTO inject_cursor(session_id, last_msg_row, last_atom_ids, updated_at) "
                "VALUES(?,0,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET last_atom_ids=excluded.last_atom_ids, "
                "updated_at=excluded.updated_at",
                (session_id, json.dumps(last_ids), int(time.time())),
            )
            return
        await self.store.execute(
            "INSERT INTO inject_cursor(session_id, last_msg_row, last_atom_ids, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET last_msg_row=excluded.last_msg_row, "
            "last_atom_ids=excluded.last_atom_ids, updated_at=excluded.updated_at",
            (session_id, int(last_msg_row), json.dumps(last_ids), int(time.time())),
        )
