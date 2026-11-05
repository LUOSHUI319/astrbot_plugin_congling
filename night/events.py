"""精力流水：独立 SQLite 表，记录失败不影响消息和作息。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from astrbot.api import logger

LABELS = {
    "snapshot": "开始观察",
    "message_drain": "收到消息扣精力",
    "fatigue_drain": "疲劳时段按时间消耗",
    "awake_recover": "非疲劳时段恢复",
    "sleep_recover": "睡眠恢复",
    "sleep": "进入睡眠",
    "sleep_skipped": "入睡被开关阻止",
    "sleep_guard": "最低消息轮数保护",
    "closing": "识别到收线结束语",
    "closing_cancelled": "取消待入睡任务",
    "closing_skipped": "结束语未触发入睡",
    "wake_check": "睡眠中的唤醒判定",
    "wake": "醒来",
    "waking_done": "醒后补课结束",
}

REASONS = {
    "energy": "精力耗尽",
    "latest": "到达最晚入睡时间",
    "talk": "结束语收线",
    "manual": "手动操作",
    "natural": "到点自然醒",
    "energy_full": "精力满格自动醒",
    "message": "被消息叫醒",
    "allow_sleep_off": "关闭真睡开关",
    "strong_off": "强档入睡开关已关闭",
    "closing_invalid": "收线任务过期或相关开关关闭",
    "conversation_continued": "会话已有新消息",
}


class NightEvents:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.ready = False
        self._lock = asyncio.Lock()
        self._last_prune = 0.0

    async def prepare(self) -> None:
        async with self._lock:
            if self.ready:
                return
            await self.store.execute("""
                CREATE TABLE IF NOT EXISTS night_energy_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL, event TEXT NOT NULL,
                    energy_before REAL NOT NULL, energy_after REAL NOT NULL,
                    delta REAL NOT NULL, mode_before TEXT, mode_after TEXT,
                    reason TEXT, session_id TEXT, msg_id TEXT, details_json TEXT
                )
            """)
            await self.store.execute(
                "CREATE INDEX IF NOT EXISTS idx_night_energy_ts ON night_energy_log(ts)"
            )
            self.ready = True

    async def add(self, event: str, before: float, state: Any, *,
                  mode_before: str = "", reason: str = "", session_id: str = "",
                  msg_id: str = "", ts: float | None = None, **details: Any) -> None:
        # 在线程写入/建表前固定快照，避免并发消息把这一条的终值改掉。
        after, mode_after = float(state.energy), str(state.mode)
        stamp = time.time() if ts is None else ts
        try:
            await self.prepare()
            await self.store.execute(
                "INSERT INTO night_energy_log "
                "(ts,event,energy_before,energy_after,delta,mode_before,mode_after,"
                "reason,session_id,msg_id,details_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (stamp, event, float(before), after,
                 after - float(before), mode_before or mode_after, mode_after,
                 reason, str(session_id)[:200], str(msg_id)[:100],
                 json.dumps(details, ensure_ascii=False)),
            )
            # 保留七天且最多两万条，清理仅作用于观察日志。
            now = time.time()
            if now - self._last_prune >= 3600:
                self._last_prune = now
                await self.store.execute("DELETE FROM night_energy_log WHERE ts < ?", (now - 7 * 86400,))
                await self.store.execute(
                    "DELETE FROM night_energy_log WHERE id <= "
                    "(SELECT id FROM night_energy_log ORDER BY id DESC LIMIT 1 OFFSET 20000)"
                )
        except Exception as exc:
            logger.debug(f"[congling] 精力流水写入失败: {type(exc).__name__}")

    async def list(self, *, limit: int = 100, before_id: int = 0,
                   since: float = 0, category: str = "all") -> dict[str, Any]:
        await self.prepare()
        filters, values = ["ts >= ?"], [float(since)]
        if before_id > 0:
            filters.append("id < ?")
            values.append(int(before_id))
        categories = {
            "drain": ("message_drain", "fatigue_drain"),
            "recover": ("awake_recover", "sleep_recover"),
            "state": ("snapshot", "closing", "closing_cancelled", "closing_skipped", "sleep", "sleep_skipped", "sleep_guard",
                      "wake", "wake_check", "waking_done"),
        }
        if category in categories:
            names = categories[category]
            filters.append("event IN (" + ",".join("?" for _ in names) + ")")
            values.extend(names)
        limit = max(1, min(200, int(limit)))
        rows = await self.store.fetch_all(
            "SELECT * FROM night_energy_log WHERE " + " AND ".join(filters) +
            " ORDER BY id DESC LIMIT ?", (*values, limit + 1),
        )
        more = len(rows) > limit
        rows = rows[:limit]
        for row in rows:
            row["label"] = LABELS.get(row["event"], row["event"])
            row["reason_label"] = REASONS.get(row["reason"], row["reason"])
            row["details"] = json.loads(row.pop("details_json") or "{}")
        first = await self.store.scalar("SELECT MIN(ts) FROM night_energy_log", default=0)
        return {"items": rows, "has_more": more,
                "next_before_id": rows[-1]["id"] if more and rows else 0,
                "retention_days": 7, "max_records": 20000, "observed_since": first}
