"""QQ 空间桥。

三种模式：
- auto（默认）：本插件自己打空间接口，登录态从 OneBot 客户端里拿
- builtin：同 auto，但即使检测到别的空间插件也自己来
- off：完全不碰空间

登录态来源优先级：设置里手填的 cookie > 持久化的上一次 cookie > 现场从 OneBot 拉。
失效时会自动重拉一次再重试，还不行就诚实报错，不假装成功。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

from astrbot.api import logger
from ..core.memory_guard import memory_work

from .targets import TargetResolver
from .qzone_client import (
    QzoneAuth,
    QzoneAuthError,
    QzoneClient,
    QzoneRequestError,
    extract_cookies,
    has_login_cookie,
    uin_from_cookies,
)

QZONE_PLUGIN = "astrbot_plugin_qzone_ultra"
AUTH_KV_KEY = "qzone:auth"
COOKIE_ACTION_TIMEOUT = 6.0


class QzoneBridge:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self._auth: QzoneAuth | None = None
        self._last_error = ""
        self.targets = TargetResolver(plugin)

    # ================================================================ 状态
    def mode(self) -> str:
        configured = str(self.config.get("qq_extra.qzone_mode", "auto") or "auto")
        # 老配置里的 reuse 没有真正生效过，统一当 auto
        if configured in {"reuse"}:
            return "auto"
        if configured not in {"auto", "builtin", "off"}:
            return "auto"
        return configured

    def available(self) -> bool:
        return self.mode() != "off"

    def write_allowed(self) -> bool:
        return self.config.bool("qq_extra.qzone_allow_write", False)

    def _external_present(self) -> bool:
        try:
            return bool(self.plugin.probe.present(QZONE_PLUGIN))
        except Exception:
            return False

    async def status(self) -> dict[str, Any]:
        if not self.available():
            return {
                "mode": self.mode(),
                "available": False,
                "write_allowed": self.write_allowed(),
                "auth": {"ok": False},
                "external": self._external_present(),
                "note": "空间功能关着呢",
            }
        native = await self._native_action("get_qzone_profile")
        if native is not None:
            return {
                "mode": self.mode(),
                "available": True,
                "write_allowed": self.write_allowed(),
                "auth": {"ok": True, "source": "onebot-native"},
                "external": self._external_present(),
                "note": "",
                "last_error": "",
            }
        try:
            auth = await self.auth()
            summary = auth.summary()
            note = "" if summary["ok"] else "还没拿到空间登录态"
        except Exception as exc:
            summary = {"ok": False}
            note = str(exc)[:120]
        return {
            "mode": self.mode(),
            "available": True,
            "write_allowed": self.write_allowed(),
            "auth": summary,
            "external": self._external_present(),
            "note": note,
            "last_error": self._last_error,
        }

    # ================================================================ 登录态
    async def auth(self, *, force: bool = False) -> QzoneAuth:
        if self._auth is not None and self._auth.usable and not force:
            return self._auth

        # 1) 设置里手填的
        manual = str(self.config.get("qq_extra.qzone_cookie", "") or "").strip()
        if manual:
            from .qzone_client import parse_cookie_text

            cookies = parse_cookie_text(manual)
            if has_login_cookie(cookies) and uin_from_cookies(cookies):
                self._auth = QzoneAuth(cookies, source="manual")
                return self._auth

        # 2) 上次存下来的
        if not force:
            saved = await self._load_saved()
            if saved is not None and saved.usable:
                self._auth = saved
                return saved

        # 3) 现场从 OneBot 拉
        fetched = await self._fetch_from_onebot()
        self._auth = fetched
        await self._save(fetched)
        return fetched

    async def refresh(self) -> dict[str, Any]:
        try:
            auth = await self.auth(force=True)
            return {"ok": True, "auth": auth.summary()}
        except Exception as exc:
            self._last_error = str(exc)[:200]
            return {"ok": False, "text": str(exc)[:200]}

    async def _load_saved(self) -> QzoneAuth | None:
        try:
            raw = await self.plugin.store.kv_get(AUTH_KV_KEY)
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return QzoneAuth.from_dict(raw)
        except Exception:
            return None

    async def _save(self, auth: QzoneAuth) -> None:
        try:
            await self.plugin.store.kv_set(AUTH_KV_KEY, auth.to_dict())
        except Exception as exc:
            logger.debug(f"[congling] 空间登录态没存上: {exc!r}")

    # ---------------------------------------------------------------- OneBot
    def _onebot_bot(self) -> Any:
        try:
            instances = self.plugin.context.platform_manager.platform_insts
        except Exception:
            return None
        for platform in instances or []:
            bot = getattr(platform, "bot", None)
            if bot is None:
                continue
            name = ""
            meta = getattr(platform, "meta", None)
            if callable(meta):
                try:
                    name = str(meta().name)
                except Exception:
                    name = ""
            if "aiocqhttp" in name or callable(getattr(bot, "call_action", None)):
                return bot
        return None

    @staticmethod
    async def _call_action(bot: Any, action: str, **params: Any) -> Any:
        method = getattr(bot, "call_action", None)
        if not callable(method):
            return None
        result = method(action, **params)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _native_action(self, action: str, **params: Any) -> dict[str, Any] | None:
        """Call a Qzone action exposed directly by newer OneBot clients."""
        bot = self._onebot_bot()
        if bot is None:
            return None
        try:
            payload = await asyncio.wait_for(
                self._call_action(bot, action, **params),
                timeout=COOKIE_ACTION_TIMEOUT,
            )
        except Exception as exc:
            logger.debug(f"[congling] OneBot 原生空间动作 {action} 不可用: {exc!r}")
            if action == "send_qzone_msg":
                # 发送超时/断线不代表未发布，不能再用 HTTP 把同一条重发。
                raise QzoneRequestError("原生空间发布结果未知，请先查看空间确认") from exc
            return None
        if not isinstance(payload, dict):
            if action == "send_qzone_msg":
                raise QzoneRequestError("原生空间发布返回未知结果，请先查看空间确认")
            return None
        if payload.get("status") == "failed":
            return None
        retcode = payload.get("retcode")
        if isinstance(retcode, int) and retcode != 0:
            return None
        data = payload.get("data") if "data" in payload else payload
        return data if isinstance(data, dict) else None

    @staticmethod
    def _native_msg_items(payload: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        rows = payload.get("msglist")
        if not isinstance(rows, list):
            return []
        items: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            created = int(row.get("time") or 0)
            items.append(
                {
                    "fid": str(row.get("tid") or ""),
                    "appid": 311,
                    "uin": int(row.get("uin") or 0),
                    "nickname": str(row.get("name") or ""),
                    "content": str(row.get("content") or "")[:800],
                    "created_at": created,
                    "created_text": (
                        time.strftime("%m-%d %H:%M", time.localtime(created)) if created else ""
                    ),
                    "comment_count": int(row.get("comment_num") or 0),
                    "like_count": int(row.get("like_num") or 0),
                    "images": list(row.get("images") or [])[:9],
                }
            )
            if len(items) >= max(1, int(limit)):
                break
        return items

    async def _login_uin(self, bot: Any) -> int:
        try:
            info = await asyncio.wait_for(
                self._call_action(bot, "get_login_info"), timeout=COOKIE_ACTION_TIMEOUT
            )
        except Exception:
            return 0
        if isinstance(info, dict):
            for key in ("user_id", "uin", "self_id"):
                value = info.get(key)
                if value and str(value).isdigit():
                    return int(value)
        return 0

    async def _fetch_from_onebot(self) -> QzoneAuth:
        bot = self._onebot_bot()
        if bot is None:
            raise QzoneAuthError("没找到 OneBot 客户端，拿不到空间登录态")

        for action in ("get_cookies", "get_credentials"):
            for domain in ("user.qzone.qq.com", ""):
                kwargs = {"domain": domain} if domain else {}
                try:
                    payload = await asyncio.wait_for(
                        self._call_action(bot, action, **kwargs),
                        timeout=COOKIE_ACTION_TIMEOUT,
                    )
                except Exception as exc:
                    logger.debug(f"[congling] {action}({domain}) 失败: {exc!r}")
                    continue
                cookies = extract_cookies(payload)
                if not cookies or not has_login_cookie(cookies):
                    continue
                if not uin_from_cookies(cookies):
                    uin = await self._login_uin(bot)
                    if uin:
                        cookies["uin"] = f"o{uin}"
                        cookies["p_uin"] = f"o{uin}"
                auth = QzoneAuth(cookies, source=f"onebot:{action}")
                if auth.usable:
                    logger.info(f"[congling] 已从 OneBot 取到空间登录态（uin={auth.uin}）")
                    return auth
        raise QzoneAuthError(
            "协议端没给出空间 cookie，确认一下客户端是 NapCat/Lagrange 且已登录"
        )

    # ================================================================ 读
    async def feed(
        self,
        *,
        limit: int = 10,
        page: int = 1,
        target: str = "",
        target_uin: int = 0,
        target_label: str = "",
        event: Any = None,
    ) -> dict[str, Any]:
        """读某个人的空间。

        target_uin 没给时：用 target 文本解析（"我"、QQ 号、昵称），再退到说话人自己。
        """
        if not self.available():
            return {"ok": False, "text": "空间功能关着呢", "items": []}

        # SnowLuma and other newer clients expose Qzone directly. Prefer that
        # path because their in-process session remains valid even when the
        # browser-style cookie returned by get_cookies cannot access Qzone.
        native_target = int(target_uin or 0)
        if not native_target and event is None and target.strip() in {"", "我", "我的", "自己", "me"}:
            native = await self._native_action(
                "get_qzone_msg_list",
                pos=max(0, (max(1, int(page)) - 1) * max(1, int(limit))),
                num=max(1, int(limit)),
            )
            if native is not None:
                items = self._native_msg_items(native, limit)
                return {
                    "ok": True,
                    "items": items,
                    "text": (
                        f"你自己的空间：{self._render_feed(items)}"
                        if items
                        else "你自己的空间最近没动静"
                    ),
                    "source": "onebot",
                }
        elif native_target:
            native = await self._native_action(
                "get_qzone_msg_list",
                target_uin=native_target,
                pos=max(0, (max(1, int(page)) - 1) * max(1, int(limit))),
                num=max(1, int(limit)),
            )
            if native is not None:
                items = self._native_msg_items(native, limit)
                who = target_label or str(native_target)
                return {
                    "ok": True,
                    "items": items,
                    "text": (
                        f"{who}的空间：{self._render_feed(items)}"
                        if items
                        else f"{who}的空间最近没动静"
                    ),
                    "source": "onebot",
                }
        try:
            auth = await self.auth()
        except Exception as exc:
            self._last_error = str(exc)[:200]
            return {"ok": False, "text": str(exc)[:200], "items": []}

        if not target_uin:
            resolution = await self.targets.resolve(target, event)
            if resolution.ok:
                target_uin = resolution.uin
                target_label = resolution.label or str(target_uin)
            elif event is None and target.strip() in {"", "我", "我的", "自己", "me"}:
                # 页面里没有消息上下文，直接按登录账号算
                target_uin = auth.uin
                target_label = "你自己"
            else:
                return {"ok": False, "text": resolution.error, "items": []}

        if target_uin != auth.uin and not self.config.bool("qq_extra.qzone_read_others", True):
            return {
                "ok": False,
                "text": "看别人的空间这个开关是关的，只能看自己的",
                "items": [],
            }

        who = target_label or ("你自己" if target_uin == auth.uin else str(target_uin))
        try:
            result = await self._feed_once(limit=limit, page=page, target_uin=target_uin)
            result["target"] = {"uin": target_uin, "label": who}
            result["text"] = f"{who}的空间：{result['text']}" if result.get("items") else f"{who}的空间最近没动静"
            return result
        except QzoneAuthError:
            try:
                await self.auth(force=True)
                result = await self._feed_once(limit=limit, page=page, target_uin=target_uin)
                result["target"] = {"uin": target_uin, "label": who}
                return result
            except Exception as exc:
                self._last_error = str(exc)[:200]
                return {"ok": False, "text": str(exc)[:200], "items": []}
        except QzoneRequestError as exc:
            self._last_error = str(exc)[:200]
            return {"ok": False, "text": str(exc)[:200], "items": []}
        except Exception as exc:
            self._last_error = repr(exc)[:200]
            logger.warning(f"[congling] 读空间失败: {exc!r}")
            return {"ok": False, "text": "读空间出错了，稍后再试", "items": []}

    async def _feed_once(self, *, limit: int, page: int, target_uin: int) -> dict[str, Any]:
        auth = await self.auth()
        client = QzoneClient(auth)
        items = await client.user_feeds(target_uin, page=page, limit=limit)
        self._last_error = ""
        return {
            "ok": True,
            "items": items,
            "text": self._render_feed(items),
            "auth": auth.summary(),
        }

    async def timeline(self, *, limit: int = 10, page: int = 1) -> dict[str, Any]:
        """好友动态流（所有人最近发的），和「看某人空间」不是一回事。"""
        auth = await self.auth()
        items = await QzoneClient(auth).feed(page=page, limit=limit)
        return {"ok": True, "items": items, "text": self._render_feed(items)}

    @staticmethod
    def _render_feed(items: list[dict[str, Any]]) -> str:
        if not items:
            return "最近空间没什么动静"
        lines: list[str] = []
        for item in items[:5]:
            content = str(item.get("content") or "").replace("\n", " ").strip()
            name = str(item.get("nickname") or "").strip()
            if not content:
                continue
            body = content[:60] + ("…" if len(content) > 60 else "")
            lines.append(f"{name}：{body}" if name else body)
        return "；".join(lines) or "没读到内容"

    # ================================================================ 写
    @memory_work
    async def publish(self, text: str) -> dict[str, Any]:
        if not self.available():
            return {"ok": False, "text": "空间功能关着呢"}
        if not self.write_allowed():
            return {"ok": False, "text": "发说说默认是关的，先去 QQ 页把写权限打开"}
        content = (text or "").strip()
        if not content:
            return {"ok": False, "text": "内容是空的"}

        try:
            native = await self._native_action("send_qzone_msg", content=content, ugc_right=1)
        except QzoneRequestError as exc:
            self._last_error = str(exc)
            return {"ok": False, "text": str(exc)}
        if native is not None:
            fid = str(native.get("tid") or "")
            if not fid:
                return {"ok": False, "text": "原生发布未确认成功，请先查看空间"}
            self._last_error = ""
            from ..memory.perspective import remember_action
            await remember_action(
                self.plugin, "qzone", fid,
                f"我在{time.strftime('%Y-%m-%d %H:%M')}发了QQ空间说说：{content[:300]}",
                result={"fid": fid, "source": "onebot"},
            )
            return {
                "ok": True,
                "text": "发出去了",
                "fid": fid,
                "source": "onebot",
            }

        try:
            return await self._publish_once(content)
        except QzoneAuthError:
            try:
                await self.auth(force=True)
                return await self._publish_once(content)
            except Exception as exc:
                self._last_error = str(exc)[:200]
                return {"ok": False, "text": str(exc)[:200]}
        except QzoneRequestError as exc:
            self._last_error = str(exc)[:200]
            return {"ok": False, "text": str(exc)[:200]}
        except Exception as exc:
            self._last_error = repr(exc)[:200]
            logger.warning(f"[congling] 发说说失败: {exc!r}")
            return {"ok": False, "text": "发布出错了，稍后再试"}

    async def _publish_once(self, content: str) -> dict[str, Any]:
        auth = await self.auth()
        client = QzoneClient(auth)
        result = await client.publish(content)
        if not isinstance(result, dict) or result.get("ok") is not True:
            return {"ok": False, "text": "发布未确认成功"}
        self._last_error = ""
        from ..memory.perspective import remember_action
        import time
        import uuid

        await remember_action(
            self.plugin, "qzone", str(result.get("fid") or uuid.uuid4().hex),
            f"我在{time.strftime('%Y-%m-%d %H:%M')}发了QQ空间说说：{content[:300]}",
            result={"fid": str(result.get("fid") or "")},
        )
        return {"ok": True, "text": "发出去了", "fid": result.get("fid", "")}

    # ================================================================ 自动发
    async def maybe_auto_post(self, content: str) -> bool:
        if not self.config.bool("qq_extra.qzone_auto_post", False):
            return False
        today = time.strftime("%Y-%m-%d")
        key = f"qzone:auto:{today}"
        try:
            done = await self.plugin.store.kv_get(key)
        except Exception:
            done = None
        if done:
            return False
        result = await self.publish(content)
        if result.get("ok"):
            try:
                await self.plugin.store.kv_set(key, True)
            except Exception:
                pass
        return bool(result.get("ok"))
