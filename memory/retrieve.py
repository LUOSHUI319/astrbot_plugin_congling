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
from .refs import actor_ref as make_actor_ref, session_ref as make_session_ref, parse_ref
from .subject import SubjectBook
from ..core.exc import exc_brief

# 虚词不参与"实词命中"加权：不然「你还记得…吗」里的「你/还/吗」会把
# 真正有信息量的词（人名、事件名）压下去。
_STOP_CHARS = set("你我他她它们这那还就都也的吗呢吧啊呀了是有在和与跟把被给对很太又再才只")
_WORD_RE = re.compile(r"[a-zA-Z0-9_]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_META_RE = re.compile(
    r"^(?:你|您)?(?:还|仍然|真的|是不是|是否|已经|都|就|也|会|能|不|没|有)*(?:记得|记住|认识|知道)我(?:是谁)?(?:吗|么|嘛|呢|啊|呀|了)?$"
    r"|^(?:你|您)?(?:是不是|是否|已经|都|又|还)*忘(?:记|了)?我(?:了)?(?:吗|么|嘛|呢|啊|呀)?$"
    r"|^(?:你|您)?(?:还|已经|都)?忘了吗$"
    r"|^(?:你|您)?(?:是不是|是否|已经|都|又|还)*把我忘(?:记)?了(?:吗|么|嘛|呢)?$"
)


def is_identity_question(query: str) -> bool:
    return bool(_META_RE.fullmatch(re.sub(r"[\s，。！？?,.!~～]", "", query or "")))


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
        session_ref: str = "",
        actor_ref: str = "",
    ) -> list[MemoryHit]:
        # 不许再退回写死的 "ling"：多分区环境下那会把所有人的记忆串到一起
        persona_id = persona_id or self.config.persona_id
        query = (query or "").strip()
        top_k = int(self.config.int("memory.inject_top_k", 5) if k is None else k)
        if not query or top_k <= 0:
            return []

        session_ref, actor_ref = self._compat_refs(scope, subject, session_ref, actor_ref)
        aliases = await SubjectBook(self.plugin).query_aliases(query, persona_id)
        rows = await self._candidates(scope, subject, persona_id, cross_session=cross_session,
                                      mode=mode, session_ref=session_ref, actor_ref=actor_ref,
                                      row_query=query, alias_ids=tuple(aliases))
        for row in rows:
            about = parse_ref(str(row.get("subject_ref") or ""), "user")
            uid = about[1] if about and about[0] == "qq" else (
                str(row.get("subject_id") or "") if not row.get("subject_ref") else "")
            row["_alias_terms"] = " ".join(aliases.get(uid, []))
        if not rows:
            return []

        excluded = exclude or set()
        if is_identity_question(query):
            # about_ref is mem_atom.subject_ref, not the evidence speaker (actor_ref).
            personal = [r for r in rows if r.get("_personal") and int(r["id"]) not in excluded]
            personal.sort(key=lambda r: (6 if r.get("mem_level") == 0 else int(r.get("mem_level") or 3),
                                          float(r.get("importance") or 0), int(r["id"])), reverse=True)
            return [AtomStore.to_hit(r, float(r.get("importance") or 0.5)) for r in personal[:top_k]]

        terms = tokenize(query)
        # jieba 会按上下文把「大猫」分成不同粒度；精确别名额外保留整词。
        terms.extend(name for names in aliases.values() for name in names if name not in terms)
        bm25_scores = self._bm25(rows, terms)
        vector_scores = await self._vector(rows, query)

        fused: dict[int, float] = {}
        for ranking in (bm25_scores, vector_scores):
            ordered = sorted(ranking.items(), key=lambda item: item[1], reverse=True)
            for rank, (atom_id, _score) in enumerate(ordered):
                fused[atom_id] = fused.get(atom_id, 0.0) + 1.0 / (60 + rank + 1)

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

        # 实词加权：候选池同质时（比如全是"关于这个人"的记忆），BM25 的 idf
        # 会被拉平，含"你/还/吗"的句子反而排前面。按实词命中数补一档。
        # 「记得/认识/知道」这类元问句动词同样没有信息量：她该想起的是"谁"和"什么事"
        _META_WORDS = ("记得", "记住", "认识", "知道", "想起", "忘记", "忘了")
        content_terms = [t for t in set(terms)
                         if len(t) >= 2 and not any(ch in _STOP_CHARS for ch in t)
                         and t not in _META_WORDS]
        if content_terms:
            for hit in hits:
                text = hit.text or ""
                matched = sum(1 for t in content_terms if t in text)
                if matched:
                    hit.score *= 1.0 + 0.6 * matched
        hits.sort(key=lambda item: item.score, reverse=True)
        if not hits:
            fallback = [r for r in rows if r.get("_session") and int(r["id"]) not in excluded]
            fallback.sort(key=lambda r: (float(r.get("importance") or 0) * float(r.get("decay") or 0),
                                          int(r["id"])), reverse=True)
            for row in fallback[:top_k]:
                hit = AtomStore.to_hit(row, float(row.get("importance") or 0) * float(row.get("decay") or 0))
                hit.weak_related = True
                hits.append(hit)
        return hits[:top_k]

    async def recall_text(
        self, query: str, *, scope: str, subject: str, k: int = 5, cross_session: bool = False
    ) -> str:
        hits = await self.search(
            query, scope=scope, subject=subject, k=k, cross_session=cross_session
        )
        return "\n".join(hit.line() for hit in hits)

    @staticmethod
    def _compat_refs(scope: str, subject: str, session: str, actor: str) -> tuple[str, str]:
        # The pre-ref API belongs to the existing QQ plugin. Explicit refs never
        # borrow the group number as the current speaker.
        if not session:
            session = make_session_ref("qq", "group" if scope == "group" else "private", subject)
        parsed = parse_ref(session, "session")
        if not actor and scope == "user" and subject and parsed and parsed[1] == "private":
            actor = make_actor_ref(parsed[0], subject)
        return session, actor

    # ---------------------------------------------------------------- 候选
    @staticmethod
    def _alias_gate(ids: tuple[str, ...]) -> tuple[str, tuple]:
        if not ids:
            return "0", ()
        marks = ",".join("?" for _ in ids)
        return (f"(m.subject_ref IN ({marks}) OR (COALESCE(m.subject_ref,'')='' AND m.subject_id IN ({marks})))",
                tuple(make_actor_ref("qq", uid) for uid in ids) + ids)

    async def _candidates(
        self,
        scope: str,
        subject: str,
        persona_id: str,
        cross_session: bool = False,
        mode: str = "",
        *,
        session_ref: str = "",
        actor_ref: str = "",
        row_query: str = "",
        alias_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        session_ref, actor_ref = self._compat_refs(scope, subject, session_ref, actor_ref)
        session = parse_ref(session_ref, "session")
        actor = parse_ref(actor_ref, "user")
        if not session or session[1] not in {"group", "private"}:
            # 没有会话上下文（工具/面板主动检索）。all 档下按「群聊 + 公共」全库搜，
            # 私聊一律不碰——工具不该成为翻别人私房话的入口。
            if str(mode or "").strip().lower() == "all" and cross_session:
                return await self._all_public(persona_id, row_query, alias_ids=alias_ids)
            if not session_ref and not subject:
                rows = await self.store.fetch_all(
                    "SELECT * FROM mem_atom WHERE persona_id=? AND scope='global' AND status='active' "
                    "AND decay>=0.15 AND COALESCE(unread,0)=0 AND COALESCE(session_kind,'')<>'private' "
                    "AND COALESCE(session_ref,'') NOT LIKE 'session:%:private:%' "
                    "ORDER BY importance*decay DESC,id DESC LIMIT 800", (persona_id,),
                )
                for row in rows:
                    row["_public"] = True
                return rows
            return []
        platform, kind, sid = session
        uid = actor[1] if actor and actor[0] == platform else ""
        configured = self.config.str("memory.cross_session_scope", "same_user").strip().lower()
        mode = str(mode or configured).strip().lower()
        cross = bool(cross_session and uid and configured != "off" and mode != "off"
                     and self.config.bool("memory.cross_session_recall", True))
        allow = self.config.list("memory.cross_session_whitelist", []) if mode == "whitelist" else []
        if mode == "whitelist" and sid not in allow and session_ref not in allow:
            cross = False

        # Legacy reads require the old session type and reject conflicting known
        # platforms. New refs are authoritative; old subject_id never means where.
        legacy_platform = (
            "(COALESCE(m.session_id,''),COALESCE(m.session_kind,'')) NOT IN "
            "(SELECT COALESCE(p.session_id,''),COALESCE(p.session_kind,'') FROM msg_archive p "
            "WHERE COALESCE(lower(p.platform),'') NOT IN ('','default') "
            "AND lower(p.platform) NOT IN (?,?,?,?))"
        )
        aliases = (platform, "aiocqhttp", "onebot", "onebot_v11") if platform == "qq" else (platform,) * 4
        if platform != "qq":
            # Unlabelled historical rows belong only to the old QQ compatibility
            # path. Other platforms need positive archive evidence as well.
            legacy_platform += (
                " AND EXISTS(SELECT 1 FROM msg_archive p WHERE p.session_id=m.session_id "
                "AND p.session_kind=m.session_kind AND lower(p.platform)=?)"
            )
            aliases += (platform,)
        session_gate = (
            "(m.session_ref=? OR (COALESCE(m.session_ref,'')='' AND " + legacy_platform +
            " AND ((m.session_id=? AND m.session_kind=?) OR "
            "(COALESCE(m.session_id,'')='' AND m.scope=? AND m.subject_id=?))))"
        )
        session_args = (session_ref, *aliases, sid, kind, "group" if kind == "group" else "user", sid)
        personal_gate = "(m.subject_ref=? OR (COALESCE(m.subject_ref,'')='' AND m.subject_id=? AND " + legacy_platform + "))"
        personal_args = (actor_ref if uid else "!", uid or "!", *aliases)
        # Even all/whitelist must never open another person's private session.
        private = ("(COALESCE(m.session_ref,'') LIKE 'session:%:private:%' OR (COALESCE(m.session_ref,'')='' "
                   "AND (COALESCE(m.session_kind,'')='private' OR m.scope='user')))")
        private_gate = (
            "(NOT " + private + " OR (m.session_ref=? OR (COALESCE(m.session_ref,'')='' "
            "AND (m.session_id=? OR (COALESCE(m.session_id,'')='' AND m.subject_id=?)))))"
        )
        private_args = (make_session_ref(platform, "private", uid) or "!", uid or "!", uid or "!")
        base = ("SELECT m.* FROM mem_atom m WHERE m.persona_id=? AND m.status='active' "
                "AND m.scope<>'self' AND m.decay>=0.15 AND (m.unread IS NULL OR m.unread=0) AND "
                + private_gate + " AND ")
        layers = [("_session", session_gate, session_args)]
        if uid:
            gate, args = personal_gate, personal_args
            if not cross:
                gate += " AND " + session_gate
                args += session_args
            elif mode == "whitelist":
                marks = ",".join("?" for _ in allow)
                gate += f" AND (m.session_id IN ({marks}) OR m.session_ref IN ({marks}))"
                args += (*allow, *allow)
            layers.append(("_personal", gate, args))
        if cross and mode == "all":
            # all 档：所有群聊 + 公共 + 自己的私聊（别人私聊被 private_gate 挡住）。
            # 没有这一层，"在别的群问起某个人"永远只会查关于自己的记忆。
            # 但全库有几千条，只按重要度取前 800 很可能把目标挤出去，
            # 所以先用查询词做一次粗筛：命中词的优先，没词时退化为纯排序。
            broad_terms = [t for t in tokenize(row_query) if len(t) >= 2][:6] if row_query else []
            if broad_terms:
                like = " OR ".join(["m.text LIKE ?" for _ in broad_terms])
                alias_gate, alias_args = self._alias_gate(alias_ids)
                layers.append(("_broad", f"({like} OR {alias_gate})", tuple(f"%{t}%" for t in broad_terms) + alias_args))
            else:
                layers.append(("_broad", "1=1", ()))
        layers.append(("_public", "m.scope='global'", ()))
        merged: dict[int, dict] = {}
        for layer, gate, args in layers:
            # Personal candidates are level ordered before LIMIT for meta questions.
            order = "CASE WHEN m.mem_level=0 THEN 6 ELSE m.mem_level END DESC, " if layer == "_personal" else ""
            rows = await self.store.fetch_all(base + gate + " ORDER BY " + order +
                                               "m.importance*m.decay DESC, m.id DESC LIMIT 800",
                                               (persona_id, *private_args, *args))
            for row in rows:
                item = merged.setdefault(int(row["id"]), row)
                item[layer] = True
        return list(merged.values())

    async def _all_public(self, persona_id: str, row_query: str = "", *, alias_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """全库检索（只含群聊与公共记忆）。用于没有会话上下文的主动回忆。"""
        terms = [t for t in tokenize(row_query) if len(t) >= 2][:6]
        where = "m.scope IN ('group','global') AND m.scope<>'self'"
        args: tuple = ()
        if terms:
            like = " OR ".join(["m.text LIKE ?" for _ in terms])
            alias_gate, alias_args = self._alias_gate(alias_ids)
            where += f" AND ({like} OR {alias_gate})"
            args = tuple(f"%{t}%" for t in terms) + alias_args
        sql = ("SELECT m.* FROM mem_atom m WHERE m.persona_id=? AND m.status='active' "
               "AND m.decay>=0.15 AND (m.unread IS NULL OR m.unread=0) AND " + where +
               " ORDER BY m.importance*m.decay DESC, m.id DESC LIMIT 800")
        try:
            rows = await self.store.fetch_all(sql, (persona_id, *args))
        except Exception as exc:
            logger.warning(f"[congling] 全库检索失败: {exc_brief(exc)}")
            return []
        for row in rows:
            row["_broad"] = True
        return rows

    # ---------------------------------------------------------------- BM25
    @staticmethod
    def _bm25(rows: list[dict[str, Any]], terms: list[str]) -> dict[int, float]:
        if not terms:
            return {}
        docs: list[tuple[int, dict[str, int], int]] = []
        for row in rows:
            text = f"{row.get('text') or ''} {row.get('keywords') or ''} {row.get('_alias_terms') or ''}"
            tokens = tokenize(text)
            tokens.extend(str(row.get("_alias_terms") or "").split())
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
        row = await self.store.fetch_one("SELECT * FROM inject_cursor WHERE persona_id=? AND session_id=?",
                                         (self.config.persona_id, session_id))
        return row or {}

    async def save_cursor(
        self, session_id: str, last_ids: list[int], last_msg_row: int | None = None
    ) -> None:
        """记录注入过的原子。

        last_msg_row 是消池水位，只有传了值才写——否则注入会顺手把水位清零，
        池子就会反复重抽同一批消息。
        """
        import json

        persona_id = self.config.persona_id
        now = int(time.time())
        if last_msg_row is None:
            await self.store.execute(
                "INSERT INTO inject_cursor(persona_id, session_id, last_msg_row, last_atom_ids, updated_at) "
                "VALUES(?,?,0,?,?) "
                "ON CONFLICT(persona_id, session_id) DO UPDATE SET last_atom_ids=excluded.last_atom_ids, "
                "updated_at=excluded.updated_at",
                (persona_id, session_id, json.dumps(last_ids), now),
            )
            return
        await self.store.execute(
            "INSERT INTO inject_cursor(persona_id, session_id, last_msg_row, last_atom_ids, updated_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(persona_id, session_id) DO UPDATE SET last_msg_row=excluded.last_msg_row, "
            "last_atom_ids=excluded.last_atom_ids, updated_at=excluded.updated_at",
            (persona_id, session_id, int(last_msg_row), json.dumps(last_ids), now),
        )
