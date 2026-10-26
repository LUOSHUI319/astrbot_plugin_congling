"""配置版本升级。

为什么要这一层：AstrBot 的配置一旦落盘，就再也不会跟着 schema 里改过的
默认值走。0.5.0 把「说说写入权限」这类开关默认打开了，老用户不迁移的话
永远停在 False，等于白改。

规矩：只升级「还停在旧默认值」的项。用户手动改过的一律不碰——他关掉写入
权限一定有他的理由，不能替他把权限打开。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from astrbot.api import logger

CONFIG_VERSION = "0.5.0"

# 旧默认 → 新默认。只有当前值仍等于旧默认时才升级。
UPGRADES: dict[str, tuple[Any, Any]] = {
    "qq_extra.qzone_allow_write": (False, True),
    "qq_extra.qzone_auto_post": (False, True),
    "qq_extra.signature_auto_update": (False, True),
}

MESSAGES = {
    "qq_extra.qzone_allow_write": "说说写入权限已默认打开",
    "qq_extra.qzone_auto_post": "每日自动说说已默认打开（1~3 条）",
    "qq_extra.signature_auto_update": "签名跟随心情已默认打开",
}


def _raw_get(raw: Any, path: str) -> tuple[bool, Any]:
    """在用户原始配置里找路径，返回 (是否存在, 值)。"""
    node: Any = raw
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _persist(plugin: Any, path: str, value: Any) -> bool:
    """尽量写回 AstrBot 配置文件；写不了就只留在本次运行里。"""
    raw = getattr(plugin, "raw_config", None)
    if not isinstance(raw, dict):
        return False
    parts = path.split(".")
    node: Any = raw
    try:
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value
        save = getattr(raw, "save_config", None)
        if callable(save):
            save()
            return True
    except Exception as exc:
        logger.debug(f"[congling] 配置回写失败 {path}: {exc!r}")
    return False


async def run(plugin: Any) -> dict[str, Any]:
    """执行一次配置升级。可重复调用，跑过的直接跳过。"""
    store = getattr(plugin, "store", None)
    if store is None:
        return {"applied": False, "reason": "no_store"}

    try:
        done = await store.kv_get("config_version", "")
    except Exception:
        done = ""

    if str(done) == CONFIG_VERSION:
        return {"applied": False, "reason": "already", "version": CONFIG_VERSION}

    raw = getattr(plugin, "raw_config", None)
    changed: list[str] = []
    persisted = 0

    for path, (legacy, modern) in UPGRADES.items():
        exists, current = _raw_get(raw, path)
        # 配置里压根没这个键 → 直接用新默认（第一次装的人本来就该是新默认）
        target_ok = (not exists) or current == legacy
        if not target_ok:
            continue
        try:
            plugin.cfg.override(path, modern)
        except Exception:
            continue
        changed.append(path)
        if _persist(plugin, path, modern):
            persisted += 1

    try:
        await store.kv_set("config_version", CONFIG_VERSION)
    except Exception:
        pass

    if changed:
        notes = "；".join(MESSAGES.get(p, p) for p in changed)
        logger.info(f"[congling] 配置已升级到 {CONFIG_VERSION}：{notes}")
    else:
        logger.info(f"[congling] 配置版本记为 {CONFIG_VERSION}（无需改动）")

    return {
        "applied": True,
        "version": CONFIG_VERSION,
        "changed": changed,
        "persisted": persisted,
        "notes": [MESSAGES.get(p, p) for p in changed],
    }
