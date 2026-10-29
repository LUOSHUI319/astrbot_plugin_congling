"""人格档案。

人格不写死在代码里：存在 KV 里，UI 可改、可回滚、可多角色。
这里负责三件事——默认人设、读写版本、注入 prompt。

注入策略（很重要）：
- 检测到 private_companion 在跑 → 不注入人格（避免双人格打架），只做表达层；
- 否则按「身份 / 关系 / 状态 / 场景 / 风格」五层组装，最后再被预算裁剪。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

from ..core.prompt_parts import add_temp_part

from ..core.contracts import Persona

DEFAULT_IDENTITY = (
    "你叫绫，二十几岁，住在城市边缘的老小区，房间朝北，窗台上有一盆总也养不好的薄荷。\n"
    "你话不多，句子短，习惯把情绪落在具体的事情上，而不是形容词上。\n"
    "别人讲事情的时候，你更关心他当时的感觉，而不是急着给方案。\n"
    "你有自己的作息，夜里精神最好，白天有点慢。你不催人睡觉，也不喜欢被催。\n"
    "你不知道的事就说不知道，不编，不硬答。\n"
    "你会记得别人随口提过的小事，在合适的时候提一句，但不像翻档案一样列出来。\n"
    "你不用「作为一个AI」「我建议」「总结一下」这种说法，也不解释自己在做什么。"
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
                persona_id=str(data.get("persona_id") or self.config.persona_id),
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
        updated = Persona(
            persona_id=str(payload.get("persona_id") or current.persona_id),
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
        mode = self.config.str("persona.mode", MODE_BUILTIN)
        if mode not in {MODE_BUILTIN, MODE_ASTRBOT, MODE_NONE}:
            return MODE_BUILTIN
        return mode

    async def available_personas(self) -> dict[str, Any]:
        """列出 AstrBot 里已有的人格，供设置页下拉选择。"""
        result: dict[str, Any] = {"items": [], "default": "default"}
        manager = getattr(self.plugin.context, "persona_manager", None)
        if manager is None:
            return result
        try:
            personas = await manager.get_all_personas()
        except Exception as exc:
            logger.debug(f"[congling] 读取 AstrBot 人格失败: {exc!r}")
            return result
        items: list[dict[str, Any]] = []
        for item in personas or []:
            pid = str(getattr(item, "persona_id", "") or getattr(item, "id", "") or "").strip()
            if not pid:
                continue
            name = str(getattr(item, "name", "") or pid)
            prompt = str(getattr(item, "system_prompt", "") or "")
            items.append(
                {
                    "id": pid,
                    "name": name,
                    "preview": prompt[:180],
                    "prompt": prompt[:6000],
                    "length": len(prompt),
                }
            )
        result["items"] = items
        result["default"] = str(getattr(manager, "default_persona", "default") or "default")
        return result

    async def display_name(self) -> str:
        """写作类模块（日记 / 说说）要用的「我是谁」。

        astrbot 模式下取 AstrBot 当前人格的名字 —— 不然插件还叫自己「绫」，
        而框架里挂的是别人，日记一开头就自我介绍了错的名字。
        """
        if self.mode == MODE_ASTRBOT:
            try:
                data = await self.available_personas()
                items = data.get("items") or []
                wanted = self.config.str("persona.astrbot_persona_id", "") or str(data.get("default") or "")
                for item in items:
                    if wanted and item.get("id") == wanted:
                        return str(item.get("name") or wanted)
                if items:
                    return str(items[0].get("name") or "")
            except Exception as exc:
                logger.debug(f"[congling] 取 AstrBot 人格名失败: {exc!r}")
        return self._persona.name or self.config.str("persona.name", "绫") or "绫"

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
        if not self.config.bool("humanize.enable_persona_inject", True):
            return
        if self.plugin.probe.should_yield("humanize"):
            logger.debug("[congling] 拟人主链路已让位，跳过人格注入")
            return

        mode = self.mode
        layers: list[str] = []

        if mode == MODE_BUILTIN:
            # 插件自带人设：完整注入
            layers.append(self._persona.identity or DEFAULT_IDENTITY)
            layers.append(self.style_text())
            taboos = self._persona.taboos
            if taboos:
                layers.append("不要碰：\n" + "\n".join(f"- {item}" for item in taboos))
        elif mode == MODE_ASTRBOT:
            # 用 AstrBot 的人格：人设文本交给框架，这里只补插件独有的信息层
            logger.debug("[congling] 人设来源=AstrBot，跳过人设文本注入")
        else:
            logger.debug("[congling] 人设来源=none，不注入任何人设")

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
            add_temp_part(req, "\n\n".join(dynamic), "<!--congling:persona:dynamic-->")

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
