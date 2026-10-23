"""把「看看谁的空间」变成 QQ 号。

三种指向：
1. 说话人自己（"我的空间"、没指定时）
2. 消息里 @ 的那个人
3. 明确的 QQ 号 / 昵称（群成员或好友里查）

昵称查不到或不唯一时明确说出来，不猜。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

SELF_WORDS = {
    "我",
    "我的",
    "自己",
    "我的空间",
    "我空间",
    "本人",
    "me",
    "self",
    "mine",
}
AT_WORDS = {"他", "她", "ta", "TA", "这个", "这个人", "那人", "那个人", "楼上", "楼下"}
# 模型有时会把整句塞进 target（"看我空间"），这些片段一出现就说明指的是自己
SELF_PATTERNS = ("我的空间", "我空间", "看看我", "看我", "我的主页", "我主页")
CACHE_TTL = 300.0


@dataclass
class Resolution:
    uin: int = 0
    label: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.uin > 0 and not self.error


class TargetResolver:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._cache: dict[str, tuple[float, list[dict[str, str]]]] = {}

    # ================================================================ 对外
    async def resolve(self, raw: str, event: Any = None) -> Resolution:
        text = str(raw or "").strip()

        # 1) 明确的 QQ 号
        digits = re.fullmatch(r"[@oO]?\s*(\d{5,12})", text)
        if digits:
            uin = int(digits.group(1))
            return Resolution(uin=uin, label=str(uin))

        sender = self._sender(event)

        # 2) 自己
        lowered = text.lower()
        if text and (
            lowered in SELF_WORDS or any(pattern in lowered for pattern in SELF_PATTERNS)
        ):
            if sender:
                return Resolution(uin=sender, label="你自己")
            return Resolution(error="拿不到你的 QQ 号")

        # 3) 空着或指代别人 → 先看有没有 @
        if not text or text in AT_WORDS:
            at_uin = self._at_uin(event)
            if at_uin:
                return Resolution(uin=at_uin, label=f"@{at_uin}")
            if sender:
                return Resolution(uin=sender, label="你自己")

        # 4) 昵称
        if text:
            return await self._by_nickname(text, event)

        return Resolution(error="没看出要看谁的空间")

    # ================================================================ 上下文
    @staticmethod
    def _sender(event: Any) -> int:
        if event is None:
            return 0
        try:
            return int(event.get_sender_id() or 0)
        except Exception:
            return 0

    @staticmethod
    def _at_uin(event: Any) -> int:
        if event is None:
            return 0
        message_obj = getattr(event, "message_obj", None)
        chain = getattr(message_obj, "message", None) or []
        for component in chain:
            name = type(component).__name__
            if name not in {"At", "AtComponent"} and getattr(component, "type", "") != "at":
                continue
            qq = str(getattr(component, "qq", "") or "")
            if qq.isdigit() and qq != "all":
                return int(qq)
        return 0

    # ================================================================ 昵称
    async def _by_nickname(self, text: str, event: Any) -> Resolution:
        people = await self._people(event)
        if not people:
            return Resolution(error=f"没找到叫「{text}」的人，可以直接给我 QQ 号")

        exact = [item for item in people if text in item["names"]]
        pool = exact or [item for item in people if any(text in name for name in item["names"])]
        if not pool:
            return Resolution(error=f"没找到叫「{text}」的人，可以直接给我 QQ 号")

        uins = []
        for item in pool:
            if item["uin"] not in uins:
                uins.append(item["uin"])
        if len(uins) > 1:
            preview = "、".join(f"{pool[i]['names'][0]}({uins[i]})" for i in range(min(3, len(uins))))
            return Resolution(error=f"叫「{text}」的有好几个人：{preview}，说具体点或者直接给 QQ 号")

        target = next(item for item in pool if item["uin"] == uins[0])
        return Resolution(uin=uins[0], label=f"{target['names'][0]}({uins[0]})")

    async def _people(self, event: Any) -> list[dict[str, Any]]:
        """群成员 + 好友，带缓存。"""
        result: list[dict[str, Any]] = []
        result.extend(await self._group_members(event))
        result.extend(await self._friends())
        return result

    async def _group_members(self, event: Any) -> list[dict[str, Any]]:
        if event is None:
            return []
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            group_id = ""
        if not group_id:
            return []
        key = f"group:{group_id}"
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return cached[1]

        rows = await self._call("get_group_member_list", group_id=int(group_id))
        people: list[dict[str, Any]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    uin = int(row.get("user_id") or 0)
                except (TypeError, ValueError):
                    continue
                if uin <= 0:
                    continue
                names = [
                    str(row.get(key2) or "").strip()
                    for key2 in ("card", "nickname", "remark")
                ]
                names = [name for name in names if name]
                if names:
                    people.append({"uin": uin, "names": names})
        self._cache[key] = (time.time(), people)
        return people

    async def _friends(self) -> list[dict[str, Any]]:
        key = "friends"
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < CACHE_TTL:
            return cached[1]
        rows = await self._call("get_friend_list")
        people: list[dict[str, Any]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    uin = int(row.get("user_id") or 0)
                except (TypeError, ValueError):
                    continue
                if uin <= 0:
                    continue
                names = [
                    str(row.get(key2) or "").strip()
                    for key2 in ("remark", "nickname")
                ]
                names = [name for name in names if name]
                if names:
                    people.append({"uin": uin, "names": names})
        self._cache[key] = (time.time(), people)
        return people

    async def _call(self, action: str, **params: Any) -> Any:
        bridge = getattr(self.plugin.qqext, "qzone", None) if self.plugin.qqext else None
        if bridge is None:
            return None
        bot = bridge._onebot_bot()
        if bot is None:
            return None
        try:
            return await bridge._call_action(bot, action, **params)
        except Exception as exc:
            logger.debug(f"[congling] 取名单失败 {action}: {exc!r}")
            return None
