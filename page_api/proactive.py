"""主动找人接口（0.7.0 · 第 11 条）。

只看不动：谁会收到、今天发了多少、最近说了什么、有没有被回。
以及三个手动操作：立刻发一条、静音、存配置。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request


def _ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "ok": True, "data": data, "ts": int(time.time())}


def _err(message: str) -> dict[str, Any]:
    return {"status": "error", "ok": False, "message": str(message)[:300], "ts": int(time.time())}


async def _body() -> dict[str, Any]:
    payload = await request.json(default={})
    return payload if isinstance(payload, dict) else {}


async def _params() -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:
        raw = getattr(request, "query", None)
        if callable(raw):
            raw = raw()
        if raw is not None:
            for key in ("limit", "session_id"):
                try:
                    value = raw.get(key)
                except Exception:
                    value = None
                if value not in (None, ""):
                    data[key] = value
    except Exception:
        pass
    return data


class ProactiveMixin:
    async def proactive_state(self) -> dict[str, Any]:
        try:
            proactive = getattr(self.plugin, "proactive", None)
            if proactive is None:
                return _ok({"enabled": False})
            return _ok(await proactive.state_view())
        except Exception as exc:
            logger.warning(f"[congling] 主动状态失败: {exc!r}")
            return _err(repr(exc)[:200])

    async def proactive_sessions(self) -> dict[str, Any]:
        try:
            proactive = getattr(self.plugin, "proactive", None)
            if proactive is None:
                return _ok({"items": []})
            return _ok({"items": await proactive.sessions_view()})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def proactive_log(self) -> dict[str, Any]:
        try:
            proactive = getattr(self.plugin, "proactive", None)
            if proactive is None:
                return _ok({"items": []})
            params = await _params()
            try:
                limit = int(params.get("limit") or 30)
            except (TypeError, ValueError):
                limit = 30
            return _ok({"items": await proactive.log_view(limit)})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def proactive_now(self) -> dict[str, Any]:
        try:
            proactive = getattr(self.plugin, "proactive", None)
            if proactive is None:
                return _err("主动模块未就绪")
            body = await _body()
            session_id = str(body.get("session_id") or "").strip()
            if not session_id:
                return _err("要带 session_id")
            result = await proactive.send_now(session_id)
            if not result.get("ok"):
                return _err(result.get("error") or "没发出去")
            return _ok(result)
        except Exception as exc:
            logger.warning(f"[congling] 手动主动发送失败: {exc!r}")
            return _err(repr(exc)[:200])

    async def proactive_mute(self) -> dict[str, Any]:
        try:
            proactive = getattr(self.plugin, "proactive", None)
            if proactive is None:
                return _err("主动模块未就绪")
            body = await _body()
            session_id = str(body.get("session_id") or "").strip()
            try:
                hours = int(body.get("hours") or 6)
            except (TypeError, ValueError):
                hours = 6
            await proactive.mute(session_id, hours)
            return _ok({"muted": True, "hours": hours, "session_id": session_id})
        except Exception as exc:
            return _err(repr(exc)[:200])
