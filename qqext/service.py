"""QQ 扩展功能：签名、名片、资料、戳一戳、空间。

写操作（改签名、发说说）默认关闭——这不是保守，是账号安全。
所有 OneBot 调用都做能力探测：拿不到 bot 就安静失败，不刷错误日志。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from .qzone import QzoneBridge


class QQExt:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.qzone = QzoneBridge(plugin)
        self._last_poke: dict[str, float] = {}

    # ================================================================ 底层
    def _bot(self) -> Any:
        bot = getattr(self.plugin, "_current_bot", None)
        if bot is not None:
            return bot
        event = getattr(self.plugin, "_current_event", None)
        return getattr(event, "bot", None) if event is not None else None

    async def _call(self, action: str, **params: Any) -> dict[str, Any] | None:
        bot = self._bot()
        if bot is None:
            return None
        try:
            if hasattr(bot, "call_action"):
                data = await bot.call_action(action, **params)
            else:
                data = await bot.api.call_action(action, **params)
            return data if isinstance(data, dict) else {"data": data}
        except Exception as exc:
            logger.debug(f"[congling] OneBot 调用 {action} 失败: {exc!r}")
            return None

    # ================================================================ 签名
    async def set_signature(self, text: str) -> tuple[bool, str]:
        text = (text or "").strip()
        if not text:
            return False, "签名不能是空的"
        if not self.config.bool("qq_extra.signature_auto_update", False):
            return False, "自动改签名是关着的，先在设置里打开"
        for action in ("set_self_longnick", "set_qq_profile"):
            payload = {"longNick": text} if action == "set_self_longnick" else {"personal_note": text}
            data = await self._call(action, **payload)
            if data is not None:
                return True, f"签名改成「{text}」了"
        return False, "这个协议端不支持改签名"

    # ================================================================ 名片
    async def set_card(self, group_id: str, card: str, user_id: str | None = None) -> tuple[bool, str]:
        params: dict[str, Any] = {"group_id": int(group_id), "card": str(card)[:30]}
        if user_id:
            params["user_id"] = int(user_id)
        data = await self._call("set_group_card", **params)
        return (data is not None), "改好了" if data is not None else "改名失败，可能没有权限"

    # ================================================================ 资料
    async def profile(self, user_id: str, group_id: str | None = None) -> dict[str, Any]:
        user_id = str(user_id or "").strip()
        if not user_id.isdigit():
            return {"ok": False, "text": "这个 QQ 号不对"}
        info: dict[str, Any] = {}
        if group_id:
            data = await self._call(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
            info = (data or {}).get("data") or {}
        if not info:
            data = await self._call("get_stranger_info", user_id=int(user_id), no_cache=True)
            info = (data or {}).get("data") or {}

        nickname = info.get("nickname") or info.get("card") or user_id
        lines = [f"{nickname}（{user_id}）"]
        if info.get("sex") in {"male", "female"}:
            lines.append("男" if info["sex"] == "male" else "女")
        if info.get("age"):
            lines.append(f"{info['age']} 岁")
        if info.get("level"):
            lines.append(f"等级 {info['level']}")
        if info.get("card") and info.get("card") != nickname:
            lines.append(f"群名片 {info['card']}")

        relation = {}
        if self.plugin.memory is not None:
            try:
                relation = await self.plugin.memory.relation(user_id)
                count = await self.plugin.memory.atoms.count()
                lines.append(f"记忆库 {count} 条")
            except Exception:
                relation = {}
        if relation:
            intimacy = float(relation.get("intimacy") or 0)
            lines.append(f"亲密度 {intimacy:.2f}")
            last = int(relation.get("last_active") or 0)
            if last:
                days = (int(time.time()) - last) // 86400
                lines.append("今天聊过" if days <= 0 else f"{days} 天前聊过")
        return {"ok": True, "text": "，".join(lines), "data": info, "relation": relation}

    # ================================================================ 戳一戳
    async def poke(self, user_id: str, *, force: bool = False) -> tuple[bool, str]:
        if not self.config.bool("qq_extra.poke_enabled", True):
            return False, "戳一戳关着呢"
        user_id = str(user_id or "").strip()
        if not user_id.isdigit():
            return False, "戳不到这个人"
        now = time.time()
        if not force and now - self._last_poke.get(user_id, 0.0) < 60:
            return False, ""  # 太频繁，安静地不回
        self._last_poke[user_id] = now
        data = await self._call("send_poke", user_id=int(user_id))
        return (data is not None), "戳了一下"

    # ================================================================ 状态
    async def status(self) -> dict[str, Any]:
        try:
            qzone = await self.qzone.status()
        except Exception as exc:
            qzone = {"mode": self.qzone.mode(), "available": False, "auth": {"ok": False}, "note": str(exc)[:120]}
        return {
            "qzone": qzone,
            "signature_write": self.config.bool("qq_extra.signature_auto_update", False),
            "poke": self.config.bool("qq_extra.poke_enabled", True),
            "profile_card": self.config.bool("qq_extra.profile_card_enabled", True),
        }
