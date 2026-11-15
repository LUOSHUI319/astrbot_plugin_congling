"""长夜如梦。

一个板块，四件事：作息（state）、道别（lullaby）、唤醒（wake）、做梦（dream）。
爬楼和结束语识别是配套的零件，不单独对外。

对外只暴露 NightModule，其余都是内部件。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from ..core import clock
from ..core.contracts import NIGHT_ASLEEP, NIGHT_AWAKE, NIGHT_WAKING, PerceivedMessage
from .close import close_minutes, is_closing, match_strength, trigger_allowed
from .quiet import QuietBreaker
from .crawl import Crawler
from .dream import Dreamer
from .plans import DreamPlans
from .state import SLEEP_WINDOW_KEY, NightService
from ..core.exc import exc_brief

__all__ = [
    "NightModule",
    "NightService",
    "Dreamer",
    "Crawler",
    "is_closing",
    "match_strength",
    "close_minutes",
    "trigger_allowed",
    "QuietBreaker",
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
        self.breaker = QuietBreaker(plugin)
        self._waking_started: dict[str, int] = {}
        self.pending_sleep: dict[str, Any] | None = None
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
    async def on_message(self, msg: PerceivedMessage, *, self_id: str = "") -> dict[str, Any]:
        """睡眠期拦截 + 唤醒判定 + 收线判定。返回 state.on_message 的结果。"""
        result = await self.state.on_message(msg, self_id=self_id)

        # 正在醒：先爬楼再说
        if self.state.state.mode == "waking":
            started = self._waking_started.get(msg.session_id)
            if not started:
                self._waking_started[msg.session_id] = int(time.time())

        # 安静期：人醒着，但这段时间不接话。
        # 管理员在窗口内攒够条数就解除（群聊还要 @ 或引用才算数）。
        if not self.state.state.is_asleep() and self.state.is_quiet():
            is_admin = False
            try:
                is_admin = str(msg.sender_id) in {str(x) for x in (self.plugin.admin_ids() or [])}
            except Exception:
                is_admin = False
            broke, why = self.breaker.check(msg, is_admin=is_admin, self_id=self_id)
            if broke:
                await self.state.clear_quiet(reason=why)
                result["quiet_broken"] = True
                result["hint"] = f"安静被打破（{why}），正常接话"
                logger.info(f"[congling] {result['hint']}")
            else:
                result["block"] = True
                result["quiet"] = True
                result["hint"] = (
                    f"安静中（{why}，到 {self.state.quiet_clock()}）"
                )
                return result

        # 醒着的时候判断这句话是不是在收线
        if not self.state.state.is_asleep() and self.config.bool("close.enabled", True):
            closing, strength, word = is_closing(msg.text, self.config)
            if closing and strength in ("strong", "medium"):
                allowed, who = self._close_allowed(msg)
                if not allowed:
                    # 话确实是结束语，但人不在这条权限链里。不跟着停，也不挂
                    # pending_sleep —— 照常回一句就行，别让路人的一句晚安把整只
                    # bot 按下去。这里不改除 hint 外的任何状态，也不 return，
                    # 免得连带跳过下面「精力耗尽也要道别」那段。
                    result["closing"] = False
                    result["close_denied"] = who
                    result["hint"] = f"对方收线（{word}），但触发人不在允许范围（{who}），不跟着停"
                    logger.info(f"[congling] 收线被忽略：{word}（{who}）")
                else:
                    kind = "noon" if ("午" in word or "眯" in word or "盹" in word) else "night"
                    sleeping_enabled = (
                        self.config.bool("night.allow_sleep", True)
                        and self.config.bool("night.enabled", False)
                    )
                    if sleeping_enabled:
                        # 真睡开着：维持老行为，回完这句就躺下
                        self.pending_sleep = {
                            "kind": kind,
                            "reason": "talk",
                            "word": word,
                            "strength": strength,
                            "session_id": msg.session_id,
                            "trigger": who,
                        }
                        result["closing"] = True
                        result["hint"] = f"对方收线（{word}），回完这句就睡"
                    else:
                        # 不睡，只安静。强档词安静到下次起床点，其余按分钟数。
                        minutes = close_minutes(self.config, strength, word)
                        until = await self.state.start_quiet(
                            minutes=minutes, kind=kind, word=word
                        )
                        result["closing"] = True
                        result["quiet_until"] = until
                        result["hint"] = (
                            f"对方收线（{word}），安静到 {clock.fmt('%H:%M', until)}（不入睡）"
                        )

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

    def quiet_line(self) -> str:
        """面板和状态指令用的一句话。没在安静就返回空串。"""
        state = self.state
        if not state.is_quiet():
            return ""
        return (
            f"安静中（{state.state.quiet_word or '收线'}，到 {state.quiet_clock()}，"
            f"还有 {state.quiet_left_minutes()} 分钟）"
        )

    def _close_allowed(self, msg: PerceivedMessage) -> tuple[bool, str]:
        """这次收线该不该认。

        人对了才停。判定本身是纯函数（night/close.trigger_allowed），这里只负责
        把「说话人」和「管理员名单」递进去。名单取不到时按拒绝处理——宁可少停一次，
        也别让陌生人一句话把 bot 关掉。
        """
        try:
            return trigger_allowed(
                getattr(msg, "sender_id", ""),
                self.config,
                admins=getattr(self.plugin, "admin_ids", ()) or (),
                session_id=getattr(msg, "session_id", ""),
            )
        except Exception as exc:
            logger.warning(f"[congling] 收线权限判定失败: {exc_brief(exc)}")
            return False, "error"

    async def after_reply(self, msg: PerceivedMessage | None = None, *, session_id: str = "") -> None:
        """回完一句话之后：执行收线入睡。

        爬楼**不在这里**做。这里在 after_message_sent 之后才被调，
        那时候回复已经发出去了，生成的爬楼语没人发得出去（原来就是这么丢的）。
        爬楼统一走 take_crawl_line()，由 express_hook 在发送前插入。
        """
        pending = self.pending_sleep
        if not pending:
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

    async def crawl(self, session_id: str) -> str:
        """执行爬楼，返回要说的话（面板/指令用；回复链路走 take_crawl_line）。"""
        line = await self.crawler.crawl_line()
        removed = await self.crawler.mark_read(session_id)
        await self.crawler.mark_crawled(session_id)
        await self.state.crawl_done(session_id)
        self._waking_started.pop(session_id, None)
        if removed:
            logger.info(f"[congling] 爬楼完成：撤销 {removed} 条未读（{session_id}）")
        return line

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

    def prompt_hint(self, consume: bool = True, session_key: str = "", *, include_private: bool = False) -> str:
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
        if state.mode == "asleep" and not self.pending_sleep:
            # 只挡「真的睡着了」。pending_sleep 表示"这条回完就睡"，
            # 那时候必须继续往下走，好让下面那段把道别要求加进 prompt ——
            # 原来这里一律 return ""，睡前道别永远没机会出现。
            return ""

        from . import energy as energy_mod

        lines: list[str] = []
        if state.mode == "waking":
            lines.append("你刚从睡眠里被叫醒，昨晚的消息还没看。不要直接回答具体内容，先说一句你要去翻消息。")

        woke_from = getattr(self.state, "_woke_from", None) or {}
        woke_key = str(woke_from.get("key") or "")
        if consume and not self.pending_sleep and state.mode in ("awake", "waking") and woke_key:
            if woke_key in ("n3", "rem") and self._hint_take("woke", session_key, now):
                if woke_key == "n3":
                    lines.append("你是从深度睡眠里被硬拽起来的，脑子还是木的，说话短、慢，别急着接话题。")
                else:
                    lines.append("你是从 REM 睡眠里醒过来的，脑子还在慢慢清醒，说话可以稍缓一些。")

        if self.pending_sleep:
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

        # 精力提示只留最后一档：掉到 30 以下才说「要休息了」。
        # 中间那些「有点困」「精力剩 55%」全去掉了——报电量式的句子
        # 模型只会照着复述，反而不像人在说话。时间已经由最后那段单独给，
        # 凌晨两点这个事实本身比任何百分比都有说服力。
        #
        # allow_sleep 关着的时候不能说「准备去睡」：它永远不会睡，
        # 嘴上说要休息、实际一直聊，比不提醒更怪。
        kind = energy_mod.fatigue_kind(self.config)
        if kind and float(state.energy) < 30:
            if self.config.bool("night.allow_sleep", True):
                lines.append(
                    "你已经很困了，心里在准备休息。对方要是没有要紧事，"
                    "你想早点收场；回话短一点，别开新话题。"
                )
            else:
                lines.append(
                    "你已经很困了，但还醒着愿意陪。回话会短、会慢，"
                    "不用硬撑出精神来。"
                )

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
