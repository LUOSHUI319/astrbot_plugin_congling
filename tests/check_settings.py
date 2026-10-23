"""配置分区与类型转换的自检（不需要 HTTP）。"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for path in (str(PLUGIN_ROOT), str(PLUGIN_ROOT.parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

from astrbot_plugin_congling.core.config import Config
from astrbot_plugin_congling.page_api import _coerce, _schema_groups, _schema_types


def main() -> int:
    cfg = Config()
    groups = _schema_groups(cfg)
    types = _schema_types()

    print(f"分区数：{len(groups)}")
    for group in groups:
        print(f"  {group['name']:12} {group['title']:6} items={len(group['items'])}")

    print("\n类型抽样：")
    for key in ("persona.mode", "humanize.reply_threshold", "guard.forbid_phrases", "basic.platforms"):
        print(f"  {key:32} {types.get(key)}")

    print("\n转换：")
    print("  bool 'false' ->", _coerce("bool", "false"))
    print("  bool '开'    ->", _coerce("bool", "开"))
    print("  int '42'     ->", _coerce("int", "42"))
    print("  float '0.5'  ->", _coerce("float", "0.5"))
    print("  list 'a\\nb,c'->", _coerce("list", "a\nb,c"))

    cfg.apply({"persona": {"mode": "astrbot", "astrbot_persona_id": "abc"}, "humanize": {"reply_threshold": 60}})
    print("\napply 后：mode =", cfg.str("persona.mode"), "/ id =", cfg.str("persona.astrbot_persona_id"),
          "/ threshold =", cfg.int("humanize.reply_threshold"))

    # 重新读一次分区：UI 保存后就是这么拿新值的
    groups_after = _schema_groups(cfg)
    persona_group = next((g for g in groups_after if g["name"] == "persona"), None)
    mode_item = next((i for i in persona_group["items"] if i["key"] == "mode"), None) if persona_group else None
    print("人设来源项：", mode_item)

    problems = []
    if len(groups) < 10:
        problems.append("分区数量不足")
    if not mode_item or mode_item.get("value") != "astrbot":
        problems.append("mode 项未反映 apply 后的值")
    if cfg.int("humanize.reply_threshold") != 60:
        problems.append("apply 未生效")

    if problems:
        print("\n问题：", problems)
        return 1
    print("\n配置自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
