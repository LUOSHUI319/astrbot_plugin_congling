"""醒后补课流水线（L2 爬楼摘要 / L3 分流判定 / L4 逐条回复）。

把「睡着期间没回的消息」在醒后变成一份可执行的清单，逐条判断值不值得开口、
该不该回、怎么回。

设计边界（铁律）：
- 本模块只依赖 ``PerceivedMessage`` / ``msg_archive`` / ``decision_log`` 与
  ``night`` 暴露的门面方法，不认识 at_guard 的实现细节——at_guard 只是 L3 里
  一个可替换的「打分零件」。
- L1（落库）在 perception.archive 里，本模块只负责「醒后读增量」。
- 幂等：压缩游标持久化到 kv，崩溃重启不重复压。

分层：
  L2  分块 + 摘要 + 结构化清单（主力模型一次干完）
  L3  接 at_guard 打分，或本地判据（被@/点名/引用/问句）三档分流
  L4  清单逐条解答，合并成一条发，过期跳过、待追问
  L5  存档（长线记忆镜像到 livingmemory）在 memory 侧，本模块只产出原料
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from ..core.contracts import PerceivedMessage

# kv 键：醒后补课的压缩游标，session_id -> 上次压到的 msg_archive.id
ROLLUP_CURSOR_KEY = "night.rollup.cursor"
# kv 键：醒后待机计时（session_id -> 最后一条消息时间戳）
IDLE_MARK_KEY = "night.rollup.idle_mark"

# 日志前缀，出问题能定位是哪一层糊的
LOG = "[congling:rollup]"


@dataclass
class RollupItem:
    """清单里的一条：一条「值得看的历史消息」的判定产物。"""

    msg_id: str = ""
    session_id: str = ""
    who: str = ""            # 谁问的
    what: str = ""           # 问的什么
    targets_me: bool = False  # 是不是冲我来的
    answered: bool = False    # 我答过没有
    refs: list[str] = field(default_factory=list)  # 涉及哪几条引用
    priority: int = 0         # 0~100，越高越该回
    expired: bool = False     # 是否已过期（话题翻篇/问题自解）

    def to_dict(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "session_id": self.session_id,
            "who": self.who,
            "what": self.what,
            "targets_me": self.targets_me,
            "answered": self.answered,
            "refs": list(self.refs),
            "priority": self.priority,
            "expired": self.expired,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RollupItem":
        return cls(
            msg_id=str(data.get("msg_id") or ""),
            session_id=str(data.get("session_id") or ""),
            who=str(data.get("who") or ""),
            what=str(data.get("what") or ""),
            targets_me=bool(data.get("targets_me")),
            answered=bool(data.get("answered")),
            refs=[str(x) for x in (data.get("refs") or [])],
            priority=int(data.get("priority") or 0),
            expired=bool(data.get("expired")),
        )


@dataclass
class RollupChunk:
    """一块被压完的历史消息。"""

    session_id: str = ""
    chunk_start_id: int = 0   # 首条 msg_archive.id
    chunk_end_id: int = 0     # 末条 msg_archive.id
    started_at: int = 0
    ended_at: int = 0
    summary: str = ""         # 一段话摘要
    items: list[RollupItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "chunk_start_id": self.chunk_start_id,
            "chunk_end_id": self.chunk_end_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "summary": self.summary,
            "items": [it.to_dict() for it in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RollupChunk":
        return cls(
            session_id=str(data.get("session_id") or ""),
            chunk_start_id=int(data.get("chunk_start_id") or 0),
            chunk_end_id=int(data.get("chunk_end_id") or 0),
            started_at=int(data.get("started_at") or 0),
            ended_at=int(data.get("ended_at") or 0),
            summary=str(data.get("summary") or ""),
            items=[RollupItem.from_dict(x) for x in (data.get("items") or [])],
        )


class Rollup:
    """醒后补课流水线。挂在 NightModule 下，由 wake 流程触发。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self._locks: dict[str, asyncio.Lock] = {}
        self._cursor_lock = asyncio.Lock()

    # ------------------------------------------------------------ 开关与参数
    @property
    def enabled(self) -> bool:
        return self.config.bool("night.rollup.enabled", False)

    def _chunk_size(self) -> int:
        return max(1, self.config.int("night.rollup.chunk_size", 45))

    def _chunk_window_minutes(self) -> int:
        return max(1, self.config.int("night.rollup.chunk_window_minutes", 60))

    def _chunk_hard_cap(self) -> int:
        return max(self._chunk_size(), self.config.int("night.rollup.chunk_hard_cap", 80))

    def _hi_threshold(self) -> int:
        return self.config.int("night.rollup.hi_threshold", 80)

    def _mid_threshold(self) -> int:
        return self.config.int("night.rollup.mid_threshold", 50)

    def _max_replies(self) -> int:
        return max(1, self.config.int("night.rollup.max_replies_per_wake", 3))

    # ------------------------------------------------------------ L2 增量读
    async def _cursor(self, session_id: str) -> int:
        raw = await self.store.kv_get(ROLLUP_CURSOR_KEY, {}) or {}
        if not isinstance(raw, dict):
            return 0
        return int(raw.get(session_id) or 0)

    async def _save_cursor(self, session_id: str, end_id: int) -> None:
        async with self._cursor_lock:
            raw = await self.store.kv_get(ROLLUP_CURSOR_KEY, {}) or {}
            if not isinstance(raw, dict):
                raw = {}
            raw[session_id] = max(int(raw.get(session_id) or 0), int(end_id))
            await self.store.kv_set(ROLLUP_CURSOR_KEY, raw)

    async def _read_incremental(self, session_id: str, since_id: int, since_ts: int = 0,
                                until_ts: int = 0, exclude_msg_id: str = "") -> list[dict[str, Any]]:
        """从 since_id 往后读这个会话的消息，按 id 升序。"""
        if not session_id:
            return []
        try:
            rows = await self.store.fetch_all(
                "SELECT id, session_id, msg_id, sender_id, sender_name, ts, text "
                "FROM msg_archive WHERE session_id=? AND id>? AND ts>=? "
                "AND (?=0 OR ts<=?) AND msg_id<>? ORDER BY id ASC",
                (session_id, since_id, since_ts, until_ts, until_ts, exclude_msg_id),
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.debug(f"{LOG} L2 增量读失败: {exc!r}")
            raise

    def _split_chunks(self, rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """按「条数 / 时间窗 / 硬上限」谁先到谁封块。"""
        chunks: list[list[dict[str, Any]]] = []
        cur: list[dict[str, Any]] = []
        size = self._chunk_size()
        window = self._chunk_window_minutes() * 60
        cap = self._chunk_hard_cap()
        first_ts = 0
        for row in rows:
            if not cur:
                cur.append(row)
                first_ts = int(row.get("ts") or 0)
                continue
            ts = int(row.get("ts") or 0)
            if len(cur) >= size or (first_ts and ts - first_ts >= window) or len(cur) >= cap:
                chunks.append(cur)
                cur = [row]
                first_ts = ts
            else:
                cur.append(row)
        if cur:
            chunks.append(cur)
        return chunks

    async def _summarize_chunk(self, chunk: list[dict[str, Any]]) -> RollupChunk:
        """把一块消息交给主力模型，产出摘要 + 结构化清单。"""
        session_id = str(chunk[0].get("session_id") or "")
        start_id = int(chunk[0].get("id") or 0)
        end_id = int(chunk[-1].get("id") or 0)
        started_at = int(chunk[0].get("ts") or 0)
        ended_at = int(chunk[-1].get("ts") or 0)

        lines = []
        for r in chunk:
            name = str(r.get("sender_name") or r.get("sender_id") or "某人")
            if str(r.get("sender_id") or "") == "bot":
                name = "bot（当前角色）"
            text = " ".join(str(r.get("text") or "").split())
            if text:
                lines.append(f"[msg_id={r.get('msg_id', '')}][{name}] {text[:200]}")
        transcript = "\n".join(lines)

        items, summary = await self._llm_summarize(transcript)
        for item in items:
            item.session_id = session_id

        return RollupChunk(
            session_id=session_id,
            chunk_start_id=start_id,
            chunk_end_id=end_id,
            started_at=started_at,
            ended_at=ended_at,
            summary=summary,
            items=items,
        )

    async def _llm_summarize(self, transcript: str) -> tuple[list[RollupItem], str]:
        """主力模型一次干完：摘要 + 清单。失败降级为空。"""
        model = getattr(self.plugin, "models", None)
        if model is None:
            return [], ""
        prompt = (
            "下面是一段你睡着期间没来得及看的群聊/私聊记录。请你只输出一个 JSON 对象，"
            "不要输出任何 JSON 以外的文字。JSON 结构如下：\n"
            '{"summary": "一段话概括这段记录发生了什么", '
            '"items": [{"msg_id":"原消息ID","who":"谁","what":"问的/说的什么","targets_me":true或false,'
            '"answered":true或false,"refs":["引用的消息片段"],"priority":0到100的整数}]}\n'
            "targets_me 表示这条是不是冲着你（bot）来的（被@、被点名、直接问你）；"
            "answered 表示你在记录里是否已经回答过；priority 表示值不值得你醒来后回，"
            "冲你来的、还没回答的、是明确问题的打高分。\n\n记录：\n" + transcript
        )
        try:
            raw = await model.chat(
                prompt,
                system="你负责从聊天记录里提取值得回复的事项。只输出 JSON。",
                temperature=0.2,
                max_tokens=2000,
            )
        except Exception as exc:
            logger.debug(f"{LOG} L2 摘要模型调用失败: {exc!r}")
            return [], ""
        data = self._parse_json(raw)
        if not data:
            return [], ""
        summary = str(data.get("summary") or "").strip()
        items: list[RollupItem] = []
        for it in data.get("items") or []:
            if not isinstance(it, dict):
                continue
            try:
                items.append(
                    RollupItem(
                        who=str(it.get("who") or ""),
                        msg_id=str(it.get("msg_id") or ""),
                        what=str(it.get("what") or ""),
                        targets_me=bool(it.get("targets_me")),
                        answered=bool(it.get("answered")),
                        refs=[str(x) for x in (it.get("refs") or [])],
                        priority=max(0, min(100, int(it.get("priority") or 0))),
                    )
                )
            except (TypeError, ValueError):
                continue
        return items, summary

    @staticmethod
    def _parse_json(raw: Any) -> dict[str, Any] | None:
        if not raw:
            return None
        text = str(raw).strip()
        # 去掉可能的代码块围栏
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            # 兜底：截取第一个 { 到最后一个 }
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                return None
            try:
                data = json.loads(text[start : end + 1])
            except (ValueError, TypeError):
                return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------ L2 入口
    async def rollup_session(self, session_id: str, since_ts: int = 0, *,
                             until_ts: int = 0, exclude_msg_id: str = "",
                             commit: bool = True) -> list[RollupChunk]:
        """醒来后对某个会话跑一次增量摘要。返回压出来的块（可能多块）。"""
        if not self.enabled:
            return []
        cursor = await self._cursor(session_id)
        rows = await self._read_incremental(session_id, cursor, since_ts, until_ts, exclude_msg_id)
        if not rows:
            return []
        chunks: list[RollupChunk] = []
        for chunk_rows in self._split_chunks(rows):
            chunk = await self._summarize_chunk(chunk_rows)
            if chunk.summary or chunk.items:
                chunks.append(chunk)
                if commit:
                    await self._save_cursor(session_id, chunk.chunk_end_id)
            else:
                # 生成失败不假装已读；主动正文必须等补课真正完成。
                raise RuntimeError("补课摘要生成失败，保留游标")
        return chunks

    # ------------------------------------------------------------ L3 分流
    async def triage(self, chunk: RollupChunk) -> dict[str, Any]:
        """对一块清单做三档分流。返回可执行的回复计划。

        分流规则（道友定的三态）：
        - 耦合 at_guard（compat.at_guard_coupled=true）→ 逐条交 at_guard 打分；
        - 没耦合 → 走 congling 自己的 Decider（回复阈值 + 什么情况下回），
          完整保留既有信号逻辑（被点名/叫名字/问句/话题相关/群太吵/静默时段）。
        """
        scored: list[dict[str, Any]] = []
        for it in chunk.items:
            score, verdict, signals = await self._score_item(chunk, it)
            scored.append(
                {"item": it, "score": score, "verdict": verdict, "signals": signals}
            )

        to_reply = [s for s in scored if s["verdict"] == "reply"]
        to_follow = [s for s in scored if s["verdict"] == "followup"]
        return {
            "to_reply": to_reply[: self._max_replies()],
            "to_follow": to_follow,
            "all": scored,
        }

    def _coupled(self) -> bool:
        """是否耦合 at_guard。开关在「与其他插件共存」（compat.at_guard_coupled）。"""
        return self.config.bool("compat.at_guard_coupled", False)

    async def _score_item(
        self, chunk: RollupChunk, it: RollupItem
    ) -> tuple[int, str, dict[str, Any]]:
        """对清单里的一条打分，返回 (score, verdict, signals)。

        score 归一化到 0~100；verdict ∈ reply / followup / silent。
        """
        if it.answered or it.expired:
            return 0, "silent", {"source": "already_answered_or_expired"}
        if self._coupled():
            return await self._score_by_at_guard(it)
        return await self._score_by_decider(chunk, it)

    async def _score_by_at_guard(
        self, it: RollupItem
    ) -> tuple[int, str, dict[str, Any]]:
        """耦合：把这条交给 at_guard 打分。返回 0~100 分值 + 三档判定。

        at_guard 的 GateEngine.decide 需要 resolve_provider 回调，congling 拿不到
        provider，所以走 at_guard 暴露的对外轻量入口 at_guard_score()。
        拿不到入口 / 打分失败 → 降级回 Decider，不让整个补课停摆。
        """
        score = await self._call_at_guard_score(it)
        if score is None:
            # 降级：at_guard 没暴露对外入口，回退 congling 自己的判据
            return await self._score_by_decider(None, it)
        verdict = self._verdict(score)
        return score, verdict, {"source": "at_guard"}

    async def _call_at_guard_score(self, it: RollupItem) -> int | None:
        """调用 at_guard 的对外打分入口（若存在）。返回 0~100 或 None。"""
        try:
            stars = self.plugin.context.get_all_stars() or []
            guard_star = None
            for meta in stars:
                name = str(getattr(meta, "root_dir_name", "") or getattr(meta, "name", "") or "")
                if name == "astrbot_plugin_at_guard" and getattr(meta, "activated", True):
                    guard_star = meta
                    break
            if guard_star is None:
                return None
            star_cls = getattr(guard_star, "star_cls", None)
            if star_cls is None or not hasattr(star_cls, "at_guard_score"):
                return None
            # 构造一个最小消息文本交给 at_guard 打分
            text = f"{it.who}: {it.what}".strip()
            if not text:
                return None
            raw = await star_cls.at_guard_score(text=text)
            if raw is None:
                return None
            if isinstance(raw, dict) and "score" in raw:
                return int(raw["score"])
            if isinstance(raw, (int, float)):
                return int(raw)
        except Exception as exc:
            logger.debug(f"{LOG} L3 at_guard 打分失败: {exc!r}")
        return None

    async def _score_by_decider(
        self, chunk: RollupChunk | None, it: RollupItem
    ) -> tuple[int, str, dict[str, Any]]:
        """没耦合：复用 congling 的 Decider（回复阈值 + 什么情况下回）。

        构造一个 PerceivedMessage，塞进 it 的 who/what/targets_me/answered，
        交给 decider.decide() 打分。完整保留「什么情况下回」的信号逻辑。
        """
        decider = getattr(self.plugin, "decider", None)
        if decider is None:
            # 兜底：没有 decider 就按 priority 粗判
            score = max(0, min(100, it.priority))
            return score, self._verdict(score), {"source": "priority_fallback"}

        session_id = it.session_id or (chunk.session_id if chunk else "")
        session_kind = "group"  # 群聊默认；私聊时 decide 直接给 100 分
        # 醒后补课的清单本质是「群聊里冲我来的历史消息」，按 group 判
        msg = PerceivedMessage(
            msg_id=it.msg_id,
            session_id=session_id,
            session_kind=session_kind,
            sender_id="",
            sender_name=it.who,
            text=it.what,
            mentions=[it.who] if it.targets_me else [],
            quoted="",
        )
        try:
            decision = await decider.decide(None, msg)
        except Exception as exc:
            logger.debug(f"{LOG} L3 Decider 打分失败: {exc!r}")
            score = max(0, min(100, it.priority))
            return score, self._verdict(score), {"source": "decider_error"}

        # ReplyDecision.score 是 congling 的信号分（可能负值），映射到 0~100
        score = 100 if it.targets_me else int(max(0, min(100, decision.score)))
        # 已答过 / 明确过期 → 降档
        if it.answered:
            score = max(0, score - 40)
        verdict = self._verdict(score)
        return score, verdict, {
            "source": "decider",
            "signals": dict(decision.signals or {}),
            "threshold": decision.threshold,
            "reason": decision.reason,
        }

    def _verdict(self, score: int) -> str:
        if score >= self._hi_threshold():
            return "reply"
        if score >= self._mid_threshold():
            return "followup"  # 中分：可让小模型补判，本版记为待追问/低优先
        return "silent"

    # ------------------------------------------------------------ L4 回复文本
    async def compose_reply(self, plan: dict[str, Any], *, umo: str = "",
                            identity: dict[str, Any] | None = None, strict: bool = False) -> str:
        """把待回复项合并成一条拟人回复（道友选定：合并一条发，逐条解答）。

        让主力模型逐条生成自然语言回复，而不是拼「· 问的什么」的骨架。
        失败降级：退化成结构化清单文本，保证不空手。
        """
        to_reply = plan.get("to_reply") or []
        if not to_reply:
            return ""
        items = [s["item"] for s in to_reply if isinstance(s.get("item"), RollupItem)]
        if not items:
            return ""
        text = await self._llm_compose(items, umo=umo, identity=identity)
        if text:
            return text
        if strict:
            raise RuntimeError("补课回复生成失败，保留未读")
        # 降级：模型失败就拼骨架
        lines = [f"· {it.what}" for it in items]
        return "我刚把睡着那会儿的消息翻完了，有几件冲我来的回一下：\n" + "\n".join(lines)

    async def _llm_compose(self, items: list[RollupItem], *, umo: str = "",
                           identity: dict[str, Any] | None = None) -> str:
        """让主力模型逐条生成拟人回复。"""
        model = getattr(self.plugin, "models", None)
        if model is None:
            return ""
        lines = []
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {it.who}：{it.what}")
        body = "\n".join(lines)
        prompt = (
            "你已经醒来，翻完了睡着期间没回的消息。下面这几条是冲着你来的，"
            "还没回答过。请你用自然、拟人的口吻，把它们**合并成一段连贯的话**回应，"
            "像人在群里接完话一样，不要机械地念问题，也不要逐条罗列、不要用编号。"
            "尽量少分段，能揉进一个自然段落就揉进去，最多分两段。"
            "开头可以带一句「我刚醒/翻完消息」，然后顺下来回应。\n\n"
            + body
        )
        try:
            raw = await model.character_chat(
                prompt,
                system="你是一个刚睡醒、在群里补消息的人，说话自然接地气。",
                umo=umo, identity=identity,
                temperature=0.8,
                max_tokens=600,
            )
        except Exception as exc:
            logger.debug(f"{LOG} L4 拟人回复生成失败: {exc!r}")
            return ""
        return (raw or "").strip()

    # ------------------------------------------------------------ 待机（问题 2 双控制）
    async def note_message(self, session_id: str) -> None:
        """醒后每处理一条消息都记一下时间戳，供 idle 判定。"""
        await self.store.kv_set(f"{IDLE_MARK_KEY}:{session_id}", int(time.time()))

    async def idle_since(self, session_id: str) -> int:
        """返回这个会话距上次消息静默了多少秒。没记录返回 -1。"""
        raw = await self.store.kv_get(f"{IDLE_MARK_KEY}:{session_id}", None)
        if raw is None:
            return -1
        return int(time.time()) - int(raw or 0)
