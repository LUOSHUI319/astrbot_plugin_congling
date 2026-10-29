"""0.5.0 新增页面接口：记忆等级 / 遗忘曲线 / 好感度 / 用户画像 / 头像库。

单独放一个 mixin，PageApi 继承它。这样新功能不往 4000 行的老文件里堆。
返回格式跟老接口一致：{status, ok, data|message, ts}。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request

from ..memory.grade import BOOST, CAP, FLOOR, HARD, KNOWN_TAGS, LEVEL_NAMES, STRENGTH, TAG_LABELS


def _ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "ok": True, "data": data, "ts": int(time.time())}


def _err(message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "ok": False,
        "message": message,
        "error": message,
        "data": {},
        "ts": int(time.time()),
    }


async def _body() -> dict[str, Any]:
    payload = await request.json(default={})
    return payload if isinstance(payload, dict) else {}


async def _params() -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:
        raw = getattr(request, "query", None)
        if callable(raw):
            raw = raw()
        items = getattr(raw, "items", None)
        if callable(items):
            for key, value in items():
                data[str(key)] = value
    except Exception:
        pass
    try:
        payload = await request.json(default={})
        if isinstance(payload, dict):
            data.update(payload)
    except Exception:
        pass
    return data


class MemoryV2Mixin:
    """记忆 2.0 与头像库的页面接口。"""

    # ------------------------------------------------------------ 工具
    def _mem(self) -> Any:
        return getattr(self.plugin, "memory", None)

    def _int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def _save_cfg(self, nested: dict[str, Any]) -> bool:
        for group, payload in nested.items():
            for key, value in payload.items():
                if isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        self.cfg.override(f"{group}.{key}.{sub_key}", sub_value)
                else:
                    self.cfg.override(f"{group}.{key}", value)
        persist = getattr(self, "_persist", None)
        if callable(persist):
            try:
                return bool(await persist(nested))
            except Exception as exc:
                logger.debug(f"[congling] 配置落盘失败: {exc!r}")
        return False

    # ------------------------------------------------------------ 等级
    async def memory_levels(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            counts: dict[int, int] = {}
            try:
                rows = await self.plugin.store.fetch_all(
                    "SELECT mem_level, COUNT(*) AS cnt FROM mem_atom "
                    "WHERE status='active' GROUP BY mem_level"
                )
                for row in rows:
                    counts[int(row.get("mem_level") or 3)] = int(row.get("cnt") or 0)
            except Exception:
                counts = {}
            levels = []
            for level in range(0, 6):
                levels.append(
                    {
                        "level": level,
                        "name": LEVEL_NAMES.get(level, "平常事实"),
                        "count": counts.get(level, 0),
                        "strength": STRENGTH.get(level, 0.65),
                        "keep": "永不衰减" if level == 0 else "",
                    }
                )
            tags = []
            for tag in sorted(KNOWN_TAGS):
                tags.append(
                    {
                        "tag": tag,
                        "label": TAG_LABELS.get(tag, tag),
                        "floor": FLOOR.get(tag, 0),
                        "cap": CAP.get(tag, 0),
                        "boost": BOOST.get(tag, 0),
                        "fixed": HARD.get(tag, -1),
                    }
                )
            return _ok(
                {
                    "enabled": bool(self.cfg.bool("memory.levels.enabled", True)),
                    "manual_edit": bool(self.cfg.bool("memory.levels.manual_edit", True)),
                    "levels": levels,
                    "tags": tags,
                    "total": sum(counts.values()),
                    "forgetting_mode": str(self.cfg.str("memory.forgetting.mode", "balanced")),
                    "affinity_enabled": bool(self.cfg.bool("memory.affinity.enabled", True)),
                }
            )
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def memory_levels_save(self) -> dict[str, Any]:
        try:
            body = await _body()
            payload: dict[str, Any] = {}
            if "enabled" in body:
                payload["enabled"] = bool(body["enabled"])
            if "manual_edit" in body:
                payload["manual_edit"] = bool(body["manual_edit"])
            if "tag_weights" in body and isinstance(body["tag_weights"], dict):
                weights = {
                    str(k): self._float(v, 0)
                    for k, v in body["tag_weights"].items()
                    if str(k) in KNOWN_TAGS
                }
                if weights:
                    payload["tag_weights"] = weights
            saved = await self._save_cfg({"memory": {"levels": payload}}) if payload else False
            return _ok({"saved": saved, "applied": payload})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def memory_set_level(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            if not self.cfg.bool("memory.levels.manual_edit", True):
                return _err("手动改等级已关闭")
            body = await _body()
            atom_id = self._int(body.get("atom_id") or body.get("id"), 0)
            level = max(0, min(5, self._int(body.get("level"), 3)))
            if not atom_id:
                return _err("缺 atom_id")
            reason = str(body.get("reason") or "手动调整")[:160]
            await mem.atoms.set_fields(
                atom_id,
                {
                    "mem_level": level,
                    "level_source": "manual",
                    "level_reason": reason,
                    "strength": STRENGTH.get(level, 0.65),
                    "protect": 1 if level == 0 else 0,
                },
            )
            return _ok({"atom_id": atom_id, "level": level, "name": LEVEL_NAMES.get(level, "")})
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ------------------------------------------------------------ 遗忘曲线
    async def memory_forgetting(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            from ..memory.forgetting import MODES, ForgettingCurve

            params = await _params()
            days = self._int(params.get("days"), 45)
            curve = ForgettingCurve(self.plugin)
            data = curve.preview(days)
            data["modes"] = list(MODES)
            data["hit_boost"] = curve.hit_boost
            try:
                pending = await self.plugin.store.scalar(
                    "SELECT COUNT(*) FROM mem_atom WHERE status='active' "
                    "AND next_review_at>0 AND next_review_at<=?",
                    (int(time.time()),),
                    0,
                )
                data["due_now"] = int(pending or 0)
            except Exception:
                data["due_now"] = 0
            return _ok(data)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def memory_forgetting_save(self) -> dict[str, Any]:
        try:
            body = await _body()
            from ..memory.forgetting import MODES

            payload: dict[str, Any] = {}
            mode = str(body.get("mode") or "").lower()
            if mode in MODES:
                payload["mode"] = mode
            for key in ("archive_ratio", "forget_ratio", "hit_boost"):
                if key in body:
                    value = self._float(body.get(key), -1)
                    if 0 < value <= 3:
                        payload[key] = value
            if not payload:
                return _err("没有可保存的项")
            saved = await self._save_cfg({"memory": {"forgetting": payload}})
            return _ok({"saved": saved, "applied": payload})
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ------------------------------------------------------------ 用户识别库
    async def memory_subjects(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            params = await _params()
            keyword = str(params.get("keyword") or "").strip()
            limit = max(1, min(300, self._int(params.get("limit"), 100)))
            items = await mem.subjects.list(keyword, limit)
            for item in items:
                key = str(item.get("user_id") or "")
                try:
                    item["affinity"] = await mem.affinity.get(key)
                except Exception:
                    item["affinity"] = {}
                try:
                    profile = await mem.profiles.get(key)
                    item["has_profile"] = bool(profile.get("exists"))
                except Exception:
                    item["has_profile"] = False
            return _ok(
                {
                    "items": items,
                    "total": len(items),
                    "profile_enabled": bool(self.cfg.bool("memory.user_profile.enabled", True)),
                    "whitelist_enabled": bool(self.cfg.bool("memory.user_profile.whitelist_enabled", False)),
                    "whitelist": self.cfg.list("memory.user_profile.whitelist", []),
                    "blocklist": self.cfg.list("memory.user_profile.blocklist", []),
                }
            )
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def memory_subject_detail(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            params = await _params()
            key = str(params.get("key") or params.get("user_id") or "").strip()
            if not key:
                return _err("缺 key")
            atoms = await mem.subjects.atoms_of(key, status="", limit=120)
            for row in atoms:
                try:
                    row["level_tags"] = self._json(row.get("level_tags"), [])
                except Exception:
                    row["level_tags"] = []
            affinity = await mem.affinity.get(key)
            profile = await mem.profiles.get(key)
            return _ok(
                {
                    "key": key,
                    "name": await mem.subjects.display_name(key),
                    "atoms": atoms,
                    "affinity": affinity,
                    "profile": profile,
                    "stage_names": mem.affinity.names(),
                    "thresholds": mem.affinity.thresholds(),
                    "blocked": key in [str(x) for x in self.cfg.list("memory.user_profile.blocklist", [])],
                    "whitelisted": key in [str(x) for x in self.cfg.list("memory.user_profile.whitelist", [])],
                }
            )
        except Exception as exc:
            return _err(repr(exc)[:200])

    @staticmethod
    def _json(value: Any, default: Any) -> Any:
        import json

        if isinstance(value, (list, dict)):
            return value
        if not value:
            return default
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default

    # ------------------------------------------------------------ 画像
    async def memory_profile_build(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            body = await _body()
            key = str(body.get("key") or body.get("user_id") or "").strip()
            if not key:
                return _err("缺 key")
            if not self.cfg.bool("memory.user_profile.enabled", True):
                return _err("画像总开关关着")
            result = await mem.profiles.build(key, force=True)
            return _ok(result) if result.get("ok") else _err(str(result.get("reason") or "生成失败"))
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def memory_profile_save(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            body = await _body()
            key = str(body.get("key") or body.get("user_id") or "").strip()
            if not key:
                return _err("缺 key")
            result = await mem.profiles.update(
                key,
                summary=body.get("summary") if "summary" in body else None,
                keywords=body.get("keywords") if isinstance(body.get("keywords"), list) else None,
                fields=body.get("fields") if isinstance(body.get("fields"), dict) else None,
            )
            return _ok(result) if result.get("exists", True) else _err("还没有画像")
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def memory_profile_delete(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            body = await _body()
            key = str(body.get("key") or body.get("user_id") or "").strip()
            if not key:
                return _err("缺 key")
            block = bool(body.get("block", True))
            result = await mem.profiles.delete(key, block=block)
            return _ok(result)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def memory_profile_unblock(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            body = await _body()
            key = str(body.get("key") or body.get("user_id") or "").strip()
            if not key:
                return _err("缺 key")
            await mem.profiles.set_block(key, False)
            return _ok({"key": key, "blocked": False})
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ------------------------------------------------------------ 好感度
    async def affinity_get(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            params = await _params()
            key = str(params.get("key") or params.get("user_id") or "").strip()
            data: dict[str, Any] = {
                "enabled": bool(self.cfg.bool("memory.affinity.enabled", True)),
                "editable": bool(self.cfg.bool("memory.affinity.ui_editable", True)),
                "names": mem.affinity.names(),
                "thresholds": mem.affinity.thresholds(),
                "max_daily_delta": mem.affinity.max_delta(),
                "neglect_days": mem.affinity.neglect_days(),
            }
            if key:
                data["one"] = await mem.affinity.get(key)
                data["name"] = await mem.subjects.display_name(key)
                # 前端要据此显示"还会不会被自动加减"，不能由它自己猜
                one = data["one"] or {}
                data["manual"] = int(one.get("manual") or 0)
                data["manual_override"] = bool(self.cfg.bool("memory.affinity.manual_override", False))
                data["effective_frozen"] = bool(data["manual"] and data["manual_override"])
            data["ranking"] = await mem.affinity.leaderboard(10)
            return _ok(data)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def affinity_save(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            if not self.cfg.bool("memory.affinity.ui_editable", True):
                return _err("面板手动调整已关闭")
            body = await _body()
            key = str(body.get("key") or body.get("user_id") or "").strip()
            if not key:
                return _err("缺 key")
            score = body.get("score")
            stage = body.get("stage")
            delta = body.get("delta")
            if delta is not None:
                delta = max(-200.0, min(200.0, self._float(delta, 0)))
            result = await mem.affinity.set_manual(
                key,
                score=self._float(score, 0) if score is not None else None,
                stage=self._int(stage, 0) if stage is not None else None,
                delta=delta,
            )
            return _ok(result)
        except PermissionError as exc:
            return _err(str(exc))
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def affinity_save_config(self) -> dict[str, Any]:
        try:
            body = await _body()
            payload: dict[str, Any] = {}
            if "stage_names" in body and isinstance(body["stage_names"], list):
                names = [str(x)[:12] for x in body["stage_names"]][:6]
                if len(names) == 6:
                    payload["stage_names"] = names
            if "stage_thresholds" in body and isinstance(body["stage_thresholds"], list):
                values = [self._float(x, 0) for x in body["stage_thresholds"]][:6]
                if len(values) == 6:
                    payload["stage_thresholds"] = values
            for key in ("max_daily_delta", "neglect_days"):
                if key in body:
                    payload[key] = self._int(body[key], 0) if key == "neglect_days" else self._float(body[key], 15)
            for key in ("enabled", "ui_editable", "manual_override"):
                if key in body:
                    payload[key] = bool(body[key])
            if not payload:
                return _err("没有可保存的项")
            saved = await self._save_cfg({"memory": {"affinity": payload}})
            return _ok({"saved": saved, "applied": payload})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def affinity_settle(self) -> dict[str, Any]:
        try:
            mem = self._mem()
            if mem is None:
                return _err("记忆模块未就绪")
            result = await mem.affinity.settle_daily(force=True)
            return _ok(result)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def profile_save_config(self) -> dict[str, Any]:
        try:
            body = await _body()
            payload: dict[str, Any] = {}
            for key in ("enabled", "whitelist_enabled"):
                if key in body:
                    payload[key] = bool(body[key])
            for key in ("whitelist", "blocklist"):
                if key in body and isinstance(body[key], list):
                    payload[key] = [str(x).strip() for x in body[key] if str(x).strip()]
            for key in ("min_stage", "max_keywords"):
                if key in body:
                    payload[key] = self._int(body[key], 0)
            if not payload:
                return _err("没有可保存的项")
            saved = await self._save_cfg({"memory": {"user_profile": payload}})
            return _ok({"saved": saved, "applied": payload})
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ------------------------------------------------------------ 头像
    async def avatar_state(self) -> dict[str, Any]:
        try:
            ext = getattr(self.plugin, "qqext", None)
            pool = getattr(ext, "avatar_pool", None) if ext is not None else None
            data: dict[str, Any] = {
                "auto_change": bool(self.cfg.bool("qq_extra.avatar_auto_change", True)),
                "daily_limit": self._int(self.cfg.str("qq_extra.avatar_daily_limit", "1"), 1),
                "pool_limit": self._int(self.cfg.str("qq_extra.avatar_pool_limit", "30"), 30),
                "allow_bot_inject": bool(self.cfg.bool("qq_extra.avatar_allow_bot_inject", True)),
                "allow_user_request": bool(self.cfg.bool("qq_extra.avatar_allow_user_request", True)),
                "max_kb": self._int(self.cfg.str("qq_extra.avatar_max_kb", "512"), 512),
            }
            if pool is not None:
                data.update(await pool.state())
            else:
                data.update({"items": [], "pending": [], "trusted": 0})
            return _ok(data)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def avatar_set(self) -> dict[str, Any]:
        try:
            ext = getattr(self.plugin, "qqext", None)
            avatar = getattr(ext, "avatar", None) if ext is not None else None
            if avatar is None:
                return _err("头像模块未就绪")
            body = await _body()
            pool = getattr(ext, "avatar_pool", None)
            item_id = self._int(body.get("id"), 0)
            path = str(body.get("path") or "").strip()
            if item_id and pool is not None:
                item = await pool.get(item_id)
                if not item:
                    return _err("头像库里没这张")
                path = str(item.get("path") or "")
            if not path:
                return _err("缺图片路径或库内 id")
            result = await avatar.set_from_file(path, reason="page")
            return _ok(result) if result.get("ok") else _err(str(result.get("reason") or "换不上"))
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def avatar_pool_save(self) -> dict[str, Any]:
        try:
            ext = getattr(self.plugin, "qqext", None)
            pool = getattr(ext, "avatar_pool", None) if ext is not None else None
            if pool is None:
                return _err("头像库未就绪")
            body = await _body()
            action = str(body.get("action") or "").strip()
            item_id = self._int(body.get("id"), 0)
            if action == "trust" and item_id:
                ok = await pool.trust(item_id, True)
                return _ok({"id": item_id, "trusted": True, "changed": ok})
            if action == "untrust" and item_id:
                ok = await pool.trust(item_id, False)
                return _ok({"id": item_id, "trusted": False, "changed": ok})
            if action == "delete" and item_id:
                ok = await pool.remove(item_id)
                return _ok({"id": item_id, "removed": ok})
            if action == "tags" and item_id:
                tags = [str(x)[:12] for x in (body.get("tags") or [])][:6]
                await pool.set_tags(item_id, tags)
                return _ok({"id": item_id, "tags": tags})
            return _err("不认识的动作")
        except Exception as exc:
            return _err(repr(exc)[:200])
