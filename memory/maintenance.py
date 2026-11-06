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
from ..core.exc import exc_brief

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
        await self.purge_image_cache()

    async def purge_image_cache(self, keep_days: int = 7) -> int:
        """清掉过期的图片理解缓存。

        这些结果存在 kv 表里（key 是 img:<sha1(url)>），每条都不大，但会一直攒：
        十一天就攒到三千多条，把 kv 撑成三千多行。留着最近几天的足够命中重复图片，
        更早的再留着也没人问。
        """
        try:
            keep = max(1, int(keep_days))
        except (TypeError, ValueError):
            keep = 7
        cutoff = int(time.time()) - keep * 86400
        try:
            n = await self.store.scalar(
                "SELECT COUNT(*) FROM kv WHERE k LIKE 'img:%' AND updated_at<?", (cutoff,), 0
            )
            if not n:
                return 0
            await self.store.execute(
                "DELETE FROM kv WHERE k LIKE 'img:%' AND updated_at<?", (cutoff,)
            )
            logger.info(f"[congling] 清理图片缓存 {int(n)} 条（早于 {keep} 天）")
            return int(n)
        except Exception as exc:
            logger.warning(f"[congling] 清理图片缓存失败: {exc_brief(exc)}")
            return 0

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
                "WHERE status='active' AND protect=0 "
                "AND ((last_hit_at=0 AND created_at<?) OR (last_hit_at>0 AND last_hit_at<?))",
                (now - forget_days * 86400, now - forget_days * 86400),
            )
            # 以前这里直接 return 0，绕过了末尾的日标记写入：
            # off 档下每小时的 housekeeping 都会把 decay_factor 再乘一遍，
            # 一天乘 24 次。走统一出口落标才算修好。
            await self._mark_done(mark_key)
            return 0

        rows = await self.store.fetch_all(
            "SELECT id, mem_level, created_at, last_hit_at, hit_count, decay, status, protect "
            "FROM mem_atom WHERE status='active'"
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
            # 只处理 active：归档等于冻住时间，要恢复就捞出来还是原来那个分。
            # 以前这里会把 archived 一路衰减到 forgotten，「归档可恢复」名不副实。
            status = curve.status_for(value)
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
        await self._mark_done(mark_key)
        return changed

    async def reflect(self) -> None:
        # 默认关：reflect 的产出目前没有任何消费者（面板预览还没上线），
        # 开着就是每小时给每个主体烧一次模型调用。等面板接上再默认打开。
        if not self.config.bool("memory.reflect_enabled", False):
            return

        now = int(time.time())
        lt = time.localtime(now)
        hour = max(0, min(23, self.config.int("memory.reflect_hour", 4)))
        # 「到点之后」而不是「整点相等」：_housekeeping 每小时醒一次，
        # 一旦那一跳因为重启/长耗时被错过，按相等判断当天就永远不反思了。
        if lt.tm_hour < hour:
            return
        day_key = time.strftime("%Y-%m-%d", lt)
        mark_key = f"maintenance:reflect:{self.config.persona_id}:{day_key}"
        try:
            if await self.store.kv_get(mark_key, "") == "done":
                return
        except Exception:
            pass

        # subject_ref 是写入时就建好的身份键：真实 QQ 号的都是 user:...，
        # 而「QQ用户」「望山的插件」、文件名这类脏主体一律为空。
        # 拿文件名去问「你对 TA 的新认识」只会产出噪声，还占一次调用。
        rows = await self.store.fetch_all(
            "SELECT subject_id, subject_ref, text, importance FROM mem_atom "
            "WHERE status='active' AND scope='user' AND importance>=0.55 "
            "AND (unread IS NULL OR unread=0) AND subject_ref LIKE 'user:%' "
            "ORDER BY importance DESC, id DESC LIMIT 200"
        )
        if not rows:
            await self._mark_done(mark_key)
            return
        provider = self._provider()
        if provider is None:
            return

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            ref = str(row.get("subject_ref") or "")
            if not ref.startswith("user:"):
                continue
            grouped.setdefault(ref, []).append(row)
        if not grouped:
            await self._mark_done(mark_key)
            return

        # 一轮最多反思 N 个主体，防止库涨大之后一次维护变成火灾
        limit = max(1, self.config.int("memory.reflect_subjects_max", 8))
        picked = sorted(grouped.items(), key=lambda kv: len(kv[1]), reverse=True)[:limit]
        done = 0
        for ref, items in picked:
            # 单主体门槛：一条记忆也要模型概括人格，只是噪声
            if len(items) < 4:
                continue
            subject = str(items[0].get("subject_id") or "")
            body = "\n".join(f"- {item['text']}" for item in items[:30])
            try:
                resp = await provider.text_chat(
                    prompt=REFLECT_PROMPT.format(atoms=body), contexts=[]
                )
                summary = str(getattr(resp, "completion_text", "") or "").strip()
            except Exception as exc:
                logger.warning(f"[congling] 反思失败: {exc_brief(exc)}")
                continue
            if not summary:
                continue
            await self.store.kv_set(
                f"reflect:{self.config.persona_id}:{ref}",
                {"text": summary, "ts": int(time.time()), "count": len(items),
                 "subject": subject},
            )
            await self.plugin.bus.emit(
                self.plugin.bus.ON_REFLECT, {"subject": subject, "ref": ref, "text": summary}
            )
            done += 1
        # 跑完才落标：中途抛异常不写，下次还能重试（和 decay 一个做法）
        await self._mark_done(mark_key)
        if done:
            logger.info(f"[congling] 反思完成：{done} 个主体（{day_key}）")

    async def _mark_done(self, mark_key: str) -> None:
        try:
            await self.store.kv_set(mark_key, "done")
        except Exception:
            pass

    # ================================================================ 备份
    async def backup(self) -> None:
        # 业务日幂等：跟 decay 一样的做法。以前这里没有任何去重，
        # 而 run_daily() 在启动时会被多个钩子各调一次，于是每次重启
        # 就连续写出两三份 39MB 的备份——目录里 7 份就是 271MB。
        now = int(time.time())
        day_key = time.strftime("%Y-%m-%d", time.localtime(now))
        mark_key = f"maintenance:backup:{self.config.persona_id}:{day_key}"
        try:
            if await self.store.kv_get(mark_key, "") == "done":
                logger.debug(f"[congling] 今天已经备份过记忆（{day_key}），跳过")
                return
        except Exception:
            pass
        try:
            rows = await self.store.fetch_all("SELECT * FROM mem_atom")
            episodes = await self.store.fetch_all("SELECT * FROM mem_episode ORDER BY id DESC LIMIT 500")
        except Exception as exc:
            logger.warning(f"[congling] 备份读取失败: {exc_brief(exc)}")
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
            logger.warning(f"[congling] 备份写入失败: {exc_brief(exc)}")
            return
        try:
            await self.store.kv_set(mark_key, "done")
        except Exception:
            pass
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
