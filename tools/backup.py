"""插件备份。

规则（望山定的）：
    同时存在四个版本 = 当前工作版 + 3 份备份
    再改动一次就先备份，备份超过 3 份就删掉最老的那份

用法：
    python backup.py            # 备份一次（改动前跑）
    python backup.py --list     # 看现有备份
    python backup.py --restore 20261023_1358   # 从某份备份恢复

备份放在 E:\AI工作区\congling_backups，不放在 plugins 目录里——
放那里会被 AstrBot 当成插件扫描。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

PLUGIN_SRC = Path(r"E:\BotLauncher\AstrBot\AstrBot\data\plugins\astrbot_plugin_congling")
BACKUP_ROOT = Path(r"E:\AI工作区\congling_backups")
MAX_BACKUPS = 3

# 不备份的东西：缓存、临时文件、编辑器垃圾
EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "node_modules"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".tmp", ".bak"}


def _ignore(directory: str, names: list[str]) -> set[str]:
    skipped: set[str] = set()
    for name in names:
        if name in EXCLUDE_DIRS:
            skipped.add(name)
            continue
        if Path(name).suffix.lower() in EXCLUDE_SUFFIX:
            skipped.add(name)
    return skipped


def existing() -> list[Path]:
    if not BACKUP_ROOT.exists():
        return []
    items = [p for p in BACKUP_ROOT.iterdir() if p.is_dir() and p.name.startswith("astrbot_plugin_congling_")]
    return sorted(items, key=lambda p: p.name)


def snapshot(note: str = "") -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_ROOT / f"astrbot_plugin_congling_{stamp}"
    counter = 1
    while dest.exists():
        dest = BACKUP_ROOT / f"astrbot_plugin_congling_{stamp}_{counter}"
        counter += 1

    shutil.copytree(PLUGIN_SRC, dest, ignore=_ignore)

    meta = {
        "created_at": int(time.time()),
        "created_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
        "source": str(PLUGIN_SRC),
    }
    (dest / "_backup_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    pruned = prune()
    print(f"已备份 -> {dest.name}")
    if note:
        print(f"  备注：{note}")
    if pruned:
        print(f"  删掉最老的 {len(pruned)} 份：{', '.join(p.name for p in pruned)}")
    print(f"  当前共存 {len(existing()) + 1} 个版本（含工作版）")
    return dest


def prune() -> list[Path]:
    """只留最新 MAX_BACKUPS 份，多的从最老的开始删。"""
    items = existing()
    if len(items) <= MAX_BACKUPS:
        return []
    removed: list[Path] = []
    for old in items[: len(items) - MAX_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old)
    return removed


def show() -> None:
    items = existing()
    if not items:
        print("还没有备份")
        return
    print(f"备份目录：{BACKUP_ROOT}")
    print(f"工作版  ：{PLUGIN_SRC}")
    for idx, item in enumerate(items, 1):
        meta = {}
        meta_file = item / "_backup_meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        mark = "最老" if idx == 1 and len(items) > 1 else ("最新" if idx == len(items) else "   ")
        print(f"  {idx}. {item.name}  {mark}  {meta.get('note', '')}")
    print(f"共 {len(items)} 份备份 + 1 个工作版")


def restore(name: str, dry: bool = False) -> int:
    target = BACKUP_ROOT / name
    if not target.exists():
        matches = [p for p in existing() if name in p.name]
        if not matches:
            print(f"找不到备份：{name}")
            return 1
        target = matches[-1]

    if not PLUGIN_SRC.exists():
        print(f"工作版不存在：{PLUGIN_SRC}")
        return 1

    if dry:
        print(f"会把 {target.name} 覆盖到 {PLUGIN_SRC}")
        return 0

    # 先给当前状态留一份，免得恢复错了没退路
    snapshot(note=f"恢复 {target.name} 之前的自动备份")
    for item in target.iterdir():
        if item.name == "_backup_meta.json":
            continue
        dest = PLUGIN_SRC / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(item, dest, ignore=_ignore)
        else:
            shutil.copy2(item, dest)
    print(f"已从 {target.name} 恢复")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="congling 插件备份")
    parser.add_argument("--list", action="store_true", help="列出备份")
    parser.add_argument("--restore", metavar="NAME", help="从备份恢复")
    parser.add_argument("--dry", action="store_true", help="只演示，不动文件")
    parser.add_argument("--note", default="", help="给这份备份加个备注")
    args = parser.parse_args()

    if args.list:
        show()
        return 0
    if args.restore:
        return restore(args.restore, dry=args.dry)
    snapshot(note=args.note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
