"""记忆服务门面。

模块之间只认这个类：感知层把消息交给它、拟人层向它要关系、UI 向它要列表。
内部再分给 extractor / retriever / atom store / maintenance。

一条对话的完整路径：
  注入前召回（inject） → 对话进行中缓存（observe_response）
  → 一轮结束回写（after_sent） → 静默触发抽取（_extract_session）
  → 每日衰减与反思（daily_tick）
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

from astrbot.api import logger

from ..core.budget import BudgetSlice
from ..core.contracts import MemoryDraft, MemoryHit, PerceivedMessage
from .atom import AtomStore
from .extractor import MemoryExtractor
from .retrieve import Retriever

MARKER = "<!--congling:memory-->"
MAX_BUFFER_TURNS = 60


class MemoryService:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self.atoms = AtomStore(plugin)
        self.retriever = Retriever(plugin, self.atoms)
        self.extractor = MemoryExtractor(plugin)
        self._buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._last_activity: dict[str, float] = {}
        self._extracting: set[str] = set()
        self._task: asyncio.Task | None = None
        self._stopped = False

    # ================================================================ 生命周期
    async def start(self) -> None:
        self.plugin.bus.subscribe(self.plugin.bus.ON_PERCEIVED, self._on_perceived)
        self._stopped = False
        self._task = asyncio.get_running_loop().create_task(self._scheduler())
        try:
            await self.store.kv_set("memory:started_at", int(time.time()))
        except Exception:
            pass
        logger.info("[congling] 记忆模块就绪")

    async def flush(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        # 内存缓冲已经不作数了，卸载前把库里所有未消池的会话各抽一次
        try:
            rows = await self.store.fetch_all(
                "SELECT DISTINCT m.session_id AS sid FROM msg_archive m "
                "WHERE m.id > COALESCE((SELECT c.last_msg_row FROM inject_cursor c "
                "WHERE c.session_id = m.session_id), 0) AND m.text IS NOT NULL AND m.text <> ''"
            )
            for row in rows:
                session_id = str(row.get("sid") or "")
                if session_id:
                    await self._extract_session(session_id, force=True)
        except Exception as exc:
            logger.debug(f"[congling] 卸载前消池失败: {exc!r}")

    async def _scheduler(self) -> None:
        while not self._stopped:
            try:
                await self._sweep()
                try:
                    await self.store.kv_set("memory:tick", await self._tick_snapshot())
                except Exception:
                    pass
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug(f"[congling] 记忆调度异常: {exc!r}")
                await asyncio.sleep(30)

    async def _sweep(self) -> None:
        """扫所有会话的消息池：攒满就消池，安静得够久也消一次。

        池子不是内存 list，而是 msg_archive 里 id 大于水位线的那批行，
        所以重启不丢、也不重复存。
        """
        try:
            batch = max(4, self.config.int("memory.batch_extract_size", 20))
            idle = self.config.int("memory.idle_extract_seconds", 120)
            min_turns = max(2, self.config.int("memory.min_extract_turns", 4))
        except Exception:
            batch, idle, min_turns = 20, 120, 4
        try:
            rows = await self.store.fetch_all(
                "SELECT m.session_id AS sid, COUNT(*) AS cnt, MAX(m.ts) AS last_ts "
                "FROM msg_archive m "
                "WHERE m.id > COALESCE((SELECT c.last_msg_row FROM inject_cursor c "
                "WHERE c.session_id = m.session_id), 0) "
                "AND m.text IS NOT NULL AND m.text <> '' "
                "GROUP BY m.session_id"
            )
        except Exception as exc:
            logger.debug(f"[congling] 扫描消息池失败: {exc!r}")
            return
        now = time.time()
        for row in rows:
            session_id = str(row.get("sid") or "")
            if not session_id:
                continue
            count = int(row.get("cnt") or 0)
            quiet = now - int(row.get("last_ts") or now)
            if count >= batch or (quiet >= idle and count >= min_turns):
                await self._extract_session(session_id)

    async def _tick_snapshot(self) -> dict[str, Any]:
        rows = await self.store.fetch_all(
            "SELECT m.session_id AS sid, COUNT(*) AS cnt FROM msg_archive m "
            "WHERE m.id > COALESCE((SELECT c.last_msg_row FROM inject_cursor c "
            "WHERE c.session_id = m.session_id), 0) "
            "AND m.text IS NOT NULL AND m.text <> '' GROUP BY m.session_id"
        )
        return {
            "at": int(time.time()),
            "pool": {str(row["sid"]): int(row["cnt"]) for row in rows},
        }

    # ------------------------------------------------------------ 会话水位
    async def _water(self, session_id: str) -> int:
        row = await self.store.fetch_one(
            "SELECT last_msg_row FROM inject_cursor WHERE session_id=?", (session_id,)
        )
        return int(row.get("last_msg_row") or 0) if row else 0

    async def _set_water(self, session_id: str, row_id: int) -> None:
        await self.store.execute(
            "INSERT INTO inject_cursor(session_id, last_msg_row, last_atom_ids, updated_at) "
            "VALUES(?,?,'[]',?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "last_msg_row=excluded.last_msg_row, updated_at=excluded.updated_at",
            (session_id, int(row_id), int(time.time())),
        )

    async def _pending_rows(self, session_id: str) -> list[dict[str, Any]]:
        """该会话还没消池的消息，按时间正序。"""
        water = await self._water(session_id)
        rows = await self.store.fetch_all(
            "SELECT id, sender_id, sender_name, text, ts FROM msg_archive "
            "WHERE session_id=? AND id>? AND text IS NOT NULL AND text<>'' "
            "ORDER BY id ASC LIMIT 120",
            (session_id, water),
        )
        return rows

    async def _scope_from_archive(self, session_id: str) -> tuple[str, str]:
        row = await self.store.fetch_one(
            "SELECT session_kind, sender_id FROM msg_archive "
            "WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if not row:
            return "user", session_id
        if str(row.get("session_kind") or "") == "group":
            return "group", session_id
        return "user", str(row.get("sender_id") or session_id)

    # ================================================================ 档位
    def tier(self) -> str:
        """当前记忆档位：vector（有向量模型）/ llm（只有 chat 模型）/ keyword（都没有）。"""
        configured = str(self.config.str("memory.tier", "auto") or "auto").strip().lower()
        if configured in {"vector", "llm", "keyword"}:
            return configured
        try:
            if self.retriever.embedding_available():
                return "vector"
        except Exception:
            pass
        return "llm" if self._has_chat_provider() else "keyword"

    def _has_chat_provider(self) -> bool:
        try:
            provider_id = str(self.config.str("memory.llm_provider_id", "") or "")
            if provider_id and self.plugin.context.get_provider_by_id(provider_id) is not None:
                return True
            return self.plugin.context.get_using_provider() is not None
        except Exception:
            return False

    # ================================================================ 注入
    async def inject(self, event: Any, req: Any) -> None:
        if self.plugin.probe.should_yield("memory") and self.config.bool("compat.yield_memory", True):
            logger.debug("[congling] 记忆已让位给外部插件")
            return

        msg: PerceivedMessage | None = getattr(self.plugin, "_last_perceived", None)
        if msg is None:
            return

        query = (msg.text or "").strip()
        if not query:
            return

        await self._buffer_user(msg)

        # 会话身份以事件为准，别用全局 _last_perceived
        ev_session, ev_kind, ev_sender = self._session_of(event)
        if ev_session:
            if ev_kind == "group":
                scope, subject = "group", ev_session
            else:
                scope, subject = "user", ev_sender or ev_session
        else:
            scope, subject = self._scope_of(msg)
        cursor = await self.retriever.cursor(msg.session_id)
        recent_ids = set()
        if cursor.get("last_atom_ids"):
            import json

            try:
                recent_ids = {int(x) for x in json.loads(cursor["last_atom_ids"])}
            except (ValueError, TypeError):
                recent_ids = set()

        try:
            top_k = int(self.config.int("memory.inject_top_k", 5))
        except (TypeError, ValueError):
            top_k = 5
        try:
            hits = await self.retriever.search(
                query, scope=scope, subject=subject, exclude=recent_ids, k=top_k
            )
        except Exception as exc:
            logger.debug(f"[congling] 记忆召回失败: {exc!r}")
            return
        # 当前会话不够时跨会话补齐：混着查，但每条都带 [私聊 望山] 这种来源标签
        if len(hits) < top_k and self.config.bool("memory.cross_session_recall", True):
            try:
                seen = {hit.atom_id for hit in hits}
                extra = await self.retriever.search(
                    query,
                    scope=scope,
                    subject=subject,
                    exclude=recent_ids | seen,
                    k=top_k - len(hits),
                    cross_session=True,
                )
                hits = hits + [hit for hit in extra if hit.atom_id not in seen]
            except Exception as exc:
                logger.debug(f"[congling] 跨会话召回失败: {exc!r}")
        if not hits:
            return

        # 预算裁剪：记忆最多占一定比例
        ratio = self.config.float("memory.inject_budget_ratio", 0.25)
        limit = max(200, int(4000 * max(0.05, min(ratio, 0.6))))
        slices = [
            BudgetSlice(name=f"hit:{hit.atom_id}", priority=index, text=hit.line())
            for index, hit in enumerate(hits)
        ]
        packed = self.plugin.budget.pack(slices, limit)
        if not packed.kept:
            return

        body = "\n".join(item.text for item in packed.kept)
        block = (
            f"\n\n{MARKER}\n"
            "【长期记忆 · 仅供参考；与当前对话冲突时以当前对话为准】\n"
            f"{body}\n"
            "【记忆结束】"
        )
        existing = getattr(req, "system_prompt", "") or ""
        if MARKER in existing:
            # 已经注入过：替换旧块，避免叠加
            head = existing.split(MARKER)[0]
            req.system_prompt = head + block
        else:
            req.system_prompt = existing + block

        hit_ids = [
            hit.atom_id
            for hit in hits
            if any(item.name == f"hit:{hit.atom_id}" for item in packed.kept)
        ]
        try:
            await self.atoms.mark_hit(hit_ids)
            await self.retriever.save_cursor(ev_session or msg.session_id, hit_ids)
        except Exception:
            pass

    # ================================================================ 观察
    async def observe_response(self, event: Any, resp: Any) -> None:
        try:
            text = str(getattr(resp, "completion_text", "") or "").strip()
        except Exception:
            text = ""
        if not text:
            return
        session_id, _kind, _sender = self._session_of(event)
        if not session_id:
            msg: PerceivedMessage | None = getattr(self.plugin, "_last_perceived", None)
            if msg is None:
                return
            session_id = msg.session_id
        buffer = self._buffers[session_id]
        buffer.append(
            {
                "role": "assistant",
                "text": text,
                "ts": int(time.time()),
                "idx": len(buffer),
            }
        )
        self._last_activity[session_id] = time.time()

    async def after_sent(self, event: Any) -> None:
        session_id, kind, sender = self._session_of(event)
        msg: PerceivedMessage | None = getattr(self.plugin, "_last_perceived", None)
        if not session_id:
            if msg is None:
                return
            session_id, kind = msg.session_id, msg.session_kind
        self._last_activity[session_id] = time.time()
        if msg is not None and msg.session_id == session_id:
            await self._touch_relation(msg)

        # 池子在库里，按未消费条数判断，不看内存缓冲
        try:
            batch_size = max(4, self.config.int("memory.batch_extract_size", 20))
            pending = await self._pending_rows(session_id)
            if len(pending) >= batch_size:
                await self._extract_session(session_id)
        except Exception as exc:
            logger.debug(f"[congling] 满池判定失败: {exc!r}")

    def _session_of(self, event: Any) -> tuple[str, str, str]:
        """从事件本身取会话信息。

        不能再用 plugin._last_perceived：那是全局"最后一条被感知的消息"，
        群聊活跃时会把私聊的回复塞进群的缓冲，抽出来的记忆就串了。
        """
        try:
            group_id = str(event.get_group_id() or "")
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            return "", "private", ""
        if group_id:
            return group_id, "group", sender_id
        return sender_id, "private", sender_id

    async def _buffer_user(self, msg: PerceivedMessage) -> None:
        buffer = self._buffers[msg.session_id]
        if buffer and buffer[-1].get("text") == msg.text and buffer[-1].get("role") == "user":
            return
        buffer.append(
            {
                "role": "user",
                "text": msg.text,
                "ts": msg.ts,
                "idx": len(buffer),
                "msg_id": msg.msg_id,
                "session_kind": msg.session_kind,
                "sender_id": msg.sender_id,
                "sender_name": getattr(msg, "sender_name", "") or "",
            }
        )
        while len(buffer) > MAX_BUFFER_TURNS:
            buffer.pop(0)
        self._last_activity[msg.session_id] = time.time()

    async def _on_perceived(self, msg: PerceivedMessage) -> None:
        # 群聊里非触发消息也留一份，便于「刚才那个」这类追问
        if not msg.text:
            return
        # 所有被感知到的消息都进缓冲（按 session 分开），
        # 不能只在 LLM 请求里缓冲，否则群聊闲聊永远抽不出记忆。
        await self._buffer_user(msg)
        try:
            await self.store.execute(
                "INSERT INTO inject_cursor(session_id, last_msg_row, last_atom_ids, updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at",
                (msg.session_id, 0, "[]", int(time.time())),
            )
        except Exception:
            pass

    # ================================================================ 抽取
    async def _extract_session(self, session_id: str, force: bool = False) -> None:
        """消池：把该会话未消费的消息整批交给模型压成事实。

        顺序是「先抽、抽成功才推进水位」，失败不动水位，下轮自动重试。
        """
        if session_id in self._extracting:
            return
        try:
            rows = await self._pending_rows(session_id)
        except Exception as exc:
            logger.debug(f"[congling] 读取消息池失败: {exc!r}")
            return
        if not rows:
            return
        try:
            min_turns = max(2, self.config.int("memory.min_extract_turns", 4))
        except (TypeError, ValueError):
            min_turns = 4
        if not force and len(rows) < min_turns:
            return

        self._extracting.add(session_id)
        turns = [
            {
                "role": "user",
                "text": str(row.get("text") or ""),
                "ts": int(row.get("ts") or 0),
                "idx": index,
                "sender_id": str(row.get("sender_id") or ""),
                "sender_name": str(row.get("sender_name") or ""),
            }
            for index, row in enumerate(rows)
        ]
        max_row = max(int(row["id"]) for row in rows)
        try:
            drafts = await self.extractor.extract(turns)
            ok = bool(getattr(self.extractor, "last_ok", True))
            try:
                await self.store.kv_set(
                    "memory:last_extract",
                    {
                        "at": int(time.time()),
                        "session": session_id,
                        "turns": len(turns),
                        "drafts": len(drafts),
                        "ok": ok,
                        "water": max_row,
                    },
                )
            except Exception:
                pass
            if drafts:
                scope, subject = await self._scope_from_archive(session_id)
                kind, label = await self._label_of_session(session_id)
                for draft in drafts:
                    draft.session_id = str(session_id or "")
                    draft.session_kind = kind
                    draft.session_label = label
                ids = await self.atoms.add_many(
                    drafts, scope=scope, subject=subject, persona_id=self.config.persona_id
                )
                if ids:
                    await self.plugin.bus.emit(self.plugin.bus.ON_MEMORY_WRITTEN, drafts)
                    logger.debug(f"[congling] 写入 {len(ids)} 条记忆（{session_id}）")
            # 只有解析成功才推水位：模型返回废话或调用失败，下一轮还要重试
            if ok:
                await self._set_water(session_id, max_row)
        except Exception as exc:
            logger.debug(f"[congling] 记忆抽取失败: {exc!r}")
            try:
                await self.store.kv_set(
                    "memory:last_error",
                    {"at": int(time.time()), "session": session_id, "error": repr(exc)[:400]},
                )
            except Exception:
                pass
        finally:
            self._extracting.discard(session_id)

    # ================================================================ 对外 API
    async def write_text(
        self,
        text: str,
        *,
        scope: str = "user",
        subject: str = "",
        kind: str = "fact",
        importance: float = 0.8,
    ) -> int:
        draft = MemoryDraft(kind=kind, text=text, importance=importance, confidence=1.0)
        return await self.atoms.add(
            draft, scope=scope, subject=subject, persona_id=self.config.persona_id
        )

    async def recall(
        self,
        query: str,
        *,
        scope: str = "user",
        subject: str = "",
        k: int = 5,
        cross_session: bool = False,
    ) -> list[MemoryHit]:
        return await self.retriever.search(
            query, scope=scope, subject=subject, k=k, cross_session=cross_session
        )

    async def forget(self, keyword: str, *, scope: str | None = None, subject: str | None = None) -> int:
        rows = await self.atoms.list(scope=scope, subject=subject, keyword=keyword, limit=50)
        for row in rows:
            await self.atoms.set_status(int(row["id"]), "forgotten")
        return len(rows)

    async def relation(self, user_id: str) -> dict[str, Any]:
        row = await self.store.fetch_one(
            "SELECT * FROM persona_state WHERE persona_id=? AND scope='user' AND subject_id=?",
            (self.config.persona_id, str(user_id)),
        )
        return row or {}

    async def _touch_relation(self, msg: PerceivedMessage) -> None:
        if msg.session_kind != "private":
            return
        try:
            row = await self.relation(msg.sender_id)
            now = int(time.time())
            if not row:
                await self.store.execute(
                    "INSERT INTO persona_state(persona_id, scope, subject_id, intimacy, mood, energy, last_active) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (self.config.persona_id, "user", msg.sender_id, 0.12, "calm", 0.7, now),
                )
                return
            intimacy = min(1.0, float(row.get("intimacy") or 0.1) + 0.005)
            await self.store.execute(
                "UPDATE persona_state SET intimacy=?, last_active=? WHERE persona_id=? AND scope='user' AND subject_id=?",
                (intimacy, now, self.config.persona_id, msg.sender_id),
            )
        except Exception as exc:
            logger.debug(f"[congling] 关系值更新失败: {exc!r}")

    # ================================================================ 维护
    async def daily_tick(self) -> None:
        from .maintenance import Maintenance

        await Maintenance(self.plugin, self.atoms, self.retriever).run_daily()
        pending = await self.retriever.embed_pending(30)
        if pending:
            logger.debug(f"[congling] 补齐 {pending} 条记忆向量")

    # ================================================================ 工具
    def _scope_of(self, msg: PerceivedMessage) -> tuple[str, str]:
        if msg.session_kind == "group":
            return "group", msg.session_id
        return "user", msg.sender_id

    async def _label_of_session(self, session_id: str) -> tuple[str, str]:
        """返回 (session_kind, 人话标签)，标签用于回答时标明出处。

        从库里取而不是从内存缓冲取：重启后缓冲是空的，标签就没了。
        """
        row = await self.store.fetch_one(
            "SELECT session_kind, sender_name FROM msg_archive "
            "WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if not row:
            return "private", f"私聊 {session_id}"
        if str(row.get("session_kind") or "") == "group":
            return "group", f"群 {session_id}"
        name = str(row.get("sender_name") or "") or str(session_id)
        return "private", f"私聊 {name}"

    def _scope_of_session(self, session_id: str) -> tuple[str, str]:
        """从 session_id 反推 scope：群号一般较短且以数字开头，但不可靠，
        所以优先用缓冲里最后一条消息的真实类型。"""
        buffer = self._buffers.get(session_id) or []
        for turn in reversed(buffer):
            role = turn.get("role")
            if role == "user" and turn.get("session_kind"):
                if turn["session_kind"] == "group":
                    return "group", session_id
                return "user", str(turn.get("sender_id") or session_id)
        return "user", session_id
