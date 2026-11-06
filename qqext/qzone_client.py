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
# 评论说说。返回被 frameElement.callback(...) 包着，_parse 会剥掉。
COMMENT_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
)
# 点赞。注意不是 emotion_cgi_like —— 那个会返回 HTTP 500。
LIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
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

# 发说说带图：先传到这个图床拿 richval，再在发布参数里带上。
# 实测 2026-11-06 可用，参数格式见 upload_image 的注释。
UPLOAD_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
UPLOAD_MAX_BYTES = 8 * 1024 * 1024


def _find_first(node: Any, keys: tuple[str, ...]) -> Any:
    """上传返回是嵌套结构，递归找第一个非空匹配。"""
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if value not in (None, "", 0, "0"):
                return value
        for value in node.values():
            got = _find_first(value, keys)
            if got is not None:
                return got
    elif isinstance(node, list):
        for value in node:
            got = _find_first(value, keys)
            if got is not None:
                return got
    return None


def _extract_pic_bo(url: Any) -> str:
    """pic_bo 不在任何 JSON 键里，它是返回 url 的 `bo=` 参数值。"""
    match = re.search(r"(?:^|[?&])(?:bo|pic_bo|picbo)=([^&\s]+)", str(url or ""))
    return match.group(1).strip() if match else ""


def _build_richval(payload: dict[str, Any]) -> str:
    """拼 richval：10 段，第一位和倒数第二位是空串。缺字段就是空串。"""
    albumid = str(_find_first(payload, ("albumid", "album_id")) or "").strip()
    lloc = str(_find_first(payload, ("lloc",)) or "").strip()
    sloc = str(_find_first(payload, ("sloc",)) or "").strip()
    image_type = str(_find_first(payload, ("type", "phototype", "image_type")) or "").strip() or "1"
    height = str(_find_first(payload, ("height", "h")) or "").strip() or "0"
    width = str(_find_first(payload, ("width", "w")) or "").strip() or "0"
    if not (albumid and lloc and sloc):
        return ""
    return ",".join(["", albumid, lloc, sloc, image_type, height, width, "", height, width])


# ------------------------------------------------ feeds3_html_more 的 JS 字面量
# 那个接口返回的根本不是 JSON：
#   {"code":0,...,"data":{main:{attach:'',...},data:[{ver:'1',abstime:'179...',html:'\x3Cli...'}]}}
# 键没引号、字符串用单引号、HTML 里的尖括号还写成 \x3C。
# 下面这三个函数是一个「够用的迷你 JS 对象读取器」：字符串感知地找边界，只取顶层标量字段。
# 不要用正则硬套 —— html 里全是单引号和逗号，非贪婪必被截断。

def _read_js_string(text: str, i: int) -> tuple[str, int]:
    quote = text[i]
    i += 1
    buf: list[str] = []
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "x" and i + 3 < len(text):
                try:
                    buf.append(chr(int(text[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
            if nxt == "u" and i + 5 < len(text):
                try:
                    buf.append(chr(int(text[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
            i += 2
            continue
        if ch == quote:
            return "".join(buf), i + 1
        buf.append(ch)
        i += 1
    return "".join(buf), i


def _matching_brace(text: str, start: int) -> int:
    """从 text[start] == '{' 开始，字符串感知地找到配对的 '}'，返回它的下标。"""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "'\"":
            _, i = _read_js_string(text, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _skip_js_value(text: str, i: int, end: int) -> int:
    """跳过一个嵌套值（对象/数组/标量），返回下一个逗号之后的位置。"""
    depth = 0
    while i < end:
        ch = text[i]
        if ch in "'\"":
            _, i = _read_js_string(text, i)
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            if depth == 0:
                return i
            depth -= 1
        elif ch == "," and depth == 0:
            return i + 1
        i += 1
    return i


def _parse_js_object(text: str, start: int, end: int) -> dict[str, Any]:
    """解析 `{k:'v',n:1,...}`，只取顶层标量字段；嵌套的直接跳过。"""
    out: dict[str, Any] = {}
    i = start
    if i < end and text[i] == "{":
        i += 1          # 跳过对象自己的左花括号，否则会把它当成键名的一部分
    while i < end:
        while i < end and text[i] in " \t\r\n,":
            i += 1
        if i >= end or text[i] == "}":
            break
        if text[i] in "'\"":
            key, i = _read_js_string(text, i)
        else:
            j = i
            while j < end and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            key = text[i:j]
            i = j
        while i < end and text[i] in " \t\r\n":
            i += 1
        if i >= end or text[i] != ":":
            i = _skip_js_value(text, i, end)
            continue
        i += 1
        while i < end and text[i] in " \t\r\n":
            i += 1
        if i >= end:
            break
        ch = text[i]
        if ch in "'\"":
            value, i = _read_js_string(text, i)
            out[key] = value
        elif ch in "{[":
            i = _skip_js_value(text, i, end)
        else:
            j = i
            while j < end and text[j] not in ",}":
                j += 1
            out[key] = text[i:j].strip()
            i = j
    return out


def parse_friend_feeds(body: str, limit: int = 10) -> list[dict[str, Any]]:
    """从 feeds3_html_more 的原始响应里抠出好友动态条目。"""
    anchor = body.find('"data"')
    if anchor < 0:
        return []
    array_at = body.find("data:[", anchor)
    if array_at < 0:
        return []
    items: list[dict[str, Any]] = []
    i = array_at + len("data:[")
    while i < len(body) and len(items) < max(1, limit):
        while i < len(body) and body[i] in " \t\r\n,":
            i += 1
        if i >= len(body) or body[i] != "{":
            break
        end = _matching_brace(body, i)
        if end < 0:
            break
        items.append(_parse_js_object(body, i, end + 1))
        i = end + 1
    return items


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
        # 评论类接口用这种包裹：<script>frameElement.callback({...})</script>
        if "</script>" in text:
            text = text[: text.find("</script>")]
        index = text.find("callback(")
        if index >= 0 and not text.startswith("{"):
            start = index + len("callback(")
            end = text.rfind(")")
            if end > start:
                text = text[start:end].strip()
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

    async def _get_text(self, url: str, params: dict[str, Any], referer: str) -> str:
        """拿原始文本。feeds3_html_more 不能走 _get_json —— 它不是 JSON。"""
        import aiohttp

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        ) as session:
            async with session.get(url, params=params, headers=self._headers(referer)) as resp:
                text = await resp.text(errors="ignore")
                if resp.status != 200:
                    raise QzoneRequestError(f"HTTP {resp.status}")
        return text

    async def feed(self, *, page: int = 1, limit: int = 10) -> list[dict[str, Any]]:
        """读好友动态流（feeds3_html_more）。

        这个接口返回的是 JS 对象字面量，不是 JSON，所以走 `_get_text` + `parse_friend_feeds`。
        想看「她自己发的说说」用 `user_feeds(uin)`，那个是标准 JSON。
        """
        if not self.auth.usable:
            raise QzoneAuthError("还没有可用的空间登录态")
        uin = self.auth.uin
        referer = f"https://user.qzone.qq.com/{uin}"
        raw = await self._get_text(
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
            referer,
        )
        head = raw[:600]
        if "登录" in head or "ptlogin" in head:
            raise QzoneAuthError("登录态失效了，要重新拿 cookie")
        match = re.search(r'"code"\s*:\s*(-?\d+)', head)
        if match and int(match.group(1)) in (-3000, 1000):
            raise QzoneAuthError("登录态失效了，要重新拿 cookie")

        # 多取一些：广告会被过滤掉，按 limit 取可能不够数
        rows = parse_friend_feeds(raw, max(int(limit) * 3, 12))
        return self._parse_feed({"data": rows}, limit)
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

        body_re = re.compile(r'<div class="f-info"[^>]*>(.*?)</div>', re.S)
        box_re = re.compile(r'<div class="img-box[^"]*"[^>]*>(.*?)</div>\s*</div>', re.S)
        img_re = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
        topic_re = re.compile(r'data-topicid="([^"]+)"')
        unikey_re = re.compile(r'data-unikey="([^"]+)"')
        curkey_re = re.compile(r'data-curkey="([^"]+)"')

        def pick_images(html: str) -> list[str]:
            out: list[str] = []
            box = box_re.search(html)
            source = box.group(1) if box else html
            for src in img_re.findall(source):
                # 头像是 qlogo*.store.qq.com/.../50，不是说说配图
                if "qlogo" in src or "store.qq.com/qzone/" in src:
                    continue
                if src not in out:
                    out.append(src)
            return out[:9]

        items: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            html = str(row.get("html") or "")
            # 广告：广点通的 appid 是 6600、typeid 4，图也从 adsmind 出
            if (
                str(row.get("appid") or "") == "6600"
                or str(row.get("typeid") or "") == "4"
                or "adsmind.gdtimg.com" in html
            ):
                continue

            # 正文只在 <div class="f-info"> 里，整段 strip 会把昵称、浏览数、
            # 点赞人名字一起卷进来（以前就是这样，内容很脏）。
            content = ""
            match = body_re.search(html)
            if match:
                content = strip_html(match.group(1)).strip()
            if not content:
                content = strip_html(str(row.get("content") or row.get("summary") or "")).strip()
            images = pick_images(html)
            # 纯图说说没有正文，留着（有图就能看）；两条都空才丢
            if not content and not images:
                continue

            created = row.get("abstime") or row.get("created_time") or row.get("createtime") or 0
            try:
                created = int(created)
            except (TypeError, ValueError):
                created = 0
            items.append(
                {
                    "fid": str(row.get("key") or row.get("fid") or row.get("cellid") or ""),
                    "topic_id": (topic_re.search(html).group(1) if topic_re.search(html) else ""),
                    "unikey": (unikey_re.search(html).group(1) if unikey_re.search(html) else ""),
                    "curkey": (curkey_re.search(html).group(1) if curkey_re.search(html) else ""),
                    "appid": int(row.get("appid") or 311),
                    "uin": int(row.get("uin") or 0) if str(row.get("uin") or "").isdigit() else 0,
                    "nickname": strip_html(str(row.get("nickname") or "")),
                    "content": content[:800],
                    "created_at": created,
                    "created_text": (
                        time.strftime("%m-%d %H:%M", time.localtime(created)) if created else ""
                    ),
                    "comment_count": int(row.get("commentcnt") or row.get("cmtnum") or 0),
                    "like_count": int(
                        (row.get("like") or {}).get("cnt", 0)
                        if isinstance(row.get("like"), dict)
                        else 0
                    ),
                    "images": images,
                }
            )
            if len(items) >= max(1, int(limit)):
                break
        return items

    # ----------------------------------------------------------------- 写
    async def upload_image(self, data: bytes, filename: str = "image.jpg") -> dict[str, Any] | None:
        """把一张图传到空间图床。

        返回 ``{"richval": ..., "pic_bo": ..., "type": ...}``，任何一步失败都返回 None，
        由调用方降级成纯文本 —— 配图是加分项，不能让它把整条说说卡死。

        参数格式是实测出来的（2026-11-06）：
        - `application/x-www-form-urlencoded`，图片走 base64（`base64=1` + `picfile`），**不是 multipart**
        - `richval` 由 albumid / lloc / sloc / type / width / height 拼成 10 段，首尾是空串
        - `pic_bo` 只能从返回 url 的 `bo=` 参数里提，而且 `origin_url` 那版没有，要从 `url` / `pre` 找
        """
        if not data:
            return None
        if len(data) > UPLOAD_MAX_BYTES:
            logger.warning(f"[congling] 空间配图太大（{len(data)} bytes），跳过")
            return None
        if not self.auth.usable:
            logger.warning("[congling] 空间登录态不可用，跳过配图")
            return None

        import base64

        referrer = f"https://user.qzone.qq.com/{self.auth.uin}"
        cookies = self.auth.cookies
        form = {
            "filename": filename or "image.jpg",
            "uin": str(self.auth.uin),
            "skey": cookies.get("skey", ""),
            "p_skey": cookies.get("p_skey", "") or cookies.get("skey", ""),
            "zzpaneluin": str(self.auth.uin),
            "p_uin": str(self.auth.uin),
            "qzonetoken": cookies.get("qzonetoken", ""),
            "zzpanelkey": "",
            "uploadtype": "1",
            "albumtype": "7",
            "exttype": "0",
            "refer": "shuoshuo",
            "output_type": "json",
            "charset": "utf-8",
            "output_charset": "utf-8",
            "upload_hd": "1",
            "hd_width": "2048",
            "hd_height": "10000",
            "hd_quality": "96",
            "backUrls": ("http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                         "http://119.147.64.75/cgi-bin/upload/cgi_upload_image"),
            "url": f"{UPLOAD_URL}?g_tk={self.auth.gtk}",
            "base64": "1",
            "picfile": base64.b64encode(data).decode("ascii"),
            "qzreferrer": referrer,
        }
        try:
            payload = await self._post_form(
                UPLOAD_URL, {"g_tk": self.auth.gtk}, form, referrer
            )
        except Exception as exc:
            logger.warning(f"[congling] 空间配图上传失败: {exc!r}")
            return None
        if payload.get("ret") not in (0, "0", None):
            logger.warning(f"[congling] 空间配图被拒: {payload.get('msg') or payload.get('ret')}")
            return None

        richval = _build_richval(payload)
        pic_bo = ""
        for key in ("url", "pre", "raw_url", "origin_url"):
            pic_bo = _extract_pic_bo(_find_first(payload, (key,)))
            if pic_bo:
                break
        if not pic_bo:
            pic_bo = str(_find_first(payload, ("pic_bo", "picbo")) or "").strip()
        if not richval or not pic_bo:
            logger.warning(
                f"[congling] 空间配图上传成功但字段不全：richval={bool(richval)} pic_bo={bool(pic_bo)}"
            )
            return None
        image_type = str(_find_first(payload, ("type", "phototype")) or "1").strip()
        return {"richval": richval, "pic_bo": pic_bo, "type": image_type}

    async def publish(
        self,
        content: str,
        *,
        images: list[dict[str, Any]] | None = None,
        sync_weibo: bool = False,
    ) -> dict[str, Any]:
        text = (content or "").strip()
        if not text:
            raise QzoneRequestError("内容是空的")
        if len(text) > MAX_POST_CHARS:
            raise QzoneRequestError(f"说说最多 {MAX_POST_CHARS} 字，现在 {len(text)} 字")
        if not self.auth.usable:
            raise QzoneAuthError("还没有可用的空间登录态")

        uin = self.auth.uin
        form = {
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
        }
        usable = [i for i in (images or []) if i.get("richval") and i.get("pic_bo")]
        if usable:
            # 多张之间：richval 用制表符，pic_bo 用逗号（两个分隔符不一样，别写反）
            form.update({
                "richtype": "1",
                "subrichtype": "1",
                "richval": "\t".join(str(i["richval"]) for i in usable),
                "pic_bo": ",".join(str(i["pic_bo"]) for i in usable),
                "pic_template": "",
            })
        payload = await self._post_form(
            PUBLISH_URL,
            {"g_tk": self.auth.gtk},
            form,
            f"https://user.qzone.qq.com/{uin}",
        )
        self._check_code(payload)
        if payload.get("ret") not in (None, 0):
            raise QzoneRequestError(str(payload.get("msg") or "发布失败"))
        logger.info(f"[congling] 空间说说已发布，长度 {len(text)}，图 {len(usable)} 张")
        return {
            "ok": True,
            "fid": str(payload.get("tid") or payload.get("fid") or ""),
            "images": len(usable),
        }

    async def like(
        self,
        *,
        unikey: str,
        curkey: str,
        fid: str,
        abstime: int = 0,
        appid: str = "311",
        typeid: str = "0",
        host_uin: int = 0,
    ) -> dict[str, Any]:
        """给一条说说点赞。

        `unikey` / `curkey` 从说说 HTML 的 `data-unikey` / `data-curkey` 取，
        接口是 `internal_dolike_app`（不是 emotion_cgi_like，那个返回 500）。

        公开行为：点完对方的说说下面会显示她的赞，调用方自己把关频率。
        """
        if not unikey or not curkey:
            raise QzoneRequestError("缺 unikey/curkey，这条说说可能不支持点赞")
        if not self.auth.usable:
            raise QzoneAuthError("还没有可用的空间登录态")

        opuin = str(self.auth.uin)
        referer = f"https://user.qzone.qq.com/{host_uin or self.auth.uin}/mood/{fid}"
        payload = await self._post_form(
            LIKE_URL,
            {"g_tk": self.auth.gtk},
            {
                "qzreferrer": f"https://user.qzone.qq.com/{self.auth.uin}",
                "opuin": opuin,
                "unikey": unikey,
                "curkey": curkey,
                "appid": str(appid or "311"),
                "from": 1,
                "typeid": str(typeid or "0"),
                "abstime": int(abstime or 0),
                "fid": fid,
                "active": 0,
                "format": "json",
                "fupdate": 1,
            },
            referer,
        )
        code = payload.get("code", payload.get("ret"))
        if code not in (0, "0", None):
            raise QzoneRequestError(str(payload.get("message") or payload.get("msg") or f"点赞失败 code={code}"))
        logger.info(f"[congling] 空间点赞成功（fid={fid}）")
        return {"ok": True, "fid": fid, "liked": True}

    async def download_image(self, url: str, *, referer: str = "") -> bytes | None:
        """下载空间图床里的图（说说配图 / 相册图）。

        qpic / photo.store 这些域名基本公开可读，但带上 cookie 和 referer 更稳。
        失败返回 None —— 调用方应该当成「这张图不存在」继续处理，不要中断整批。
        """
        if not url:
            return None
        import aiohttp

        headers = {
            "User-Agent": UA,
            "Referer": referer or f"https://user.qzone.qq.com/{self.auth.uin}",
            "Cookie": self.auth.header(),
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        logger.debug(f"[congling] 下载图片 HTTP {resp.status}: {url[:80]}")
                        return None
                    data = await resp.read()
        except Exception as exc:
            logger.debug(f"[congling] 下载图片失败: {exc!r}")
            return None
        if not data or len(data) > UPLOAD_MAX_BYTES:
            return None
        return data

    async def comment(
        self, topic_id: str, content: str, *, host_uin: int = 0
    ) -> dict[str, Any]:
        """在一条说说下面评论。

        `topic_id` 从说说 HTML 的 `data-topicid` 取（形如 `3404044656_xxxx__1`）。
        这是**公开行为**：内容会挂在别人的说说下面，调用方必须自己把关频率和内容。
        """
        text = (content or "").strip()
        if not text:
            raise QzoneRequestError("评论内容是空的")
        if len(text) > 200:
            raise QzoneRequestError("评论最多 200 字")
        if not topic_id:
            raise QzoneRequestError("缺 topic_id")
        if not self.auth.usable:
            raise QzoneAuthError("还没有可用的空间登录态")

        uin = self.auth.uin
        referer = f"https://user.qzone.qq.com/{uin}"
        payload = await self._post_form(
            COMMENT_URL,
            {"g_tk": self.auth.gtk},
            {
                "topicId": topic_id,
                "feedsType": "100",
                "uin": str(uin),
                "hostuin": str(host_uin or uin),
                "content": text,
                "plat": "qz",
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "paramstr": "1",
                "source": "1",
                "format": "json",
                "qzreferrer": referer,
            },
            referer,
        )
        if payload.get("ret") not in (0, "0", None):
            raise QzoneRequestError(str(payload.get("msg") or "评论失败"))
        logger.info(f"[congling] 空间评论已发（{topic_id}）：{text[:30]}")
        return {"ok": True, "topic_id": topic_id}
