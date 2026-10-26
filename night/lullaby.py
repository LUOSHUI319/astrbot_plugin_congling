"""睡前道别语。

用户明确要求：不能固定模板，否则一眼假。

所以这里不存完整句子，而是存「零件」，每次现拼：
    开头 + 动作 + 尾巴
三段各抽一个，拼出来就是一句没重复过的话。

不用大模型（省钱），但也留了口子：night.farewell_use_llm 打开就走模型。
"""

from __future__ import annotations

import random
import time
from typing import Any

from ..core.contracts import PerceivedMessage

# ---------------------------------------------------------------- 零件池
# 开头：承接上一句、收住话头
_OPENERS = [
    "", "那我", "那我先", "行了", "好了", "不聊了", "得了", "那啥",
    "诶", "嗯", "好啦",
]

# 动作：夜间用
_NIGHT_ACTS = [
    "去睡了", "睡了", "回被窝了", "下线了", "先撤了", "困死了去睡",
    "躺下了", "眼皮打架了", "撑不住了", "得睡了", "关灯了",
]

# 这些动作是「状态描述」，前面不加"那我"这类承接词，否则读不通
_STATE_ACTS = ("困死了去睡", "眼皮打架了", "撑不住了", "得睡了")

# 动作：午睡用
_NOON_ACTS = [
    "眯一会儿", "睡个午觉", "躺会儿", "打个盹", "补个觉", "午睡去",
]

# 尾巴：叮嘱 / 收尾，不抛问题
_TAILS = [
    "", "你也早点", "晚安", "别熬太晚", "你也歇着", "明天见",
    "睡了啊", "别太晚", "有空再聊", "你也睡吧", "就这样",
    "醒来再说", "好梦",
]

_NOON_TAILS = [
    "", "午安", "一会儿见", "你也歇会儿", "马上回来", "等我醒了说",
]

# 极短兜底，零件拼失败时用（也仍然是随机的）
_FALLBACK = ["睡了", "先睡啦", "晚安", "困了", "下线啦", "去睡了"]

# 这些开头词后面要加逗号，直接连会读成别的词
_COMMA_OPENERS = {"嗯", "诶", "好啦", "行了", "得了", "不聊了"}

_SESSION_TAG = "night.recent_farewell"


class Lullaby:
    """道别语生成器。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.cfg
        self.store = plugin.store
        self._recent: dict[str, list[str]] = {}

    # ------------------------------------------------------------ 主入口
    def compose(
        self,
        *,
        kind: str = "night",
        session_id: str = "",
    ) -> str:
        """拼一句道别语。kind: night / noon。"""
        if self.config.bool("night.farewell_use_llm", False):
            # 交给上层用模型生成，这里返回空串表示"该走模型了"
            return ""

        recent = self._recent.setdefault(session_id or "__all__", [])
        for _ in range(8):
            text = self._assemble(kind)
            if text and text not in recent:
                recent.append(text)
                if len(recent) > 12:
                    del recent[:-12]
                return text
        return random.choice(_FALLBACK)

    def compose_many(
        self,
        session_ids: list[str],
        *,
        kind: str = "night",
    ) -> dict[str, str]:
        """给多个会话各拼一句。

        按两个开关决定要不要统一：
        - farewell_same 开 -> 全部同一句
        - farewell_same 关 + farewell_random 开 -> 掷骰子决定这次统一还是各说各的
        - 都关 -> 强制各不相同
        """
        same_switch = self.config.bool("night.farewell_same", False)
        random_switch = self.config.bool("night.farewell_random", True)

        if not session_ids:
            return {}

        unify = same_switch
        if not same_switch and random_switch:
            unify = random.random() < 0.5

        if unify:
            one = self.compose(kind=kind, session_id="__unified__")
            return {sid: one for sid in session_ids}

        if not random_switch and not same_switch:
            # 强制不同：先取一批互不相同的
            pool: list[str] = []
            for _ in range(len(session_ids) * 4):
                text = self._assemble(kind)
                if text and text not in pool:
                    pool.append(text)
                if len(pool) >= len(session_ids):
                    break
            while len(pool) < len(session_ids):
                pool.append(random.choice(_FALLBACK))
            return {sid: pool[index] for index, sid in enumerate(session_ids)}

        return {sid: self.compose(kind=kind, session_id=sid) for sid in session_ids}

    # ------------------------------------------------------------ 拼装
    def _assemble(self, kind: str) -> str:
        if kind == "noon":
            act = random.choice(_NOON_ACTS)
            tail = random.choice(_NOON_TAILS)
            opener = random.choice(_OPENERS)
        else:
            act = random.choice(_NIGHT_ACTS)
            tail = random.choice(_TAILS)
            opener = random.choice(_OPENERS)

        # 开头偶尔省略，避免每条都"那我"
        if opener and random.random() < 0.35:
            opener = ""
        # 状态型动作前面不加承接词，不然会读成「那眼皮打架了」
        if opener and any(act.startswith(state) for state in _STATE_ACTS):
            opener = ""

        if not opener:
            body = act
        elif opener in _COMMA_OPENERS:
            # 「嗯，困死了去睡」比「嗯困死了去睡」自然
            body = f"{opener}，{act}"
        else:
            body = f"{opener}{act}"

        if tail:
            # 统一用逗号，中文聊天里空格看着像机器拼的
            text = f"{body}，{tail}"
        else:
            text = body

        text = text.strip()
        # 收尾不带句号（跟 guard 的规矩一致）
        while text and text[-1] in "。｡":
            text = text[:-1]
        return text

    # ------------------------------------------------------------ 写记忆用
    @staticmethod
    def is_farewell(text: str) -> bool:
        """粗判一句自己发出去的话是不是道别。"""
        tail_markers = ("晚安", "睡了", "先撤", "下线", "眯一会", "午安", "困")
        return any(marker in (text or "") for marker in tail_markers)
