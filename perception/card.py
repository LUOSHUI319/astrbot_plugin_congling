"""卡片 / 小程序 / 链接识别。

只取标题、来源、域名，不抓正文——成本低、不越界，且足以让模型知道
「用户在分享一个 B 站视频」而不是瞎猜。
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")

KNOWN_HOSTS = {
    "bilibili.com": "B 站",
    "b23.tv": "B 站",
    "douyin.com": "抖音",
    "v.douyin.com": "抖音",
    "music.163.com": "网易云音乐",
    "y.qq.com": "QQ 音乐",
    "github.com": "GitHub",
    "xiaohongshu.com": "小红书",
    "weibo.com": "微博",
    "zhihu.com": "知乎",
    "taobao.com": "淘宝",
    "tmall.com": "天猫",
    "jd.com": "京东",
    "youtube.com": "YouTube",
    "steampowered.com": "Steam",
}


def _load_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        return data if isinstance(data, dict) else {}
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"prompt": text}
        if isinstance(parsed, dict):
            inner = parsed.get("data")
            if isinstance(inner, dict):
                return inner
            return parsed
    return {}


def _host_label(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    for key, label in KNOWN_HOSTS.items():
        if host == key or host.endswith("." + key):
            return label
    return host


def parse_card(component: Any) -> tuple[str, dict[str, Any]]:
    """解析 Json 组件，返回 (一句话描述, 元信息)。"""
    payload = _load_payload(getattr(component, "data", None))
    title = str(payload.get("title") or payload.get("desc") or "").strip()
    prompt = str(payload.get("prompt") or "").strip()
    jump = str(payload.get("jumpUrl") or payload.get("qqdocurl") or payload.get("url") or "").strip()
    music = str(payload.get("musicUrl") or "").strip()
    app = str(payload.get("app") or payload.get("appid") or "").strip()

    if music:
        label = _host_label(music) or "音乐"
        return f"[{label} 音乐卡片：{title or prompt or '一首歌'}]", {"url": music, "title": title}
    if jump:
        label = _host_label(jump) or "链接"
        head = title or prompt or "一个链接"
        return f"[{label}：{head}]", {"url": jump, "title": title, "site": label}
    if title:
        return f"[卡片：{title}]", {"title": title, "app": app}
    if prompt:
        return f"[卡片：{prompt[:60]}]", {"prompt": prompt}
    return "[卡片]", {}


def find_urls(text: str) -> list[str]:
    return _URL_RE.findall(text or "")


def describe_url(url: str) -> tuple[str, dict[str, Any]]:
    label = _host_label(url) or "链接"
    return f"[{label} 链接：{url}]", {"url": url, "site": label}
