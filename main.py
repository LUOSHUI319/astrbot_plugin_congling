"""从绫开始 · 插件入口。

职责只有三件事：
1. 装配——把内核、各能力模块按配置拼起来，缺哪个就降级哪个；
2. 挂载——在正确的优先级上挂框架钩子；
3. 委托——所有业务逻辑交给 modules，本文件不写业务。

任何模块初始化失败都不允许让插件加载失败：聊天链路不能因为一个功能坏掉而整体停摆。
"""

from __future__ import annotations

import asyncio
import time
from sys import maxsize
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core import paths
from .core.budget import Budget
from .core.bus import EventBus
from .core.config import Config
from .core.contracts import Persona
from .core.guard import OutputGuard
from .core.log import DecisionLogger
from .core.prompt_parts import add_temp_part
from .core.probe import Probe
from .core.rhythm import RhythmController
from .core.store import Store

PLUGIN_VERSION = "0.6.1"
PLUGIN_NAME = "从绫开始"

# 钩子优先级（数值越小越晚执行）
# 长夜拦截要排在归档之后：睡着期间消息照样进库，只是不回复
PRIO_NIGHT = maxsize - 130
PRIO_PERCEIVE = maxsize - 100
PRIO_MEMORY = maxsize - 90
PRIO_PERSONA = maxsize - 80
PRIO_RESPONSE = -100
PRIO_EXPRESS = -100000


@register(
    "astrbot_plugin_congling",
    "洛水",
    "从绫开始：拟人化表达、长期记忆、特殊消息解析、实用查询与 QQ 扩展",
    PLUGIN_VERSION,
)
class ConglingPlugin(Star):
    """从绫开始。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.raw_config = config
        self.cfg = Config(config)
        self.bus = EventBus()
        self.store = Store(paths.db_path())
        self.probe = Probe(context, self.cfg)
        self.guard = OutputGuard(self.cfg, self.probe)
        self.rhythm = RhythmController(self.cfg, self.guard, self.probe)
        self.budget = Budget(self.cfg)
        self.decision = DecisionLogger(self.store, self.cfg)

        # 业务模块（可能缺席，全部按 Optional 处理）
        self.perception: Any = None
        self.persona: Any = None
        self.decider: Any = None
        self.composer: Any = None
        self.memory: Any = None
        self.extractor: Any = None
        self.retriever: Any = None
        self.reflector: Any = None
        self.capabilities: Any = None
        self.models: Any = None
        self.qqext: Any = None
        self.poke_inbox: Any = None
        self.night: Any = None
        self.page_api: Any = None
        self.commands: Any = None

        self._started_at = int(time.time())
        self._boot_errors: list[str] = []
        self.config_upgrade: dict[str, Any] = {}

    # ================================================================ 生命周期
    async def initialize(self) -> None:
        await self.store.open()
        try:
            from .core import migrate as config_migrate

            self.config_upgrade = await config_migrate.run(self)
        except Exception as exc:
            self.config_upgrade = {"applied": False, "reason": repr(exc)}
            logger.debug(f"[congling] 配置升级失败: {exc!r}")
        await self._safe_boot("models", self._boot_models)

        await self._safe_boot("perception", self._boot_perception)
        await self._safe_boot("persona", self._boot_persona)
        await self._safe_boot("humanize", self._boot_humanize)
        await self._safe_boot("memory", self._boot_memory)
        await self._safe_boot("night", self._boot_night)
        await self._safe_boot("capability", self._boot_capability)
        await self._safe_boot("qqext", self._boot_qqext)
        await self._safe_boot("commands", self._boot_commands)
        await self._safe_boot("page_api", self._boot_page_api)

        self.probe.refresh(force=True)
        logger.info(
            f"[congling] 已启动：人格={self.cfg.persona_name} "
            f"模式={self.cfg.str('compat.mode', 'auto')} "
            f"| {self.probe.summary_line()}"
        )
        if self._boot_errors:
            logger.warning(f"[congling] 部分模块降级: {', '.join(self._boot_errors)}")

        try:
            asyncio.get_running_loop().create_task(self._housekeeping())
            if self.night is not None:
                asyncio.get_running_loop().create_task(self._dream_loop())
        except RuntimeError:
            pass

    async def terminate(self) -> None:
        try:
            if self.memory is not None and hasattr(self.memory, "flush"):
                await self.memory.flush()
        except Exception as exc:
            logger.debug(f"[congling] 记忆落盘失败: {exc!r}")
        try:
            await self.store.close()
        except Exception:
            pass
        logger.info("[congling] 已卸载")

    async def _safe_boot(self, name: str, boot) -> None:
        try:
            await boot()
        except Exception as exc:
            self._boot_errors.append(f"{name}: {exc}")
            logger.warning(f"[congling] 模块 {name} 初始化失败，已降级: {exc!r}")

    # ---------------------------------------------------------------- 装配
    async def _boot_models(self) -> None:
        from .core.modelhub import ModelHub

        self.models = ModelHub(self)
        logger.debug("[congling] 模型中心就绪")

    async def _boot_perception(self) -> None:
        from .perception.service import PerceptionService

        self.perception = PerceptionService(self)
        logger.debug("[congling] 感知模块就绪")

    async def _boot_persona(self) -> None:
        from .humanize.persona import PersonaService

        self.persona = PersonaService(self)
        await self.persona.load()

    async def _boot_humanize(self) -> None:
        from .humanize.compose import Composer
        from .humanize.decide import Decider

        self.decider = Decider(self)
        self.composer = Composer(self)

    async def _boot_memory(self) -> None:
        if not self.cfg.bool("memory.enabled", True):
            logger.info("[congling] 记忆模块已关闭")
            return
        from .memory.service import MemoryService

        self.memory = MemoryService(self)
        await self.memory.start()

    async def _boot_night(self) -> None:
        # 长夜如梦没开、但收线静默开着时也要装起来：这时只有「说晚安就停」这一件事生效
        if not self.cfg.bool("night.enabled", False) and not self.cfg.bool("close.enabled", True):
            logger.info("[congling] 长夜如梦与收线静默都未启用")
            return
        from .night import NightModule

        self.night = NightModule(self)
        await self.night.start()

    async def _boot_capability(self) -> None:
        if not self.cfg.bool("capability.enabled", True):
            return
        from .capability.registry import CapabilityRegistry

        self.capabilities = CapabilityRegistry(self)
        await self.capabilities.start()

    async def _boot_qqext(self) -> None:
        from .qqext import PokeInbox, QQExt

        self.qqext = QQExt(self)
        self.poke_inbox = PokeInbox(self)

    async def _boot_commands(self) -> None:
        from .commands import Commands

        self.commands = Commands(self)

    async def _boot_page_api(self) -> None:
        from .page_api import PageApi

        self.page_api = PageApi(self)
        self.page_api.register_routes()

    async def _housekeeping(self) -> None:
        """每日维护：衰减、反思、日志清理、过期缓存。"""
        await asyncio.sleep(30)
        while True:
            try:
                if self.memory is not None and hasattr(self.memory, "daily_tick"):
                    await self.memory.daily_tick()
                await self.decision.purge()
                await self._purge_cache()
                await self._qq_daily_tick()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug(f"[congling] 维护任务出错: {exc!r}")
            await asyncio.sleep(3600)

    async def _dream_loop(self) -> None:
        """每 5 分钟看一次该不该做梦。比整点轮询细，开销可以忽略。"""
        await asyncio.sleep(120)
        while True:
            try:
                if self.night is not None:
                    await self.night.dream_tick()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug(f"[congling] 做梦任务出错: {exc!r}")
            await asyncio.sleep(300)

    async def _purge_cache(self) -> None:
        cutoff = int(time.time()) - 7 * 86400
        try:
            await self.store.execute("DELETE FROM forward_cache WHERE expires_at < ?", (cutoff,))
        except Exception:
            pass

    async def _qq_daily_tick(self) -> None:
        """每小时看一次：今天的说说到点了没有，该换脸了没有。

        两件事都很轻：说说受配额和时段约束，头像受日限和头像库约束，
        没到点就立刻返回，不占资源。
        """
        ext = self.qqext
        if ext is None:
            return
        try:
            daily = getattr(ext, "qzone_daily", None)
            if daily is not None:
                await daily.tick()
        except Exception as exc:
            logger.debug(f"[congling] 每日说说任务出错: {exc!r}")
        try:
            pool = getattr(ext, "avatar_pool", None)
            avatar = getattr(ext, "avatar", None)
            if pool is not None and avatar is not None:
                if self.cfg.bool("qq_extra.avatar_auto_change", True):
                    await pool.auto_change(avatar)
        except Exception as exc:
            logger.debug(f"[congling] 自动换头像失败: {exc!r}")

    # ================================================================ 工具方法
    @property
    def persona_id(self) -> str:
        return self.cfg.persona_id

    def current_persona(self) -> Persona:
        if self.persona is not None:
            return self.persona.get()
        return Persona(
            persona_id=self.cfg.persona_id,
            name=self.cfg.persona_name,
            identity=self.cfg.str("persona.identity", ""),
            active_hours=self.cfg.str("persona.active_hours", "09:00-03:30"),
        )

    def enabled_for(self, event: AstrMessageEvent) -> bool:
        """插件是否对该事件生效。"""
        if not self.cfg.bool("basic.enabled", True):
            return False
        try:
            platform = (event.get_platform_name() or "").lower()
        except Exception:
            platform = ""
        platforms = self.cfg.platforms()
        if platform and platform not in platforms:
            return False
        is_group = True
        try:
            is_group = bool(event.get_group_id())
        except Exception:
            pass
        if is_group and not self.cfg.bool("basic.enable_group", True):
            return False
        if not is_group and not self.cfg.bool("basic.enable_private", True):
            return False
        return True

    # ================================================================ 管理员
    def astrbot_admins(self) -> list[str]:
        """AstrBot 全局管理员（WebUI 里的 admins_id）。"""
        result: list[str] = []
        try:
            conf = self.context.get_config()
            raw = conf.get("admins_id") or []
        except Exception:
            raw = []
        if isinstance(raw, str):
            raw = [raw]
        for item in raw or []:
            text = str(item).strip()
            if text and text.lower() != "astrbot":
                result.append(text)
        return result

    def extra_admins(self) -> list[str]:
        """插件自己加的管理员（补 AstrBot 名单用的）。"""
        items = self.cfg.list("basic.extra_admins", [])
        if not items:  # 兼容旧配置
            items = self.cfg.list("basic.admins", [])
        return [str(item).strip() for item in items if str(item).strip()]

    def admin_ids(self) -> list[str]:
        merged: list[str] = []
        for item in self.astrbot_admins() + self.extra_admins():
            if item not in merged:
                merged.append(item)
        return merged

    def is_admin(self, event: AstrMessageEvent) -> bool:
        """判定顺序：AstrBot 全局管理员 → 插件额外管理员 → （可选）群主/群管。"""
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            sender = ""
        if sender and sender in self.admin_ids():
            return True

        if not self.cfg.bool("basic.include_group_role", False):
            return False

        role = ""
        try:
            message_obj = event.get_message_obj() if hasattr(event, "get_message_obj") else None
            sender_obj = getattr(message_obj, "sender", None)
            role = str(getattr(sender_obj, "role", "") or "")
        except Exception:
            role = ""
        if not role:
            try:
                role = str(getattr(event, "role", "") or "")
            except Exception:
                role = ""
        return role.lower() in {"admin", "owner"}

    # ================================================================ 钩子
    @filter.event_message_type(filter.EventMessageType.ALL, priority=PRIO_PERCEIVE - 2)
    async def poke_hook(self, event: AstrMessageEvent):
        """别人戳我，总得有点反应。

        戳一戳是 notice 事件，不是文本消息，所以要抢在归档之前认出来，
        否则它会被当成一条没有内容的消息悄悄放过。
        """
        if self.poke_inbox is None or not self.enabled_for(event):
            return
        try:
            async for msg in self.poke_inbox.handle(event):
                if msg is not None:
                    yield msg
        except Exception as exc:
            logger.debug(f"[congling] 戳一戳处理失败: {exc!r}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=PRIO_PERCEIVE - 1)
    async def archive_hook(self, event: AstrMessageEvent) -> None:
        """所有消息都归档，包括没触发回复的群聊闲聊。

        记忆的原料在这里进库；不 yield，不影响其他 handler 和回复流程。
        """
        if self.perception is None or not self.enabled_for(event):
            return
        try:
            msg = await self.perception.perceive(event, enrich=False)
            await self.perception.archive(msg)
        except Exception as exc:
            logger.debug(f"[congling] 全局归档失败: {exc!r}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=PRIO_NIGHT)
    async def night_hook(self, event: AstrMessageEvent) -> None:
        """睡着的时候，除了被叫醒，其余一律不接。

        放在归档之后：睡着的这段消息照样进记忆（只是打上没爬楼的标记），
        但不会叫模型、不会回复。唤醒是一条概率递增的路，攒到位才会醒。
        """
        if self.night is None or self.perception is None or not self.enabled_for(event):
            return
        try:
            msg = await self.perception.perceive(event)
            self_id = ""
            for getter in ("get_self_id", "get_bot_id"):
                func = getattr(event, getter, None)
                if callable(func):
                    try:
                        value = func()
                        if value:
                            self_id = str(value)
                            break
                    except Exception:
                        continue
            result = await self.night.on_message(msg, self_id=self_id)
            if result.get("block"):
                # 记一条决策日志，方便你回头看成因为什么没理人
                try:
                    from .core.contracts import ReplyDecision

                    self.decision.decide(
                        ReplyDecision(
                            should_reply=False,
                            reason=f"长夜·{result.get('hint', '')}",
                        ),
                        session_id=msg.session_id,
                        msg_id=msg.msg_id,
                    )
                except Exception:
                    pass
                event.stop_event()
        except Exception as exc:
            logger.debug(f"[congling] 长夜拦截失败: {exc!r}")

    @filter.on_llm_request(priority=PRIO_PERCEIVE)
    async def perceive_hook(self, event: AstrMessageEvent, req: Any) -> None:
        if not self.enabled_for(event) or self.perception is None:
            return
        try:
            await self.perception.inject(event, req)
        except Exception as exc:
            logger.debug(f"[congling] 感知注入失败: {exc!r}")

    @filter.on_llm_request(priority=PRIO_MEMORY)
    async def memory_hook(self, event: AstrMessageEvent, req: Any) -> None:
        if not self.enabled_for(event) or self.memory is None:
            return
        try:
            await self.memory.inject(event, req)
        except Exception as exc:
            logger.debug(f"[congling] 记忆注入失败: {exc!r}")

    @filter.on_llm_request(priority=PRIO_PERSONA)
    async def persona_hook(self, event: AstrMessageEvent, req: Any) -> None:
        if not self.enabled_for(event):
            return
        try:
            if self.persona is not None:
                await self.persona.inject(event, req)
            if self.night is not None:
                hint = self.night.prompt_hint()
                if hint:
                    add_temp_part(req, hint, "<!--congling:night-->")
            if self.qqext is not None:
                avatar_hint = self.qqext.avatar_hint()
                if avatar_hint:
                    add_temp_part(req, avatar_hint, "<!--congling:avatar-->")
            if self.capabilities is not None:
                self.capabilities.attach_tools(req)
        except Exception as exc:
            logger.debug(f"[congling] 人格注入失败: {exc!r}")

    @filter.on_llm_response(priority=PRIO_RESPONSE)
    async def response_hook(self, event: AstrMessageEvent, resp: Any) -> None:
        if not self.enabled_for(event) or self.memory is None:
            return
        try:
            await self.memory.observe_response(event, resp)
        except Exception as exc:
            logger.debug(f"[congling] 记忆观察失败: {exc!r}")

    @filter.on_decorating_result(priority=PRIO_EXPRESS)
    async def express_hook(self, event: AstrMessageEvent) -> None:
        """最后一道工序：清理 → 分句 → 接管发送节奏。"""
        if not self.enabled_for(event):
            return
        try:
            result = event.get_result()
            if result is None or not getattr(result, "chain", None):
                return
            # 流式输出不做分句：分句会把流式体验撕碎
            try:
                from astrbot.core.message.message_event_result import ResultContentType

                if getattr(result, "result_content_type", None) == ResultContentType.STREAMING_RESULT:
                    self.guard.clean_chain(result.chain)
                    return
            except Exception:
                pass
            # 工具调用类结果不动（图片、卡片由各模块自己保证格式）
            if self.composer is not None and hasattr(result, "is_llm_result"):
                if not result.is_llm_result():
                    self.guard.clean_chain(result.chain)
                    return
            if self.composer is not None:
                await self.composer.decorate(event, result)
            else:
                self.guard.clean_chain(result.chain)
        except Exception as exc:
            logger.debug(f"[congling] 表达阶段失败: {exc!r}")

    @filter.after_message_sent()
    async def after_sent_hook(self, event: AstrMessageEvent) -> None:
        if not self.enabled_for(event):
            return
        if self.memory is not None:
            try:
                await self.memory.after_sent(event)
            except Exception as exc:
                logger.debug(f"[congling] 记忆回写失败: {exc!r}")
        # 收线 / 爬楼：交给长夜模块判断现在该睡还是该先翻消息
        if self.night is not None:
            try:
                session_id = ""
                try:
                    session_id = str(event.get_group_id() or event.get_sender_id() or "")
                except Exception:
                    session_id = ""
                await self.night.after_reply(None, session_id=session_id)
            except Exception as exc:
                logger.debug(f"[congling] 长夜收尾失败: {exc!r}")

    # ================================================================ 指令
    @filter.command("绫")
    async def cmd_congling(self, event: AstrMessageEvent):
        """从绫开始：状态 / 记忆 / 记住 / 忘掉 / 空间 / 兼容 / 调试"""
        if self.commands is None:
            yield event.plain_result("插件还没准备好")
            return
        try:
            raw = getattr(event, "message_str", "") or ""
            parts = raw.strip().split()
            args = parts[1:] if len(parts) > 1 else []
            reply = await self.commands.handle(event, args)
        except Exception as exc:
            logger.debug(f"[congling] 指令处理失败: {exc!r}")
            reply = "这条指令我没处理好"
        if reply:
            yield event.plain_result(reply)

    # ================================================================ 状态
    def health(self) -> dict[str, Any]:
        return {
            "plugin": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "uptime": int(time.time()) - self._started_at,
            "persona": {"id": self.cfg.persona_id, "name": self.cfg.persona_name},
            "modules": {
                "perception": self.perception is not None,
                "persona": self.persona is not None,
                "memory": self.memory is not None,
                "night": self.night is not None,
                "capability": self.capabilities is not None,
                "models": self.models is not None,
                "qqext": self.qqext is not None,
                "page_api": self.page_api is not None,
            },
            "compat": self.probe.matrix(),
            "boot_errors": list(self._boot_errors),
            "config_upgrade": dict(self.config_upgrade or {}),
        }
