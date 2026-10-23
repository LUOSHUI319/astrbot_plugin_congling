"""戳一戳互动（入站）。

之前 qqext/service.py 里那个 poke() 只是「机器人主动去戳别人」，属于出站，
别人戳它的时候没有任何人接这个球，所以看起来静悄悄。

OneBot 的戳一戳是 notice 事件：
    post_type=notice / notice_type=notify / sub_type=poke
框架会把它当作一条普通消息事件塞进 event_message_type(ALL)，
所以在钩子里认一下 raw_message 就能拿到谁戳了谁。

行为全部走配置，概率不给写死：
    qq_extra.poke_enabled              总开关
    qq_extra.poke_antipoke             被戳时反戳
    qq_extra.poke_talk_probability     反戳后补一句话的概率
    qq_extra.poke_talk_when_no_antipoke 不反戳时也说话
    qq_extra.poke_follow_probability   别人被戳时跟戳的概率
    qq_extra.poke_cooldown_seconds     同一人冷却
    qq_extra.poke_notice_in_prompt     让模型知道这是戳一戳
"""

from __future__ import annotations

import random
import time
from typing import Any

from astrbot.api import logger

# 配置缺省时用的兜底值
DEFAULT_COOLDOWN = 15
DEFAULT_TALK_PROBABILITY = 0.45
DEFAULT_FOLLOW_PROBABILITY = 0.2

POKE_PROMPT = "[系统] 对方刚用戳一戳碰了你一下，用一句话回应，别解释这是什么"
POKE_PROMPT_PLAIN = "[系统] 对方刚碰了你一下，用一句话回应"


def parse_poke(event: Any) -> dict[str, Any] | None:
    """从事件里抠出戳一戳，不是戳一戳就返回 None。"""
    message_obj = getattr(event, "message_obj", None)
    raw = getattr(message_obj, "raw_message", None) if message_obj is not None else None
    if not isinstance(raw, dict):
        return None
    if raw.get("post_type") != "notice":
        return None
    if raw.get("notice_type") != "notify":
        return None
    if raw.get("sub_type") != "poke":
        return None
    return {
        "self_id": str(raw.get("self_id") or ""),
        "user_id": str(raw.get("user_id") or ""),
        "target_id": str(raw.get("target_id") or ""),
        "group_id": str(raw.get("group_id") or ""),
    }


class PokeInbox:
    """被戳之后做点什么。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self._last: dict[str, float] = {}

    # ------------------------------------------------------------ 配置
    def enabled(self) -> bool:
        return self.config.bool("qq_extra.poke_enabled", True)

    def _probability(self, key: str, default: float) -> float:
        try:
            value = self.config.float(key, default)
        except Exception:
            return default
        return max(0.0, min(1.0, float(value)))

    def _cooldown(self) -> int:
        try:
            return max(0, int(self.config.int("qq_extra.poke_cooldown_seconds", DEFAULT_COOLDOWN)))
        except Exception:
            return DEFAULT_COOLDOWN

    def _talk_prompt(self) -> str:
        return POKE_PROMPT if self.config.bool("qq_extra.poke_notice_in_prompt", True) else POKE_PROMPT_PLAIN

    def _cool_down_ok(self, key: str) -> bool:
        gap = self._cooldown()
        now = time.time()
        if gap > 0 and now - self._last.get(key, 0.0) < gap:
            return False
        self._last[key] = now
        return True

    # ------------------------------------------------------------ 出站
    async def _send_poke(self, event: Any, *, user_id: str, group_id: str = "") -> bool:
        params: dict[str, Any] = {"user_id": int(user_id)}
        if group_id:
            params["group_id"] = int(group_id)
        bot = getattr(event, "bot", None)
        if bot is None:
            return False
        try:
            if hasattr(bot, "call_action"):
                await bot.call_action("send_poke", **params)
            else:
                await bot.api.call_action("send_poke", **params)
            return True
        except Exception as exc:
            logger.debug(f"[congling] 反戳失败: {exc!r}")
            return False

    # ------------------------------------------------------------ 诊断
    async def _trace(self, payload: dict[str, Any]) -> None:
        """把最近一次戳一戳的判定落到 kv，UI 和排查都能看。"""
        try:
            store = getattr(self.plugin, "store", None)
            if store is None:
                return
            payload = dict(payload)
            payload["at"] = int(time.time())
            await store.kv_set("poke:last", payload)
        except Exception as exc:
            logger.debug(f"[congling] 戳一戳诊断写入失败: {exc!r}")

    # ------------------------------------------------------------ 主流程
    async def handle(self, event: Any):
        if not self.enabled():
            return
        info = parse_poke(event)
        if info is None:
            return

        me = info["self_id"]
        who = info["user_id"]
        target = info["target_id"]
        group = info["group_id"]

        # 自己戳出去的，先收手，不然会来回弹
        if who and who == me:
            return

        # ---------------------------------------------------- 戳的是我
        if target == me and who:
            if not self._cool_down_ok(f"in:{who}"):
                await self._trace({"branch": "self", "who": who, "group": group, "skip": "cooldown"})
                return

            antipoke = self.config.bool("qq_extra.poke_antipoke", True)
            poked_back = False
            if antipoke:
                poked_back = await self._send_poke(event, user_id=who, group_id=group)

            talk_prob = self._probability("qq_extra.poke_talk_probability", DEFAULT_TALK_PROBABILITY)
            will_talk = talk_prob > 0 and random.random() < talk_prob
            if not poked_back and not self.config.bool("qq_extra.poke_talk_when_no_antipoke", True):
                will_talk = False
            await self._trace(
                {
                    "branch": "self",
                    "who": who,
                    "group": group,
                    "antipoke": poked_back,
                    "talk": will_talk,
                }
            )
            if will_talk:
                yield event.request_llm(prompt=self._talk_prompt())
            return

        # ---------------------------------------------------- 戳的是别人
        if target and target != me:
            key = f"follow:{group}:{target}" if group else f"follow:dm:{target}"
            if not self._cool_down_ok(key):
                await self._trace({"branch": "follow", "who": who, "target": target, "group": group, "skip": "cooldown"})
                return
            follow_prob = self._probability("qq_extra.poke_follow_probability", DEFAULT_FOLLOW_PROBABILITY)
            hit = follow_prob > 0 and random.random() < follow_prob
            sent = False
            if hit and group:
                sent = await self._send_poke(event, user_id=target, group_id=group)
            await self._trace(
                {
                    "branch": "follow",
                    "who": who,
                    "target": target,
                    "group": group,
                    "prob": follow_prob,
                    "hit": hit,
                    "sent": sent,
                    "note": "" if group else "私聊不跟戳",
                }
            )
            return
