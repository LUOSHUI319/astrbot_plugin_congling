"""日记接口（0.7.0 · 第 6 条）。

列表 / 单篇 / 现在就写 / 删 / 置顶。
写的时候是一次模型调用，可能几秒，前端要按钮转圈。
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
    """GET 参数可能在 query 里，也可能被塞进 body，两边都收。"""
    data: dict[str, Any] = {}
    try:
        raw = getattr(request, "query", None)
        if callable(raw):
            raw = raw()
        if raw is not None:
            for key in ("limit", "offset", "day", "id"):
                try:
                    value = raw.get(key)
                except Exception:
                    value = None
                if value not in (None, ""):
                    data[key] = value
    except Exception:
        pass
    return data


class DiaryMixin:
    # ------------------------------------------------------------ 读
    async def diary_list(self) -> dict[str, Any]:
        try:
            diary = getattr(self.plugin, "diary", None)
            if diary is None:
                return _ok({"items": [], "enabled": False})
            params = await _params()

            def as_int(name: str, default: int) -> int:
                try:
                    return int(params.get(name, default) or default)
                except (TypeError, ValueError):
                    return default

            limit = max(1, min(120, as_int("limit", 30)))
            offset = max(0, as_int("offset", 0))
            items = await diary.list(limit, offset)
            return _ok({
                "items": items,
                "enabled": diary.enabled,
                "hour": self.cfg.int("diary.hour", 23),
            })
        except Exception as exc:
            logger.warning(f"[congling] 日记列表失败: {exc!r}")
            return _err(repr(exc)[:200])

    async def diary_get(self) -> dict[str, Any]:
        try:
            diary = getattr(self.plugin, "diary", None)
            if diary is None:
                return _err("日记模块未就绪")
            params = await _params()
            day = str(params.get("day") or "").strip()
            if not day:
                return _err("要带 day=YYYY-MM-DD")
            row = await diary.get(day)
            if row is None:
                return _err("这天没有日记")
            return _ok(row)
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ------------------------------------------------------------ 写
    async def diary_generate(self) -> dict[str, Any]:
        try:
            diary = getattr(self.plugin, "diary", None)
            if diary is None:
                return _err("日记模块未就绪")
            body = await _body()
            day = str(body.get("day") or "").strip() or None
            if day and not diary.enabled:
                # 手动指定日期时也不许绕过总开关，避免"关了还在写"
                return _err("日记总开关是关的，先在设置里打开")
            if not diary.enabled:
                return _err("日记总开关是关的，先在设置里打开")
            row = await diary.compose(day_key=day)
            if row is None:
                return _err("这次没写成（可能这天没什么可记的，或者模型没给出内容）")
            return _ok(row)
        except Exception as exc:
            logger.warning(f"[congling] 日记生成失败: {exc!r}")
            return _err(repr(exc)[:200])

    # ------------------------------------------------------------ 改
    async def diary_delete(self) -> dict[str, Any]:
        try:
            diary = getattr(self.plugin, "diary", None)
            if diary is None:
                return _err("日记模块未就绪")
            body = await _body()
            try:
                diary_id = int(body.get("id") or 0)
            except (TypeError, ValueError):
                diary_id = 0
            if diary_id <= 0:
                return _err("要带 id")
            return _ok({"deleted": await diary.delete(diary_id)})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def diary_pin(self) -> dict[str, Any]:
        try:
            diary = getattr(self.plugin, "diary", None)
            if diary is None:
                return _err("日记模块未就绪")
            body = await _body()
            try:
                diary_id = int(body.get("id") or 0)
            except (TypeError, ValueError):
                diary_id = 0
            if diary_id <= 0:
                return _err("要带 id")
            on = bool(body.get("pinned", True))
            return _ok({"pinned": await diary.pin(diary_id, on)})
        except Exception as exc:
            return _err(repr(exc)[:200])
