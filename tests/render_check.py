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
                "uin": 3190568934,
                "source": "onebot:get_cookies",
                "refreshed_at": 1792590000,
                "refreshed_text": "10-21 21:40",
                "fingerprint": "3190568934_123",
            },
            "note": "",
            "last_error": "",
        },
        "page/inbox": {"items": []},
        "page/admins": {
            "astrbot": ["3190568934"],
            "extra": ["10001"],
            "effective": ["3190568934", "10001"],
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
