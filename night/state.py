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
from . import energy as energy_mod
from .lullaby import Lullaby
from .wake import Waker

KV_KEY = "night.state"

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

        self._lock = asyncio.Lock()
        self._fatigue_turns = 0      # 本轮疲劳期内已经处理过多少条消息
        self._fatigue_entered = 0    # 进入本轮疲劳期的时间戳
        self._last_sleep_kind = ""   # night / noon，决定道别语用哪套
        self._loaded = False

    # ================================================================ 开关
    @property
    def enabled(self) -> bool:
        return self.config.bool("night.enabled", False)

    # ================================================================ 生命周期
    async def start(self) -> None:
        if not self.enabled:
            logger.info("[congling] 长夜如梦未启用")
            return
        await self.load()
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
            logger.debug(f"[congling] 读取作息状态失败: {exc!r}")
        if isinstance(raw, dict):
            self.state = NightState.from_dict(raw)
        elif isinstance(raw, str):
            import json

            try:
                self.state = NightState.from_dict(json.loads(raw))
            except Exception:
                self.state = NightState()
        self._loaded = True
        await self.tick()

    async def save(self) -> None:
        try:
            await self.store.kv_set(KV_KEY, self.state.to_dict())
        except Exception as exc:
            logger.debug(f"[congling] 保存作息状态失败: {exc!r}")

    # ================================================================ 时间推进
    async def tick(self, now: float | None = None) -> list[str]:
        """按时间差补算状态。返回发生的事件名，方便日志和面板。"""
        now = now or time.time()
        if not self.enabled:
            # 只有「收线静默」在跑：不管精力，只管到点自然醒
            if self.state.is_asleep() and self.state.wake_at and now >= self.state.wake_at:
                await self._do_wake(reason="natural")
                return ["wake_natural"]
            return []
        events: list[str] = []

        if self.state.last_tick <= 0:
            self.state.last_tick = int(now)
            await self.save()
            return events

        elapsed_minutes = max(0.0, (now - self.state.last_tick) / 60.0)
        self.state.last_tick = int(now)

        if self.state.is_asleep():
            # 睡着的时候精力冻结，只检查要不要自然醒
            if self.state.wake_at and now >= self.state.wake_at:
                await self._do_wake(reason="natural")
                events.append("wake_natural")
        else:
            kind = energy_mod.fatigue_kind(self.config, now)
            if kind:
                if not self._fatigue_entered:
                    self._fatigue_entered = int(now)
                    self._fatigue_turns = 0
                # 精力随时间自然消耗（消息消耗在 on_message 里另算）
                self.state.energy = energy_mod.drain(
                    self.state.energy,
                    minutes=elapsed_minutes,
                    messages=0,
                    per_minute=self.config.float("night.energy_per_minute", 0.8),
                    per_message=0.0,
                )
                if self._can_sleep_by_energy():
                    await self._do_sleep(reason="energy", kind=kind)
                    events.append("sleep_energy")
            else:
                self._fatigue_entered = 0
                self._fatigue_turns = 0
                self.state.energy = energy_mod.recover(
                    self.state.energy,
                    minutes=elapsed_minutes,
                    per_minute=self.config.float("night.energy_recover_per_minute", 1.5),
                )

            # 最晚睡觉时间兜底
            if not self.state.is_asleep() and self._past_latest_sleep(now):
                await self._do_sleep(reason="latest", kind="night")
                events.append("sleep_latest")

        if events:
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
            # 醒着：只扣精力、累计疲劳期轮次
            if energy_mod.in_fatigue(self.config):
                self._fatigue_turns += 1
                self.state.energy = energy_mod.drain(
                    self.state.energy,
                    minutes=0.0,
                    messages=1,
                    per_minute=0.0,
                    per_message=self.config.float("night.energy_per_message", 4.0),
                )
                if self._can_sleep_by_energy():
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
        if attempt.woke:
            self.state.mode = NIGHT_WAKING
            await self.save()
            return {
                "block": False,
                "wake": attempt,
                "waking": True,
                "hint": attempt.reason,
            }

        return {
            "block": True,
            "wake": attempt,
            "hint": attempt.reason,
        }

    # ================================================================ 睡 / 醒
    async def _do_sleep(self, *, reason: str, kind: str = "night", force: bool = False) -> None:
        # allow_sleep 关掉时只算精力、不真的睡：想聊天的时候把它关掉就行
        # force=True 来自手动点的「睡」，不受这个开关影响
        if not force and not self.config.bool("night.allow_sleep", True):
            self.state.energy = max(
                self.state.energy,
                self.config.float("night.energy_after_wake", 35.0),
            )
            logger.info(f"[congling] 长夜如梦：allow_sleep 关闭，跳过入睡（本来原因={reason}）")
            return
        now = time.time()
        self.state.mode = NIGHT_ASLEEP
        self.state.sleep_at = int(now)
        self.state.wake_at = energy_mod.next_wake_time(self.config, now, kind=kind)
        self.state.reason = reason
        self.state.dream_count_today = 0
        # 午睡要掷一次骰：这一次到底做不做梦
        if (kind or "night") == "noon":
            chance = max(0.0, min(1.0, self.config.float("night.noon_dream_chance", 0.5)))
            self.state.dream_skip = random.random() >= chance
        else:
            self.state.dream_skip = False
        self._last_sleep_kind = kind or "night"
        self.waker.reset_all()
        await self.save()
        logger.info(
            f"[congling] 入睡：原因={reason} "
            f"预计醒来={time.strftime('%H:%M', time.localtime(self.state.wake_at))}"
        )

    async def _do_wake(self, *, reason: str = "natural") -> None:
        self.state.mode = NIGHT_AWAKE
        self.state.sleep_at = 0
        self.state.wake_at = 0
        self.state.reason = ""
        # 正常睡醒：精力满格，取消一切精力影响
        self.state.energy = float(self.config.int("night.energy_max", 100))
        self._fatigue_turns = 0
        self._fatigue_entered = 0
        self.waker.reset_all()
        await self.save()
        logger.info(f"[congling] 醒来：原因={reason}")

    async def sleep_now(self, *, reason: str = "manual", kind: str | None = None) -> None:
        """外部调用（面板/指令）强制入睡。

        kind 不传时按当前时段自动判断：在午间疲劳窗口里就是午睡，
        否则算夜里睡。免得下午手点一下「睡」，一路睡到第二天早上。
        """
        if not kind:
            try:
                kind = energy_mod.fatigue_kind(self.config) or "night"
            except Exception:
                kind = "night"
        await self._do_sleep(reason=reason, kind=kind, force=True)

    async def wake_now(self, *, reason: str = "manual") -> None:
        """外部调用强制唤醒。"""
        await self._do_wake(reason=reason)

    async def finish_waking(self) -> None:
        """爬楼结束后调，把 waking 落回 awake。"""
        if self.state.mode == NIGHT_WAKING:
            self.state.mode = NIGHT_AWAKE
            self.state.energy = float(self.config.int("night.energy_after_wake", 35))
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
            "crawl_on": self.config.bool("night.crawl_enabled", True),
            "allow_sleep": self.config.bool("night.allow_sleep", True),
        }
        if state.is_asleep():
            data["sleep_at"] = state.sleep_at
            data["wake_at"] = state.wake_at
            data["sleep_reason"] = state.reason
            data["remaining"] = max(0, state.wake_at - int(now))
            data["remaining_text"] = energy_mod.human_duration(max(0, state.wake_at - int(now)))
        return data

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
            return "正醒着，还没爬起来"
        return (
            f"睡着中 剩余 {data.get('remaining_text', '?')} "
            f"预计 {data['wake_window']} 醒 精力 {data['energy']:.0f}"
        )
