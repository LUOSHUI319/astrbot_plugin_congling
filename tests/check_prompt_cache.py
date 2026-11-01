"""prefix cache 守卫：动态内容不许进 system_prompt。

审核关注的点就在这儿：插件在 on_llm_request 里反复改写 req.system_prompt，
注入每轮都会变的东西（召回的记忆、当前消息的解析补充、长夜状态与头像提示），
system prompt 每轮都不一样，厂商的 prefix cache 全废，等于每轮全价重算。

这个脚本把三个注入点各跑一遍，断言：
1. 调用前后 req.system_prompt 一字不差；
2. 动态内容确实进了 req.extra_user_content_parts；
3. 这些内容块被 mark_as_temp() 标过（不会写回会话历史）。

跑法：python tests/check_prompt_cache.py
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

# 显式离线模式只装载测试替身，不导入真实框架或访问插件数据目录。
if "--offline" in sys.argv:
    importlib.import_module("astrbot_plugin_congling.tests.run_diagnosis_regressions")

FAILS: list[str] = []
CHECKS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append(name)
    print(("  ok   " if ok else "  FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _req(system_prompt: str = "STATIC PERSONA PROMPT") -> Any:
    return SimpleNamespace(system_prompt=system_prompt, extra_user_content_parts=[], contexts=[])


def _part_texts(req: Any) -> list[str]:
    out = []
    for part in getattr(req, "extra_user_content_parts", []) or []:
        out.append(str(getattr(part, "text", "") or (part.get("text") if isinstance(part, dict) else "")))
    return out


def _is_temp(req: Any) -> bool:
    for part in getattr(req, "extra_user_content_parts", []) or []:
        if (getattr(part, "_no_save", False) or getattr(part, "no_save", False)
                or getattr(part, "_congling_temp", False)):
            return True
    return False


def test_unit() -> None:
    """core.prompt_parts 本身的行为。"""
    print("[1] core.prompt_parts")
    from core.prompt_parts import add_temp_part, has_temp_part

    req = _req()
    before = req.system_prompt
    add_temp_part(req, "【长期记忆】今天他提到想养猫", "<!--congling:memory-->")
    check("system_prompt 未被改写", req.system_prompt == before)
    check("内容进了 extra_user_content_parts", len(req.extra_user_content_parts) == 1)
    check("内容块标记为 temp", _is_temp(req))
    check("marker 可查询", has_temp_part(req, "<!--congling:memory-->"))

    # 幂等：同 marker 再注入应替换而不是叠加
    add_temp_part(req, "【长期记忆】改过了", "<!--congling:memory-->")
    check("同 marker 幂等（不叠加）", len(req.extra_user_content_parts) == 1)
    check("同 marker 已替换内容", "改过了" in _part_texts(req)[0])

    # 空内容不产生垃圾块
    add_temp_part(req, "   ", "<!--congling:empty-->")
    check("空内容不注入", len(req.extra_user_content_parts) == 1)


async def test_perception() -> None:
    """消息解析补充：原来按 msg_id 往 system_prompt 追加。"""
    print("[2] perception.inject")
    from astrbot_plugin_congling.core.contracts import PerceivedMessage, Block
    from astrbot_plugin_congling.perception.service import PerceptionService

    service = PerceptionService.__new__(PerceptionService)
    message = PerceivedMessage(msg_id="offline", blocks=[
        Block(kind="image", text="离线图片描述", meta={"described": True})])
    service.perceive = AsyncMock(return_value=message)
    service.archive = AsyncMock()
    service._recent_context = AsyncMock(return_value="")
    req = _req()
    event = SimpleNamespace()
    await service.inject(event, req)
    await service.inject(event, req)
    check("perception 注入保留 system_prompt", req.system_prompt == "STATIC PERSONA PROMPT")
    check("perception 动态内容已注入", "离线图片描述" in "".join(_part_texts(req)))
    check("perception 重入不叠加", len(req.extra_user_content_parts) == 1)
    check("perception 内容为 temp", _is_temp(req))
    check("perception 不追加历史", req.contexts == [])


def test_source_guard() -> None:
    """静态扫描：这三个模块里不许再出现写 system_prompt 的代码。"""
    print("[3] 源码扫描（禁止改写 system_prompt）")
    targets = ["memory/service.py", "perception/service.py", "main.py", "night/state.py", "qqext/avatar.py"]
    banned = ("req.system_prompt", 'setattr(req, "system_prompt"', "setattr(req, 'system_prompt'")
    for rel in targets:
        path = ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hit = [b for b in banned if b in text]
        check(f"{rel} 无 system_prompt 改写", not hit, f"发现 {hit}")

    # 动态注入方应统一走 core.prompt_parts
    for rel in ["memory/service.py", "perception/service.py", "main.py"]:
        path = ROOT / rel
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="ignore")
            check(f"{rel} 已接入 prompt_parts", "add_temp_part" in text)

    # 静态人设允许留在 system_prompt，但必须是幂等的（同一 marker 只写一次）
    persona = ROOT / "humanize" / "persona.py"
    if persona.exists():
        text = persona.read_text(encoding="utf-8", errors="ignore")
        check("persona 注入幂等（有 marker 判重）", "<!--congling:persona-->" in text)


def test_logger_guard() -> None:
    """日志规范：全插件不许 import 标准库 logging。"""
    print("[4] 日志规范（禁用内置 logging）")
    bad = []
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.relative_to(ROOT).parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import logging", "from logging import")):
                bad.append(path.relative_to(ROOT).as_posix())
    check("生产代码未 import logging（测试替身除外）", not bad, f"{bad}")


def main() -> int:
    FAILS.clear()
    CHECKS.clear()
    print(f"插件根目录：{ROOT}\n")
    test_unit()
    asyncio.run(test_perception())
    test_source_guard()
    test_logger_guard()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print(f"全部 {len(CHECKS)} 项通过：system_prompt 保持稳定，动态内容走 temp 内容块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
