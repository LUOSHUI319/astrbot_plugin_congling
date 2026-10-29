"""用户识别库。

记忆里写着「小张说…」，可小张是谁？换个群名片、改个昵称，人还是那个人。
这张表就是拿来回答「这句话说的是谁」的。

判定优先级：QQ 号 > 别名精确命中 > 字符相似度唯一候选 > 新建。
QQ 号是最硬的证据，其余都是猜，所以猜出来的都带低 confidence。
"""

from __future__ import annotations

import json
import time
import uuid
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
            "SELECT * FROM mem_atom WHERE persona_id=? AND status=? AND (unread IS NULL OR unread=0) "
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
                        "SELECT COUNT(*) FROM mem_atom WHERE persona_id=? AND subject_id=? AND status='active' "
                        "AND (unread IS NULL OR unread=0)",
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

    # ---------------------------------------------------------------- 修复日志
    async def _ensure_repair_table(self) -> None:
        """合并/修复的前后像。逆转要靠它，不建就只能整库备份回滚。"""
        await self.store.execute(
            "CREATE TABLE IF NOT EXISTS mem_repair_log ("
            " run_id TEXT PRIMARY KEY, kind TEXT, at INTEGER, reversed INTEGER DEFAULT 0, detail TEXT)"
        )

    async def _log_repair(self, run_id: str, kind: str, detail: dict[str, Any]) -> None:
        await self._ensure_repair_table()
        await self.store.execute(
            "INSERT OR REPLACE INTO mem_repair_log (run_id, kind, at, reversed, detail) VALUES (?,?,?,?,?)",
            (run_id, kind, int(time.time()), 0, json.dumps(detail, ensure_ascii=False)),
        )

    async def rollback_merge(self, run_id: str) -> dict[str, Any]:
        """把一次合并整体逆转。之后只要有过新的写入就拒绝，避免把别人的数据带回去。"""
        await self._ensure_repair_table()
        row = await self.store.fetch_one(
            "SELECT kind, at, reversed, detail FROM mem_repair_log WHERE run_id=?", (run_id,)
        )
        if not row:
            return {"ok": False, "error": "找不到这条修复记录"}
        if int(row.get("reversed") or 0):
            return {"ok": True, "already": True, "message": "这条已经逆转过"}
        detail = self._loads(row.get("detail"), {})
        pairs = detail.get("pairs") or []
        if not pairs:
            return {"ok": False, "error": "记录里没有可逆的数据"}

        # 冲突检查：合并之后有没有新记忆落到目标主体上
        since = int(row.get("at") or 0)
        for pair in pairs:
            dst = str(pair.get("to") or "")
            if not dst:
                continue
            hit = await self.store.fetch_one(
                "SELECT COUNT(*) AS c FROM mem_atom WHERE subject_id=? AND created_at>?", (dst, since)
            )
            if int((hit or {}).get("c") or 0) > 0:
                return {
                    "ok": False,
                    "error": f"合并之后「{dst}」上又有新记忆写入，逆转会把它们一起带错，已拒绝",
                }

        persona_id = self.config.persona_id
        await self.store.begin()
        try:
            for pair in pairs:
                src = str(pair.get("from") or "")
                dst = str(pair.get("to") or "")
                atoms = [int(x) for x in (pair.get("atom_ids") or []) if str(x).isdigit()]
                if not atoms:
                    continue
                marks = ",".join("?" * len(atoms))
                await self.store.execute(
                    f"UPDATE mem_atom SET subject_id=? WHERE persona_id=? AND id IN ({marks})",
                    (src, persona_id, *atoms),
                )
                # 主体行还原（当初被删掉的那条 inferred 记录）
                restored = pair.get("subject_row")
                if isinstance(restored, dict) and src:
                    await self.store.execute(
                        "INSERT OR IGNORE INTO mem_subject "
                        "(persona_id, user_id, display_name, aliases, confidence, source, first_seen_at, last_seen_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            persona_id, src, restored.get("display_name") or src,
                            json.dumps(restored.get("aliases") or [], ensure_ascii=False),
                            float(restored.get("confidence") or 0.5), "inferred",
                            int(restored.get("first_seen_at") or 0), int(restored.get("last_seen_at") or 0),
                        ),
                    )
                # 别名回滚：把这次加进去的旧名再摘掉
                if dst:
                    dst_row = await self.store.fetch_one(
                        "SELECT aliases FROM mem_subject WHERE persona_id=? AND user_id=?", (persona_id, dst)
                    )
                    if dst_row:
                        aliases = [a for a in (self._loads(dst_row.get("aliases"), [])) if a != src]
                        await self.store.execute(
                            "UPDATE mem_subject SET aliases=? WHERE persona_id=? AND user_id=?",
                            (json.dumps(aliases, ensure_ascii=False), persona_id, dst),
                        )
            await self.store.execute(
                "UPDATE mem_repair_log SET reversed=1 WHERE run_id=?", (run_id,)
            )
            await self.store.commit()
        except Exception as exc:
            await self.store.rollback()
            logger.warning(f"[congling] 合并逆转失败: {exc!r}")
            return {"ok": False, "error": repr(exc)[:200]}
        return {"ok": True, "reversed": len(pairs)}

    # ---------------------------------------------------------------- 归并
    @staticmethod
    def _name_set(*values: Any) -> set[str]:
        out: set[str] = set()
        for v in values:
            if isinstance(v, (list, tuple, set)):
                for x in v:
                    s = str(x or "").strip()
                    if s:
                        out.add(s)
            else:
                s = str(v or "").strip()
                if s:
                    out.add(s)
        return out

    @staticmethod
    def _match_item(item: dict[str, Any], qq: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
        mine = SubjectBook._name_set(item.get("key"), item.get("name"), item.get("aliases"))
        if not mine:
            return None, ""
        for cand in qq:
            known = SubjectBook._name_set(cand.get("key"), cand.get("name"), cand.get("aliases"))
            hit = mine & known
            if hit:
                return cand, "别名命中：" + "、".join(sorted(hit)[:3])
        return None, ""

    async def merge_candidates(self, limit: int = 120) -> list[dict[str, Any]]:
        """找出可能属于同一个人的主体对。

        来源有两类：主体表里 source != 'qq' 的行，以及"业务表里有、主体表里没有"
        的孤儿键 —— 后者最容易漏，`affinity` 里可能还挂着一条高分。
        """
        rows = await self.store.fetch_all(
            "SELECT user_id, display_name, aliases, source FROM mem_subject"
        )
        subs: list[dict[str, Any]] = []
        for r in rows:
            subs.append(
                {
                    "key": str(r.get("user_id") or ""),
                    "name": str(r.get("display_name") or ""),
                    "aliases": self._loads(r.get("aliases"), []),
                    "source": str(r.get("source") or ""),
                }
            )
        qq = [x for x in subs if x["source"] == "qq"]
        others = [x for x in subs if x["source"] != "qq"]

        known = {x["key"] for x in subs}
        orphans: set[str] = set()
        for table, col in (("mem_atom", "subject_id"), ("affinity", "subject_key"),
                           ("mem_atom_tag", "subject_key")):
            try:
                for r in await self.store.fetch_all(
                    f"SELECT DISTINCT {col} AS k FROM {table} WHERE {col} IS NOT NULL AND {col}<>''"
                ):
                    k = str(r.get("k") or "")
                    if k and k not in known:
                        orphans.add(k)
            except Exception:
                continue
        for k in orphans:
            others.append({"key": k, "name": k, "aliases": [], "source": "orphan"})

        out: list[dict[str, Any]] = []
        for item in others:
            cand, why = self._match_item(item, qq)
            if cand is None:
                continue
            row = await self.store.fetch_one(
                "SELECT COUNT(*) AS c FROM mem_atom WHERE status='active' AND subject_id=?",
                (item["key"],),
            )
            out.append(
                {
                    "from": item["key"],
                    "to": cand["key"],
                    "to_name": cand["name"],
                    "why": why,
                    "atoms": int((row or {}).get("c") or 0),
                    "source": item["source"],
                }
            )
        out.sort(key=lambda x: -int(x["atoms"]))
        return out[:limit]

    async def _merge_one(self, persona_id: str, src: str, dst: str) -> tuple[int, list[int], dict]:
        """把 src 并进 dst。返回 (条数, 迁走的 atom id, 源主体行快照)，逆转要靠后两个。"""
        rows = await self.store.fetch_all(
            "SELECT id FROM mem_atom WHERE persona_id=? AND subject_id=?", (persona_id, src)
        )
        atom_ids = [int(r.get("id") or 0) for r in rows if r.get("id")]
        src_row = await self.store.fetch_one(
            "SELECT display_name, aliases, confidence, first_seen_at, last_seen_at "
            "FROM mem_subject WHERE persona_id=? AND user_id=?",
            (persona_id, src),
        )
        cur = await self.store.execute(
            "UPDATE mem_atom SET subject_id=? WHERE persona_id=? AND subject_id=?", (dst, persona_id, src)
        )
        moved = int(cur or 0)
        await self.store.execute(
            "INSERT OR IGNORE INTO mem_atom_tag (atom_id, subject_key, role, created_at) "
            "SELECT atom_id, ?, role, created_at FROM mem_atom_tag WHERE subject_key=?",
            (dst, src),
        )
        await self.store.execute("DELETE FROM mem_atom_tag WHERE subject_key=?", (src,))

        cols = "score, stage, delta_today, floor, last_talk_at, talk_days_30, manual"
        a_src = await self.store.fetch_one(
            f"SELECT {cols} FROM affinity WHERE persona_id=? AND subject_key=?", (persona_id, src)
        )
        a_dst = await self.store.fetch_one(
            f"SELECT {cols} FROM affinity WHERE persona_id=? AND subject_key=?", (persona_id, dst)
        )
        now = int(time.time())
        if a_src and a_dst:
            merged = (
                max(float(a_src.get("score") or 0), float(a_dst.get("score") or 0)),
                max(int(a_src.get("stage") or 0), int(a_dst.get("stage") or 0)),
                max(float(a_src.get("delta_today") or 0), float(a_dst.get("delta_today") or 0)),
                max(float(a_src.get("floor") or 0), float(a_dst.get("floor") or 0)),
                max(int(a_src.get("last_talk_at") or 0), int(a_dst.get("last_talk_at") or 0)),
                max(int(a_src.get("talk_days_30") or 0), int(a_dst.get("talk_days_30") or 0)),
                max(int(a_src.get("manual") or 0), int(a_dst.get("manual") or 0)),
            )
            await self.store.execute(
                "UPDATE affinity SET score=?, stage=?, delta_today=?, floor=?, last_talk_at=?, "
                "talk_days_30=?, manual=?, updated_at=? WHERE persona_id=? AND subject_key=?",
                (*merged, now, persona_id, dst),
            )
            await self.store.execute(
                "DELETE FROM affinity WHERE persona_id=? AND subject_key=?", (persona_id, src)
            )
        elif a_src:
            await self.store.execute(
                "UPDATE affinity SET subject_key=?, updated_at=? WHERE persona_id=? AND subject_key=?",
                (dst, now, persona_id, src),
            )

        p_src = await self.store.fetch_one(
            "SELECT source_atom_ids FROM mem_profile WHERE persona_id=? AND subject_key=?", (persona_id, src)
        )
        p_dst = await self.store.fetch_one(
            "SELECT source_atom_ids FROM mem_profile WHERE persona_id=? AND subject_key=?", (persona_id, dst)
        )
        if p_src and p_dst:
            left = self._loads(p_src.get("source_atom_ids"), [])
            right = self._loads(p_dst.get("source_atom_ids"), [])
            merged_ids = sorted({int(x) for x in list(left) + list(right) if str(x).isdigit()})
            await self.store.execute(
                "UPDATE mem_profile SET source_atom_ids=?, updated_at=? WHERE persona_id=? AND subject_key=?",
                (json.dumps(merged_ids), now, persona_id, dst),
            )
            await self.store.execute(
                "DELETE FROM mem_profile WHERE persona_id=? AND subject_key=?", (persona_id, src)
            )
        elif p_src:
            await self.store.execute(
                "UPDATE mem_profile SET subject_key=? WHERE persona_id=? AND subject_key=?",
                (dst, persona_id, src),
            )

        await self.store.execute(
            "UPDATE mem_diary SET affinity_subject=? WHERE affinity_subject=?", (dst, src)
        )
        await self.store.execute("UPDATE mem_edge SET src=? WHERE src=?", (dst, src))
        await self.store.execute("UPDATE mem_edge SET dst=? WHERE dst=?", (dst, src))

        row_dst = await self.store.fetch_one(
            "SELECT aliases, display_name, user_id FROM mem_subject WHERE persona_id=? AND user_id=?",
            (persona_id, dst),
        )
        row_src = await self.store.fetch_one(
            "SELECT aliases, display_name, user_id FROM mem_subject WHERE persona_id=? AND user_id=?",
            (persona_id, src),
        )
        if row_dst and row_src:
            merged_alias: list[str] = []
            for name in (
                list(self._loads(row_dst.get("aliases"), []))
                + list(self._loads(row_src.get("aliases"), []))
                + [row_src.get("display_name"), row_src.get("user_id")]
            ):
                s = str(name or "").strip()
                if s and s not in merged_alias:
                    merged_alias.append(s)
            await self.store.execute(
                "UPDATE mem_subject SET aliases=?, last_seen_at=? WHERE persona_id=? AND user_id=?",
                (json.dumps(merged_alias, ensure_ascii=False), now, persona_id, dst),
            )
            await self.store.execute(
                "DELETE FROM mem_subject WHERE persona_id=? AND user_id=?", (persona_id, src)
            )
        return moved, atom_ids, (dict(src_row) if src_row else {})

    async def merge_subjects(self, pairs: list[dict[str, Any]]) -> dict[str, Any]:
        """按用户勾选执行合并。整批一个事务，中途失败全部回滚。"""
        clean: list[tuple[str, str]] = []
        for pair in pairs or []:
            src = str(pair.get("from") or "").strip()
            dst = str(pair.get("to") or "").strip()
            if src and dst and src != dst:
                clean.append((src, dst))
        if not clean:
            return {"ok": False, "error": "没有要合并的项"}

        persona_id = self.config.persona_id
        moved = 0
        done = 0
        pairs_log: list[dict[str, Any]] = []
        run_id = uuid.uuid4().hex[:12]
        await self._ensure_repair_table()
        await self.store.begin()
        try:
            for src, dst in clean:
                cnt, atom_ids, src_row = await self._merge_one(persona_id, src, dst)
                moved += cnt
                done += 1
                pairs_log.append(
                    {"from": src, "to": dst, "atom_ids": atom_ids, "subject_row": src_row}
                )
            await self._log_repair(run_id, "merge", {"pairs": pairs_log})
            await self.store.commit()
        except Exception as exc:
            await self.store.rollback()
            logger.warning(f"[congling] 主体合并失败已回滚: {exc!r}")
            return {"ok": False, "error": repr(exc)[:200]}
        logger.info(f"[congling] 主体合并完成：{done} 组，迁移记忆 {moved} 条（run_id={run_id}）")
        return {"ok": True, "merged": done, "atoms": moved, "run_id": run_id}

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
