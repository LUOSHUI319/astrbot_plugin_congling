"""媒体理解：图片、语音、视频、文件。

原则：全部是「尽力而为」。没配视觉模型就当没看见图片，没配 ASR 就说没听清，
绝不让一次解析失败影响整条回复。
"""

from __future__ import annotations

import hashlib
import asyncio
import time
from typing import Any

from astrbot.api import logger

IMAGE_PROMPT = (
    "用一两句话说明这张图里有什么。若有文字，把文字原样抄下来；"
    "若是表情包/梗图，说明它的情绪和梗；若是截图，说明是什么界面。"
    "只描述事实，不要评价，不要寒暄。"
)


class MediaInterpreter:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        # 模型调用统一走 ModelHub：它能按配置在「AstrBot 的 provider」和
        # 「插件里自己填的 API」之间切换
        hub = getattr(plugin, "models", None)
        if hub is None:
            from ..core.modelhub import ModelHub

            hub = ModelHub(plugin)
        self.hub = hub
        self._images: dict[str, tuple[float, str]] = {}
        self._image_tasks: dict[str, Any] = {}

    # ------------------------------------------------------------ 图片
    async def describe_image(self, url: str, source: str = "") -> str:
        if not self.config.bool("perception.image_describe", True):
            return "[图片]"
        url = str(url or "").strip()
        if not url:
            return "[图片]"
        # source由事件传入，含会话与用户；不读取旧的无受众、永久图片缓存。
        cache_key = hashlib.sha256((source + "\0" + url + "\0" + IMAGE_PROMPT).encode()).hexdigest()
        now = time.monotonic()
        self._images = {key: value for key, value in self._images.items() if value[0] > now}
        if source and cache_key in self._images:
            return self._images[cache_key][1]
        if source and cache_key in self._image_tasks:
            return await asyncio.shield(self._image_tasks[cache_key])
        if not source:
            return await self._describe_uncached(url, "")
        task = asyncio.create_task(self._describe_uncached(url, cache_key))
        self._image_tasks[cache_key] = task
        task.add_done_callback(lambda _: self._image_tasks.pop(cache_key, None))
        return await asyncio.shield(task)
    # 图片缓存未命中时沿用原有模型入口。
    async def _describe_uncached(self, url: str, cache_key: str) -> str:
        text = ""
        try:
            text = await self.hub.describe_image(url, IMAGE_PROMPT)
        except Exception as exc:
            logger.debug(f"[congling] 图片理解失败: {exc!r}")
            text = ""

        if not text:
            return "[图片]"
        # 截图文字不能在200字处丢失关键报错/对象；仍限制上下文大小。
        text = text.replace("\n", " ")[:1200]
        result = f"[图片：{text}]"
        if cache_key:
            if len(self._images) >= 256:
                self._images.pop(next(iter(self._images)))
            self._images[cache_key] = (time.monotonic() + 3600, result)
        return result

    # ------------------------------------------------------------ 诊断
    async def _trace(self, key: str, payload: dict[str, Any]) -> None:
        try:
            payload = dict(payload)
            payload["at"] = int(time.time())
            await self.store.kv_set(key, payload)
        except Exception:
            pass

    # ------------------------------------------------------------ 语音
    async def transcribe(self, audio: Any, source: str = "") -> str:
        if not self.config.bool("perception.voice_transcribe", True):
            await self._trace("voice:last", {"ok": False, "why": "voice_transcribe 关着"})
            return "[语音]"
        target = self._audio_target(audio)
        if not target:
            await self._trace("voice:last", {"ok": False, "why": "取不到音频地址", "input": str(audio)[:120]})
            return "[语音]"
        try:
            text = await self.hub.transcribe(target)
        except Exception as exc:
            await self._trace(
                "voice:last",
                {
                    "ok": False,
                    "why": f"{type(exc).__name__}: {exc}"[:250],
                    "mode": self.hub.route("stt"),
                    "target": target[:160],
                },
            )
            logger.debug(f"[congling] 语音转写失败: {exc!r}")
            return "[语音]"
        text = str(text or "").strip()
        if not text:
            await self._trace(
                "voice:last",
                {"ok": False, "why": "provider 返回空文本", "mode": self.hub.route("stt"), "target": target[:160]},
            )
            return "[语音]"
        await self._trace(
            "voice:last", {"ok": True, "mode": self.hub.route("stt"), "target": target[:160], "text": text[:100]}
        )
        return f"[语音：{text[:200]}]"

    @staticmethod
    def _audio_target(audio: Any) -> str:
        # service.py 传进来的是字符串 URL，不是组件对象；这里必须直通，
        # 否则会一路取不到值，函数开头就 return "[语音]"，转写永远不生效。
        if isinstance(audio, str):
            return audio.strip()
        for attr in ("file", "url", "path"):
            value = getattr(audio, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if isinstance(audio, dict):
            for key in ("file", "url", "path"):
                value = audio.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    # ------------------------------------------------------------ 视频 / 文件
    @staticmethod
    def video_hint(component: Any) -> str:
        for attr in ("file", "url"):
            value = getattr(component, attr, None)
            if isinstance(value, str) and value:
                return "[视频]"
        return "[视频]"

    @staticmethod
    def file_hint(component: Any) -> str:
        name = str(getattr(component, "name", "") or getattr(component, "file", "") or "").strip()
        size = getattr(component, "file_size", None) or getattr(component, "size", None)
        if name and size:
            try:
                return f"[文件 {name}，{int(size) // 1024}KB]"
            except (TypeError, ValueError):
                return f"[文件 {name}]"
        return f"[文件 {name}]" if name else "[文件]"

    # ------------------------------------------------------------ 批量
    async def enrich(self, message: Any) -> None:
        """把 PerceivedMessage 里的媒体块补全成人话（原地修改）。"""
        from ..core.contracts import Block

        new_blocks: list[Block] = []
        for block in message.blocks:
            if block.kind == "image" and block.text in {"", "[图片]"}:
                block.text = await self.describe_image(str(block.meta.get("url") or ""))
            new_blocks.append(block)
        message.blocks = new_blocks
