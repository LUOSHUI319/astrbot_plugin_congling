"""长夜如梦。

一个板块，四件事：作息（state）、道别（lullaby）、唤醒（wake）、做梦（dream）。
爬楼和结束语识别是配套的零件，不单独对外。

对外只暴露 NightModule，其余都是内部件。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from ..core.contracts import NIGHT_ASLEEP, NIGHT_AWAKE, NIGHT_WAKING, PerceivedMessage
from .close import close_minutes, is_closing, match_strength
from .crawl import Crawler
from .dream import Dreamer
from .plans import DreamPlans
from .state import SLEEP_WINDOW_KEY, NightService
from ..core.exc import exc_brief
from .wake_idle import WakeIdle
from .rollup import Rollup
from .wake_flow import WakeFlow

LOG = "[congling:night]"
PENDING_SLEEP_SECONDS = 120  # 只允许当前收线消息等待回复，不能无限挂起到后续聊天

__all__ = [
    "NightModule",
    "NightService",
    "Dreamer",
    "Crawler",
    "is_closing",
    "match_strength",
    "close_minutes",
]


class NightModule:
    """把作息、做梦、爬楼装在一起，给 main 一个门面。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self.state = NightService(plugin)
        self.dreamer = Dreamer(plugin)
        self.plans = DreamPlans(self)
        self.state.on_sleep = self.plans.plan_sleep
        self.state.on_wake = self.plans.note_wake
        self.crawler = Crawler(plugin)
        self.wake_idle = WakeIdle(plugin)
        self.rollup = Rollup(plugin)
        self.wake_flow = WakeFlow(self)
        self._waking_started: dict[str, int] = {}
        self.pending_sleep: dict[str, Any] | None = None
        # 醒后补课的分流计划：session_id -> list[(RollupChunk, plan)]
        self._rollup_plans: dict[str, list[Any]] = {}
        # 爬楼语念过哪些会话（crawl_announce=once 时用）
        self._announced: set[str] = set()
        self.morning_dream: Any = None
        self._dream_event_id = ""

    # ================================================================ 生命周期
    async def start(self) -> None:
        # 开关关着也读一遍：面板要显示「共几段」，而且万一开着之后第一段梦
        # 直接 append + save，会把以前存的梦整个覆盖掉
        await self.dreamer.load()
        await self.state.start()
        if self.state.state.mode == NIGHT_ASLEEP:
            await self.plans.plan_sleep()

    async def shutdown(self) -> None:
        await self.plans.cancel()
        await self.state.save()
        if self.dreamer.enabled and self.dreamer._loaded:
            await self.dreamer.save()

    # ================================================================ 消息入口
    def _pending_allowed(self) -> bool:
        pending = self.pending_sleep
        if not pending:
            return False
        if pending.get("reason") != "talk":
            return True
        return (
            self.config.bool("close.enabled", True)
            and self.config.bool("night.allow_sleep", True)
            and (pending.get("strength") != "strong" or self.config.bool("close.strong_enabled", True))
            and 0 <= time.time() - float(pending.get("created_at") or 0) <= PENDING_SLEEP_SECONDS
        )

    def _pending_matches(self, session_id: str = "", msg_id: str = "") -> bool:
        pending = self.pending_sleep
        if not pending:
            return False
        if session_id and pending.get("session_id") != session_id:
            return False
        if msg_id and pending.get("msg_id") and pending["msg_id"] != msg_id:
            return False
        return True

    async def _cancel_pending(self, reason: str) -> None:
        pending = self.pending_sleep
        self.pending_sleep = None
        if pending:
            await self.state.events.add(
                "closing_cancelled", self.state.state.energy, self.state.state,
                reason=reason, session_id=pending.get("session_id", ""),
                msg_id=pending.get("msg_id", ""), word=pending.get("word", ""),
                strength=pending.get("strength", ""),
            )

    async def on_message(self, msg: PerceivedMessage, *, self_id: str = "") -> dict[str, Any]:
        """睡眠期拦截 + 唤醒判定 + 收线判定。返回 state.on_message 的结果。"""
        if self.pending_sleep and self.pending_sleep.get("reason") == "talk":
            if not self._pending_allowed():
                await self._cancel_pending("closing_invalid")
            elif self.pending_sleep.get("session_id") == msg.session_id and self.pending_sleep.get("msg_id") != msg.msg_id:
                # 该会话已经继续说话，不再用上一条结束语指导本轮回复或入睡。
                await self._cancel_pending("conversation_continued")
        result = await self.state.on_message(msg, self_id=self_id)

        # 醒着：刷新「最后消息时间」，供醒后静默待机判定（双控制第二个时钟）
        if not self.state.state.is_asleep() and self.rollup.enabled:
            try:
                await self.wake_idle.note_message(msg.session_id)
            except Exception as exc:
                logger.warning(f"{LOG} 记待机时间失败: {exc_brief(exc)}")

        # 正在醒：先爬楼再说
        if self.state.state.mode == "waking":
            started = self._waking_started.get(msg.session_id)
            if not started:
                self._waking_started[msg.session_id] = int(time.time())

        # 醒着的时候判断这句话是不是在收线
        if not self.state.state.is_asleep() and self.config.bool("close.enabled", True):
            closing, strength, word = is_closing(msg.text, self.config)
            # 强档结束语单独开关：关掉后「晚安/我睡了」不再触发入睡，避免聊天中误触发；
            # 中档（拜拜/回头聊）仍照常静默。此时把 strong 降级为不触发（直接跳过）。
            if strength == "strong" and not self.config.bool("close.strong_enabled", True):
                closing = False
                await self.state.events.add(
                    "closing_skipped", self.state.state.energy, self.state.state,
                    reason="strong_off", session_id=msg.session_id, msg_id=msg.msg_id,
                    word=word, strength=strength,
                )
            if closing and strength in ("strong", "medium"):
                await self.state.events.add(
                    "closing", self.state.state.energy, self.state.state,
                    reason="talk", session_id=msg.session_id, word=word,
                    strength=strength, allow_sleep=self.config.bool("night.allow_sleep", True),
                )
                # 收线归哪一觉：先看收线词里的「午/眯/盹/躺」，再看当前时段。
                # 大白天的「晚安」不能一路睡到明天早上，按午睡（短觉）处理。
                from . import energy as _energy_mod

                kind = "noon" if ("午" in word or "眯" in word or "盹" in word or "躺" in word) else "night"
                if kind == "night" and not _energy_mod.fatigue_kind(self.config):
                    # 不在任何疲劳期（白天），默认按午睡处理，睡个短觉
                    kind = "noon"
                # 真睡开关关着：静默照旧（对方就是要下线了），但不挂入睡、
                # 也不让 prompt 去写「说完这句就睡」的道别。
                if self.config.bool("night.allow_sleep", True):
                    self.pending_sleep = {
                        "kind": kind,
                        "reason": "talk",
                        "word": word,
                        "strength": strength,
                        "session_id": msg.session_id,
                        "msg_id": msg.msg_id,
                        "created_at": time.time(),
                    }
                    result["closing"] = True
                    result["hint"] = f"对方收线（{word}），回完这句就睡"
                else:
                    result["closing"] = True
                    result["hint"] = f"对方收线（{word}），不睡，只是安静一会儿"

        # 精力耗尽触发的入睡，也要给一次道别的机会
        # 双保险：state.on_message 那边已经拦过一次，这里再拦一次，
        # 免得以后谁改了一头，另一头又把「关了开关还道别」放回来。
        if result.get("sleep_triggered") and self.config.bool("night.allow_sleep", True):
            kind = "night"
            try:
                from . import energy as energy_mod

                kind = energy_mod.fatigue_kind(self.config) or "night"
            except Exception:
                pass
            self.pending_sleep = {"kind": kind, "reason": "energy", "session_id": msg.session_id}

        return result

    async def after_reply(self, msg: PerceivedMessage | None = None, *, session_id: str = "", msg_id: str = "") -> None:
        """回完一句话之后：执行收线入睡。

        爬楼**不在这里**做。这里在 after_message_sent 之后才被调，
        那时候回复已经发出去了，生成的爬楼语没人发得出去（原来就是这么丢的）。
        爬楼统一走 take_crawl_line()，由 express_hook 在发送前插入。
        """
        pending = self.pending_sleep
        if not pending:
            return
        if not self._pending_allowed():
            await self._cancel_pending("closing_invalid")
            return
        if not self._pending_matches(session_id or getattr(msg, "session_id", ""),
                                     msg_id or getattr(msg, "msg_id", "")):
            return
        self.pending_sleep = None
        self.morning_dream = None
        try:
            # 自动收线：不许走 by_user，sleep_now 内部会照 night.allow_sleep 判断
            await self.state.sleep_now(
                reason=pending.get("reason", "talk"),
                kind=pending.get("kind") or None,
            )
            logger.info(
                f"[congling] 入睡：{pending.get('reason')} "
                f"（{pending.get('word') or '精力耗尽'}）"
            )
        except Exception as exc:
            logger.warning(f"[congling] 入睡失败: {exc_brief(exc)}")

    def _pick_morning_dream(self) -> None:
        """本次醒来首次对话时掷一次，只回忆本次睡眠中的梦。"""
        try:
            dream = self.dreamer.pick_to_mention(
                since=self.state._wake_sleep_at, until=self.state.wake_hints.started_at,
            )
        except Exception:
            dream = None
        self.morning_dream = dream
        if dream is not None:
            logger.info(f"[congling] 醒来记得那个梦：{dream.level} 级 / {'、'.join(dream.tags)}")

    # ================================================================ 醒后补课
    async def rollup_on_wake(self, session_id: str) -> list[Any]:
        """醒来后对某会话跑一次增量摘要+分流（L2/L3）。返回可回复计划。

        只在 rollup.enabled 时生效；失败静默降级，不影响正常回复链路。
        """
        if not self.rollup.enabled:
            return []
        try:
            chunks = await self.rollup.rollup_session(session_id)
            plans: list[Any] = []
            for chunk in chunks:
                plan = await self.rollup.triage(chunk)
                plans.append((chunk, plan))
                logger.info(
                    f"{LOG} L2/L3 完成 session={session_id} "
                    f"摘要={len(chunk.summary)}字 待回={len(plan.get('to_reply') or [])} "
                    f"待追问={len(plan.get('to_follow') or [])}"
                )
            return plans
        except Exception as exc:
            logger.warning(f"{LOG} 醒后补课失败: {exc_brief(exc)}")
            return []

    async def idle_tick(self) -> None:
        """由 main 的心跳调用：醒着时检查是否该「想继续睡」。

        只在 rollup.enabled 且 wake_idle_minutes>0 时生效。60 秒一拍，精度 ±60s。
        """
        if not self.rollup.enabled:
            return
        state = self.state.state
        if state.mode not in (NIGHT_AWAKE, NIGHT_WAKING):
            return
        # 只对「醒着且有静默待机配置」做检查；会话列表来自待爬/最近活跃，这里保守地
        # 只对当前 pending_crawl 与醒时标记过的会话做，避免无差别扫描所有历史会话。
        sessions = list(getattr(state, "pending_crawl", []) or [])
        if not sessions:
            # 没有待爬会话时，检查「醒时标记」过的会话（kv 里有 wake_at 标记的）
            sessions = await self.wake_idle._active_sessions()
        for sid in sessions:
            try:
                decision = await self.wake_idle.claim(sid)
                if decision.trigger:
                    await self._want_more_sleep(sid, decision)
            except Exception as exc:
                logger.warning(f"{LOG} 待机判定失败 {sid}: {exc_brief(exc)}")

    async def _want_more_sleep(self, session_id: str, decision: Any) -> None:
        """本轮静默准备一次「想继续睡」文案，不主动重新入睡。

        60s 并入窗口由 express_hook 里配合处理；这里先落一个待发文案，
        由 express_hook 在发送前决定「并入主回复」还是「单独发」。
        """
        mood = self.wake_idle.pick_mood()
        line = self.wake_idle.compose_sleepy_line(mood)
        self.wake_idle._pending_line[session_id] = line
        self.wake_idle._pending_at[session_id] = time.time()
        self.wake_idle._pending_persona[session_id] = (
            await self.plugin.persona.resolve()
        )["token"]
        logger.info(f"{LOG} 触发想继续睡 session={session_id} 情绪={mood or '自由发挥'}")

    async def take_rollup_reply(self, session_id: str) -> str:
        """express_hook 发送前调用：取该会话「待逐条解答」的拟人回复，取一次清一次。"""
        plans = self._rollup_plans.pop(session_id, None)
        if not plans:
            return ""
        for chunk, plan in plans:
            text = await self.rollup.compose_reply(plan)
            if text:
                return text
        return ""

    def take_sleepy_line(self, session_id: str, persona_token: str = "") -> str:
        """express_hook 发送前调用：取「想继续睡」待发文案（60s 并入窗口内）。"""
        return self.wake_idle.take_line(session_id, persona_token)

    # ================================================================ 爬楼
    async def need_crawl(self, session_id: str) -> int:
        """这个会话现在要不要爬楼，要的话还有多少条没翻。

        只在 waking 状态、且该会话确实在「待爬清单」里时才为真。
        清单在一次醒来时算好（见 state._do_wake），爬完一个划掉一个。
        """
        if not self.config.bool("night.crawl_enabled", True):
            return 0
        state = self.state.state
        if state.mode != NIGHT_WAKING:
            return 0
        if not session_id or session_id not in (state.pending_crawl or []):
            return 0
        since = await self._sleep_since()
        if since <= 0:
            return 1
        return await self.crawler.unread_count(session_id, since)

    async def _sleep_since(self) -> int:
        """最近一次入睡的时间戳（从落盘的睡眠区间里取）。"""
        try:
            raw = await self.store.kv_get(SLEEP_WINDOW_KEY, []) or []
        except Exception:
            return 0
        if not isinstance(raw, list) or not raw:
            return 0
        last = raw[-1]
        if not isinstance(last, dict):
            return 0
        try:
            return int(last.get("sleep_at") or 0)
        except (TypeError, ValueError):
            return 0

    async def take_crawl_line(self, session_id: str) -> str:
        """醒来后的第一句：先念一句爬楼语，然后把这个会话的未读清掉。

        必须在**发送前**调用（main.express_hook），否则生成的句子没人发。
        返回空串表示这次不用爬。
        """
        try:
            if not await self.need_crawl(session_id):
                return ""
        except Exception as exc:
            logger.warning(f"[congling] 爬楼判定失败: {exc_brief(exc)}")
            return ""
        # 静默档：off 只清未读不说话；once 每个会话只念一次；always 每次都念。
        # 半夜被叫醒先来一句"我去翻翻"其实很人机，off 就是给这种情况留的。
        mode = str(self.config.str("night.crawl_announce", "once") or "once").strip().lower()
        announce = mode != "off"
        if mode == "once" and session_id in self._announced:
            announce = False
        line = ""
        if announce:
            try:
                line = await self.crawler.crawl_line()
            except Exception as exc:
                logger.warning(f"[congling] 爬楼语生成失败: {exc_brief(exc)}")
                line = ""
        self._announced.add(session_id)
        try:
            removed = await self.crawler.mark_read(session_id)
            await self.crawler.mark_crawled(session_id)
            await self.state.crawl_done(session_id)
            if removed:
                logger.info(f"[congling] 爬楼完成：撤销 {removed} 条未读（{session_id}）")
        except Exception as exc:
            logger.warning(f"[congling] 爬楼收尾失败: {exc_brief(exc)}")
        return line

    async def trigger_rollup(self, session_id: str) -> None:
        """醒后补课：对睡着期间的消息做摘要+分流，产出「该回什么」。

        独立于爬楼——爬楼开关关掉也不影响补课。由 express_hook 在每次醒后
        回复前调用，不依赖 need_crawl / crawl_enabled。结果挂到 self._rollup_plans，
        由 express_hook 在发送前并入回复。
        """
        if not self.rollup.enabled:
            return
        try:
            await self.wake_idle.mark_wake(session_id)
            self._rollup_plans = getattr(self, "_rollup_plans", {})
            plans = await self.rollup_on_wake(session_id)
            if plans:
                self._rollup_plans[session_id] = plans
        except Exception as exc:
            logger.warning(f"{LOG} 醒后补课触发失败: {exc_brief(exc)}")

    async def crawl(self, session_id: str) -> str:
        """执行爬楼，返回要说的话（面板/指令用；回复链路走 take_crawl_line）。"""
        rows = await self.crawler.pending(session_id)
        if not rows:
            return "之前的消息已经读过了"
        line = await self.crawler.crawl_line()
        # 手动入口也先完整读完冻结批次，失败时不撤销未读。
        chunks = await self.rollup.rollup_session(
            session_id, commit=False, rows=rows, read_only=True, include_items=False)
        removed = await self.crawler.mark_read(session_id, strict=True, rows=rows)
        await self.crawler.mark_crawled(session_id)
        await self.state.crawl_done(session_id)
        self._waking_started.pop(session_id, None)
        if removed:
            logger.info(f"[congling] 爬楼完成：撤销 {removed} 条未读（{session_id}）")
        return line + "\n" + "\n".join(
            chunk.context_text() for chunk in chunks if chunk.summary or chunk.facts)

    # ================================================================ 做梦
    async def dream_tick(self) -> None:
        """由 housekeeping 定时调用：该做梦就做一段。"""
        if self.plans.mode != "live":
            await self.plans.generate_live()
            return
        if not self.dreamer.enabled:
            return
        state = self.state.state
        if state.mode != NIGHT_ASLEEP:
            return
        if state.dream_skip:
            return
        if not self.dreamer.due(
            sleep_at=state.sleep_at,
            already=state.dream_count_today,
        ):
            return
        if self.config.bool("night.dream_in_rem_only", True):
            from . import stages as stages_mod

            stage = stages_mod.at(
                self.config,
                sleep_at=int(state.sleep_at or 0),
                wake_at=int(state.wake_at or 0),
                kind=state.sleep_kind or "night",
            )
            if not stage or stage.get("key") != "rem":
                return
        last = self.dreamer.list_recent(1)
        continuation = ""
        if last:
            continuation = (last[0].text or "")[-200:]
        entry = await self.dreamer.make(continuation=continuation)
        if entry is not None:
            state.dream_count_today += 1
            await self.state.save()

    async def deliver_dream_plans(self, now: int | None = None) -> int:
        return await self.plans.deliver_due(now)

    async def reconcile_dream_plans(self, now: int | None = None) -> dict[str, int]:
        task = self.plans._spawn("reconcile", lambda: self.plans.reconcile(now))
        return await task

    # ================================================================ 面板
    def _hint_take(self, kind: str, session_key: str, now: float) -> bool:
        # woke 与 dream 共用同一个醒来事件；按类型/会话独立计数，过期不清账。
        if kind == "dream" and self.config.str("night.dream_hint_scope", "session") == "global":
            session_key = "__global__"
        return self.state.wake_hints.take(
            kind, session_key,
            limit=self.config.int(f"night.{kind}_hint_max_uses", 2 if kind == "dream" else 1),
            ttl_minutes=self.config.int(f"night.{kind}_hint_ttl_minutes", 120), now=now,
        )

    def prompt_hint(self, consume: bool = True, session_key: str = "", *,
                    include_private: bool = False, session_id: str = "", msg_id: str = "") -> str:
        """生成临时用户上下文；不修改 system prompt。

        consume=True 按事件、提示类型和会话消耗注入机会，不代表实际提及。
        consume=False 只提供持续状态，绝不返回醒来语气或梦境正文。
        """
        state = self.state.state
        hints = self.state.wake_hints
        now = time.time()
        if self._dream_event_id != hints.event_id:
            self.morning_dream = None
            self._dream_event_id = ""
        dream_valid = hints.valid(
            now=now, ttl_minutes=self.config.int("night.dream_hint_ttl_minutes", 120),
        )
        if not dream_valid or not self.dreamer.enabled:
            self.morning_dream = None
        pending_here = self._pending_allowed() and self._pending_matches(session_id, msg_id)
        if state.mode == "asleep" and not pending_here:
            # 只挡「真的睡着了」。pending_sleep 表示"这条回完就睡"，
            # 那时候必须继续往下走，好让下面那段把道别要求加进 prompt ——
            # 原来这里一律 return ""，睡前道别永远没机会出现。
            return ""

        from . import energy as energy_mod

        lines: list[str] = []
        if state.mode == "waking" and session_id in state.pending_crawl:
            lines.append("你刚从睡眠里被叫醒，昨晚的消息还没看。不要直接回答具体内容，先说一句你要去翻消息。")

        # 强制唤醒情绪池：只在「被强行叫醒 且 没睡够（精力低）」时抽一次情绪。
        # 精力满（自然醒 / 已经缓过来了）不触发——哪怕 _forced_wake 标志残留，
        # 精力一恢复满就不该再有「没睡醒」的情绪。
        if (
            consume and self.rollup.enabled
            and state.mode in ("awake", "waking")
            and getattr(self.state, "_forced_wake", False)
            and float(state.energy) <= float(self.config.int("night.energy_after_wake", 40))
        ):
            mood = self.wake_idle.pick_mood()
            if mood:
                hint = self.wake_idle.prompt_hint(mood)
                if hint:
                    lines.append(hint)
            # 一次性消费：注入过就不再重复
            self.state._forced_wake = False

        woke_from = getattr(self.state, "_woke_from", None) or {}
        woke_key = str(woke_from.get("key") or "")
        if (consume and not pending_here and state.mode in ("awake", "waking") and woke_key
                and float(state.energy) < self.config.float("night.energy_max", 100)):
            if woke_key in ("n3", "rem") and self._hint_take("woke", session_key, now):
                if woke_key == "n3":
                    lines.append("你是从深度睡眠里被硬拽起来的，脑子还是木的，说话短、慢，别急着接话题。")
                else:
                    lines.append("你是从 REM 睡眠里醒过来的，脑子还在慢慢清醒，说话可以稍缓一些。")

        if pending_here:
            limit = max(4, self.config.int("close.max_closing_chars", 12))
            if self.config.bool("night.farewell_before_sleep", True):
                lines.append(
                    f"这句话说完你就要去睡了。回复里带上道别，控制在 {limit} 字以内，"
                    "不要抛问题、不要开新话题、不要约明天的事。"
                )
            else:
                lines.append(f"这句话说完你就去睡了，回复控制在 {limit} 字以内，简短收尾就行。")

        # Select at most once per wake, including a failed recall roll. Crawl,
        # preview and non-REM wake paths must not re-roll or renew the TTL.
        if (consume and not self.pending_sleep and state.mode == NIGHT_AWAKE
                and dream_valid and self.dreamer.enabled
                and self._dream_event_id != hints.event_id):
            self._dream_event_id = hints.event_id
            self._pick_morning_dream()

        if (consume and not self.pending_sleep and state.mode == NIGHT_AWAKE
                and self.morning_dream is not None and not self.morning_dream.expired
                and self.morning_dream.level >= self.dreamer.mention_min_level
                and any(d is self.morning_dream for d in self.dreamer.living())
                and self._hint_take("dream", session_key, now)):
            dream = self.morning_dream
            if include_private:
                excerpt = (dream.text or "")[:120]
                lines.append(
                    f"你这次睡觉时做了一个梦（{'、'.join(dream.tags) or '说不清'}，强度 {dream.level} 级）：{excerpt}"
                    "\n如果这轮对话有自然的缝隙，可以顺口提一句这个梦；没有就别说，别硬插。"
                )
            else:
                lines.append("你这次睡觉时做过梦，正文是私人内容；本会话只可提及做过梦，不可透露梦的内容。")

        kind = energy_mod.fatigue_kind(self.config)
        if kind:
            percent = max(0, int(state.energy))
            if percent <= 25:
                tone = "已经很困了，脑子转不太动，说话会短、会卡"
            elif percent <= 55:
                tone = "有点犯困，反应慢半拍"
            else:
                tone = "开始犯困了，但还能撑"
            lines.append(f"现在是疲劳时段，你的精力还剩 {percent}%，{tone}。")

        if not lines:
            return ""
        return "【作息状态】\n" + "\n".join(f"- {line}" for line in lines)

    def snapshot(self) -> dict[str, Any]:
        data = self.state.snapshot()
        data["state"] = {"last_tick": data["last_tick"]}
        data["dream"] = {
            "enabled": self.dreamer.enabled,
            "pending": self.dreamer.pending,
            "stats": self.dreamer.stats() if self.dreamer.enabled else {},
        }
        return data

    def status_text(self) -> str:
        return self.state.status_text()
