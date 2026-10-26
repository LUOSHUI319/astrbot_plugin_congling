"""遗忘曲线。

四种档位，同一个接口：

- off        不衰减，只按天数走归档线（0.4.x 的老行为）
- dream      照长夜如梦的梦境衰减来，忘得快、有性格
- balanced   默认。dream 与 ebbinghaus 的中间地带
- ebbinghaus 经典遗忘曲线，最保守

关键实现选择：**按「距上次被想起多少天」重算保留率，而不是每天拿旧值乘一次**。
这样维护任务跑一次、跑两次结果一样（幂等），差一天也不会因为累积误差跑偏。
"""

from __future__ import annotations

import math
from typing import Any

# 每日比：等级 -> 每天还剩多少
DAILY_KEEP = {
    "dream": {1: 0.70, 2: 0.82, 3: 0.90, 4: 0.96, 5: 0.99},
    "balanced": {1: 0.53, 2: 0.77, 3: 0.88, 4: 0.96, 5: 0.99},
}

# ebbinghaus 的「1 单位周期」基准（天）。
# 这组数是往「比 dream 更保守」方向校过的：低等级也能撑几天，高等级能撑大半年。
S_BASE = {1: 3, 2: 8, 3: 18, 4: 45, 5: 120}
S_GROWTH = {1: 1.4, 2: 1.4, 3: 1.5, 4: 1.6, 5: 1.8}
S_CEILING = 8.0

MODES = ("off", "dream", "balanced", "ebbinghaus")


class ForgettingCurve:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg if plugin is not None else None

    # ------------------------------------------------------------ 配置
    @property
    def mode(self) -> str:
        if self.config is None:
            return "balanced"
        mode = str(self.config.str("memory.forgetting.mode", "balanced") or "balanced").lower()
        return mode if mode in MODES else "balanced"

    @property
    def archive_ratio(self) -> float:
        if self.config is None:
            return 0.45
        return max(0.05, min(0.95, self.config.float("memory.forgetting.archive_ratio", 0.45)))

    @property
    def forget_ratio(self) -> float:
        if self.config is None:
            return 0.12
        return max(0.01, min(self.archive_ratio, self.config.float("memory.forgetting.forget_ratio", 0.12)))

    @property
    def hit_boost(self) -> float:
        if self.config is None:
            return 1.5
        return max(1.0, min(3.0, self.config.float("memory.forgetting.hit_boost", 1.5)))

    # ------------------------------------------------------------ 计算
    def decay_at(self, level: int, days: float, hits: int = 0) -> float:
        """某条记忆在「days 天没被想起」之后的保留率。"""
        level = max(0, min(5, int(level or 0)))
        if level == 0:
            return 1.0
        days = max(0.0, float(days))
        mode = self.mode
        if mode == "off":
            return 1.0
        if mode == "dream":
            return self._keep_decay(level, days)
        if mode == "ebbinghaus":
            return self._exp_decay(level, days, hits)
        # balanced：两条曲线的保留率取平均。
        # 注意不能拿「每日保留比」去平均——等比衰减下差之毫厘会指数放大，
        # 二十天后反而比 dream 掉得更狠，那就不是「中间地带」了。
        middle = (self._keep_decay(level, days) + self._exp_decay(level, days, hits)) / 2.0
        return max(0.02, min(1.0, middle))

    def _keep_decay(self, level: int, days: float) -> float:
        keep = DAILY_KEEP["dream"].get(level, 0.9)
        try:
            return max(0.02, min(1.0, keep ** days))
        except OverflowError:
            return 0.02

    def _exp_decay(self, level: int, days: float, hits: int = 0) -> float:
        stability = S_BASE.get(level, 7) * min(
            S_CEILING, S_GROWTH.get(level, 1.4) ** max(0, int(hits or 0))
        )
        try:
            return max(0.02, min(1.0, math.exp(-days / max(0.2, stability))))
        except OverflowError:
            return 0.02

    def next_review_days(self, level: int, hits: int = 0) -> int:
        """过多少天该重新评估一次（不为 0 才有意义）。"""
        level = max(0, min(5, int(level or 0)))
        if level == 0:
            return 0
        if self.mode == "ebbinghaus":
            base = S_BASE.get(level, 7) * min(
                S_CEILING, S_GROWTH.get(level, 1.4) ** max(0, int(hits or 0))
            )
            return max(1, int(round(base * 0.8)))
        keep = DAILY_KEEP["dream"].get(level, 0.9)
        dream_days = 0
        if keep < 1.0:
            try:
                dream_days = max(1, int(round(math.log(self.archive_ratio) / math.log(keep))))
            except (ValueError, ZeroDivisionError):
                dream_days = 1
        if self.mode == "dream":
            return dream_days
        # balanced：取两档到达归档线的天数平均
        exp_days = max(1, int(round(self._exp_review_days(level, hits))))
        return max(1, int(round((dream_days + exp_days) / 2)))

    def _exp_review_days(self, level: int, hits: int = 0) -> float:
        stability = S_BASE.get(level, 7) * min(
            S_CEILING, S_GROWTH.get(level, 1.4) ** max(0, int(hits or 0))
        )
        try:
            return max(1.0, -stability * math.log(self.archive_ratio))
        except (ValueError, ZeroDivisionError):
            return 1.0

    def status_for(self, decay: float) -> str:
        if decay < self.forget_ratio:
            return "forgotten"
        if decay < self.archive_ratio:
            return "archived"
        return "active"

    def label(self) -> str:
        return {
            "off": "不衰减",
            "dream": "长夜式（忘得快）",
            "balanced": "均衡（默认）",
            "ebbinghaus": "经典遗忘曲线（保守）",
        }.get(self.mode, self.mode)

    # ------------------------------------------------------------ 给 UI
    def preview(self, days: int = 45, hits: int = 0) -> dict[str, Any]:
        """返回每条曲线 1~5 级的每日保留率，UI 直接画折线。"""
        days = max(7, min(180, int(days or 45)))
        series: dict[str, list[float]] = {}
        for level in range(1, 6):
            points: list[float] = []
            for day in range(days + 1):
                points.append(round(self.decay_at(level, day, hits), 4))
            series[str(level)] = points
        return {
            "mode": self.mode,
            "label": self.label(),
            "days": days,
            "archive_ratio": self.archive_ratio,
            "forget_ratio": self.forget_ratio,
            "series": series,
            "review_days": {str(level): self.next_review_days(level, hits) for level in range(0, 6)},
        }
