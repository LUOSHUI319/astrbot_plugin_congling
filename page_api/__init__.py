"""插件页面后端接口。

约定：
- 路由前缀 /congling/page
- 所有返回 {ok, data, error}
- 所有 handler 自己 try/except，不让 500 冒到前端
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request

from ..core import paths
from .diary import DiaryMixin
from .memory_m3 import MemoryM3Mixin
from .memory_v2 import MemoryV2Mixin
from .proactive import ProactiveMixin

PAGE_API_PREFIX = "/astrbot_plugin_congling/page"
"""必须与 metadata.yaml 的 name 完全一致。

页面桥接的前端会请求 /api/plug/{plugin_name}/{endpoint}，其中 plugin_name 取自
页面上下文的 metadata.name；这里少一个 astrbot_plugin_ 前缀就会全 404。
"""


def _ok(data: Any = None) -> dict[str, Any]:
    # 同时给 status 和 ok：dashboard 用 status 判断错误，本插件前端也认 ok
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
    """GET 的参数可能在 query 里，也可能被前端塞进 body，两边都收。

    注意 AstrBot 的 query 是 PluginMultiDict，**不是 dict 子类**，
    用 isinstance(x, dict) 判断会直接漏掉所有 query 参数。
    """
    data: dict[str, Any] = {}
    try:
        raw = getattr(request, "query", None)
        if callable(raw):
            raw = raw()
        if raw is not None:
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


GROUP_TITLES = {
    "basic": "基础",
    "persona": "人格",
    "humanize": "拟人化",
    "guard": "输出风格",
    "perception": "消息感知",
    "memory": "长期记忆",
    "capability": "实用工具",
    "qq_extra": "QQ 扩展",
    "compat": "兼容共存",
    "debug": "调试",
    "night": "长夜如梦",
    "close": "收线静默",
}

def _fallback_night_key(ts: int) -> str:
    """没有 night_key 的老数据：按中午切分归夜。"""
    import datetime as _dt

    if ts <= 0:
        return ""
    moment = _dt.datetime.fromtimestamp(ts)
    if moment.hour < 12:
        moment = moment - _dt.timedelta(days=1)
    return moment.strftime("%Y-%m-%d")


_SCHEMA_CACHE: dict[str, Any] | None = None


def _load_schema() -> dict[str, Any]:
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is not None:
        return _SCHEMA_CACHE
    path = paths.schema_path()
    try:
        _SCHEMA_CACHE = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"[congling] 读取配置 schema 失败: {exc!r}")
        _SCHEMA_CACHE = {}
    return _SCHEMA_CACHE


def _schema_types() -> dict[str, str]:
    result: dict[str, str] = {}
    for group, spec in _load_schema().items():
        for key, raw_item in (spec.get("items") or {}).items():
            item = raw_item or {}
            # 嵌套 object（比如 models.chat）要把子字段也登记上，
            # 否则保存时类型查不到，数字会被当字符串写回去
            if item.get("type") == "object" and item.get("items"):
                for sub_key, raw_sub in item["items"].items():
                    sub = raw_sub or {}
                    result[f"{group}.{key}.{sub_key}"] = str(sub.get("type") or "string")
                continue
            result[f"{group}.{key}"] = str(item.get("type") or "string")
    return result


def _coerce(item_type: str | None, value: Any) -> Any:
    if item_type is None:
        return value
    if item_type == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
        return bool(value)
    if item_type == "int":
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0
    if item_type == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if item_type == "list":
        if isinstance(value, (list, tuple)):
            return [item for item in value]
        if isinstance(value, str):
            parts = value.replace("，", ",").replace("\n", ",").split(",")
            return [item.strip() for item in parts if item.strip()]
        return []
    if isinstance(value, str):
        return value
    return value if value is not None else ""


def _schema_groups(config: Any) -> list[dict[str, Any]]:
    """把 _conf_schema.json + 当前值组装成前端能直接渲染的分区结构。"""
    schema = _load_schema()
    values = config.snapshot()
    groups: list[dict[str, Any]] = []
    for group, spec in schema.items():
        spec = spec or {}
        current = values.get(group) if isinstance(values.get(group), dict) else {}
        items: list[dict[str, Any]] = []
        for key, raw_item in (spec.get("items") or {}).items():
            item = raw_item or {}
            # 嵌套 object 摊平成多行，path 用 models.chat.source 这种三层路径
            if item.get("type") == "object" and item.get("items"):
                sub_values = current.get(key) if isinstance(current.get(key), dict) else {}
                fan_title = item.get("description") or key
                for sub_key, raw_sub in item["items"].items():
                    sub = raw_sub or {}
                    items.append(
                        {
                            "key": f"{key}.{sub_key}",
                            "path": f"{group}.{key}.{sub_key}",
                            "label": f"{fan_title} · {sub.get('description') or sub_key}",
                            "hint": sub.get("hint") or "",
                            "type": sub.get("type") or "string",
                            "options": sub.get("options") or [],
                            "default": sub.get("default"),
                            "value": sub_values.get(sub_key, sub.get("default")),
                            "special": sub.get("_special") or "",
                        }
                    )
                continue
            value = current.get(key, item.get("default"))
            items.append(
                {
                    "key": key,
                    "path": f"{group}.{key}",
                    "label": item.get("description") or key,
                    "hint": item.get("hint") or "",
                    "type": item.get("type") or "string",
                    "options": item.get("options") or [],
                    "default": item.get("default"),
                    "value": value,
                    "special": item.get("_special") or "",
                }
            )
        groups.append(
            {
                "name": group,
                "title": GROUP_TITLES.get(group, group),
                "desc": spec.get("description") or "",
                "hint": spec.get("hint") or "",
                "items": items,
            }
        )
    return groups


class PageApi(ProactiveMixin, DiaryMixin, MemoryM3Mixin, MemoryV2Mixin):
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.cfg = plugin.cfg

    @staticmethod
    def _deep_merge(target: dict[str, Any], payload: dict[str, Any]) -> None:
        for key, value in payload.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                PageApi._deep_merge(target[key], value)
            else:
                target[key] = value

    async def _persist(self, nested: dict[str, Any]) -> bool:
        """把嵌套配置写进插件配置对象并落盘。"""
        raw = self.plugin.raw_config
        saved = False
        if isinstance(raw, dict):
            for group, payload in nested.items():
                current = raw.get(group)
                if isinstance(current, dict) and isinstance(payload, dict):
                    # 必须深合并：models.chat 这类嵌套段只传了 source，
                    # 直接 update 会把同级的 api_base / model 抹掉
                    self._deep_merge(current, payload)
                else:
                    raw[group] = payload
            save = getattr(raw, "save_config", None)
            if callable(save):
                try:
                    save()
                    saved = True
                except Exception as exc:
                    logger.warning(f"[congling] 写配置文件失败: {exc!r}")
        self.cfg.apply(nested)
        return saved

    # ================================================================ 注册
    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            ("/overview", self.overview, ["GET"]),
            ("/persona", self.get_persona, ["GET"]),
            ("/persona/save", self.save_persona, ["POST"]),
            ("/persona/rollback", self.rollback_persona, ["POST"]),
            ("/settings", self.get_settings, ["GET"]),
            ("/settings/save", self.save_settings, ["POST"]),
            ("/memory/list", self.memory_list, ["GET"]),
            ("/memory/search", self.memory_search, ["POST"]),
            ("/memory/update", self.memory_update, ["POST"]),
            ("/memory/delete", self.memory_delete, ["POST"]),
            ("/inbox", self.inbox, ["GET"]),
            ("/tools", self.tools, ["GET"]),
            ("/subjects/merge_candidates", self.subject_merge_candidates, ["GET"]),
            ("/subjects/merge", self.subject_merge, ["POST"]),
            ("/subjects/rollback", self.subject_rollback, ["POST"]),
            ("/tools/test", self.tools_test, ["POST"]),
            ("/qzone", self.qzone, ["GET"]),
            ("/qzone/feed", self.qzone_feed, ["GET"]),
            ("/qzone/publish", self.qzone_publish, ["POST"]),
            ("/qzone/auth", self.qzone_auth, ["POST"]),
            ("/admins", self.admins, ["GET"]),
            ("/health", self.health, ["GET"]),
            ("/diagnostics", self.diagnostics_tail, ["GET"]),
            ("/night/state", self.night_state, ["GET"]),
            ("/night/dreams", self.night_dreams, ["GET"]),
            ("/night/wake", self.night_wake, ["POST"]),
            ("/night/sleep", self.night_sleep, ["POST"]),
            ("/night/dream/delete", self.night_dream_delete, ["POST"]),
            ("/night/dream/pin", self.night_dream_pin, ["POST"]),
            ("/night/dream/make", self.night_dream_make, ["POST"]),
            ("/night/unread", self.night_unread, ["GET"]),
            ("/night/crawl", self.night_crawl, ["POST"]),
            # ---- 0.7.0 日记
            ("/diary/list", self.diary_list, ["GET"]),
            ("/diary/get", self.diary_get, ["GET"]),
            ("/diary/generate", self.diary_generate, ["POST"]),
            ("/diary/delete", self.diary_delete, ["POST"]),
            ("/diary/pin", self.diary_pin, ["POST"]),
            # ---- 0.7.0 主动找人
            ("/proactive/state", self.proactive_state, ["GET"]),
            ("/proactive/sessions", self.proactive_sessions, ["GET"]),
            ("/proactive/log", self.proactive_log, ["GET"]),
            ("/proactive/now", self.proactive_now, ["POST"]),
            ("/proactive/mute", self.proactive_mute, ["POST"]),
            # ---- 0.5.0 记忆 2.0
            ("/memory/levels", self.memory_levels, ["GET"]),
            ("/memory/levels/save", self.memory_levels_save, ["POST"]),
            ("/memory/level/set", self.memory_set_level, ["POST"]),
            ("/memory/forgetting", self.memory_forgetting, ["GET"]),
            ("/memory/forgetting/save", self.memory_forgetting_save, ["POST"]),
            ("/memory/subjects", self.memory_subjects, ["GET"]),
            # ---- 0.6.0 记忆三板块
            ("/memory/m3/overview", self.memory_m3_overview, ["GET"]),
            ("/memory/sessions", self.memory_sessions, ["GET"]),
            ("/memory/session", self.memory_session_detail, ["GET"]),
            ("/memory/subject", self.memory_subject_detail, ["GET"]),
            ("/memory/profile/build", self.memory_profile_build, ["POST"]),
            ("/memory/profile/save", self.memory_profile_save, ["POST"]),
            ("/memory/profile/delete", self.memory_profile_delete, ["POST"]),
            ("/memory/profile/unblock", self.memory_profile_unblock, ["POST"]),
            ("/memory/profile/config", self.profile_save_config, ["POST"]),
            ("/affinity", self.affinity_get, ["GET"]),
            ("/affinity/save", self.affinity_save, ["POST"]),
            ("/affinity/config", self.affinity_save_config, ["POST"]),
            ("/affinity/settle", self.affinity_settle, ["POST"]),
            # ---- 0.5.0 头像
            ("/avatar", self.avatar_state, ["GET"]),
            ("/avatar/set", self.avatar_set, ["POST"]),
            ("/avatar/pool", self.avatar_pool_save, ["POST"]),
        ]
        for path, handler, methods in routes:
            try:
                register(f"{PAGE_API_PREFIX}{path}", handler, methods, f"从绫开始 {path}")
            except Exception as exc:
                logger.warning(f"[congling] 注册页面接口失败 {path}: {exc!r}")

    # ================================================================ 概览
    async def overview(self) -> dict[str, Any]:
        try:
            stats = await self.plugin.store.stats()
            logs = await self.plugin.decision.recent(10)
            memory_count = 0
            if self.plugin.memory is not None:
                memory_count = await self.plugin.memory.atoms.count()
            reply_count = sum(1 for item in logs if item.get("decided") == "sent")
            mmx = {"available": False, "reason": "模型模块未就绪"}
            try:
                if self.plugin.models is not None:
                    status = self.plugin.models.mmx_status()
                    mmx = {
                        "available": bool(status.get("available")),
                        "state": status.get("state", ""),
                        "reason": status.get("reason", ""),
                    }
            except Exception as exc:
                logger.debug(f"[congling] 读取 mmx 状态失败: {exc!r}")
                mmx = {"available": False, "reason": f"{exc!r}"[:160]}
            return _ok(
                {
                    "health": self.plugin.health(),
                    "stats": stats,
                    "memory_active": memory_count,
                    "recent": logs,
                    "reply_count_recent": reply_count,
                    "mmx": mmx,
                }
            )
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ================================================================ 人格
    async def get_persona(self) -> dict[str, Any]:
        try:
            if self.plugin.persona is None:
                return _err("人格模块未就绪")
            data = self.plugin.persona.as_dict()
            data["versions"] = await self.plugin.persona.versions()
            data["astrbot_personas"] = await self.plugin.persona.available_personas()
            return _ok(data)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def save_persona(self) -> dict[str, Any]:
        try:
            if self.plugin.persona is None:
                return _err("人格模块未就绪")
            body = await _body()
            # 人设来源属于插件配置，不进人格 KV
            mode = body.pop("mode", None)
            astrbot_id = body.pop("astrbot_persona_id", None)
            data = await self.plugin.persona.save(body)

            changes: dict[str, str] = {}
            if isinstance(mode, str) and mode in {"builtin", "astrbot", "none"}:
                changes["persona.mode"] = mode
            if isinstance(astrbot_id, str):
                changes["persona.astrbot_persona_id"] = astrbot_id
            if changes:
                nested = {"persona": {}}
                for key, value in changes.items():
                    nested["persona"][key.split(".")[-1]] = value
                await self._persist(nested)
                data = self.plugin.persona.as_dict()
            return _ok(data)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def rollback_persona(self) -> dict[str, Any]:
        try:
            if self.plugin.persona is None:
                return _err("人格模块未就绪")
            body = await _body()
            index = int(body.get("index", 0) or 0)
            data = await self.plugin.persona.rollback(index)
            if data is None:
                return _err("没有这个版本")
            return _ok(data)
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ================================================================ 设置
    async def get_settings(self) -> dict[str, Any]:
        try:
            return _ok(
                {
                    "groups": _schema_groups(self.cfg),
                    "config": self.cfg.snapshot(),
                    "compat": self.plugin.probe.matrix(),
                }
            )
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def save_settings(self) -> dict[str, Any]:
        """写入配置并落盘。

        与 v0.1.1 的「临时覆写」不同：这里会真的改插件配置文件，
        重载插件后依然生效。
        """
        try:
            body = await _body()
            changes = body.get("changes") or {}
            if not isinstance(changes, dict):
                return _err("changes 格式不对")

            types = _schema_types()
            nested: dict[str, Any] = {}
            applied = 0
            for path, value in changes.items():
                if not isinstance(path, str) or not path:
                    continue
                coerced = _coerce(types.get(path), value)
                node = nested
                parts = path.split(".")
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                    if not isinstance(node, dict):
                        break
                else:
                    node[parts[-1]] = coerced
                    applied += 1

            saved = await self._persist(nested)
            return _ok(
                {
                    "applied": applied,
                    "saved": saved,
                    "note": "已写入配置文件" if saved else "已生效，但未能写入文件（配置对象不支持保存）",
                }
            )
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ================================================================ 记忆
    async def memory_list(self) -> dict[str, Any]:
        try:
            if self.plugin.memory is None:
                return _err("记忆模块未就绪")
            params = await _params()
            atoms = self.plugin.memory.atoms

            def as_int(name: str, default: int) -> int:
                try:
                    return int(params.get(name, default) or default)
                except (TypeError, ValueError):
                    return default

            session_id = str(params.get("session") or params.get("session_id") or "")
            kind = str(params.get("kind") or "")
            keyword = str(params.get("keyword") or "")
            rows = await atoms.list(
                session_id=session_id or None,
                kind=kind or None,
                keyword=keyword or None,
                limit=max(1, min(500, as_int("limit", 50))),
                offset=max(0, as_int("offset", 0)),
            )
            total = await atoms.count()
            try:
                unread = await atoms.count_unread()
            except Exception:
                unread = 0
            try:
                sessions = await atoms.sessions()
            except Exception:
                sessions = []
            try:
                tier = str(self.plugin.memory.tier() or "")
            except Exception:
                tier = ""
            return _ok(
                {
                    "total": total,
                    "unread": unread,
                    "items": rows,
                    "sessions": sessions,
                    "tier": tier,
                    "session": session_id,
                    "kind": kind,
                    "keyword": keyword,
                }
            )
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def memory_search(self) -> dict[str, Any]:
        try:
            if self.plugin.memory is None:
                return _err("记忆模块未就绪")
            body = await _body()
            query = str(body.get("query") or body.get("q") or "").strip()
            if not query:
                return _err("没有关键词")
            hits = await self.plugin.memory.recall(
                query,
                scope=str(body.get("scope") or "user"),
                subject=str(body.get("subject") or ""),
                k=int(body.get("k") or 8),
                cross_session=bool(body.get("cross_session", True)),
            )
            items: list[dict[str, Any]] = []
            for hit in hits or []:
                if isinstance(hit, dict):
                    items.append(hit)
                elif hasattr(hit, "__dict__"):
                    items.append(dict(vars(hit)))
                else:
                    items.append({"text": str(hit)})
            return _ok({"items": items, "query": query})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def memory_update(self) -> dict[str, Any]:
        try:
            if self.plugin.memory is None:
                return _err("记忆模块未就绪")
            body = await _body()
            try:
                atom_id = int(body.get("id") or 0)
            except (TypeError, ValueError):
                atom_id = 0
            if not atom_id:
                return _err("没给记忆 id")
            fields = {key: body[key] for key in ("text", "importance", "kind", "status") if key in body}
            if not fields:
                return _err("没有要改的字段")
            await self.plugin.memory.atoms.set_fields(atom_id, fields)
            return _ok({"updated": atom_id, "fields": list(fields)})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def memory_delete(self) -> dict[str, Any]:
        try:
            if self.plugin.memory is None:
                return _err("记忆模块未就绪")
            body = await _body()
            try:
                atom_id = int(body.get("id") or 0)
            except (TypeError, ValueError):
                atom_id = 0
            if not atom_id:
                return _err("没给记忆 id")
            await self.plugin.memory.atoms.set_status(atom_id, str(body.get("status") or "deleted"))
            return _ok({"deleted": atom_id})
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ================================================================ 收件箱
    async def inbox(self) -> dict[str, Any]:
        try:
            last = await self.plugin.store.kv_get("poke:last")
            items = [last] if isinstance(last, dict) else []
            return _ok({"items": items})
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ================================================================ 工具
    async def tools(self) -> dict[str, Any]:
        try:
            if self.plugin.capabilities is None:
                return _err("能力模块未就绪")
            return _ok({"items": self.plugin.capabilities.list()})
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ================================================================ 主体归并
    async def subject_merge_candidates(self) -> dict[str, Any]:
        try:
            mem = getattr(self.plugin, "memory", None)
            if mem is None:
                return _err("记忆模块未就绪")
            items = await mem.subjects.merge_candidates()
            return _ok({"items": items, "count": len(items)})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def subject_rollback(self) -> dict[str, Any]:
        """撤销一次主体合并。之后有新写入会被拒绝，避免把别人的数据带回去。"""
        try:
            mem = getattr(self.plugin, "memory", None)
            if mem is None:
                return _err("记忆模块未就绪")
            body = await _body()
            run_id = str(body.get("run_id") or "").strip()
            if not run_id:
                return _err("缺 run_id")
            result = await mem.subjects.rollback_merge(run_id)
            return _ok(result) if result.get("ok") else _err(str(result.get("error") or "撤销失败"))
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def subject_merge(self) -> dict[str, Any]:
        try:
            mem = getattr(self.plugin, "memory", None)
            if mem is None:
                return _err("记忆模块未就绪")
            body = await _body()
            pairs = body.get("pairs")
            if not isinstance(pairs, list) or not pairs:
                return _err("没有勾选要合并的项")
            result = await mem.subjects.merge_subjects(pairs)
            return _ok(result) if result.get("ok") else _err(str(result.get("error") or "合并失败"))
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def diagnostics_tail(self) -> dict[str, Any]:
        """最近一条诊断轨迹，只读。排查"为什么没生效"时先看这个。"""
        try:
            diag = getattr(self.plugin, "diag", None)
            if diag is None:
                return _err("诊断模块未就绪")
            return _ok({"items": await diag.tail(30)})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def tools_test(self) -> dict[str, Any]:
        try:
            if self.plugin.capabilities is None:
                return _err("能力模块未就绪")
            body = await _body()
            name = str(body.get("name") or "").strip()
            if not name:
                return _err("没给能力名")
            # 前端一直发的是 params，后端只认 args，于是"试跑"永远等于空参调用
            args = body.get("args")
            if not isinstance(args, dict):
                args = body.get("params")
            kwargs = args if isinstance(args, dict) else {}
            result = await self.plugin.capabilities.invoke(name, admin=True, **kwargs)
            elapsed = getattr(result, "elapsed", None)
            if elapsed is None:
                elapsed = getattr(result, "ms", 0)
            return _ok(
                {
                    "name": name,
                    "ok": bool(getattr(result, "ok", False)),
                    "text": str(getattr(result, "text", "") or ""),
                    "error": str(getattr(result, "error", "") or ""),
                    "ms": elapsed,
                    "cached": bool(getattr(result, "cached", False)),
                    "data": getattr(result, "data", None),
                }
            )
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ================================================================ 空间
    def _qzone(self) -> Any:
        return getattr(self.plugin.qqext, "qzone", None) if self.plugin.qqext is not None else None

    async def qzone(self) -> dict[str, Any]:
        try:
            bridge = self._qzone()
            if bridge is None:
                return _err("空间功能没接上")
            data = await bridge.status()
            if not isinstance(data, dict):
                return _err("状态读取失败")
            return _ok(data)
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def qzone_feed(self) -> dict[str, Any]:
        try:
            bridge = self._qzone()
            if bridge is None:
                return _err("空间功能没接上")
            params = await _params()
            try:
                limit = int(params.get("limit") or 5)
            except (TypeError, ValueError):
                limit = 5
            data = await bridge.feed(
                limit=max(1, min(50, limit)),
                target=str(params.get("target") or ""),
            )
            return _ok(data if isinstance(data, dict) else {"items": data})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def qzone_publish(self) -> dict[str, Any]:
        try:
            bridge = self._qzone()
            if bridge is None:
                return _err("空间功能没接上")
            body = await _body()
            result = await bridge.publish(str(body.get("content") or body.get("text") or ""))
            return _ok(result if isinstance(result, dict) else {"ok": bool(result)})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def qzone_auth(self) -> dict[str, Any]:
        try:
            bridge = self._qzone()
            if bridge is None:
                return _err("空间功能没接上")
            body = await _body()
            data = await bridge.auth(force=bool(body.get("force", True)))
            if isinstance(data, dict):
                return _ok(data)
            return _ok(
                {
                    "ok": bool(getattr(data, "usable", False)),
                    "uin": int(getattr(data, "uin", 0) or 0),
                    "source": str(getattr(data, "source", "") or ""),
                }
            )
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ================================================================ 杂项
    async def admins(self) -> dict[str, Any]:
        try:
            astrbot: list[str] = []
            try:
                config = self.plugin.context.get_config()
                raw = config.get("admins_id") if isinstance(config, dict) else None
                if isinstance(raw, list):
                    astrbot = [str(item) for item in raw]
            except Exception as exc:
                logger.debug(f"[congling] 读取 AstrBot 管理员失败: {exc!r}")

            extra = [str(item) for item in self.cfg.list("basic.extra_admins", [])]
            effective = list(dict.fromkeys(astrbot + extra))
            return _ok(
                {
                    "astrbot": astrbot,
                    "extra": extra,
                    "effective": effective,
                    "include_group_role": self.cfg.bool("basic.include_group_role", False),
                    "roles_note": "AstrBot 名单在 WebUI 全局配置的 admins_id 里改",
                }
            )
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def health(self) -> dict[str, Any]:
        try:
            return _ok(self.plugin.health())
        except Exception as exc:
            return _err(repr(exc)[:200])

    # ================================================================ 长夜如梦
    async def night_state(self) -> dict[str, Any]:
        try:
            if getattr(self.plugin, "night", None) is None:
                return _ok({"enabled": False, "reason": "长夜如梦没有启用"})
            return _ok(self.plugin.night.snapshot())
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def night_dream_make(self) -> dict[str, Any]:
        """手动补一段梦。

        正常是睡着期间定时做的，这里给面板一个「现在做一段」的入口：
        想确认梦这条链路通不通、或者刚改完配置想立刻看效果，不用等下一觉。
        """
        try:
            night = getattr(self.plugin, "night", None)
            if night is None or getattr(night, "dreamer", None) is None:
                return _err("长夜如梦没有启用")
            dreamer = night.dreamer
            if not dreamer.enabled:
                return _err("梦境开关是关着的，先把 night.dream_enabled 打开")
            if not getattr(dreamer, "_loaded", False):
                await dreamer.load()
            last = dreamer.list_recent(1)
            continuation = ""
            if last:
                continuation = (last[0].text or "")[-200:]
            entry = await dreamer.make(continuation=continuation)
            if entry is None:
                return _err("这段梦没生成出来，可能模型没返回或素材为空，看看日志")
            return _ok({"dream": entry.to_dict(), "stats": dreamer.stats()})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def night_dreams(self) -> dict[str, Any]:
        try:
            night = getattr(self.plugin, "night", None)
            if night is None or getattr(night, "dreamer", None) is None:
                return _ok({"items": [], "stats": {}})
            params = await _params()
            try:
                limit = int(params.get("limit") or 20)
            except (TypeError, ValueError):
                limit = 20
            if not getattr(night.dreamer, "_loaded", False):
                try:
                    await night.dreamer.load()
                except Exception:
                    pass
            dreams = night.dreamer.list_recent(max(1, min(100, limit)))
            items = []
            for dream in dreams:
                total_days = 16
                try:
                    if 0 < dream.decay_per_day < 1:
                        total_days = max(1, int(round(math.log(0.15) / math.log(1 - dream.decay_per_day))))
                except Exception:
                    total_days = 16
                items.append(
                    {
                        **dream.to_dict(),
                        "remaining_days": dream.remaining_days(),
                        "total_days": total_days,
                        "strength_text": f"{dream.strength:.0%}",
                    }
                )
            # 按「夜」分组：中午之前的梦归前一晚
            nights: dict[str, list[dict[str, Any]]] = {}
            for item in items:
                key = str(item.get("night_key") or "")
                if not key:
                    key = _fallback_night_key(int(item.get("created_at") or 0))
                item["night_key"] = key
                nights.setdefault(key, []).append(item)
            for group in nights.values():
                group.sort(key=lambda d: int(d.get("created_at") or 0))
            night_list = [
                {"night_key": key, "segments": group}
                for key, group in sorted(nights.items(), reverse=True)
            ]
            return _ok({
                "items": items,
                "nights": night_list,
                "stats": night.dreamer.stats(),
            })
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def night_wake(self) -> dict[str, Any]:
        try:
            night = getattr(self.plugin, "night", None)
            if night is None:
                return _err("长夜如梦没有启用")
            await night.state.wake_now(reason="manual")
            return _ok(night.snapshot())
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def night_sleep(self) -> dict[str, Any]:
        try:
            night = getattr(self.plugin, "night", None)
            if night is None:
                return _err("长夜如梦没有启用")
            body = await _body()
            # 不传 kind 时交给 sleep_now 按当前时段自己判断，
            # 否则下午在面板上点「睡」会按夜里那套算到第二天早上
            kind = str(body.get("kind") or "") or None
            await night.state.sleep_now(reason="manual", kind=kind)
            return _ok(night.snapshot())
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def night_dream_delete(self) -> dict[str, Any]:
        try:
            night = getattr(self.plugin, "night", None)
            if night is None or getattr(night, "dreamer", None) is None:
                return _err("长夜如梦没有启用")
            body = await _body()
            dream_id = str(body.get("dream_id") or "")
            if not dream_id:
                return _err("缺 dream_id")
            removed = night.dreamer.forget(dream_id)
            if removed:
                await night.dreamer.save()
            return _ok({"removed": removed})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def night_dream_pin(self) -> dict[str, Any]:
        """拽住一段梦：不再衰减，也不会被忘掉。"""
        try:
            night = getattr(self.plugin, "night", None)
            if night is None or getattr(night, "dreamer", None) is None:
                return _err("长夜如梦没有启用")
            body = await _body()
            dream_id = str(body.get("dream_id") or "")
            if not dream_id:
                return _err("缺 dream_id")
            pinned = bool(body.get("pinned", True))
            changed = night.dreamer.set_pinned(dream_id, pinned)
            if changed:
                await night.dreamer.save()
            return _ok({"changed": changed, "pinned": pinned})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def night_unread(self) -> dict[str, Any]:
        try:
            night = getattr(self.plugin, "night", None)
            if night is None:
                return _ok({"items": [], "groups": [], "count": 0, "total": 0})
            params = await _params()
            try:
                limit = int(params.get("limit") or 20)
            except (TypeError, ValueError):
                limit = 20
            items = await night.crawler.unread_atoms(max(1, min(100, limit)))
            total = 0
            groups: list[dict[str, Any]] = []
            try:
                rows = await self.plugin.store.fetch_all(
                    "SELECT session_id, session_kind, session_label, COUNT(*) AS c, "
                    "MIN(unread_since) AS since FROM mem_atom "
                    "WHERE status='active' AND unread=1 GROUP BY session_id, session_kind, session_label "
                    "ORDER BY since DESC LIMIT 20"
                )
                for row in rows:
                    total += int(row["c"] or 0)
                    groups.append({
                        "session_id": str(row["session_id"] or ""),
                        "kind": str(row["session_kind"] or ""),
                        "label": str(row["session_label"] or "") or str(row["session_id"] or ""),
                        "count": int(row["c"] or 0),
                        "since": int(row["since"] or 0),
                    })
            except Exception as exc:
                logger.debug(f"[congling] 未爬分组失败: {exc!r}")
            if not total:
                total = len(items)
            return _ok({"items": items, "groups": groups, "count": len(items), "total": total})
        except Exception as exc:
            return _err(repr(exc)[:200])

    async def night_crawl(self) -> dict[str, Any]:
        try:
            night = getattr(self.plugin, "night", None)
            if night is None:
                return _err("长夜如梦没有启用")
            body = await _body()
            session_id = str(body.get("session_id") or "")
            removed = await night.crawler.mark_read(session_id)
            return _ok({"removed": removed})
        except Exception as exc:
            return _err(repr(exc)[:200])
