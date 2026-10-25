"""人设来源切换的行为验证。

证明三件事：
- builtin：注入插件自带「绫」的人设文本
- astrbot：不注入插件人设，只补关系/状态/场景
- none：同样不注入，且不参与风格约束
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for path in (str(PLUGIN_ROOT), str(PLUGIN_ROOT.parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

from astrbot_plugin_congling.core.config import Config
from astrbot_plugin_congling.humanize.persona import PersonaService


class FakeProbe:
    def should_yield(self, capability: str) -> bool:
        return False


class FakeStore:
    async def kv_get(self, key, default=None):
        return default

    async def kv_set(self, key, value):
        return None


class FakeContext:
    def get_all_stars(self):
        return []


class FakeEvent:
    def get_sender_id(self):
        return "10001"

    def get_group_id(self):
        return None


def make_plugin(mode: str) -> SimpleNamespace:
    cfg = Config({"persona": {"mode": mode}})
    return SimpleNamespace(
        cfg=cfg,
        store=FakeStore(),
        probe=FakeProbe(),
        memory=None,
        context=FakeContext(),
    )


async def run_mode(mode: str) -> str:
    plugin = make_plugin(mode)
    service = PersonaService(plugin)
    await service.load()
    req = SimpleNamespace(system_prompt="")
    await service.inject(FakeEvent(), req)
    return req.system_prompt


async def main() -> int:
    results = {}
    for mode in ("builtin", "astrbot", "none"):
        prompt = await run_mode(mode)
        results[mode] = prompt
        print(f"\n===== mode={mode} =====")
        print(prompt.strip()[:400] if prompt.strip() else "(空，未注入)")

    problems = []
    if "你叫绫" not in results["builtin"]:
        problems.append("builtin 未注入内置人设")
    if "你叫绫" in results["astrbot"]:
        problems.append("astrbot 模式仍然注入了内置人设")
    if "说话方式" in results["astrbot"]:
        problems.append("astrbot 模式仍然注入了插件风格约束")
    if "你叫绫" in results["none"] or "说话方式" in results["none"]:
        problems.append("none 模式仍然注入了人设")
    if "当前状态" not in results["astrbot"]:
        problems.append("astrbot 模式缺少状态层（增强层应保留）")

    if problems:
        print("\n=== 问题 ===")
        for item in problems:
            print("-", item)
        return 1
    print("\n人设来源切换验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
