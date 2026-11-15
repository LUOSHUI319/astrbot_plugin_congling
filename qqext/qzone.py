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
from ..core import clock
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
# 删说说的时间窗。更久的要人自己在手机上删——不是做不到，
# 是「删掉几个月前的一条说说」不该由一个工具悄悄完成。
DELETE_WINDOW_SECONDS = 6 * 3600


def within_delete_window(created_at: int, now: float | None = None) -> bool:
    """这条说说还在可以删的时间窗里吗。

    没时间戳时放行——外层还有「写权限」和「只能删自己的」两道，不必在这里卡死。
    """
    try:
        stamp = int(created_at or 0)
    except (TypeError, ValueError):
        return True
    if stamp <= 0:
        return True
    return ((now if now is not None else time.time()) - stamp) <= DELETE_WINDOW_SECONDS


def pick_default_target(raw: str, owner: str) -> str:
    """没指定看谁的时候，看谁。

    顺序：显式 target > 配好的 owner_qq > 空（空会交给 resolve 退回当前说话人）。

    有 owner 就不跟说话人走：群里谁喊一句「看看空间」，看的也是那个人，
    而不是那个群友——既是隐私问题，也不是想要的东西。
    """
    text = str(raw or "").strip()
    if text:
        return text
    who = str(owner or "").strip()
    return who if who.isdigit() else ""


# 中文情绪词 → 表情包库的分类名。表情包的 desc 是中文描述、category 是英文，
# 所以两样都往 pick() 里塞：中文词命中 desc，英文分类命中 category。
# 没命中任何词就不配图——「配图是情绪的标点」，没情绪就别硬配。
MOOD_WORDS: dict[str, tuple[str, ...]] = {
    "tired": ("累", "困", "疲惫", "没劲", "撑不住", "熬夜", "睡"),
    "happy": ("开心", "高兴", "笑", "舒服", "不错", "好耶", "爽"),
    "sad": ("难过", "丧", "失落", "想哭", "遗憾", "舍不得"),
    "cry": ("哭", "眼泪", "委屈"),
    "angry": ("生气", "气死", "烦", "火大", "受不了", "讨厌"),
    "dumb": ("无语", "离谱", "服了", "什么鬼", "离谱"),
    "surprised": ("惊", "没想到", "居然", "震惊"),
    "shy": ("害羞", "脸热", "不好意思", "脸红"),
    "love": ("喜欢", "爱", "心动", "想你"),
    "thank": ("谢谢", "感谢", "多谢"),
    "sigh": ("叹气", "唉", "算了", "摆烂"),
    "excited": ("激动", "兴奋", "期待"),
    "confused": ("不懂", "困惑", "懵", "什么情况", "看不懂"),
    "fear": ("怕", "吓", "慌"),
    "embarrassed": ("尴尬", "社死", "丢人"),
    "troll": ("呵呵", "阴阳", "乐"),
}


def mood_words(text: str) -> list[str]:
    """从正文里抠出情绪词，给表情包库检索用。

    返回的是「中文词 + 英文分类」混在一起：中文词去命中 desc，英文分类去命中
    category。一个都没命中就返回空列表，调用方据此决定不配图。
    """
    body = str(text or "")
    if not body:
        return []
    found: list[str] = []
    for category, words in MOOD_WORDS.items():
        hits = [word for word in words if word and word in body]
        if not hits:
            continue
        found.extend(hits)
        found.append(category)
    # 去重但保序
    seen: set[str] = set()
    out: list[str] = []
    for word in found:
        if word not in seen:
            seen.add(word)
            out.append(word)
    return out


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

    def behavior_active(self) -> bool:
        """主动行为总闸：只有 `intent` 放行。

        `off` 不是禁用空间，是禁用主动——工具照样能发、能评、能删，
        只是没有任何东西会自己发起。写错配置也按 `off`。
        """
        mode = str(self.config.str("qq_extra.qzone_behavior_mode", "off") or "off").strip().lower()
        return mode == "intent"

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
            # 默认对象：显式 target > owner_qq > 当前说话人
            target = pick_default_target(
                target, self.config.str("qq_extra.qzone_owner_qq", "") or ""
            )
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
        today = clock.fmt("%Y-%m-%d")
        try:
            used = int(await self.plugin.store.kv_get(f"qzone:browse:{today}", 0) or 0)
        except Exception:
            used = 0
        return used < cap

    async def browse_timeline(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """主动刷一次好友动态，并用掉一次配额。

        返回空列表 = 没开 / 今天用完了 / 出错了，调用方按「什么都没看到」处理。
        """
        # 主动行为总闸：off 的时候刷动态这件事本身就不该发生。
        # 顺手挡在这里，比在每个调用点各写一遍靠得住。
        if not self.behavior_active():
            return []
        if not await self.may_browse():
            return []
        try:
            result = await self.timeline(limit=limit)
        except Exception as exc:
            logger.warning(f"[congling] 主动浏览好友动态失败: {exc!r}")
            return []
        if not result.get("ok"):
            return []
        key = f"qzone:browse:{clock.fmt('%Y-%m-%d')}"
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
        today = clock.fmt("%Y-%m-%d")
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
            key = f"qzone:liked:{clock.fmt('%Y-%m-%d')}"
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
        today = clock.fmt("%Y-%m-%d")
        try:
            used = int(await self.plugin.store.kv_get(f"qzone:replied:{today}", 0) or 0)
        except Exception:
            used = 0
        return used < cap

    # ---------------------------------------------------------------- 单条（工具层用）
    async def like_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """给指定的那一条点赞。

        跟 like_feed 的区别：那个是「她自己刷到就随手点几个」，受 `qzone_feed_like`
        这个**主动开关**管；这个是「有人明确让我点这条」，所以只看写权限和去重，
        不看主动开关——否则用户说「给这条点个赞」，开关关着就点不了，很怪。

        这是公开行为，点完对方说说下面会显示她的赞。
        """
        if not self.write_allowed():
            return {"ok": False, "text": "空间写权限关着呢，先打开才能操作"}
        fid = str(item.get("fid") or "")
        unikey = str(item.get("unikey") or "")
        curkey = str(item.get("curkey") or "")
        if not (fid and unikey and curkey):
            return {
                "ok": False,
                "text": "这一条拿不到点赞要的参数（unikey 只在好友动态流里给，翻历史说说没有）",
            }
        try:
            auth = await self.auth()
        except Exception as exc:
            self._last_error = str(exc)[:200]
            return {"ok": False, "text": "还没拿到空间登录态"}
        if int(item.get("uin") or 0) == int(auth.uin or 0):
            return {"ok": False, "text": "这是你自己发的说说"}
        try:
            client = QzoneClient(auth)
            await client.like(
                unikey=unikey,
                curkey=curkey,
                fid=fid,
                abstime=int(item.get("created_at") or 0),
                appid=str(item.get("appid") or "311"),
                host_uin=int(item.get("uin") or 0),
            )
        except Exception as exc:
            self._last_error = repr(exc)[:200]
            logger.warning(f"[congling] 点赞失败: {exc!r}")
            return {"ok": False, "text": "赞没点上"}

        try:
            seen = list(await self.plugin.store.kv_get("qzone:liked_topics", []) or [])
            seen.append(fid)
            await self.plugin.store.kv_set("qzone:liked_topics", seen[-100:])
        except Exception:
            pass
        return {"ok": True, "text": "点了赞"}

    async def comment_item(self, item: dict[str, Any], text: str) -> dict[str, Any]:
        """在指定的那一条下面评论。同理，只看写权限，不看主动开关。

        评论是比点赞更重的公开行为：内容会挂在别人的说说下面，说完就撤不回。
        """
        if not self.write_allowed():
            return {"ok": False, "text": "空间写权限关着呢，先打开才能操作"}
        topic_id = str(item.get("topic_id") or "")
        if not topic_id:
            return {"ok": False, "text": "这一条拿不到 topic_id，评不了"}
        content = str(text or "").strip()
        if not content:
            return {"ok": False, "text": "评论内容是空的"}
        try:
            auth = await self.auth()
        except Exception as exc:
            self._last_error = str(exc)[:200]
            return {"ok": False, "text": "还没拿到空间登录态"}
        if int(item.get("uin") or 0) == int(auth.uin or 0):
            return {"ok": False, "text": "这是你自己发的说说"}
        try:
            client = QzoneClient(auth)
            await client.comment(topic_id, content, host_uin=int(item.get("uin") or 0))
        except Exception as exc:
            self._last_error = repr(exc)[:200]
            logger.warning(f"[congling] 评论失败: {exc!r}")
            return {"ok": False, "text": "评论没发出去"}

        try:
            seen = list(await self.plugin.store.kv_get("qzone:replied_topics", []) or [])
            seen.append(topic_id)
            await self.plugin.store.kv_set("qzone:replied_topics", seen[-200:])
        except Exception:
            pass
        return {"ok": True, "text": "评论发出去了"}

    async def own_feed(self, *, limit: int = 5, page: int = 1) -> dict[str, Any]:
        """只读自己（登录账号）的空间。

        跟 feed() 的区别：feed 的默认目标可能是 owner_qq（你），
        而「删说说」必须落在自己的号上，不能被默认目标带偏。
        """
        try:
            auth = await self.auth()
        except Exception as exc:
            self._last_error = str(exc)[:200]
            return {"ok": False, "text": "还没拿到空间登录态", "items": []}
        return await self.feed(
            limit=limit, page=page, target_uin=int(auth.uin or 0), target_label="你自己"
        )

    # ---------------------------------------------------------------- 详情 / 删 / 回评 / 访客
    async def detail_of(self, item: dict[str, Any]) -> dict[str, Any]:
        """读一条说说的详情，重点是评论列表。"""
        fid = str(item.get("fid") or "")
        if not fid:
            return {"ok": False, "text": "这一条拿不到 fid，读不了详情", "comments": []}
        try:
            auth = await self.auth()
        except Exception as exc:
            self._last_error = str(exc)[:200]
            return {"ok": False, "text": "还没拿到空间登录态", "comments": []}
        host = int(item.get("uin") or auth.uin or 0)
        try:
            client = QzoneClient(auth)
            data = await client.post_detail(host, fid)
        except Exception as exc:
            self._last_error = repr(exc)[:200]
            logger.warning(f"[congling] 读说说详情失败: {exc!r}")
            return {"ok": False, "text": "详情没读出来", "comments": []}
        data["nickname"] = str(item.get("nickname") or "")
        data["fid"] = fid
        return data

    async def delete_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """删自己的一条说说。

        **只允许 6 小时内的。** 再久就得人自己去手机上删——不是技术上做不到，
        是"删掉一条几个月前的说说"这种事不该由一个工具悄悄完成。
        """
        if not self.write_allowed():
            return {"ok": False, "text": "空间写权限关着呢"}
        fid = str(item.get("fid") or "")
        if not fid:
            return {"ok": False, "text": "这一条拿不到 fid，删不了"}
        created = int(item.get("created_at") or 0)
        if not within_delete_window(created):
            return {
                "ok": False,
                "text": "这条发出去超过 6 小时了，删它得你自己去手机上操作",
            }
        try:
            auth = await self.auth()
        except Exception as exc:
            self._last_error = str(exc)[:200]
            return {"ok": False, "text": "还没拿到空间登录态"}
        if int(item.get("uin") or 0) != int(auth.uin or 0):
            return {"ok": False, "text": "只能删自己发的说说"}
        try:
            client = QzoneClient(auth)
            await client.delete_post(
                fid, appid=int(item.get("appid") or 311), created_at=created
            )
        except Exception as exc:
            self._last_error = repr(exc)[:200]
            logger.warning(f"[congling] 删说说失败: {exc!r}")
            return {"ok": False, "text": "没删掉"}
        return {"ok": True, "text": "删掉了"}

    async def reply_comment_item(
        self, item: dict[str, Any], comment: dict[str, Any], text: str
    ) -> dict[str, Any]:
        """回复某条评论。跟 comment_item 不是一个动作：那是新开一条，这是接在别人话下面。"""
        if not self.write_allowed():
            return {"ok": False, "text": "空间写权限关着呢"}
        fid = str(item.get("fid") or "")
        comment_id = str((comment or {}).get("comment_id") or "")
        if not fid or not comment_id:
            return {"ok": False, "text": "缺 fid 或者评论 id，回不了"}
        content = str(text or "").strip()
        if not content:
            return {"ok": False, "text": "回复内容是空的"}
        try:
            auth = await self.auth()
        except Exception as exc:
            self._last_error = str(exc)[:200]
            return {"ok": False, "text": "还没拿到空间登录态"}
        host = int(item.get("uin") or auth.uin or 0)
        try:
            client = QzoneClient(auth)
            await client.reply_comment(
                host,
                fid,
                comment_id,
                int((comment or {}).get("uin") or 0),
                content,
                appid=int(item.get("appid") or 311),
            )
        except Exception as exc:
            self._last_error = repr(exc)[:200]
            logger.warning(f"[congling] 回评失败: {exc!r}")
            return {"ok": False, "text": "回复没发出去"}

        try:
            seen = list(await self.plugin.store.kv_get("qzone:replied_comments", []) or [])
            seen.append(comment_id)
            await self.plugin.store.kv_set("qzone:replied_comments", seen[-200:])
        except Exception:
            pass
        return {"ok": True, "text": "回复发出去了"}

    async def visitors(self, *, limit: int = 10) -> dict[str, Any]:
        """最近访客。

        **每天只允许看一次。** 这不是抠，是分寸：访客列表是双向可见的，
        你刷了谁的动态，对方在「最近访客」里就能看到你。
        """
        if not self.available():
            return {"ok": False, "text": "空间功能关着呢", "items": []}
        today = clock.fmt("%Y-%m-%d")
        key = f"qzone:visitors:{today}"
        try:
            used = int(await self.plugin.store.kv_get(key, 0) or 0)
        except Exception:
            used = 0
        if used:
            return {"ok": False, "text": "今天已经看过一次了，明天再看", "items": []}
        try:
            auth = await self.auth()
        except Exception as exc:
            self._last_error = str(exc)[:200]
            return {"ok": False, "text": "还没拿到空间登录态", "items": []}
        try:
            client = QzoneClient(auth)
            data = await client.visitors(count=max(5, min(int(limit or 10), 30)))
        except Exception as exc:
            self._last_error = repr(exc)[:200]
            logger.warning(f"[congling] 读访客失败: {exc!r}")
            return {"ok": False, "text": "访客没读出来", "items": []}
        try:
            await self.plugin.store.kv_set(key, 1)
        except Exception:
            pass
        items = list(data.get("items") or [])[: max(1, int(limit or 10))]
        return {"ok": True, "items": items, "text": f"最近有 {len(items)} 个人来过"}

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
            key = f"qzone:replied:{clock.fmt('%Y-%m-%d')}"
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
    async def publish(
        self,
        text: str,
        *,
        images: list[dict[str, Any]] | None = None,
        auto_image: bool = True,
    ) -> dict[str, Any]:
        if not self.available():
            return {"ok": False, "text": "空间功能关着呢"}
        if not self.write_allowed():
            return {"ok": False, "text": "发说说默认是关的，先去 QQ 页把写权限打开"}
        content = (text or "").strip()
        if not content:
            return {"ok": False, "text": "内容是空的"}

        # 没显式给图就自己找一张。配图是加分项，整条链路尽力而为：
        # 开关关着、库里没连上、没匹到情绪、上传失败——任何一步塌了都退回纯文本，
        # 绝不因为配不上图把整条说说卡死。
        if not images and auto_image:
            images = await self._meme_images(content)

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

    async def _meme_images(self, text: str) -> list[dict[str, Any]]:
        """按正文的情绪挑一张表情包，传上图床，返回能直接塞进 publish 的 images。

        挑不到、传不上，一律返回空列表 —— 上层会当纯文本发。
        """
        if not self.config.bool("qq_extra.qzone_image_from_meme", True):
            return []
        qqext = getattr(self.plugin, "qqext", None)
        sticker = getattr(qqext, "sticker", None) if qqext is not None else None
        if sticker is None:
            return []
        moods = mood_words(text)
        if not moods:
            # 正文里没有情绪词就不配图。「配图是情绪的标点」，硬配反而怪。
            return []
        try:
            picked = await sticker.pick_bytes(moods)
        except Exception as exc:
            logger.debug(f"[congling] 挑表情包失败: {exc!r}")
            return []
        if not picked:
            return []
        data, suffix = picked
        try:
            auth = await self.auth()
            client = QzoneClient(auth)
            info = await client.upload_image(data, filename=f"meme{suffix}")
        except Exception as exc:
            logger.debug(f"[congling] 表情包上传失败: {exc!r}")
            return []
        if not info:
            return []
        logger.info(f"[congling] 说说配图：{moods[0]} → 一张表情包")
        return [info]

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
            f"我在{clock.fmt('%Y-%m-%d %H:%M')}发了QQ空间说说{shot}：{content[:300]}",
            result={"fid": str(result.get("fid") or "")},
        )
        # 记进「自知表」：下次 she 想发的时候，这句话会作为反重复的依据出现
        try:
            from .qzone_hint import remember_post

            await remember_post(
                self.plugin, content, fid=str(result.get("fid") or ""), source="manual"
            )
        except Exception:
            pass
        return {"ok": True, "text": "发出去了", "fid": result.get("fid", ""),
                "images": int(result.get("images") or 0)}

    # ================================================================ 自动发
    async def maybe_auto_post(self, content: str) -> bool:
        # 主动行为总闸：off 时她不会自己发。这个方法目前没有调用点，
        # 但留着这道判断，免得哪天接回来又绕过总闸。
        if not self.behavior_active():
            return False
        if not self.config.bool("qq_extra.qzone_auto_post", False):
            return False
        today = clock.fmt("%Y-%m-%d")
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
