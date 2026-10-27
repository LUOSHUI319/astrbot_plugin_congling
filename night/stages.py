"""睡眠阶段：刚入睡 / 浅层 / 深度 / 快速眼动。

设计上有三条硬约束（见 docs/sleep_stages_plan.md）：

1. **不落库**。阶段是派生量，`f(sleep_at, now)` 现算。存进 NightState 就得靠 tick 推进，
   插件重启、电脑休眠、时钟跳变之后必然对不上。
2. **不靠 tick 精度**。心跳可能一分钟一次也可能十分钟一次，用绝对时间求值就不受影响。
3. **要跟已有逻辑咬合**。dream / energy / wake 都从这里取系数，阶段才不只是个标签。

模型抄真实睡眠结构的两条规律：一个周期 90 分钟、一晚重复 4~6 次；
前半夜深睡多、后半夜 REM 多。
"""

from __future__ import annotations

import time
from typing import Any

STAGE_KEYS = ("n1", "n2", "n3", "rem")

STAGE_CN = {
    "n1": "刚入睡",
    "n2": "浅层睡眠",
    "n3": "深度睡眠",
    "rem": "快速眼动",
}

# 精力恢复系数：深睡那一小时补得最多，这跟「前半夜睡扎实、后半夜容易醒」对得上
ENERGY_BOOST = {"n1": 1.0, "n2": 1.2, "n3": 2.2, "rem": 1.3}

# 叫醒难度系数：深睡喊不动，刚躺下和 REM 一叫就应
WAKE_FACTOR = {"n1": 1.3, "n2": 0.8, "n3": 0.45, "rem": 1.1}

MAX_CYCLES = 24


# ================================================================ 配置读取
def enabled(config: Any) -> bool:
    try:
        return bool(config.bool("night.stage_enabled", True))
    except Exception:
        return True


def _int(config: Any, key: str, default: int) -> int:
    try:
        return int(config.int(key, default))
    except Exception:
        return default


def _float(config: Any, key: str, default: float) -> float:
    try:
        return float(config.float(key, default))
    except Exception:
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ================================================================ 分段
def _cycle(
    c: int,
    cycle: int,
    n1: int,
    deep_max: int,
    deep_decay: float,
    rem_base: int,
    rem_grow: int,
    rem_max: int,
) -> list[tuple[str, float, int]]:
    """第 c 个周期的分段：N1 → N2 → N3 → N2 → REM。"""
    head_n1 = n1 if c == 0 else 0
    deep = _clamp(round(deep_max * (1 - deep_decay * c)), 0, deep_max) if deep_max else 0
    rem = _clamp(round(rem_base + rem_grow * c), rem_base, rem_max) if rem_max else 0
    rest = cycle - head_n1 - deep - rem
    if rest < 6:
        # 配置被调得很怪（周期太短、深睡/REM 太长）时先压 REM 再压深睡，别让浅睡消失
        over = 6 - rest
        take = min(over, rem)
        rem -= take
        over -= take
        take = min(over, deep)
        deep -= take
        rest = max(2, cycle - head_n1 - deep - rem)
    before = round(rest * 0.6)
    after = rest - before
    out: list[tuple[str, float, int]] = []
    if head_n1:
        out.append(("n1", head_n1, c))
    out.append(("n2", before, c))
    if deep:
        out.append(("n3", deep, c))
    if after:
        out.append(("n2", after, c))
    if rem:
        out.append(("rem", rem, c))
    return out


def _noon_minutes(total: float, n1: int) -> list[tuple[str, float, int]]:
    """午睡：只走浅睡，尾巴上给一小段轻 REM（梦短、醒来只记得一句）。"""
    if total <= 0:
        return []
    n1m = min(float(n1), max(0.0, total * 0.2)) if n1 else 0.0
    rem = 8.0 if total >= 25 else 0.0
    rem = min(rem, max(0.0, total - n1m))
    n2 = max(0.0, total - n1m - rem)
    out: list[tuple[str, float, int]] = []
    if n1m > 0:
        out.append(("n1", n1m, 0))
    if n2 > 0:
        out.append(("n2", n2, 0))
    if rem > 0:
        out.append(("rem", rem, 0))
    return out


def build_plan(config: Any, *, sleep_at: int, wake_at: int, kind: str = "night") -> list[dict]:
    """整段睡眠计划 → [{key,label,start,end,cycle}, ...]，覆盖 sleep_at → wake_at。"""
    if not enabled(config):
        return []
    try:
        sleep_at = int(sleep_at or 0)
        wake_at = int(wake_at or 0)
    except (TypeError, ValueError):
        return []
    if sleep_at <= 0 or wake_at <= sleep_at:
        return []

    total_min = (wake_at - sleep_at) / 60.0
    cycle = max(20, _int(config, "night.stage_cycle_minutes", 90))
    n1 = max(0, _int(config, "night.stage_n1_minutes", 8))
    deep_max = max(0, _int(config, "night.stage_deep_max_minutes", 36))
    deep_decay = max(0.0, _float(config, "night.stage_deep_decay", 0.28))
    rem_base = max(0, _int(config, "night.stage_rem_base_minutes", 10))
    rem_grow = max(0, _int(config, "night.stage_rem_grow_minutes", 6))
    rem_max = max(rem_base, _int(config, "night.stage_rem_max_minutes", 28))

    light_only = False
    try:
        light_only = bool(config.bool("night.stage_noon_light_only", True)) and kind == "noon"
    except Exception:
        light_only = kind == "noon"

    minutes: list[tuple[str, float, int]] = []
    if light_only:
        minutes = _noon_minutes(total_min, n1)
    else:
        c = 0
        covered = 0.0
        while covered < total_min - 0.01 and c < MAX_CYCLES:
            chunk = _cycle(c, cycle, n1, deep_max, deep_decay, rem_base, rem_grow, rem_max)
            minutes.extend(chunk)
            covered += sum(item[1] for item in chunk)
            c += 1

    plan: list[dict] = []
    cursor = sleep_at
    for key, length, c in minutes:
        if cursor >= wake_at:
            break
        end = min(wake_at, cursor + int(round(length * 60)))
        if end <= cursor:
            continue
        plan.append(
            {
                "key": key,
                "label": STAGE_CN.get(key, key),
                "start": cursor,
                "end": end,
                "cycle": int(c),
            }
        )
        cursor = end

    # 配置有零头或者总长没盖满时，把最后一段拉到 wake_at，保证首尾严丝合缝
    if plan and plan[-1]["end"] < wake_at:
        plan[-1]["end"] = wake_at
    return plan


# ================================================================ 查询
def at(
    config: Any,
    *,
    sleep_at: int,
    wake_at: int,
    kind: str = "night",
    now: float | None = None,
) -> dict | None:
    """某一刻处在哪个阶段。睡醒/没睡时返回 None。"""
    plan = build_plan(config, sleep_at=sleep_at, wake_at=wake_at, kind=kind)
    if not plan:
        return None
    now_ts = int(now if now is not None else time.time())

    index = len(plan) - 1
    if now_ts < plan[0]["start"]:
        # 时钟回拨：还没到入睡点，按刚躺下算，别报「已经睡到最后一段」
        index = 0
    else:
        for i, seg in enumerate(plan):
            if seg["start"] <= now_ts < seg["end"]:
                index = i
                break

    seg = plan[index]
    span = max(1, seg["end"] - seg["start"])
    progress = _clamp((now_ts - seg["start"]) / span, 0.0, 1.0)
    return {
        "key": seg["key"],
        "label": seg["label"],
        "since": seg["start"],
        "until": seg["end"],
        "progress": round(progress, 3),
        "cycle": seg["cycle"],
        "index": index,
        "count": len(plan),
        "remaining": max(0, seg["end"] - now_ts),
    }


def stats(plan: list[dict]) -> dict[str, int]:
    """各阶段总分钟数。"""
    out: dict[str, int] = {key: 0 for key in STAGE_KEYS}
    for seg in plan or []:
        key = seg.get("key")
        if key not in out:
            out[key] = 0
        out[key] += int(round((int(seg.get("end") or 0) - int(seg.get("start") or 0)) / 60))
    return out


def seq(plan: list[dict], *, now: float | None = None, horizon: int = 0) -> list[dict]:
    """给甘特条用：当前段往后的一段序列。horizon=0 表示到计划末尾。"""
    if not plan:
        return []
    now_ts = int(now if now is not None else time.time())
    limit = now_ts + int(horizon) if horizon else plan[-1]["end"]
    picked = [seg for seg in plan if seg["end"] > now_ts and seg["start"] < limit]
    return picked or [plan[-1]]


def describe(stage: dict | None) -> str:
    """「深度睡眠 · 第 2 个周期」这种给人看的短句。"""
    if not stage:
        return ""
    return f"{stage.get('label') or ''} · 第 {int(stage.get('cycle') or 0) + 1} 个周期"


def energy_factor(stage: dict | None) -> float:
    if not stage:
        return 1.0
    return ENERGY_BOOST.get(str(stage.get("key") or ""), 1.0)


def wake_factor(stage: dict | None) -> float:
    if not stage:
        return 1.0
    return WAKE_FACTOR.get(str(stage.get("key") or ""), 1.0)
