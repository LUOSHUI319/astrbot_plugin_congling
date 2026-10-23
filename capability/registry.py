"""能力注册表。

所有"实用查询"都做成同一个形状：一个 Capability + 一个 handler。
好处是：LLM 工具、UI 试跑、限流、超时、降级全都只有一份实现。

硬规矩：能力执行永远不抛异常。失败返回一句人话 + ok=False，模型看到的是
「没查到」而不是 traceback。
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

from astrbot.api import logger

from ..core.contracts import Capability, CapabilityResult

TOOL_PREFIX = "congling_"


class CapabilityRegistry:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.caps: dict[str, Capability] = {}
        self._uses: dict[str, list[float]] = defaultdict(list)
        self._stats: dict[str, dict[str, Any]] = {}
        self._tools: dict[str, Any] = {}

    # ================================================================ 生命周期
    async def start(self) -> None:
        from .providers import build_builtin

        for cap in build_builtin(self.plugin):
            self.register(cap)
        logger.info(f"[congling] 能力注册完成：{len(self.caps)} 项")

    def register(self, cap: Capability) -> None:
        if not self.config.bool("capability.enabled", True):
            return
        groups = {str(item) for item in self.config.list("capability.groups", [])}
        if groups and cap.group not in groups:
            return
        self.caps[cap.name] = cap
        self._stats.setdefault(cap.name, {"calls": 0, "errors": 0, "total_ms": 0.0})

    def unregister(self, name: str) -> None:
        self.caps.pop(name, None)

    def list(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for name, cap in self.caps.items():
            stat = self._stats.get(name, {})
            calls = int(stat.get("calls", 0))
            result.append(
                {
                    "name": name,
                    "description": cap.description,
                    "group": cap.group,
                    "enabled": cap.enabled,
                    "timeout": cap.timeout,
                    "cache_ttl": cap.cache_ttl,
                    "calls": calls,
                    "errors": int(stat.get("errors", 0)),
                    "avg_ms": round(float(stat.get("total_ms", 0.0)) / calls, 1) if calls else 0.0,
                    "parameters": cap.parameters,
                }
            )
        return sorted(result, key=lambda item: (item["group"], item["name"]))

    # ================================================================ 调用
    async def invoke(
        self,
        name: str,
        *,
        session_id: str = "",
        admin: bool = False,
        event: Any = None,
        **kwargs: Any,
    ) -> CapabilityResult:
        cap = self.caps.get(name)
        if cap is None:
            return CapabilityResult(ok=False, text="这个能力没有启用", error="not_found")
        if not cap.enabled:
            return CapabilityResult(ok=False, text="这个能力暂时关掉了", error="disabled")
        if cap.auth == "admin" and not admin:
            return CapabilityResult(ok=False, text="这个要管理员才行", error="forbidden")

        if not self._allow(name, session_id):
            return CapabilityResult(ok=False, text="问得有点勤，先让我喘口气", error="rate_limited")

        cached = await self._cache_get(cap, kwargs)
        if cached is not None:
            return cached

        started = time.perf_counter()
        try:
            handler = cap.handler
            if handler is None:
                raise RuntimeError("能力未绑定实现")
            timeout = cap.timeout or self.config.float("capability.default_timeout", 8.0)
            call_kwargs = dict(kwargs)
            if cap.takes_event and event is not None:
                call_kwargs["_event"] = event
            data = await asyncio.wait_for(handler(**call_kwargs), timeout=timeout)
            elapsed = time.perf_counter() - started
            result = self._normalize(data, cap, elapsed)
        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - started
            result = CapabilityResult(
                ok=False, text=cap.fallback or "这个查得有点慢，没查出来", elapsed=elapsed, error="timeout"
            )
        except Exception as exc:
            elapsed = time.perf_counter() - started
            logger.debug(f"[congling] 能力 {name} 执行失败: {exc!r}")
            result = CapabilityResult(
                ok=False, text=cap.fallback or "这个我查不到欸", elapsed=elapsed, error=repr(exc)[:200]
            )

        self._record(name, result)
        if result.ok and cap.cache_ttl > 0:
            await self._cache_set(cap, kwargs, result)
        return result

    def _normalize(self, data: Any, cap: Capability, elapsed: float) -> CapabilityResult:
        if isinstance(data, CapabilityResult):
            data.elapsed = elapsed
            return data
        if isinstance(data, dict):
            return CapabilityResult(
                ok=True,
                text=str(data.get("text") or "").strip() or cap.fallback,
                data=data.get("data", data),
                source=str(data.get("source") or cap.name),
                elapsed=elapsed,
            )
        if isinstance(data, str):
            return CapabilityResult(ok=True, text=data.strip(), source=cap.name, elapsed=elapsed)
        return CapabilityResult(ok=True, text=str(data), source=cap.name, elapsed=elapsed)

    # ---------------------------------------------------------------- 限流
    def _allow(self, name: str, session_id: str) -> bool:
        cap = self.caps.get(name)
        limit = cap.rate_limit if cap and cap.rate_limit else self.config.int(
            "capability.rate_limit_per_min", 20
        )
        if limit <= 0:
            return True
        key = f"{name}:{session_id}" if session_id else name
        now = time.time()
        bucket = self._uses[key]
        cutoff = now - 60
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    # ---------------------------------------------------------------- 缓存
    async def _cache_get(self, cap: Capability, kwargs: dict[str, Any]) -> CapabilityResult | None:
        if cap.cache_ttl <= 0:
            return None
        key = self._cache_key(cap, kwargs)
        try:
            cached = await self.plugin.store.kv_get(key)
        except Exception:
            return None
        if not cached or not isinstance(cached, dict):
            return None
        if int(cached.get("expires_at") or 0) < int(time.time()):
            return None
        return CapabilityResult(
            ok=True,
            text=str(cached.get("text") or ""),
            data=cached.get("data"),
            source=cap.name,
            cached=True,
        )

    async def _cache_set(self, cap: Capability, kwargs: dict[str, Any], result: CapabilityResult) -> None:
        key = self._cache_key(cap, kwargs)
        try:
            await self.plugin.store.kv_set(
                key,
                {
                    "text": result.text,
                    "data": result.data,
                    "expires_at": int(time.time()) + cap.cache_ttl,
                },
            )
        except Exception:
            pass

    @staticmethod
    def _cache_key(cap: Capability, kwargs: dict[str, Any]) -> str:
        import hashlib

        payload = repr(sorted((str(k), str(v)) for k, v in kwargs.items()))
        digest = hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()[:16]
        return f"cap:{cap.name}:{digest}"

    def _record(self, name: str, result: CapabilityResult) -> None:
        stat = self._stats.setdefault(name, {"calls": 0, "errors": 0, "total_ms": 0.0})
        stat["calls"] = int(stat.get("calls", 0)) + 1
        if not result.ok:
            stat["errors"] = int(stat.get("errors", 0)) + 1
        stat["total_ms"] = float(stat.get("total_ms", 0.0)) + result.elapsed * 1000

    # ================================================================ LLM 工具
    def attach_tools(self, req: Any) -> None:
        """把能力挂到本轮请求的可用工具上。"""
        if not self.cfg_enabled() or not self.caps:
            return
        try:
            tool_set = getattr(req, "func_tool", None)
            if tool_set is None:
                from astrbot.core.agent.tool import ToolSet

                tool_set = ToolSet()
                req.func_tool = tool_set
            existing = {tool.name for tool in getattr(tool_set, "tools", [])}
            for cap in self.caps.values():
                tool_name = f"{TOOL_PREFIX}{cap.name.replace('.', '_')}"
                if tool_name in existing:
                    continue
                tool = self._make_tool(cap, tool_name)
                if tool is not None:
                    tool_set.add_tool(tool)
        except Exception as exc:
            logger.debug(f"[congling] 挂载工具失败: {exc!r}")

    def cfg_enabled(self) -> bool:
        return self.config.bool("capability.enabled", True)

    def _make_tool(self, cap: Capability, tool_name: str) -> Any:
        cached = self._tools.get(tool_name)
        if cached is not None:
            return cached
        try:
            from .tool import CapabilityTool

            schema = cap.parameters or {"type": "object", "properties": {}}
            tool = CapabilityTool(
                name=tool_name,
                description=cap.description,
                parameters=schema,
                registry=self,
                capability_name=cap.name,
                handler=None,
            )
            self._tools[tool_name] = tool
            return tool
        except Exception as exc:
            logger.debug(f"[congling] 构造工具 {tool_name} 失败: {exc!r}")
            return None

    def tools_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "name": f"{TOOL_PREFIX}{cap.name.replace('.', '_')}",
                "description": cap.description,
                "parameters": cap.parameters,
            }
            for cap in self.caps.values()
        ]
