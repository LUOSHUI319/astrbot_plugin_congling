"""自身行为账本。动作成功后登记，再在同一事务生成唯一的自述记忆。

planned/failed/expired 仅是显式账本状态，不是成功经历；四条动作链只在成功后
调用 record(status="succeeded")。外部发布/发送不可能与 SQLite 构成分布式事务。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .atom import norm_hash
from .grade import STRENGTH, next_review_at
from .refs import actor_ref, event_key, session_ref

EVENT_TYPES = {"diary", "dream", "qzone", "proactive"}
PRIVATE_AUDIENCE = "owner:__private__"


class EventLedger:
    def __init__(self, plugin: Any) -> None:
        self.store = plugin.store
        self.config = plugin.cfg

    async def record(self, event_type: str, source_id: str, text: str = "", **kwargs) -> int:
        return await self.store.transaction(self.prepare_record(event_type, source_id, text, **kwargs))

    def prepare_record(self, event_type: str, source_id: str, text: str = "", *,
                     status: str = "succeeded", session_id: str = "", session_kind: str = "",
                     platform: str = "qq", private_text: str = "", occurred_at: int | None = None,
                     result: dict[str, Any] | None = None):
        """返回同步事务回调，允许梦的发放状态与账本/记忆一起原子提交。"""
        if event_type not in EVENT_TYPES or not str(source_id).strip():
            raise ValueError("事件类型或来源 ID 无效")
        if status not in {"planned", "succeeded", "failed", "expired"}:
            raise ValueError("事件状态无效")
        if status == "succeeded" and not text.strip():
            raise ValueError("成功事件必须有可回忆的结果")
        if session_kind not in {"", "owner", "private", "group"} or (session_kind and not session_id):
            raise ValueError("事件受众无效")
        if session_id and not session_kind:
            raise ValueError("有会话号必须指定会话类型")
        if session_kind == "owner" and session_id != "__private__":
            raise ValueError("私密正文只能属于 owner:__private__")
        persona = self.config.persona_id
        key = event_key(event_type, source_id)
        now = int(time.time())
        happened = now if occurred_at is None else int(occurred_at)
        who = actor_ref(persona_id=persona)
        where = session_ref(platform, session_kind, session_id)
        audience = PRIVATE_AUDIENCE if private_text or session_kind == "owner" else (
            where if session_id else "public")
        public_text = text if private_text else ""
        body = private_text or text
        old_audience = PRIVATE_AUDIENCE if audience == PRIVATE_AUDIENCE else (
            f"{session_kind}:{session_id}" if session_id else "")

        def write(conn):
            old = conn.execute("SELECT * FROM event_ledger WHERE persona_id=? AND event_key=?",
                               (persona, key)).fetchone()
            if old and old["status"] != "planned":
                if old["status"] != status:
                    raise ValueError("终态不可重写；新动作必须用新幂等键")
                return int(old["memory_id"] or 0)
            if old and status == "planned":
                return 0
            eid = old["event_id"] if old else uuid.uuid4().hex
            if old and (old["event_type"] != event_type or old["session_ref"] != where
                        or old["audience"] != audience):
                raise ValueError("事件受众和身份不可变更")
            # 先入账本。后续任何 SQL 失败，连这一步一起回滚。
            if not old:
                conn.execute(
                    "INSERT INTO event_ledger(event_id,persona_id,event_key,event_type,subject_ref,actor_ref,"
                    "session_ref,audience,occurred_at,recorded_at,status,result_ref,result_text,result_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, persona, key, event_type, who, who, where, audience, happened, now, status,
                     str(source_id), body, json.dumps(result or {}, ensure_ascii=False)))
            else:
                conn.execute("UPDATE event_ledger SET status=?,occurred_at=?,result_text=?,result_json=? WHERE event_id=?",
                             (status, happened, body, json.dumps(result or {}, ensure_ascii=False), eid))
            if status != "succeeded":
                return 0
            cur = conn.execute(
                "INSERT INTO mem_atom(persona_id,scope,subject_id,kind,text,norm_hash,keywords,"
                "importance,confidence,source_msg_ids,created_at,decay,status,unread,unread_since,"
                "session_id,session_kind,mem_level,level_source,level_reason,strength,next_review_at,"
                "subject_ref,actor_ref,session_ref,ref_status,event_id,event_type,event_key,audience,public_text) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (persona, "self", old_audience, "event", body, norm_hash(body), json.dumps([event_type]),
                 0.6, 1.0, json.dumps([f"action:{event_type}:{source_id}"]), happened, 1.0, "active", 0, 0,
                 session_id, session_kind, 3, "rule", "已成功完成的自身行为", STRENGTH[3],
                 next_review_at(3, happened), who, who, where, "resolved", eid, event_type, key, audience, public_text))
            atom_id = int(cur.lastrowid)
            conn.execute("UPDATE event_ledger SET memory_id=? WHERE event_id=?", (atom_id, eid))
            return atom_id

        return write
