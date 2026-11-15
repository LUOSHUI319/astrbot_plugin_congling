"""表情包桥：从「表情包小偷」的库里按情绪取一张图。

**为什么不直接调它的 `search_meme`：** 那是 LLM tool，签名是
`search_meme(event: AstrMessageEvent, query: str)`，是 async generator，
还拿 event 做作用域判断。自动发说说的时候没有 event，调不动。

**所以这里只做一件事**：读它的库，按情绪词挑一张，返回文件路径。
从绫要的是「一张图」，不是「发出去」——发送链路是它自己的事。

数据源两条，按顺序降级：

1. 插件实例 → `star_cls.db_service.get_index_cache_readonly()`（它的公开读法）
2. 直接只读打开 `plugin_data/astrbot_plugin_stealer/cache/emoji.db`

全部尽力而为：没装、读不到、表结构变了，一律返回 None，上层走纯文字。
配图是加分项，不能让它把整条说说卡死——这是 `core/mmx.py` 定下的规矩。
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
import time
from typing import Any

from astrbot.api import logger

from ..core import paths

PLUGIN_NAME = "astrbot_plugin_stealer"
DB_RELATIVE = ("cache", "emoji.db")
# 最近用过的图别再出现，免得连着两条说说配同一张
RECENT_KEY = "qzone:sticker_recent"
RECENT_KEEP = 24


class StickerBridge:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.context = plugin.context
        self.store = plugin.store
        self._last_error = ""
        self._load_error = ""
        self._cache: list[dict[str, Any]] | None = None
        self._cache_at = 0.0

    # ------------------------------------------------------------ 发现
    def available(self) -> bool:
        """能不能拿到图。只认「库里真有东西」，光有文件不算。"""
        try:
            return bool(self._load())
        except Exception:
            return False

    def _candidates(self) -> list[Any]:
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

    def _locate(self) -> tuple[Any, str]:
        """(库文件路径, 怎么找到的)。via: instance / db_file / ''"""
        from pathlib import Path

        for meta in self._candidates():
            owner = getattr(meta, "star_cls", None)
            db = getattr(owner, "db_service", None)
            raw = getattr(db, "db_path", None)
            if raw:
                path = Path(str(raw))
                if path.exists():
                    return path, "instance"
        # 约定路径：plugin_data/astrbot_plugin_stealer/cache/emoji.db
        try:
            base = Path(paths.data_dir())
            sibling = base.parent / PLUGIN_NAME
            for part in DB_RELATIVE:
                sibling = sibling / part
            if sibling.exists():
                return sibling, "db_file"
        except Exception:
            pass
        return None, ""

    def _db_path(self) -> Any:
        return self._locate()[0]

    # ------------------------------------------------------------ 读库
    def _load(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self._cache is not None and not refresh and (time.time() - self._cache_at) < 600:
            return self._cache
        path = self._db_path()
        if path is None:
            self._last_error = "没找到表情包库（表情包小偷没装或没启用）"
            self._load_error = ""
            self._cache = []
            self._cache_at = time.time()
            return self._cache
        rows: list[dict[str, Any]] = []
        self._load_error = ""
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3.0)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT path, category, desc, overlay_text, emotions_json, character, "
                    "is_favorite, use_count, last_used_at FROM emoji"
                )
                for row in cur:
                    rows.append(dict(row))
            finally:
                conn.close()
        except Exception as exc:
            self._last_error = f"读表情包库失败: {exc!r}"[:160]
            self._load_error = self._last_error
            logger.debug(f"[congling] {self._last_error}")
        self._cache = rows
        self._cache_at = time.time()
        return rows

    def categories(self) -> list[str]:
        """面板用：库里有哪些分类。"""
        seen = {str(r.get("category") or "").strip() for r in self._load()}
        return sorted(x for x in seen if x)

    def snapshot(self) -> dict[str, Any]:
        rows = self._load()
        return {
            "available": self._db_path() is not None,
            "count": len(rows),
            "categories": len(self.categories()),
            "error": self._last_error,
        }

    # ------------------------------------------------------------ 扫描
    async def scan(self, *, force: bool = True) -> dict[str, Any]:
        """重新探测一遍，把「能不能用、为什么不能用」说清楚。面板状态条吃这个。

        为什么把状态拆成六种：它们要修的地方完全不一样。
        `no_plugin` 去装插件，`disabled` 去启用，`no_db` 让她先偷几张图，
        `error` 才轮到看日志。全都显示「不可用」等于什么都没说。
        """
        if force:
            self._cache = None
            self._cache_at = 0.0
            self._load_error = ""

        result: dict[str, Any] = {
            "state": "error",
            "connected": False,
            "via": "",
            "count": 0,
            "categories": 0,
            "db_path": "",
            "plugin_present": False,
            "plugin_activated": False,
            "error": "",
            "scanned_at": int(time.time()),
        }

        metas = self._candidates()
        result["plugin_present"] = bool(metas)
        result["plugin_activated"] = any(
            bool(getattr(meta, "activated", True)) for meta in metas
        )

        path, via = self._locate()
        result["via"] = via
        result["db_path"] = str(path or "")

        if path is not None:
            rows = await asyncio.to_thread(self._load, True)
            result["count"] = len(rows)
            result["categories"] = len(
                {str(r.get("category") or "") for r in rows if str(r.get("category") or "")}
            )
            if self._load_error:
                result["state"] = "error"
                result["error"] = self._load_error
                return result
            if rows:
                result["state"] = "ok"
                result["connected"] = True
                return result
            # 文件在、能读，但一张图都没有
            result["state"] = "empty"
            result["connected"] = True
            result["error"] = "库里没有图，先让它偷几张"
            return result

        if not result["plugin_present"]:
            result["state"] = "no_plugin"
            result["error"] = "没装 astrbot_plugin_stealer"
        elif not result["plugin_activated"]:
            result["state"] = "disabled"
            result["error"] = "表情包小偷装了但被停用"
        else:
            result["state"] = "no_db"
            result["error"] = "表情包小偷在跑，但库文件还没生成"
        return result

    @staticmethod
    def describe(state: dict[str, Any]) -> str:
        """给人看的一句话。面板和 /绫 状态 共用，别两头各写一套文案。"""
        key = str(state.get("state") or "")
        count = int(state.get("count") or 0)
        cats = int(state.get("categories") or 0)
        if key == "ok":
            return f"表情包库 · 已连接（{count} 张 / {cats} 类）"
        if key == "empty":
            return "表情包库 · 空库（还没偷到图）"
        if key == "no_plugin":
            return "表情包库 · 未连接（没装表情包小偷）"
        if key == "disabled":
            return "表情包库 · 未连接（表情包小偷已停用）"
        if key == "no_db":
            return "表情包库 · 未连接（库文件还没生成）"
        return f"表情包库 · 读取失败（{state.get('error') or '原因不明'}）"

    # ------------------------------------------------------------ 打分
    @staticmethod
    def _score(row: dict[str, Any], moods: list[str], now: float) -> float:
        category = str(row.get("category") or "").lower()
        desc = str(row.get("desc") or "")
        overlay = str(row.get("overlay_text") or "")
        emotions = str(row.get("emotions_json") or "").lower()
        character = str(row.get("character") or "").lower()

        score = 0.0
        for mood in moods:
            word = str(mood or "").strip()
            if not word:
                continue
            lower = word.lower()
            if lower and lower == category:
                score += 8.0
            elif lower and lower in category:
                score += 5.0
            if word in desc:
                score += 4.0
            if word in overlay:
                score += 3.0
            if lower and lower in emotions:
                score += 2.5
            if lower and lower in character:
                score += 1.0
        if score <= 0:
            return 0.0

        if row.get("is_favorite"):
            score += 1.5
        # 用得多的压一压，很久没用的抬一抬——别老是那几张脸
        score -= min(2.0, float(row.get("use_count") or 0) / 20.0)
        last = float(row.get("last_used_at") or 0)
        if last and (now - last) > 7 * 86400:
            score += 1.0
        return score

    async def _recent(self) -> set[str]:
        try:
            rows = await self.store.kv_get(RECENT_KEY, []) or []
        except Exception:
            return set()
        return {str(x) for x in rows if str(x).strip()}

    async def _remember(self, picked: str) -> None:
        try:
            rows = list(await self.store.kv_get(RECENT_KEY, []) or [])
        except Exception:
            rows = []
        rows.append(str(picked))
        try:
            await self.store.kv_set(RECENT_KEY, rows[-RECENT_KEEP:])
        except Exception:
            pass

    # ------------------------------------------------------------ 取图
    async def pick(self, moods: Any, *, limit: int = 8) -> dict[str, Any] | None:
        """按情绪词挑一张。挑不到返回 None，调用方走纯文字。

        moods 可以是一个字符串，也可以是若干关键词。
        """
        if isinstance(moods, str):
            words = [moods]
        else:
            words = [str(m) for m in (moods or [])]
        words = [w.strip() for w in words if str(w).strip()]
        if not words:
            return None

        rows = await asyncio.to_thread(self._load)
        if not rows:
            return None

        recent = await self._recent()
        now = time.time()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            picked_path = str(row.get("path") or "")
            if not picked_path or picked_path in recent:
                continue
            score = self._score(row, words, now)
            if score > 0:
                scored.append((score, row))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        top = [row for _, row in scored[: max(1, min(int(limit), len(scored)))]]
        # 库里登记了、文件却没了是真会发生的（手动清过目录、盘挪过）。
        # 挑中的那张验一下存在，不存在就换——最多试 5 次，别为了配一张图去 stat 两千行。
        from pathlib import Path

        for _ in range(5):
            if not top:
                break
            # 不总挑最高的：前三里随机，避免同一个情绪词永远同一张
            chosen = random.choice(top[:3] if len(top) >= 3 else top)
            path = str(chosen.get("path") or "")
            top = [row for row in top if str(row.get("path") or "") != path]
            if not path or not Path(path).exists():
                continue
            await self._remember(path)
            return {
                "path": path,
                "category": str(chosen.get("category") or ""),
                "desc": str(chosen.get("desc") or "")[:80],
                "overlay": str(chosen.get("overlay_text") or "")[:40],
            }
        return None

    async def pick_bytes(self, moods: Any) -> tuple[bytes, str] | None:
        """直接给字节，省得上层再读一遍文件。"""
        item = await self.pick(moods)
        if not item:
            return None
        from pathlib import Path

        path = Path(str(item.get("path") or ""))
        try:
            data = path.read_bytes()
        except Exception as exc:
            logger.debug(f"[congling] 读表情包文件失败: {exc!r}")
            return None
        if not data or len(data) > 8 * 1024 * 1024:
            return None
        return data, path.suffix.lower() or ".jpg"
