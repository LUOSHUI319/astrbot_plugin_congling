"""做梦。

睡着满一小时开始，之后每小时一段。梦不是凭空编的——是从真实记忆里抽碎片改写，
所以醒了才有得聊。

等级决定它能留多久：一级梦第二天就剩七成，四天忘干净；五级梦能撑一个月。
醒后主动说不说，也是按等级给概率的（1 级 20%，5 级 100%）。
"""

from __future__ import annotations

import json
import random
import re
import time
import uuid
from typing import Any

from astrbot.api import logger

from ..core.contracts import DreamEntry, night_key_of

KV_KEY = "night.dreams"
MAX_KEEP = 200

# 等级 -> 每天压缩比例
LEVEL_DECAY = {
    1: 0.30,
    2: 0.22,
    3: 0.15,
    4: 0.08,
    5: 0.03,
}

BUILTIN_TAGS = ["美梦", "噩梦", "反映现实", "天马行空"]

PROMPT = """你在做梦。下面是可能飘进梦里的碎片：

{fragments}

{continuation}梦的基调：{mood}

写一段梦，300~500 字。要求：
1. 不要求有完整结尾，像是还在继续做下去的梦
2. 可以荒诞、可以跳跃、人物可以变形
3. 用第一人称，像醒来后还记得的那种碎片
4. 不要总结，不要说"这是一个梦"
5. 只输出 JSON，格式：{{"text":"梦境正文","level":1-5,"tags":["标签"]}}
   level：这段梦的情绪强度，1 很淡 / 5 非常强烈（噩梦、极度真实、戳中要害的给高分）
   tags：从 {tags} 里挑 1~2 个，也可以自己起一个更贴切的两字词
"""


class Dreamer:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self._dreams: list[DreamEntry] = []
        self._loaded = False
        self._last_dream_at = 0

    # ================================================================ 开关
    @property
    def enabled(self) -> bool:
        return self.config.bool("night.dream_enabled", False)

    # ================================================================ 存取
    async def load(self) -> None:
        try:
            raw = await self.store.kv_get(KV_KEY, []) or []
        except Exception:
            raw = []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = []
        dreams: list[DreamEntry] = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict):
                try:
                    dreams.append(DreamEntry.from_dict(item))
                except Exception:
                    continue
        # 老数据没有 night_key，按创建时间补一下
        for dream in dreams:
            if not dream.night_key:
                dream.night_key = night_key_of(dream.created_at)
        self._dreams = dreams
        self._loaded = True

    async def save(self) -> None:
        try:
            payload = [dream.to_dict() for dream in self._dreams[-MAX_KEEP:]]
            await self.store.kv_set(KV_KEY, payload)
        except Exception as exc:
            # 这里的失败是「梦做了但没存下来」，面板上看着就是一条都没有，
            # 别再拿 debug 埋掉了
            logger.warning(f"[congling] 保存梦境失败: {exc!r}")

    # ================================================================ 时机
    def due(self, *, sleep_at: int, already: int, now: float | None = None) -> bool:
        """现在该不该做一段梦。"""
        if not self.enabled or sleep_at <= 0:
            return False
        now = now or time.time()
        start_after = max(1, self.config.int("night.dream_start_after_minutes", 60)) * 60
        interval = max(5, self.config.int("night.dream_interval_minutes", 60)) * 60
        max_per_sleep = max(1, self.config.int("night.dream_max_per_sleep", 8))

        if already >= max_per_sleep:
            return False
        if now - sleep_at < start_after:
            return False
        if self._last_dream_at and now - self._last_dream_at < interval:
            return False
        return True

    def noon_roll(self) -> bool:
        """午睡这一次做不做梦，入睡时掷一次。"""
        if not self.enabled:
            return False
        chance = self.config.float("night.noon_dream_chance", 0.5)
        return random.random() < max(0.0, min(1.0, chance))

    # ================================================================ 生成
    async def make(
        self,
        *,
        session_id: str = "",
        mood: str = "",
        continuation: str = "",
    ) -> DreamEntry | None:
        if not self.enabled:
            return None
        # 梦的开关可能是运行中才打开的，_dreams 还没从库里读出来；
        # 这时候直接 append + save 会把以前的梦整段覆盖掉
        if not self._loaded:
            await self.load()

        fragments = await self._pick_fragments()
        if not fragments:
            fragments = ["（今晚没有想起什么具体的事）"]
        else:
            fragments = [f"- {item}" for item in fragments]

        pool = [str(t) for t in (self.config.list("night.dream_tags", []) or []) if str(t).strip()]
        tags = (pool or list(BUILTIN_TAGS)) + [str(t) for t in self.config.list("night.dream_extra_tags", []) or []]
        mood = mood or random.choice(tags or ["天马行空"])

        prompt = PROMPT.format(
            fragments="\n".join(fragments),
            continuation=f"这是刚才那个梦的延续，接着往下写：\n{continuation}\n\n" if continuation else "",
            mood=mood,
            tags="、".join(tags),
        )

        text, level, dream_tags = await self._call_model(prompt, mood)
        if not text:
            return None

        entry = DreamEntry(
            dream_id=f"dream_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            session_id=session_id,
            created_at=int(time.time()),
            level=max(1, min(5, level)),
            tags=dream_tags or [mood],
            text=text,
            importance=min(1.0, 0.2 * max(1, min(5, level))),
            decay_per_day=LEVEL_DECAY.get(max(1, min(5, level)), 0.15),
            strength=1.0,
            night_key=night_key_of(int(time.time())),
        )
        self._dreams.append(entry)
        self._last_dream_at = time.time()
        await self.save()
        logger.info(f"[congling] 做了一段梦：{entry.level} 级 / {'、'.join(entry.tags)}")
        return entry

    async def _call_model(self, prompt: str, mood: str) -> tuple[str, int, list[str]]:
        model = getattr(self.plugin, "models", None)
        if model is None:
            return "", 3, [mood]
        try:
            raw = await model.chat(prompt, system="你是一个正在做梦的人，只输出 JSON。", temperature=0.9, max_tokens=900)
        except Exception as exc:
            logger.debug(f"[congling] 梦境生成失败: {exc!r}")
            return "", 3, [mood]

        text = (raw or "").strip()
        if not text:
            return "", 3, [mood]

        data = _extract_json(text)
        if data:
            body = str(data.get("text") or "").strip()
            try:
                level = int(data.get("level") or 3)
            except (TypeError, ValueError):
                level = 3
            tags = data.get("tags") or []
            tags = [str(t) for t in tags if str(t).strip()][:2]
            if body:
                return body, level, tags

        # 模型没按格式来：把整段当正文，等级按长度和经验估
        return text[:800], 3, [mood]

    async def _pick_fragments(self, limit: int = 5) -> list[str]:
        try:
            rows = await self.store.fetch_all(
                "SELECT text FROM mem_atom WHERE status = 'active' AND decay >= 0.2 "
                "ORDER BY RANDOM() LIMIT ?",
                (max(1, limit),),
            )
        except Exception:
            return []
        # 同上：行是 dict，取 row[0] 会 KeyError，梦就一段也做不出来
        out: list[str] = []
        for row in rows or []:
            value = row.get("text") if isinstance(row, dict) else (row[0] if row else None)
            if value:
                out.append(str(value))
        return out

    # ================================================================ 衰减
    async def daily_tick(self) -> int:
        """每天跑一次：压缩强度，掉到底的标记过期。"""
        if not self._loaded:
            await self.load()
        changed = 0
        for dream in self._dreams:
            if dream.expired or dream.pinned:
                continue
            dream.strength = max(0.0, dream.strength * (1 - max(0.0, dream.decay_per_day)))
            if dream.strength < 0.15:
                dream.expired = True
            changed += 1
        if changed:
            await self.save()
        return changed

    # ================================================================ 查询
    def living(self) -> list[DreamEntry]:
        return [dream for dream in self._dreams if not dream.expired]

    def list_recent(self, limit: int = 20) -> list[DreamEntry]:
        items = list(self._dreams)
        items.sort(key=lambda d: d.created_at, reverse=True)
        return items[:limit]

    def remember_chance(self, level: int) -> float:
        if not self.config.bool("night.dream_remember_by_level", True):
            return 0.4
        return min(1.0, 0.2 * max(0, min(5, level)))

    def pick_to_mention(self) -> DreamEntry | None:
        """醒后挑一段来提：挑等级最高的掷骰。"""
        alive = self.living()
        if not alive:
            return None
        top = max(alive, key=lambda d: (d.level, d.strength))
        if random.random() < self.remember_chance(top.level):
            return top
        return None

    def set_pinned(self, dream_id: str, pinned: bool) -> bool:
        """拽住 / 松开。拽住的梦不再衰减，也不会被忘掉。"""
        for dream in self._dreams:
            if dream.dream_id == dream_id:
                dream.pinned = bool(pinned)
                if pinned:
                    dream.expired = False
                    dream.strength = max(dream.strength, 0.9)
                return True
        return False

    def forget(self, dream_id: str) -> bool:
        for index, dream in enumerate(self._dreams):
            if dream.dream_id == dream_id:
                del self._dreams[index]
                return True
        return False

    def stats(self) -> dict[str, Any]:
        alive = self.living()
        return {
            "total": len(self._dreams),
            "living": len(alive),
            "by_level": {level: sum(1 for d in alive if d.level == level) for level in range(1, 6)},
        }


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里挖出第一个 JSON 对象。"""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
