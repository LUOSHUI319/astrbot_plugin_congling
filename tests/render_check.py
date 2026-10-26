"""页面渲染自检：生成假数据，交给 node 跑渲染函数，检查每页结构。

跑法：
    python tests/render_check.py

检查点：
- 每页都能渲染出内容（不是空白/报错）
- 设置确实分散在各板块，而不是全堆在系统页
- 每页核心配置项数量收敛，其余在「高级」里
- 人格页会随人设来源切换内容
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for path in (str(PLUGIN_ROOT), str(PLUGIN_ROOT.parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

from astrbot_plugin_congling.core.config import Config
from astrbot_plugin_congling.page_api import _schema_groups


def build_data() -> dict:
    groups = _schema_groups(Config())
    return {
        "page/overview": {
            "health": {
                "persona": {"id": "ling", "name": "绫"},
                "uptime": 3720,
                "modules": {
                    "perception": True,
                    "persona": True,
                    "memory": True,
                    "capability": True,
                    "qqext": True,
                    "page_api": True,
                },
                "boot_errors": [],
                "compat": {
                    "yield": {"humanize": False, "memory": False, "rhythm": True},
                    "present": {"astrbot_plugin_reply_delay": ["rhythm"]},
                },
            },
            "stats": {"msg_archive": 1842, "decision_log": 377},
            "memory_active": 12,
            "recent": [
                {"time": "10-21 21:30", "decided": "sent", "reason": "私聊+100 → 100/35"},
                {"time": "10-21 21:28", "decided": "silent", "reason": "群太吵-15 → -15/35"},
            ],
            "mmx": {"available": True, "state": "ready", "reason": ""},
        },
        "page/persona": {
            "mode": "builtin",
            "astrbot_persona_id": "",
            "persona_id": "ling",
            "name": "绫",
            "identity": "你叫绫，二十几岁。",
            "style": {"max_chars": 25, "habits": ["嗯"], "tone": "平静"},
            "taboos": ["不催人睡觉"],
            "active_hours": "09:00-03:30",
            "versions": [{"ts": 1792586000, "persona": {"name": "绫"}}],
            "astrbot_personas": {
                "items": [
                    {
                        "id": "luoshui",
                        "name": "洛水",
                        "preview": "你是洛水…",
                        "prompt": "你是洛水，话密，爱笑。",
                        "length": 10,
                    }
                ],
                "default": "luoshui",
            },
        },
        "page/settings": {
            "groups": groups,
            "config": {},
            "compat": {
                "yield": {"humanize": False, "memory": False, "rhythm": True},
                "present": {},
            },
        },
        "page/memory/list": {
            "total": 4,
            "items": [
                {
                    "id": 1,
                    "kind": "pref",
                    "text": "望山喜欢熬夜",
                    "importance": 0.86,
                    "created_text": "10-21 20:10",
                }
            ],
        },
        "page/tools": {
            "items": [
                {
                    "name": "life.time",
                    "group": "life",
                    "description": "查时间",
                    "calls": 1,
                    "errors": 0,
                    "avg_ms": 3.0,
                }
            ]
        },
        "page/qzone": {
            "mode": "auto",
            "available": True,
            "external": False,
            "write_allowed": True,
            "auth": {
                "ok": True,
                "uin": 10001,
                "source": "onebot:get_cookies",
                "refreshed_at": 1792590000,
                "refreshed_text": "10-21 21:40",
                "fingerprint": "10001_123",
            },
            "note": "",
            "last_error": "",
        },
        # ---- 0.5.0 记忆 2.0 / 头像
        "page/memory/levels": {
            "enabled": True,
            "manual_edit": True,
            "total": 1581,
            "forgetting_mode": "balanced",
            "affinity_enabled": True,
            "levels": [
                {"level": 0, "name": "不可忘", "count": 77, "strength": 1.0},
                {"level": 1, "name": "随口", "count": 2, "strength": 0.35},
                {"level": 2, "name": "细节", "count": 341, "strength": 0.5},
                {"level": 3, "name": "平常事实", "count": 1145, "strength": 0.65},
                {"level": 4, "name": "在意的事", "count": 17, "strength": 0.82},
                {"level": 5, "name": "关键", "count": 1, "strength": 0.95},
            ],
            "tags": [
                {"tag": "identity", "label": "身份信息", "floor": 4, "cap": 0, "boost": 0, "fixed": -1}
            ],
        },
        "page/memory/forgetting": {
            "mode": "balanced",
            "label": "均衡（默认）",
            "days": 45,
            "archive_ratio": 0.45,
            "forget_ratio": 0.12,
            "hit_boost": 1.5,
            "due_now": 12,
            "modes": ["off", "dream", "balanced", "ebbinghaus"],
            "series": {str(lv): [max(0.02, 1 - i / 60.0) for i in range(46)] for lv in range(1, 6)},
            "review_days": {str(lv): lv * 5 for lv in range(0, 6)},
        },
        "page/memory/subjects": {
            "items": [
                {
                    "user_id": "3190568934",
                    "display_name": "望山",
                    "atom_count": 1200,
                    "last_text": "2026-10-25 23:10",
                    "has_profile": True,
                    "affinity": {"score": 640.0, "stage": 3, "stage_name": "朋友"},
                }
            ],
            "profile_enabled": True,
            "whitelist_enabled": False,
            "whitelist": [],
            "blocklist": [],
        },
        "page/affinity": {
            "enabled": True,
            "editable": True,
            "names": ["陌生", "眼熟", "熟人", "朋友", "亲近", "交心"],
            "thresholds": [0, 100, 250, 450, 650, 850],
            "max_daily_delta": 15.0,
            "neglect_days": 3,
            "ranking": [
                {"subject_key": "3190568934", "name": "望山", "score": 640.0, "stage": 3, "stage_name": "朋友"}
            ],
        },
        "page/avatar": {
            "auto_change": True,
            "daily_limit": 1,
            "pool_limit": 30,
            "allow_bot_inject": True,
            "allow_user_request": True,
            "max_kb": 512,
            "available": True,
            "items": [
                {
                    "id": 1,
                    "path": "/demo/a.jpg",
                    "source": "user_upload",
                    "trusted": True,
                    "used_text": "没用过",
                }
            ],
            "pending": [
                {
                    "id": 2,
                    "path": "/demo/b.jpg",
                    "source": "bot_pending",
                    "trusted": False,
                    "used_text": "没用过",
                }
            ],
            "trusted": 1,
            "total": 2,
            "last": {"at": 1792900000, "reason": "auto"},
            "used_today": 0,
        },
        "page/inbox": {"items": []},
        "page/admins": {
            "astrbot": ["10001"],
            "extra": ["10001"],
            "effective": ["10001"],
            "include_group_role": False,
            "roles_note": "AstrBot 名单在 WebUI 全局配置的 admins_id 里改",
        },
    }


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("没找到 node，跳过渲染自检")
        return 0

    env = dict(os.environ)
    env["CONGLING_PREVIEW_DATA"] = json.dumps(build_data(), ensure_ascii=False)
    proc = subprocess.run(
        [node, "tests/render_check.mjs"],
        cwd=str(PLUGIN_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr[-800:])
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
