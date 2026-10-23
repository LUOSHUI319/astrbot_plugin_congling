"""QQ 空间 HTTP 客户端（内置实现）。

不依赖其它空间插件，直接用 OneBot 客户端里的登录态打空间接口。
协议细节对照 astrbot_plugin_qzone_ultra：

- cookie：OneBot 的 ``get_cookies`` / ``get_credentials``
- gt=hash33(skey, 5381)
- 读动态：ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more
- 发说说：taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6

这里的函数只负责"把请求发出去、把结果解释成人话"，不碰插件状态。
"""

from __future__ import annotations

import html as html_lib
import json
import re
import time
from typing import Any

from astrbot.api import logger

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

FEED_URL = (
    "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
)
PUBLISH_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
)
# 读"某个人"的说说列表（含别人的主页动态），比 timeline 精准
USER_FEEDS_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
)

COOKIE_SECRET_KEYS = ("p_skey", "skey", "pskey", "skey2")
COOKIE_KEY_ALIASES = {
    "p_skey": "p_skey",
    "pskey": "p_skey",
    "gtk": "g_tk",
    "bkn": "g_tk",
    "csrf_token": "g_tk",
    "skey2": "skey2",
}
AUTH_COOKIE_KEYS = ("skey", "p_skey", "pskey")

MAX_POST_CHARS = 1000


class QzoneAuthError(RuntimeError):
    """登录态拿不到或已失效。"""


class QzoneRequestError(RuntimeError):
    """接口本身出错。"""


# --------------------------------------------------------------------- 基础
def hash33(key: str, phash: int = 5381) -> int:
    for char in key:
        phash += (phash << 5) + ord(char)
    return 0x7FFFFFFF & phash


def gtk_of(cookies: dict[str, str]) -> int:
    for key in COOKIE_SECRET_KEYS:
        value = cookies.get(key)
        if value:
            return hash33(value, phash=5381)
    for key in ("g_tk", "gtk", "bkn", "csrf_token"):
        value = cookies.get(key)
        if value and str(value).strip().isdigit():
            return int(str(value).strip())
    return 0


def normalize_cookie_fields(cookies: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in cookies.items():
        if value in (None, ""):
            continue
        name = str(key).strip()
        if not name:
            continue
        text = str(value).strip().strip('"')
        if not text:
            continue
        canonical = COOKIE_KEY_ALIASES.get(name.lower().replace("-", "_"), name)
        normalized.setdefault(name, text)
        normalized.setdefault(canonical, text)
    if "uin" in normalized and "p_uin" not in normalized:
        normalized["p_uin"] = normalized["uin"]
    if "p_uin" in normalized and "uin" not in normalized:
        normalized["uin"] = normalized["p_uin"]
    return normalized


def parse_cookie_text(text: str) -> dict[str, str]:
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
        except Exception:
            return {}
        if isinstance(payload, dict):
            return normalize_cookie_fields(payload)
        return {}
    text = text.replace("\r", ";").replace("\n", ";")
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1]
    cookies: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        if key:
            cookies[key] = value
    return normalize_cookie_fields(cookies)


def extract_cookies(payload: Any, depth: int = 0) -> dict[str, str]:
    """从 OneBot 各种形状的返回里把 cookie 抠出来。"""
    if payload is None or depth > 6:
        return {}
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8", "ignore")
        except Exception:
            return {}
    if isinstance(payload, str):
        return parse_cookie_text(payload)
    if isinstance(payload, (list, tuple)):
        merged: dict[str, str] = {}
        for item in payload:
            merged.update(extract_cookies(item, depth + 1))
        return merged
    if not isinstance(payload, dict):
        return {}

    # {"name": "uin", "value": "o123"} 这种单条形式
    name = payload.get("name")
    value = payload.get("value")
    if isinstance(name, str) and isinstance(value, (str, int)) and "=" not in str(name):
        return normalize_cookie_fields({name: value})

    flat = normalize_cookie_fields(
        {
            str(key): val
            for key, val in payload.items()
            if isinstance(val, (str, int, float)) and "=" not in str(key)
        }
    )
    # 顶层就是 cookie 本体（有登录键）才直接收，否则先往里钻
    if has_login_cookie(flat):
        return flat

    for key in ("cookies", "cookie", "data", "result", "retdata", "payload", "response"):
        if key in payload:
            found = extract_cookies(payload[key], depth + 1)
            if found and has_login_cookie(found):
                return found

    if flat:
        return flat
    for key in ("cookies", "cookie", "data", "result", "retdata", "payload", "response"):
        if key in payload:
            found = extract_cookies(payload[key], depth + 1)
            if found:
                return found
    return {}


def uin_from_cookies(cookies: dict[str, str]) -> int:
    for key in ("uin", "p_uin", "ptui_loginuin", "luin"):
        value = cookies.get(key)
        if not value:
            continue
        digits = str(value).strip().lstrip("oO")
        if digits.isdigit():
            return int(digits)
    return 0


def has_login_cookie(cookies: dict[str, str]) -> bool:
    return any(cookies.get(key) for key in AUTH_COOKIE_KEYS)


def strip_html(raw: str) -> str:
    text = str(raw or "")
    if not text:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(
        r"</?(?:b|i|em|strong|span|font|a|u|s|mark|small|label)\b[^>]*>", "", text, flags=re.I
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(div|p|li)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# --------------------------------------------------------------------- 登录态
class QzoneAuth:
    """一份可用的空间登录态。"""

    def __init__(
        self,
        cookies: dict[str, str] | None = None,
        *,
        source: str = "",
        refreshed_at: int = 0,
    ) -> None:
        self.cookies = normalize_cookie_fields(cookies or {})
        self.source = source
        self.refreshed_at = int(refreshed_at or (time.time() if self.cookies else 0))

    @property
    def uin(self) -> int:
        return uin_from_cookies(self.cookies)

    @property
    def gtk(self) -> int:
        return gtk_of(self.cookies)

    @property
    def usable(self) -> bool:
        return bool(self.cookies) and has_login_cookie(self.cookies) and self.uin > 0

    def header(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self.cookies.items())

    def to_dict(self) -> dict[str, Any]:
        return {
            "cookies": self.cookies,
            "source": self.source,
            "refreshed_at": self.refreshed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QzoneAuth":
        if not isinstance(data, dict):
            return cls()
        return cls(
            data.get("cookies") if isinstance(data.get("cookies"), dict) else {},
            source=str(data.get("source") or ""),
            refreshed_at=int(data.get("refreshed_at") or 0),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.usable,
            "uin": self.uin,
            "source": self.source,
            "refreshed_at": self.refreshed_at,
            "refreshed_text": (
                time.strftime("%m-%d %H:%M", time.localtime(self.refreshed_at))
                if self.refreshed_at
                else ""
            ),
            "fingerprint": f"{self.uin}_{self.gtk}" if self.cookies else "",
        }


# --------------------------------------------------------------------- 客户端
class QzoneClient:
    def __init__(self, auth: QzoneAuth, *, timeout: float = 15.0) -> None:
        self.auth = auth
        self.timeout = timeout

    def _headers(self, referer: str) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Cookie": self.auth.header(),
            "Referer": referer,
            "Origin": "https://user.qzone.qq.com",
            "Accept": "application/json, text/plain, */*",
        }

    async def _get_json(self, url: str, params: dict[str, Any], referer: str) -> dict[str, Any]:
        import aiohttp

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        ) as session:
            async with session.get(url, params=params, headers=self._headers(referer)) as resp:
                body = await resp.text(errors="ignore")
                if resp.status != 200:
                    raise QzoneRequestError(f"HTTP {resp.status}")
        return self._parse(body)

    async def _post_form(
        self, url: str, params: dict[str, Any], data: dict[str, Any], referer: str
    ) -> dict[str, Any]:
        import aiohttp

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        ) as session:
            async with session.post(
                url, params=params, data=data, headers=self._headers(referer)
            ) as resp:
                body = await resp.text(errors="ignore")
                if resp.status != 200:
                    raise QzoneRequestError(f"HTTP {resp.status}")
        return self._parse(body)

    @staticmethod
    def _parse(body: str) -> dict[str, Any]:
        text = (body or "").strip()
        if not text:
            raise QzoneRequestError("接口没返回内容")
        # 有些接口回 JSONP：_Callback({...});
        if not text.startswith("{") and "(" in text[:40]:
            inner = text[text.find("(") + 1 : text.rfind(")")]
            text = inner.strip()
        try:
            payload = json.loads(text)
        except Exception as exc:
            if "登录" in text[:400] or "ptlogin" in text[:400]:
                raise QzoneAuthError("登录态失效了，要重新拿 cookie") from exc
            raise QzoneRequestError(f"返回的不是 JSON：{text[:80]}") from exc
        if not isinstance(payload, dict):
            raise QzoneRequestError("返回结构不认识")
        return payload

    # ----------------------------------------------------------------- 读
    async def user_feeds(self, uin: int, *, page: int = 1, limit: int = 10) -> list[dict[str, Any]]:
        """读指定用户空间里的说说列表。

        这个接口（emotion_cgi_msglist_v6）既能读自己也能读别人，
        比好友动态流更适合回答「看看某人的空间」。
        """
        if not self.auth.usable:
            raise QzoneAuthError("还没有可用的空间登录态")
        target = int(uin or 0)
        if target <= 0:
            raise QzoneRequestError("没识别出要看谁的 QQ 号")
        payload = await self._get_json(
            USER_FEEDS_URL,
            {
                "uin": target,
                "hostUin": target,
                "pos": max(0, (max(1, int(page)) - 1) * max(1, int(limit))),
                "num": max(1, int(limit)),
                "replynum": 50,
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "json",
                "need_comment": 0,
                "need_private_comment": 0,
                "g_tk": self.auth.gtk,
            },
            f"https://user.qzone.qq.com/{target}",
        )
        self._check_code(payload)
        return self._parse_msglist(payload, limit)

    @staticmethod
    def _parse_msglist(payload: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        rows = payload.get("msglist")
        if rows is None:
            data = payload.get("data")
            if isinstance(data, dict):
                rows = data.get("msglist") or data.get("list")
        if not isinstance(rows, list):
            rows = []
        items: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            content = strip_html(str(row.get("content") or ""))
            if not content:
                continue
            created = row.get("created_time") or row.get("createTime") or 0
            try:
                created = int(created)
            except (TypeError, ValueError):
                created = 0
            images = []
            for pic in row.get("pic") or []:
                if isinstance(pic, dict):
                    url = pic.get("url1") or pic.get("url2") or pic.get("picId")
                    if isinstance(url, str) and url.startswith("http"):
                        images.append(url)
            items.append(
                {
                    "fid": str(row.get("tid") or row.get("cellid") or ""),
                    "appid": int(row.get("appid") or 311),
                    "uin": int(row.get("uin") or 0),
                    "nickname": strip_html(str(row.get("name") or "")),
                    "content": content[:800],
                    "created_at": created,
                    "created_text": (
                        time.strftime("%m-%d %H:%M", time.localtime(created)) if created else ""
                    ),
                    "comment_count": int(row.get("cmtnum") or 0),
                    "like_count": int(row.get("likenum") or 0),
                    "images": images[:9],
                }
            )
            if len(items) >= max(1, int(limit)):
                break
        return items

    async def feed(self, *, page: int = 1, limit: int = 10) -> list[dict[str, Any]]:
        if not self.auth.usable:
            raise QzoneAuthError("还没有可用的空间登录态")
        uin = self.auth.uin
        payload = await self._get_json(
            FEED_URL,
            {
                "uin": uin,
                "scope": 0,
                "view": 1,
                "filter": "all",
                "flag": 1,
                "applist": "all",
                "pagenum": max(1, int(page)),
                "aisortEndTime": 0,
                "aisortOffset": 0,
                "aisortBeginTime": 0,
                "begintime": 0,
                "format": "json",
                "useutf8": 1,
                "outputhtmlfeed": 1,
                "g_tk": self.auth.gtk,
            },
            f"https://user.qzone.qq.com/{uin}",
        )
        self._check_code(payload)
        return self._parse_feed(payload, limit)

    def _check_code(self, payload: dict[str, Any]) -> None:
        code = payload.get("code")
        subcode = payload.get("subcode")
        message = str(payload.get("message") or payload.get("msg") or "")
        if code in (-3000, 1000) or "登录" in message:
            raise QzoneAuthError("登录态失效了，要重新拿 cookie")
        if isinstance(code, int) and code not in (0,):
            raise QzoneRequestError(message or f"接口返回 code={code}")
        if isinstance(subcode, int) and subcode not in (0,):
            raise QzoneRequestError(message or f"接口返回 subcode={subcode}")

    @staticmethod
    def _parse_feed(payload: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        container = payload.get("data")
        rows = []
        if isinstance(container, dict):
            for key in ("data", "feeds", "feedpage", "main"):
                value = container.get(key)
                if isinstance(value, list):
                    rows = value
                    break
        elif isinstance(container, list):
            rows = container
        if not rows and isinstance(payload.get("data"), list):
            rows = payload["data"]

        items: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            content = strip_html(str(row.get("content") or row.get("summary") or ""))
            if not content:
                content = strip_html(str(row.get("html") or ""))
                # 去掉渲染出来的昵称/时间/评论尾缀，保留正文
                content = re.sub(r"\s*\n\s*", "\n", content).strip()
            if not content:
                continue
            created = row.get("created_time") or row.get("createtime") or row.get("abstime") or 0
            try:
                created = int(created)
            except (TypeError, ValueError):
                created = 0
            items.append(
                {
                    "fid": str(row.get("fid") or row.get("cellid") or ""),
                    "appid": int(row.get("appid") or 311),
                    "uin": int(row.get("uin") or 0),
                    "nickname": strip_html(str(row.get("nickname") or "")),
                    "content": content[:800],
                    "created_at": created,
                    "created_text": (
                        time.strftime("%m-%d %H:%M", time.localtime(created)) if created else ""
                    ),
                    "comment_count": int(row.get("cmtnum") or 0),
                    "like_count": int(
                        (row.get("like") or {}).get("cnt", 0)
                        if isinstance(row.get("like"), dict)
                        else 0
                    ),
                    "images": re.findall(
                        r"https?://[^\s\"'<>]+\.(?:jpg|jpeg|png|gif|webp)", str(row.get("html") or "")
                    )[:9],
                }
            )
            if len(items) >= max(1, int(limit)):
                break
        return items

    # ----------------------------------------------------------------- 写
    async def publish(self, content: str, *, sync_weibo: bool = False) -> dict[str, Any]:
        text = (content or "").strip()
        if not text:
            raise QzoneRequestError("内容是空的")
        if len(text) > MAX_POST_CHARS:
            raise QzoneRequestError(f"说说最多 {MAX_POST_CHARS} 字，现在 {len(text)} 字")
        if not self.auth.usable:
            raise QzoneAuthError("还没有可用的空间登录态")

        uin = self.auth.uin
        payload = await self._post_form(
            PUBLISH_URL,
            {"g_tk": self.auth.gtk},
            {
                "syn_tweet_verson": "1",
                "paramstr": "1",
                "who": "1",
                "con": text,
                "feedversion": "1",
                "ver": "1",
                "ugc_right": 1,
                "to_sign": 0,
                "hostuin": uin,
                "code_version": "1",
                "richval": "",
                "issyncweibo": int(bool(sync_weibo)),
                "format": "json",
                "qzreferrer": f"https://user.qzone.qq.com/{uin}",
            },
            f"https://user.qzone.qq.com/{uin}",
        )
        self._check_code(payload)
        if payload.get("ret") not in (None, 0):
            raise QzoneRequestError(str(payload.get("msg") or "发布失败"))
        logger.info(f"[congling] 空间说说已发布，长度 {len(text)}")
        return {"ok": True, "fid": str(payload.get("tid") or payload.get("fid") or "")}
