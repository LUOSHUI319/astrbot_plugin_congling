"""全插件唯一的「现在几点」。

为什么要有这一个模块：作息窗口、起床点、最晚睡觉、按天切分的日记，原来全都
各自调 `time.localtime()`，跟着本机时区走。机器时区一设错，23:30 的疲劳期、
08:00 的起床点会一起偏掉，而且面板上显示的钟点还可能是另一个时间。

配置项 `clock.source`：

- `beijing`（默认）：固定 UTC+8。中国没有夏令时，所以它和 `Asia/Shanghai`
  完全等价，但不需要 `tzdata`——Windows 自带的 Python 没有时区库，
  为了一个固定偏移去加依赖，会破坏本插件「零强制依赖」的定位。
- `local`：继续跟本机时区，给不在东八区的人用。

`time.time()` 不动。它是绝对时间戳，和时区无关。
"""

from __future__ import annotations


import time
from datetime import datetime, timedelta, timezone
from typing import Any

# 固定偏移，名字写 CST 只是给日志看的
BEIJING = timezone(timedelta(hours=8), "CST")
SOURCES = ("beijing", "local")

_source = "beijing"


def configure(config: Any) -> str:
    """从插件配置读一次。返回最终生效的值。"""
    raw = ""
    try:
        raw = str(config.str("clock.source", "beijing") or "beijing").strip().lower()
    except Exception:
        raw = "beijing"
    return set_source(raw)


def set_source(name: str) -> str:
    global _source
    _source = name if name in SOURCES else "beijing"
    return _source


def source() -> str:
    return _source


def _tz() -> timezone | None:
    return BEIJING if _source == "beijing" else None


def now_dt(ts: float | None = None) -> datetime:
    """带时区的当前时间（`local` 模式下是不带时区的本地时间）。"""
    moment = time.time() if ts is None else float(ts)
    zone = _tz()
    return datetime.fromtimestamp(moment, zone) if zone else datetime.fromtimestamp(moment)


def struct(ts: float | None = None) -> time.struct_time:
    """兼容 `tm_hour` / `tm_min` 这类旧用法。"""
    return now_dt(ts).timetuple()


def hour_of(ts: float | None = None) -> int:
    return now_dt(ts).hour


def minute_of_day(ts: float | None = None) -> int:
    moment = now_dt(ts)
    return moment.hour * 60 + moment.minute


def day_key(ts: float | None = None) -> str:
    """按哪一天归档：%Y-%m-%d。"""
    return now_dt(ts).strftime("%Y-%m-%d")


def day_start(ts: float | None = None) -> int:
    """当天 0 点的时间戳，替代 `time.mktime(localtime(...))` 那套。"""
    moment = now_dt(ts)
    return int(moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def fmt(pattern: str, ts: float | None = None) -> str:
    """格式化时间戳，替代 `time.strftime(p, time.localtime(ts))`。"""
    return now_dt(ts).strftime(pattern)


def at_hhmm(hhmm: str, ts: float | None = None) -> int:
    """把 '08:00' 落到「当天那个时刻」的时间戳。解析失败返回 0。"""
    raw = str(hhmm or "").strip()
    if ":" not in raw:
        return 0
    try:
        hour, minute = (int(x) for x in raw.split(":", 1))
    except (TypeError, ValueError):
        return 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return 0
    return day_start(ts) + hour * 3600 + minute * 60


def moment_line(ts: float | None = None) -> str:
    """给模型看的一句「现在是几点」，附带用的是哪套钟。

    单独抽出来是为了让它排在注入内容的最后一段：这句话每轮都变，
    放在前面会把整段前缀打散；放最后，前面的记忆、人格、场景都不受影响。
    """
    moment = now_dt(ts)
    weekday = "一二三四五六日"[moment.weekday()]
    # describe() 本身带括号，直接塞进去会变成「（北京时间（UTC+8））」，这里重新拼
    zone_text = "北京时间 UTC+8" if _source == "beijing" else "本机时间"
    return (
        f"现在是 {moment.strftime('%Y-%m-%d')} 周{weekday} "
        f"{moment.strftime('%H:%M')}（{zone_text}）"
    )


def describe() -> str:
    if _source == "beijing":
        return "北京时间（UTC+8）"
    return "本机时间"
