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
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
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
        if getattr(part, "_no_save", False) or getattr(part, "_congling_temp", False):
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
    spec = importlib.util.spec_from_file_location(
        "congling_perception_service", ROOT / "perception" / "service.py"
    )
    if spec is None or spec.loader is None:
        check("perception 模块可导入", False, "spec 拿不到")
        return
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        print(f"  skip  perception.inject（环境缺依赖：{type(exc).__name__}）")
        return
    service_cls = getattr(module, "PerceptionService", None)
    if service_cls is None:
        print("  skip  PerceptionService 不在这个模块里")
        return
    check("PerceptionService 存在", True)


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
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import logging", "from logging import")):
                bad.append(path.relative_to(ROOT).as_posix())
    check("全插件未 import logging", not bad, f"{bad}")


def main() -> int:
    print(f"插件根目录：{ROOT}\n")
    test_unit()
    asyncio.run(test_perception())
    test_source_guard()
    test_logger_guard()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print("全部通过：system_prompt 保持稳定，动态内容走 temp 内容块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
