"""图片登记表（原名头像库）。

不是文件夹，是一张带信任标记的登记表。核心规矩只有一条：
**只有用户主动存进来的、或用户点头同意过的图，才允许被自动用掉。**

来源分三种：
- user_upload      用户自己发图入库，直接可信
- user_authorized  她扒到的图，用户点头同意后才可信
- bot_pending      她扒到但还没被允许的，只躺着，不参与自动挑

用途分三种（kind）：
- avatar  头像。**只有这一种能进自动换脸**
- scene   生活图，说说配图用
- meme    表情，说说配图用（多从表情包小偷的索引现取，一般不入库）

表名还叫 avatar_pool，是历史包袱。改名要迁移、要改面板、要改一堆 SQL，
收益只是好看，不值。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..core import clock
from ..core import paths

SOURCES = ("user_upload", "user_authorized", "bot_pending")
KINDS = ("avatar", "scene", "meme")
RECENT_DAYS = 14
ALLOWED_SUFFIX = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


class AvatarPool:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store

    # ------------------------------------------------------------ 基础
    def limit(self) -> int:
        try:
            return max(3, min(200, self.config.int("qq_extra.avatar_pool_limit", 30)))
        except (TypeError, ValueError):
            return 30

    def gallery_limit(self) -> int:
        """生活图/表情各自的上限。跟头像分开算，否则图一多头像先被挤掉。"""
        try:
            return max(5, min(300, self.config.int("qq_extra.gallery_limit", 60)))
        except (TypeError, ValueError):
            return 60

    async def get(self, item_id: int) -> dict[str, Any] | None:
        return await self.store.fetch_one(
            "SELECT * FROM qq_avatar_pool WHERE id=?", (int(item_id),)
        )

    async def state(self) -> dict[str, Any]:
        rows = await self.store.fetch_all(
            "SELECT * FROM qq_avatar_pool ORDER BY trusted DESC, id DESC LIMIT 200"
        )
        items: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for row in rows:
            item = self._shape(row)
            if item["trusted"]:
                items.append(item)
            else:
                pending.append(item)
        return {
            "items": items,
            "pending": pending,
            "trusted": len(items),
            "total": len(rows),
            "limit": self.limit(),
        }

    def _shape(self, row: dict[str, Any]) -> dict[str, Any]:
        import json

        item = dict(row)
        item["trusted"] = bool(row.get("trusted"))
        item["kind"] = str(row.get("kind") or "avatar")
        try:
            item["tags"] = json.loads(row.get("tags") or "[]")
        except (ValueError, TypeError):
            item["tags"] = []
        item["added_text"] = clock.fmt(
            "%Y-%m-%d %H:%M", int(row.get("added_at") or 0)
        )
        item["used_text"] = (
            clock.fmt("%Y-%m-%d %H:%M", int(row.get("last_used_at") or 0))
            if row.get("last_used_at")
            else "没用过"
        )
        return item

    # ------------------------------------------------------------ 入库
    async def add_path(
        self,
        path: str,
        *,
        source: str = "user_upload",
        added_by: str = "",
        tags: list[str] | None = None,
        note: str = "",
        kind: str = "avatar",
    ) -> dict[str, Any]:
        file_path = Path(str(path or "").strip())
        if not file_path.exists() or not file_path.is_file():
            return {"ok": False, "reason": "找不到这张图"}
        if file_path.suffix.lower() not in ALLOWED_SUFFIX:
            return {"ok": False, "reason": f"不认 {file_path.suffix} 这种格式"}
        try:
            data = file_path.read_bytes()
        except Exception as exc:
            return {"ok": False, "reason": f"读不了：{exc!r}"[:100]}
        return await self.add_bytes(
            data,
            suffix=file_path.suffix.lower(),
            source=source,
            added_by=added_by,
            tags=tags,
            note=note,
            kind=kind,
        )

    async def add_bytes(
        self,
        data: bytes,
        *,
        suffix: str = ".jpg",
        source: str = "user_upload",
        added_by: str = "",
        tags: list[str] | None = None,
        note: str = "",
        kind: str = "avatar",
    ) -> dict[str, Any]:
        import json

        if not data:
            return {"ok": False, "reason": "空图"}
        if len(data) > 8 * 1024 * 1024:
            return {"ok": False, "reason": "图片超过 8MB，先压一下"}
        if source not in SOURCES:
            source = "bot_pending"
        kind = str(kind or "avatar").strip().lower()
        if kind not in KINDS:
            kind = "avatar"
        digest = hashlib.sha1(data).hexdigest()
        exists = await self.store.fetch_one(
            "SELECT * FROM qq_avatar_pool WHERE persona_id=? AND sha1=?",
            (self.config.persona_id, digest),
        )
        if exists:
            return {
                "ok": True,
                "id": int(exists["id"]),
                "duplicated": True,
                "trusted": bool(exists.get("trusted")),
                "kind": str(exists.get("kind") or "avatar"),
            }

        folder = paths.gallery_dir() if kind != "avatar" else paths.avatar_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{digest[:16]}{suffix if suffix.startswith('.') else '.' + suffix}"
        try:
            target.write_bytes(data)
        except Exception as exc:
            return {"ok": False, "reason": f"存不下：{exc!r}"[:100]}

        trusted = 0 if source == "bot_pending" else 1
        try:
            item_id = await self.store.execute(
                "INSERT INTO qq_avatar_pool(persona_id, path, sha1, source, trusted, added_by, "
                "added_at, last_used_at, use_count, tags, note, kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.config.persona_id,
                    str(target),
                    digest,
                    source,
                    trusted,
                    str(added_by)[:40],
                    int(time.time()),
                    0,
                    0,
                    json.dumps([str(t)[:12] for t in (tags or [])][:6], ensure_ascii=False),
                    str(note)[:120],
                    kind,
                ),
            )
        except Exception as exc:
            return {"ok": False, "reason": f"入库失败：{exc!r}"[:120]}
        await self.prune()
        return {"ok": True, "id": int(item_id or 0), "path": str(target),
                "trusted": bool(trusted), "kind": kind}

    # ------------------------------------------------------------ 维护
    async def trust(self, item_id: int, value: bool) -> bool:
        row = await self.get(item_id)
        if not row:
            return False
        source = str(row.get("source") or "bot_pending")
        if value and source == "bot_pending":
            source = "user_authorized"
        await self.store.execute(
            "UPDATE qq_avatar_pool SET trusted=?, source=? WHERE id=?",
            (1 if value else 0, source, int(item_id)),
        )
        return True

    async def set_tags(self, item_id: int, tags: list[str]) -> bool:
        import json

        if not await self.get(item_id):
            return False
        await self.store.execute(
            "UPDATE qq_avatar_pool SET tags=? WHERE id=?",
            (json.dumps(tags[:6], ensure_ascii=False), int(item_id)),
        )
        return True

    async def remove(self, item_id: int) -> bool:
        row = await self.get(item_id)
        if not row:
            return False
        try:
            Path(str(row.get("path") or "")).unlink(missing_ok=True)
        except Exception:
            pass
        await self.store.execute("DELETE FROM qq_avatar_pool WHERE id=?", (int(item_id),))
        return True

    async def prune(self) -> int:
        """按 kind 分别裁剪：头像、生活图、表情各有各的上限。"""
        removed = await self._prune_kind("avatar", self.limit())
        cap = self.gallery_limit()
        removed += await self._prune_kind("scene", cap)
        removed += await self._prune_kind("meme", cap)
        return removed

    async def _prune_kind(self, kind: str, cap: int) -> int:
        rows = await self.store.fetch_all(
            "SELECT id, path FROM qq_avatar_pool WHERE persona_id=? "
            "AND COALESCE(kind,'avatar')=? "
            "ORDER BY CASE WHEN source='user_upload' THEN 0 ELSE 1 END, "
            "COALESCE(last_used_at,0) ASC, id ASC",
            (self.config.persona_id, kind),
        )
        extra = len(rows) - int(cap)
        if extra <= 0:
            return 0
        removed = 0
        for row in rows[:extra]:
            try:
                Path(str(row.get("path") or "")).unlink(missing_ok=True)
            except Exception:
                pass
            await self.store.execute("DELETE FROM qq_avatar_pool WHERE id=?", (int(row["id"]),))
            removed += 1
        return removed

    # ------------------------------------------------------------ 取图
    async def pick(
        self, *, tags: list[str] | None = None, kind: str = "avatar"
    ) -> dict[str, Any] | None:
        """挑一张可以自动用的。库空了就返回 None——绝不临时去网上找。

        kind 默认 avatar。**换脸只能从头像里挑**，生活图和表情图绝不能顶上，
        否则哪天她拿一张窗外的雨当头像换了。
        """
        kind = str(kind or "avatar").strip().lower()
        if kind not in KINDS:
            kind = "avatar"
        cutoff = int(time.time()) - RECENT_DAYS * 86400
        rows = await self.store.fetch_all(
            "SELECT * FROM qq_avatar_pool WHERE persona_id=? AND trusted=1 "
            "AND COALESCE(kind,'avatar')=? "
            "AND (last_used_at=0 OR last_used_at<?) ORDER BY use_count ASC, last_used_at ASC LIMIT 12",
            (self.config.persona_id, kind, cutoff),
        )
        if not rows:
            rows = await self.store.fetch_all(
                "SELECT * FROM qq_avatar_pool WHERE persona_id=? AND trusted=1 "
                "AND COALESCE(kind,'avatar')=? "
                "ORDER BY use_count ASC, last_used_at ASC LIMIT 8",
                (self.config.persona_id, kind),
            )
        if not rows:
            return None
        if tags:
            wanted = {str(t) for t in tags}
            rows.sort(key=lambda row: len(wanted & set(self._tags(row))), reverse=True)
        import random

        choice = random.choice(rows[: min(3, len(rows))])
        return self._shape(choice)

    async def mark_used(self, item_id: int) -> None:
        await self.store.execute(
            "UPDATE qq_avatar_pool SET last_used_at=?, use_count=use_count+1 WHERE id=?",
            (int(time.time()), int(item_id)),
        )

    @staticmethod
    def _tags(row: dict[str, Any]) -> list[str]:
        import json

        try:
            value = json.loads(row.get("tags") or "[]")
        except (ValueError, TypeError):
            return []
        return [str(x) for x in value] if isinstance(value, list) else []

    async def auto_change(self, avatar: Any) -> dict[str, Any]:
        """自动换一张。只在可信库里挑，挑不到就不换。"""
        if not self.config.bool("qq_extra.avatar_auto_change", True):
            return {"ok": False, "reason": "自主换头像关着"}
        # 写死 avatar：换脸这件事只认头像，生活图和表情图不许顶上来
        item = await self.pick(kind="avatar")
        if item is None:
            return {"ok": False, "reason": "头像库里还没有能用的图"}
        result = await avatar.set_from_file(str(item.get("path") or ""), reason="auto")
        if result.get("ok"):
            await self.mark_used(int(item.get("id") or 0))
        return result
