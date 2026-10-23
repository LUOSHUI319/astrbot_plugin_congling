"""决策日志。

它解决的是「为什么它刚才不回我」这类问题：把判断得分、阈值、抽签结果、
分句方案、预算消耗都落库，UI 里能一条条翻。只留最近 N 天。
"""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from .contracts import ReplyDecision, RhythmPlan


class DecisionLogger:
    def __init__(self, store: Any, config: Any) -> None:
        self.store = store
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.bool("debug.decision_log", True)

    async def log(
        self,
        *,
        session_id: str = "",
        msg_id: str = "",
        decided: str = "",
        reason: str = "",
        payload: dict[str, Any] | None = None,
        cost_tokens: int = 0,
    ) -> None:
        if not self.enabled or not getattr(self.store, "ready", False):
            return
        try:
            await self.store.execute(
                "INSERT INTO decision_log(ts, session_id, msg_id, decided, reason, plan_json, cost_tokens) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    int(time.time()),
                    session_id,
                    msg_id,
                    decided,
                    reason[:500],
                    json.dumps(payload or {}, ensure_ascii=False, default=str)[:8000],
                    int(cost_tokens),
                ),
            )
        except Exception as exc:
            logger.debug(f"[congling] 决策日志写入失败: {exc!r}")

    async def decide(self, decision: ReplyDecision, *, session_id: str = "", msg_id: str = "") -> None:
        await self.log(
            session_id=session_id,
            msg_id=msg_id,
            decided="reply" if decision.should_reply else "silent",
            reason=decision.reason,
            payload={
                "score": decision.score,
                "threshold": decision.threshold,
                "signals": decision.signals,
                "roll": decision.roll,
                "in_window": decision.in_window,
            },
        )

    async def planned(self, plan: RhythmPlan, *, session_id: str = "", msg_id: str = "", cost: int = 0) -> None:
        await self.log(
            session_id=session_id,
            msg_id=msg_id,
            decided="plan",
            reason=plan.reason,
            payload={
                "bubbles": plan.bubbles,
                "delays": plan.delays,
                "quote": plan.quote,
                "voice": plan.as_voice,
                "sticker": plan.sticker_tag,
            },
            cost_tokens=cost,
        )

    async def sent(self, plan: RhythmPlan, *, session_id: str = "", msg_id: str = "") -> None:
        await self.log(
            session_id=session_id,
            msg_id=msg_id,
            decided="sent",
            reason=f"{len(plan.bubbles)} 条 / {plan.total_chars()} 字",
            payload={"bubbles": plan.bubbles},
        )

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        if not getattr(self.store, "ready", False):
            return []
        rows = await self.store.fetch_all(
            "SELECT * FROM decision_log ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
        )
        for row in rows:
            try:
                row["plan"] = json.loads(row.get("plan_json") or "{}")
            except (ValueError, TypeError):
                row["plan"] = {}
            row.pop("plan_json", None)
            stamp = row.get("ts") or 0
            row["time"] = time.strftime("%m-%d %H:%M:%S", time.localtime(int(stamp)))
        return rows

    async def purge(self) -> None:
        days = self.config.int("debug.log_retention_days", 7)
        if days <= 0 or not getattr(self.store, "ready", False):
            return
        cutoff = int(time.time()) - days * 86400
        try:
            await self.store.execute("DELETE FROM decision_log WHERE ts < ?", (cutoff,))
        except Exception as exc:
            logger.debug(f"[congling] 清理日志失败: {exc!r}")
