"""每日说说。

不是"每天从文案库里抽一句"，而是从她自己今天的生活里抽——梦里剩下的一段、
今天刚记住的事、刚才那句情绪很浓的话。这样发出来的东西才像一个人随手写的。

配额是一天 1~3 条（可配），时段随机撒在白天到深夜之间。素材 7 天内不复用，
涉及别人的内容一律不发。
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any

from astrbot.api import logger

PROMPT = """你要发一条 QQ 空间的说说，像随手写的一小段日常。

今天的素材：
{materials}

要求：
1. 不超过 60 个字，一到两句话
2. 第一人称，口语，别写成文案、别励志、别总结人生
3. 不要 emoji，不要话题标签，不要括号注释
4. 可以用素材里的细节，但不要逐字复述
5. 只输出说说正文，不要任何前缀说明
"""

REUSE_DAYS = 7
PRIVATE_PATTERNS = (
    re.compile(r"\d{6,}"),          # QQ 号、手机号、长数字
    re.compile(r"(密码|验证码|身份证|银行卡)"),
    re.compile(r"(https?://|www\.)"),
    re.compile(r"@\S+"),
)


class QzoneDaily:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self._recent_cache: list[str] | None = None
        self._name_cache: list[str] | None = None

    # ------------------------------------------------------------ 配置
    def enabled(self) -> bool:
        try:
            return bool(self.config.bool("qq_extra.qzone_auto_post", True))
        except Exception:
            return False

    def quota_range(self) -> tuple[int, int]:
        try:
            low = int(self.config.int("qq_extra.qzone_daily_min", 1))
            high = int(self.config.int("qq_extra.qzone_daily_max", 3))
        except (TypeError, ValueError):
            low, high = 1, 3
        low = max(0, min(10, low))
        high = max(low, min(10, high))
        return low, high

    def _state_key(self, day: str) -> str:
        return f"qzone:daily:{day}"

    async def _state(self, day: str) -> dict[str, Any]:
        raw = await self.store.kv_get(self._state_key(day), {})
        if not isinstance(raw, dict):
            raw = {}
        if not raw:
            low, high = self.quota_range()
            target = random.randint(low, high) if high > low else low
            hours = sorted(random.sample(range(9, 24), min(target, 15)))
            raw = {"day": day, "target": target, "hours": hours, "posted": []}
            await self.store.kv_set(self._state_key(day), raw)
        return raw

    # ------------------------------------------------------------ 触发
    async def tick(self) -> dict[str, Any]:
        """每小时被叫一次，决定现在要不要发。"""
        if not self.enabled():
            return {"ok": False, "reason": "自动说说关着"}
        bridge = self._bridge()
        if bridge is None or not getattr(bridge, "available", lambda: False)():
            return {"ok": False, "reason": "空间不可用"}

        day = time.strftime("%Y-%m-%d")
        state = await self._state(day)
        posted = state.get("posted") or []
        if len(posted) >= int(state.get("target") or 0):
            return {"ok": False, "reason": "今天配额已满"}
        hour = time.localtime().tm_hour
        if hour not in (state.get("hours") or []):
            return {"ok": False, "reason": "还没到今天的时段"}

        return await self.post_now(reason="auto")

    async def force_post(self) -> dict[str, Any]:
        bridge = self._bridge()
        if bridge is None:
            return {"ok": False, "reason": "空间不可用"}
        return await self.post_now(reason="manual")

    async def post_now(self, *, reason: str = "auto") -> dict[str, Any]:
        bridge = self._bridge()
        if bridge is None:
            return {"ok": False, "reason": "空间不可用"}
        await self.load_recent()
        await self.load_names()
        materials = await self.materials()
        if not materials:
            return {"ok": False, "reason": "今天没什么可说的"}
        text = await self.compose(materials)
        if not text:
            return {"ok": False, "reason": "没生成出来"}
        if not self.safe(text):
            return {"ok": False, "reason": "内容里带隐私，拦下了"}

        if self.config.bool("qq_extra.qzone_write_confirm", False) and reason == "auto":
            # 让主链路去问一句，这里只登记待发
            try:
                await self.store.kv_set(
                    "qzone:pending",
                    {"text": text, "at": int(time.time())},
                )
            except Exception:
                pass
            return {"ok": True, "pending": True, "text": text}

        result = await bridge.publish(text)
        if not result.get("ok"):
            return {"ok": False, "reason": str(result.get("text") or "发布失败")}

        day = time.strftime("%Y-%m-%d")
        state = await self._state(day)
        posted = list(state.get("posted") or [])
        posted.append({"text": text[:80], "at": int(time.time())})
        state["posted"] = posted
        try:
            await self.store.kv_set(self._state_key(day), state)
            await self._remember_material(materials)
        except Exception:
            pass
        logger.info(f"[congling] 说说已发（{reason}）：{text[:30]}")
        return {"ok": True, "text": text, "fid": result.get("fid", "")}

    # ------------------------------------------------------------ 素材
    async def materials(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        now = int(time.time())

        # 1) 最近的高等级梦
        dreamer = None
        night = getattr(self.plugin, "night", None)
        if night is not None:
            dreamer = getattr(night, "dreamer", None)
        if dreamer is not None:
            try:
                dreams = [d for d in dreamer.living() if int(getattr(d, "level", 0) or 0) >= 3]
                for dream in dreams[-2:]:
                    items.append(
                        {"kind": "dream", "text": str(getattr(dream, "text", ""))[:300], "weight": 3.0}
                    )
            except Exception as exc:
                logger.debug(f"[congling] 读梦失败: {exc!r}")

        # 2) 今天新记下的事
        try:
            rows = await self.store.fetch_all(
                "SELECT text, mem_level FROM mem_atom WHERE persona_id=? AND status='active' "
                "AND created_at>=? AND mem_level BETWEEN 2 AND 4 ORDER BY created_at DESC LIMIT 8",
                (self.config.persona_id, now - 86400),
            )
            for row in rows:
                items.append(
                    {
                        "kind": "memory",
                        "text": str(row.get("text") or "")[:200],
                        "weight": float(row.get("mem_level") or 3),
                    }
                )
        except Exception:
            pass

        # 3) 情绪浓度高的记忆（近三天）
        try:
            rows = await self.store.fetch_all(
                "SELECT text FROM mem_atom WHERE persona_id=? AND status='active' "
                "AND level_tags LIKE '%emotion_high%' AND created_at>=? ORDER BY created_at DESC LIMIT 4",
                (self.config.persona_id, now - 3 * 86400),
            )
            for row in rows:
                items.append({"kind": "emotion", "text": str(row.get("text") or "")[:200], "weight": 2.5})
        except Exception:
            pass

        # 4) 兜底：当前状态
        try:
            if night is not None and hasattr(night, "prompt_hint"):
                hint = str(night.prompt_hint() or "")
                if hint:
                    items.append({"kind": "state", "text": hint[:160], "weight": 1.0})
        except Exception:
            pass

        recent = set(self._recent_hashes())
        fresh = [item for item in items if self._hash(item["text"]) not in recent]
        fresh.sort(key=lambda item: item.get("weight", 1.0), reverse=True)
        return fresh[:5]

    def _hash(self, text: str) -> str:
        import hashlib

        return hashlib.sha1((text or "").strip().encode("utf-8", "ignore")).hexdigest()[:16]

    def _recent_hashes(self) -> list[str]:
        # 用内存缓存：素材复用只查个大概，不必每次读库
        return list(self._recent_cache or [])

    async def load_recent(self) -> None:
        try:
            items = await self.store.kv_get("qzone:recent_materials", [])
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []
        cutoff = int(time.time()) - REUSE_DAYS * 86400
        self._recent_cache = [
            str(item.get("hash"))
            for item in items
            if isinstance(item, dict) and int(item.get("at") or 0) >= cutoff
        ]

    async def _remember_material(self, materials: list[dict[str, Any]]) -> None:
        """发出去的素材 7 天内不再用。"""
        if not materials:
            return
        now = int(time.time())
        cutoff = now - REUSE_DAYS * 86400
        try:
            items = await self.store.kv_get("qzone:recent_materials", [])
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []
        keep = [
            item
            for item in items
            if isinstance(item, dict) and int(item.get("at") or 0) >= cutoff
        ]
        for item in materials:
            keep.append({"hash": self._hash(item.get("text") or ""), "at": now})
        await self.store.kv_set("qzone:recent_materials", keep[-80:])
        self._recent_cache = [str(entry["hash"]) for entry in keep]

    # ------------------------------------------------------------ 生成
    async def compose(self, materials: list[dict[str, Any]]) -> str:
        provider = self._provider()
        body = "\n".join(f"- {item['text']}" for item in materials)
        if provider is None:
            return ""
        try:
            resp = await provider.text_chat(prompt=PROMPT.replace("{materials}", body[:1500]), contexts=[])
            text = str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.debug(f"[congling] 说说生成失败: {exc!r}")
            return ""
        text = text.strip().strip("“”\"'")
        text = re.sub(r"^```.*?\n|```$", "", text).strip()
        if len(text) > 120:
            text = text[:120]
        return text

    def safe(self, text: str) -> bool:
        body = str(text or "")
        if not body.strip():
            return False
        for pattern in PRIVATE_PATTERNS:
            if pattern.search(body):
                return False
        # 别人的名字别发出去
        for name in self._other_names():
            if name and len(name) >= 2 and name in body:
                return False
        return True

    def _other_names(self) -> list[str]:
        return list(self._name_cache or [])

    async def load_names(self) -> None:
        try:
            rows = await self.store.fetch_all(
                "SELECT display_name FROM mem_subject WHERE persona_id=? LIMIT 200",
                (self.config.persona_id,),
            )
        except Exception:
            rows = []
        self._name_cache = [str(row.get("display_name") or "") for row in rows if row.get("display_name")]

    # ------------------------------------------------------------ 工具
    def _bridge(self) -> Any:
        qqext = getattr(self.plugin, "qqext", None)
        return getattr(qqext, "qzone", None) if qqext is not None else None

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
