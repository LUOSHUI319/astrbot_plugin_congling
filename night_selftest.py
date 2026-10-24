"""长夜如梦 · 离线自检。

不依赖 AstrBot，把 night 下几个模块的纯逻辑跑一遍。
跑法：python night_selftest.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
import time
import types

BASE = r"E:\BotLauncher\AstrBot\AstrBot\data\plugins\astrbot_plugin_congling"


# ---------------------------------------------------------------- 环境准备
def _mock_astrbot() -> None:
    astrbot = types.ModuleType("astrbot")
    sys.modules["astrbot"] = astrbot
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("night.selftest")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    astrbot.api = api
    sys.modules["astrbot.api"] = api


def _mock_pkg() -> None:
    for name, path in (
        ("cg", BASE),
        ("cg.core", os.path.join(BASE, "core")),
        ("cg.night", os.path.join(BASE, "night")),
    ):
        mod = types.ModuleType(name)
        mod.__path__ = [path]
        mod.__package__ = name
        sys.modules[name] = mod


def _load(fullname: str, relpath: str):
    path = os.path.join(BASE, relpath)
    spec = importlib.util.spec_from_file_location(fullname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = mod
    spec.loader.exec_module(mod)
    return mod


def _prepare():
    _mock_astrbot()
    _mock_pkg()
    _load("cg.core.contracts", os.path.join("core", "contracts.py"))
    _load("cg.night.energy", os.path.join("night", "energy.py"))
    _load("cg.night.wake", os.path.join("night", "wake.py"))
    _load("cg.night.lullaby", os.path.join("night", "lullaby.py"))
    _load("cg.night.close", os.path.join("night", "close.py"))
    _load("cg.night.crawl", os.path.join("night", "crawl.py"))
    _load("cg.night.dream", os.path.join("night", "dream.py"))
    return _load("cg.night.state", os.path.join("night", "state.py"))


# ---------------------------------------------------------------- 假件
class FakeCfg:
    def __init__(self, data=None):
        self.d = data or {}

    def str(self, key, default=""):
        return str(self.d.get(key, default))

    def int(self, key, default=0):
        try:
            return int(self.d.get(key, default))
        except (TypeError, ValueError):
            return default

    def float(self, key, default=0.0):
        try:
            return float(self.d.get(key, default))
        except (TypeError, ValueError):
            return default

    def bool(self, key, default=False):
        return bool(self.d.get(key, default))

    def list(self, key, default=None):
        return self.d.get(key, default if default is not None else [])


class FakeStore:
    def __init__(self):
        self.kv = {}

    async def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key, value):
        self.kv[key] = value

    async def scalar(self, sql, params=()):
        return 0

    async def fetch_all(self, sql, params=()):
        return []


class FakePlugin:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = FakeStore()
        self.models = None


def base_cfg(**over):
    data = {
        "night.enabled": True,
        "night.night_fatigue": "23:30-07:00",
        "night.noon_fatigue": "13:00-14:30",
        "night.energy_max": 100,
        "night.energy_per_message": 4.0,
        "night.energy_per_minute": 0.8,
        "night.energy_recover_per_minute": 1.5,
        "night.energy_after_wake": 35,
        "night.min_turns_before_sleep": 8,
        "night.wake_time": "08:00",
        "night.wake_jitter_minutes": 0,
        "night.latest_sleep_time": "02:30",
        "night.wake_mode": "private_only",
        "night.wake_window_minutes": 10,
        "night.wake_start_count": 2,
        "night.wake_step_count": 4,
        "night.dream_enabled": False,
    }
    data.update(over)
    return FakeCfg(data)


# ---------------------------------------------------------------- 用例
async def case_state(state_mod, contracts):
    print("\n========== 状态机 ==========")
    plugin = FakePlugin(base_cfg())
    svc = state_mod.NightService(plugin)
    await svc.load()
    print("初始：", svc.status_text())

    PM = contracts.PerceivedMessage
    msg = PM(session_id="p1", session_kind="private", text="在吗")

    # 精力压到 5，测下限保护
    svc.state.energy = 5.0
    svc._fatigue_turns = 0
    svc._fatigue_entered = int(time.time())
    expect_fatigue = svc.state.is_asleep() is False

    slept_at = 0
    for n in range(1, 15):
        await svc.on_message(msg)
        if svc.state.is_asleep():
            slept_at = n
            break
    print(f"疲劳期内第 {slept_at} 条消息后入睡（下限保护 8 轮）")
    print("  入睡原因：", svc.state.reason)
    print("  状态：", svc.status_text())
    assert slept_at >= 8, "下限保护没生效，睡得太早"
    assert svc.state.is_asleep(), "没有进入睡眠"

    print("\n睡眠中继续发私聊：")
    for n in range(1, 6):
        result = await svc.on_message(
            PM(session_id="p1", session_kind="private", text="喂")
        )
        print(f"  第{n}条 block={result.get('block')} 说明={result.get('hint')}")
        if not result.get("block"):
            print("  -> 被叫醒了")
            break

    result = await svc.on_message(
        PM(session_id="g1", session_kind="group", text="早安")
    )
    print(f"\n群聊发消息：block={result.get('block')} 说明={result.get('hint')}")
    assert result.get("block"), "private_only 档下群聊不该唤醒"

    await svc.wake_now(reason="selftest")
    print("\n强制醒来：", svc.status_text())
    print("  精力：", svc.state.energy)
    assert svc.state.energy == 100, "正常醒来精力该满格"
    return True


async def case_energy(state_mod):
    print("\n========== 精力与起床时间 ==========")
    import cg.night.energy as E

    cfg = base_cfg()

    print("时段：", E.parse_window("23:30-07:00"))
    for label, hour in (("凌晨2点", 2), ("上午10点", 10), ("下午13:30", 13)):
        minute = hour * 60 + (30 if hour == 13 else 0)
        print(f"  {label} 疲劳档：{E.fatigue_kind(cfg, _at(minute)) or '无'}")

    print("\n起床时间（固定8点，浮动90）：")
    cfg_jitter = base_cfg(**{"night.wake_jitter_minutes": 90})
    base_ts = _at(2 * 60)
    for _ in range(8):
        stamp = E.next_wake_time(cfg_jitter, base_ts)
        print("  ", time.strftime("%H:%M", time.localtime(stamp)))
        assert 6 * 60 + 30 <= time.localtime(stamp).tm_hour * 60 + time.localtime(stamp).tm_min <= 9 * 60 + 30, "超出 8点±90分"

    print("\n浮动 0（固定点）连续 3 次：")
    for _ in range(3):
        stamp = E.next_wake_time(cfg, base_ts)
        print("  ", time.strftime("%H:%M", time.localtime(stamp)))

    drained = E.drain(100, minutes=10, messages=5, per_minute=0.8, per_message=4)
    print(f"\n扣精力：100 - (10分钟*0.8 + 5条*4) = {drained:.0f}")
    assert drained == 72
    return True


def _at(minute_of_day: int) -> float:
    return time.mktime((2026, 10, 25, minute_of_day // 60, minute_of_day % 60, 0, 0, 0, -1))


async def case_close():
    print("\n========== 结束语识别 ==========")
    import cg.night.close as C

    cfg = base_cfg()
    cases = [
        ("晚安", True),
        ("晚安呀", True),
        ("那我先睡了，你也早点", True),
        ("他昨天晚上跟我说晚安了", False),
        ("你睡了吗", False),
        ("晚安曲这首歌不错", False),
        ("拜拜歌真好听", False),
        ("先这样吧", True),
        ("去忙了哈", True),
        ("拜拜", True),
        ("午安", True),
    ]
    for text, expect in cases:
        ok, strength, word = C.is_closing(text, cfg)
        flag = "✓" if ok == expect else "✗"
        print(f"  {flag} {text[:22]:<24} -> {'收线' if ok else '不算'} {strength}")
        assert ok == expect, f"{text} 判断错了"
    return True


async def case_wake():
    print("\n========== 唤醒概率 ==========")
    import collections
    import cg.night.wake as W
    import cg.core.contracts as contracts

    plugin = FakePlugin(base_cfg())
    waker = W.Waker(plugin)
    PM = contracts.PerceivedMessage

    dist = collections.Counter()
    trials = 2000
    for _ in range(trials):
        waker.reset_all()
        for n in range(1, 12):
            attempt = waker.check(
                PM(session_id="p", session_kind="private", text="在吗")
            )
            if attempt.woke:
                dist[n] += 1
                break
    print("  私聊 y=2 n=4，醒在第几条：")
    for n in sorted(dist):
        print(f"    第{n}条：{dist[n] / trials:.1%}")
    assert dist[2] / trials > 0.12, "第2条唤醒率太低"
    assert sum(dist.values()) == trials, "有 trial 没醒"
    assert max(dist) <= 6, "超过 y+n 还醒不了"

    plugin2 = FakePlugin(base_cfg(**{"night.wake_mode": "both"}))
    waker2 = W.Waker(plugin2)
    no_at = waker2.check(
        PM(session_id="g", session_kind="group", text="随便说"), self_id="123"
    )
    with_at = waker2.check(
        PM(session_id="g", session_kind="group", text="@你", mentions=["@123"]),
        self_id="123",
    )
    print(f"  群聊没@：{no_at.reason}")
    print(f"  群聊被@：{with_at.reason}")
    assert not no_at.allowed, "群聊没被@不该进入掷骰"
    return True


async def case_lullaby():
    print("\n========== 道别语 ==========")
    import cg.night.lullaby as L

    def make(**over):
        return L.Lullaby(FakePlugin(base_cfg(**over)))

    same = make(**{"night.farewell_same": True}).compose_many(["a", "b", "c"])
    print("  same=开：", same)
    assert len(set(same.values())) == 1

    differ = make(**{"night.farewell_same": False, "night.farewell_random": False})
    differ_map = differ.compose_many(["a", "b", "c"])
    print("  都关：", differ_map)
    assert len(set(differ_map.values())) == len(differ_map)

    rand = make(**{"night.farewell_same": False, "night.farewell_random": True})
    unified = 0
    for _ in range(20):
        result = rand.compose_many(["a", "b", "c", "d"])
        if len(set(result.values())) == 1:
            unified += 1
    print(f"  random=开：20 次里统一了 {unified} 次")
    assert 0 < unified < 20, "随机开关没起作用"

    print("  随便看几句：")
    for _ in range(6):
        line = rand.compose(session_id="demo")
        print("    ", line)
        assert " " not in line, "道别语里不该有空格"
        assert not line.endswith("。"), "道别语不该以句号结尾"
    return True


async def case_crawl():
    print("\n========== 爬楼 ==========")
    import cg.night.crawl as Cr

    crawler = Cr.Crawler(FakePlugin(base_cfg()))
    print("  爬楼语：")
    for _ in range(6):
        print("    ", await crawler.crawl_line())
    count = await crawler.unread_count("p1", 0)
    print("  未爬楼（空库）：", count)
    await crawler.mark_crawled("p1")
    print("  记录爬楼时间戳：", await crawler.last_crawled("p1") > 0)
    return True


async def case_dream():
    print("\n========== 做梦 ==========")
    import cg.night.dream as D
    import cg.core.contracts as contracts

    cfg = base_cfg(**{"night.dream_enabled": True})
    dreamer = D.Dreamer(FakePlugin(cfg))
    await dreamer.load()

    now = time.time()
    print("  入睡 30 分钟该不该做：", dreamer.due(sleep_at=int(now - 1800), already=0))
    print("  入睡 70 分钟该不该做：", dreamer.due(sleep_at=int(now - 4200), already=0))
    print("  已经做满 8 段：", dreamer.due(sleep_at=int(now - 4200), already=8))
    assert dreamer.due(sleep_at=int(now - 4200), already=0) is True
    assert dreamer.due(sleep_at=int(now - 4200), already=8) is False

    print("  各等级醒后提梦概率：")
    for level in range(1, 6):
        print(f"    {level} 级 -> {dreamer.remember_chance(level):.0%}")

    entry = contracts.DreamEntry(
        dream_id="d1", created_at=int(now), level=4,
        tags=["噩梦"], text="梦里走廊又变长了", decay_per_day=0.08,
    )
    dreamer._dreams.append(entry)
    print("  模拟衰减 5 天：", end=" ")
    for day in range(5):
        for dream in dreamer._dreams:
            dream.strength *= 0.92
        print(f"{entry.strength:.2f}", end=" ")
    print()
    assert entry.strength > 0.6, "4 级梦衰减太快"
    return True


async def main():
    state_mod = _prepare()
    contracts = sys.modules["cg.core.contracts"]
    results = []
    for name, coro in (
        ("状态机", case_state(state_mod, contracts)),
        ("精力", case_energy(state_mod)),
        ("结束语", case_close()),
        ("唤醒", case_wake()),
        ("道别语", case_lullaby()),
        ("爬楼", case_crawl()),
        ("做梦", case_dream()),
    ):
        try:
            await coro
            results.append((name, "通过"))
        except AssertionError as exc:
            results.append((name, f"断言失败：{exc}"))
        except Exception as exc:
            results.append((name, f"异常：{type(exc).__name__}: {exc}"))

    print("\n================ 汇总 ================")
    for name, result in results:
        mark = "✓" if result == "通过" else "✗"
        print(f"  {mark} {name}: {result}")
    return all(result == "通过" for _, result in results)


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
