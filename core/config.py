"""配置访问层。

AstrBot 注入的 config 是 dict 子类（AstrBotConfig），但插件页面里可能只传进来
某一段，所以这里提供点路径访问 + 类型转换 + 内存覆写（供 UI 热改）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# 与 _conf_schema.json 保持一致的代码内兜底默认值。
# 作用：schema 缺字段、旧配置升级、单测直接构造，都能正常跑。
DEFAULTS: dict[str, dict[str, Any]] = {
    "basic": {
        "enabled": True,
        "enable_private": True,
        "enable_group": True,
        "platforms": ["aiocqhttp"],
        "extra_admins": [],
        "include_group_role": False,
    },
    "persona": {
        "mode": "builtin",
        "astrbot_persona_id": "",
        "persona_id": "ling",
        "name": "绫",
        "identity": "",
        "active_hours": "09:00-03:30",
        "version_history_limit": 20,
    },
    "humanize": {
        "enable_persona_inject": True,
        "reply_threshold": 35,
        "reply_window_seconds": 300,
        "max_bubbles_private": 3,
        "max_bubbles_group": 2,
        "think_delay_base": 1.2,
        "bubble_delay_factor": 0.09,
        "quote_probability": 0.08,
        "quote_probability_max": 0.35,
        "quote_cooldown_seconds": 30,
        "typo_probability": 0.04,
        "sticker_enabled": True,
        "voice_enabled": True,
        "voice_id": "zhangzixin",
    },
    "guard": {
        "strip_emoji": True,
        "strip_trailing_period": True,
        "flatten_newline": True,
        "max_chars_per_bubble": 60,
        "forbid_phrases": [
            "作为AI",
            "作为一个AI",
            "希望对你有帮助",
            "总结一下",
            "有什么可以帮你",
        ],
        "self_repeat_guard": True,
    },
    "perception": {
        "expand_forward": True,
        "forward_max_nodes": 60,
        "forward_max_depth": 2,
        "forward_summary_threshold": 1200,
        "quote_depth": 3,
        "image_describe": True,
        "voice_transcribe": True,
        "card_parse": True,
        "recent_context_count": 6,
        "recent_context_chars": 1500,
    },
    "memory": {
        "enabled": True,
        "tier": "auto",
        "embedding_provider_id": "",
        "llm_provider_id": "",
        "idle_extract_seconds": 120,
        "batch_extract_size": 20,
        "min_extract_turns": 4,
        "max_atoms_per_extract": 10,
        "keep_context_turns": 4,
        "inject_top_k": 5,
        "inject_budget_ratio": 0.25,
        "cross_session_recall": True,
        "decay_factor": 0.985,
        "archive_after_days": 30,
        "forget_after_days": 90,
        "reflect_hour": 4,
        "levels": {
            "enabled": True,
            "manual_edit": True,
            "tag_weights": {
                "identity": 4,
                "promise": 1,
                "user_stress": 1,
                "taboo": 0,
                "emotion_high": 1,
                "routine": 2,
                "transient": 1,
                "preference": 4,
                "milestone": 5,
            },
        },
        "forgetting": {
            "mode": "balanced",
            "archive_ratio": 0.45,
            "forget_ratio": 0.12,
            "hit_boost": 1.5,
        },
        "affinity": {
            "enabled": True,
            "ui_editable": True,
            "manual_override": False,
            "stage_names": ["陌生", "眼熟", "熟人", "朋友", "亲近", "交心"],
            "stage_thresholds": [0, 100, 250, 450, 650, 850],
            "max_daily_delta": 15.0,
            "neglect_days": 3,
        },
        "user_profile": {
            "enabled": True,
            "whitelist_enabled": False,
            "whitelist": [],
            "blocklist": [],
            "min_stage": 2,
            "max_keywords": 32,
        },
    },
    "capability": {
        "enabled": True,
        "groups": ["life", "entertain", "knowledge", "game", "self"],
        "default_timeout": 8.0,
        "rate_limit_per_min": 20,
        "web_search_enabled": False,
    },
    "qq_extra": {
        "qzone_mode": "auto",
        "qzone_allow_write": True,
        "qzone_auto_post": True,
        "qzone_daily_min": 1,
        "qzone_daily_max": 3,
        "qzone_write_confirm": False,
        "qzone_cookie": "",
        "qzone_page_size": 5,
        "qzone_read_others": True,
        "signature_auto_update": True,
        "avatar_auto_change": True,
        "avatar_daily_limit": 1,
        "avatar_pool_limit": 30,
        "avatar_allow_bot_inject": True,
        "avatar_allow_user_request": True,
        "avatar_max_kb": 512,
        "poke_enabled": True,
        "poke_antipoke": True,
        "poke_talk_probability": 0.45,
        "poke_talk_when_no_antipoke": True,
        "poke_follow_probability": 0.2,
        "poke_cooldown_seconds": 15,
        "poke_notice_in_prompt": True,
        "profile_card_enabled": True,
    },
    "compat": {
        "mode": "auto",
        "yield_humanize": True,
        "yield_memory": True,
        "yield_rhythm": True,
    },
    "night": {
        "enabled": False,
        "allow_sleep": True,
        "noon_fatigue": "13:00-14:30",
        "night_fatigue": "23:30-07:00",
        "energy_max": 100,
        "energy_per_message": 4.0,
        "energy_per_minute": 0.8,
        "energy_recover_per_minute": 1.5,
        "energy_after_wake": 35,
        "min_turns_before_sleep": 8,
        "wake_mode": "private_only",
        "wake_window_minutes": 10,
        "wake_start_count": 2,
        "wake_step_count": 4,
        "wake_time": "08:00",
        "wake_jitter_minutes": 90,
        "latest_sleep_time": "02:30",
        "farewell_before_sleep": True,
        "farewell_same": False,
        "farewell_random": True,
        "farewell_use_llm": False,
        "crawl_enabled": True,
        "crawl_use_llm": False,
        "dream_enabled": False,
        "dream_remember_by_level": True,
        "dream_start_after_minutes": 60,
        "dream_interval_minutes": 60,
        "dream_max_per_sleep": 8,
        "noon_dream_chance": 0.5,
        "noon_nap_minutes": 60,
        "noon_nap_jitter_minutes": 15,
        "dream_tags": ["美梦", "噩梦"],
        "dream_extra_tags": [],
    },
    "close": {
        "enabled": True,
        "max_closing_chars": 40,
    },
    "debug": {"decision_log": True, "log_retention_days": 7, "verbose": False},
}

_MISSING = object()


def _merge(base: dict, incoming: Mapping | None) -> dict:
    result = {key: (dict(value) if isinstance(value, dict) else value) for key, value in base.items()}
    if not isinstance(incoming, Mapping):
        return result
    for key, value in incoming.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """只读为主、可热改的配置视图。"""

    def __init__(self, raw: Mapping | None = None) -> None:
        self._raw: dict[str, Any] = _merge(DEFAULTS, raw)
        self._overrides: dict[str, Any] = {}

    # ---------------------------------------------------------------- 读取
    def get(self, path: str, default: Any = None) -> Any:
        if path in self._overrides:
            return self._overrides[path]
        node: Any = self._raw
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        if node is None:
            return default
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return dict(value) if isinstance(value, Mapping) else {}

    def as_dict(self) -> dict[str, Any]:
        result = _merge(self._raw, None)
        for path, value in self._overrides.items():
            node = result
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    break
            else:
                node[parts[-1]] = value
        return result

    # ---------------------------------------------------------------- 类型化
    def bool(self, path: str, default: bool = False) -> bool:
        value = self.get(path, _MISSING)
        if value is _MISSING:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是"}
        return bool(value)

    def int(self, path: str, default: int = 0) -> int:
        try:
            return int(self.get(path, default))
        except (TypeError, ValueError):
            return default

    def float(self, path: str, default: float = 0.0) -> float:
        try:
            return float(self.get(path, default))
        except (TypeError, ValueError):
            return default

    def str(self, path: str, default: str = "") -> str:
        value = self.get(path, default)
        return value if isinstance(value, str) else str(value) if value is not None else default

    def list(self, path: str, default: list | None = None) -> list:
        value = self.get(path, None)
        if isinstance(value, (list, tuple)):
            return [item for item in value]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return list(default or [])

    # ---------------------------------------------------------------- 写入
    def override(self, path: str, value: Any) -> None:
        """内存覆写（UI 即时生效，不回写 AstrBot 配置文件）。"""
        self._overrides[path] = value

    def clear_overrides(self) -> None:
        self._overrides.clear()

    def apply(self, payload: Mapping | None) -> dict[str, Any]:
        """把一份嵌套配置合并进运行时配置（UI 保存后用）。

        与 override 的区别：apply 会真的改动 _raw，也就是后续所有读取都变了；
        override 只是临时覆盖，重启即失效。
        """
        self._raw = _merge(self._raw, payload)
        self._overrides.clear()
        return self.as_dict()

    def snapshot(self) -> dict[str, Any]:
        return self.as_dict()

    # ---------------------------------------------------------------- 便捷
    @property
    def persona_id(self) -> str:
        return self.str("persona.persona_id", "ling") or "ling"

    @property
    def persona_name(self) -> str:
        return self.str("persona.name", "绫") or "绫"

    def platforms(self) -> list[str]:
        items = [str(item).lower() for item in self.list("basic.platforms", ["aiocqhttp"])]
        return items or ["aiocqhttp"]
