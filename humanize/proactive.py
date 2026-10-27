"""主动找人 / 主动补一句（0.7.0 · 第 11 条）。

两件事必须先在脑子里定死，不然写出来一定是错的：

1. **插件别的模块没有任何主动发送通道。** 长夜的道别语是塞进下一条回复带出去的，
   不是主动发。要主动说话只能 `context.send_message(umo, chain)`，
   它绕过 `on_decorating_result`——所以自己分句、自己 sleep，
   节奏控制（F6/rhythm）对这条消息完全不生效。
2. **`msg_archive.session_id` 不是 umo**，里面只有群号或 QQ 号。
   umo 在归档时一并存（老数据为空就按 platform + kind + id 拼）。

判定顺序刻意写成「先硬否决、再打分」：睡着、静默时段、额度用完这些不该靠分数兜，
一旦靠分数，某天调了阈值就会在凌晨三点冒一句。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from astrbot.api import logger

KV_SESSIONS = "proactive.sessions"
KV_GLOBAL = "proactive.global"
KV_STATE = "proactive.state"

TEXT_PROMPT = """现在没人跟你说话，你想主动找对方说一句。

{scene}

手上的材料：
{materials}

要求：
1. 第一人称、口语，像突然想到什么就说一句，{length} 字以内
2. 不许客套，不许「在吗」「有什么可以帮你」「需要我做什么」
3. 可以点具体的事（只能是上面材料里有的），不许编造
4. 只输出这句话本身，不要引号，不要解释
"""


def _in_spec(spec: str, now: float | None = None) -> bool:
    """判断现在在不在 "HH:MM-HH:MM" 里。跨午夜也认。"""
    text = str(spec or "").strip()
    if not text or "-" not in text:
        return True
    try:
        left, right = text.split("-", 1)
        sh, sm = [int(x) for x in left.strip().split(":")]
        eh, em = [int(x) for x in right.strip().split(":")]
    except (ValueError, AttributeError):
        return True
    moment = time.localtime(now or time.time())
    current = moment.tm_hour * 60 + moment.tm_min
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= current < end
    return current >= start or current < end


class Proactive:
    """闲下来太久就自己找上来的人。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self._lock = asyncio.Lock()
        self._boot_at = time.time()
        self._last_attempt = 0.0
        self._sessions: dict[str, dict[str, Any]] = {}
        self._global: dict[str, Any] = {}
        self._loaded = False

    # ================================================================ 状态
    async def load(self) -> None:
        if self._loaded:
            return
        try:
            raw = await self.store.kv_get(KV_SESSIONS, {}) or {}
            self._sessions = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
        except Exception:
            self._sessions = {}
        try:
            raw = await self.store.kv_get(KV_GLOBAL, {}) or {}
            self._global = dict(raw) if isinstance(raw, dict) else {}
        except Exception:
            self._global = {}
        self._loaded = True

    async def save(self) -> None:
        try:
            await self.store.kv_set(KV_SESSIONS, self._sessions)
            await self.store.kv_set(KV_GLOBAL, self._global)
        except Exception as exc:
            logger.debug(f"[congling] 主动状态保存失败: {exc!r}")

    def enabled(self) -> bool:
        return self.config.bool("proactive.enabled", False)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.int(f"proactive.{key}", default))
        except Exception:
            return default

    def _cfg_str(self, key: str, default: str = "") -> str:
        try:
            return str(self.config.str(f"proactive.{key}", default) or default)
        except Exception:
            return default

    def _sess(self, session_id: str) -> dict[str, Any]:
        item = self._sessions.setdefault(session_id, {})
        item.setdefault("last_at", 0)
        item.setdefault("unanswered", 0)
        item.setdefault("next_ok_at", 0)
        item.setdefault("muted_until", 0)
        item.setdefault("sent_today", 0)
        item.setdefault("day", "")
        return item

    # ================================================================ 候选
    async def candidates(self, now: float | None = None) -> list[dict[str, Any]]:
        now = now or time.time()
        idle_min = max(5, self._cfg_int("idle_min_minutes", 90)) * 60
        idle_max = max(1, self._cfg_int("idle_max_hours", 48)) * 3600
        try:
            rows = await self.store.fetch_all(
                "SELECT session_id, session_kind, MAX(ts) AS last_ts, COUNT(*) AS cnt, "
                "MAX(umo) AS umo, MAX(platform) AS platform, MAX(sender_name) AS who "
                "FROM msg_archive WHERE ts >= ? GROUP BY session_id, session_kind "
                "ORDER BY last_ts DESC LIMIT 120",
                (int(now - idle_max),),
            )
        except Exception as exc:
            logger.debug(f"[congling] 取主动候选失败: {exc!r}")
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            last_ts = int(item.get("last_ts") or 0)
            idle = now - last_ts
            if idle < idle_min:
                continue
            item["idle_seconds"] = int(idle)
            item["idle_text"] = self._human(idle)
            item["umo"] = str(item.get("umo") or "")
            item["platform"] = str(item.get("platform") or "")
            out.append(item)
        return out

    async def sessions_view(self) -> list[dict[str, Any]]:
        """给面板：候选 + 冷却 + 未回复数 + 单会话静音。"""
        await self.load()
        now = time.time()
        rows = await self.candidates(now)
        out = []
        for row in rows:
            state = self._sess(str(row["session_id"]))
            out.append({
                "session_id": str(row["session_id"]),
                "session_kind": str(row.get("session_kind") or "group"),
                "label": str(row.get("who") or row["session_id"]),
                "idle_text": row.get("idle_text") or "",
                "idle_seconds": int(row.get("idle_seconds") or 0),
                "last_at": int(state.get("last_at") or 0),
                "unanswered": int(state.get("unanswered") or 0),
                "next_ok_at": int(state.get("next_ok_at") or 0),
                "muted_until": int(state.get("muted_until") or 0),
                "sent_today": int(state.get("sent_today") or 0),
            })
        return out

    # ================================================================ 判定
    async def should(self, row: dict[str, Any], now: float | None = None) -> tuple[bool, str, float]:
        """该不该在这个会话主动说一句。返回 (可以, 原因, 分数)。"""
        now = now or time.time()
        await self.load()
        session_id = str(row.get("session_id") or "")
        kind = str(row.get("session_kind") or "group")
        if not session_id:
            return False, "没有会话号", 0.0

        # ---- 硬否决：这些不该靠分数兜
        if kind == "group" and not self.config.bool("proactive.allow_group", False):
            return False, "群聊默认不主动", 0.0
        if _in_spec(self._cfg_str("quiet_hours", "01:00-08:00"), now):
            return False, "免打扰时段", 0.0
        active = self._cfg_str("active_hours", "") or self.config.str("persona.active_hours", "09:00-03:30")
        if not _in_spec(active, now):
            return False, "不在活跃时段", 0.0
        night = getattr(self.plugin, "night", None)
        if self.config.bool("proactive.respect_night", True) and night is not None:
            state_obj = getattr(getattr(night, "state", None), "state", None)
            mode = str(getattr(state_obj, "mode", "") or "")
            if mode in ("asleep", "waking"):
                return False, "她在睡/刚醒", 0.0
            try:
                energy = float(getattr(state_obj, "energy", 100.0))
            except (TypeError, ValueError):
                energy = 100.0
            if energy < self._cfg_int("energy_min", 40):
                return False, "精力不够", 0.0

        state = self._sess(session_id)
        if state.get("muted_until", 0) > now:
            return False, "这个会话被静音了", 0.0
        if state.get("next_ok_at", 0) > now:
            return False, "还在冷却", 0.0

        # ---- 额度
        day_key = time.strftime("%Y-%m-%d", time.localtime(now))
        if state.get("day") != day_key:
            state["day"] = day_key
            state["sent_today"] = 0
        self._roll_day(day_key, now)
        if int(self._global.get("sent_today") or 0) >= self._cfg_int("daily_cap", 6):
            return False, "今天说够了", 0.0
        if int(state.get("sent_today") or 0) >= self._cfg_int("per_session_cap", 2):
            return False, "这个会话今天说够了", 0.0

        unanswered = int(state.get("unanswered") or 0)
        if unanswered >= self._cfg_int("unanswered_limit", 2):
            return False, "连着两条没被回，先退避", 0.0

        # ---- 打分
        score = 0.0
        signals: list[str] = []
        idle_minutes = float(row.get("idle_seconds") or 0) / 60.0
        idle_score = min(45.0, max(0.0, (idle_minutes - self._cfg_int("idle_min_minutes", 90)) * 0.25))
        if idle_score:
            score += idle_score
            signals.append(f"空闲{idle_minutes:.0f}分{idle_score:+.0f}")

        if kind == "private":
            stage = 0
            memory = getattr(self.plugin, "memory", None)
            affinity = getattr(memory, "affinity", None)
            if affinity is not None:
                try:
                    data = await affinity.get(session_id)
                    stage = int(data.get("stage") or 0)
                except Exception:
                    stage = 0
            if stage < self._cfg_int("min_affinity_stage", 1):
                return False, "还不够熟（好感度档位）", 0.0
            if stage >= 4:
                score += 25.0
                signals.append("很熟了+25")
            elif stage >= 2:
                score += 15.0
                signals.append("认识+15")

        if unanswered:
            score -= 20.0 * unanswered
            signals.append(f"没回{-20 * unanswered:+.0f}")

        group_silence = self._cfg_int("group_silence_minutes", 30) * 60
        if kind == "group":
            if float(row.get("idle_seconds") or 0) < group_silence:
                return False, "群里刚热闹过", 0.0
            score -= 10.0
            signals.append("群聊-10")

        hour = time.localtime(now).tm_hour
        if hour >= 1 and hour < 8:
            score *= 0.6
            signals.append("深夜×0.6")

        warm = await self._promise_warm(session_id)
        if warm:
            score += 20.0
            signals.append("有没兑现的事+20")

        score += random.uniform(-15.0, 15.0)
        score = round(score, 1)
        return score >= 60.0, "、".join(signals) or "没信号", score

    def _roll_day(self, day_key: str, now: float) -> None:
        if self._global.get("day") != day_key:
            self._global["day"] = day_key
            self._global["sent_today"] = 0

    async def _promise_warm(self, session_id: str) -> bool:
        """有没有「答应了还没做」的事，或者上一轮以问句收尾。"""
        if not self.config.bool("proactive.promise_nudge", True):
            return False
        try:
            row = await self.store.fetch_one(
                "SELECT COUNT(*) AS c FROM mem_atom WHERE status='active' AND kind='event' "
                "AND (unread IS NULL OR unread=0) AND (text LIKE '%答应%' OR text LIKE '%说好%' "
                "OR text LIKE '%下次%' OR text LIKE '%回头%') AND created_at >= ?",
                (int(time.time() - 7 * 86400),),
            )
            if row and int(row["c"] or 0) > 0:
                return True
        except Exception:
            pass
        try:
            last = await self.store.fetch_one(
                "SELECT text FROM msg_archive WHERE session_id=? AND ts >= ? "
                "ORDER BY ts DESC LIMIT 1",
                (session_id, int(time.time() - 6 * 3600)),
            )
        except Exception:
            return False
        text = str((last or {}).get("text") or "")
        return text.rstrip().endswith(("?", "？", "吗", "呢"))

    # ================================================================ 生成
    async def compose(self, row: dict[str, Any], *, reason: str = "idle") -> str:
        session_id = str(row.get("session_id") or "")
        length = max(10, min(120, self._cfg_int("length", 40)))
        scene_bits = [f"现在 {time.strftime('%H:%M', time.localtime())}"]
        if reason == "after_wake":
            scene_bits.append("你刚睡醒，想先跟人说一句刚看到的消息")
        elif reason == "unanswered":
            scene_bits.append("对方上一句你没接，过一会儿补一句")
        elif reason == "promise":
            scene_bits.append("你想起之前答应过对方一件事")
        else:
            scene_bits.append("你有点想找对方说话，但没什么特别的理由")

        materials: list[str] = []
        try:
            recent = await self.store.fetch_all(
                "SELECT sender_name, text, ts FROM msg_archive WHERE session_id=? "
                "ORDER BY ts DESC LIMIT 6",
                (session_id,),
            )
            for item in reversed(recent):
                who = str(item.get("sender_name") or "对方")
                what = str(item.get("text") or "").strip()
                if what:
                    materials.append(f"- {who}：{what[:120]}")
        except Exception:
            pass
        try:
            atoms = await self.store.fetch_all(
                "SELECT text FROM mem_atom WHERE status='active' AND (unread IS NULL OR unread=0) "
                "AND importance >= 0.6 ORDER BY importance DESC, id DESC LIMIT 3"
            )
            for atom in atoms:
                materials.append(f"- 记得：{str(atom.get('text') or '')[:120]}")
        except Exception:
            pass
        if not materials:
            return ""

        prompt = TEXT_PROMPT.format(
            scene="；".join(scene_bits),
            materials="\n".join(materials),
            length=length,
        )
        model = getattr(self.plugin, "models", None)
        if model is None:
            return ""
        try:
            raw = await model.chat(
                prompt,
                system="你现在想主动跟人说一句话，只输出这句话。",
                temperature=0.9,
                max_tokens=200,
            )
        except Exception as exc:
            logger.debug(f"[congling] 主动消息生成失败: {exc!r}")
            return ""
        text = (raw or "").strip().strip('"“”')
        text = text.split("\n")[0].strip() if text else ""
        if not text:
            return ""
        if len(text) > length * 2:
            text = text[: length * 2]
        # 跟上一条太像就别发了，重复一句比不说话更怪
        try:
            last = await self.store.fetch_one(
                "SELECT text FROM msg_archive WHERE session_id=? AND sender_id='bot' "
                "ORDER BY ts DESC LIMIT 1",
                (session_id,),
            )
            prev = str((last or {}).get("text") or "")
        except Exception:
            prev = ""
        if prev and prev.strip() == text:
            logger.debug("[congling] 主动消息与上次重复，放弃")
            return ""
        return text

    # ================================================================ 发送
    def _umo_of(self, row: dict[str, Any]) -> str:
        umo = str(row.get("umo") or "").strip()
        if umo and ":" in umo:
            return umo
        platform = str(row.get("platform") or "").strip() or self._cfg_str("platform", "aiocqhttp")
        kind = "GroupMessage" if str(row.get("session_kind") or "") == "group" else "FriendMessage"
        return f"{platform}:{kind}:{row.get('session_id')}"

    async def send_one(self, row: dict[str, Any], *, reason: str = "manual") -> dict[str, Any]:
        """真发一条。成功返回 {ok, text}；失败不抛。"""
        session_id = str(row.get("session_id") or "")
        if not session_id:
            return {"ok": False, "error": "没有会话号"}
        async with self._lock:
            text = await self.compose(row, reason=reason)
            if not text:
                return {"ok": False, "error": "这次没生成出来"}
            umo = self._umo_of(row)
            context = getattr(self.plugin, "context", None)
            if context is None or not hasattr(context, "send_message"):
                return {"ok": False, "error": "拿不到发送通道"}

            composer = getattr(self.plugin, "composer", None)
            try:
                parts = composer.split(text, str(row.get("session_kind") or "private")) if composer else [text]
            except Exception:
                parts = [text]
            parts = [p for p in parts if p.strip()][:3]

            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain

            sent: list[str] = []
            for index, part in enumerate(parts):
                if index:
                    await asyncio.sleep(random.uniform(0.8, 2.2))
                ok = False
                for attempt in range(2):
                    try:
                        await context.send_message(umo, MessageChain([Plain(text=part)]))
                        ok = True
                        break
                    except Exception as exc:
                        logger.debug(f"[congling] 主动发送失败({attempt + 1}/2): {exc!r}")
                        await asyncio.sleep(1.5)
                if not ok:
                    state = self._sess(session_id)
                    state["next_ok_at"] = int(time.time() + 3600)
                    await self.save()
                    return {"ok": False, "error": "发送失败"}
                sent.append(part)

            now = int(time.time())
            state = self._sess(session_id)
            state["last_at"] = now
            state["sent_today"] = int(state.get("sent_today") or 0) + 1
            state["next_ok_at"] = now + max(10, self._cfg_int("cooldown_minutes", 120)) * 60
            self._global["sent_today"] = int(self._global.get("sent_today") or 0) + 1
            await self.save()

            await self._archive(session_id, umo, " ".join(sent), str(row.get("session_kind") or "private"))
            await self._log(session_id, umo, reason, " ".join(sent))
            await self._bump_affinity(session_id)
            logger.info(f"[congling] 主动说了一句（{reason}）：{session_id} / {len(sent)} 段")
            return {"ok": True, "text": " ".join(sent), "umo": umo}

    async def _archive(self, session_id: str, umo: str, text: str, kind: str) -> None:
        """主动消息也要进归档，否则下一轮上下文里她不知道自己刚说过话。"""
        try:
            platform = umo.split(":", 1)[0] if ":" in umo else ""
            await self.store.execute(
                "INSERT INTO msg_archive(msg_id, session_id, session_kind, sender_id, sender_name, ts, text, "
                "blocks_json, expanded, umo, platform) VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                (
                    f"proactive-{int(time.time() * 1000)}",
                    session_id,
                    kind,
                    "bot",
                    self.config.persona_name,
                    int(time.time()),
                    text,
                    "[]",
                    umo,
                    platform,
                ),
            )
        except Exception as exc:
            logger.debug(f"[congling] 主动消息归档失败: {exc!r}")

    async def _log(self, session_id: str, umo: str, reason: str, text: str, kind: str = "text") -> None:
        try:
            await self.store.execute(
                "INSERT INTO proactive_log(persona_id, session_id, umo, at, reason, text, kind) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    self.config.str("persona.id", "ling") or "ling",
                    session_id,
                    umo,
                    int(time.time()),
                    reason,
                    text[:1000],
                    kind,
                ),
            )
        except Exception as exc:
            logger.debug(f"[congling] 主动记录写入失败: {exc!r}")

    async def _bump_affinity(self, session_id: str) -> None:
        memory = getattr(self.plugin, "memory", None)
        affinity = getattr(memory, "affinity", None)
        if affinity is None:
            return
        try:
            await affinity.add(session_id, 2.0)
        except Exception as exc:
            logger.debug(f"[congling] 主动消息加好感失败: {exc!r}")

    # ================================================================ 结算
    async def settle_replies(self, now: float | None = None) -> int:
        """被回复了就把 unanswered 清零、好感 +3。

        只结算 12 小时内的，太老的不算——「三天后才回一句」不该给奖励。
        """
        now = now or time.time()
        await self.load()
        try:
            rows = await self.store.fetch_all(
                "SELECT id, session_id, at FROM proactive_log "
                "WHERE replied_at=0 AND at >= ? ORDER BY at DESC LIMIT 40",
                (int(now - 12 * 3600),),
            )
        except Exception:
            return 0
        settled = 0
        for row in rows:
            session_id = str(row.get("session_id") or "")
            at = int(row.get("at") or 0)
            try:
                hit = await self.store.fetch_one(
                    "SELECT ts FROM msg_archive WHERE session_id=? AND ts > ? AND sender_id<>'bot' "
                    "ORDER BY ts ASC LIMIT 1",
                    (session_id, at),
                )
            except Exception:
                continue
            if not hit:
                continue
            delta = int(hit["ts"]) - at
            try:
                await self.store.execute(
                    "UPDATE proactive_log SET replied_at=?, replied_delta=? WHERE id=?",
                    (int(hit["ts"]), float(delta), int(row.get("id") or 0)),
                )
            except Exception:
                continue
            state = self._sess(session_id)
            state["unanswered"] = 0
            settled += 1
            memory = getattr(self.plugin, "memory", None)
            affinity = getattr(memory, "affinity", None)
            if affinity is not None:
                try:
                    await affinity.add(session_id, 3.0)
                except Exception:
                    pass
        if settled:
            await self.save()
        return settled

    async def _count_unanswered(self, session_id: str, now: float) -> int:
        try:
            row = await self.store.fetch_one(
                "SELECT COUNT(*) AS c FROM proactive_log WHERE session_id=? AND replied_at=0 AND at >= ?",
                (session_id, int(now - 24 * 3600)),
            )
        except Exception:
            return 0
        return int((row or {}).get("c") or 0)

    # ================================================================ 调度
    async def tick(self, now: float | None = None) -> None:
        if not self.enabled():
            return
        now = now or time.time()
        # 重启就冒一堆消息是最吓人的失败形态，前 3 分钟什么都不做
        if now - self._boot_at < 180:
            return
        if now - self._last_attempt < 60:
            return
        self._last_attempt = now
        await self.load()
        if int(self._global.get("muted_until") or 0) > now:
            return
        try:
            await self.settle_replies(now)
            rows = await self.candidates(now)
            for row in rows:
                session_id = str(row.get("session_id") or "")
                # 未回复数要以库里的行动记录为准：上次主动之后对方一句话没说，
                # 这条就算没被回。只看内存里的计数会在重启后归零。
                state = self._sess(session_id)
                state["unanswered"] = await self._count_unanswered(session_id, now)
                ok, reason, score = await self.should(row, now)
                if not ok:
                    continue
                # 先占位再发，防止同一分钟重复进两条
                state["next_ok_at"] = int(now + 60)
                await self.save()
                asyncio.get_running_loop().create_task(
                    self._send_guarded(row, reason=("after_wake" if self._just_woke(now) else "idle"))
                )
                logger.info(f"[congling] 决定主动说一句：{row.get('session_id')} 分数 {score}")
                break  # 一轮只发一条
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[congling] 主动 tick 失败: {exc!r}")

    def _just_woke(self, now: float) -> bool:
        night = getattr(self.plugin, "night", None)
        state = getattr(getattr(night, "state", None), "state", None)
        if state is None:
            return False
        try:
            waking = int(getattr(state, "enter_waking_at", 0) or 0)
            return bool(waking and now - waking < 3600)
        except Exception:
            return False

    async def _send_guarded(self, row: dict[str, Any], *, reason: str) -> None:
        session_id = str(row.get("session_id") or "")
        try:
            result = await self.send_one(row, reason=reason)
        except Exception as exc:
            result = {"ok": False, "error": repr(exc)}
        if not result.get("ok"):
            state = self._sess(session_id)
            state["unanswered"] = int(state.get("unanswered") or 0) + 1
            state["next_ok_at"] = int(time.time() + 1800)
            await self.save()

    async def send_now(self, session_id: str) -> dict[str, Any]:
        """面板「立刻发一条」：不受额度限制，但仍尊重静音。"""
        await self.load()
        now = time.time()
        state = self._sess(session_id)
        if int(state.get("muted_until") or 0) > now:
            return {"ok": False, "error": "这个会话被静音了"}
        try:
            row = await self.store.fetch_one(
                "SELECT session_id, session_kind, MAX(umo) AS umo, MAX(platform) AS platform "
                "FROM msg_archive WHERE session_id=? GROUP BY session_id, session_kind "
                "ORDER BY MAX(ts) DESC LIMIT 1",
                (session_id,),
            )
        except Exception:
            row = None
        if not row:
            return {"ok": False, "error": "这个会话没有归档记录"}
        return await self.send_one(dict(row), reason="manual")

    async def mute(self, session_id: str = "", hours: int = 6) -> None:
        await self.load()
        until = int(time.time() + max(0, hours) * 3600)
        if session_id:
            self._sess(session_id)["muted_until"] = until
        else:
            self._global["muted_until"] = until
        await self.save()

    async def state_view(self) -> dict[str, Any]:
        await self.load()
        now = time.time()
        day_key = time.strftime("%Y-%m-%d", time.localtime(now))
        self._roll_day(day_key, now)
        return {
            "enabled": self.enabled(),
            "sent_today": int(self._global.get("sent_today") or 0),
            "daily_cap": self._cfg_int("daily_cap", 6),
            "muted_until": int(self._global.get("muted_until") or 0),
            "session_mode": self._cfg_str("session_mode", "recent"),
            "quiet_hours": self._cfg_str("quiet_hours", "01:00-08:00"),
            "active_hours": self._cfg_str("active_hours", "") or self.config.str("persona.active_hours", "09:00-03:30"),
            "allow_group": self.config.bool("proactive.allow_group", False),
            "in_quiet": _in_spec(self._cfg_str("quiet_hours", "01:00-08:00"), now),
        }

    async def log_view(self, limit: int = 30) -> list[dict[str, Any]]:
        try:
            rows = await self.store.fetch_all(
                "SELECT id, session_id, at, reason, text, kind, replied_at, replied_delta "
                "FROM proactive_log ORDER BY at DESC LIMIT ?",
                (max(1, min(200, limit)),),
            )
        except Exception:
            return []
        out = []
        for row in rows:
            item = dict(row)
            at = int(item.get("at") or 0)
            item["at_text"] = time.strftime("%m-%d %H:%M", time.localtime(at)) if at else ""
            item["replied"] = bool(int(item.get("replied_at") or 0))
            out.append(item)
        return out

    @staticmethod
    def _human(seconds: float) -> str:
        minutes = int(max(0, seconds) // 60)
        if minutes < 60:
            return f"{minutes} 分钟"
        hours = minutes // 60
        if hours < 48:
            return f"{hours} 小时"
        return f"{hours // 24} 天"
