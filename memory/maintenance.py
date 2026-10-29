"""记忆维护：衰减、归档、反思、备份。

这一层决定记忆系统的「性格」：
- 该忘的真的会忘（decay 掉下去就归档，再掉就进回收站）
- 被反复用到的会越来越牢（hit_count 加成）
- 重要的事永不衰减（protect）
- 每天反思一次，产出的是「我对这个人的新认识」，不是冷冰冰的条目
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from astrbot.api import logger

from ..core import paths
from .forgetting import ForgettingCurve

REFLECT_PROMPT = """你在整理对一个用户的认识。下面是关于 TA 的记忆条目（已经过时间衰减筛选）。

请用 3~5 句话写出「我现在对 TA 的理解」，要求：
1. 只写有依据的，不要推测性格标签
2. 优先写：TA 最近在做什么、在意什么、讨厌什么、有什么约定还没兑现
3. 用第一人称，像自己心里的判断，不要分条列点

记忆条目：
{atoms}
"""


class Maintenance:
    def __init__(self, plugin: Any, atoms: Any, retriever: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self.atoms = atoms
        self.retriever = retriever

    # ================================================================ 每日
    async def run_daily(self) -> None:
        await self.decay()
        await self.reflect()
        await self.backup()

    async def decay(self) -> int:
        """按等级衰减。返回本次实际改动的条数（给日志和 UI 用）。

        0.5.0 起不再全表乘一个因子：每条按自己的等级、距上次被想起的天数、
        被想起过几次来算保留率。off 档保留 0.4.x 的老逻辑，方便回退。
        """
        curve = ForgettingCurve(self.plugin)
        now = int(time.time())

        # 业务日幂等：同一天被触发两次（重启、多实例、定时器抖动）会把衰减系数乘两次。
        # 用「人设 + 业务日 + phase」做标记，成功跑完才写，失败不落标。
        day_key = time.strftime("%Y-%m-%d", time.localtime(now))
        mark_key = f"maintenance:decay:{self.config.persona_id}:{day_key}"
        try:
            if await self.store.kv_get(mark_key, "") == "done":
                logger.debug(f"[congling] 今天已经衰减过（{day_key}），跳过")
                return 0
        except Exception:
            pass

        archive_days = self.config.int("memory.archive_after_days", 30)
        forget_days = self.config.int("memory.forget_after_days", 90)

        if curve.mode == "off":
            factor = self.config.float("memory.decay_factor", 0.985)
            await self.store.execute(
                "UPDATE mem_atom SET decay=MIN(1.0, decay*?) WHERE status='active' AND protect=0",
                (factor,),
            )
            await self.store.execute(
                "UPDATE mem_atom SET status='archived' "
                "WHERE status='active' AND protect=0 AND decay<0.45 "
                "AND last_hit_at>0 AND last_hit_at<?",
                (now - archive_days * 86400,),
            )
            await self.store.execute(
                "UPDATE mem_atom SET status='forgotten' "
                "WHERE status IN ('active','archived') AND protect=0 "
                "AND ((last_hit_at=0 AND created_at<?) OR (last_hit_at>0 AND last_hit_at<?))",
                (now - forget_days * 86400, now - forget_days * 86400),
            )
            return 0

        rows = await self.store.fetch_all(
            "SELECT id, mem_level, created_at, last_hit_at, hit_count, decay, status, protect "
            "FROM mem_atom WHERE status IN ('active','archived')"
        )
        updates: list[tuple[Any, ...]] = []
        changed = 0
        for row in rows:
            if int(row.get("protect") or 0):
                continue
            _lv = row.get("mem_level")
            level = 3 if _lv is None or _lv == "" else int(_lv)
            if level <= 0:
                continue
            last = max(int(row.get("last_hit_at") or 0), int(row.get("created_at") or 0))
            days = (now - last) / 86400 if last else 0.0
            hits = int(row.get("hit_count") or 0)
            value = curve.decay_at(level, days, hits)
            status = str(row.get("status") or "active")
            if status == "active":
                status = curve.status_for(value)
            elif status == "archived" and value < curve.forget_ratio:
                status = "forgotten"
            old_decay = float(row.get("decay") or 1.0)
            review = now + curve.next_review_days(level, hits) * 86400 if level else 0
            if abs(old_decay - value) < 0.002 and status == str(row.get("status") or "active"):
                continue
            updates.append((value, status, review, int(row["id"])))
            changed += 1

        if updates:
            await self.store.executemany(
                "UPDATE mem_atom SET decay=?, status=?, next_review_at=? WHERE id=?", updates
            )
        # 跑完才落标：中途抛异常不会写，下次还会重试
        try:
            await self.store.kv_set(mark_key, "done")
        except Exception:
            pass
        return changed

    async def reflect(self) -> None:
        rows = await self.store.fetch_all(
            "SELECT subject_id, scope, text, importance FROM mem_atom "
            "WHERE status='active' AND scope='user' AND importance>=0.55 "
            "ORDER BY importance DESC, id DESC LIMIT 60"
        )
        if len(rows) < 4:
            return
        provider = self._provider()
        if provider is None:
            return
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("subject_id") or ""), []).append(row)
        for subject, items in grouped.items():
            if not subject:
                continue
            body = "\n".join(f"- {item['text']}" for item in items[:30])
            try:
                resp = await provider.text_chat(
                    prompt=REFLECT_PROMPT.format(atoms=body), contexts=[]
                )
                summary = str(getattr(resp, "completion_text", "") or "").strip()
            except Exception as exc:
                logger.debug(f"[congling] 反思失败: {exc!r}")
                continue
            if not summary:
                continue
            await self.store.kv_set(
                f"reflect:{self.config.persona_id}:{subject}",
                {"text": summary, "ts": int(time.time()), "count": len(items)},
            )
            await self.plugin.bus.emit(self.plugin.bus.ON_REFLECT, {"subject": subject, "text": summary})

    # ================================================================ 备份
    async def backup(self) -> None:
        try:
            rows = await self.store.fetch_all("SELECT * FROM mem_atom")
            episodes = await self.store.fetch_all("SELECT * FROM mem_episode ORDER BY id DESC LIMIT 500")
        except Exception as exc:
            logger.debug(f"[congling] 备份读取失败: {exc!r}")
            return
        payload = {
            "version": 1,
            "exported_at": int(time.time()),
            "persona_id": self.config.persona_id,
            "atoms": rows,
            "episodes": episodes,
        }
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = paths.backup_dir() / f"memory-{stamp}.json"
        try:
            target.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        except Exception as exc:
            logger.debug(f"[congling] 备份写入失败: {exc!r}")
            return
        # 只保留最近 7 份
        files = sorted(paths.backup_dir().glob("memory-*.json"))
        for old in files[:-7]:
            try:
                old.unlink()
            except Exception:
                pass

    # ================================================================ 统计
    async def report(self) -> dict[str, Any]:
        stats = await self.store.stats()
        try:
            avg_decay = await self.store.scalar("SELECT AVG(decay) FROM mem_atom WHERE status='active'", (), 0)
        except Exception:
            avg_decay = 0
        return {
            "counts": stats,
            "avg_decay": round(float(avg_decay or 0), 3),
            "recent": await self.store.fetch_all(
                "SELECT id, text, kind, importance, created_at FROM mem_atom "
                "WHERE status='active' ORDER BY id DESC LIMIT 10"
            ),
        }

    # ================================================================ 工具
    def _provider(self) -> Any:
        provider_id = self.config.str("memory.llm_provider_id", "")
        try:
            if provider_id:
                provider = self.plugin.context.get_provider_by_id(provider_id)
                if provider is not None:
                    return provider
            return self.plugin.context.get_using_provider()
        except Exception:
            return None


def _safe_exp(value: float) -> float:
    try:
        return math.exp(value)
    except OverflowError:
        return 0.0
