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

from ..core.prompt_parts import add_temp_part

from ..core.budget import BudgetSlice
from ..core.contracts import MemoryDraft, MemoryHit, PerceivedMessage
from .affinity import Affinity
from .atom import AtomStore, norm_hash
from .extractor import MemoryExtractor
from .grade import LevelJudge
from .profile import ProfileBook
from .retrieve import Retriever
from .subject import SubjectBook

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
        # 0.5.0：等级判定 / 用户识别库 / 好感度 / 画像，四件套都挂在门面上
        self.grades = LevelJudge(plugin)
        self.subjects = SubjectBook(plugin)
        self.affinity = Affinity(plugin)
        self.profiles = ProfileBook(plugin, self.subjects, self.affinity)
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

        # 先看事件自己带着的消息，再退回全局变量。
        # 全局变量是「最后一次被感知的消息」，两个会话并发时 A 会用 B 的话去搜。
        msg: PerceivedMessage | None = getattr(event, "_congling_msg", None)
        if msg is None:
            msg = getattr(self.plugin, "_last_perceived", None)
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
        # 跨会话档位：管理员和普通用户分开配。
        # 「管理员 → 全库」这句话里藏着危险：全库包含别人的私聊。
        # self 是推荐默认 —— 管理员能记得自己参与过的所有会话，但不碰别人的私聊。
        is_admin = False
        try:
            checker = getattr(self.plugin, "is_admin", None)
            if callable(checker):
                is_admin = bool(checker(event))
        except Exception:
            is_admin = False
        if is_admin:
            cs_mode = str(self.config.str("memory.cross_session_admin_mode", "self") or "self")
        else:
            cs_mode = str(self.config.str("memory.cross_session_scope", "same_user") or "same_user")
        cs_mode = cs_mode.strip().lower()
        if cs_mode == "self":
            cs_mode = "same_user"
        if getattr(self.plugin, "diag", None) is not None:
            await self.plugin.diag.trace("cross_session", admin=is_admin, mode=cs_mode)

        if len(hits) < top_k and cs_mode != "off" and self.config.bool("memory.cross_session_recall", True):
            try:
                seen = {hit.atom_id for hit in hits}
                extra = await self.retriever.search(
                    query,
                    scope=scope,
                    subject=subject,
                    exclude=recent_ids | seen,
                    k=top_k - len(hits),
                    cross_session=True,
                    mode=cs_mode,
                )
                hits = hits + [hit for hit in extra if hit.atom_id not in seen]
            except Exception as exc:
                logger.debug(f"[congling] 跨会话召回失败: {exc!r}")
        # 关系远近和已有画像：没有记忆命中时也要注入，这两块属于「我怎么看 TA」
        speaker_key = str(ev_sender or (subject if scope == "user" else "") or "")
        extras: list[str] = []
        if speaker_key:
            try:
                aff = await self.affinity.prompt_hint(speaker_key)
                if aff:
                    extras.append(aff)
            except Exception as exc:
                logger.debug(f"[congling] 好感度注入失败: {exc!r}")
            # 受众门禁：画像只在这个人的私聊里注入。
            # 群聊里注进去，她就会当着一屋子人说出"我对你这个人的印象"，
            # 甚至是别处攒下的私密认识 —— 你库里群聊记忆占 98%，这个口子必须堵。
            inject_profile = True
            try:
                session_kind = str(getattr(msg, "session_kind", "") or "")
                if session_kind == "group":
                    inject_profile = bool(self.config.bool("memory.profile_inject_in_group", False))
                elif str(getattr(msg, "sender_id", "") or "") != str(speaker_key or ""):
                    # 私聊但主体对不上说话人（比如在群里聊第三人），不注入
                    inject_profile = False
            except Exception:
                inject_profile = False
            if inject_profile:
                try:
                    prof = await self.profiles.prompt_hint(speaker_key)
                    if prof:
                        extras.append(f"你对 TA 已有的认识：{prof}")
                except Exception as exc:
                    logger.debug(f"[congling] 画像注入失败: {exc!r}")
            if getattr(self.plugin, "diag", None) is not None:
                await self.plugin.diag.trace(
                    "profile_gate",
                    kind=str(getattr(msg, "session_kind", "")),
                    injected=inject_profile,
                )

        if not hits and not extras:
            return

        # 预算裁剪：记忆最多占一定比例
        ratio = self.config.float("memory.inject_budget_ratio", 0.25)
        limit = max(200, int(4000 * max(0.05, min(ratio, 0.6))))
        slices = [
            BudgetSlice(name=f"hit:{hit.atom_id}", priority=index, text=hit.line())
            for index, hit in enumerate(hits)
        ]
        packed = self.plugin.budget.pack(slices, limit) if hits else None
        body = "\n".join(item.text for item in packed.kept) if packed is not None else ""
        content = "\n".join(part for part in [*extras, body] if part).strip()
        if not content:
            return
        # 动态内容一律走 extra_user_content_parts，绝不碰 system_prompt：
        # system_prompt 每轮都变的话，厂商的 prefix cache 直接失效，全价重算。
        block = (
            f"{MARKER}\n"
            "【长期记忆 · 仅供参考；与当前对话冲突时以当前对话为准】\n"
            f"{content}\n"
            "【记忆结束】"
        )
        add_temp_part(req, block, MARKER)

        kept_names = {item.name for item in packed.kept} if packed is not None else set()
        hit_ids = [hit.atom_id for hit in hits if f"hit:{hit.atom_id}" in kept_names]
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
            # 默认 False：拿不到明确成功信号就不推水位，宁可下一轮重试
            ok = bool(getattr(self.extractor, "last_ok", False))
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
                await self._prepare_drafts(drafts, rows)
                ids = await self.atoms.add_many(
                    drafts, scope=scope, subject=subject, persona_id=self.config.persona_id
                )
                if ids:
                    await self._link_subjects(drafts, ids)
                    await self.plugin.bus.emit(self.plugin.bus.ON_MEMORY_WRITTEN, drafts)
                    logger.debug(f"[congling] 写入 {len(ids)} 条记忆（{session_id}）")
            # 只有解析成功才推水位：模型返回废话或调用失败，下一轮还要重试。
            # 另外只推到「真的送进模型」的最后一行 —— 被 6000 字符截掉的尾巴
            # 下一轮还得重新抽，不能让它们静默消失。
            if ok:
                target = max_row
                used = getattr(self.extractor, "consumed_turns", None)
                if isinstance(used, list) and used and len(used) < len(rows):
                    last_idx = max(used)
                    if 0 <= last_idx < len(rows):
                        target = int(rows[last_idx]["id"])
                if target > 0:
                    await self._set_water(session_id, target)
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

    # ------------------------------------------------------------ 等级与主体
    async def _prepare_drafts(self, drafts: list[MemoryDraft], rows: list[dict[str, Any]]) -> None:
        """给每条草稿定主体、定等级。

        顺序不能反：先认出「这条说的是谁」，才知道该按谁的好感度去调等级。
        群里一句话可能是别人说的，所以只在草稿主体对得上最后一位发言人时才用他的 QQ 号。
        """
        # 逐条证据反查说话人。以前是"整批都用最后一个发言人"，于是群里
        # A 说「B 昨天吵架了」也会记到 A 头上，主体就这么分裂出去的。
        by_idx = {
            str(i): (str(r.get("sender_id") or ""), str(r.get("sender_name") or ""))
            for i, r in enumerate(rows)
        }
        tail_id, tail_name = "", ""
        for row in reversed(rows):
            if str(row.get("sender_id") or ""):
                tail_id = str(row.get("sender_id"))
                tail_name = str(row.get("sender_name") or "")
                break

        for draft in drafts:
            try:
                hint = str(draft.subject or "").strip()
                ev_id, ev_name = "", ""
                for token in (getattr(draft, "evidence_msg_ids", []) or []):
                    idx = str(token).lstrip("#").strip()
                    if idx in by_idx and by_idx[idx][0]:
                        ev_id, ev_name = by_idx[idx]
                        break
                speaker_id = ev_id or tail_id
                speaker_name = ev_name or tail_name
                same_person = bool(
                    hint and speaker_name and (hint == speaker_name or hint in speaker_name or speaker_name in hint)
                )
                key = await self.subjects.identify(
                    sender_id=speaker_id if (same_person or not hint) else "",
                    sender_name=speaker_name if same_person else "",
                    subject_text=hint,
                )
                draft.subject_key = key
                stage = await self.affinity.stage(key) if key else None
                level, reason, tags = self.grades.judge_one(draft, stage=stage)
                draft.level = level
                draft.level_reason = reason
                draft.level_tags = tags
            except Exception as exc:
                logger.debug(f"[congling] 等级判定失败: {exc!r}")

    async def _link_subjects(self, drafts: list[MemoryDraft], ids: list[int]) -> None:
        """把人挂到记忆上。

        不能用 zip(drafts, ids)：写入阶段任何一条失败都不会进 ids，两个列表就错位了，
        人会被记到别人头上。所以按文本哈希回查一遍，稳一点。
        """
        for draft in drafts:
            key = str(getattr(draft, "subject_key", "") or "")
            if not key:
                continue
            atom_id = 0
            try:
                row = await self.store.fetch_one(
                    "SELECT id FROM mem_atom WHERE persona_id=? AND norm_hash=? AND status='active'",
                    (self.config.persona_id, norm_hash(draft.text)),
                )
                atom_id = int((row or {}).get("id") or 0)
            except Exception:
                atom_id = 0
            if atom_id:
                await self.subjects.link_atom(atom_id, key)

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
        try:
            settled = await self.affinity.settle_daily()
            logger.debug(f"[congling] 好感度结算：{settled}")
        except Exception as exc:
            logger.debug(f"[congling] 好感度结算失败: {exc!r}")
        try:
            await self.build_profiles()
        except Exception as exc:
            logger.debug(f"[congling] 画像轮询失败: {exc!r}")
        pending = await self.retriever.embed_pending(30)
        if pending:
            logger.debug(f"[congling] 补齐 {pending} 条记忆向量")

    async def build_profiles(self, limit: int = 30, force: bool = False) -> dict[str, Any]:
        """每天挑几个够格的人重新画像。

        够格不等于每次都建：概率由好感度决定，今天掷不中明天再说。
        force 是给 UI 的「现在生成」按钮用的，跳过概率但仍受开关与名单约束。
        """
        if not self.profiles.enabled():
            return {"built": 0, "reason": "画像总开关关着"}
        today = time.strftime("%Y-%m-%d")
        if not force:
            try:
                if await self.store.kv_get("profile:last_day", "") == today:
                    return {"built": 0, "skipped": "今天已经轮过"}
            except Exception:
                pass
        rows = await self.store.fetch_all(
            "SELECT user_id FROM mem_subject WHERE persona_id=? ORDER BY last_seen_at DESC LIMIT ?",
            (self.config.persona_id, max(1, min(200, limit))),
        )
        built = 0
        for row in rows:
            key = str(row.get("user_id") or "")
            if not key:
                continue
            try:
                result = await self.profiles.build(key, force=force)
                if result.get("ok"):
                    built += 1
            except Exception as exc:
                logger.debug(f"[congling] 画像生成失败 {key}: {exc!r}")
        if not force:
            try:
                await self.store.kv_set("profile:last_day", today)
            except Exception:
                pass
        return {"built": built}

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
