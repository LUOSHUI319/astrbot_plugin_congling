"""用户识别库。

记忆里写着「小张说…」，可小张是谁？换个群名片、改个昵称，人还是那个人。
这张表就是拿来回答「这句话说的是谁」的。

判定优先级：QQ 号 > 别名精确命中 > 字符相似度唯一候选 > 新建。
QQ 号是最硬的证据，其余都是猜，所以猜出来的都带低 confidence。
"""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from .atom import similarity

ALIAS_RATIO = 0.82
MAX_ALIASES = 12


class SubjectBook:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store

    # ------------------------------------------------------------ 识别
    async def identify(
        self,
        *,
        sender_id: str = "",
        sender_name: str = "",
        subject_text: str = "",
    ) -> str:
        """返回这条记忆该挂的主体 key（通常是 QQ 号）。"""
        sid = str(sender_id or "").strip()
        name = str(sender_name or "").strip()
        text = str(subject_text or "").strip()

        if sid:
            await self.ensure(sid, name or text or sid, source="qq")
            if name:
                await self.add_alias(sid, name)
            return sid

        for candidate in (text, name):
            if not candidate:
                continue
            hit = await self.find_by_name(candidate)
            if hit:
                key = str(hit.get("user_id") or "")
                if key:
                    return key

        # 实在认不出来：用文本本身当 key，等以后有 QQ 号了再合并
        key = text or name
        if key:
            await self.ensure(key, key, source="inferred", confidence=0.3)
        return key

    async def ensure(
        self,
        user_id: str,
        display_name: str = "",
        *,
        source: str = "inferred",
        confidence: float = 0.9,
    ) -> dict[str, Any]:
        key = str(user_id or "").strip()
        if not key:
            return {}
        persona = self.config.persona_id
        now = int(time.time())
        row = await self.store.fetch_one(
            "SELECT * FROM mem_subject WHERE persona_id=? AND user_id=?", (persona, key)
        )
        if row:
            if display_name and display_name != row.get("display_name"):
                try:
                    await self.store.execute(
                        "UPDATE mem_subject SET display_name=?, last_seen_at=?, msg_count=msg_count+1 "
                        "WHERE persona_id=? AND user_id=?",
                        (display_name[:40], now, persona, key),
                    )
                except Exception:
                    pass
            else:
                try:
                    await self.store.execute(
                        "UPDATE mem_subject SET last_seen_at=?, msg_count=msg_count+1 "
                        "WHERE persona_id=? AND user_id=?",
                        (now, persona, key),
                    )
                except Exception:
                    pass
            return row

        try:
            await self.store.execute(
                "INSERT INTO mem_subject(persona_id, user_id, display_name, aliases, confidence, "
                "source, first_seen_at, last_seen_at, msg_count) VALUES(?,?,?,?,?,?,?,?,0) "
                "ON CONFLICT(persona_id, user_id) DO NOTHING",
                (
                    persona,
                    key,
                    (display_name or key)[:40],
                    json.dumps([display_name] if display_name else [], ensure_ascii=False),
                    float(confidence),
                    source,
                    now,
                    now,
                ),
            )
        except Exception as exc:
            logger.debug(f"[congling] 建主体失败: {exc!r}")
        return await self.store.fetch_one(
            "SELECT * FROM mem_subject WHERE persona_id=? AND user_id=?", (persona, key)
        ) or {}

    async def add_alias(self, user_id: str, alias: str) -> None:
        key = str(user_id or "").strip()
        name = str(alias or "").strip()[:40]
        if not key or not name:
            return
        row = await self.store.fetch_one(
            "SELECT aliases FROM mem_subject WHERE persona_id=? AND user_id=?",
            (self.config.persona_id, key),
        )
        if not row:
            return
        try:
            aliases = json.loads(row.get("aliases") or "[]")
        except (ValueError, TypeError):
            aliases = []
        if name in aliases:
            return
        aliases.append(name)
        aliases = aliases[-MAX_ALIASES:]
        await self.store.execute(
            "UPDATE mem_subject SET aliases=? WHERE persona_id=? AND user_id=?",
            (json.dumps(aliases, ensure_ascii=False), self.config.persona_id, key),
        )

    async def find_by_name(self, name: str) -> dict[str, Any] | None:
        text = str(name or "").strip()
        if not text:
            return None
        rows = await self.store.fetch_all(
            "SELECT * FROM mem_subject WHERE persona_id=?", (self.config.persona_id,)
        )
        best: dict[str, Any] | None = None
        best_ratio = 0.0
        for row in rows:
            if str(row.get("display_name") or "") == text:
                return row
            try:
                aliases = json.loads(row.get("aliases") or "[]")
            except (ValueError, TypeError):
                aliases = []
            if text in [str(x) for x in aliases]:
                return row
            for candidate in [str(row.get("display_name") or "")] + [str(x) for x in aliases]:
                if not candidate:
                    continue
                ratio = similarity(text, candidate)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best = row
        if best is not None and best_ratio >= ALIAS_RATIO:
            return best
        return None

    # ------------------------------------------------------------ 关联
    async def link_atom(self, atom_id: int, subject_key: str, role: str = "subject") -> None:
        if not atom_id or not subject_key:
            return
        try:
            await self.store.execute(
                "INSERT INTO mem_atom_tag(atom_id, subject_key, role, created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(atom_id, subject_key) DO NOTHING",
                (int(atom_id), str(subject_key), role, int(time.time())),
            )
        except Exception as exc:
            logger.debug(f"[congling] 记忆打标签失败: {exc!r}")

    async def atoms_of(
        self,
        subject_key: str,
        *,
        status: str = "active",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        rows = await self.store.fetch_all(
            "SELECT * FROM mem_atom WHERE persona_id=? AND status=? "
            "AND (subject_id=? OR id IN (SELECT atom_id FROM mem_atom_tag WHERE subject_key=?)) "
            "ORDER BY (COALESCE(decay,1.0) * (1.0 + COALESCE(mem_level,3) * 0.2)) DESC, id DESC LIMIT ?",
            (
                self.config.persona_id,
                status,
                str(subject_key),
                str(subject_key),
                max(1, min(1000, limit)),
            ),
        )
        return rows

    # ------------------------------------------------------------ 列表
    async def list(self, keyword: str = "", limit: int = 100) -> list[dict[str, Any]]:
        persona = self.config.persona_id
        if keyword:
            rows = await self.store.fetch_all(
                "SELECT * FROM mem_subject WHERE persona_id=? AND (display_name LIKE ? OR user_id LIKE ? OR aliases LIKE ?) "
                "ORDER BY last_seen_at DESC LIMIT ?",
                (persona, f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", max(1, min(300, limit))),
            )
        else:
            rows = await self.store.fetch_all(
                "SELECT * FROM mem_subject WHERE persona_id=? ORDER BY last_seen_at DESC LIMIT ?",
                (persona, max(1, min(300, limit))),
            )
        out: list[dict[str, Any]] = []
        for row in rows:
            key = str(row.get("user_id") or "")
            count = 0
            try:
                count = int(
                    await self.store.scalar(
                        "SELECT COUNT(*) FROM mem_atom WHERE persona_id=? AND subject_id=? AND status='active'",
                        (persona, key),
                        0,
                    )
                    or 0
                )
            except Exception:
                count = 0
            item = dict(row)
            item["aliases"] = self._loads(row.get("aliases"), [])
            item["atom_count"] = count
            item["last_text"] = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(int(row.get("last_seen_at") or 0))
            )
            out.append(item)
        return out

    async def display_name(self, subject_key: str) -> str:
        row = await self.store.fetch_one(
            "SELECT display_name FROM mem_subject WHERE persona_id=? AND user_id=?",
            (self.config.persona_id, str(subject_key)),
        )
        if row and row.get("display_name"):
            return str(row["display_name"])
        return str(subject_key)

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
