"""记忆星图同步。

星图是纯静态页面（file:// 打开，CSP 也禁了 connect-src），所以「接口」不是 HTTP，
而是把数据落成它能读的文件，再跑它自己的构建脚本：

    mem_atom ──导出──▶ data/memory_snapshot.json ──node build-data.js──▶ data/memory-data.js

memory-data.js 里是 window.MEMORY_DATA = {...}，星图直接读。
所以同步一次之后，每次插件启动再同步一遍，打开星图就是最新。

为什么不在 Python 里重写关联算法：build-data.js 那套「同会话 + 实体 + 二元词 +
时间衰减」是调过的（K=7、阈值 0.38），重写一遍既容易跑偏也没必要 —— 直接调 node 跑它。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

# 星图那边只展示这两种状态；forgotten / deleted 不进去
VISIBLE_STATUS = ("active", "archived")
SYNC_KEY = "starmap:last_sync"


class StarMapSync:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 路径
    def root(self) -> Path | None:
        raw = str(self.config.str("qq_extra.star_map_dir", "") or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_dir() else None

    def snapshot_path(self) -> Path | None:
        root = self.root()
        return (root.parent / "data" / "memory_snapshot.json") if root else None

    def script_path(self) -> Path | None:
        root = self.root()
        return (root / "scripts" / "build-data.js") if root else None

    # ------------------------------------------------------------ 导出
    async def export_snapshot(self) -> dict[str, Any]:
        """把 mem_atom 导成星图认识的快照结构。"""
        target = self.snapshot_path()
        if target is None:
            return {"ok": False, "reason": "没配星图目录"}
        rows = await self.store.fetch_all(
            "SELECT id, kind, text, importance, strength, mem_level, status,"
            " session_id, session_kind, session_label, subject_id, subject_ref,"
            " created_at, last_hit_at, hit_count, level_tags"
            " FROM mem_atom WHERE status IN ('active','archived') ORDER BY id"
        )
        atoms: list[dict[str, Any]] = []
        for row in rows:
            try:
                tags = json.loads(str(row.get("level_tags") or "[]"))
                if not isinstance(tags, list):
                    tags = []
            except (TypeError, ValueError):
                tags = []
            subject = str(row.get("subject_id") or "")
            ent = [{"k": subject, "r": "subject"}] if subject else []
            atoms.append({
                "id": int(row.get("id") or 0),
                "kind": str(row.get("kind") or "fact"),
                "level": int(row.get("mem_level") or 3),
                "importance": round(float(row.get("importance") or 0.5), 3),
                "strength": round(float(row.get("strength") or 0.65), 3),
                "text": str(row.get("text") or ""),
                "session": str(row.get("session_label") or row.get("session_id") or ""),
                "session_kind": str(row.get("session_kind") or ""),
                "created": int(row.get("created_at") or 0),
                "last_hit": int(row.get("last_hit_at") or 0),
                "hits": int(row.get("hit_count") or 0),
                "status": str(row.get("status") or "active"),
                "tags": [str(t)[:24] for t in tags][:8],
                "subject": subject,
                "ent": ent,
            })

        payload = {
            "exported_at": int(time.time()),
            "count": len(atoms),
            "atoms": atoms,
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            tmp.replace(target)
        except Exception as exc:
            logger.warning(f"[congling] 导出星图快照失败: {exc!r}")
            return {"ok": False, "reason": f"写不进去：{exc!r}"[:120]}
        logger.info(f"[congling] 星图快照已导出：{len(atoms)} 条")
        return {"ok": True, "count": len(atoms), "path": str(target)}

    # ------------------------------------------------------------ 构建
    def _node(self) -> str:
        found = shutil.which("node")
        if found:
            return found
        for candidate in (
            r"C:\Program Files\nodejs\node.exe",
            r"C:\Program Files (x86)\nodejs\node.exe",
        ):
            if Path(candidate).exists():
                return candidate
        return ""

    async def build(self) -> dict[str, Any]:
        """跑星图自己的 build-data.js。"""
        script = self.script_path()
        if script is None or not script.exists():
            return {"ok": False, "reason": "找不到 build-data.js"}
        node = self._node()
        if not node:
            return {"ok": False, "reason": "找不到 node，装了 Node.js 才能编译星图数据"}

        def run() -> tuple[int, str]:
            import subprocess

            proc = subprocess.run(
                [node, str(script)],
                cwd=str(script.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

        try:
            code, output = await asyncio.to_thread(run)
        except Exception as exc:
            logger.warning(f"[congling] 星图编译失败: {exc!r}")
            return {"ok": False, "reason": f"编译失败：{exc!r}"[:120]}
        if code != 0:
            logger.warning(f"[congling] 星图编译返回 {code}: {output[:400]}")
            return {"ok": False, "reason": f"编译返回 {code}", "output": output[:400]}
        logger.info(f"[congling] 星图数据已重建 {output.strip()[:120]}")
        return {"ok": True, "output": output.strip()[:400]}

    # ------------------------------------------------------------ 同步
    async def sync(self, *, force: bool = False) -> dict[str, Any]:
        """导出 + 编译。配置了目录才会真的动。"""
        if self.root() is None:
            return {"ok": False, "reason": "没配星图目录"}
        async with self._lock:
            exported = await self.export_snapshot()
            if not exported.get("ok"):
                return exported
            built = await self.build()
            result = {
                "ok": bool(built.get("ok")),
                "count": exported.get("count", 0),
                "at": int(time.time()),
                "reason": built.get("reason", ""),
            }
            try:
                await self.store.kv_set(SYNC_KEY, result)
            except Exception:
                pass
            return result

    async def last_sync(self) -> dict[str, Any]:
        try:
            data = await self.store.kv_get(SYNC_KEY, {}) or {}
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    async def state(self) -> dict[str, Any]:
        root = self.root()
        last = await self.last_sync()
        return {
            "configured": root is not None,
            "dir": str(root) if root else "",
            "snapshot": str(self.snapshot_path() or ""),
            "script": str(self.script_path() or ""),
            "node": bool(self._node()),
            "last": last,
        }
