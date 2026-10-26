"""记忆抽取：把一段聊天压成几条能长期用的事实。

三条铁律：
1. 只从**用户说的话**里抽事实，模型自己的回复不作为事实来源（防自我强化）；
2. 一条记忆必须能独立读懂——带主语、带具体指代，不能是「他昨天说的那个」；
3. 宁少勿滥。抽 3 条准的，胜过抽 10 条半对半错的。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from astrbot.api import logger

from ..core.contracts import MemoryDraft

PROMPT = """你在帮一个聊天机器人整理长期记忆。下面是一段真实聊天记录。

请抽取值得长期记住的信息，输出 JSON 数组，不要输出任何其他文字。

每条记录格式：
{"kind":"fact|pref|event|promise|relation|emotion","text":"一句完整陈述","subject":"主体","keywords":["词"],"importance":0.0,"confidence":0.0,"time_hint":"","evidence":["消息序号"],"level":3,"level_reason":"","level_tags":[]}

规则：
1. text 必须是一句能独立看懂的话，带主语，不出现"他/那个/上面说的"这类指代
2. 涉及时间的，换算成具体日期（今天是 {today}）
3. importance：0.3 无关紧要 / 0.6 一般偏好 / 0.9 生日、禁忌、承诺、重要事件
4. confidence：不确定就低于 0.5，低于 0.5 的会被丢弃
5. kind 含义：fact 客观事实，pref 喜好厌恶，event 发生过的事，promise 承诺或约定，relation 人际关系，emotion 情绪模式
6. 最多 {max_atoms} 条；没有值得记的就输出 []
7. 每行开头的名字是说话人。群聊里不同人的话要分开记，事实写清是谁说的（例如"清风说…"），不要一律写成"用户"
8. level 是这条该记多久：1 随口一句（今天好热、在吗）/ 2 细节小事 / 3 平常事实（工作、爱好、常去的地方）/ 4 在意的事（偏好、雷点、约定、重要日期）/ 5 关键（身份、承诺、创伤、关系里程碑）
9. level_tags 从这几个里挑，可以多选：identity 身份信息、promise 约定承诺、user_stress 用户强调、taboo 禁区底线、emotion_high 情绪浓度高、routine 日常寒暄、transient 一次性时效、preference 明确喜好、milestone 关系里程碑
10. level_reason 用一句话写依据，例如「用户说别忘了这件事」「只是随口一问天气」；不要写空话

聊天记录：
{conversation}
"""

MAX_INPUT_CHARS = 6000


class MemoryExtractor:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        # 本次抽取是否真的拿到并解析了模型输出；失败时不能推进对话水位
        self.last_ok = False

    async def extract(self, turns: list[dict[str, Any]]) -> list[MemoryDraft]:
        self.last_ok = False
        if not turns:
            return []
        conversation = self._render(turns)
        if not conversation.strip():
            return []

        provider = self._provider()
        if provider is None:
            return []

        try:
            max_atoms = int(self.config.int("memory.max_atoms_per_extract", 10))
        except (TypeError, ValueError):
            max_atoms = 10
        max_atoms = max(1, min(max_atoms, 30))

        # 不能用 str.format：PROMPT 里的 JSON 示例带花括号，会被当成占位符
        # 直接抛 KeyError('"kind"')，历史上记忆一条都没写进去就是这个原因。
        prompt = (
            PROMPT.replace("{today}", time.strftime("%Y-%m-%d", time.localtime()))
            .replace("{max_atoms}", str(max_atoms))
            .replace("{conversation}", conversation[:MAX_INPUT_CHARS])
        )
        try:
            resp = await provider.text_chat(prompt=prompt, contexts=[])
            raw = str(getattr(resp, "completion_text", "") or "")
        except Exception as exc:
            logger.debug(f"[congling] 记忆抽取调用失败: {exc!r}")
            return []

        payload = self._parse(raw)
        self.last_ok = bool(raw and ("[" in raw))
        try:
            await self.plugin.store.kv_set(
                "memory:last_raw",
                {"at": int(time.time()), "raw_len": len(raw), "items": len(payload), "head": raw[:240]},
            )
        except Exception:
            pass
        drafts: list[MemoryDraft] = []
        for item in payload[: max(1, max_atoms)]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if len(text) < 4:
                continue
            try:
                confidence = float(item.get("confidence", 0.7))
            except (TypeError, ValueError):
                confidence = 0.7
            if confidence < 0.5:
                continue
            try:
                importance = float(item.get("importance", 0.5))
            except (TypeError, ValueError):
                importance = 0.5
            drafts.append(
                MemoryDraft(
                    kind=str(item.get("kind") or "fact")[:20],
                    text=text[:300],
                    subject=str(item.get("subject") or "")[:40],
                    keywords=[str(word)[:20] for word in (item.get("keywords") or []) if word][:8],
                    importance=max(0.0, min(1.0, importance)),
                    confidence=max(0.0, min(1.0, confidence)),
                    time_hint=str(item.get("time_hint") or "")[:40],
                    evidence_msg_ids=[str(x)[:40] for x in (item.get("evidence") or [])][:5],
                    level=self._level_of(item),
                    level_reason=str(item.get("level_reason") or "")[:160],
                    level_tags=[str(t)[:20] for t in (item.get("level_tags") or []) if str(t).strip()][:6],
                )
            )
        return drafts

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _level_of(item: dict[str, Any]) -> int:
        """模型给的等级只在 1~5 时可信；拿不准就返回 0，让 LevelJudge 从重要度推。"""
        try:
            value = int(float(item.get("level", 0) or 0))
        except (TypeError, ValueError):
            return 0
        return value if 1 <= value <= 5 else 0

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
    def _render(turns: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for turn in turns:
            is_user = turn.get("role") == "user"
            text = str(turn.get("text") or "").strip().replace("\n", " ")
            if not text:
                continue
            stamp = ""
            if turn.get("ts"):
                stamp = time.strftime("%m-%d %H:%M", time.localtime(int(turn["ts"])))
            # 群里要能看出是谁说的，否则事实会记到别人头上
            name = str(turn.get("sender_name") or "").strip()
            who = name if (is_user and name) else ("用户" if is_user else "机器人")
            lines.append(f"[{turn.get('idx', 0)}] {stamp} {who}: {text}")
        return "\n".join(lines)

    @staticmethod
    def _parse(raw: str) -> list[Any]:
        raw = (raw or "").strip()
        if not raw:
            return []
        # 去掉 ```json 包裹
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            data = json.loads(raw[start : end + 1])
        except ValueError:
            logger.debug("[congling] 记忆抽取返回不是合法 JSON")
            return []
        return data if isinstance(data, list) else []
