"""congling → livingmemory 记忆镜像桥。

把 congling 抽取出来的原子记忆，镜像一份到 livingmemory 作为统一事实仓库。
只做「追加写入」，不改 congling 自身的记忆库、好感度、画像、遗忘曲线 —— 那些是
congling 自己的关系状态机，与 livingmemory 的事实存储互不干扰。

设计原则：
- 挂载在 ``bus.ON_MEMORY_WRITTEN`` 信号上（service.py 在 mem_atom 写入成功后 emit），
  主流程零改动，congling 升级也不受影响。
- 每个 draft 按 kind 映射成 livingmemory 的 memory_type（新命名，与前端下拉一致）。
- 幂等：以 (persona_id, text 哈希) 为键，写过的跳过，避免重载插件后重复镜像。
- 一切失败静默降级：livingmemory 没装 / 没就绪 / 写入失败，都不影响 congling 主链路。

kind → memory_type 映射（方案 B）：
  fact     → FACTUAL    客观事实
  pref     → PREFERENCE 偏好
  event    → EPISODIC   发生过的事
  promise  → PLANNED    承诺/约定
  relation → RELATIONAL 人际关系
  emotion  → GENERAL    情绪模式（归入通用）
"""

from __future__ import annotations

import hashlib
from typing import Any

from astrbot.api import logger

from ..core.contracts import MemoryDraft, MemoryHit
from ..core.memory_guard import memory_work
from .refs import session_ref, platform_key, parse_ref

LIVINGMEMORY_PLUGIN = "astrbot_plugin_livingmemory"

# congling kind → livingmemory memory_type
_KIND_TO_TYPE: dict[str, str] = {
    "fact": "FACTUAL",
    "pref": "PREFERENCE",
    "event": "EPISODIC",
    "promise": "PLANNED",
    "relation": "RELATIONAL",
    "emotion": "GENERAL",
}

_SEEN_MAX = 20000


def _text_key(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _memory_type(draft: MemoryDraft) -> str:
    return _KIND_TO_TYPE.get(str(getattr(draft, "kind", "") or "").strip().lower(), "GENERAL")


def _get_lm_engine(plugin: Any) -> Any:
    """拿到 livingmemory 的 memory_engine 实例，拿不到返回 None。

    与 bilibot 的 _engine() 同套路：context.get_registered_star → star_cls →
    initializer.memory_engine。兼容没有 get_registered_star 的旧 host。
    """
    context = getattr(plugin, "context", None)
    if context is None:
        return None
    lm = None
    try:
        meta = context.get_registered_star(LIVINGMEMORY_PLUGIN)
        if meta is not None and getattr(meta, "activated", True) is not False:
            lm = getattr(meta, "star_cls", None)
    except Exception:
        lm = None
    if lm is None:
        # 退回 get_all_stars 扫描（probe.py 里也是这么干的）
        try:
            for star_meta in context.get_all_stars() or []:
                name = str(getattr(star_meta, "root_dir_name", "") or getattr(star_meta, "name", "") or "")
                if name == LIVINGMEMORY_PLUGIN and getattr(star_meta, "activated", True):
                    lm = getattr(star_meta, "star_cls", None)
                    break
        except Exception:
            lm = None
    if lm is None:
        return None
    initializer = getattr(lm, "initializer", None)
    engine = getattr(initializer, "memory_engine", None)
    if engine is None or getattr(engine, "index_writer", None) is None:
        return None
    return engine


class LivingMemoryBridge:
    """订阅 congling 的记忆写入信号，把原子记忆镜像到 livingmemory。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._engine: Any = None
        self._engine_checked = False
        self._enabled = bool(plugin.cfg.bool("bridge_livingmemory.enabled", False))
        self._seen: set[tuple[str, str, str]] = set()

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if not self._enabled:
            logger.info("[congling] livingmemory 镜像桥已关闭")
            return
        self.plugin.bus.subscribe(self.plugin.bus.ON_MEMORY_WRITTEN, self._on_memory_written)
        logger.warning("[congling] livingmemory 镜像桥就绪")

    async def _ensure_engine(self) -> Any:
        if not self._enabled:
            return None
        engine = _get_lm_engine(self.plugin)
        self._engine = engine
        self._engine_checked = True
        if engine is None:
            logger.debug("[congling] livingmemory 引擎不可用，镜像桥降级")
        return engine

    # ------------------------------------------------------------ 事件处理
    @memory_work
    async def _on_memory_written(self, payload: Any) -> None:
        """bus.ON_MEMORY_WRITTEN 的订阅者。payload 是 list[MemoryDraft]。"""
        drafts = payload if isinstance(payload, list) else [payload]
        real_drafts: list[MemoryDraft] = []
        for d in drafts:
            if isinstance(d, MemoryDraft):
                real_drafts.append(d)
        if not real_drafts:
            return
        # 随写入事件执行，参与新版清除闸门，不另起可能在卸载后回写的任务。
        await self._mirror(real_drafts)

    async def _mirror(self, drafts: list[MemoryDraft]) -> None:
        engine = await self._ensure_engine()
        if engine is None:
            return
        persona_id = str(self.plugin.cfg.persona_id or "ling")
        written = 0
        for draft in drafts:
            text = str(getattr(draft, "text", "") or "").strip()
            if not text:
                continue
            where = session_ref(platform_key(draft.platform), draft.session_kind, draft.session_id)
            if not where or draft.subject_key == "__self__":
                continue  # 缺受众信息和私人自述不进入共享镜像。
            key = (persona_id, where, _text_key(text))
            if key in self._seen:
                continue
            try:
                await engine.add_memory(
                    content=text,
                    session_id=str(getattr(draft, "session_id", "") or "") or None,
                    persona_id=persona_id,
                    importance=float(getattr(draft, "importance", 0.5) or 0.5),
                    metadata={
                        "memory_origin": "congling",
                        "memory_type": _memory_type(draft),
                        "congling_kind": str(getattr(draft, "kind", "") or ""),
                        "congling_session_kind": str(getattr(draft, "session_kind", "") or ""),
                        "congling_level": int(getattr(draft, "level", 3) or 3),
                        "congling_subject_key": str(getattr(draft, "subject_key", "") or ""),
                        "congling_persona_id": persona_id,
                        "congling_session_ref": where,
                    },
                )
                self._seen.add(key)
                written += 1
                if len(self._seen) > _SEEN_MAX:
                    self._seen.clear()
            except Exception as exc:
                logger.debug(f"[congling] 镜像到 livingmemory 失败: {exc!r}")
        if written:
            logger.warning(f"[congling] 镜像 {written} 条记忆到 livingmemory")

    # ------------------------------------------------------------ 检索（读）
    async def search(self, query: str, k: int = 5, *, where_ref: str = "") -> list[MemoryHit]:
        """从 livingmemory 检索记忆，构造成 congling 的 MemoryHit。

        供 MemoryService.inject 在「让位」状态下调用，替代 congling 自己的
        retriever 检索。检索失败 / 引擎不可用一律返回空列表，不阻断回复。
        """
        if not parse_ref(where_ref, "session"):
            return []
        engine = await self._ensure_engine()
        if engine is None:
            return []
        query = (query or "").strip()
        if not query:
            return []
        try:
            results = await engine.search_memories(query=query, k=max(1, min(int(k), 10)))
        except Exception as exc:
            logger.debug(f"[congling] livingmemory 检索失败: {exc!r}")
            return []
        hits: list[MemoryHit] = []
        for r in results or []:
            content = str(getattr(r, "content", "") or "").strip()
            if not content:
                continue
            md = getattr(r, "metadata", None) or {}
            # 不接受外部库未标注受众的旧记录，更不做全库跨私聊召回。
            if (not isinstance(md, dict)
                    or md.get("congling_session_ref") != where_ref
                    or md.get("congling_persona_id") != self.plugin.cfg.persona_id
                    or md.get("memory_origin") != "congling"):
                continue
            try:
                importance = float(md.get("importance", 0.5) or 0.5)
            except (TypeError, ValueError):
                importance = 0.5
            try:
                created_at = int(md.get("create_time", 0) or md.get("created_at", 0) or 0)
            except (TypeError, ValueError):
                created_at = 0
            hits.append(
                MemoryHit(
                    atom_id=0,  # livingmemory 的 doc 没有 congling 的 atom_id，置 0
                    text=content,
                    kind=str(md.get("memory_type", "fact") or "fact").lower(),
                    importance=importance,
                    score=float(getattr(r, "final_score", 0.0) or 0.0),
                    created_at=created_at,
                    session_label=str(md.get("memory_origin", "") or ""),
                )
            )
        return hits


def create_bridge(plugin: Any) -> LivingMemoryBridge:
    return LivingMemoryBridge(plugin)
