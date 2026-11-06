"""LivingHello 文字判定恢复后的独立补读入口。"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from astrbot.api import logger

CONTRACT = "congling.classifier-recovery.v1"
MAX_ROWS_PER_SESSION = 500


@dataclass(frozen=True)
class RecoveryDelivery:
    """Task 6 可接管的单会话投递对象。"""

    event_id: str
    session_umo: str
    archive_session_id: str
    session_kind: str
    dedupe_key: str
    text: str
    identity: dict[str, Any]


class ClassifierRecovery:
    """按冻结窗口补读归档；不读取或修改任何睡眠状态。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.store = plugin.store
        self._fallback_rollup: Any = None

    async def recover(
        self,
        work: Any,
        *,
        sender: Callable[[RecoveryDelivery], Awaitable[Any]] | None = None,
    ) -> dict[str, Any]:
        event_id = str(self._value(work, "event_id") or "").strip()
        window_start = self._timestamp(self._value(work, "window_start"))
        window_end = self._timestamp(self._value(work, "window_end"))
        sessions = tuple(self._value(work, "sessions", ()) or ())
        if not event_id or window_end < window_start:
            raise ValueError("invalid classifier recovery work")

        sender = sender or getattr(getattr(self.plugin, "proactive", None),
                                   "send_recovery", None)
        available = (
            getattr(self.store, "ready", False)
            and getattr(self.plugin, "persona", None) is not None
            and getattr(self.plugin, "models", None) is not None
            and callable(sender)
        )
        results: list[dict[str, str]] = []
        for item in sessions:
            session_umo = str(self._value(item, "session_id") or "").strip()
            previous = self._status_value(
                self._value(item, "backfill_status", "")
            )
            if previous in {"completed", "no_reply", "unavailable"}:
                results.append(self._result(session_umo, previous))
                continue
            if previous == "send_unknown":
                results.append(
                    self._result(
                        session_umo,
                        "send_unknown",
                        self._dedupe_key(event_id, session_umo),
                    )
                )
                continue
            if not available:
                results.append(self._result(session_umo, "unavailable"))
                continue
            try:
                result = await self._recover_session(
                    event_id,
                    session_umo,
                    window_start,
                    window_end,
                    sender,
                )
            except Exception:
                logger.warning(
                    "[congling] 判定恢复补读未完成，已保留会话级待重试状态。"
                )
                result = self._result(session_umo, "retry_pending")
            results.append(result)
        return {
            "contract": CONTRACT,
            "event_id": event_id,
            "sessions": results,
        }

    async def _recover_session(
        self,
        event_id: str,
        session_umo: str,
        window_start: float,
        window_end: float,
        sender: Callable[[RecoveryDelivery], Awaitable[Any]],
    ) -> dict[str, str]:
        archive_session_id, session_kind = self._parse_umo(session_umo)
        dedupe_key = self._dedupe_key(event_id, session_umo)
        status_getter = getattr(
            getattr(self.plugin, "proactive", None),
            "recovery_delivery_status",
            None,
        )
        if callable(status_getter):
            prior = str(await status_getter(dedupe_key) or "")
            if prior == "sent":
                return self._result(session_umo, "completed", dedupe_key)
            if prior == "unknown":
                return self._result(session_umo, "send_unknown", dedupe_key)

        rows = await self._window_rows(
            session_umo,
            archive_session_id,
            session_kind,
            window_start,
            window_end,
        )
        if not rows:
            return self._result(session_umo, "no_reply")

        rollup = self._rollup()
        identity = await self.plugin.persona.resolve(session_umo)
        if not str(identity.get("token") or ""):
            raise RuntimeError("current persona is unavailable")
        chunks = await rollup.rollup_session(
            archive_session_id,
            since_ts=int(window_start),
            until_ts=int(window_end),
            commit=False,
            rows=rows,
            read_only=True,
            include_items=True,
            umo=session_umo,
        )
        selected: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        seen: set[tuple[int, str]] = set()
        for chunk in chunks:
            plan = await rollup.triage(chunk)
            facts.extend(chunk.facts)
            for scored in plan.get("to_reply") or []:
                item = scored.get("item")
                key = (
                    int(getattr(item, "source_row_id", 0) or 0),
                    str(getattr(item, "msg_id", "") or ""),
                )
                if item is None or key in seen:
                    continue
                seen.add(key)
                selected.append(scored)
                if len(selected) >= rollup._max_replies():
                    break
            if len(selected) >= rollup._max_replies():
                break
        if not selected:
            return self._result(session_umo, "no_reply")

        text = await rollup.compose_recovery_reply(
            {"to_reply": selected, "facts": facts},
            umo=session_umo,
            identity=identity,
        )
        if not text:
            return self._result(session_umo, "retry_pending")
        delivery = RecoveryDelivery(
            event_id=event_id,
            session_umo=session_umo,
            archive_session_id=archive_session_id,
            session_kind=session_kind,
            dedupe_key=dedupe_key,
            text=text,
            identity=identity,
        )
        raw = await sender(delivery)
        send_status = str(
            raw.get("status") if isinstance(raw, dict) else raw or ""
        ).strip().lower()
        status = {
            "sent": "completed",
            "unknown": "send_unknown",
        }.get(send_status, "retry_pending")
        return self._result(session_umo, status, dedupe_key)

    async def _window_rows(
        self,
        session_umo: str,
        archive_session_id: str,
        session_kind: str,
        window_start: float,
        window_end: float,
    ) -> list[dict[str, Any]]:
        start = int(window_start)
        end = int(window_end)
        args = (
            archive_session_id,
            session_kind,
            start,
            end,
            session_umo,
        )
        upper = int(await self.store.scalar(
            "SELECT MAX(id) FROM msg_archive WHERE session_id=? "
            "AND session_kind=? AND ts>=? AND ts<=? "
            "AND (COALESCE(umo,'')='' OR umo=?)",
            args,
            0,
        ) or 0)
        if upper <= 0:
            return []
        rows = await self.store.fetch_all(
            "SELECT id,msg_id,session_id,session_kind,sender_id,sender_name,"
            "ts,text,umo,platform FROM msg_archive WHERE session_id=? "
            "AND session_kind=? AND ts>=? AND ts<=? "
            "AND (COALESCE(umo,'')='' OR umo=?) AND id<=? "
            "ORDER BY id ASC LIMIT ?",
            (*args, upper, MAX_ROWS_PER_SESSION + 1),
        )
        if len(rows) > MAX_ROWS_PER_SESSION:
            raise RuntimeError("classifier recovery archive window is over limit")
        return rows

    def _rollup(self):
        night_rollup = getattr(getattr(self.plugin, "night", None), "rollup", None)
        if night_rollup is not None:
            return night_rollup
        if self._fallback_rollup is None:
            from ..night.rollup import Rollup

            self._fallback_rollup = Rollup(self.plugin)
        return self._fallback_rollup

    @staticmethod
    def _parse_umo(value: str) -> tuple[str, str]:
        parts = str(value or "").split(":", 2)
        if len(parts) != 3 or not parts[0] or not parts[2]:
            raise ValueError("invalid recovery session")
        kind = {
            "GroupMessage": "group",
            "FriendMessage": "private",
        }.get(parts[1])
        if kind is None:
            raise ValueError("unsupported recovery session")
        return parts[2], kind

    @staticmethod
    def _timestamp(value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("invalid recovery timestamp")
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ValueError("invalid recovery timestamp")
        return result

    @staticmethod
    def _value(value: Any, key: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    @staticmethod
    def _status_value(value: Any) -> str:
        return str(getattr(value, "value", value) or "").strip().lower()

    @staticmethod
    def _dedupe_key(event_id: str, session_umo: str) -> str:
        return hashlib.sha256(
            f"{event_id}\0{session_umo}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _result(
        session_id: str,
        status: str,
        dedupe_key: str = "",
    ) -> dict[str, str]:
        return {
            "session_id": session_id,
            "status": status,
            "dedupe_key": dedupe_key,
        }
