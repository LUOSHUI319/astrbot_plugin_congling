"""模型调用中心。

不再让用户选模型。规则只有一条：

    先用 AstrBot 自己的 provider，调不动就走 MiniMax 多模态工具

「调不动」包括三种：没有这个能力的 provider、provider 调用抛异常、返回空。
所以视觉、语音识别、语音合成这三类只要 mmx 工具在，就一定有兜底；
对话和向量 mmx 没有，provider 不行就直接返回空，由上层降级。

状态可以随时刷新，页面上的「刷新」按钮就是干这个的。
"""

from __future__ import annotations

import copy
import contextvars
import functools
import time
from typing import Any

from astrbot.api import logger

CAPABILITIES = ("chat", "vision", "stt", "tts", "embedding")

# 能力 → 插件里用到它的地方
USAGE = {
    "chat": "主回复、记忆抽取、反思",
    "vision": "看图、表情包内容",
    "stt": "语音条转文字",
    "tts": "发语音条",
    "embedding": "记忆语义召回",
}

# mmx 工具能顶替的能力
MMX_CAPS = {"vision", "stt", "tts"}


def _max_tokens_scope(provider: Any) -> Any:
    """原生兼容提供商丢弃 kwargs 时，在 SDK 边界落实本次输出限制。"""
    resource = getattr(getattr(getattr(provider, "client", None), "chat", None), "completions", None)
    original = getattr(resource, "create", None)
    if not callable(original):
        return None  # API 路由等提供商直接处理 text_chat 的 max_tokens。
    existing = getattr(original, "_congling_max_tokens_scope", None)
    if existing is not None:
        return existing
    scope = contextvars.ContextVar("congling_max_tokens", default=None)

    @functools.wraps(original)
    async def create(*args: Any, **kwargs: Any) -> Any:
        limit = scope.get()
        if limit is not None:
            extra_body = dict(kwargs.get("extra_body") or {})
            field = "max_completion_tokens" if (
                "max_completion_tokens" in kwargs or "max_completion_tokens" in extra_body
            ) else "max_tokens"
            # 配置中的同名字段不能覆盖本次限制；只修改请求副本，不改提供商配置。
            for name in ("max_tokens", "max_completion_tokens"):
                kwargs.pop(name, None)
                extra_body.pop(name, None)
            kwargs[field] = limit
            if "extra_body" in kwargs:
                kwargs["extra_body"] = extra_body
        return await original(*args, **kwargs)

    # 保存 scope 而非 ModelHub 实例，热重载可复用；无本次限制时原样透传。
    create._congling_max_tokens_scope = scope
    resource.create = create
    return scope


class ModelHub:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.context = plugin.context
        self._bridge: Any = None
        self._checked_at = 0.0
        self._mmx_cache: dict[str, Any] = {}

    # ================================================================ provider
    def provider_of(self, capability: str) -> Any:
        getter_map = {
            "chat": "get_using_provider",
            "vision": "get_using_provider",
            "stt": "get_using_stt_provider",
            "tts": "get_using_tts_provider",
            "embedding": "get_using_embedding_provider",
        }
        # 图片理解本来就用对话模型，没有单独的视觉 provider
        target = "chat" if capability == "vision" else capability
        getter = getattr(self.context, getter_map.get(target, ""), None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception as exc:
            logger.debug(f"[congling] 取默认 {capability} provider 失败: {exc!r}")
            return None

    def provider_ready(self, capability: str) -> bool:
        return self.provider_of(capability) is not None

    # ================================================================ mmx
    @property
    def bridge(self) -> Any:
        if self._bridge is None:
            from .mmx import MmxBridge

            self._bridge = MmxBridge(self.plugin)
        return self._bridge

    def mmx_status(self, refresh: bool = False) -> dict[str, Any]:
        now = time.time()
        if not refresh and self._mmx_cache and (now - self._checked_at) < 30:
            return self._mmx_cache
        try:
            data = self.bridge.status(refresh=refresh)
        except Exception as exc:
            data = {"available": False, "reason": f"{exc!r}"[:160]}
        self._mmx_cache = data
        self._checked_at = now
        return data

    def mmx_available(self, refresh: bool = False) -> bool:
        return bool(self.mmx_status(refresh=refresh).get("available"))

    def refresh(self) -> dict[str, Any]:
        """把 mmx 状态重新读一遍，页面刷新按钮调这个。"""
        return self.mmx_status(refresh=True)

    # ================================================================ 路由
    def route(self, capability: str) -> str:
        """这个能力最终由谁承担：provider / mmx / none。"""
        if self.provider_ready(capability):
            return "provider"
        if capability in MMX_CAPS and self.mmx_available():
            return "mmx"
        return "none"

    # ================================================================ 对话
    async def chat(self, prompt: str, system: str = "", temperature: float = 0.7, max_tokens: int = 1024) -> str:
        provider = self.provider_of("chat")
        if provider is None:
            return ""
        scope = None
        token = None
        try:
            if type(max_tokens) is not int or max_tokens <= 0:
                raise ValueError("max_tokens must be a positive integer")
            scope = _max_tokens_scope(provider)
            if scope is not None:
                token = scope.set(max_tokens)
            resp = await provider.text_chat(
                prompt=prompt, contexts=[], system_prompt=system or None, max_tokens=max_tokens,
            )
            return str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.debug(f"[congling] 对话调用失败: {exc!r}")
            return ""
        finally:
            if scope is not None and token is not None:
                scope.reset(token)

    # ================================================================ 图片
    async def character_chat(self, prompt: str, system: str = "", *, umo: str = "",
                             identity: dict[str, Any] | None = None,
                             temperature: float = 0.7, max_tokens: int = 1024) -> str:
        """角色发言使用当前唯一人格和目标会话模型；抽取任务仍使用 chat。"""
        persona = self.plugin.persona
        current = await persona.resolve(umo)
        if identity is not None and identity["token"] != current["token"]:
            raise ValueError("人格已切换，旧角色文本作废")
        identity = identity or current
        getter = getattr(self.context, "get_using_provider_async", None)
        provider = await getter(umo or None) if callable(getter) else self.provider_of("chat")
        if provider is None:
            return ""
        system_prompt = identity["prompt"]
        if system:
            system_prompt += "\n\n" + system
        response = await provider.text_chat(
            prompt=prompt, contexts=copy.deepcopy(identity["dialogs"]),
            system_prompt=system_prompt or None,
        )
        if (await persona.resolve(umo))["token"] != identity["token"]:
            raise ValueError("人格已切换，旧角色文本作废")
        return str(getattr(response, "completion_text", "") or "").strip()

    async def describe_image(self, image: str, prompt: str) -> str:
        # 公共版本只使用模型中心中的 vision 配置，不绑定部署实例的 provider ID。
        provider = self.provider_of("vision")
        if provider is not None:
            try:
                resp = await provider.text_chat(prompt=prompt, image_urls=[image], contexts=[])
                text = str(getattr(resp, "completion_text", "") or "").strip()
                if text:
                    return text
            except Exception as exc:
                logger.debug(f"[congling] 图片理解 provider 失败，转 mmx: {exc!r}")

        if self.mmx_available():
            text = await self.bridge.describe(image, prompt)
            if text:
                return text
        return ""

    # ================================================================ 语音识别
    async def transcribe(self, target: str) -> str:
        provider = self.provider_of("stt")
        if provider is not None:
            try:
                text = str(await provider.get_text(target) or "").strip()
                if text:
                    return text
            except Exception as exc:
                logger.debug(f"[congling] 语音识别 provider 失败，转 mmx: {exc!r}")

        if self.mmx_available():
            text = await self.bridge.transcribe(target)
            if text:
                return text
        return ""

    # ================================================================ 语音合成
    async def synthesize(self, text: str, voice: str = "") -> bytes | None:
        provider = self.provider_of("tts")
        if provider is not None:
            try:
                data = await provider.get_audio(text)
                if isinstance(data, (bytes, bytearray)) and data:
                    return bytes(data)
            except Exception as exc:
                logger.debug(f"[congling] 语音合成 provider 失败，转 mmx: {exc!r}")

        if self.mmx_available():
            audio = await self.bridge.synthesize(text, voice)
            if audio:
                return audio
        return None

    # ================================================================ 向量
    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        provider = self.provider_of("embedding")
        if provider is None:
            return []
        try:
            result = await provider.get_embedding(texts)
            return [list(vec) for vec in (result or [])]
        except Exception as exc:
            logger.debug(f"[congling] 向量调用失败: {exc!r}")
            return []
