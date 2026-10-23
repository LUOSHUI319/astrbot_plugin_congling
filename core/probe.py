"""能力探测与让位。

为什么要有这个：本机同时装着 private_companion、livingmemory、reply_delay 等
插件，如果本插件不判断就上手，会出现双人格、双延时、记忆双写——这三种问题
在日志里极难排查，只能靠启动时把责任划分清楚。

探测结果只做一次缓存，失败一律视为「不存在」（宁可自己多干，也不要互相打架）。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

# 外部插件模块名 → 能力标签
KNOWN_PLUGINS: dict[str, tuple[str, ...]] = {
    "astrbot_plugin_private_companion": ("humanize", "memory"),
    "astrbot_plugin_livingmemory": ("memory",),
    "astrbot_plugin_memory_companion": ("memory",),
    "astrbot_plugin_lite_memory": ("memory",),
    "astrbot_plugin_reply_delay": ("rhythm",),
    "astrbot_plugin_no_newline": ("guard_newline",),
    "astrbot_plugin_quote_probability": ("guard_quote",),
    "astrbot_plugin_stealer": ("sticker",),
    "astrbot_plugin_qzone_ultra": ("qzone",),
    "astrbot_plugin_mmx_cli_tool": ("mmx",),
    "astrbot_plugin_tts": ("tts",),
}

# 能力 → 让位配置项
YIELD_FLAGS = {
    "humanize": "compat.yield_humanize",
    "memory": "compat.yield_memory",
    "rhythm": "compat.yield_rhythm",
}


class Probe:
    """探测谁在跑，并给出「我该不该让位」。"""

    def __init__(self, context: Any, config: Any) -> None:
        self.context = context
        self.config = config
        self._present: dict[str, dict[str, Any]] | None = None
        self._stamp = 0.0

    # ------------------------------------------------------------ 探测
    def _scan(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        try:
            stars = self.context.get_all_stars()
        except Exception as exc:
            logger.debug(f"[congling] 读取插件列表失败: {exc!r}")
            stars = []
        for meta in stars or []:
            module = str(getattr(meta, "root_dir_name", "") or getattr(meta, "name", "") or "")
            name = str(getattr(meta, "name", "") or "")
            activated = bool(getattr(meta, "activated", True))
            if not activated:
                continue
            for plugin_key, tags in KNOWN_PLUGINS.items():
                if module == plugin_key or name == plugin_key:
                    found[plugin_key] = {
                        "name": name,
                        "module": module,
                        "tags": list(tags),
                        "activated": activated,
                    }
        return found

    def refresh(self, force: bool = False) -> dict[str, dict[str, Any]]:
        if self._present is not None and not force and (time.time() - self._stamp) < 300:
            return self._present
        self._present = self._scan()
        self._stamp = time.time()
        if self._present:
            logger.info(
                "[congling] 探测到外部插件: "
                + "、".join(f"{key}" for key in self._present)
            )
        return self._present

    # ------------------------------------------------------------ 查询
    def present(self, plugin_key: str) -> bool:
        return plugin_key in self.refresh()

    def has_capability(self, tag: str) -> bool:
        for info in self.refresh().values():
            if tag in info.get("tags", ()):
                return True
        return False

    def providers(self, tag: str) -> list[str]:
        return [
            key for key, info in self.refresh().items() if tag in info.get("tags", ())
        ]

    # ------------------------------------------------------------ 让位判定
    def _mode(self) -> str:
        mode = str(self.config.get("compat.mode", "auto") or "auto").lower()
        if mode not in {"auto", "force_local", "force_yield"}:
            return "auto"
        return mode

    def should_yield(self, capability: str) -> bool:
        """capability: humanize / memory / rhythm"""
        mode = self._mode()
        if mode == "force_local":
            return False
        tag = capability
        if mode == "force_yield":
            return True
        flag_path = YIELD_FLAGS.get(tag)
        if flag_path and not self.config.bool(flag_path, True):
            return False
        return self.has_capability(tag)

    def guard_owned_externally(self, rule: str) -> bool:
        """输出守卫的某条规则是否由别的插件负责（避免双重改写）。"""
        if self._mode() == "force_local":
            return False
        return self.has_capability(rule)

    # ------------------------------------------------------------ 汇报
    def matrix(self) -> dict[str, Any]:
        present = self.refresh()
        return {
            "mode": self._mode(),
            "present": {key: info["tags"] for key, info in present.items()},
            "yield": {
                "humanize": self.should_yield("humanize"),
                "memory": self.should_yield("memory"),
                "rhythm": self.should_yield("rhythm"),
            },
            "guard_external": {
                "newline": self.guard_owned_externally("guard_newline"),
                "quote": self.guard_owned_externally("guard_quote"),
            },
            "optional": {
                "sticker": self.has_capability("sticker"),
                "qzone": self.has_capability("qzone"),
                "mmx": self.has_capability("mmx"),
            },
            "scanned_at": int(self._stamp),
        }

    def summary_line(self) -> str:
        data = self.matrix()
        yield_text = "、".join([key for key, value in data["yield"].items() if value]) or "无"
        present_text = "、".join(data["present"].keys()) or "无"
        return f"外部插件：{present_text}；让位：{yield_text}"
