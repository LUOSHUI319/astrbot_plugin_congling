"""精力与疲劳期。

这里全是纯函数，不碰存储、不碰事件，方便单测。
状态机只管什么时候调用，怎么算交在这里。

时间一律用本地时区，跨午夜用「分钟数取模」处理，不搞 datetime 比较那套。
"""

from __future__ import annotations

import random
import time
from typing import Any

DEFAULT_NOON = "13:00-14:30"
DEFAULT_NIGHT = "23:30-07:00"


# ---------------------------------------------------------------- 时段解析
def _parse_hhmm(text: str) -> int | None:
    """'23:30' -> 1410（当天第几分钟）。解析失败返回 None。"""
    if not text:
        return None
    raw = str(text).strip()
    if "-" in raw:  # 传了整段区间，只取前半
        raw = raw.split("-", 1)[0]
    parts = raw.split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def parse_window(spec: str) -> tuple[int, int] | None:
    """'23:30-07:00' -> (1410, 420)。空串或非法返回 None。"""
    if not spec:
        return None
    raw = str(spec).strip()
    if not raw or "-" not in raw:
        return None
    left, right = raw.split("-", 1)
    start = _parse_hhmm(left)
    end = _parse_hhmm(right)
    if start is None or end is None:
        return None
    if start == end:
        return None
    return start, end


def in_window(now_minute: int, window: tuple[int, int] | None) -> bool:
    if window is None:
        return False
    start, end = window
    if start < end:
        return start <= now_minute < end
    # 跨午夜
    return now_minute >= start or now_minute < end


def now_minute(ts: float | None = None) -> int:
    stamp = time.localtime(ts if ts is not None else time.time())
    return stamp.tm_hour * 60 + stamp.tm_min


# ---------------------------------------------------------------- 疲劳期
def fatigue_kind(config: Any, ts: float | None = None) -> str:
    """返回 'noon' / 'night' / ''。夜间优先。"""
    minute = now_minute(ts)
    night = parse_window(config.str("night.night_fatigue", DEFAULT_NIGHT))
    noon = parse_window(config.str("night.noon_fatigue", DEFAULT_NOON))
    if in_window(minute, night):
        return "night"
    if in_window(minute, noon):
        return "noon"
    return ""


def in_fatigue(config: Any, ts: float | None = None) -> bool:
    return bool(fatigue_kind(config, ts))


# ---------------------------------------------------------------- 精力
def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def drain(
    energy: float,
    *,
    minutes: float,
    messages: int,
    per_minute: float,
    per_message: float,
) -> float:
    """疲劳期内扣精力。"""
    spend = max(0.0, minutes) * max(0.0, per_minute) + max(0, messages) * max(0.0, per_message)
    return clamp(energy - spend)


def recover(energy: float, *, minutes: float, per_minute: float) -> float:
    """非疲劳期回精力。"""
    return clamp(energy + max(0.0, minutes) * max(0.0, per_minute))


def exhausted(energy: float, threshold: float = 0.5) -> bool:
    return energy <= threshold


# ---------------------------------------------------------------- 起床时间
def next_wake_time(
    config: Any,
    ts: float | None = None,
    *,
    kind: str = "night",
    rng: random.Random | None = None,
) -> int:
    """算出这次该几点醒。

    夜里睡：固定起床点 ± 随机浮动，浮动值有上限（默认 90 分钟，硬上限 90）。
    午睡：按固定时长（默认 60 分钟 ± 15）算，不跟着夜间起床点走，
          否则下午睡下去会一路算到第二天早上八点。
    """
    base = ts if ts is not None else time.time()
    rng = rng or random

    if (kind or "night") == "noon":
        nap = config.int("night.noon_nap_minutes", 60)
        nap = max(10, min(180, nap))
        wobble = config.int("night.noon_nap_jitter_minutes", 15)
        wobble = max(0, min(60, wobble))
        offset = rng.randint(-wobble, wobble) * 60 if wobble else 0
        return int(base + nap * 60 + offset)

    wake_minute = _parse_hhmm(config.str("night.wake_time", "08:00"))
    if wake_minute is None:
        wake_minute = 8 * 60

    jitter_max = config.int("night.wake_jitter_minutes", 90)
    jitter_max = max(0, min(90, jitter_max))  # 硬上限 90

    local = time.localtime(base)
    today = time.mktime(
        (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)
    )
    base_wake = today + wake_minute * 60

    # 如果今天这个点已经过去了（比如凌晨 2 点睡，目标 8 点），用今天的
    # 如果把固定点设在凌晨（比如 06:00 而现在是 23:00），同样用今天
    if base_wake <= base:
        # 已经过了今天的点，说明要睡到明天
        tomorrow = time.localtime(base + 86400)
        base_wake = time.mktime(
            (tomorrow.tm_year, tomorrow.tm_mon, tomorrow.tm_mday, 0, 0, 0, 0, 0, -1)
        ) + wake_minute * 60

    if jitter_max <= 0:
        return int(base_wake)

    offset = rng.randint(-jitter_max, jitter_max) * 60
    target = base_wake + offset
    # 兜底：至少睡 20 分钟，不能算出一个马上就到的时间
    minimum = base + 20 * 60
    if target < minimum:
        target = minimum
    return int(target)


def wake_window_text(config: Any, base: int) -> str:
    """给面板看的「预计 07:00-09:00 醒来」文案。"""
    wake_minute = _parse_hhmm(config.str("night.wake_time", "08:00")) or 480
    jitter = max(0, min(90, config.int("night.wake_jitter_minutes", 90)))
    early = (wake_minute - jitter) % 1440
    late = (wake_minute + jitter) % 1440
    fmt = lambda m: f"{m // 60:02d}:{m % 60:02d}"  # noqa: E731
    if jitter <= 0:
        return fmt(wake_minute)
    return f"{fmt(early)}-{fmt(late)}"


def human_duration(seconds: float) -> str:
    """把秒数说成人话，给面板和日志用。"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if minutes:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{hours} 小时"
