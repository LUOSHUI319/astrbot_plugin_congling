"""persona_id 不可变守卫。

改个名字顺手把 ID 也改了，等于换一个记忆分区：旧记忆不会消失，但会
「看起来全没了」，而且当时没有任何提示。这组断言就是钉住这件事。

独立跑：
    E:\\BotLauncher\\python\\python.exe tests/test_persona_id_lock.py
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_congling.humanize.persona import PersonaService  # noqa: E402


class FakeCfg:
    def __init__(self, values: dict | None = None) -> None:
        self.v = values or {}

    @property
    def persona_id(self) -> str:
        return str(self.v.get("persona.persona_id", "ling"))

    @property
    def persona_name(self) -> str:
        return str(self.v.get("persona.name", "绫"))

    def str(self, key: str, default: str = "") -> str:
        return str(self.v.get(key, default))

    def int(self, key: str, default: int = 0) -> int:
        return int(self.v.get(key, default))

    def float(self, key: str, default: float = 0.0) -> float:
        return float(self.v.get(key, default))

    def bool(self, key: str, default: bool = False) -> bool:
        return bool(self.v.get(key, default))

    def list(self, key: str, default=None):
        return self.v.get(key, default if default is not None else [])

    def get(self, key: str, default=None):
        return self.v.get(key, default)


class FakeStore:
    def __init__(self) -> None:
        self.kv: dict = {}

    async def kv_get(self, key: str, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key: str, value) -> None:
        self.kv[key] = value


class FakePlugin:
    def __init__(self) -> None:
        self.cfg = FakeCfg({"persona.persona_id": "ling", "persona.name": "绫"})
        self.store = FakeStore()


def build() -> tuple[PersonaService, FakePlugin]:
    plugin = FakePlugin()
    return PersonaService(plugin), plugin


class PersonaIdLockTest(unittest.TestCase):
    def test_rename_keeps_persona_id(self):
        svc, plugin = build()
        before = svc.get().persona_id
        asyncio.run(svc.save({"name": "洛水"}))
        self.assertEqual(svc.get().persona_id, before)
        self.assertEqual(svc.get().name, "洛水")
        self.assertIn("persona:ling", plugin.store.kv)

    def test_reject_different_id(self):
        svc, plugin = build()
        snapshot = dict(plugin.store.kv)
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(svc.save({"persona_id": "someone_else", "name": "冒名"}))
        # 只断言「被拒」，不断言具体措辞——提示文案会改，行为不能改
        self.assertIn("不可修改", str(ctx.exception))
        # 拒绝之后，内存态与库都不能被动过
        self.assertEqual(svc.get().persona_id, "ling")
        self.assertNotEqual(svc.get().name, "冒名")
        self.assertEqual(plugin.store.kv, snapshot)

    def test_same_id_still_accepted(self):
        """老前端会把当前 id 原样回传，这条不能报错。"""
        svc, _ = build()
        asyncio.run(svc.save({"persona_id": "ling", "name": "绫酱"}))
        self.assertEqual(svc.get().persona_id, "ling")
        self.assertEqual(svc.get().name, "绫酱")

    def test_version_history_keeps_persona_id(self):
        svc, plugin = build()
        asyncio.run(svc.save({"name": "洛水"}))
        versions = plugin.store.kv.get("persona:ling:versions") or []
        self.assertTrue(versions)
        self.assertEqual(
            str(versions[0].get("persona", {}).get("persona_id") or ""), "ling"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
