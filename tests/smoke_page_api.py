"""页面接口冒烟：验证「UI 点开能拿到数据」。

不需要浏览器，也不需要真实 HTTP：
1. 假 Context 初始化插件 → 收集注册的路由
2. 用框架自己的 _match_registered_web_api 验证路径能匹配上
3. 直接调用不需要 request 的 handler，检查返回信封

这一层能挡住两类问题：路由前缀写错（404）、handler 一调就炸（500）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for path in (str(PLUGIN_ROOT), str(PLUGIN_ROOT.parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

from astrbot_plugin_congling.page_api import PAGE_API_PREFIX

SKIP = {
    "memory_list",
    "memory_search",
    "inbox",
    "settings",
    "save_persona",
    "rollback_persona",
    "save_settings",
    "memory_update",
    "memory_delete",
    "tools_test",
    "qzone_feed",
    "qzone_publish",
    "models_probe",
    "qzone_auth",
    # 0.5.0 新增：都靠请求体传参，脚本里没有 HTTP 上下文
    "memory_levels_save",
    "memory_set_level",
    "memory_forgetting_save",
    "memory_profile_build",
    "memory_profile_save",
    "memory_profile_delete",
    "memory_profile_unblock",
    "profile_save_config",
    "affinity_save",
    "affinity_save_config",
    "avatar_set",
    "avatar_pool_save",
    "memory_subject_detail",
    # 老接口同样靠请求体，之前一直漏在 SKIP 外面
    "night_sleep",
    "night_wake",
    "night_dream_delete",
    "night_dream_pin",
    "night_crawl",
    # 0.7.0 日记：全部走 query / body
    "diary_list",
    "diary_get",
    "diary_generate",
    "diary_delete",
    "diary_pin",
    # 0.7.0 主动找人：now / mute 读请求体
    "proactive_now",
    "proactive_mute",
    # 这个本来就靠 query 里的 session_id，之前漏在 SKIP 外面
    "memory_session_detail",
    "memory_session",
}
"""这些 handler 依请求体或 query（astrbot.api.web.request），
在无 HTTP 上下文的脚本里调不通；它们的异常兜底已经由 _err 覆盖。"""

# 空配置下「返回 error」就是正确行为：这类 handler 要模型 / 开关 / 真实数据才跑得动
ALLOW_ERR = {
    # 做梦：脚本里没模型、配置里 dream_enabled 也是关的，返回「开关关着」是对的
    "night_dream_make",
}


class FakeStar:
    def __init__(self, name: str) -> None:
        self.name = name
        self.root_dir_name = name
        self.activated = True
        self.star_cls = None


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[tuple] = []
        self.stars = [FakeStar("astrbot_plugin_private_companion")]

    def get_all_stars(self):
        return self.stars

    def register_web_api(self, route, handler, methods, desc):
        self.routes.append((route, handler, methods, desc))

    def get_using_provider(self):
        return None

    def get_provider_by_id(self, provider_id):
        return None

    def get_using_stt_provider(self):
        return None

    def get_config(self):
        return {}


async def main() -> int:
    from astrbot.dashboard.api.plugins import _match_registered_web_api

    from astrbot_plugin_congling.main import ConglingPlugin

    context = FakeContext()
    plugin = ConglingPlugin(context, {})
    await plugin.initialize()

    problems: list[str] = []
    print(f"前缀：{PAGE_API_PREFIX}")
    print(f"注册路由：{len(context.routes)} 条\n")

    for route, handler, methods, _desc in context.routes:
        # 1) 路径匹配（模拟前端 /api/plug/<name>/page/xxx）
        relative = route.lstrip("/")
        matched = _match_registered_web_api(context.routes, relative, methods[0])
        if not matched:
            problems.append(f"路由匹配失败: {methods[0]} {route}")
            continue

        # 2) 直接调用 handler（跳过依赖 request 的）
        name = getattr(handler, "__name__", "")
        if name in SKIP:
            print(f"  [skip] {methods[0]:4} {route}（依赖 request）")
            continue
        try:
            result = await handler()
            ok = isinstance(result, dict) and result.get("status") == "ok"
            print(f"  [{'ok ' if ok else 'err'}] {methods[0]:4} {route}")
            if not ok and name not in ALLOW_ERR:
                problems.append(f"返回异常 {route}: {str(result)[:120]}")
            elif not ok:
                print(f"       ↳ 空配置下正常拒（{str(result.get('message') or '')[:40]}）")
        except Exception as exc:
            problems.append(f"调用抛错 {route}: {exc!r}")
            print(f"  [exception] {methods[0]:4} {route}: {exc!r}")

    await plugin.terminate()

    if problems:
        print("\n=== 问题 ===")
        for item in problems:
            print("-", item)
        return 1
    print("\n页面接口冒烟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
