"""原子记忆：入库、去重、合并、状态流转。

去重不做花哨的 SimHash：同 scope 同主体下，用字符集 Jaccard 相似度找近似条目
就足够准（中文短句的字符重合度区分度很高），而且零依赖、可解释。
冲突时新条 supersedes 旧条，旧的转 archived —— 不删，保留证据链。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from astrbot.api import logger

from ..core.contracts import MemoryDraft, MemoryHit

DUP_THRESHOLD = 0.86


def norm_hash(text: str) -> str:
    cleaned = "".join(ch for ch in (text or "") if ch.isalnum())
    return hashlib.sha1(cleaned.encode("utf-8", "ignore")).hexdigest()[:24]


def similarity(a: str, b: str) -> float:
    left, right = set(a or ""), set(b or "")
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class AtomStore:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store

    # ================================================================ 写入
    async def add_many(
        self,
        drafts: list[MemoryDraft],
        *,
        scope: str,
        subject: str,
        persona_id: str = "ling",
    ) -> list[int]:
        written: list[int] = []
        for draft in drafts:
            try:
                atom_id = await self.add(draft, scope=scope, subject=subject, persona_id=persona_id)
            except Exception as exc:
                logger.debug(f"[congling] 写入记忆失败: {exc!r}")
                continue
            if atom_id:
                written.append(atom_id)
        return written

    async def add(
        self,
        draft: MemoryDraft,
        *,
        scope: str = "user",
        subject: str = "",
        persona_id: str = "ling",
    ) -> int:
        text = (draft.text or "").strip()
        if not text:
            return 0
        subject = draft.subject or subject
        digest = norm_hash(text)

        # 1) 精确重复
        existing = await self.store.fetch_one(
            "SELECT id, importance, hit_count FROM mem_atom "
            "WHERE persona_id=? AND scope=? AND subject_id=? AND norm_hash=? AND status='active'",
            (persona_id, scope, subject, digest),
        )
        if existing:
            await self._reinforce(int(existing["id"]), draft.importance)
            return int(existing["id"])

        # 2) 近似重复 / 冲突
        candidates = await self.store.fetch_all(
            "SELECT id, text, importance FROM mem_atom "
            "WHERE persona_id=? AND scope=? AND subject_id=? AND status='active' "
            "ORDER BY id DESC LIMIT 200",
            (persona_id, scope, subject),
        )
        similar_id = 0
        best_ratio = 0.0
        for row in candidates:
            ratio = similarity(text, str(row.get("text") or ""))
            if ratio > best_ratio:
                best_ratio = ratio
                similar_id = int(row.get("id") or 0)
        if similar_id and best_ratio >= DUP_THRESHOLD:
            # 太像了：不重复存，只强化
            await self._reinforce(similar_id, draft.importance)
            return similar_id

        supersedes = None
        if similar_id and 0.6 <= best_ratio < DUP_THRESHOLD and self._looks_conflicting(
            text, next((str(r.get("text") or "") for r in candidates if int(r.get("id") or 0) == similar_id), "")
        ):
            supersedes = similar_id

        now = int(time.time())
        atom_id = await self.store.execute(
            "INSERT INTO mem_atom(persona_id, scope, subject_id, kind, text, norm_hash, keywords, "
            "importance, confidence, source_msg_ids, created_at, last_hit_at, hit_count, decay, status, protect, supersedes, "
            "session_id, session_kind, session_label) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                persona_id,
                scope,
                subject,
                draft.kind,
                text,
                digest,
                json.dumps(draft.keywords, ensure_ascii=False),
                draft.importance,
                draft.confidence,
                json.dumps(draft.evidence_msg_ids, ensure_ascii=False),
                now,
                0,
                0,
                1.0,
                "active",
                1 if draft.importance >= 0.85 else 0,
                supersedes,
                draft.session_id or "",
                draft.session_kind or "",
                draft.session_label or "",
            ),
        )
        if supersedes:
            await self.store.execute(
                "UPDATE mem_atom SET status='archived' WHERE id=?", (supersedes,)
            )
        return int(atom_id or 0)

    @staticmethod
    def _looks_conflicting(new_text: str, old_text: str) -> bool:
        """粗判冲突：一个带否定词，一个不带。"""
        negations = ("不", "没", "别", "讨厌", "反感")
        return any(word in new_text for word in negations) != any(
            word in old_text for word in negations
        )

    async def _reinforce(self, atom_id: int, importance: float) -> None:
        await self.store.execute(
            "UPDATE mem_atom SET hit_count=hit_count+1, last_hit_at=?, "
            "importance=MIN(1.0, MAX(importance, ?)+0.02), decay=MIN(1.0, decay+0.05) WHERE id=?",
            (int(time.time()), importance, atom_id),
        )

    # ================================================================ 读取
    async def get(self, atom_id: int) -> dict[str, Any] | None:
        return await self.store.fetch_one("SELECT * FROM mem_atom WHERE id=?", (int(atom_id),))

    async def list(
        self,
        *,
        scope: str | None = None,
        subject: str | None = None,
        status: str | None = "active",
        kind: str | None = None,
        keyword: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM mem_atom WHERE 1=1"
        params: list[Any] = []
        if session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        if subject:
            sql += " AND subject_id=?"
            params.append(subject)
        if status:
            sql += " AND status=?"
            params.append(status)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if keyword:
            sql += " AND (text LIKE ? OR keywords LIKE ?)"
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(500, limit)), max(0, offset)])
        rows = await self.store.fetch_all(sql, params)
        for row in rows:
            row["keywords"] = self._loads(row.get("keywords"), [])
            row["evidence"] = self._loads(row.get("source_msg_ids"), [])
            row["created_text"] = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(int(row.get("created_at") or 0))
            )
        return rows

    async def sessions(self, status: str = "active") -> list[dict[str, Any]]:
        """按会话汇总记忆条数，给管理页做左侧会话列表。"""
        rows = await self.store.fetch_all(
            "SELECT session_id, session_kind, session_label, COUNT(*) AS cnt, "
            "MAX(created_at) AS last_at, SUM(CASE WHEN importance>=0.85 THEN 1 ELSE 0 END) AS important "
            "FROM mem_atom WHERE status=? GROUP BY session_id, session_kind, session_label "
            "ORDER BY last_at DESC",
            (status,),
        )
        for row in rows:
            row["count"] = int(row.get("cnt") or 0)
            row["important"] = int(row.get("important") or 0)
            row["last_text"] = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(int(row.get("last_at") or 0))
            )
            row["kind"] = str(row.get("session_kind") or "")
            row["label"] = str(row.get("session_label") or row.get("session_id") or "未知会话")
        return rows

    async def count(self, status: str = "active") -> int:
        value = await self.store.scalar(
            "SELECT COUNT(*) FROM mem_atom WHERE status=?", (status,), 0
        )
        return int(value or 0)

    # ================================================================ 修改
    async def set_status(self, atom_id: int, status: str) -> None:
        await self.store.execute("UPDATE mem_atom SET status=? WHERE id=?", (status, atom_id))

    async def set_fields(self, atom_id: int, fields: dict[str, Any]) -> None:
        allowed = {"text", "importance", "protect", "kind", "status", "scope", "subject_id"}
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key}=?")
            params.append(value)
        if not sets:
            return
        params.append(int(atom_id))
        await self.store.execute(f"UPDATE mem_atom SET {', '.join(sets)} WHERE id=?", params)

    async def mark_hit(self, ids: list[int]) -> None:
        if not ids:
            return
        now = int(time.time())
        for atom_id in ids:
            try:
                await self.store.execute(
                    "UPDATE mem_atom SET hit_count=hit_count+1, last_hit_at=?, "
                    "decay=MIN(1.0, decay+0.12) WHERE id=?",
                    (now, int(atom_id)),
                )
            except Exception:
                continue

    # ================================================================ 转换
    @staticmethod
    def to_hit(row: dict[str, Any], score: float = 0.0) -> MemoryHit:
        try:
            evidence = json.loads(row.get("source_msg_ids") or "[]")
        except (ValueError, TypeError):
            evidence = []
        return MemoryHit(
            atom_id=int(row.get("id") or 0),
            text=str(row.get("text") or ""),
            kind=str(row.get("kind") or "fact"),
            importance=float(row.get("importance") or 0.5),
            score=score,
            created_at=int(row.get("created_at") or 0),
            decay=float(row.get("decay") or 1.0),
            evidence=[str(item) for item in evidence][:5],
            subject_id=str(row.get("subject_id") or ""),
            scope=str(row.get("scope") or "user"),
            session_id=str(row.get("session_id") or ""),
            session_kind=str(row.get("session_kind") or ""),
            session_label=str(row.get("session_label") or ""),
        )

    @staticmethod
    def _loads(value: Any, default: Any) -> Any:
        if isinstance(value, (list, dict)):
            return value
        if not value:
            return default
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default
