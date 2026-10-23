"""装配冒烟测试：不启动 AstrBot，用假 Context 走一遍初始化。

目的：把「加载即崩」这类问题挡在交付之前——模块 boot 顺序、DB 建表、
页面路由注册、能力注册表、记忆服务启动。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = PLUGIN_ROOT.parent
for path in (str(PLUGIN_ROOT), str(PLUGINS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


class FakeStar:
    def __init__(self, name: str, root: str) -> None:
        self.name = name
        self.root_dir_name = root
        self.activated = True
        self.star_cls = None


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[tuple] = []
        self.stars: list[FakeStar] = []

    def get_all_stars(self):
        return self.stars

    def register_web_api(self, route, handler, methods, desc):
        self.routes.append((route, methods, desc))

    def get_using_provider(self):
        return None

    def get_provider_by_id(self, provider_id):
        return None

    def get_using_stt_provider(self):
        return None

    def get_config(self):
        return {}

    def get_platform_inst(self, platform_id):
        return None


async def main() -> int:
    from astrbot_plugin_congling.main import ConglingPlugin

    context = FakeContext()
    # 故意塞一个"外部插件"，验证让位逻辑不会崩
    context.stars.append(FakeStar("astrbot_plugin_reply_delay", "astrbot_plugin_reply_delay"))

    plugin = ConglingPlugin(context, {})
    await plugin.initialize()

    health = plugin.health()
    print("=== health ===")
    for key, value in health.items():
        if key in {"compat"}:
            continue
        print(f"{key}: {value}")
    print("=== compat ===")
    print(health["compat"])
    print("=== routes ===")
    for route, methods, _desc in context.routes:
        print(methods, route)

    problems: list[str] = []
    if health["boot_errors"]:
        problems.append("boot_errors: " + "; ".join(health["boot_errors"]))
    if not health["modules"]["page_api"]:
        problems.append("page_api 未就绪")
    if not context.routes:
        problems.append("没有注册任何页面接口")
    if not health["compat"]["yield"]["rhythm"]:
        problems.append("未检测到 reply_delay 让位")
    if plugin.capabilities is None or not plugin.capabilities.caps:
        problems.append("能力注册表为空")

    await plugin.terminate()

    if problems:
        print("\n=== 问题 ===")
        for item in problems:
            print("-", item)
        return 1
    print("\n冒烟测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
