"""MiniMax 多模态工具桥接。

本机如果装着 astrbot_plugin_mmx_cli_tool，它已经维护好 key、限流、文件白名单，
我们没必要再让用户填一遍 API。这里按它的「插件协作 API v1」拿门面：

    meta = context.get_registered_star("astrbot_plugin_mmx_cli_tool")
    service = meta.star_cls.get_service(api_version=1)

只用三个方法：describe_image / transcribe / synthesize_speech。
全部尽力而为——工具没装、没 key、调用报错，一律返回空，让上层回落到别的来源。
"""

from __future__ import annotations

import base64
import os
import tempfile
import time
from typing import Any

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_mmx_cli_tool"
API_VERSION = 1


class MmxBridge:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.context = plugin.context
        self._service: Any = None
        self._last_error = ""

    # ------------------------------------------------------------ 发现
    def _candidates(self) -> list[Any]:
        """把可能是 mmx 的插件元数据都找出来。

        不只用 get_registered_star（它按 name 精确匹配，某些加载路径下
        name 和目录名不一致就找不到），把 get_all_stars 也扫一遍，两个字段都认。
        """
        found: list[Any] = []
        seen: set[int] = set()

        def add(meta: Any) -> None:
            if meta is None or id(meta) in seen:
                return
            seen.add(id(meta))
            found.append(meta)

        try:
            add(self.context.get_registered_star(PLUGIN_NAME))
        except Exception:
            pass

        try:
            for meta in self.context.get_all_stars() or []:
                names = {
                    str(getattr(meta, "name", "") or ""),
                    str(getattr(meta, "root_dir_name", "") or ""),
                }
                if PLUGIN_NAME in names:
                    add(meta)
        except Exception:
            pass
        return found

    def service(self, refresh: bool = False) -> Any:
        if self._service is not None and not refresh:
            try:
                state = str(self._service.get_status().get("state") or "")
                if state not in {"closing", "closed"}:
                    return self._service
            except Exception:
                pass
            self._service = None

        steps: list[str] = []
        candidates = self._candidates()
        if not candidates:
            self._last_error = "没找到 astrbot_plugin_mmx_cli_tool，确认插件装了并启用"
            return None

        for meta in candidates:
            name = str(getattr(meta, "name", "") or getattr(meta, "root_dir_name", "") or "?")
            if not getattr(meta, "activated", False):
                steps.append(f"{name} 已停用")
                continue

            owner = getattr(meta, "star_cls", None)
            if owner is None:
                steps.append(f"{name} 没有实例（可能加载失败）")
                continue

            getter = getattr(owner, "get_service", None)
            if not callable(getter):
                steps.append(f"{name} 不支持协作 API（没有 get_service）")
                continue

            try:
                service = getter(api_version=API_VERSION)
            except TypeError as exc:
                # 典型情况：star_cls 拿到的是类而不是实例，方法没绑定 self
                steps.append(f"{name} get_service 绑定失败: {exc!r}")
                continue
            except Exception as exc:
                steps.append(f"{name} 取服务抛错: {exc!r}")
                continue

            if service is None:
                steps.append(f"{name} 返回了空服务")
                continue

            self._service = service
            self._last_error = ""
            return service

        self._last_error = "；".join(steps) or "找到了插件但拿不到服务"
        return None

    def available(self) -> bool:
        svc = self.service()
        if svc is None:
            return False
        try:
            status = svc.get_status()
        except Exception:
            return False
        return bool(status.get("ready")) or str(status.get("state") or "") == "ready"

    def status(self, refresh: bool = False) -> dict[str, Any]:
        svc = self.service(refresh=refresh)
        if svc is None:
            return {"available": False, "reason": self._last_error or "不可用"}
        try:
            data = dict(svc.get_status())
        except Exception as exc:
            return {"available": False, "reason": f"读取状态失败: {exc!r}"}
        try:
            features = (svc.capabilities() or {}).get("features") or []
        except Exception:
            features = []
        return {
            "available": bool(data.get("ready")) or str(data.get("state") or "") == "ready",
            "state": data.get("state", ""),
            "reason": data.get("reason", ""),
            "instance_id": data.get("instance_id", ""),
            "features": features,
        }

    # ------------------------------------------------------------ 图片
    async def describe(self, image: str, prompt: str) -> str:
        svc = self.service()
        if svc is None or not image:
            return ""
        try:
            result = await svc.describe_image(image=image, prompt=prompt)
        except Exception as exc:
            self._last_error = f"describe_image 失败: {exc!r}"
            logger.debug(f"[congling] mmx 图片理解失败: {exc!r}")
            return ""
        return _extract_text(result)

    # ------------------------------------------------------------ 语音识别
    async def transcribe(self, target: str, language: str = "zh") -> str:
        svc = self.service()
        if svc is None or not target:
            return ""

        # mmx 的 file 参数只收插件数据目录 / AstrBot 临时目录里的本地文件，
        # 不收远端 URL，所以先把语音落到 temp 再喂过去
        temp_path = ""
        try:
            local = target
            if str(target).startswith(("http://", "https://")):
                temp_path = await _download_to_temp(target)
                if not temp_path:
                    return ""
                local = temp_path
            result = await svc.transcribe(local, language=language or None, response_format="json")
        except Exception as exc:
            self._last_error = f"transcribe 失败: {exc!r}"
            logger.debug(f"[congling] mmx 语音转写失败: {exc!r}")
            return ""
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        return _extract_text(result)

    # ------------------------------------------------------------ 语音合成
    async def synthesize(self, text: str, voice: str = "") -> bytes | None:
        svc = self.service()
        if svc is None or not text:
            return None
        options: dict[str, Any] = {"audio_format": "wav"}
        if voice:
            options["voice"] = voice
        try:
            result = await svc.synthesize_speech(text, **options)
        except Exception as exc:
            self._last_error = f"synthesize_speech 失败: {exc!r}"
            logger.debug(f"[congling] mmx 语音合成失败: {exc!r}")
            return None
        return _extract_audio(result)


# ================================================================ 解析
def _extract_text(result: Any) -> str:
    """从各种返回结构里把文本挖出来。"""
    if result is None:
        return ""
    if isinstance(result, str):
        return result.strip()
    if not isinstance(result, dict):
        return ""

    # transcribe: {ok, data: {text}}
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("text", "content", "transcription"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # describe_image 走 VLM：choices[0].message.content
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
    if isinstance(data, str) and data.strip():
        return data.strip()

    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()

    for key in ("text", "reply", "content", "transcription", "output"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_audio(result: Any) -> bytes | None:
    """MiniMax TTS 返回 {data: {audio: "<hex>"}}，也兼容 base64 和直给字节。"""
    if isinstance(result, (bytes, bytearray)):
        return bytes(result)
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    blob = None
    if isinstance(data, dict):
        blob = data.get("audio") or data.get("audio_hex") or data.get("audio_base64")
    if blob is None:
        blob = result.get("audio")
    if isinstance(blob, (bytes, bytearray)):
        return bytes(blob)
    if not isinstance(blob, str) or not blob.strip():
        return None
    text = blob.strip()
    try:
        return bytes.fromhex(text)
    except ValueError:
        pass
    try:
        return base64.b64decode(text)
    except Exception:
        return None


async def _download_to_temp(url: str) -> str:
    """把远端音频拉到 AstrBot 临时目录，返回本地路径。"""
    try:
        import httpx
    except Exception:
        return ""
    target_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "..", "temp")
    target_dir = os.path.abspath(target_dir)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError:
        target_dir = tempfile.gettempdir()
    path = os.path.join(target_dir, f"congling_voice_{int(time.time() * 1000)}.amr")
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            with open(path, "wb") as fh:
                fh.write(resp.content)
        return path
    except Exception as exc:
        logger.debug(f"[congling] 音频下载失败: {exc!r}")
        try:
            os.remove(path)
        except OSError:
            pass
        return ""
