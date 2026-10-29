"""验证「按段累加」真的生效：跨睡眠阶段补算，结果不能等于整段按终点阶段算。"""

import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE))

import night_selftest as st  # noqa: E402

st._prepare()

from cg.night import stages as stages_mod, energy as energy_mod  # noqa: E402
from cg.night.state import NightService  # noqa: E402

cfg = st.base_cfg(**{"night.stage_cycle_minutes": 90})
plugin = st.FakePlugin(cfg)
svc = NightService(plugin)

now = time.time()
sleep_at = int(now - 5.5 * 3600)
wake_at = int(now + 3600)
svc.state.sleep_at = sleep_at
svc.state.wake_at = wake_at
svc.state.sleep_kind = "night"
svc.state.energy = 0.0

# 分段版
seg = svc._recover_over(now - 5.5 * 3600, now)

# 旧算法：整段按终点那一刻的阶段
stage = stages_mod.at(cfg, sleep_at=sleep_at, wake_at=wake_at, kind="night", now=now)
flat = 5.5 * 60 * cfg.float("night.energy_recover_per_minute", 1.5) * (
    stages_mod.energy_factor(stage) if stage else 1.0
)

print(f"旧算法（整段按终点阶段）: {flat:.2f}")
print(f"新算法（按阶段分段累加）: {seg:.2f}")
print(f"差值: {abs(seg - flat):.2f}")

assert abs(seg - flat) > 0.05, "分段没生效：结果和整段算法一样"

# 再验一次：跨疲劳窗口
bounds = svc._awake_segments(now - 5.5 * 3600, now)
kinds = {k for _, _, k in bounds}
print(f"\n醒着分段数: {len(bounds)}  出现的疲劳类型: {sorted(kinds)}")
assert len(bounds) >= 1, "分段为空"

print("\nPASS")
