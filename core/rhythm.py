"""发送节奏控制（全插件唯一单例）。

技法来源：reply_delay —— 把 event.send 换成自己的包装函数，之后框架不管怎么
调用发送，都会经过这里。好处：
- 分段回复天然「一条一抽」，不用去猜框架内部什么时候发；
- 只影响当前事件对象，不污染别的会话；
- 可以中途改变主意（用户又说话了就合并剩余气泡立刻发）。

与 reply_delay 并存时不包装（见 probe.should_yield('rhythm')），避免双重延时。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Plain, Reply

from .contracts import RhythmPlan

PATCH_FLAG = "_congling_rhythm_patched"


class RhythmController:
    def __init__(self, config: Any, guard: Any, probe: Any) -> None:
        self.config = config
        self.guard = guard
        self.probe = probe
        self._patched_events: set[int] = set()

    # ------------------------------------------------------------ 计划
    def build_plan(
        self,
        bubbles: list[str],
        *,
        input_chars: int = 0,
        session_kind: str = "group",
        quote: bool = False,
        mention: bool = False,
        as_voice: bool = False,
        sticker_tag: str | None = None,
        reason: str = "",
        extra_hold: float = 0.0,
    ) -> RhythmPlan:
        cleaned = self.guard.clean_bubbles(bubbles)
        if not cleaned:
            cleaned = [""]
        max_bubbles = self.config.int(
            "humanize.max_bubbles_private" if session_kind == "private" else "humanize.max_bubbles_group",
            3 if session_kind == "private" else 2,
        )
        if len(cleaned) > max_bubbles:
            head = cleaned[: max_bubbles - 1]
            tail = " ".join(cleaned[max_bubbles - 1 :])
            cleaned = head + [tail]

        delays: list[float] = []
        for index, bubble in enumerate(cleaned):
            if index == 0:
                base = self.config.float("humanize.think_delay_base", 1.2)
                delay = base + 0.02 * max(0, input_chars)
                delay = min(max(delay, 1.0), 4.5)
            else:
                factor = self.config.float("humanize.bubble_delay_factor", 0.09)
                delay = 0.6 + factor * len(bubble)
            delay *= random.uniform(0.7, 1.3)
            delays.append(round(max(delay, 0.35), 2))

        # 延时回复（humanize.reply_delay）不另起一层 event.send 包装，
        # 只在这里给首条气泡加一段额外等待——两层包装会互相以为对方没发。
        if extra_hold and delays:
            delays[0] = round(min(25.0, delays[0] + max(0.0, float(extra_hold))), 2)

        return RhythmPlan(
            bubbles=cleaned,
            delays=delays,
            quote=quote,
            mention=mention,
            as_voice=as_voice,
            sticker_tag=sticker_tag,
            reason=reason,
        )

    # ------------------------------------------------------------ 接管发送
    def attach(self, event: Any, plan: RhythmPlan) -> bool:
        """把 event.send 换成按 plan 逐条发送。返回是否成功接管。"""
        if not plan.bubbles:
            return False
        if self.probe is not None and self.probe.should_yield("rhythm"):
            logger.debug("[congling] 检测到外部延时插件，跳过节奏接管")
            return False
        if getattr(event, PATCH_FLAG, False):
            return False

        original_send = getattr(event, "send", None)
        if not callable(original_send):
            return False

        state: dict[str, Any] = {"done": False}
        bubbles = list(plan.bubbles)
        delays = list(plan.delays) or [0.0] * len(bubbles)
        joined = "".join(bubbles)

        async def wrapped_send(message: Any = None) -> None:
            if state["done"]:
                # 已经按计划发过了：框架若还有分段残留，丢弃；其他插件的补发放行
                text = self._plain_text(message)
                if text and self._covered(text, joined):
                    return
                await original_send(message)
                return
            state["done"] = True
            reply_comp = self._extract_reply(message)
            extras = self._extract_extras(message)
            try:
                for index, bubble in enumerate(bubbles):
                    delay = delays[index] if index < len(delays) else 0.5
                    await asyncio.sleep(delay)
                    components: list[Any] = []
                    if index == 0 and reply_comp is not None:
                        components.append(reply_comp)
                    if bubble:
                        components.append(Plain(text=bubble))
                    if index == len(bubbles) - 1:
                        components.extend(extras)
                    if not components:
                        continue
                    await original_send(self._make_chain(components))
                if plan.typo_fix:
                    await asyncio.sleep(1.2)
                    await original_send(self._make_chain([Plain(text=plan.typo_fix)]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[congling] 逐条发送失败，退回整条发送: {exc!r}")
                try:
                    await original_send(message)
                except Exception:
                    pass

        try:
            event.send = wrapped_send  # type: ignore[assignment]
            setattr(event, PATCH_FLAG, True)
            self._patched_events.add(id(event))
            # 兜底：框架若根本没调用 event.send（例如走工具直发），8 秒后自己发出去，
            # 宁可少一次分句，也不能吞消息。
            # 注意这里没有框架传来的 message，得自己按计划拼一条完整内容，
            # 之前这里直接引用了 wrapped_send 的形参名，在外面根本不存在，必挂。
            fallback = self._make_chain([Plain(text=joined)]) if joined else None
            if fallback is not None:
                try:
                    asyncio.get_running_loop().create_task(
                        self._watchdog(state, original_send, fallback)
                    )
                except RuntimeError:
                    pass
            return True
        except Exception as exc:
            logger.warning(f"[congling] 接管发送失败: {exc!r}")
            return False

    @staticmethod
    async def _watchdog(state: dict[str, Any], original_send: Any, message: Any) -> None:
        await asyncio.sleep(8)
        if state.get("done"):
            return
        state["done"] = True
        try:
            await original_send(message)
        except Exception as exc:
            logger.debug(f"[congling] 兜底发送失败: {exc!r}")

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _make_chain(components: list[Any]) -> Any:
        from astrbot.core.message.message_event_result import MessageChain

        try:
            return MessageChain(components=components)
        except TypeError:
            chain = MessageChain()
            for comp in components:
                chain.chain.append(comp)
            return chain

    @staticmethod
    def _plain_text(message: Any) -> str:
        if message is None:
            return ""
        chain = getattr(message, "chain", None) or getattr(message, "components", None) or []
        parts = []
        for comp in chain:
            if isinstance(comp, Plain):
                parts.append(getattr(comp, "text", "") or "")
        return "".join(parts).strip()

    @staticmethod
    def _covered(text: str, joined: str) -> bool:
        """判断这段文本是不是我们已经发过的内容（框架分段残留）。"""
        if not text or not joined:
            return False
        strip = lambda value: "".join(ch for ch in value if ch.isalnum())  # noqa: E731
        a, b = strip(text), strip(joined)
        if not a:
            return False
        if a in b:
            return True
        overlap = len(set(a) & set(b)) / max(1, len(set(a)))
        return overlap >= 0.85

    @staticmethod
    def _extract_reply(message: Any) -> Any:
        if message is None:
            return None
        chain = getattr(message, "chain", None) or getattr(message, "components", None) or []
        for comp in chain:
            if isinstance(comp, Reply):
                return comp
        return None

    @staticmethod
    def _extract_extras(message: Any) -> list[Any]:
        """非文本组件（图片、语音、文件等）挂到最后一条气泡上。"""
        if message is None:
            return []
        chain = getattr(message, "chain", None) or getattr(message, "components", None) or []
        extras: list[Any] = []
        for comp in chain:
            if isinstance(comp, (Plain, Reply)):
                continue
            extras.append(comp)
        return extras

    def release(self, event: Any) -> None:
        self._patched_events.discard(id(event))
