"""媒体理解：图片、语音、视频、文件。

原则：全部是「尽力而为」。没配视觉模型就当没看见图片，没配 ASR 就说没听清，
绝不让一次解析失败影响整条回复。
"""

from __future__ import annotations

import hashlib
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

    # ------------------------------------------------------------ 图片
    async def describe_image(self, url: str, source: str = "") -> str:
        if not self.config.bool("perception.image_describe", True):
            return "[图片]"
        url = str(url or "").strip()
        if not url:
            return "[图片]"
        cache_key = "img:" + hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:20]
        try:
            cached = await self.store.kv_get(cache_key)
            if isinstance(cached, str) and cached:
                return cached
        except Exception:
            pass

        text = ""
        try:
            text = await self.hub.describe_image(url, IMAGE_PROMPT)
        except Exception as exc:
            logger.debug(f"[congling] 图片理解失败: {exc!r}")
            text = ""

        if not text:
            return "[图片]"
        text = text.replace("\n", " ")[:200]
        result = f"[图片：{text}]"
        try:
            await self.store.kv_set(cache_key, result)
        except Exception:
            pass
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
