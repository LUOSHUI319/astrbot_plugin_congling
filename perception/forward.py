"""合并转发展开。

坑位记录（都是踩过的）：
1. 转发里的话不是当前用户说的 —— 渲染时必须标明发言人，否则模型会认错人；
2. 同一个 forward_id 一轮只注入一次 —— 用 marker 去重；
3. 长转发直接塞进去会吃掉整个上下文 —— 超阈值先摘要，原文仍入库可查；
4. 部分 OneBot 实现的返回结构不一致 —— data 可能是 dict 也可能是 list，都要兼容。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from astrbot.api import logger

from ..core import paths


def _sender_name(node: dict[str, Any]) -> str:
    data = node.get("data")
    for source in (node, data if isinstance(data, dict) else {}):
        if not isinstance(source, dict):
            continue
        sender = source.get("sender") or {}
        if isinstance(sender, dict):
            for key in ("card", "nickname", "name"):
                value = sender.get(key)
                if value:
                    return str(value)
            uid = sender.get("user_id") or sender.get("uin")
            if uid:
                return str(uid)
        for key in ("name", "nickname", "uin", "user_id"):
            value = source.get(key)
            if value:
                return str(value)
    return "某人"


def _node_content(node: dict[str, Any]) -> Any:
    """节点内容字段各协议端不一样：content / message / raw_message 都见过。"""
    if not isinstance(node, dict):
        return None
    data = node.get("data")
    for source in (node, data if isinstance(data, dict) else {}):
        if not isinstance(source, dict):
            continue
        for key in ("content", "message", "raw_message", "message_chain"):
            value = source.get(key)
            if value not in (None, "", []):
                return value
    return None


def _segment_to_text(segment: Any) -> str:
    if isinstance(segment, str):
        return segment
    if not isinstance(segment, dict):
        return ""
    seg_type = str(segment.get("type") or "")
    data = segment.get("data") or {}
    if not isinstance(data, dict):
        data = {"text": str(data)}
    if seg_type == "text":
        return str(data.get("text") or "")
    if seg_type == "image":
        return "[图片]"
    if seg_type == "face":
        return "[表情]"
    if seg_type == "mface":
        return f"[表情 {data.get('summary') or ''}]".strip()
    if seg_type == "record":
        return "[语音]"
    if seg_type == "video":
        return "[视频]"
    if seg_type == "file":
        return f"[文件 {data.get('name') or ''}]".strip()
    if seg_type == "at":
        target = data.get("name") or data.get("qq") or ""
        return f"@{target}" if target else "@某人"
    if seg_type == "json":
        payload = data.get("data") or ""
        return f"[卡片 {str(payload)[:40]}]"
    if seg_type == "forward":
        return "[嵌套转发]"
    if seg_type == "reply":
        return ""
    if seg_type == "location":
        return "[位置]"
    if seg_type == "xml":
        return "[卡片]"
    return str(data.get("text") or "")


def segments_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_segment_to_text(item) for item in content).strip()
    if isinstance(content, dict):
        return _segment_to_text(content).strip()
    return ""


class ForwardExpander:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store

    # ------------------------------------------------------------ 对外
    async def expand(self, forward_id: str, depth: int = 0) -> tuple[str, int]:
        """展开一条合并转发，返回 (可读文本, 条数)。失败返回 ("", 0)。"""
        forward_id = str(forward_id or "").strip()
        if not forward_id:
            return "", 0
        cached = await self._read_cache(forward_id)
        if cached:
            return cached[0], cached[1]

        nodes = await self._fetch(forward_id)
        if not nodes:
            return "", 0

        max_nodes = self.config.int("perception.forward_max_nodes", 60)
        max_depth = self.config.int("perception.forward_max_depth", 2)
        lines: list[str] = []
        count = 0
        for node in nodes:
            if count >= max_nodes:
                lines.append(f"…（还有 {len(nodes) - count} 条未展开）")
                break
            if not isinstance(node, dict):
                continue
            name = _sender_name(node)
            content = _node_content(node)
            text = segments_to_text(content)
            # 嵌套转发：深度允许时继续展开
            nested_id = self._nested_forward_id(content)
            if nested_id and depth < max_depth:
                nested_text, nested_count = await self.expand(nested_id, depth + 1)
                if nested_text:
                    text = f"{text}（内含转发 {nested_count} 条：{nested_text[:200]}…）" if len(
                        nested_text
                    ) > 200 else f"{text}（内含转发：{nested_text}）"
            if not text:
                continue
            lines.append(f"{name}: {text}")
            count += 1

        full_text = "\n".join(lines).strip()
        if not full_text:
            # 节点拿到了但一条都渲染不出来，说明字段名又变了；把结构记下来
            await self._trace(
                {
                    "id": forward_id,
                    "ok": False,
                    "why": "节点有了但内容为空",
                    "nodes": len(nodes),
                    "keys": sorted({k for node in nodes[:3] if isinstance(node, dict) for k in node.keys()}),
                    "sample": str(nodes[0])[:400] if nodes else "",
                }
            )
            return "", 0
        await self._write_cache(forward_id, full_text, count)
        return full_text, count

    async def summarize(self, text: str) -> str:
        """长转发摘要。失败则截断，绝不让异常冒泡。"""
        threshold = self.config.int("perception.forward_summary_threshold", 1200)
        if len(text) <= threshold:
            return text
        try:
            provider = self.plugin.context.get_using_provider()
            if provider is None:
                raise RuntimeError("无可用模型")
            prompt = (
                "下面是一段群聊转发记录。请压缩成不超过 400 字的要点，"
                "必须保留：谁说了什么关键结论、时间地点数字、待办事项。"
                "保持「昵称: 内容」的说话人标注，不要添加评论。\n\n" + text
            )
            resp = await provider.text_chat(prompt=prompt, contexts=[])
            summary = str(getattr(resp, "completion_text", "") or "").strip()
            if summary:
                return f"{summary}\n（原文 {len(text)} 字，完整版已存本地）"
        except Exception as exc:
            logger.debug(f"[congling] 转发摘要失败: {exc!r}")
        return text[:threshold] + "…（后文过长已截断）"

    # ------------------------------------------------------------ 内部
    # ------------------------------------------------------------ 诊断
    async def _trace(self, payload: dict[str, Any]) -> None:
        try:
            payload = dict(payload)
            payload["at"] = int(time.time())
            await self.store.kv_set("forward:last", payload)
        except Exception as exc:
            logger.debug(f"[congling] 转发诊断写入失败: {exc!r}")

    async def _fetch(self, forward_id: str) -> list[dict[str, Any]]:
        bot = self._get_bot()
        if bot is None:
            await self._trace({"id": forward_id, "error": "拿不到 bot"})
            return []
        # 各协议端参数名不统一：go-cqhttp 认 message_id，NapCat 有时只认 id，
        # 有的实现要 res_id / resid。逐个试，别只赌一个名字。
        attempts: list[dict[str, Any]] = [
            {"message_id": forward_id},
            {"id": forward_id},
            {"res_id": forward_id},
            {"resid": forward_id},
        ]
        if forward_id.isdigit():
            attempts.append({"message_id": int(forward_id)})
        attempts.append({"id": int(forward_id) if forward_id.isdigit() else forward_id})

        tried: list[str] = []
        last_shape = ""
        for params in attempts:
            label = ",".join(params.keys())
            try:
                if hasattr(bot, "call_action"):
                    data = await bot.call_action("get_forward_msg", **params)
                else:
                    data = await bot.api.call_action("get_forward_msg", **params)
            except Exception as exc:
                tried.append(f"{label}=ERR {str(exc)[:60]}")
                logger.debug(f"[congling] 展开转发失败 {forward_id} {list(params)}: {exc!r}")
                continue
            nodes = self._extract_nodes(data)
            if nodes:
                await self._trace({"id": forward_id, "ok": True, "via": label, "nodes": len(nodes)})
                return nodes
            last_shape = str(data)[:300]
            tried.append(f"{label}=空")
            logger.debug(f"[congling] 转发返回结构不认识 {forward_id} {list(params)}: {last_shape}")
        await self._trace({"id": forward_id, "ok": False, "tried": tried, "shape": last_shape})
        return []

    @staticmethod
    def _extract_nodes(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            return []
        data = payload.get("data") if "data" in payload else payload
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("messages", "message", "nodes"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _nested_forward_id(content: Any) -> str:
        if not isinstance(content, list):
            return ""
        for segment in content:
            if isinstance(segment, dict) and segment.get("type") == "forward":
                data = segment.get("data") or {}
                if isinstance(data, dict) and data.get("id"):
                    return str(data["id"])
        return ""

    def _get_bot(self) -> Any:
        bot = getattr(self.plugin, "_current_bot", None)
        if bot is not None:
            return bot
        event = getattr(self.plugin, "_current_event", None)
        if event is not None:
            return getattr(event, "bot", None)
        return None

    # ------------------------------------------------------------ 缓存
    async def _read_cache(self, forward_id: str) -> tuple[str, int] | None:
        try:
            row = await self.store.fetch_one(
                "SELECT full_text, node_count, expires_at FROM forward_cache WHERE forward_id=?",
                (forward_id,),
            )
        except Exception:
            return None
        if not row:
            return None
        if int(row.get("expires_at") or 0) < int(time.time()):
            return None
        return str(row.get("full_text") or ""), int(row.get("node_count") or 0)

    async def _write_cache(self, forward_id: str, text: str, count: int) -> None:
        now = int(time.time())
        digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
        try:
            await self.store.execute(
                "INSERT INTO forward_cache(forward_id, digest, full_text, node_count, created_at, expires_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(forward_id) DO UPDATE SET "
                "full_text=excluded.full_text, node_count=excluded.node_count, expires_at=excluded.expires_at",
                (forward_id, digest, text, count, now, now + 7 * 86400),
            )
        except Exception as exc:
            logger.debug(f"[congling] 转发缓存写入失败: {exc!r}")
        # 顺手落一份纯文本，方便排查
        try:
            (paths.forward_cache_dir() / f"{forward_id}.json").write_text(
                json.dumps({"text": text, "count": count}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
