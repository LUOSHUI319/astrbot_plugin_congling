"""作息状态机。

只做一件事：把「醒着 / 睡着 / 正在醒」这三种状态管对。

精力是全局一份（不是每会话一份）——一个 bot 只有一副身体，
三个群同时聊不可能有三倍精力。

所有时间推进都用「惰性 tick」：没人说话的时候不跑定时器，
下一次有人说话时按时间差一次性补算，这样重启、休眠都不会漏。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from astrbot.api import logger

from ..core.contracts import (
    NIGHT_ASLEEP,
    NIGHT_AWAKE,
    NIGHT_WAKING,
    NightState,
    PerceivedMessage,
    WakeAttempt,
)
from ..core.transient_hints import TransientHints
from . import energy as energy_mod
from . import stages as stages_mod
from .lullaby import Lullaby
from .wake import Waker
from ..core.exc import exc_brief
from .events import NightEvents, REASONS

KV_KEY = "night.state"
# 最近三次睡眠区间，给「睡着期间的记忆打未读」判定用（抽取是延后的，实时状态靠不住）
WOKE_FROM_KEY = "night.woke_from"
SLEEP_WINDOW_KEY = "night.last_sleep_window"

# 疲劳期内的下限保护：至少还能撑这么多条消息才允许睡着
DEFAULT_MIN_TURNS = 8


class NightService:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self.state = NightState()
        self.lullaby = Lullaby(plugin)
        self.waker = Waker(plugin)
        self.events = NightEvents(self.store)

        self._lock = asyncio.Lock()
        self._fatigue_turns = 0      # 本轮疲劳期内已经处理过多少条消息
        self._fatigue_entered = 0    # 进入本轮疲劳期的时间戳
        self._last_sleep_kind = ""   # night / noon，决定道别语用哪套
        self._woke_from: dict | None = None  # 醒来那一刻所在的睡眠阶段
        self.wake_hints = TransientHints()
        self._wake_sleep_at = 0
        # 本次醒来是不是「被强行叫醒」（message/manual），区别于自然醒。
        # 只作进程内一次性标志：情绪池据此触发一次，自然醒不触发。
        self._forced_wake: bool = False
        self._loaded = False
        self.on_sleep = None
        self.on_wake = None

    # ================================================================ 开关
    @property
    def enabled(self) -> bool:
        return self.config.bool("night.enabled", False)

    # ================================================================ 生命周期
    async def start(self) -> None:
        await self.load()
        if not self.enabled:
            logger.info("[congling] 长夜如梦未启用")
            return
        logger.info(
            f"[congling] 长夜如梦就绪：状态={self.state.mode} "
            f"精力={self.state.energy:.0f} "
            f"起床点={self.config.str('night.wake_time', '08:00')}"
            f"±{min(90, self.config.int('night.wake_jitter_minutes', 90))}min"
        )

    async def load(self) -> None:
        raw = None
        try:
            raw = await self.store.kv_get(KV_KEY, None)
        except Exception as exc:
            logger.warning(f"[congling] 读取作息状态失败: {exc!r}")
            raise
        if isinstance(raw, str):
            import json

            raw = json.loads(raw)
        if isinstance(raw, dict):
            self.state = NightState.from_dict(raw)
            self._fatigue_turns = max(0, int(raw.get("fatigue_turns", 0)))
            self._fatigue_entered = max(0, int(raw.get("fatigue_entered", 0)))
        # 「从哪个阶段醒的」以前只在内存里，重启就没了，醒来那句语气也跟着丢。
        # 写进 kv，重启后在提示 TTL 内还能接着用。
        try:
            woke = await self.store.kv_get(WOKE_FROM_KEY, None)
            if isinstance(woke, str):
                import json as _json

                woke = _json.loads(woke)
            if isinstance(woke, dict) and woke.get("key"):
                self._woke_from = woke
        except Exception:
            pass
        self._loaded = True
        if not self.state.last_wake_at and not self.state.is_asleep():
            windows = await self.store.kv_get(SLEEP_WINDOW_KEY, []) or []
            if isinstance(windows, list) and windows and isinstance(windows[-1], dict):
                self.state.last_wake_at = int(windows[-1].get("wake_at") or 0)
        # 起点为加载时的真实快照，不补造改版前的精力明细。
        await self.events.add("snapshot", self.state.energy, self.state,
                              reason=self.state.reason, sleep_at=self.state.sleep_at,
                              wake_at=self.state.wake_at)
        await self.tick()

    async def save(self) -> None:
        try:
            payload = self.state.to_dict()
            payload.update(fatigue_turns=self._fatigue_turns, fatigue_entered=self._fatigue_entered)
            await self.store.kv_set(KV_KEY, payload)
        except Exception as exc:
            logger.warning(f"[congling] 保存作息状态失败: {exc!r}")

    # ================================================================ 时间推进
    def _recover_over(self, start: float, end: float) -> float:
        """按睡眠阶段分段回精力。

        原来拿"终点那一刻"的阶段乘整段 elapsed：重启后一次补算五个小时，
        会全按醒来前那段 REM 的系数算，跨阶段全错。这里按阶段边界切开逐段累加。
        """
        sleep_at = int(self.state.sleep_at or 0)
        wake_at = int(self.state.wake_at or 0)
        kind = self.state.sleep_kind or "night"
        if end <= start:
            return 0.0
        plan = stages_mod.build_plan(self.config, sleep_at=sleep_at, wake_at=wake_at, kind=kind)
        bounds = {float(start), float(end)}
        for item in plan:
            bounds.add(float(item.get("start") or 0))
            bounds.add(float(item.get("end") or 0))
        pts = sorted(b for b in bounds if start <= b <= end)
        if len(pts) < 2:
            pts = [float(start), float(end)]
        rate = self.config.float("night.energy_recover_per_minute", 1.5)
        total = 0.0
        for a, b in zip(pts, pts[1:]):
            a2, b2 = max(a, float(start)), min(b, float(end))
            if b2 <= a2:
                continue
            stage = stages_mod.at(
                self.config, sleep_at=sleep_at, wake_at=wake_at, kind=kind, now=(a2 + b2) / 2
            )
            factor = stages_mod.energy_factor(stage) if stage else 1.0
            total += (b2 - a2) / 60.0 * rate * factor
        return total

    def _awake_segments(self, start: float, end: float) -> list[tuple[float, float, str]]:
        """把醒着的一段按疲劳窗口切换点切开，返回 (起, 止, 类型)。

        一天里会进出疲劳期好几次，重启补算时如果只看终点时刻，
        整段都会被当成"在疲劳期"或"不在疲劳期"，精力要么掉光要么白涨。
        """
        if end <= start:
            return []
        pts = [float(start)]
        step = 600.0  # 10 分钟粒度扫，边界精度足够
        prev = energy_mod.fatigue_kind(self.config, start)
        cursor = start + step
        while cursor < end:
            kind = energy_mod.fatigue_kind(self.config, cursor)
            if kind != prev:
                pts.append(cursor)
                prev = kind
            cursor += step
        pts.append(float(end))
        out: list[tuple[float, float, str]] = []
        for a, b in zip(pts, pts[1:]):
            if b > a:
                out.append((a, b, energy_mod.fatigue_kind(self.config, (a + b) / 2)))
        return out

    async def _auto_wake(self, now: float) -> str:
        """到点或精力满格都走正常醒来流程，避免满格仍等到计划起床。"""
        if not self.state.is_asleep():
            return ""
        if self.state.wake_at and now >= self.state.wake_at:
            await self._do_wake(reason="natural")
            return "wake_natural"
        cap = float(self.config.int("night.energy_max", 100))
        if cap > 0 and self.state.energy >= cap:
            await self._do_wake(reason="energy_full")
            return "wake_energy_full"
        return ""

    async def tick(self, now: float | None = None) -> list[str]:
        """按时间差补算状态。返回发生的事件名，方便日志和面板。"""
        async with self._lock:
            return await self._tick(now)

    async def _tick(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        if not self.enabled:
            # 只有「收线静默」在跑：不管精力，只管到点自然醒
            self.state.last_tick = int(now)
            if self.state.is_asleep() and self.state.wake_at and now >= self.state.wake_at:
                await self._do_wake(reason="natural")
                return ["wake_natural"]
            await self.save()
            return []
        events: list[str] = []

        # waking 不能一直挂着：叫醒之后那句没说成、或者用户就此不说话，
        # 状态机会永远停在「正醒着，还没爬起来」，面板和记忆都被冻住。
        if self.state.mode == NIGHT_WAKING and not self.state.enter_waking_at:
            # 兜底：将来若又有入口只改 mode 不设起点，别让它永久挂着
            await self.finish_waking()
            events.append("wake_orphan")
        if self.state.mode == NIGHT_WAKING and self.state.enter_waking_at:
            limit = max(1, self.config.int("night.waking_timeout_minutes", 5)) * 60
            if now - self.state.enter_waking_at >= limit:
                await self.finish_waking()
                events.append("wake_timeout")

        if self.state.last_tick <= 0:
            self.state.last_tick = int(now)
            # 首次启动也检查满格，不能因为没有上次心跳而多睡一个周期。
            wake_event = await self._auto_wake(now)
            if wake_event:
                events.append(wake_event)
            await self.save()
            return events

        elapsed_minutes = max(0.0, (now - self.state.last_tick) / 60.0)
        self.state.last_tick = int(now)

        if self.state.is_asleep():
            # 开关中途被关掉（allow_sleep=false），这一觉就不该再睡下去了，
            # 立刻醒过来。reason == "manual" 是人明确点的「睡」，那种睡满不打扰。
            # 不加这段的话，半夜关掉开关也得等 wake_at 到点才肯睁眼。
            if not self.config.bool("night.allow_sleep", True) and self.state.reason != "manual":
                await self._do_wake(reason="allow_sleep_off")
                events.append("wake_by_switch")
                return events
            # 睡着的时候按睡眠阶段回精力：深睡那一小时补得多，REM 补得少。
            # 以前是直接冻结，面板上看着像一条直线，也解释不了「睡得浅等于没睡」
            # 分段累加：跨睡眠阶段/跨疲劳窗口都不能按终点那一刻一票算完
            gain = self._recover_over(float(now) - elapsed_minutes * 60.0, float(now))
            before = self.state.energy
            if gain > 0:
                cap = float(self.config.int("night.energy_max", 100))
                self.state.energy = energy_mod.clamp(
                    self.state.energy + max(0.0, gain), high=cap
                )
            if self.state.energy != before:
                await self.events.add("sleep_recover", before, self.state, ts=now,
                                      minutes=elapsed_minutes, calculated_gain=gain,
                                      rate=self.config.float("night.energy_recover_per_minute", 1.5),
                                      sleep_at=self.state.sleep_at, wake_at=self.state.wake_at)
            wake_event = await self._auto_wake(now)
            if wake_event:
                events.append(wake_event)
        else:
            start_ts = float(now - elapsed_minutes * 60)
            segments = self._awake_segments(start_ts, float(now))
            if not segments:
                segments = [(start_ts, float(now), energy_mod.fatigue_kind(self.config, now))]
            last_kind = energy_mod.fatigue_kind(self.config, now)
            for seg_start, seg_end, kind in segments:
                minutes = (seg_end - seg_start) / 60.0
                if minutes <= 0:
                    continue
                last_kind = kind
                before = self.state.energy
                if kind:
                    if not self._fatigue_entered:
                        self._fatigue_entered = int(seg_start)
                        self._fatigue_turns = 0
                    # 精力按段消耗（消息消耗在 on_message 里另算）
                    self.state.energy = energy_mod.drain(
                        self.state.energy,
                        minutes=minutes,
                        messages=0,
                        per_minute=self.config.float("night.energy_per_minute", 0.8),
                        per_message=0.0,
                        high=self.config.float("night.energy_max", 100.0),
                    )
                else:
                    self._fatigue_entered = 0
                    self._fatigue_turns = 0
                    self.state.energy = energy_mod.recover(
                        self.state.energy,
                        minutes=minutes,
                        per_minute=self.config.float("night.energy_recover_per_minute", 1.5),
                        high=self.config.float("night.energy_max", 100.0),
                    )
                if self.state.energy != before:
                    await self.events.add(
                        "fatigue_drain" if kind else "awake_recover", before, self.state,
                        ts=seg_end, minutes=minutes, fatigue=kind,
                        start=seg_start, end=seg_end,
                        rate=self.config.float("night.energy_per_minute", 0.8) if kind
                        else self.config.float("night.energy_recover_per_minute", 1.5),
                    )
            if not last_kind:
                self._fatigue_entered = 0
                self._fatigue_turns = 0
            guard_before = self.state.energy
            can_sleep = self._can_sleep_by_energy() if last_kind else False
            if last_kind and energy_mod.exhausted(guard_before) and not can_sleep:
                await self.events.add("sleep_guard", guard_before, self.state, ts=now,
                                      turns=self._fatigue_turns,
                                      required=self.config.int("night.min_turns_before_sleep", DEFAULT_MIN_TURNS))
            if can_sleep:
                await self._do_sleep(reason="energy", kind=last_kind)
                events.append("sleep_energy")

            # 最晚睡觉时间兜底
            if not self.state.is_asleep() and self._past_latest_sleep(now):
                await self._do_sleep(reason="latest", kind="night")
                events.append("sleep_latest")

        await self.save()
        return events

    def _can_sleep_by_energy(self) -> bool:
        if not energy_mod.exhausted(self.state.energy):
            return False
        min_turns = max(0, self.config.int("night.min_turns_before_sleep", DEFAULT_MIN_TURNS))
        if self._fatigue_turns < min_turns:
            # 拉回来一点，保证还能撑
            self.state.energy = max(self.state.energy, 0.5)
            return False
        return True

    def _past_latest_sleep(self, now: float) -> bool:
        spec = self.config.str("night.latest_sleep_time", "02:30")
        target = energy_mod._parse_hhmm(spec)
        if target is None:
            return False
        minute = energy_mod.now_minute(now)
        # 只看 4 小时窗口，避免白天误判（比如 02:30 到了，但下午不该触发）
        if minute < target:
            return False
        return (minute - target) <= 240

    # ================================================================ 收发消息
    async def on_message(self, msg: PerceivedMessage, *, self_id: str = "") -> dict[str, Any]:
        """消息进来时调这个。

        返回：
            block     True 表示这条不要回（连模型都别叫）
            wake      唤醒掷骰结果（睡眠期间才有）
            hint      给调用方的说明
        """
        if not self.enabled:
            return {"block": False, "hint": "未启用"}

        await self.tick()

        if not self.state.is_asleep():
            # 每条消息消耗精力：全天生效（醒着聊天就耗精力），不在疲劳期也扣。
            # 原来这里整段包在 in_fatigue() 里，导致非疲劳期精力只涨不跌、
            # 面板一直 100%。「每分钟消耗」才是只在疲劳期扣的（见 tick）。
            before = self.state.energy
            self.state.energy = energy_mod.drain(
                self.state.energy,
                minutes=0.0,
                messages=1,
                per_minute=0.0,
                per_message=self.config.float("night.energy_per_message", 4.0),
                high=self.config.float("night.energy_max", 100.0),
            )
            await self.events.add(
                "message_drain", before, self.state,
                session_id=msg.session_id, msg_id=getattr(msg, "msg_id", ""),
                rate=self.config.float("night.energy_per_message", 4.0),
                fatigue=energy_mod.fatigue_kind(self.config),
                counted_messages=1,
            )
            if energy_mod.in_fatigue(self.config):
                if not self._fatigue_entered:
                    self._fatigue_entered = int(time.time())
                    self._fatigue_turns = 0
                self._fatigue_turns += 1
                guard_before = self.state.energy
                can_sleep = self._can_sleep_by_energy()
                if energy_mod.exhausted(guard_before) and not can_sleep:
                    await self.events.add(
                        "sleep_guard", guard_before, self.state,
                        session_id=msg.session_id, turns=self._fatigue_turns,
                        required=self.config.int("night.min_turns_before_sleep", DEFAULT_MIN_TURNS),
                    )
                if can_sleep:
                    # 开关关着就别报 sleep_triggered：报了上层会挂 pending_sleep，
                    # 回复完 after_reply 再走一次入睡，等于白道别一场还照样躺下。
                    if not self.config.bool("night.allow_sleep", True):
                        await self.events.add("sleep_skipped", self.state.energy, self.state,
                                              reason="energy", session_id=msg.session_id)
                        await self.save()
                        return {
                            "block": False,
                            "hint": "精力耗尽，但真睡开关关着",
                        }
                    kind = energy_mod.fatigue_kind(self.config)
                    await self._do_sleep(reason="energy", kind=kind)
                    return {
                        "block": False,
                        "sleep_triggered": True,
                        "hint": "精力耗尽，正在道别",
                    }
            await self.save()
            return {"block": False, "hint": "醒着"}

        # 睡着：先看能不能叫醒
        attempt: WakeAttempt = self.waker.check(msg, self_id=self_id)
        await self.events.add(
            "wake_check", self.state.energy, self.state,
            session_id=msg.session_id, msg_id=getattr(msg, "msg_id", ""),
            reason=attempt.reason, attempt={
                key: getattr(attempt, key) for key in
                ("allowed", "woke", "count", "needed", "chance", "roll")
            },
        )
        if attempt.woke:
            # 走统一的醒来流程：补睡眠窗口、置满精力、重置掷骰、算待爬清单。
            # 以前这里只把 mode 改成 waking，既不算 pending_crawl 也不设
            # enter_waking_at，于是 is_asleep() 一直为真，接下来每条消息都被
            # 当成"还在睡"拦掉，爬楼也永远启动不了。
            await self._do_wake(reason="message")
            if self.state.mode == NIGHT_WAKING:
                return {
                    "block": False,
                    "wake": attempt,
                    "waking": True,
                    "hint": attempt.reason,
                }
            return {"block": False, "wake": attempt, "hint": attempt.reason}

        # 没叫醒：这条不接，但把原因带出去方便日志
        return {
            "block": True,
            "wake": attempt,
            "hint": attempt.reason,
        }

    # ================================================================ 睡 / 醒
    async def _do_sleep(self, *, reason: str, kind: str = "night", force: bool = False) -> None:
        before, mode_before = self.state.energy, self.state.mode
        # allow_sleep 关掉时只算精力、不真的睡：想聊天的时候把它关掉就行
        # force=True 来自手动点的「睡」，不受这个开关影响
        if not force and not self.config.bool("night.allow_sleep", True):
            self.state.energy = max(
                self.state.energy,
                self.config.float("night.energy_after_wake", 35.0),
            )
            logger.info(f"[congling] 长夜如梦：allow_sleep 关闭，跳过入睡（本来原因={reason}）")
            await self.events.add("sleep_skipped", before, self.state,
                                  mode_before=mode_before, reason=reason)
            return
        now = time.time()
        self.wake_hints.clear()
        self._woke_from = None
        self.state.mode = NIGHT_ASLEEP
        self.state.sleep_at = int(now)
        self.state.wake_at = energy_mod.next_wake_time(self.config, now, kind=kind)
        self.state.reason = reason
        self.state.sleep_kind = kind or "night"
        self.state.enter_waking_at = 0
        self.state.pending_crawl = []
        self.state.dream_count_today = 0
        # 入睡时清掉「强制唤醒」标志：否则上次唤醒的情绪残留，下次醒来会误判
        self._forced_wake = False
        self._woke_from = None
        # 午睡要掷一次骰：这一次到底做不做梦
        if (kind or "night") == "noon":
            chance = max(0.0, min(1.0, self.config.float("night.noon_dream_chance", 0.5)))
            self.state.dream_skip = random.random() >= chance
        else:
            self.state.dream_skip = False
        self._last_sleep_kind = kind or "night"
        self.waker.reset_all()
        await self.save()
        if self.on_sleep is not None:
            await self.on_sleep()
        await self.events.add("sleep", before, self.state, mode_before=mode_before,
                              reason=reason, sleep_kind=kind, sleep_at=self.state.sleep_at,
                              wake_at=self.state.wake_at, turns=self._fatigue_turns)
        logger.info(
            f"[congling] 入睡：原因={reason} "
            f"预计醒来={time.strftime('%H:%M', time.localtime(self.state.wake_at))}"
        )

    async def _do_wake(self, *, reason: str = "natural", enter_waking: bool = True) -> None:
        """醒。

        醒来先看有没有东西要爬：有就进 waking（回第一句时先念一句爬楼语），
        没有就直接 awake。原来是一律置 awake，等于自然醒、面板叫醒这两条路
        永远不会爬楼，睡着期间攒下的未读也永远清不掉。
        """
        # 已经醒着（包括补课中）时重复叫醒，不制造新周期或把满精力降回 40。
        if not self.state.is_asleep():
            return
        before, mode_before = self.state.energy, self.state.mode
        sleep_at = int(self.state.sleep_at or 0)
        wake_at = int(self.state.wake_at or 0)
        sleep_kind = self.state.sleep_kind or self._last_sleep_kind or "night"
        if not sleep_kind or sleep_kind not in ("night", "noon"):
            sleep_kind = "night"

        now = time.time()
        # Natural wake may be processed hours late by a lazy tick.
        woke_at = min(now, float(wake_at)) if reason == "natural" and wake_at > 0 else now
        if sleep_at > 0 and self.on_wake is not None:
            await self.on_wake(sleep_at, int(woke_at))
        if sleep_at > 0:
            self.wake_hints.begin(woke_at)
            self._wake_sleep_at = sleep_at

        # 记下是从哪个阶段醒的：深睡被拽起来和 REM 醒过来，说话状态不一样
        try:
            self._woke_from = stages_mod.at(
                self.config, sleep_at=sleep_at, wake_at=wake_at, kind=sleep_kind, now=woke_at
            )
        except Exception:
            self._woke_from = None
        try:
            await self.store.kv_set(WOKE_FROM_KEY, self._woke_from or {})
        except Exception:
            pass

        pending: list[str] = []
        if sleep_at > 0:
            # 提前醒来时按实际结束时间归档，不能把计划起床前的新消息仍标成睡中。
            actual_wake_at = min(wake_at or int(time.time()), int(time.time()))
            await self._remember_sleep_window(sleep_at, actual_wake_at, sleep_kind)
            if enter_waking and self.config.bool("night.crawl_enabled", True):
                pending = await self._pending_sessions(sleep_at)

        self.state.sleep_at = 0
        self.state.wake_at = 0
        self.state.reason = ""
        self.state.last_wake_at = int(woke_at)
        self.state.last_wake_reason = reason
        # 强制唤醒（被消息叫醒 / 手动叫醒）＝没睡够，精力回「被强行叫醒后的精力」；
        # 到点或精力满格自动醒都保留满精力，不触发强制叫醒情绪。
        self._forced_wake = reason in ("message", "manual")
        if self._forced_wake:
            self.state.energy = float(self.config.int("night.energy_after_wake", 35))
        else:
            self.state.energy = float(self.config.int("night.energy_max", 100))
        self._fatigue_turns = 0
        self._fatigue_entered = 0
        self.waker.reset_all()

        if pending:
            self.state.mode = NIGHT_WAKING
            self.state.enter_waking_at = int(time.time())
            self.state.pending_crawl = pending
        else:
            self.state.mode = NIGHT_AWAKE
            self.state.enter_waking_at = 0
            self.state.pending_crawl = []
            self.state.sleep_kind = ""
        await self.save()
        await self.events.add("wake", before, self.state, mode_before=mode_before,
                              reason=reason, sleep_at=sleep_at, wake_at=wake_at,
                              pending_crawl=len(pending))
        logger.info(
            f"[congling] 醒来：原因={reason}"
            + (f"，待爬 {len(pending)} 个会话" if pending else "")
        )

    async def _pending_sessions(self, since: int) -> list[str]:
        """从入睡那一刻起，哪些会话有新消息没翻。"""
        if since <= 0:
            return []
        try:
            rows = await self.store.fetch_all(
                "SELECT DISTINCT session_id FROM msg_archive "
                "WHERE ts >= ? AND session_id<>'' ORDER BY ts DESC LIMIT 50",
                (since,),
            )
        except Exception as exc:
            logger.warning(f"[congling] 查待爬会话失败: {exc_brief(exc)}")
            return []
        # store.fetch_all 给的是 dict 行，这里以前按元组取 row[0]，
        # 一进 _do_wake 就 KeyError，于是状态永远卡在「睡着」——
        # 面板上看到的就是「bot 都醒了，界面还说睡着」。
        out: list[str] = []
        for row in rows or []:
            value = row.get("session_id") if isinstance(row, dict) else (row[0] if row else None)
            if value:
                out.append(str(value))
        return out

    async def _remember_sleep_window(self, sleep_at: int, wake_at: int, kind: str) -> None:
        """把这一觉的时间区间落盘，最近三段。

        记忆抽取是延后批量的，等它跑的时候人早就醒了，靠实时状态判断
        「这条是不是睡着期间记的」必然判错，只能靠这里留下的区间。
        """
        try:
            raw = await self.store.kv_get(SLEEP_WINDOW_KEY, []) or []
        except Exception:
            raw = []
        items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        record = {"sleep_at": int(sleep_at), "wake_at": int(wake_at), "kind": kind}
        # 同一觉重复写（以前醒来失败会一直重试）会把这段区间记成三份，
        # 记忆抽取时按区间判「这条是不是睡着时记的」就会重复算
        if not items or any(
            item.get("sleep_at") != record["sleep_at"] or item.get("wake_at") != record["wake_at"]
            for item in items[-1:]
        ):
            items.append(record)
        items = items[-3:]
        try:
            await self.store.kv_set(SLEEP_WINDOW_KEY, items)
        except Exception as exc:
            logger.warning(f"[congling] 记录睡眠区间失败: {exc_brief(exc)}")

    async def sleep_now(
        self,
        *,
        reason: str = "manual",
        kind: str | None = None,
        by_user: bool = False,
    ) -> None:
        """外部调用入睡。

        kind 不传时按当前时段自动判断：在午间疲劳窗口里就是午睡，
        否则算夜里睡。免得下午手点一下「睡」，一路睡到第二天早上。

        by_user=True 只给「人明确点了睡」的入口（面板 / 指令），那种无视
        night.allow_sleep；自动收线（after_reply）走默认 False，老老实实认开关。
        以前这里写死 force=True，结果关掉开关照样被一句「晚安」放倒。
        """
        if not kind:
            try:
                kind = energy_mod.fatigue_kind(self.config) or "night"
            except Exception:
                kind = "night"
        await self._do_sleep(reason=reason, kind=kind, force=by_user)

    async def wake_now(self, *, reason: str = "manual") -> None:
        """外部调用强制唤醒。醒来照样要爬楼（有未读就进 waking）。"""
        await self._do_wake(reason=reason, enter_waking=True)

    async def finish_waking(self) -> None:
        """爬楼结束后调，把 waking 落回 awake。

        精力不在这里动：`_do_wake` 已经按「自然醒回满 / 强制唤醒回 energy_after_wake」
        定好了，这里只负责状态收尾。以前写死 energy=35，会把自然醒+有爬楼的情况
        也打成 35；上一轮改成按 _forced_wake，又因为 prompt_hint 提前消费了
        _forced_wake 导致误判成自然醒回满——两种都错，干脆这里不碰精力。
        """
        mode_before = self.state.mode
        if self.state.mode == NIGHT_WAKING:
            self.state.mode = NIGHT_AWAKE
        self.state.enter_waking_at = 0
        self.state.pending_crawl = []
        self.state.wake_at = 0
        self.state.sleep_kind = ""
        await self.save()
        if mode_before != self.state.mode:
            await self.events.add("waking_done", self.state.energy, self.state,
                                  mode_before=mode_before)

    def drop_pending_crawl(self, session_id: str) -> None:
        """这个会话爬完了，从待爬清单里划掉（进程内，调用方负责 save）。"""
        key = str(session_id or "")
        if not key:
            return
        remain = [sid for sid in self.state.pending_crawl if sid != key]
        self.state.pending_crawl = remain
        if not remain and self.state.mode == NIGHT_WAKING:
            self.state.enter_waking_at = 0

    async def crawl_done(self, session_id: str) -> None:
        """某个会话爬完：划掉；全爬完就正式醒。"""
        self.drop_pending_crawl(session_id)
        if not self.state.pending_crawl and self.state.mode == NIGHT_WAKING:
            await self.finish_waking()
        else:
            await self.save()

    # ================================================================ 道别
    def farewell_for(self, session_id: str, kind: str | None = None) -> str:
        kind = kind or self._last_sleep_kind or "night"
        return self.lullaby.compose(kind=kind, session_id=session_id)

    def farewell_batch(self, session_ids: list[str], kind: str | None = None) -> dict[str, str]:
        kind = kind or self._last_sleep_kind or "night"
        return self.lullaby.compose_many(session_ids, kind=kind)

    # ================================================================ 面板
    def snapshot(self) -> dict[str, Any]:
        state = self.state
        now = time.time()
        kind = energy_mod.fatigue_kind(self.config, now)
        data: dict[str, Any] = {
            "enabled": self.enabled,
            "mode": state.mode,
            "energy": round(state.energy, 1),
            "energy_max": self.config.int("night.energy_max", 100),
            "fatigue": kind,
            "wake_mode": self.waker.mode,
            "wake_time": self.config.str("night.wake_time", "08:00"),
            "wake_jitter": max(0, min(90, self.config.int("night.wake_jitter_minutes", 90))),
            "wake_window": energy_mod.wake_window_text(self.config, int(now)),
            "night_fatigue": self.config.str("night.night_fatigue", "23:30-07:00"),
            "noon_fatigue": self.config.str("night.noon_fatigue", "13:00-14:30"),
            "latest_sleep_time": self.config.str("night.latest_sleep_time", "02:30"),
            "farewell_on": self.config.bool("night.farewell_before_sleep", True),
            "dream_on": self.config.bool("night.dream_enabled", False),
            "dream_start_after_minutes": max(1, self.config.int("night.dream_start_after_minutes", 60)),
            "dream_interval_minutes": max(5, self.config.int("night.dream_interval_minutes", 60)),
            "dream_in_rem_only": self.config.bool("night.dream_in_rem_only", True),
            "last_tick": state.last_tick,
            "crawl_on": self.config.bool("night.crawl_enabled", True),
            "allow_sleep": self.config.bool("night.allow_sleep", True),
            "noon_nap_minutes": max(10, min(180, self.config.int("night.noon_nap_minutes", 60))),
            "noon_nap_jitter": max(0, min(60, self.config.int("night.noon_nap_jitter_minutes", 15))),
            "sleep_kind": state.sleep_kind or getattr(self, "_last_sleep_kind", "night"),
            "pending_crawl": len(state.pending_crawl or []),
            "waking_timeout_minutes": max(1, self.config.int("night.waking_timeout_minutes", 5)),
            "crawl_sessions": list(state.pending_crawl or []),
            "energy_per_message": self.config.float("night.energy_per_message", 4.0),
            "energy_per_minute": self.config.float("night.energy_per_minute", 0.8),
            "energy_recover_per_minute": self.config.float("night.energy_recover_per_minute", 1.5),
            "energy_after_wake": self.config.float("night.energy_after_wake", 35.0),
            "wake_when_energy_full": self.enabled,
            "fatigue_turns": self._fatigue_turns,
            "min_turns_before_sleep": self.config.int("night.min_turns_before_sleep", DEFAULT_MIN_TURNS),
        }
        # 醒时文案：午睡不能报「07:00-09:00 醒」，那是夜里的起床点
        sleep_kind = data["sleep_kind"] if data["sleep_kind"] in ("night", "noon") else "night"
        wake_ts = int(state.wake_at or 0)
        clock = time.strftime("%H:%M", time.localtime(wake_ts)) if wake_ts else ""
        day_label = self._day_label(wake_ts, now) if wake_ts else ""
        if sleep_kind == "noon":
            data["wake_kind"] = "noon"
            data["wake_window"] = ""
            data["wake_at_clock"] = clock
            data["wake_day_label"] = day_label
            data["wake_label"] = clock
        else:
            window = energy_mod.wake_window_text(self.config, int(now))
            data["wake_kind"] = "night"
            data["wake_at_clock"] = clock
            data["wake_day_label"] = day_label
            data["wake_label"] = window
        if state.is_asleep():
            data["sleep_at"] = state.sleep_at
            data["wake_at"] = state.wake_at
            data["sleep_reason"] = state.reason
            data["sleep_reason_label"] = REASONS.get(state.reason, state.reason)
            data["remaining"] = max(0, state.wake_at - int(now))
            data["remaining_text"] = energy_mod.human_duration(max(0, state.wake_at - int(now)))
            plan = stages_mod.build_plan(
                self.config,
                sleep_at=int(state.sleep_at or 0),
                wake_at=int(state.wake_at or 0),
                kind=sleep_kind,
            )
            stage = stages_mod.at(
                self.config,
                sleep_at=int(state.sleep_at or 0),
                wake_at=int(state.wake_at or 0),
                kind=sleep_kind,
                now=now,
            )
            if stage:
                stage["remaining_text"] = energy_mod.human_duration(stage.get("remaining") or 0)
                stage["energy_factor"] = stages_mod.energy_factor(stage)
                data["stage"] = stage
                data["stage_text"] = stages_mod.describe(stage)
                data["stage_seq"] = plan
                data["stage_stats"] = stages_mod.stats(plan)
        if state.mode == NIGHT_WAKING:
            # waking 已不在 is_asleep() 里，面板要的这两项单独补
            limit = max(1, self.config.int("night.waking_timeout_minutes", 5)) * 60
            since = int(state.enter_waking_at or 0)
            data["waking_since"] = since
            data["waking_left"] = max(0, since + limit - int(now)) if since else 0
        return data

    @staticmethod
    def _day_label(ts: int, now: float | None = None) -> str:
        """今天 / 明天 / 后天 / 具体日期，给状态栏用。"""
        if ts <= 0:
            return ""
        base = time.localtime(now if now is not None else time.time())
        target = time.localtime(ts)
        today = time.mktime((base.tm_year, base.tm_mon, base.tm_mday, 0, 0, 0, 0, 0, -1))
        that = time.mktime((target.tm_year, target.tm_mon, target.tm_mday, 0, 0, 0, 0, 0, -1))
        delta = int(round((that - today) / 86400))
        if delta == 0:
            return "今天"
        if delta == 1:
            return "明天"
        if delta == 2:
            return "后天"
        return time.strftime("%m-%d", target)

    def status_text(self) -> str:
        data = self.snapshot()
        if not data["enabled"]:
            return "长夜如梦没开"
        if data["mode"] == NIGHT_AWAKE:
            fatigue = "（疲劳期）" if data.get("fatigue") else ""
            return (
                f"醒着{fatigue} 精力 {data['energy']:.0f}/{data['energy_max']} "
                f"起床点 {data['wake_time']} 浮动 ±{data['wake_jitter']} 分钟"
            )
        if data["mode"] == NIGHT_WAKING:
            left = data.get("pending_crawl") or 0
            return "正醒着，还没爬起来" + (f"（待爬 {left} 个会话）" if left else "")
        return (
            f"睡着中 剩余 {data.get('remaining_text', '?')} "
            f"预计 {data.get('wake_day_label', '')}{data.get('wake_label', '')} 醒 精力 {data['energy']:.0f}"
        )
