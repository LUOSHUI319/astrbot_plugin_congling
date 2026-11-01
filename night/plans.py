"""梦排产只管调度；generated 正文不可见，发放时与账本、梦库原子提交。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from typing import Any

from astrbot.api import logger
from ..core.memory_guard import memory_work

from ..core.contracts import DreamEntry, night_key_of
from ..memory.ledger import EventLedger
from . import stages
from .dream import KV_KEY, MAX_KEEP
from ..core.exc import exc_brief


class DreamPlans:
    def __init__(self, night: Any) -> None:
        self.night = night
        self.plugin = night.plugin
        self.config = night.config
        self.store = night.store
        self.dreamer = night.dreamer
        self._lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def mode(self) -> str:
        value = self.config.str("night.dream_mode", "hybrid")
        return value if value in ("live", "hybrid", "precomputed") else "hybrid"

    def _spawn(self, key: str, factory) -> asyncio.Task:
        old = self._tasks.get(key)
        if old is not None and not old.done():
            return old
        task = self.plugin.spawn_background(factory())
        self._tasks[key] = task
        def done(finished):
            if self._tasks.get(key) is finished:
                self._tasks.pop(key, None)
        task.add_done_callback(done)
        return task

    async def cancel(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    @memory_work
    async def plan_sleep(self) -> None:
        state = self.night.state.state
        if (self.mode == "live" or not self.dreamer.enabled or state.dream_skip
                or state.mode != "asleep"):
            return
        sleep_at, wake_at = int(state.sleep_at), int(state.wake_at)
        night_key = night_key_of(sleep_at)
        persona = self.config.persona_id
        key = hashlib.sha256(f"{persona}|{night_key}|{sleep_at}|{wake_at}".encode()).hexdigest()
        segments = stages.build_plan(self.config, sleep_at=sleep_at, wake_at=wake_at,
                                     kind=state.sleep_kind or "night")
        start_after = max(1, self.config.int("night.dream_start_after_minutes", 60)) * 60
        interval = max(5, self.config.int("night.dream_interval_minutes", 60)) * 60
        maximum = max(1, self.config.int("night.dream_max_per_sleep", 8))
        selected = []
        for seg in segments:
            if seg["key"] != "rem" or seg["start"] < sleep_at + start_after:
                continue
            if selected and seg["start"] - selected[-1]["start"] < interval:
                continue
            selected.append(seg)
            if len(selected) >= maximum:
                break
        now = int(time.time())
        def write(conn):
            for index, seg in enumerate(selected, 1):
                scheduled = seg["start"] + random.randint(0, min(120, (seg["end"] - seg["start"]) // 3))
                conn.execute(
                    "INSERT OR IGNORE INTO night_dream_plan(persona_id,night_key,plan_key,stage_index,"
                    "stage_key,stage_start,stage_end,scheduled_at,context_cutoff,sleep_at,wake_at,"
                    "planned_at,cycle,precompute) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (persona, night_key, key, index, "rem", seg["start"], seg["end"], scheduled,
                     sleep_at, sleep_at, wake_at, now, seg["cycle"] + 1,
                     int(self.mode == "precomputed" or index <= 3)))
        # 所有 planned 先一次提交，才可启动任何模型请求。
        await self.store.transaction(write)
        if selected:
            self._spawn(key, lambda: self.precompute(key))

    @memory_work
    async def precompute(self, key: str) -> None:
        async with self._lock:
            rows = await self.store.fetch_all(
                "SELECT id FROM night_dream_plan WHERE persona_id=? AND plan_key=? "
                "AND precompute=1 AND status='planned' AND attempts=0 ORDER BY stage_index",
                (self.config.persona_id, key))
            for row in rows:
                await self._generate(row["id"])

    async def _row(self, row_id: int):
        return await self.store.fetch_one("SELECT * FROM night_dream_plan WHERE persona_id=? AND id=?",
                                         (self.config.persona_id, row_id))

    async def _expire(self, row_id: int, reason: str) -> None:
        await self.store.execute(
            "UPDATE night_dream_plan SET status='expired',last_error=? WHERE persona_id=? AND id=? "
            "AND status IN ('planned','generated')", (reason, self.config.persona_id, row_id))

    async def _generate(self, row_id: int, now: int | None = None) -> bool:
        row = await self._row(row_id)
        if not row or row["status"] != "planned" or not self.dreamer.enabled:
            return False
        at = int(time.time()) if now is None else now
        if at - row["wake_at"] > 86400:
            await self._expire(row_id, "backfill_window_exceeded")
            return False
        if row["attempts"] >= 2:
            await self.store.execute("UPDATE night_dream_plan SET status='failed' WHERE id=?", (row_id,))
            return False
        attempt = row["attempts"] + 1
        await self.store.execute("UPDATE night_dream_plan SET attempts=? WHERE id=?", (attempt, row_id))
        # 前三段用入睡前素材；在线后续段用实际生成时的素材。
        cutoff = row["sleep_at"] if row["precompute"] else at
        try:
            entry = await self.dreamer.make(persist=False, context_cutoff=cutoff)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            entry = None
            logger.warning(f"[congling] 梦排产生成失败({row_id}): {exc_brief(exc)}")
        if entry is None:
            await self.store.execute(
                "UPDATE night_dream_plan SET status=?,last_error='generation_failed' "
                "WHERE id=? AND status='planned'", ("failed" if attempt >= 2 else "planned", row_id))
            return False
        entry.dream_id = f"dream_{row['plan_key']}_{row['stage_index']}"
        entry.created_at = row["stage_start"]
        entry.decay_at = row["stage_start"]
        entry.night_key = row["night_key"]
        entry.planned_at = row["planned_at"]
        entry.stage_key = row["stage_key"]
        entry.cycle = row["cycle"]
        await self.store.execute(
            "UPDATE night_dream_plan SET status='generated',dream_id=?,dream_json=?,"
            "generated_at=?,context_cutoff=?,last_error='' WHERE id=? AND status='planned'",
            (entry.dream_id, json.dumps(entry.to_dict(), ensure_ascii=False),
             int(time.time()) if now is None else now, cutoff, row_id))
        return True

    @memory_work
    async def generate_live(self, now: int | None = None) -> None:
        """后半夜实时段/失败重试只在本次睡眠中生成；下一轮才交付。"""
        state = self.night.state.state
        if self.mode == "live" or state.mode != "asleep" or state.dream_skip or not self.dreamer.enabled:
            return
        at = int(time.time()) if now is None else now
        async with self._lock:
            rows = await self.store.fetch_all(
                "SELECT * FROM night_dream_plan WHERE persona_id=? AND sleep_at=? "
                "AND status='planned' AND scheduled_at<=? ORDER BY stage_index",
                (self.config.persona_id, state.sleep_at, at))
            for row in rows:
                if at >= row["wake_at"]:
                    continue
                if not row["precompute"] and at >= row["stage_end"]:
                    await self._expire(row["id"], "missed_live_stage")
                    continue
                await self._generate(row["id"], now)
                break

    async def note_wake(self, sleep_at: int, woke_at: int) -> None:
        """提前叫醒使未来阶段失效；独立保存实际醒来时刻供重启恢复。"""
        await self.store.execute(
            "UPDATE night_dream_plan SET wake_at=MIN(wake_at,?) WHERE persona_id=? AND sleep_at=? "
            "AND status IN ('planned','generated')", (woke_at, self.config.persona_id, sleep_at))
        await self.store.execute(
            "UPDATE night_dream_plan SET status='expired',last_error='woke_before_stage' "
            "WHERE persona_id=? AND sleep_at=? AND stage_start>=? AND status IN ('planned','generated')",
            (self.config.persona_id, sleep_at, woke_at))

    @memory_work
    async def deliver_due(self, now: int | None = None) -> int:
        if not self.dreamer.enabled:
            return 0
        at = int(time.time()) if now is None else now
        rows = await self.store.fetch_all(
            "SELECT * FROM night_dream_plan WHERE persona_id=? AND status='generated' "
            "AND scheduled_at<=? ORDER BY scheduled_at,id", (self.config.persona_id, at))
        for row in rows:
            if at - row["wake_at"] > 86400:
                await self._expire(row["id"], "backfill_window_exceeded")
                continue
            if await self._deliver(row["id"], now=at, backfilled=at >= row["wake_at"]):
                return 1
        return 0

    @memory_work
    async def reconcile(self, now: int | None = None) -> dict[str, int]:
        """启动三档：未醒保持；醒后 24h 内补发；超期过期。绝不逐条播报。"""
        at = int(time.time()) if now is None else now
        counts = {"kept": 0, "backfilled": 0, "expired": 0}
        async with self._lock:
            rows = await self.store.fetch_all(
                "SELECT * FROM night_dream_plan WHERE persona_id=? AND status IN ('planned','generated') "
                "AND scheduled_at<=? ORDER BY scheduled_at,id", (self.config.persona_id, at))
            for row in rows:
                if at < row["wake_at"]:
                    counts["kept"] += 1
                    continue
                if at - row["wake_at"] > 86400:
                    await self._expire(row["id"], "backfill_window_exceeded")
                    counts["expired"] += 1
                    continue
                if not self.dreamer.enabled:
                    continue
                if row["status"] == "planned":
                    if not row["precompute"]:
                        await self._expire(row["id"], "missed_live_stage")
                        counts["expired"] += 1
                        continue
                    if not await self._generate(row["id"], now):
                        continue
                if await self._deliver(row["id"], now=int(time.time()) if now is None else at, backfilled=True):
                    counts["backfilled"] += 1
        return counts

    async def _deliver(self, row_id: int, *, now: int, backfilled: bool) -> bool:
        """所有可见产物同事务。重复启动、取消等待、失败重试都使用同一事件键。"""
        async with self.dreamer.write_lock:
            row = await self._row(row_id)
            if not row or row["status"] != "generated":
                return False
            if now < row["scheduled_at"]:
                return False
            if now - row["wake_at"] > 86400:
                await self._expire(row_id, "backfill_window_exceeded")
                return False
            entry = DreamEntry.from_dict(json.loads(row["dream_json"]))
            entry.created_at = row["stage_start"]
            entry.decay_at = row["stage_start"]
            entry.backfilled = backfilled
            entry.age_to(now)
            record = EventLedger(self.plugin).prepare_record(
                "dream", entry.dream_id,
                f"我在{time.strftime('%Y-%m-%d %H:%M', time.localtime(entry.created_at))}做了一段梦；"
                "这是梦境，不是现实经历。正文保存在私人梦境记录里。",
                private_text=f"我做过的梦（梦境不是现实；私人正文）：{entry.text}",
                occurred_at=entry.created_at,
                result={"dream_id": entry.dream_id, "level": entry.level, "plan_key": row["plan_key"],
                        "stage_index": row["stage_index"], "backfilled": backfilled})
            def write(conn):
                fresh = conn.execute("SELECT status FROM night_dream_plan WHERE id=?", (row_id,)).fetchone()
                if not fresh or fresh["status"] != "generated":
                    return False
                record(conn)
                raw = conn.execute("SELECT v FROM kv WHERE k=?", (KV_KEY,)).fetchone()
                dreams = json.loads(raw["v"]) if raw else []
                if not isinstance(dreams, list):
                    raise ValueError("梦库格式错误，拒绝覆盖")
                if not any(d.get("dream_id") == entry.dream_id for d in dreams):
                    dreams.append(entry.to_dict())
                conn.execute(
                    "INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v,updated_at=excluded.updated_at",
                    (KV_KEY, json.dumps(dreams[-MAX_KEEP:], ensure_ascii=False), now))
                conn.execute(
                    "UPDATE night_dream_plan SET status='delivered',delivered_at=?,backfilled=? WHERE id=?",
                    (row["wake_at"] if backfilled else now, int(backfilled), row_id))
                raw_state = conn.execute("SELECT v FROM kv WHERE k='night.state'").fetchone()
                if raw_state:
                    data = json.loads(raw_state["v"])
                    sleep = data.get("sleep_at", 0)
                    windows = conn.execute("SELECT v FROM kv WHERE k='night.last_sleep_window'").fetchone()
                    windows = json.loads(windows["v"]) if windows else []
                    last_sleep = windows[-1].get("sleep_at", 0) if windows else 0
                    if sleep == row["sleep_at"] or (
                            not sleep and (self.night.state._wake_sleep_at or last_sleep) == row["sleep_at"]):
                        data["dream_count_today"] = int(data.get("dream_count_today", 0)) + 1
                        conn.execute("UPDATE kv SET v=?,updated_at=? WHERE k='night.state'",
                                     (json.dumps(data, ensure_ascii=False), now))
                return True
            self.dreamer._loaded = False
            try:
                return await self.store.transaction(write)
            finally:
                # 取消 await 不会停止 SQLite 工作线程。读操作会等事务锁释放，
                # 必须在退出前恢复内存，防 terminate 的 save 把已提交梦覆盖掉。
                await self.dreamer.load()
                raw_state = await self.store.kv_get("night.state", {})
                self.night.state.state.dream_count_today = int(raw_state.get("dream_count_today", 0))
