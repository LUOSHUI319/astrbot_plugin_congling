"""分块阅读结果落盘；摘要/清单/有界原句和指纹，不复制全文。"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

PREFIX = "night.rollup.cache:"
VERSION = 3
TTL_SECONDS = 7 * 24 * 60 * 60
MAX_ENTRIES = 256


def digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def provider_fingerprint(models: Any) -> str:
    """只取稳定身份字段；取不到时禁用复用，避免把不同模型视为同一个。"""
    try:
        provider = models.provider_of("chat")
        if provider is None:
            return ""
        config = getattr(provider, "provider_config", None)
        config = config if isinstance(config, dict) else {}
        meta_fn = getattr(provider, "meta", None)
        meta = meta_fn() if callable(meta_fn) else None
        provider_id = config.get("id") or getattr(meta, "id", None)
        model_fn = getattr(provider, "get_model", None)
        model = model_fn() if callable(model_fn) else config.get("model")
        if not isinstance(provider_id, str) or not provider_id:
            return ""
        if not isinstance(model, str) or not model:
            return ""
        # 不读取密钥；端点仅进入哈希，便于同 ID 改地址后失效。
        return digest({
            "type": f"{type(provider).__module__}.{type(provider).__qualname__}",
            "id": provider_id, "model": model,
            "endpoint": config.get("api_base") or config.get("base_url") or "",
        })
    except Exception:
        return ""


class RollupCache:
    def __init__(self, store: Any) -> None:
        self.store = store

    async def get(self, key: str) -> dict[str, Any] | None:
        entry = await self.store.kv_get(PREFIX + key)
        try:
            if (not isinstance(entry, dict) or entry.get("version") != VERSION
                    or not 0 <= time.time() - entry["created_at"] < TTL_SECONDS):
                return None
            payload = entry["payload"]
            if not isinstance(payload, dict) or digest(payload) != entry["checksum"]:
                return None
            return payload
        except (KeyError, TypeError, ValueError):
            return None

    async def put(self, entries: dict[str, dict[str, Any]]) -> None:
        now = int(time.time())
        rows = [
            (PREFIX + key, json.dumps({
                "version": VERSION, "created_at": now,
                "payload": payload, "checksum": digest(payload),
            }, ensure_ascii=False), now)
            for key, payload in entries.items()
        ]

        def save(conn):
            conn.executemany(
                "INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at", rows)
            conn.execute("DELETE FROM kv WHERE k LIKE ? AND updated_at<=?",
                         (PREFIX + "%", now - TTL_SECONDS))
            conn.execute(
                "DELETE FROM kv WHERE k IN (SELECT k FROM kv WHERE k LIKE ? "
                "ORDER BY updated_at DESC,k DESC LIMIT -1 OFFSET ?)",
                (PREFIX + "%", MAX_ENTRIES))

        # 保存失败必须向上抛出，不能假装这块可跨重载复用。
        await self.store.transaction(save)
