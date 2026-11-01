"""管理员专属清除 API；身份只来自框架认证上下文，绝不信任请求体。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from astrbot.api.web import request

from ..memory.reset import MemoryReset


class MemoryResetMixin:
    def _memory_reset_admin(self) -> str:
        try:
            username = request.username
            config = self.plugin.context.get_config()
            dashboard = config.get("dashboard") if isinstance(config, Mapping) else None
            expected = dashboard.get("username") if isinstance(dashboard, Mapping) else None
            # AstrBot 的页面用户只有 dashboard 管理员；ApiKey/普通用户/无上下文拒绝。
            if not isinstance(username, str) or not username or username != expected:
                raise PermissionError
            return username
        except Exception:
            raise PermissionError("仅已登录的 AstrBot 面板管理员可执行此操作") from None

    async def memory_reset(self) -> dict[str, Any]:
        from . import _body, _err, _ok
        try:
            owner = self._memory_reset_admin()
            body = await _body()
            if not hasattr(self, "_reset_service"):
                self._reset_service = MemoryReset(self.plugin)
            return _ok(await self._reset_service.handle(owner, body.get("action"), body))
        except PermissionError as exc:
            return {**_err(str(exc)), "code": "forbidden"}
        except (ValueError, OSError) as exc:
            return _err(str(exc))
        except Exception:
            from astrbot.api import logger
            logger.exception("[congling] 全量记忆清除失败")
            return _err("清除失败，事务内操作已回滚；请重新开始并检查日志")
