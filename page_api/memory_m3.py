"""0.6.0 · 记忆三板块接口：总览 / 按用户分 / 按会话分。

单独一个 mixin，PageApi 继承它。返回格式与老接口一致：{status, ok, data|message, ts}。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request

DAY = 86400


def _ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "ok": True, "data": data, "ts": int(time.time())}


def _err(message: str) -> dict[str, Any]:
    return {"status": "error", "ok": False, "message": str(message)[:300], "ts": int(time.time())}


async def _params() -> dict[str, Any]:
    """GET 的参数在 request.query 里（PluginMultiDict，不是 dict 子类）。"""
    data: dict[str, Any] = {}
    try:
        raw = getattr(request, "query", None)
        if callable(raw):
            raw = raw()
        if raw is not None:
            items = getattr(raw, "items", None)
            if callable(items):
                for key, value in items():
                    data[str(key)] = value
    except Exception:
        pass
    try:
        payload = await request.json(default={})
        if isinstance(payload, dict):
            data.update(payload)
    except Exception:
        pass
    return data


class MemoryM3Mixin:
    """记忆三板块。所有查询都走 plugin.store，不碰写路径。"""

    # ------------------------------------------------------------ 总览
    async def memory_m3_overview(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            store = self.plugin.store
            now = int(time.time())

            total = int(await store.scalar(
                "SELECT COUNT(*) FROM mem_atom WHERE status='active'") or 0)
            # level 0 = 不可忘（永久档）；fading = 衰减到快看不见的
            permanent = int(await store.scalar(
                "SELECT COUNT(*) FROM mem_atom WHERE status='active' AND mem_level=0") or 0)
            fading = int(await store.scalar(
                "SELECT COUNT(*) FROM mem_atom WHERE status='active' AND mem_level>0 AND decay IS NOT NULL AND decay<=0.35") or 0)
            pending = int(await store.scalar(
                "SELECT COUNT(*) FROM mem_atom WHERE status='pending'") or 0)
            month_new = int(await store.scalar(
                "SELECT COUNT(*) FROM mem_atom WHERE status='active' AND created_at>=?",
                (now - 30 * DAY,)) or 0)
            people = int(await store.scalar(
                "SELECT COUNT(DISTINCT subject_id) FROM mem_atom WHERE status='active' AND subject_id<>''") or 0)
            sessions = int(await store.scalar(
                "SELECT COUNT(DISTINCT session_id) FROM mem_atom WHERE status='active' AND session_id<>''") or 0)

            kinds = await store.fetch_all(
                "SELECT kind, COUNT(*) AS c FROM mem_atom WHERE status='active' GROUP BY kind ORDER BY c DESC")
            heat = await store.fetch_all(
                "SELECT date(created_at,'unixepoch','localtime') AS d, COUNT(*) AS c "
                "FROM mem_atom WHERE status='active' AND created_at>=? GROUP BY d ORDER BY d",
                (now - 26 * 7 * DAY,))
            recent = await store.fetch_all(
                "SELECT id, kind, text, importance, created_at, expire_at, session_id, session_label, subject_id, mem_level "
                "FROM mem_atom WHERE status='active' ORDER BY created_at DESC LIMIT 8")

            kind_rows = [{"kind": str(r["kind"] or "fact"), "count": int(r["c"] or 0)} for r in kinds]
            heat_map = {str(r["d"]): int(r["c"] or 0) for r in heat}

            return _ok({
                "total": total,
                "permanent": permanent,
                "permanent_pct": round(permanent * 100 / total, 1) if total else 0,
                "fading": fading,
                "pending": pending,
                "month_new": month_new,
                "people": people,
                "sessions": sessions,
                "kinds": kind_rows,
                "heatmap": heat_map,
                "recent": [dict(r) for r in recent],
                "attitude": self._m3_attitude(kind_rows, total, permanent),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[congling] 记忆总览失败: {exc!r}")
            return _err(repr(exc)[:200])

    @staticmethod
    def _m3_attitude(kinds: list[dict[str, Any]], total: int, permanent: int) -> str:
        if not total:
            return "还没有记忆，聊几句就会开始记。"
        top = kinds[0]["kind"] if kinds else "fact"
        label = {"preference": "偏好", "event": "事件", "relation": "关系", "emotion": "情绪", "fact": "事实"}.get(top, "事实")
        pct = round(permanent * 100 / total)
        return f"{label}记得最牢，情绪类最容易淡；目前 {pct}% 的条目进了永久档，不靠重复出现来续期。"

    # ------------------------------------------------------------ 按会话分
    async def memory_sessions(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            store = self.plugin.store
            rows = await store.fetch_all(
                "SELECT session_id, session_kind, session_label, COUNT(*) AS c, "
                "MAX(created_at) AS last_at, MIN(created_at) AS first_at "
                "FROM mem_atom WHERE status='active' AND session_id<>'' "
                "GROUP BY session_id ORDER BY last_at DESC LIMIT 200")
            items = []
            for r in rows:
                sid = str(r["session_id"] or "")
                items.append({
                    "session_id": sid,
                    "kind": str(r["session_kind"] or ""),
                    "label": str(r["session_label"] or "") or sid,
                    "count": int(r["c"] or 0),
                    "last_at": int(r["last_at"] or 0),
                    "first_at": int(r["first_at"] or 0),
                    "days": max(1, round((int(r["last_at"] or 0) - int(r["first_at"] or 0)) / DAY)),
                })
            return _ok({"items": items, "total": len(items)})
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[congling] 会话列表失败: {exc!r}")
            return _err(repr(exc)[:200])

    async def memory_session_detail(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            params = await _params()
            sid = str(params.get("session_id") or params.get("key") or "").strip()
            if not sid:
                return _err("缺 session_id")
            store = self.plugin.store
            atoms = await store.fetch_all(
                "SELECT id, kind, text, importance, created_at, expire_at, subject_id, mem_level "
                "FROM mem_atom WHERE status='active' AND session_id=? ORDER BY created_at DESC LIMIT 100",
                (sid,))
            kinds = await store.fetch_all(
                "SELECT kind, COUNT(*) AS c FROM mem_atom WHERE status='active' AND session_id=? GROUP BY kind",
                (sid,))
            hours = await store.fetch_all(
                "SELECT CAST(strftime('%H', created_at, 'unixepoch', 'localtime') AS INTEGER) AS h, COUNT(*) AS c "
                "FROM mem_atom WHERE status='active' AND session_id=? GROUP BY h", (sid,))
            meta = await store.fetch_one(
                "SELECT session_kind, session_label FROM mem_atom WHERE session_id=? LIMIT 1", (sid,))
            hour_map = {int(r["h"] or 0): int(r["c"] or 0) for r in hours}
            return _ok({
                "session_id": sid,
                "kind": str((meta or {}).get("session_kind") or ""),
                "label": str((meta or {}).get("session_label") or "") or sid,
                "atoms": [dict(r) for r in atoms],
                "kinds": [{"kind": str(r["kind"] or "fact"), "count": int(r["c"] or 0)} for r in kinds],
                "hours": {str(h): hour_map.get(h, 0) for h in range(24)},
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[congling] 会话详情失败: {exc!r}")
            return _err(repr(exc)[:200])
