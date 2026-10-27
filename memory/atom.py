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
from .grade import STRENGTH, next_review_at

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
        # 等级：0 不可忘 / 1~5 依次加重。等级决定初始强度、衰减速度和复习时间
        try:
            level = max(0, min(5, int(getattr(draft, "level", 3) or 3)))
        except (TypeError, ValueError):
            level = 3
        strength = STRENGTH.get(level, 0.65)
        next_review = next_review_at(level, now)
        # 睡着期间产生的记忆打上「未曾爬楼」，醒后要先爬楼才能读。
        # 判定不能只看「写入这一刻在不在睡」：抽取是延后批量的（静默 90 秒 / 攒 30 条），
        # 睡着期间的消息往往在醒来之后才入库，那时 is_asleep() 早就是假的了，
        # 于是未读标记永远打不上，睡着期间的事直接进记忆库、又被检索层排除——
        # 结果就是「面板看得见、她却想不起来」。先按证据消息的时间比对睡眠区间。
        unread = 0
        unread_since = 0
        asleep_now = False
        night = getattr(self.plugin, "night", None)
        crawl_on = True
        if night is not None and getattr(self.plugin, "cfg", None) is not None:
            try:
                crawl_on = self.plugin.cfg.bool("night.crawl_enabled", True)
                asleep_now = bool(crawl_on and night.state.state.is_asleep())
            except Exception:
                asleep_now = False
        if asleep_now:
            unread, unread_since = 1, now
        elif crawl_on:
            covered_at = await self._sleep_hit(draft.evidence_msg_ids)
            if covered_at > 0:
                unread, unread_since = 1, covered_at

        atom_id = await self.store.execute(
            "INSERT INTO mem_atom(persona_id, scope, subject_id, kind, text, norm_hash, keywords, "
            "importance, confidence, source_msg_ids, created_at, last_hit_at, hit_count, decay, status, protect, supersedes, "
            "session_id, session_kind, session_label, unread, unread_since, "
            "mem_level, level_reason, level_tags, level_source, strength, next_review_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                1 if level == 0 else 0,
                supersedes,
                draft.session_id or "",
                draft.session_kind or "",
                draft.session_label or "",
                unread,
                unread_since,
                level,
                draft.level_reason or "",
                json.dumps(draft.level_tags or [], ensure_ascii=False),
                draft.level_source or "llm",
                strength,
                next_review,
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

    async def _sleep_hit(self, msg_ids: Any, limit: int = 50) -> int:
        """这批证据消息里，有没有落在最近某段睡眠区间内的。

        返回命中的那个时间戳（用来当 unread_since），没命中返回 0。
        睡眠区间由 night.state 在醒来时落盘（kv night.last_sleep_window，最近三段）。
        """
        ids = [str(x) for x in (msg_ids or []) if str(x).strip()][:limit]
        if not ids:
            return 0
        try:
            marks = ",".join("?" * len(ids))
            row = await self.store.fetch_one(
                f"SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM msg_archive WHERE msg_id IN ({marks})",
                tuple(ids),
            )
        except Exception:
            return 0
        if not row:
            return 0
        lo = int(row.get("lo") or 0)
        hi = int(row.get("hi") or 0)
        if lo <= 0:
            return 0
        try:
            windows = await self.store.kv_get("night.last_sleep_window", []) or []
        except Exception:
            return 0
        if not isinstance(windows, list):
            return 0
        for item in windows:
            if not isinstance(item, dict):
                continue
            start = int(item.get("sleep_at") or 0)
            end = int(item.get("wake_at") or 0)
            if start <= 0:
                continue
            end = end if end > start else start
            # 区间有交集就算：一条记忆可能跨了几个消息，取头尾比一下
            if hi >= start and lo <= end:
                return max(lo, start)
        return 0

    async def _reinforce(self, atom_id: int, importance: float) -> None:
        now = int(time.time())
        boost = 1.5
        try:
            boost = float(self.config.float("memory.forgetting.hit_boost", 1.5))
        except Exception:
            boost = 1.5
        level = 3
        try:
            row = await self.store.fetch_one("SELECT mem_level FROM mem_atom WHERE id=?", (int(atom_id),))
            if row:
                level = int(row.get("mem_level") or 3)
        except Exception:
            level = 3
        base = STRENGTH.get(level, 0.65)
        await self.store.execute(
            "UPDATE mem_atom SET hit_count=hit_count+1, last_hit_at=?, "
            "importance=MIN(1.0, MAX(importance, ?)+0.02), decay=MIN(1.0, decay+0.05), "
            "strength=MAX(strength, MIN(1.0, ? * ?)), next_review_at=? WHERE id=?",
            (now, importance, base, boost, next_review_at(level, now, boost), int(atom_id)),
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
        include_unread: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM mem_atom WHERE 1=1"
        params: list[Any] = []
        # 「未曾爬楼」的记忆不算进记忆库：她还没看过，拿不出来用，
        # 也不该跟看过的混在一起展示。要看得显式传 include_unread。
        if not include_unread:
            sql += " AND (unread IS NULL OR unread=0)"
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
            row["level_tags"] = self._loads(row.get("level_tags"), [])
            row["mem_level"] = int(row.get("mem_level") or 3)
            row["created_text"] = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(int(row.get("created_at") or 0))
            )
        return rows

    async def sessions(self, status: str = "active", include_unread: bool = False) -> list[dict[str, Any]]:
        """按会话汇总记忆条数，给管理页做左侧会话列表。"""
        gate = "" if include_unread else " AND (unread IS NULL OR unread=0)"
        rows = await self.store.fetch_all(
            "SELECT session_id, session_kind, session_label, COUNT(*) AS cnt, "
            "MAX(created_at) AS last_at, SUM(CASE WHEN importance>=0.85 THEN 1 ELSE 0 END) AS important "
            "FROM mem_atom WHERE status=?" + gate + " GROUP BY session_id, session_kind, session_label "
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

    async def count(self, status: str = "active", include_unread: bool = False) -> int:
        gate = "" if include_unread else " AND (unread IS NULL OR unread=0)"
        value = await self.store.scalar(
            "SELECT COUNT(*) FROM mem_atom WHERE status=?" + gate, (status,), 0
        )
        return int(value or 0)

    async def count_unread(self) -> int:
        """未爬记忆条数（面板黄条与统计卡用）。"""
        try:
            value = await self.store.scalar(
                "SELECT COUNT(*) FROM mem_atom WHERE status='active' AND unread=1", (), 0
            )
        except Exception:
            return 0
        return int(value or 0)

    # ================================================================ 修改
    async def set_status(self, atom_id: int, status: str) -> None:
        await self.store.execute("UPDATE mem_atom SET status=? WHERE id=?", (status, atom_id))

    async def set_fields(self, atom_id: int, fields: dict[str, Any]) -> None:
        allowed = {
            "text",
            "importance",
            "protect",
            "kind",
            "status",
            "scope",
            "subject_id",
            "mem_level",
            "level_reason",
            "level_source",
            "strength",
        }
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
            level=int(row.get("mem_level") or 3),
            strength=float(row.get("strength") or 0.65),
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
