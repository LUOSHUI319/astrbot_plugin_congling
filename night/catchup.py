"""醒来后的后台补读。

睡着期间的消息照样抽成记忆，但会被打上 unread=1，检索层（retrieve 各条路径）
一律带 `unread IS NULL OR unread=0`，所以那些记忆在库里、面板看得见，却想不起来。

爬楼时代靠「用户先说话」触发一次性解锁。现在改成醒来后后台自己走：
按进度目标在 CATCHUP_MINUTES 分钟内把未读逐批转正，不需要用户开口，也不发任何话。

为什么用「进度目标」而不是「每分钟固定条数」：
  - 取整会卡住（3 条的 10% 是 0.3，按 0 算就永远不动）
  - tick 不是精确 60 秒一次，累加会漂
  - 停机那几分钟没人补，10 分钟会变成半小时
改成 target = ceil(total * elapsed/窗口) 之后，10 分钟必达、重启自动追平、粒度还更细。
"""

from __future__ import annotations

import math
import time
from typing import Any

from astrbot.api import logger

KV_KEY = "night.catchup"

# 补读窗口：醒来后多久走完。要改时长改这里，不做配置项。
CATCHUP_MINUTES = 10

# 兜底：超过这么久的未读直接放行，避免 kv 丢了 / 代码 bug 让记忆永久不可召回。
STALE_SECONDS = 24 * 3600


class Catchup:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store

    # ------------------------------------------------------------ 查询
    async def unread_total(self) -> int:
        try:
            return int(await self.store.scalar(
                "SELECT COUNT(*) FROM mem_atom WHERE status='active' AND unread=1"
            ) or 0)
        except Exception:
            return 0

    # ------------------------------------------------------------ 窗口
    async def open_window(self, now: int | None = None) -> dict | None:
        """醒来时建账。没有未读就什么都不做。"""
        stamp = int(now or time.time())
        total = await self.unread_total()
        if total <= 0:
            await self._clear()
            return None
        state = {"start": stamp, "total": total, "done": 0, "cutoff": stamp}
        await self._save(state)
        logger.info(f"[congling] 补读开始：{total} 条未读，{CATCHUP_MINUTES} 分钟走完")
        return state

    async def step(self, now: float | None = None) -> int:
        """每次 tick 调一次。返回这一轮转正的条数。"""
        if not self.config.bool("night.catchup_enabled", True):
            return 0
        stamp = float(now or time.time())
        await self._release_stale(int(stamp))

        state = await self._load()
        if not state:
            return 0
        try:
            start = int(state["start"])
            total = int(state["total"])
            done = int(state.get("done") or 0)
            cutoff = int(state.get("cutoff") or start)
        except (KeyError, TypeError, ValueError):
            await self._clear()
            return 0
        if total <= 0 or start <= 0:
            await self._clear()
            return 0

        window = CATCHUP_MINUTES * 60
        elapsed = max(0.0, stamp - start)
        ratio = min(1.0, elapsed / window) if window > 0 else 1.0
        target = int(math.ceil(total * ratio))
        need = max(0, target - done)

        released = 0
        if need > 0:
            released = await self._release_batch(cutoff, need)
            state["done"] = done + released
            await self._save(state)

        finished = (
            int(state["done"]) >= total
            or (need > 0 and released == 0)      # 已经没有可放行的了
            or elapsed >= window                  # 到点收尾
        )
        if not finished:
            return released

        # 收尾：把本轮界内的剩余未读一次清掉，别留半拉子
        left = await self.flush_all(only_before=cutoff)
        state["done"] = int(state["done"]) + left
        await self._clear()
        logger.info(f"[congling] 补读结束：本轮放行 {int(state['done'])} 条")
        return released + left

    async def flush_all(self, *, only_before: int = 0) -> int:
        """把未读全部转正。only_before>0 时只处理 unread_since 早于它的。"""
        try:
            if only_before > 0:
                count = int(await self.store.scalar(
                    "SELECT COUNT(*) FROM mem_atom WHERE status='active' AND unread=1 "
                    "AND (unread_since IS NULL OR unread_since<=?)", (only_before,)) or 0)
                if count:
                    await self.store.execute(
                        "UPDATE mem_atom SET unread=0 WHERE status='active' AND unread=1 "
                        "AND (unread_since IS NULL OR unread_since<=?)", (only_before,))
                return count
            count = await self.unread_total()
            if count:
                await self.store.execute(
                    "UPDATE mem_atom SET unread=0 WHERE status='active' AND unread=1")
            return count
        except Exception as exc:
            logger.warning(f"[congling] 补读清空失败: {exc}")
            return 0

    async def close_window(self) -> None:
        """睡回去时调用：本轮补读作废，醒来重新开。"""
        await self._clear()

    # ------------------------------------------------------------ 内部
    async def _release_batch(self, cutoff: int, limit: int) -> int:
        """放行最早的一批未读。"""
        size = max(1, int(limit))
        try:
            rows = await self.store.fetch_all(
                "SELECT id FROM mem_atom WHERE status='active' AND unread=1 "
                "AND (unread_since IS NULL OR unread_since<=?) "
                "ORDER BY unread_since, id LIMIT ?", (cutoff, size))
        except Exception as exc:
            logger.warning(f"[congling] 补读取批失败: {exc}")
            return 0
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        try:
            await self.store.execute(
                f"UPDATE mem_atom SET unread=0 WHERE id IN ({marks})", tuple(ids))
        except Exception as exc:
            logger.warning(f"[congling] 补读写回失败: {exc}")
            return 0
        return len(ids)

    async def _release_stale(self, now: int) -> int:
        """超过 STALE_SECONDS 的未读直接放行，防止记忆永久卡死。"""
        cutoff = now - STALE_SECONDS
        try:
            count = int(await self.store.scalar(
                "SELECT COUNT(*) FROM mem_atom WHERE status='active' AND unread=1 "
                "AND unread_since>0 AND unread_since<?", (cutoff,)) or 0)
            if not count:
                return 0
            await self.store.execute(
                "UPDATE mem_atom SET unread=0 WHERE status='active' AND unread=1 "
                "AND unread_since>0 AND unread_since<?", (cutoff,))
            logger.info(f"[congling] 补读兜底：{count} 条超过 24 小时的未读直接放行")
            return count
        except Exception as exc:
            logger.warning(f"[congling] 补读兜底失败: {exc}")
            return 0

    async def _load(self) -> dict | None:
        try:
            raw = await self.store.kv_get(KV_KEY, None)
        except Exception:
            return None
        return raw if isinstance(raw, dict) else None

    async def _save(self, state: dict) -> None:
        try:
            await self.store.kv_set(KV_KEY, state)
        except Exception:
            pass

    async def _clear(self) -> None:
        try:
            await self.store.kv_set(KV_KEY, {})
        except Exception:
            pass

    # ------------------------------------------------------------ 面板
    async def progress(self, now: float | None = None) -> dict[str, Any]:
        """给面板用的补读进度。没有进行中的补读时 total=0。"""
        stamp = float(now or time.time())
        state = await self._load() or {}
        try:
            start = int(state.get("start") or 0)
            total = int(state.get("total") or 0)
            done = int(state.get("done") or 0)
        except (TypeError, ValueError):
            start, total, done = 0, 0, 0
        left = 0
        if start > 0:
            left = max(0, int(start + CATCHUP_MINUTES * 60 - stamp))
        return {
            "active": bool(start > 0 and total > 0),
            "total": total,
            "done": done,
            "percent": int(round(done * 100 / total)) if total else 0,
            "left_seconds": left,
        }
