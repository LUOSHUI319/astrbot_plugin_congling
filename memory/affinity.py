"""好感度。

借的是「多维累积 + 阶段门槛」这个骨架，数值全是自己算的：

    delta = 记忆贡献 + 说话加成 - 冷淡扣分

三条自我约束：
1. 一天最多动 max_daily_delta，刷不上去；
2. 掉分不会掉穿阶段（每档有 floor），不会出现「昨天还很亲，今天就陌生」；
3. 手动改过的可以冻结自动结算，关系是两个人的事，不该被脚本拽着走。
"""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

LEVEL_WEIGHT = {0: 2.0, 1: 0.2, 2: 0.5, 3: 1.0, 4: 2.0, 5: 3.0}
DEFAULT_NAMES = ["陌生", "眼熟", "熟人", "朋友", "亲近", "交心"]
DEFAULT_THRESHOLDS = [0, 100, 250, 450, 650, 850]
MAX_SCORE = 1000.0


class Affinity:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store

    # ------------------------------------------------------------ 配置
    def enabled(self) -> bool:
        try:
            return bool(self.config.bool("memory.affinity.enabled", True))
        except Exception:
            return True

    def editable(self) -> bool:
        try:
            return bool(self.config.bool("memory.affinity.ui_editable", True))
        except Exception:
            return True

    def max_delta(self) -> float:
        try:
            return max(1.0, min(80.0, self.config.float("memory.affinity.max_daily_delta", 15.0)))
        except Exception:
            return 15.0

    def neglect_days(self) -> int:
        try:
            return max(1, self.config.int("memory.affinity.neglect_days", 3))
        except Exception:
            return 3

    def names(self) -> list[str]:
        raw = self.config.list("memory.affinity.stage_names", DEFAULT_NAMES)
        names = [str(x).strip() for x in raw if str(x).strip()]
        while len(names) < 6:
            names.append(DEFAULT_NAMES[len(names)])
        return names[:6]

    def thresholds(self) -> list[float]:
        raw = self.config.list("memory.affinity.stage_thresholds", DEFAULT_THRESHOLDS)
        values: list[float] = []
        for idx, item in enumerate(raw[:6]):
            try:
                values.append(float(item))
            except (TypeError, ValueError):
                values.append(float(DEFAULT_THRESHOLDS[idx]))
        while len(values) < 6:
            values.append(float(DEFAULT_THRESHOLDS[len(values)]))
        # 必须单调递增，否则 stage_of 会算错
        for i in range(1, len(values)):
            if values[i] < values[i - 1]:
                values[i] = values[i - 1]
        return values

    def stage_of(self, score: float) -> int:
        th = self.thresholds()
        stage = 0
        for idx, value in enumerate(th):
            if score >= value:
                stage = idx
        return max(0, min(5, stage))

    def stage_floor(self, stage: int) -> float:
        th = self.thresholds()
        return float(th[max(0, min(5, stage))])

    def stage_name(self, stage: int) -> str:
        return self.names()[max(0, min(5, stage))]

    # ------------------------------------------------------------ 读写
    async def get(self, subject_key: str) -> dict[str, Any]:
        key = str(subject_key or "").strip()
        if not key:
            return {"subject_key": "", "score": 0.0, "stage": 0, "stage_name": self.stage_name(0)}
        row = await self.store.fetch_one(
            "SELECT * FROM affinity WHERE persona_id=? AND subject_key=?",
            (self.config.persona_id, key),
        )
        if row is None:
            now = int(time.time())
            await self.store.execute(
                "INSERT INTO affinity(persona_id, subject_key, score, stage, floor, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(persona_id, subject_key) DO NOTHING",
                (self.config.persona_id, key, 0.0, 0, 0.0, now),
            )
            row = await self.store.fetch_one(
                "SELECT * FROM affinity WHERE persona_id=? AND subject_key=?",
                (self.config.persona_id, key),
            ) or {}
        score = float(row.get("score") or 0.0)
        stage = self.stage_of(score)
        return {
            "subject_key": key,
            "score": round(score, 1),
            "stage": stage,
            "stage_name": self.stage_name(stage),
            "floor": float(row.get("floor") or 0.0),
            "delta_today": float(row.get("delta_today") or 0.0),
            "last_talk_at": int(row.get("last_talk_at") or 0),
            "manual": int(row.get("manual") or 0),
            "updated_at": int(row.get("updated_at") or 0),
            "evidence": self._loads(row.get("evidence_atom_ids"), []),
        }

    async def stage(self, subject_key: str) -> int:
        try:
            return int((await self.get(subject_key)).get("stage") or 0)
        except Exception:
            return 0

    async def add(
        self,
        subject_key: str,
        delta: float,
        *,
        evidence: list[int] | None = None,
        manual: bool = False,
    ) -> dict[str, Any]:
        """加减分。自动结算会被单日上限卡住，手动改不受限。"""
        data = await self.get(subject_key)
        key = data["subject_key"]
        if not key:
            return data
        if not manual:
            if not self.enabled():
                return data
            limit = self.max_delta()
            today = float(data.get("delta_today") or 0.0)
            room = limit - abs(today)
            if room <= 0:
                return data
            delta = max(-room, min(room, delta))
            if data.get("manual") and self._frozen():
                return data
        score = max(0.0, min(MAX_SCORE, float(data["score"]) + delta))
        stage = self.stage_of(score)
        floor = max(float(data.get("floor") or 0.0), self.stage_floor(stage))
        # 掉分不掉穿阶段：分数被档位下限托住
        if score < floor:
            score = floor
        ev = [int(x) for x in (evidence or [])][:12]
        try:
            await self.store.execute(
                "UPDATE affinity SET score=?, stage=?, delta_today=delta_today+?, floor=?, "
                "evidence_atom_ids=?, updated_at=?, manual=? WHERE persona_id=? AND subject_key=?",
                (
                    score,
                    stage,
                    delta,
                    floor,
                    json.dumps(ev),
                    int(time.time()),
                    1 if manual else int(data.get("manual") or 0),
                    self.config.persona_id,
                    key,
                ),
            )
        except Exception as exc:
            logger.debug(f"[congling] 好感度写入失败: {exc!r}")
        return await self.get(key)

    def _frozen(self) -> bool:
        try:
            return bool(self.config.bool("memory.affinity.manual_override", False))
        except Exception:
            return False

    async def set_manual(
        self,
        subject_key: str,
        *,
        score: float | None = None,
        stage: int | None = None,
    ) -> dict[str, Any]:
        if not self.editable():
            raise PermissionError("面板手动调整已关闭")
        data = await self.get(subject_key)
        key = data["subject_key"]
        target = float(data["score"])
        if score is not None:
            target = max(0.0, min(MAX_SCORE, float(score)))
        elif stage is not None:
            th = self.thresholds()
            idx = max(0, min(5, int(stage)))
            target = float(th[idx])
        new_stage = self.stage_of(target)
        await self.store.execute(
            "UPDATE affinity SET score=?, stage=?, floor=?, manual=1, updated_at=? "
            "WHERE persona_id=? AND subject_key=?",
            (target, new_stage, self.stage_floor(new_stage), int(time.time()),
             self.config.persona_id, key),
        )
        return await self.get(key)

    async def reset_today(self) -> None:
        await self.store.execute("UPDATE affinity SET delta_today=0")

    # ------------------------------------------------------------ 每日结算
    async def settle_daily(self, force: bool = False) -> dict[str, Any]:
        if not self.enabled():
            return {"enabled": False}
        today = time.strftime("%Y-%m-%d")
        if not force:
            try:
                if await self.store.kv_get("affinity:last_settle_day", "") == today:
                    return {"enabled": True, "skipped": "今天已经结算过"}
            except Exception:
                pass
        now = int(time.time())
        day_start = now - 86400
        changed = 0
        try:
            await self.reset_today()
        except Exception:
            pass

        rows = await self.store.fetch_all(
            "SELECT subject_id FROM mem_atom WHERE persona_id=? AND status='active' "
            "AND scope='user' AND subject_id<>'' AND (created_at>=? OR last_hit_at>=?) "
            "GROUP BY subject_id",
            (self.config.persona_id, day_start, day_start),
        )
        subjects = {str(r.get("subject_id")) for r in rows if r.get("subject_id")}
        try:
            known = await self.store.fetch_all(
                "SELECT subject_key FROM affinity WHERE persona_id=?", (self.config.persona_id,)
            )
            subjects |= {str(r.get("subject_key")) for r in known if r.get("subject_key")}
        except Exception:
            pass

        for key in subjects:
            try:
                await self._settle_one(key, now, day_start)
                changed += 1
            except Exception as exc:
                logger.debug(f"[congling] 好感度结算失败 {key}: {exc!r}")
        try:
            await self.store.kv_set("affinity:last_settle_day", today)
        except Exception:
            pass
        return {"enabled": True, "subjects": changed}

    async def _settle_one(self, key: str, now: int, day_start: int) -> None:
        data = await self.get(key)
        if data.get("manual") and self._frozen():
            return

        levels = await self.store.fetch_all(
            "SELECT mem_level, COUNT(*) AS cnt FROM mem_atom "
            "WHERE persona_id=? AND subject_id=? AND status='active' AND (created_at>=? OR last_hit_at>=?) "
            "GROUP BY mem_level",
            (self.config.persona_id, key, day_start, day_start),
        )
        gained = 0.0
        evidence: list[int] = []
        for row in levels:
            lv = int(row.get("mem_level") or 3)
            cnt = int(row.get("cnt") or 0)
            gained += LEVEL_WEIGHT.get(lv, 1.0) * cnt
        gained = min(gained, 12.0)

        # 说话加成：按归档表里这个人今天说了多少条
        talk = 0
        try:
            talk = int(
                await self.store.scalar(
                    "SELECT COUNT(*) FROM msg_archive WHERE sender_id=? AND ts>=?",
                    (key, day_start),
                    0,
                )
                or 0
            )
        except Exception:
            talk = 0
        bonus = min(6.0, talk * 0.4)

        # 冷淡扣分：从上次说话算起，超过 neglect_days 才开始扣
        last_talk = int(data.get("last_talk_at") or 0)
        if talk > 0:
            last_talk = now
        penalty = 0.0
        if last_talk:
            idle_days = (now - last_talk) / 86400
            threshold = self.neglect_days()
            if idle_days > threshold:
                penalty = 3.0 + 0.5 * (idle_days - threshold)
                penalty = min(12.0, penalty)

        delta = gained + bonus - penalty
        if abs(delta) < 0.2:
            delta = 0.0
        if delta:
            await self.add(key, delta, evidence=evidence)
        if talk > 0:
            await self.store.execute(
                "UPDATE affinity SET last_talk_at=?, talk_days_30=talk_days_30+0 WHERE persona_id=? AND subject_key=?",
                (now, self.config.persona_id, key),
            )

    # ------------------------------------------------------------ 展示
    async def leaderboard(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.store.fetch_all(
            "SELECT subject_key, score, stage FROM affinity WHERE persona_id=? ORDER BY score DESC LIMIT ?",
            (self.config.persona_id, max(1, min(50, limit))),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            key = str(row.get("subject_key") or "")
            score = float(row.get("score") or 0.0)
            stage = self.stage_of(score)
            out.append(
                {
                    "subject_key": key,
                    "score": round(score, 1),
                    "stage": stage,
                    "stage_name": self.stage_name(stage),
                    "name": await self._display_name(key),
                }
            )
        return out

    async def _display_name(self, key: str) -> str:
        try:
            row = await self.store.fetch_one(
                "SELECT display_name FROM mem_subject WHERE persona_id=? AND user_id=?",
                (self.config.persona_id, key),
            )
            if row and row.get("display_name"):
                return str(row["display_name"])
        except Exception:
            pass
        return key

    async def prompt_hint(self, subject_key: str) -> str:
        """给提示词用的一句话。关系远近会影响说话的分寸。"""
        if not self.enabled():
            return ""
        data = await self.get(subject_key)
        stage = int(data.get("stage") or 0)
        if stage <= 0 and float(data.get("score") or 0) < 5:
            return ""
        name = self.stage_name(stage)
        hints = {
            0: "还不太熟，说话客气一点",
            1: "见过几面了，可以熟络些",
            2: "算熟人了，不用太客气",
            3: "是朋友，可以开玩笑",
            4: "关系很近，说话可以随意一些",
            5: "是最亲近的人，什么都能说",
        }
        return f"你和 TA 现在是「{name}」：{hints.get(stage, '')}"

    @staticmethod
    def _loads(value: Any, default: Any) -> Any:
        if isinstance(value, (list, dict)):
            return value
        if not value:
            return default
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default
