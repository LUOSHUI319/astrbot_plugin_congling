"""记忆抽取：把一段聊天压成几条能长期用的事实。

三条铁律：
1. 只从**用户说的话**里抽事实，模型自己的回复不作为事实来源（防自我强化）；
2. 一条记忆必须能独立读懂——带主语、带具体指代，不能是「他昨天说的那个」；
3. 宁少勿滥。抽 3 条准的，胜过抽 10 条半对半错的。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from astrbot.api import logger

from ..core.contracts import MemoryDraft
from ..core.modelhub import provider_request_budget
from .perspective import narrator_name
from .refs import actor_ref
from ..core.exc import exc_brief

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
补充来源规则：只记用户明确说出的事实。evidence 只引用用户消息序号，不引用机器人消息；不要记录对机器人错话的反省或解释。没有明确依据时不要补充性别。
11. 只从 user 的明确陈述提取事实；assistant 只用于理解指代，不能作为事实证据。问句、假设、转述的不确定内容不当成事实。
12. 机器人本人名为 {bot_name}。只有机器人本人可在 text 中称「我」，subject 固定为 __self__。
    人类发言中的「我」指该发言者，不是机器人；记人类事实时 text 用其明确姓名（无姓名用 ID），subject 用该人的 ID。
13. 不用「用户/聊天对象/对方」代替已知姓名；第三人身份无法确认时宁可不抽，不根据人名创建用户。
14. evidence 必须是支持该事实的 user 消息序号，至少一个，不能编造。不要把提问「你困吗」记成「我困了」。
15. 主体必须唯一命中证据中的发言者 ID 或已确认别名；同名无法区分时不抽。
16. 句首人称必须纠正：人类说「我今天很累」应写成「当前昵称今天很累」；
    只有明确关于 {bot_name} 的用户陈述才可写成「我…」，不得把用户自述移到 __self__。
17. 证据必须支持 text 的具体内容、肯否与主体，不是仅仅出现相关词。尽量保留陈述原话。

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
        # 上一次真正送进模型的 turn 下标，用来决定水位推到哪里
        self.consumed_turns: list[int] = []

    async def extract(self, turns: list[dict[str, Any]]) -> list[MemoryDraft]:
        self.last_ok = False
        if not turns:
            return []
        bot_name = await narrator_name(self.plugin)
        conversation, consumed = self._render(turns, bot_name=bot_name, persona_id=self.config.persona_id)
        self.consumed_turns = consumed
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
            .replace("{bot_name}", bot_name)
            .replace("{max_atoms}", str(max_atoms))
            .replace("{conversation}", conversation)
        )
        try:
            # 十条通常只需约 2k 输出；高条数配置相应放宽，避免截断 JSON 丢水位。
            output_limit = max(2048, max_atoms * 256)
            async with asyncio.timeout(90):
                with provider_request_budget(provider, output_limit) as options:
                    resp = await provider.text_chat(prompt=prompt, contexts=[], **options)
            raw = str(getattr(resp, "completion_text", "") or "")
        except Exception as exc:
            logger.warning(f"[congling] 记忆抽取调用失败: {exc_brief(exc)}")
            return []

        payload = self._parse(raw)
        # 解析成功（哪怕返回空数组）才允许推水位；只有 "[" 不算
        self.last_ok = isinstance(payload, list)
        if payload is None:
            payload = []
        try:
            await self.plugin.store.kv_set(
                "memory:last_raw",
                {"at": int(time.time()), "raw_len": len(raw), "items": len(payload), "head": raw[:240]},
            )
        except Exception:
            pass
        drafts: list[MemoryDraft] = []
        consumed_ids = set(map(str, consumed))
        user_ids = {str(turn.get("idx", 0)) for turn in turns
                    if turn.get("role") == "user" and str(turn.get("idx", 0)) in consumed_ids}
        for item in payload[: max(1, max_atoms)]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if len(text) < 4:
                continue
            raw_evidence = item.get("evidence") or []
            if not isinstance(raw_evidence, (list, tuple)):
                raw_evidence = [raw_evidence]
            evidence = [str(x).strip()[:40] for x in raw_evidence]
            # 防止机器人自己的回复被写成事实，也不接受未送入本批的消息序号。
            if evidence and not set(evidence).issubset(user_ids):
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
                    evidence_msg_ids=evidence[:5],
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
    def _render(turns: list[dict[str, Any]], limit: int = MAX_INPUT_CHARS, *,
                bot_name: str = "", persona_id: str = "") -> tuple[str, list[int]]:
        """拼成对话文本，同时返回**真的被送进模型**的那些 turn 下标。

        以前是先拼全文再在 prompt 里切 [:6000]，切掉的尾巴没人知道，
        水位却照样推到整批末尾 —— 那部分内容等于被静默丢弃。
        """
        lines: list[str] = []
        used: list[int] = []
        total = 0
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
            sid = str(turn.get("sender_id") or "").strip()
            actor = str(turn.get("actor_ref") or actor_ref(turn.get("platform"), sid,
                        persona_id=persona_id if not is_user else None))
            who = (f"user {name or sid or '身份未知'} (ID={sid}, actor_ref={actor or 'unresolved'})" if is_user
                   else f"assistant {bot_name or actor or '身份未知'} (ID={persona_id}, actor_ref={actor or 'unresolved'}；仅上下文)")
            line = f"[{turn.get('idx', 0)}] {stamp} {who}: {text}"
            if lines and total + len(line) + 1 > limit:
                break
            lines.append(line)
            used.append(int(turn.get("idx", 0)))
            total += len(line) + 1
        return "\n".join(lines), used

    @staticmethod
    def _parse(raw: str) -> list[Any] | None:
        """解析成功返回 list（可能为空），失败返回 None。"""
        raw = (raw or "").strip()
        if not raw:
            return None
        # 去掉 ```json 包裹
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except ValueError:
            logger.warning("[congling] 记忆抽取返回不是合法 JSON")
            return None
        return data if isinstance(data, list) else []
