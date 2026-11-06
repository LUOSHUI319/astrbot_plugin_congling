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
import random
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

    # ================================================ 主动浏览（预留，默认关）
    async def may_browse(self) -> bool:
        """能不能主动去刷好友动态。

        默认关。**空间有访客记录** —— 刷了谁的动态，对方「最近访客」里就能看到，
        所以这个能力只做开关、不挂定时器：什么时候用由调用方（主动找人、
        或者对方提到「我发了条说说」）自己决定，并且每天有次数上限兜底。
        """
        if not self.config.bool("qq_extra.qzone_feed_browse", False):
            return False
        if not self.available():
            return False
        cap = max(1, min(5, self.config.int("qq_extra.qzone_feed_browse_max", 1)))
        today = time.strftime("%Y-%m-%d")
        try:
            used = int(await self.plugin.store.kv_get(f"qzone:browse:{today}", 0) or 0)
        except Exception:
            used = 0
        return used < cap

    async def browse_timeline(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """主动刷一次好友动态，并用掉一次配额。

        返回空列表 = 没开 / 今天用完了 / 出错了，调用方按「什么都没看到」处理。
        """
        if not await self.may_browse():
            return []
        try:
            result = await self.timeline(limit=limit)
        except Exception as exc:
            logger.warning(f"[congling] 主动浏览好友动态失败: {exc!r}")
            return []
        if not result.get("ok"):
            return []
        key = f"qzone:browse:{time.strftime('%Y-%m-%d')}"
        try:
            used = int(await self.plugin.store.kv_get(key, 0) or 0)
            await self.plugin.store.kv_set(key, used + 1)
        except Exception:
            pass
        items = list(result.get("items") or [])
        # 刷过的东西要留下痕迹，不然下次提起来她一脸茫然
        try:
            from .feed_memory import FeedMemory

            await FeedMemory(self.plugin).remember(items)
        except Exception as exc:
            logger.warning(f"[congling] 空间动态写记忆失败: {exc!r}")
        # 随机挑一条回应（默认关，且每天有限额）
        try:
            await self.like_feed(items)
            await self.reply_to_feed(items)
        except Exception as exc:
            logger.warning(f"[congling] 空间互动失败: {exc!r}")
        return items

    # ================================================ 随机点赞（默认关）
    async def may_like_feed(self) -> bool:
        """能不能给别人点赞。默认关，每天有上限。

        点赞比评论轻（不占内容位、也说不错话），上限可以放宽到 5 条，
        但仍然是公开行为，刷到多少点多少会显得很假。
        """
        if not self.config.bool("qq_extra.qzone_feed_like", False):
            return False
        if not self.available() or not self.write_allowed():
            return False
        cap = max(1, min(5, self.config.int("qq_extra.qzone_feed_like_max", 2)))
        today = time.strftime("%Y-%m-%d")
        try:
            used = int(await self.plugin.store.kv_get(f"qzone:liked:{today}", 0) or 0)
        except Exception:
            used = 0
        return used < cap

    async def like_feed(self, items: list[dict[str, Any]], *, limit: int = 2) -> list[dict[str, Any]]:
        """随机点几个赞。返回实际点上的条目。"""
        if not await self.may_like_feed():
            return []
        try:
            auth = await self.auth()
        except Exception:
            return []
        me = int(auth.uin or 0)
        try:
            seen = list(await self.plugin.store.kv_get("qzone:liked_topics", []) or [])
        except Exception:
            seen = []
        seen_set = {str(x) for x in seen}

        pool = [
            item for item in (items or [])
            if item.get("unikey")
            and item.get("curkey")
            and item.get("fid")
            and int(item.get("uin") or 0) not in (0, me)      # 不给自己点赞
            and str(item.get("fid")) not in seen_set           # 点过的别再点
        ]
        if not pool:
            return []

        picks = random.sample(pool, min(max(1, int(limit)), len(pool)))
        client = QzoneClient(auth)
        done: list[dict[str, Any]] = []
        for item in picks:
            try:
                await client.like(
                    unikey=str(item["unikey"]),
                    curkey=str(item["curkey"]),
                    fid=str(item["fid"]),
                    abstime=int(item.get("created_at") or 0),
                    appid=str(item.get("appid") or "311"),
                    host_uin=int(item.get("uin") or 0),
                )
            except Exception as exc:
                logger.warning(f"[congling] 点赞没点上: {exc!r}")
                continue
            done.append({**item, "liked": True})
            seen_set.add(str(item["fid"]))
            key = f"qzone:liked:{time.strftime('%Y-%m-%d')}"
            try:
                used = int(await self.plugin.store.kv_get(key, 0) or 0)
                await self.plugin.store.kv_set(key, used + 1)
                await self.plugin.store.kv_set("qzone:liked_topics", list(seen_set)[-100:])
            except Exception:
                pass
        return done

    # ================================================ 随机回应（默认关）
    async def may_reply_feed(self) -> bool:
        """能不能在别人的说说下面留一句。

        默认关。评论是**公开**的：挂在别人说说下面，谁都能看见，
        所以上限压得比浏览更低（每天 1~3 条），而且必须调用方主动决定要不要用。
        """
        if not self.config.bool("qq_extra.qzone_feed_reply", False):
            return False
        if not self.available() or not self.write_allowed():
            return False
        cap = max(1, min(3, self.config.int("qq_extra.qzone_feed_reply_max", 1)))
        today = time.strftime("%Y-%m-%d")
        try:
            used = int(await self.plugin.store.kv_get(f"qzone:replied:{today}", 0) or 0)
        except Exception:
            used = 0
        return used < cap

    async def reply_to_feed(self, items: list[dict[str, Any]], *, limit: int = 1) -> list[dict[str, Any]]:
        """从刷到的动态里随机挑几条评一句。返回实际评上的条目。"""
        if not await self.may_reply_feed():
            return []
        try:
            auth = await self.auth()
        except Exception:
            return []
        me = int(auth.uin or 0)
        try:
            seen = list(await self.plugin.store.kv_get("qzone:replied_topics", []) or [])
        except Exception:
            seen = []
        seen_set = {str(x) for x in seen}

        # 只评别人发的、有 topic_id 的、内容够一句的、还没评过的
        pool = [
            item for item in (items or [])
            if item.get("topic_id")
            and int(item.get("uin") or 0) not in (0, me)
            and str(item.get("topic_id")) not in seen_set
            and len(str(item.get("content") or "").strip()) >= 8
        ]
        if not pool:
            return []

        picks = random.sample(pool, min(max(1, int(limit)), len(pool)))
        done: list[dict[str, Any]] = []
        for item in picks:
            line = await self._comment_line(item)
            if not line:
                continue
            try:
                client = QzoneClient(auth)
                await client.comment(
                    str(item["topic_id"]), line, host_uin=int(item.get("uin") or 0)
                )
            except Exception as exc:
                logger.warning(f"[congling] 评论没发出去: {exc!r}")
                continue
            done.append({**item, "comment": line})
            seen_set.add(str(item["topic_id"]))
            key = f"qzone:replied:{time.strftime('%Y-%m-%d')}"
            try:
                used = int(await self.plugin.store.kv_get(key, 0) or 0)
                await self.plugin.store.kv_set(key, used + 1)
                await self.plugin.store.kv_set(
                    "qzone:replied_topics", (list(seen_set))[-60:]
                )
            except Exception:
                pass
        return done

    async def _comment_line(self, item: dict[str, Any]) -> str:
        """让模型写一句很短的回应。写不出来就返回空。"""
        try:
            provider = self.plugin.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None:
            return ""
        nick = str(item.get("nickname") or "").strip()
        content = str(item.get("content") or "").strip()[:200]
        prompt = (
            "你在刷空间，看到一条别人发的说说。写一句很短的回应，像随手评论：\n"
            "1. 不超过 15 个字\n"
            "2. 口语，别客套、别问句、别用 emoji、结尾不要句号\n"
            "3. 只回应内容本身，不评价对方这个人\n"
            "4. 只输出这句话，不要任何前缀\n\n"
            f"说说（{nick}）：{content}"
        )
        try:
            resp = await provider.text_chat(prompt=prompt, contexts=[])
            line = str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:
            logger.debug(f"[congling] 生成评论失败: {exc!r}")
            return ""
        line = line.strip().strip("“”\"'").splitlines()[0] if line else ""
        return line[:30]

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
    async def publish(self, text: str, *, images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not self.available():
            return {"ok": False, "text": "空间功能关着呢"}
        if not self.write_allowed():
            return {"ok": False, "text": "发说说默认是关的，先去 QQ 页把写权限打开"}
        content = (text or "").strip()
        if not content:
            return {"ok": False, "text": "内容是空的"}

        try:
            return await self._publish_once(content, images=images)
        except QzoneAuthError:
            try:
                await self.auth(force=True)
                return await self._publish_once(content, images=images)
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

    async def _publish_once(
        self, content: str, *, images: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        auth = await self.auth()
        client = QzoneClient(auth)
        result = await client.publish(content, images=images)
        if not isinstance(result, dict) or result.get("ok") is not True:
            return {"ok": False, "text": "发布未确认成功"}
        self._last_error = ""
        from ..memory.perspective import remember_action
        import time
        import uuid

        shot = f"（配图 {result.get('images')} 张）" if result.get("images") else ""
        await remember_action(
            self.plugin, "qzone", str(result.get("fid") or uuid.uuid4().hex),
            f"我在{time.strftime('%Y-%m-%d %H:%M')}发了QQ空间说说{shot}：{content[:300]}",
            result={"fid": str(result.get("fid") or "")},
        )
        return {"ok": True, "text": "发出去了", "fid": result.get("fid", ""),
                "images": int(result.get("images") or 0)}

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
