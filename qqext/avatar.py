"""换头像。

走 OneBot 的 set_qq_avatar，在插件进程内直接 call_action，不用临时插件、不用重启 QQ。

几个已经踩过的坑，写在这儿免得再踩：
1. file 参数必须带 "base64://" 前缀，传裸 base64 会被协议端当成本地路径；
2. 大图先压，压不动就明说，别让协议端超时；
3. set_qq_avatar 成功时通常返回 None，不能拿返回值判成败。
"""

from __future__ import annotations

import base64
import hashlib
import io
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..core import paths

MAX_BYTES = 512 * 1024


class AvatarService:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self._last: dict[str, Any] = {}

    # ------------------------------------------------------------ 能力
    def _bot(self) -> Any:
        bot = getattr(self.plugin, "_current_bot", None)
        if bot is not None:
            return bot
        try:
            from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

            platform = self.plugin.context.get_platform(PlatformAdapterType.AIOCQHTTP)
            if platform is None:
                return None
            return platform.get_client()
        except Exception:
            return None

    def max_bytes(self) -> int:
        try:
            kb = int(self.config.int("qq_extra.avatar_max_kb", 512))
        except (TypeError, ValueError):
            kb = 512
        return max(64, min(4096, kb)) * 1024

    def daily_limit(self) -> int:
        try:
            return max(0, min(20, self.config.int("qq_extra.avatar_daily_limit", 1)))
        except (TypeError, ValueError):
            return 1

    async def _used_today(self) -> int:
        try:
            record = await self.store.kv_get("avatar:auto_today", {})
        except Exception:
            record = {}
        if not isinstance(record, dict) or record.get("day") != time.strftime("%Y-%m-%d"):
            return 0
        return int(record.get("count") or 0)

    async def _mark_used(self) -> None:
        day = time.strftime("%Y-%m-%d")
        try:
            record = await self.store.kv_get("avatar:auto_today", {})
            if not isinstance(record, dict) or record.get("day") != day:
                record = {"day": day, "count": 0}
            record["count"] = int(record.get("count") or 0) + 1
            await self.store.kv_set("avatar:auto_today", record)
        except Exception:
            pass

    # ------------------------------------------------------------ 换
    async def set_from_file(self, path: str, *, reason: str = "user") -> dict[str, Any]:
        file_path = Path(str(path or "").strip())
        if not file_path.exists() or not file_path.is_file():
            return {"ok": False, "reason": "找不到这张图"}
        try:
            data = file_path.read_bytes()
        except Exception as exc:
            return {"ok": False, "reason": f"读不了图：{exc!r}"[:120]}
        result = await self.set_from_bytes(data, reason=reason, source=str(file_path))
        return result

    async def set_from_bytes(
        self,
        data: bytes,
        *,
        reason: str = "user",
        source: str = "",
    ) -> dict[str, Any]:
        if reason == "auto":
            if not self.config.bool("qq_extra.avatar_auto_change", True):
                return {"ok": False, "reason": "自主换头像关着"}
            used = await self._used_today()
            if used >= self.daily_limit():
                return {"ok": False, "reason": "今天已经换过了"}

        payload, note = self._shrink(data)
        if payload is None:
            return {"ok": False, "reason": note or "图片太大，压不下来"}

        bot = self._bot()
        if bot is None:
            return {"ok": False, "reason": "拿不到 bot 客户端（协议端可能没连上）"}

        encoded = "base64://" + base64.b64encode(payload).decode()
        try:
            if hasattr(bot, "call_action"):
                await bot.call_action("set_qq_avatar", file=encoded)
            else:
                await bot.api.call_action("set_qq_avatar", file=encoded)
        except Exception as exc:
            text = repr(exc)
            low = text.lower()
            if "not found" in low or "unknown" in low or "不支持" in text:
                return {"ok": False, "reason": "这个协议端不支持换头像，NapCat 才行"}
            return {"ok": False, "reason": f"换不上：{text[:120]}"}

        if reason == "auto":
            await self._mark_used()
        self._last = {
            "at": int(time.time()),
            "reason": reason,
            "source": source[:200],
            "bytes": len(payload),
            "sha1": hashlib.sha1(payload).hexdigest()[:16],
        }
        try:
            await self.store.kv_set("avatar:last", self._last)
        except Exception:
            pass
        logger.info(f"[congling] 头像已更换（{reason}，{len(payload) // 1024}KB）")
        return {"ok": True, "bytes": len(payload), "reason": note or "换好了", "at": self._last["at"]}

    def _shrink(self, data: bytes) -> tuple[bytes | None, str]:
        """超限就压。优先 Pillow，没装就只接受原图。"""
        limit = self.max_bytes()
        if len(data) <= limit:
            return data, ""
        try:
            from PIL import Image
        except Exception:
            return None, f"图片 {len(data) // 1024}KB 超过上限 {limit // 1024}KB，压缩库没装"
        try:
            image = Image.open(io.BytesIO(data))
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGB")
            for side in (1024, 768, 512, 384):
                buf = io.BytesIO()
                copy = image.copy()
                copy.thumbnail((side, side))
                copy.save(buf, format="JPEG", quality=86, optimize=True)
                if buf.tell() <= limit:
                    return buf.getvalue(), f"压缩到 {side}px"
            return None, "压到 384px 还是超限"
        except Exception as exc:
            return None, f"压缩失败：{exc!r}"[:120]

    # ------------------------------------------------------------ 状态
    async def state(self) -> dict[str, Any]:
        try:
            last = await self.store.kv_get("avatar:last", {})
        except Exception:
            last = {}
        if not last:
            last = self._last
        return {
            "available": self._bot() is not None,
            "last": last if isinstance(last, dict) else {},
            "used_today": await self._used_today(),
            "daily_limit": self.daily_limit(),
            "max_kb": self.max_bytes() // 1024,
            "dir": str(paths.avatar_dir()),
        }
