"""用户画像。

把一个人散落在几十条记忆里的东西压成一段「我对 TA 的认识」。
不是简单摘要：关键词数量跟着好感度走——关系越近，值得记住的细节越多。

三道关：总开关 → 黑名单/白名单 → 好感度门槛与概率。哪一关不过都不生成。
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

from astrbot.api import logger

PROMPT = """你在整理对一个人（{name}）的认识。下面是从长期记忆里挑出来的条目，按重要程度排过序。

请输出 JSON，不要输出其他文字：
{{"summary":"3~5 句话的第一人称认识","keywords":["词"],"fields":{{"称呼":"","在意的事":"","雷点":"","日常规律":"","承诺未兑现":"","近况":"","关系标记":""}}}}

规则：
1. 只写有依据的，不要推测性格标签，不要编
2. summary 用第一人称，像心里的判断，不要分条列点
3. keywords 最多 {k} 个，越具体越好（地点、人名、口味、时间点都算）
4. fields 里没有依据的字段留空字符串，不要填「未知」
5. 每条不超过 60 字

记忆：
{atoms}
"""

FIELDS = ["称呼", "在意的事", "雷点", "日常规律", "承诺未兑现", "近况", "关系标记"]


class ProfileBook:
    def __init__(self, plugin: Any, subject_book: Any, affinity: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self.subjects = subject_book
        self.affinity = affinity

    # ------------------------------------------------------------ 开关
    def enabled(self) -> bool:
        try:
            return bool(self.config.bool("memory.user_profile.enabled", True))
        except Exception:
            return True

    def whitelist_enabled(self) -> bool:
        try:
            return bool(self.config.bool("memory.user_profile.whitelist_enabled", False))
        except Exception:
            return False

    def whitelist(self) -> list[str]:
        return [str(x).strip() for x in self.config.list("memory.user_profile.whitelist", []) if str(x).strip()]

    def blocklist(self) -> list[str]:
        return [str(x).strip() for x in self.config.list("memory.user_profile.blocklist", []) if str(x).strip()]

    def min_stage(self) -> int:
        try:
            return max(0, min(5, self.config.int("memory.user_profile.min_stage", 2)))
        except Exception:
            return 2

    def max_keywords(self) -> int:
        try:
            return max(4, min(60, self.config.int("memory.user_profile.max_keywords", 32)))
        except Exception:
            return 32

    async def allowed(self, subject_key: str, *, roll: bool = True, stage: int | None = None) -> tuple[bool, str]:
        key = str(subject_key or "").strip()
        if not key:
            return False, "空主体"
        if not self.enabled():
            return False, "画像总开关关着"
        if key in self.blocklist():
            return False, "在不再构建名单里"
        if self.whitelist_enabled() and key not in self.whitelist():
            return False, "不在白名单"
        if stage is None:
            stage = await self.affinity.stage(key)
        if stage < self.min_stage():
            return False, f"好感度只到 {self.affinity.stage_name(stage)}，没到门槛"
        if roll:
            chance = min(1.0, 0.15 + 0.15 * stage)
            if random.random() > chance:
                return False, "这次没掷中"
        return True, ""

    # ------------------------------------------------------------ 生成
    async def build(self, subject_key: str, *, force: bool = False) -> dict[str, Any]:
        key = str(subject_key or "").strip()
        stage = await self.affinity.stage(key)
        ok, reason = await self.allowed(key, roll=not force, stage=stage)
        if not ok:
            return {"ok": False, "reason": reason}

        atoms = await self.subjects.atoms_of(key, limit=120)
        if len(atoms) < 3:
            return {"ok": False, "reason": "记忆太少，还画不出什么"}

        provider = self._provider()
        if provider is None:
            return {"ok": False, "reason": "没有可用的模型"}

        limit = min(self.max_keywords(), 8 + stage * 4)
        body = "\n".join(f"- {str(a.get('text') or '')[:120]}" for a in atoms[:60])
        name = await self.subjects.display_name(key)
        prompt = (
            PROMPT.replace("{name}", name)
            .replace("{k}", str(limit))
            .replace("{atoms}", body[:6000])
        )
        try:
            resp = await provider.text_chat(prompt=prompt, contexts=[])
            raw = str(getattr(resp, "completion_text", "") or "")
        except Exception as exc:
            logger.debug(f"[congling] 画像生成失败: {exc!r}")
            return {"ok": False, "reason": "模型调用失败"}

        data = self._parse(raw)
        if not data:
            return {"ok": False, "reason": "模型返回的不是 JSON"}

        summary = str(data.get("summary") or "").strip()[:600]
        keywords = [str(x)[:20] for x in (data.get("keywords") or []) if str(x).strip()][:limit]
        fields_raw = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        fields = {name_key: str(fields_raw.get(name_key) or "")[:200] for name_key in FIELDS}

        await self.store.execute(
            "INSERT INTO mem_profile(persona_id, subject_key, fields, keywords, summary, "
            "source_atom_ids, updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(persona_id, subject_key) DO UPDATE SET fields=excluded.fields, "
            "keywords=excluded.keywords, summary=excluded.summary, "
            "source_atom_ids=excluded.source_atom_ids, updated_at=excluded.updated_at",
            (
                self.config.persona_id,
                key,
                json.dumps(fields, ensure_ascii=False),
                json.dumps(keywords, ensure_ascii=False),
                summary,
                json.dumps([int(a.get("id") or 0) for a in atoms[:60]]),
                int(time.time()),
            ),
        )
        return {
            "ok": True,
            "subject_key": key,
            "name": name,
            "stage": stage,
            "summary": summary,
            "keywords": keywords,
            "fields": fields,
            "source_count": len(atoms[:60]),
        }

    # ------------------------------------------------------------ 读改删
    @staticmethod
    def _loads(raw: Any, default: Any) -> Any:
        """把库里存的 JSON 文本还原成对象。

        这个方法以前压根不存在，凡是有画像数据的人一调 get() 就
        AttributeError，整个画像功能等于不可用。
        """
        if raw in (None, "", b""):
            return default
        if not isinstance(raw, str):
            return raw
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    async def get(self, subject_key: str) -> dict[str, Any]:
        key = str(subject_key or "").strip()
        row = await self.store.fetch_one(
            "SELECT * FROM mem_profile WHERE persona_id=? AND subject_key=?",
            (self.config.persona_id, key),
        )
        if not row:
            return {"subject_key": key, "exists": False}
        return {
            "subject_key": key,
            "exists": True,
            "summary": str(row.get("summary") or ""),
            "keywords": self._loads(row.get("keywords"), []),
            "fields": self._loads(row.get("fields"), {}),
            "source_count": len(self._loads(row.get("source_atom_ids"), [])),
            "updated_at": int(row.get("updated_at") or 0),
            "updated_text": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(int(row.get("updated_at") or 0))
            ),
        }

    async def update(
        self,
        subject_key: str,
        *,
        summary: str | None = None,
        keywords: list[str] | None = None,
        fields: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        key = str(subject_key or "").strip()
        current = await self.get(key)
        if not current.get("exists"):
            return {"ok": False, "reason": "还没有画像"}
        new_summary = current["summary"] if summary is None else str(summary)[:600]
        new_keywords = current["keywords"] if keywords is None else [str(x)[:20] for x in keywords][: self.max_keywords()]
        new_fields = dict(current["fields"] or {})
        if fields:
            for name_key in FIELDS:
                if name_key in fields:
                    new_fields[name_key] = str(fields[name_key])[:200]
        await self.store.execute(
            "UPDATE mem_profile SET summary=?, keywords=?, fields=?, updated_at=? "
            "WHERE persona_id=? AND subject_key=?",
            (
                new_summary,
                json.dumps(new_keywords, ensure_ascii=False),
                json.dumps(new_fields, ensure_ascii=False),
                int(time.time()),
                self.config.persona_id,
                key,
            ),
        )
        return await self.get(key)

    async def delete(self, subject_key: str, *, block: bool = True) -> dict[str, Any]:
        """删画像。默认顺手加进「不再构建」名单，免得下次又自己长回来。"""
        key = str(subject_key or "").strip()
        await self.store.execute(
            "DELETE FROM mem_profile WHERE persona_id=? AND subject_key=?",
            (self.config.persona_id, key),
        )
        blocked = False
        if block:
            blocked = await self.set_block(key, True)
        return {"ok": True, "subject_key": key, "blocked": blocked}

    async def set_block(self, subject_key: str, blocked: bool) -> bool:
        key = str(subject_key or "").strip()
        if not key:
            return False
        items = self.blocklist()
        if blocked and key not in items:
            items.append(key)
        elif not blocked:
            items = [x for x in items if x != key]
        else:
            return True
        try:
            self.config.override("memory.user_profile.blocklist", items)
            raw = getattr(self.plugin, "raw_config", None)
            if isinstance(raw, dict):
                node = raw.setdefault("memory", {})
                if isinstance(node, dict):
                    profile = node.setdefault("user_profile", {})
                    if isinstance(profile, dict):
                        profile["blocklist"] = items
                        save = getattr(raw, "save_config", None)
                        if callable(save):
                            save()
        except Exception as exc:
            logger.debug(f"[congling] 写入不再构建名单失败: {exc!r}")
        return True

    # ------------------------------------------------------------ 注入
    async def prompt_hint(self, subject_key: str) -> str:
        data = await self.get(subject_key)
        if not data.get("exists"):
            return ""
        parts: list[str] = []
        if data.get("summary"):
            parts.append(str(data["summary"]))
        fields = data.get("fields") or {}
        extra = [f"{k}：{v}" for k, v in fields.items() if v]
        if extra:
            parts.append("、".join(extra[:4]))
        words = data.get("keywords") or []
        if words:
            parts.append("关键词：" + "、".join(str(w) for w in words[:12]))
        return "。".join(parts) if parts else ""

    # ------------------------------------------------------------ 工具
    def _provider(self) -> Any:
        provider_id = self.config.str("memory.llm_provider_id", "")
        try:
            if provider_id:
                provider = self.plugin.context.get_provider_by_id(provider_id)
                if provider is not None:
                    return provider
            return self.plugin.context.get_using_provider()
        except Exception:
            return None

    @staticmethod
    def _parse(raw: str) -> dict[str, Any] | None:
        text = (raw or "").strip()
        if not text:
            return None
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
