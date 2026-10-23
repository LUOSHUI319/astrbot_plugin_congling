"""内置能力实现。

选型原则：优先免 key、可离线兜底的接口，保证开箱能用。
需要密钥的东西（搜索、地图、天气付费源）留成可插拔项，不塞进首发。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger

from ..core.contracts import Capability

UA = {"User-Agent": "AstrBot-CongLing/0.1 (+https://astrbot.app)"}
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

WEATHER_CODES = {
    0: "晴",
    1: "大致晴",
    2: "多云",
    3: "阴",
    45: "有雾",
    48: "雾凇",
    51: "小毛雨",
    53: "毛毛雨",
    55: "毛毛雨偏大",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "阵雨偏大",
    82: "强阵雨",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷暴",
}


# --------------------------------------------------------------------- HTTP
async def _get_json(url: str, timeout: float = 8.0) -> Any:
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=UA) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.json(content_type=None)


async def _get_text(url: str, timeout: float = 8.0) -> str:
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=UA) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.text(errors="ignore")


# --------------------------------------------------------------------- 时间
async def time_now(target_date: str = "") -> dict[str, Any]:
    now = datetime.now()
    lines = [f"现在是 {now.strftime('%Y-%m-%d %H:%M')}，{WEEKDAYS[now.weekday()]}"]
    target_date = (target_date or "").strip()
    if target_date:
        parsed = _parse_date(target_date)
        if parsed:
            delta = (parsed.date() - now.date()).days
            if delta > 0:
                lines.append(f"距离 {parsed.strftime('%Y-%m-%d')} 还有 {delta} 天")
            elif delta < 0:
                lines.append(f"{parsed.strftime('%Y-%m-%d')} 已经过去 {abs(delta)} 天")
            else:
                lines.append(f"就是今天（{parsed.strftime('%Y-%m-%d')}）")
        else:
            lines.append("那个日期我没看懂")
    return {"text": "；".join(lines), "data": {"now": now.isoformat()}}


def _parse_date(text: str) -> datetime | None:
    text = text.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    for fmt in ("%Y-%m-%d", "%m-%d", "%Y%m%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%m-%d":
                parsed = parsed.replace(year=datetime.now().year)
            return parsed
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------- 天气
async def weather(city: str, days: int = 1) -> dict[str, Any]:
    city = (city or "").strip()
    if not city:
        return {"text": "你想查哪个城市的天气"}
    geo = await _get_json(
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={city}&count=1&language=zh&format=json"
    )
    results = (geo or {}).get("results") or []
    if not results:
        return {"text": f"没找到叫「{city}」的地方"}
    place = results[0]
    lat, lon = place.get("latitude"), place.get("longitude")
    label = place.get("name") or city

    days = max(1, min(int(days or 1), 3))
    forecast = await _get_json(
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
        f"&timezone=auto&forecast_days={days}"
    )
    current = (forecast or {}).get("current") or {}
    daily = (forecast or {}).get("daily") or {}
    code = int(current.get("weather_code") or 0)
    parts = [
        f"{label}现在 {current.get('temperature_2m', '?')}°C，{WEATHER_CODES.get(code, '天气不明')}，"
        f"湿度 {current.get('relative_humidity_2m', '?')}%"
    ]
    dates = daily.get("time") or []
    for index in range(min(days, len(dates))):
        day_code = int((daily.get("weather_code") or [0])[index] or 0)
        high = (daily.get("temperature_2m_max") or [None])[index]
        low = (daily.get("temperature_2m_min") or [None])[index]
        rain = (daily.get("precipitation_probability_max") or [None])[index]
        prefix = "今天" if index == 0 else ("明天" if index == 1 else dates[index])
        line = f"{prefix} {low}~{high}°C {WEATHER_CODES.get(day_code, '')}"
        if rain not in (None, 0):
            line += f"，降水概率 {rain}%"
        parts.append(line)
    return {"text": "；".join(parts), "data": forecast}


# --------------------------------------------------------------------- MC
async def mc_status(host: str) -> dict[str, Any]:
    host = (host or "").strip()
    if not host:
        return {"text": "要查哪个服务器"}
    data = await _get_json(f"https://api.mcsrvstat.us/3/{host}")
    if not isinstance(data, dict) or not data.get("online"):
        return {"text": f"{host} 现在离线，或者这个地址不存在"}
    players = data.get("players") or {}
    version = data.get("version") or ""
    motd = data.get("motd") or {}
    motd_text = " ".join(motd.get("clean") or []) if isinstance(motd, dict) else ""
    parts = [
        f"{host} 在线",
        f"版本 {version}" if version else "",
        f"玩家 {players.get('online', 0)}/{players.get('max', '?')}",
        motd_text.strip(),
    ]
    return {"text": "，".join(part for part in parts if part), "data": data}


# --------------------------------------------------------------------- 网页
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_RE = re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']', re.I | re.S)
_DESC_RE = re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', re.I | re.S)


async def web_peek(url: str) -> dict[str, Any]:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"text": "这个链接看起来不太对"}
    html = await _get_text(url, timeout=6.0)
    title = _clean(_TITLE_RE.search(html))
    desc = _clean(_OG_RE.search(html)) or _clean(_DESC_RE.search(html))
    if not title and not desc:
        return {"text": "页面打开了，但没读到标题"}
    text = f"《{title}》" if title else ""
    if desc:
        text += f"：{desc[:160]}"
    return {"text": text.strip("："), "data": {"title": title, "desc": desc}}


def _clean(match: Any) -> str:
    if not match:
        return ""
    raw = re.sub(r"<[^>]+>", "", match.group(1))
    return re.sub(r"\s+", " ", raw).strip()


# --------------------------------------------------------------------- 自检
async def self_status(plugin: Any) -> dict[str, Any]:
    health = plugin.health()
    lines = [
        f"{health['version']} 已运行 {health['uptime'] // 60} 分钟",
        "模块：" + "、".join(name for name, ok in health["modules"].items() if ok),
    ]
    if plugin.memory is not None:
        try:
            count = await plugin.memory.atoms.count()
            tier = ""
            try:
                tier = plugin.memory.tier()
            except Exception:
                tier = ""
            lines.append(f"记忆 {count} 条" + (f"（{tier} 档）" if tier else ""))
        except Exception:
            pass
    if plugin.capabilities is not None:
        lines.append(f"能力 {len(plugin.capabilities.caps)} 项")
    return {"text": "；".join(lines), "data": health}


# --------------------------------------------------------------------- 翻译
async def translate(plugin: Any, text: str, target: str = "中文") -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {"text": "要翻什么"}
    provider = plugin.context.get_using_provider()
    if provider is None:
        return {"text": "现在没有可用的模型，翻不了"}
    prompt = f"把下面的内容翻译成{target}，只输出译文，不要解释：\n{text}"
    resp = await provider.text_chat(prompt=prompt, contexts=[])
    result = str(getattr(resp, "completion_text", "") or "").strip()
    return {"text": result or "没翻出来"}


# --------------------------------------------------------------------- 注册
# --------------------------------------------------------------------- 空间
async def qzone_feed(
    plugin: Any, _event: Any = None, target: str = "", limit: int = 5
) -> dict[str, Any]:
    bridge = getattr(plugin.qqext, "qzone", None) if plugin.qqext is not None else None
    if bridge is None:
        return {"text": "空间功能没接上"}
    try:
        size = int(limit) or 5
    except (TypeError, ValueError):
        size = 5
    result = await bridge.feed(
        limit=max(1, min(size, 20)),
        target=str(target or ""),
        event=_event,
    )
    if not result.get("ok"):
        return {"text": result.get("text") or "读不到空间"}
    return {
        "text": result.get("text") or "读到几条",
        "data": {"items": result.get("items", []), "target": result.get("target", {})},
    }


async def qzone_publish(plugin: Any, content: str) -> dict[str, Any]:
    bridge = getattr(plugin.qqext, "qzone", None) if plugin.qqext is not None else None
    if bridge is None:
        return {"text": "空间功能没接上"}
    result = await bridge.publish(str(content or ""))
    return {"text": result.get("text") or ("发出去了" if result.get("ok") else "没发出去")}


# --------------------------------------------------------------------- 记忆
async def memory_search(
    plugin: Any, query: str, k: int = 8, cross_session: bool = True
) -> dict[str, Any]:
    """让模型主动翻自己的长期记忆。"""
    if plugin.memory is None:
        return {"text": "记忆模块没开"}
    query = (query or "").strip()
    if not query:
        return {"text": "要搜什么"}
    try:
        limit = max(1, min(int(k or 8), 20))
    except (TypeError, ValueError):
        limit = 8
    try:
        body = await plugin.memory.recall(
            query, scope="user", subject="", k=limit, cross_session=bool(cross_session)
        )
    except TypeError:
        # 老签名没有 cross_session，退回按会话查
        body = await plugin.memory.recall(query, scope="user", subject="", k=limit)
    except Exception as exc:
        return {"text": f"搜记忆出错了：{repr(exc)[:100]}"}
    if not body:
        return {"text": "记忆里没找到相关的"}
    return {"text": body}


def build_builtin(plugin: Any) -> list[Capability]:
    return [
        Capability(
            name="life.time",
            description="查现在几点、今天几号、星期几、距离某个日期还有几天。用户问时间/日期/倒计时时使用。",
            group="life",
            parameters={
                "type": "object",
                "properties": {
                    "target_date": {"type": "string", "description": "可选，目标日期，如 2026-03-17"},
                },
            },
            handler=time_now,
            timeout=2.0,
        ),
        Capability(
            name="life.weather",
            description="查某个城市现在的天气和未来几天预报。用户问天气、要不要带伞、穿什么时使用。",
            group="life",
            parameters={
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，如 杭州"},
                    "days": {"type": "integer", "description": "预报天数 1~3，默认 1"},
                },
                "required": ["city"],
            },
            handler=weather,
            timeout=10.0,
            cache_ttl=900,
            fallback="天气接口没回话，晚点再看",
        ),
        Capability(
            name="game.mc_status",
            description="查 Minecraft 服务器是否在线、版本、在线人数。",
            group="game",
            parameters={
                "type": "object",
                "properties": {"host": {"type": "string", "description": "服务器地址，如 mc.hypixel.net"}},
                "required": ["host"],
            },
            handler=mc_status,
            timeout=10.0,
            cache_ttl=120,
        ),
        Capability(
            name="knowledge.web_peek",
            description="打开一个网页，读出它的标题和简介。用户发来链接问「这是什么」时使用。",
            group="knowledge",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "完整 http(s) 链接"}},
                "required": ["url"],
            },
            handler=web_peek,
            timeout=8.0,
            cache_ttl=3600,
        ),
        Capability(
            name="self.status",
            description="查看机器人自己的运行状态、记忆条数、已启用能力。用户问「你还好吗/你记得多少」时使用。",
            group="self",
            parameters={"type": "object", "properties": {}},
            handler=lambda: self_status(plugin),
            timeout=4.0,
        ),
        Capability(
            name="self.memory",
            description=(
                "在自己的长期记忆里搜索。用户问「你还记得…」「上次说过的那个」「我昨天说过什么」时先查这里；"
                "结果每条都带来源标签，比如 [私聊 望山]、[群 123456]，回答时要说清是在哪里发生的。"
            ),
            group="self",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要搜的关键词或一句话"},
                    "k": {"type": "integer", "description": "返回几条，默认 8"},
                    "cross_session": {"type": "boolean", "description": "是否跨会话一起搜，默认 true"},
                },
                "required": ["query"],
            },
            handler=lambda query="", k=8, cross_session=True: memory_search(
                plugin, query, k, cross_session
            ),
            timeout=6.0,
        ),
        Capability(
            name="qzone.feed",
            description="读某个人的 QQ 空间说说，target 留空表示当前说话人自己。",
            group="life",
            parameters={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "看谁的空间：留空=当前说话人自己，也可填 QQ 号或昵称",
                    },
                    "limit": {"type": "integer", "description": "读几条，默认 5，最多 20"},
                },
            },
            handler=lambda target="", limit=5, _event=None: qzone_feed(
                plugin, _event=_event, target=target, limit=limit
            ),
            timeout=20.0,
            cache_ttl=0,
            takes_event=True,
            fallback="空间这会儿读不出来",
        ),
        Capability(
            name="qzone.publish",
            description="发一条 QQ 空间说说。只有用户明确让你发的时候才用，不要自己决定发。",
            group="qzone",
            auth="admin",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "说说正文，最多 1000 字"},
                },
                "required": ["content"],
            },
            handler=lambda content: qzone_publish(plugin, content),
            timeout=20.0,
            fallback="没发出去",
        ),
        Capability(
            name="life.translate",
            description="把一段文本翻译成目标语言，默认中文。",
            group="life",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要翻译的内容"},
                    "target": {"type": "string", "description": "目标语言，默认中文"},
                },
                "required": ["text"],
            },
            handler=lambda text, target="中文": translate(plugin, text, target),
            timeout=20.0,
        ),
    ]
