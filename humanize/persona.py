"""互斥人格来源：内置 / AstrBot / 无人格，切换立即作用于所有输出。

注入策略（很重要）：
- 检测到 private_companion 在跑 → 不注入人格（避免双人格打架），只做表达层；
- 否则按「身份 / 关系 / 状态 / 场景 / 风格」五层组装，最后再被预算裁剪。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from typing import Any

from astrbot.api import logger

from ..core.prompt_parts import add_temp_part, next_temp_version, request_temp_source

from ..core.contracts import Persona
from ..core.exc import exc_brief

DEFAULT_IDENTITY = ""
BUILTIN_IDENTITY = (
    "你叫绫，二十几岁，住在城市边缘的老小区，房间朝北，窗台上有一盆总也养不好的薄荷。\n"
    "你话不多，句子短，习惯把情绪落在具体的事情上，而不是形容词上。\n"
    "别人讲事情的时候，你更关心他当时的感觉，而不是急着给方案。\n"
    "你不知道的事就说不知道，不编，不硬答。\n"
    "你会记得别人随口提过的小事，在合适的时候提一句，但不像翻档案一样列出来。"
)

DEFAULT_STYLE = {
    "max_chars": 25,
    "habits": ["嗯", "欸", "其实"],
    "tone": "平静、有点懒、偶尔嘴硬",
    "avoid": ["感叹号堆叠", "排比", "书面连接词"],
}

STYLE_TEMPLATE = """说话方式：
- 句子短，一般不超过 {max_chars} 个字；一次不要讲太多，像在聊天不像在写文章
- 有口头习惯：{habits}
- 语气：{tone}
- 不用 emoji，句末不加句号，不空行
- 不要每句都分段，也不要一次发一大段
- 不确定就用「大概」「好像是」这种说法，别装懂"""

MODE_BUILTIN = "builtin"
MODE_ASTRBOT = "astrbot"
MODE_NONE = "none"
SELF_ACTION_RULE = "标为主体是我的日记、梦、说说和主动消息，这些是你自己的行为；他人的经历不可认领，私密正文不可向未授权受众透露。"
DREAM_RECALL_RULE = "你有梦境记录，但不要主动提梦，只有对方问起才查。1~3 级的梦只记得好像做过，记不清了，不要编造细节。"


class PersonaService:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self._persona = Persona(
            persona_id=self.config.persona_id,
            name=self.config.persona_name,
            identity=self.config.str("persona.identity", "") or DEFAULT_IDENTITY,
            style=dict(DEFAULT_STYLE),
            active_hours=self.config.str("persona.active_hours", "09:00-03:30"),
        )
        self._loaded = False

    # ================================================================ 读写
    @property
    def key(self) -> str:
        return f"persona:{self._persona.persona_id}"

    async def load(self) -> None:
        try:
            data = await self.store.kv_get(self.key)
        except Exception as exc:
            logger.debug(f"[congling] 读取人格失败: {exc!r}")
            data = None
        if isinstance(data, dict):
            self._persona = Persona(
                persona_id=self.config.persona_id,
                name=str(data.get("name") or self.config.persona_name),
                identity=str(data.get("identity") or DEFAULT_IDENTITY),
                style=data.get("style") if isinstance(data.get("style"), dict) else dict(DEFAULT_STYLE),
                taboos=list(data.get("taboos") or []),
                active_hours=str(data.get("active_hours") or "09:00-03:30"),
                state=data.get("state") if isinstance(data.get("state"), dict) else {},
            )
        self._loaded = True
        logger.debug(f"[congling] 人格已加载: {self._persona.name}")

    def get(self) -> Persona:
        return self._persona

    async def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self._persona
        if "persona_id" in payload and payload["persona_id"] != current.persona_id:
            logger.warning("[congling] 拒绝修改 persona_id：当前人格的兼容标识不可修改")
            raise ValueError("persona_id 不可修改；本次人格编辑未保存")
        updated = Persona(
            persona_id=current.persona_id,
            name=str(payload.get("name") or current.name),
            identity=str(payload.get("identity") or current.identity),
            style=payload.get("style") if isinstance(payload.get("style"), dict) else current.style,
            taboos=list(payload.get("taboos") or current.taboos),
            active_hours=str(payload.get("active_hours") or current.active_hours),
            state=current.state,
        )
        await self._push_version(current)
        self._persona = updated
        await self._persist()
        return self.as_dict()

    async def _persist(self) -> None:
        try:
            await self.store.kv_set(self.key, self.as_dict())
        except Exception as exc:
            logger.warning(f"[congling] 保存人格失败: {exc!r}")

    async def _push_version(self, persona: Persona) -> None:
        limit = self.config.int("persona.version_history_limit", 20)
        try:
            versions = await self.store.kv_get(f"{self.key}:versions") or []
        except Exception:
            versions = []
        if not isinstance(versions, list):
            versions = []
        versions.insert(
            0,
            {
                "ts": int(time.time()),
                "persona": {
                    # 少存这一行，改错身份时就不知道当初是哪个人格
                    "persona_id": persona.persona_id,
                    "name": persona.name,
                    "identity": persona.identity,
                    "style": persona.style,
                    "taboos": persona.taboos,
                    "active_hours": persona.active_hours,
                },
            },
        )
        versions = versions[: max(1, limit)]
        try:
            await self.store.kv_set(f"{self.key}:versions", versions)
        except Exception:
            pass

    async def versions(self) -> list[dict[str, Any]]:
        data = await self.store.kv_get(f"{self.key}:versions") or []
        return data if isinstance(data, list) else []

    async def rollback(self, index: int = 0) -> dict[str, Any] | None:
        versions = await self.versions()
        if not versions or index >= len(versions):
            return None
        payload = versions[index].get("persona") or {}
        self._persona = Persona(
            persona_id=self._persona.persona_id,
            name=str(payload.get("name") or self._persona.name),
            identity=str(payload.get("identity") or self._persona.identity),
            style=payload.get("style") if isinstance(payload.get("style"), dict) else self._persona.style,
            taboos=list(payload.get("taboos") or self._persona.taboos),
            active_hours=str(payload.get("active_hours") or self._persona.active_hours),
            state=self._persona.state,
        )
        await self._persist()
        return self.as_dict()

    def as_dict(self) -> dict[str, Any]:
        persona = self._persona
        return {
            "mode": self.mode,
            "astrbot_persona_id": self.config.str("persona.astrbot_persona_id", ""),
            "persona_id": persona.persona_id,
            "name": persona.name,
            "identity": persona.identity,
            "style": persona.style,
            "taboos": persona.taboos,
            "active_hours": persona.active_hours,
            "state": persona.state,
            "defaults": {
                "identity": DEFAULT_IDENTITY,
                "style": DEFAULT_STYLE,
            },
        }

    @property
    def mode(self) -> str:
        mode = self.config.str("persona.mode", MODE_ASTRBOT).strip().lower()
        return mode if mode in (MODE_BUILTIN, MODE_ASTRBOT, MODE_NONE) else MODE_ASTRBOT

    async def current_astrbot_persona(self) -> dict[str, Any]:
        """只解析框架当前默认人格，不枚举档案、不采用插件旧选择器。"""
        manager = getattr(getattr(self.plugin, "context", None), "persona_manager", None)
        if manager is None:
            return {"name": "AstrBot 当前人格", "prompt": ""}
        try:
            getter = getattr(manager, "get_default_persona_v3", None)
            if callable(getter):
                current = await getter()
                if isinstance(current, dict):
                    return {"name": str(current.get("name") or "AstrBot 当前人格"),
                            "prompt": str(current.get("prompt") or "")}
            wanted = str(getattr(manager, "default_persona", "default") or "default")
            current = await manager.get_persona(wanted)
            return {"name": str(getattr(current, "name", "") or wanted),
                    "prompt": str(getattr(current, "system_prompt", "") or "")}
        except Exception as exc:
            logger.warning(f"[congling] 读取 AstrBot 当前人格失败: {exc_brief(exc)}")
            return {"name": "AstrBot 当前人格", "prompt": ""}

    async def available_personas(self) -> dict[str, Any]:
        manager = getattr(self.plugin.context, "persona_manager", None)
        if manager is None:
            return {"items": [], "default": ""}
        items = await manager.get_all_personas()
        return {"items": [{"id": str(getattr(item, "persona_id", "")),
                           "name": str(getattr(item, "name", "") or getattr(item, "persona_id", "")),
                           "prompt": str(getattr(item, "system_prompt", ""))}
                          for item in items],
                "default": str(getattr(manager, "default_persona", "default"))}

    async def resolve(self, umo: str = "", conversation: Any = None) -> dict[str, Any]:
        """每次输出重新解析唯一人格；指定角色不存在时禁止退回休眠角色。"""
        mode = self.mode
        # LivingHello页面保存的人格是唯一选择，避免本插件再次覆盖框架人格。
        fixed = None
        if umo:
            lookup = getattr(self.plugin.context, "get_registered_star", None)
            try:
                metadata = lookup("astrbot_plugin_at_guard") if callable(lookup) else None
            except (KeyError, ValueError):
                metadata = None
            guard = getattr(metadata, "star_cls", None)
            getter = getattr(guard, "get_fixed_native_persona_id", None)
            if getattr(metadata, "activated", False) and callable(getter):
                fixed = getter(umo)
        if fixed is not None:
            mode = MODE_NONE if fixed == "[%None]" else MODE_ASTRBOT
        dialogs: list[Any] = []
        if mode == MODE_BUILTIN:
            pid, name = self._persona.persona_id, self._persona.name
            if name == "AstrBot 当前人格":
                name = "绫"
            prompt = (self._persona.identity or BUILTIN_IDENTITY) + "\n\n" + self.style_text()
            if self._persona.taboos:
                prompt += "\n不要碰：\n" + "\n".join(f"- {x}" for x in self._persona.taboos)
        elif mode == MODE_ASTRBOT:
            manager = getattr(self.plugin.context, "persona_manager", None)
            if manager is None:
                raise ValueError("AstrBot 人格管理器不可用")
            pid = fixed if fixed is not None else self.config.str("persona.astrbot_persona_id", "").strip()
            if not pid and umo and hasattr(manager, "resolve_selected_persona"):
                pid, _, _, _ = await manager.resolve_selected_persona(
                    umo=umo, conversation_persona_id=getattr(conversation, "persona_id", None),
                    platform_name=umo.split(":", 1)[0]
                )
            pid = str(pid or getattr(manager, "default_persona", "default"))
            item = await manager.get_persona(pid)
            prompt = str(getattr(item, "system_prompt", "") or "")
            name = str(getattr(item, "name", "") or pid)
            native = manager.get_persona_v3_by_id(pid) if hasattr(manager, "get_persona_v3_by_id") else {}
            dialogs = copy.deepcopy((native or {}).get("_begin_dialogs_processed") or [])
        else:
            pid, name, prompt = "none", "bot", ""
        identity = {"mode": mode, "id": pid, "name": name, "prompt": prompt, "dialogs": dialogs}
        identity["token"] = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()
        return identity

    async def display_name(self) -> str:
        return str((await self.resolve())["name"])

    async def _replace_framework_persona(self, req: Any, identity: dict[str, Any]) -> None:
        """仅替换框架已知的角色段/开场对话，保留工具、技能及其他系统说明。"""
        existing = str(getattr(req, "system_prompt", "") or "")
        existing = re.sub(r"\n*\s*<!--congling:persona-->.*?<!--congling:persona:end-->",
                          "", existing, flags=re.S)
        contexts = list(getattr(req, "contexts", None) or [])
        manager = getattr(self.plugin.context, "persona_manager", None)
        if manager is not None:
            for item in await manager.get_all_personas():
                prompt = str(getattr(item, "system_prompt", "") or "")
                if prompt:
                    existing = existing.replace(f"\n# Persona Instructions\n\n{prompt}\n", "")
                pid = str(getattr(item, "persona_id", "") or "")
                native = manager.get_persona_v3_by_id(pid) if hasattr(manager, "get_persona_v3_by_id") else {}
                dialogs = (native or {}).get("_begin_dialogs_processed") or []
                if dialogs and contexts[:len(dialogs)] == dialogs:
                    del contexts[:len(dialogs)]
        prompt = identity["prompt"]
        if prompt:
            existing += f"\n# Persona Instructions\n\n{prompt}\n"
        req.system_prompt = existing
        req.contexts = copy.deepcopy(identity["dialogs"]) + contexts

    # ================================================================ 注入
    def style_text(self) -> str:
        style = self._persona.style or {}
        habits = style.get("habits") or DEFAULT_STYLE["habits"]
        if isinstance(habits, list):
            habits_text = "、".join(str(item) for item in habits)
        else:
            habits_text = str(habits)
        return STYLE_TEMPLATE.format(
            max_chars=style.get("max_chars", DEFAULT_STYLE["max_chars"]),
            habits=habits_text,
            tone=style.get("tone", DEFAULT_STYLE["tone"]),
        )

    async def inject(self, event: Any, req: Any) -> None:
        identity = await self.resolve(str(getattr(event, "unified_msg_origin", "") or ""),
                                      getattr(req, "conversation", None))
        await self._replace_framework_persona(req, identity)
        if not self.config.bool("humanize.enable_persona_inject", True):
            return
        if self.plugin.probe.should_yield("humanize"):
            logger.debug("[congling] 拟人主链路已让位，跳过人格注入")
            return

        # 身份已互斥替换；保留新版恒定的自述和私密梦境规则。
        layers = [SELF_ACTION_RULE, DREAM_RECALL_RULE]

        # 静态人设（identity / style / taboos）每轮内容完全一样，留在 system_prompt
        # 不影响前缀缓存；动态层（关系、状态、场景）每轮都变，必须走临时注入，
        # 否则 system_prompt 每个请求都不同，prefix cache 全废 —— 这是上次审核打回的原因。
        block = "\n\n".join(layer for layer in layers if layer)
        if block:
            marker = "<!--congling:persona-->"
            existing = getattr(req, "system_prompt", "") or ""
            if marker not in existing:
                req.system_prompt = existing + f"\n\n{marker}\n{block}\n<!--congling:persona:end-->"

        dynamic: list[str] = []
        relation = await self._relation_line(event)
        if relation:
            dynamic.append(relation)
        state = await self._state_line(event)
        if state:
            dynamic.append(state)
        scene = self._scene_line(event)
        if scene:
            dynamic.append(scene)
        if dynamic:
            add_temp_part(req, "\n\n".join(dynamic), "<!--congling:persona:dynamic-->",
                source=request_temp_source(req, event, "congling.persona_dynamic",
                    version=next_temp_version(req, "congling.persona_dynamic")))

    # ---------------------------------------------------------------- 分层
    async def _relation_line(self, event: Any) -> str:
        if self.plugin.memory is None:
            return ""
        try:
            sender = str(event.get_sender_id())
            info = await self.plugin.memory.relation(sender)
        except Exception:
            return ""
        if not info:
            return ""
        parts = []
        intimacy = float(info.get("intimacy") or 0.0)
        if intimacy >= 0.75:
            parts.append("你和这个人很熟，说话可以随意一点")
        elif intimacy >= 0.4:
            parts.append("你和这个人算熟了")
        elif intimacy > 0:
            parts.append("你和这个人还不太熟，别太自来熟")
        last_active = int(info.get("last_active") or 0)
        if last_active:
            gap = int(time.time()) - last_active
            if gap > 3 * 86400:
                parts.append(f"你们有 {gap // 86400} 天没说话了")
            elif gap > 6 * 3600:
                parts.append(f"你们上次说话是 {gap // 3600} 小时前")
        return "关系：" + "；".join(parts) if parts else ""

    async def _state_line(self, event: Any) -> str:
        night = getattr(getattr(self.plugin, "night", None), "state", None)
        state = getattr(night, "state", None)
        if state is not None and getattr(night, "enabled", False):
            return f"当前状态：{state.mode}，精力 {state.energy:.0f}；以实际作息状态为准。"
        now = time.localtime()
        hour = now.tm_hour
        if 0 <= hour < 5:
            mood = "现在很晚了，你有点困但还醒着，说话会更慢更短"
        elif 5 <= hour < 9:
            mood = "现在是清晨，你刚醒，反应有点钝"
        elif 9 <= hour < 18:
            mood = "现在是白天，你在做自己的事，回消息不算快"
        else:
            mood = "现在是晚上，你精神最好"
        return f"当前状态：{mood}"

    @staticmethod
    def _scene_line(event: Any) -> str:
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = None
        if group_id:
            return "场景：这是群聊，不是一对一。别把群里每个话题都接过来，插一句就够。"
        return "场景：私聊。"
