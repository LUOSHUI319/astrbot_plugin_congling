"""路径与目录管理。

所有落盘文件都在 AstrBot 的 plugin_data 目录下，插件目录本身不留运行期数据。
"""

from __future__ import annotations

from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import StarTools

PLUGIN_NAME = "astrbot_plugin_congling"
PAGE_NAME = "fromling"

_cache: dict[str, Path] = {}


def data_dir() -> Path:
    """插件数据根目录（自动创建）。"""
    if "root" in _cache:
        return _cache["root"]
    try:
        root = Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception as exc:  # 极端情况下退回插件同级目录
        logger.warning(f"[congling] 获取数据目录失败，退回本地目录: {exc}")
        root = Path(__file__).resolve().parent.parent / "data"
    root.mkdir(parents=True, exist_ok=True)
    _cache["root"] = root
    return root


def _sub(name: str) -> Path:
    path = data_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    return _sub("cache")


def export_dir() -> Path:
    return _sub("exports")


def backup_dir() -> Path:
    return _sub("backups")


def forward_cache_dir() -> Path:
    return _sub("cache/forward")


def db_path() -> Path:
    return data_dir() / "congling.db"


def plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def schema_path() -> Path:
    return plugin_root() / "_conf_schema.json"


def media_cache_path(key: str) -> Path:
    return forward_cache_dir() / f"{key}.json"
