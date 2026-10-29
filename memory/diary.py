"""她自己的日记（0.7.0 · 第 6 条）。

不是给用户看的报告，是"她今天记下了什么"。
素材只能来自真实记忆、真实梦境、真实作息与好感度——不许编具体事件，
这也是为什么每篇都要记 refs（引了哪些 atom / 哪段梦）。

一句话设计：能写就写，写不出来就不写；格式坏了也不能丢正文。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Any

from astrbot.api import logger

MOODS = ["安静", "雀跃", "低落", "疲惫", "温柔"]

# 好感度档位 -> 语气（0-1 客气 / 2-3 平视 / 4-5 黏）
STAGE_TONE = {
    0: "很客气，像在跟不太熟的人说话，不敢点名",
    1: "客气，留着一层距离",
    2: "平视，会提到「有个人」，但不说名字",
    3: "平视，可以说出对方名字，语气松一点",
    4: "亲近，会直呼其名，短句多，有点黏",
    5: "非常亲近，写的时候一直在想这个人",
}

PROMPT = """现在是{day}的晚上。你是{name}{identity_line}，你要写一篇日记。
只写给自己看，不打算给谁检查。

今天留下的东西（每行开头是类型，方括号里「关于 X」表示那条事实的主体是 X，
不是你；没有标注的就是泛指或你自己的状态）：
{materials}

{affinity_block}

要求：
1. 第一人称，像晚上趴在被窝里写的，一共 {length} 字上下
1.1 视角不能颠倒：「他答应给我买」不能写成「我答应给他买」；素材没写清谁做的，就模糊处理，别替自己认领
2. 只能基于上面这些真实发生过的事来写，不要编造具体事件、不要编对话
3. 可以写感受、写没说完的话、写突然想起来的一件小事
4. 分段自然，不要标题式的小标题，不要列表，不要 emoji
5. 只输出 JSON：{{"title":"不超过 12 字","text":"正文","mood":"{mood_list} 里挑一个","tags":["两到四个字","再来一个"],"mention":true 或 false}}
   mention 表示正文里有没有写到某个人
"""


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def day_key_of(ts: float | int | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts else time.time()))


def _day_bounds(day_key: str) -> tuple[int, int]:
    try:
        start = int(time.mktime(time.strptime(day_key, "%Y-%m-%d")))
    except (TypeError, ValueError):
        start = int(time.time())
    return start, start + 86400 - 1


def _guess_mood(text: str) -> str:
    if not text:
        return "安静"
    table = {
        "雀跃": ("开心", "高兴", "笑", "好耶", "喜欢", "期待"),
        "低落": ("难过", "失望", "算了", "没意思", "空", "闷"),
        "疲惫": ("累", "困", "撑不住", "眼睛", "熬夜", "乏"),
        "温柔": ("谢谢", "陪", "轻声", "抱", "暖", "慢慢"),
    }
    for mood, words in table.items():
        if any(word in text for word in words):
            return mood
    return "安静"


class Diary:
    """一天一篇。写不出来就不写，格式坏了也留正文。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self._lock = asyncio.Lock()
        self._last_attempt = 0.0

    # ================================================================ 开关
    @property
    def enabled(self) -> bool:
        return self.config.bool("diary.enabled", False)

    def _hour(self) -> int:
        return max(0, min(23, self.config.int("diary.hour", 23)))

    def _jitter(self, day_key: str) -> int:
        """每天的抖动固定（同一天多跳几次不会重复计算）。"""
        span = max(0, min(180, self.config.int("diary.jitter_minutes", 40)))
        if not span:
            return 0
        seed = sum(ord(ch) for ch in day_key)
        return seed % (span + 1)

    # ================================================================ 调度
    async def due(self, now: float | None = None) -> bool:
        if not self.enabled:
            return False
        now = now or time.time()
        moment = time.localtime(now)
        day_key = day_key_of(now)
        if moment.tm_hour < self._hour():
            return False
        if moment.tm_hour == self._hour() and moment.tm_min < self._jitter(day_key):
            return False
        return not await self.exists(day_key)

    async def tick(self) -> None:
        try:
            if not await self.due():
                return
            # 一小时最多试一次，别每分钟都叫模型
            if time.time() - self._last_attempt < 3600:
                return
            self._last_attempt = time.time()
            await self.compose()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[congling] 日记 tick 失败: {exc!r}")

    async def backfill(self) -> None:
        """启动时补昨天那一篇，最多补一天。"""
        if not self.enabled or not self.config.bool("diary.backfill", True):
            return
        yesterday = day_key_of(time.time() - 86400)
        if await self.exists(yesterday):
            return
        try:
            if not await self._has_atoms(yesterday):
                return
            await asyncio.sleep(45)  # 别跟启动抢模型
            await self.compose(day_key=yesterday)
            logger.info("[congling] 补写了昨天那篇日记")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[congling] 补写日记失败: {exc!r}")

    # ================================================================ 生成
    async def compose(self, day_key: str | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        day_key = day_key or day_key_of()
        async with self._lock:
            if await self.exists(day_key):
                return await self.get(day_key)
            bundle = await self._gather(day_key)
            if bundle is None:
                return None
            if not bundle["materials"]:
                if self.config.bool("diary.skip_when_empty", True):
                    logger.info(f"[congling] {day_key} 没什么可记的，今天不写")
                    return None
            result = await self._compose_by_model(day_key, bundle)
            if result is None:
                return None
            diary_id = await self._insert(day_key, result, bundle)
            result["id"] = diary_id
            result["day_key"] = day_key
            logger.info(f"[congling] 写了一篇日记：{day_key} / {result.get('mood') or '安静'}")
            return result

    async def _compose_by_model(self, day_key: str, bundle: dict[str, Any]) -> dict[str, Any] | None:
        # 名字和身份必须写进 prompt：只写"第一人称"模型不知道"我"是谁，
        # 容易把别人对她的称呼当成自己的名字，或者写出"作为AI"这种出戏的话。
        name = self.config.str("persona.name", "绫") or "绫"
        try:
            book = getattr(self.plugin, "persona", None)
            if book is not None and hasattr(book, "display_name"):
                name = await book.display_name() or name
        except Exception:
            pass
        identity = self.config.str("persona.identity", "").strip()
        identity_line = f"（{identity[:60]}）" if identity else ""

        prompt = PROMPT.format(
            day=day_key,
            name=name,
            identity_line=identity_line,
            materials=bundle["materials"],
            affinity_block=bundle["affinity_block"],
            length=max(200, min(1500, self.config.int("diary.length", 700))),
            mood_list=" / ".join(MOODS),
        )
        model = getattr(self.plugin, "models", None)
        raw = ""
        if model is not None:
            try:
                raw = await model.chat(
                    prompt,
                    system="你是一个在写日记的人，只输出 JSON。",
                    temperature=0.9,
                    max_tokens=1600,
                )
            except Exception as exc:
                logger.debug(f"[congling] 日记生成失败: {exc!r}")
                return None
        text = (raw or "").strip()
        if not text:
            return None

        data = _extract_json(text)
        if data:
            body = str(data.get("text") or "").strip()
            if body:
                mood = str(data.get("mood") or "").strip() or _guess_mood(body)
                tags = [str(t) for t in (data.get("tags") or []) if str(t).strip()][:4]
                return {
                    "title": str(data.get("title") or "")[:24].strip(),
                    "text": body,
                    "mood": mood if mood in MOODS else _guess_mood(body),
                    "tags": tags,
                    "mention": bool(data.get("mention")),
                }
        # 没按格式来：整段当正文，别因为 JSON 坏掉丢掉一篇
        return {
            "title": "",
            "text": text[:4000],
            "mood": _guess_mood(text),
            "tags": [],
            "mention": bool(bundle.get("subject_name") and bundle["subject_name"] in text),
        }

    # ================================================================ 素材
    async def _gather(self, day_key: str) -> dict[str, Any] | None:
        start, end = _day_bounds(day_key)
        atoms = await self._atoms_between(start, end)
        if not atoms:
            if self.config.bool("diary.skip_when_empty", True):
                return None
            atoms = await self._atoms_between(end - 72 * 3600, end)

        picked = self._pick_atoms(atoms)
        dreams = await self._dreams_for(day_key)
        state = self._night_state()
        subject_key, subject_name, stage, tone = await self._affinity(picked)

        lines: list[str] = []
        for atom in picked:
            kind = str(atom.get("kind") or "fact")
            # 带上主体：不标的话模型分不清这句是"对他做的事"还是"他对我说的话"，
            # 日记里就会出现"我答应给他充值"这种视角颠倒。
            subject = str(atom.get("subject_id") or "").strip()
            head = f"[{kind}]" + (f"[关于 {subject}]" if subject else "")
            lines.append(f"- {head} {str(atom.get('text') or '').strip()}")
        if dreams:
            for dream in dreams:
                lines.append(f"- [梦] {dream}")
        if state:
            lines.append(f"- [状态] {state}")

        allow_name = (
            self.config.bool("diary.affinity_aware", True)
            and stage >= self.config.int("diary.min_stage_to_name", 3)
        )
        affinity_block = ""
        if self.config.bool("diary.affinity_aware", True):
            affinity_block = (
                f"写的时候对某个人的态度：{tone}。"
                + (f"可以写出名字：{subject_name}。" if allow_name and subject_name else "不要写具体名字。")
            )
        return {
            "materials": "\n".join(lines),
            "atoms": picked,
            "dreams": dreams,
            "affinity_block": affinity_block,
            "subject_key": subject_key if allow_name else "",
            "subject_name": subject_name if allow_name else "",
            "stage": stage,
            "score": float(getattr(self, "_last_score", 0.0) or 0.0),
        }

    def _pick_atoms(self, atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按 kind 分桶，每桶抽 1~3 条，总数不超过 max_atoms。"""
        cap = max(1, min(40, self.config.int("diary.max_atoms", 12)))
        buckets: dict[str, list[dict[str, Any]]] = {}
        for atom in atoms:
            buckets.setdefault(str(atom.get("kind") or "fact"), []).append(atom)
        picked: list[dict[str, Any]] = []
        for items in buckets.values():
            take = min(len(items), random.randint(1, 3))
            picked.extend(random.sample(items, take))
        random.shuffle(picked)
        return picked[:cap]

    async def _dreams_for(self, day_key: str) -> list[str]:
        if not self.config.bool("diary.use_dreams", True):
            return []
        night = getattr(self.plugin, "night", None)
        dreamer = getattr(night, "dreamer", None)
        if dreamer is None:
            return []
        try:
            recent = dreamer.list_recent(40)
        except Exception:
            return []
        yday = day_key_of(
            time.mktime(time.strptime(day_key, "%Y-%m-%d")) - 3600
        )
        wanted = {day_key, yday}
        hits = [d for d in recent if str(getattr(d, "night_key", "")) in wanted]
        if not hits:
            return []
        sample = random.sample(hits, min(len(hits), random.randint(1, 2)))
        out = []
        for dream in sample:
            text = str(getattr(dream, "text", "") or "").strip()
            if text:
                out.append(text[:200])
        return out

    def _night_state(self) -> str:
        night = getattr(self.plugin, "night", None)
        state = getattr(getattr(night, "state", None), "state", None)
        if state is None:
            return ""
        try:
            bits = []
            if state.sleep_kind:
                bits.append("午睡过" if state.sleep_kind == "noon" else "夜里睡了一觉")
            bits.append(f"精力 {float(state.energy):.0f}")
            if state.dream_count_today:
                bits.append(f"做了 {state.dream_count_today} 段梦")
            return "，".join(bits)
        except Exception:
            return ""

    async def _affinity(self, atoms: list[dict[str, Any]]) -> tuple[str, str, int, str]:
        counts: dict[str, int] = {}
        for atom in atoms:
            key = str(atom.get("subject_id") or "")
            if key:
                counts[key] = counts.get(key, 0) + 1
        if not counts:
            return "", "", 0, STAGE_TONE[0]
        key = max(counts.items(), key=lambda kv: kv[1])[0]
        memory = getattr(self.plugin, "memory", None)
        affinity = getattr(memory, "affinity", None)
        if affinity is None:
            return key, "", 0, STAGE_TONE[0]
        try:
            data = await affinity.get(key)
        except Exception:
            return key, "", 0, STAGE_TONE[0]
        try:
            stage = max(0, min(5, int(data.get("stage") or 0)))
        except (TypeError, ValueError):
            stage = 0
        score = float(data.get("score") or 0.0)
        # 名字从主体库拿；拿不到就只是「有个人」，绝不拿 QQ 号当名字写进日记
        name = ""
        try:
            row = await self.store.fetch_one(
                "SELECT display_name FROM mem_subject WHERE persona_id=? AND user_id=?",
                (self.config.persona_id, key),
            )
            name = str((row or {}).get("display_name") or "").strip()
        except Exception:
            name = ""
        self._last_score = score
        return key, name, stage, STAGE_TONE.get(stage, STAGE_TONE[0])

    async def _atoms_between(self, start: int, end: int) -> list[dict[str, Any]]:
        try:
            rows = await self.store.fetch_all(
                "SELECT id, kind, text, subject_id, mem_level, importance FROM mem_atom "
                "WHERE status='active' AND (unread IS NULL OR unread=0) "
                "AND created_at BETWEEN ? AND ? "
                "ORDER BY mem_level DESC, importance DESC, id DESC LIMIT 200",
                (int(start), int(end)),
            )
        except Exception as exc:
            logger.debug(f"[congling] 取日记素材失败: {exc!r}")
            return []
        return [dict(row) for row in rows]

    async def _has_atoms(self, day_key: str) -> bool:
        start, end = _day_bounds(day_key)
        try:
            value = await self.store.scalar(
                "SELECT COUNT(*) FROM mem_atom WHERE status='active' "
                "AND (unread IS NULL OR unread=0) AND created_at BETWEEN ? AND ?",
                (int(start), int(end)),
                0,
            )
        except Exception:
            return False
        return int(value or 0) > 0

    # ================================================================ 存取
    async def _insert(self, day_key: str, result: dict[str, Any], bundle: dict[str, Any]) -> int:
        refs = [int(a.get("id") or 0) for a in bundle.get("atoms", []) if int(a.get("id") or 0)]
        persona = self.config.persona_id
        try:
            await self.store.execute(
                "INSERT OR IGNORE INTO mem_diary(persona_id, day_key, created_at, title, text, mood, "
                "tags, refs, affinity_subject, affinity_score) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    persona,
                    day_key,
                    int(time.time()),
                    str(result.get("title") or ""),
                    str(result.get("text") or ""),
                    str(result.get("mood") or ""),
                    json.dumps(result.get("tags") or [], ensure_ascii=False),
                    json.dumps(refs, ensure_ascii=False),
                    str(bundle.get("subject_key") or ""),
                    float(bundle.get("score") or 0.0),
                ),
            )
        except Exception as exc:
            logger.warning(f"[congling] 日记入库失败: {exc!r}")
            return 0
        row = await self.get(day_key)
        return int((row or {}).get("id") or 0)

    async def exists(self, day_key: str) -> bool:
        row = await self.get(day_key)
        return bool(row)

    async def get(self, day_key: str) -> dict[str, Any] | None:
        persona = self.config.persona_id
        try:
            row = await self.store.fetch_one(
                "SELECT * FROM mem_diary WHERE persona_id=? AND day_key=?",
                (persona, day_key),
            )
        except Exception:
            return None
        return self._shape(row) if row else None

    async def list(self, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
        persona = self.config.persona_id
        try:
            rows = await self.store.fetch_all(
                "SELECT * FROM mem_diary WHERE persona_id=? AND hidden=0 "
                "ORDER BY pinned DESC, day_key DESC LIMIT ? OFFSET ?",
                (persona, max(1, min(200, limit)), max(0, offset)),
            )
        except Exception as exc:
            logger.debug(f"[congling] 日记列表失败: {exc!r}")
            return []
        return [self._shape(dict(row)) for row in rows]

    async def detail(self, diary_id: int) -> dict[str, Any] | None:
        try:
            row = await self.store.fetch_one("SELECT * FROM mem_diary WHERE id=?", (int(diary_id),))
        except Exception:
            return None
        return self._shape(row) if row else None

    async def delete(self, diary_id: int) -> bool:
        try:
            await self.store.execute("DELETE FROM mem_diary WHERE id=?", (int(diary_id),))
            return True
        except Exception:
            return False

    async def pin(self, diary_id: int, on: bool) -> bool:
        try:
            await self.store.execute(
                "UPDATE mem_diary SET pinned=? WHERE id=?", (1 if on else 0, int(diary_id))
            )
            return True
        except Exception:
            return False

    async def latest_snippet(self, length: int = 200) -> str:
        """最近一篇的前若干字，给「让日记影响说话」那个开关用。"""
        rows = await self.list(1, 0)
        if not rows:
            return ""
        text = str(rows[0].get("text") or "").strip()
        return text[: max(40, length)]

    def _shape(self, row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for field, default in (("tags", []), ("refs", [])):
            try:
                value = json.loads(item.get(field) or "[]")
            except Exception:
                value = default
            item[field] = value if isinstance(value, list) else default
        created = int(item.get("created_at") or 0)
        item["created_text"] = time.strftime("%H:%M", time.localtime(created)) if created else ""
        day_key = str(item.get("day_key") or "")
        try:
            stamp = time.strptime(day_key, "%Y-%m-%d")
            week = "一二三四五六日"[stamp.tm_wday]
            item["day_label"] = f"{stamp.tm_mon}-{stamp.tm_mday:02d} 周{week}"
        except (TypeError, ValueError):
            item["day_label"] = day_key
        item["chars"] = len(str(item.get("text") or ""))
        return item
