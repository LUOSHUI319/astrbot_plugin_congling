"""醒后发送顺序、故障重入和唯一人格回归；所有模型/平台均为离线替身。"""
import asyncio
import ast
import importlib
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import night_selftest as bootstrap

bootstrap._prepare()
for name in ("cg.humanize",):
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(Path(bootstrap.BASE) / "humanize")]
    sys.modules[name] = pkg
events = types.ModuleType("astrbot.api.event")
events.MessageChain = lambda chain: SimpleNamespace(chain=chain)
sys.modules[events.__name__] = events
components = types.ModuleType("astrbot.api.message_components")
components.Plain = lambda text: SimpleNamespace(text=text)
sys.modules[components.__name__] = components
Config = importlib.import_module("cg.core.config").Config
Store = importlib.import_module("cg.core.store").Store
PersonaService = importlib.import_module("cg.humanize.persona").PersonaService
Proactive = importlib.import_module("cg.humanize.proactive").Proactive
ModelHub = importlib.import_module("cg.core.modelhub").ModelHub
NightModule = bootstrap._load("cg.night", "night/__init__.py").NightModule
rollup_mod = importlib.import_module("cg.night.rollup")
flow_mod = importlib.import_module("cg.night.wake_flow")
add_temp_part = importlib.import_module("cg.core.prompt_parts").add_temp_part
has_temp_part = importlib.import_module("cg.core.prompt_parts").has_temp_part


def main_hook(name):
    """直接编译入口中的实际钩子，隔离完整 AstrBot 启动和其他模块。"""
    tree = ast.parse((Path(bootstrap.BASE) / "main.py").read_text(encoding="utf-8-sig"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ConglingPlugin")
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    method.decorator_list = []
    namespace = {"Any": object, "AstrMessageEvent": object, "time": time,
                 "logger": sys.modules["astrbot.api"].logger, "add_temp_part": add_temp_part,
                 "has_temp_part": has_temp_part}
    helpers = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_congling_msg_id"]
    exec(compile(ast.Module(body=[*helpers, method], type_ignores=[]), "actual_main_hook", "exec"), namespace)
    return namespace[name]


class Manager:
    default_persona = "大小姐"

    def __init__(self):
        self.items = {
            "大小姐": SimpleNamespace(persona_id="大小姐", system_prompt="你是大小姐。\n" + "完整人设" * 1800),
            "另一个": SimpleNamespace(persona_id="另一个", system_prompt="你是另一个角色"),
        }

    async def get_persona(self, pid):
        if pid not in self.items:
            raise ValueError("找不到指定人格")
        return self.items[pid]

    async def get_all_personas(self):
        return list(self.items.values())

    def get_persona_v3_by_id(self, pid):
        return {"_begin_dialogs_processed": [{"role": "assistant", "content": pid + "开场"}]}


class FlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")
        await self.store.open()
        self.cfg = Config({"persona": {"mode": "astrbot", "astrbot_persona_id": "大小姐"},
                           "night": {"enabled": True, "night_fatigue": "", "noon_fatigue": "",
                                     "latest_sleep_time": "", "rollup": {"enabled": True, "chunk_size": 1}}})
        self.sent = []
        self.model_calls = []
        async def send(umo, chain):
            self.sent.extend(c.text for c in chain.chain)
            return True
        async def text_chat(**kwargs):
            self.model_calls.append(kwargs)
            prompt = kwargs["prompt"]
            if "只输出一个 JSON" in prompt:
                return SimpleNamespace(completion_text=json.dumps({
                    "summary": "有人询问视频",
                    "items": [{"msg_id": "sleep-question", "who": "AKI", "what": "视频什么时候做？",
                               "targets_me": True, "answered": False, "priority": 100}],
                }))
            return SimpleNamespace(completion_text="补课答复" if "合并成一段" in prompt else "主动正文")
        self.provider = SimpleNamespace(text_chat=text_chat,
                                        provider_config={"id": "offline-summary", "model": "test-model"})
        self.context = SimpleNamespace(persona_manager=Manager(), send_message=send,
                                       get_using_provider=lambda: self.provider,
                                       get_using_provider_async=AsyncMock(return_value=self.provider))
        self.plugin = SimpleNamespace(cfg=self.cfg, store=self.store, context=self.context,
                                      composer=None, memory=None, decider=None,
                                      probe=SimpleNamespace(should_yield=lambda _: False))
        self.plugin.persona = PersonaService(self.plugin)
        self.plugin.models = ModelHub(self.plugin)
        self.plugin.proactive = Proactive(self.plugin)
        self.plugin.night = NightModule(self.plugin)
        self.night = self.plugin.night
        now = int(time.time())
        self.since, self.wake = now - 900, now - 400
        self.night.state.state.last_wake_at = self.wake
        self.night.state.state.last_wake_reason = "message"
        self.night.state.state.energy = 40
        # 醒后发送测试必须明确标记真实待补课状态，不能把 awake 当作刚醒。
        self.night.state.state.mode = "waking"
        self.night.state.state.enter_waking_at = now
        self.night.state.state.pending_crawl = ["s"]
        await self.store.kv_set("night.last_sleep_window",
                                [{"sleep_at": self.since, "wake_at": self.wake, "kind": "night"}])
        self.night.crawler.crawl_line = AsyncMock(return_value="爬楼语")
        self.row = {"session_id": "s", "umo": "qq:GroupMessage:s", "session_kind": "group"}
        await self.archive("old", self.since - 1)
        await self.archive("sleep-question", self.since + 10)
        await self.archive("new", self.wake + 1)

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    async def archive(self, msg_id, ts, sender="u"):
        await self.store.execute(
            "INSERT INTO msg_archive(msg_id,session_id,session_kind,sender_id,sender_name,ts,text) "
            "VALUES(?,?,?,?,?,?,?)", (msg_id, "s", "group", sender, "绫" if sender == "bot" else "AKI",
                                     ts, "视频什么时候做？"))

    async def prepare(self):
        await self.night.wake_flow.prepare(
            "s", self.row["umo"],
            lambda text, phase, role: self.plugin.proactive.send_stage(self.row, text, phase, role))

    async def test_waking_timeout_does_not_skip_preparation(self):
        self.night.state.state.mode = "waking"
        self.night.state.state.enter_waking_at = self.wake
        self.night.state.state.pending_crawl = ["s"]
        await self.night.state.finish_waking()
        self.assertEqual(self.night.state.state.last_wake_at, self.wake)
        result = await self.plugin.proactive.send_one(self.row)
        self.assertTrue(result["ok"])
        self.assertEqual(self.sent, ["主动正文"])
        self.assertEqual(await self.store.scalar(
            "SELECT COUNT(*) FROM msg_archive WHERE COALESCE(handled,0)=0"), 0)
        self.assertTrue(any("msg_id=old" in c["prompt"] for c in self.model_calls))
        self.assertEqual(result["persona"], "大小姐")
        rows = await self.store.fetch_all("SELECT sender_name FROM msg_archive WHERE sender_id='bot'")
        self.assertEqual({r["sender_name"] for r in rows}, {"大小姐"})
        log = await self.store.fetch_one("SELECT persona_id FROM proactive_log")
        self.assertEqual(log["persona_id"], "大小姐")

    async def test_concurrent_normal_and_proactive_prepare_once(self):
        await asyncio.gather(*(self.prepare() for _ in range(12)))
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])
        await self.prepare()
        self.assertEqual(len(self.sent), 2)

    def event(self):
        extras = {}
        return SimpleNamespace(
            unified_msg_origin=self.row["umo"], get_group_id=lambda: "s", get_sender_id=lambda: "u",
            get_message_id=lambda: "new", set_extra=lambda k, v: extras.update({k: v}),
            get_extra=lambda k: extras.get(k), stop_event=lambda: extras.update({"stopped": True}),
            extras=extras,
        )

    async def test_real_persona_hook_prepares_before_main_reply(self):
        self.plugin.enabled_for = lambda _: True
        self.plugin.qqext = self.plugin.diary = self.plugin.capabilities = None
        event = self.event()
        request = SimpleNamespace(system_prompt="基础说明", contexts=[])
        await main_hook("persona_hook")(self.plugin, event, request)
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])
        self.assertIn("你是大小姐", request.system_prompt)
        self.assertFalse(event.extras.get("stopped"))
        self.assertIn("已经发送", str(request.extra_user_content_parts))
        self.assertIn("有人询问视频", str(request.extra_user_content_parts))
        self.assertGreaterEqual(event.get_extra("congling:wake_prepare_seconds"), 0)
        await main_hook("persona_hook")(self.plugin, event, request)
        self.assertEqual(len(self.sent), 2)

    async def test_real_persona_hook_failure_stops_main_reply(self):
        self.plugin.enabled_for = lambda _: True
        self.plugin.models.chat = AsyncMock(return_value="")
        event = self.event()
        await main_hook("persona_hook")(self.plugin, event, SimpleNamespace(system_prompt="", contexts=[]))
        self.assertTrue(event.extras["stopped"])
        self.assertTrue(event.extras["congling:wake_blocked"])
        self.assertEqual(self.sent, ["爬楼语"])

    async def test_express_hook_revokes_old_role_response(self):
        self.plugin.enabled_for = lambda _: True
        self.plugin.diag = None
        event = self.event()
        event.set_extra("congling:persona_token", (await self.plugin.persona.resolve())["token"])
        self.cfg.override("persona.astrbot_persona_id", "另一个")
        result = SimpleNamespace(chain=[SimpleNamespace(text="旧人格回复")])
        event.get_result = lambda: result
        await main_hook("express_hook")(self.plugin, event)
        self.assertEqual(result.chain, [])

    async def test_express_hook_revokes_reply_when_selected_persona_disappears(self):
        self.plugin.enabled_for = lambda _: True
        self.plugin.diag = None
        event = self.event()
        event.set_extra("congling:persona_token", (await self.plugin.persona.resolve())["token"])
        del self.context.persona_manager.items["大小姐"]
        result = SimpleNamespace(chain=[SimpleNamespace(text="旧角色回复")])
        event.get_result = lambda: result
        await main_hook("express_hook")(self.plugin, event)
        self.assertEqual(result.chain, [])

    async def test_generation_failure_preserves_cursor_and_resumes_after_crawl(self):
        original = self.plugin.models.chat
        self.plugin.models.chat = AsyncMock(return_value="")
        result = await self.plugin.proactive.send_one(self.row)
        self.assertFalse(result["ok"])
        self.assertEqual(self.sent, ["爬楼语"])
        self.assertEqual(await self.night.rollup._cursor("s"), 0)
        self.plugin.models.chat = original
        await self.prepare()
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])

    async def test_unknown_send_not_retried_even_after_reload(self):
        async def unknown(*args):
            raise TimeoutError("平台结果未知")
        self.context.send_message = unknown
        with self.assertRaises(TimeoutError):
            await self.prepare()
        self.night.wake_flow = flow_mod.WakeFlow(self.night)
        with self.assertRaisesRegex(RuntimeError, "结果未知"):
            await self.prepare()
        self.assertEqual(await self.night.rollup._cursor("s"), 0)

    async def test_cancelled_send_keeps_unknown_receipt(self):
        async def cancelled(*args):
            raise asyncio.CancelledError()
        self.context.send_message = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.prepare()
        with self.assertRaisesRegex(RuntimeError, "结果未知"):
            await self.prepare()

    async def test_sleep_mid_flow_blocks_remaining_stages(self):
        original = self.context.send_message
        async def falling_asleep(*args):
            result = await original(*args)
            self.night.state.state.mode = "asleep"
            return result
        self.context.send_message = falling_asleep
        self.assertFalse((await self.plugin.proactive.send_one(self.row))["ok"])
        self.assertEqual(self.sent, ["爬楼语"])

    async def test_role_switch_after_crawl_resumes_using_new_persona(self):
        original = self.context.send_message
        async def switch_after_crawl(*args):
            result = await original(*args)
            self.cfg.override("persona.astrbot_persona_id", "另一个")
            return result
        self.context.send_message = switch_after_crawl
        self.assertFalse((await self.plugin.proactive.send_one(self.row))["ok"])
        self.context.send_message = original
        await self.prepare()
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])
        self.assertIn("另一个角色", self.model_calls[-1]["system_prompt"])

    async def test_parallel_cursor_updates_preserve_both_sessions(self):
        await asyncio.gather(self.night.rollup._save_cursor("a", 20),
                             self.night.rollup._save_cursor("b", 30))
        self.assertEqual(await self.night.rollup._cursor("a"), 20)
        self.assertEqual(await self.night.rollup._cursor("b"), 30)

    async def test_idle_mark_from_previous_wake_is_ignored(self):
        await self.store.kv_set("night.wake_idle.wake_at:s", self.wake - 600)
        self.assertFalse((await self.night.wake_idle.claim("s")).trigger)

    async def test_role_change_during_generation_blocks_old_proactive(self):
        await self.prepare()
        original = self.provider.text_chat
        async def switching(**kw):
            response = await original(**kw)
            self.cfg.override("persona.astrbot_persona_id", "另一个")
            return response
        self.provider.text_chat = switching
        result = await self.plugin.proactive.send_one(self.row)
        self.assertFalse(result["ok"])
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])

    async def test_new_message_during_generation_blocks_proactive(self):
        await self.prepare()
        original = self.provider.text_chat
        async def incoming(**kw):
            response = await original(**kw)
            await self.archive("incoming", int(time.time()))
            return response
        self.provider.text_chat = incoming
        self.assertFalse((await self.plugin.proactive.send_one(self.row))["ok"])
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])

    async def test_sleeping_blocks_even_manual_proactive(self):
        self.night.state.state.mode = "asleep"
        self.assertFalse((await self.plugin.proactive.send_one(self.row))["ok"])
        self.assertEqual(self.sent, [])

    async def test_wake_when_already_awake_does_not_reset_energy_or_epoch(self):
        self.night.state.state.mode = "awake"
        self.night.state.state.pending_crawl = []
        self.night.state.state.energy = 100
        await self.night.state.wake_now()
        self.assertEqual(self.night.state.state.energy, 100)
        self.assertEqual(self.night.state.state.last_wake_at, self.wake)

    async def test_multi_chunk_limit_applies_to_whole_wake(self):
        await self.archive("another-question", self.since + 11)
        self.cfg.override("night.rollup.max_replies_per_wake", 1)
        await self.prepare()
        compose_prompt = next(c["prompt"] for c in self.model_calls if "合并成一段" in c["prompt"])
        self.assertEqual(compose_prompt.count("对方："), 1)
        self.assertNotIn("AKI：", compose_prompt)
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])

    async def test_failed_completion_write_does_not_resend_successful_reply(self):
        original = self.night.crawler.mark_read
        self.night.crawler.mark_read = AsyncMock(side_effect=RuntimeError("读标记写入失败"))
        with self.assertRaises(RuntimeError):
            await self.prepare()
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])
        self.night.crawler.mark_read = original
        await self.prepare()
        self.assertEqual(self.sent, ["爬楼语", "补课答复"])

    async def test_incremental_sleep_bounds_and_ids(self):
        rows = await self.night.rollup._read_incremental("s", 0, self.since, self.wake)
        self.assertEqual([r["msg_id"] for r in rows], ["sleep-question"])
        chunk = await self.night.rollup._summarize_chunk(rows)
        self.assertEqual(chunk.session_id, "s")
        self.assertEqual(chunk.items[0].session_id, "s")
        self.assertEqual(chunk.items[0].msg_id, "sleep-question")

    async def test_bot_names_in_model_material_are_normalized(self):
        await self.archive("old-bot", self.since + 20, sender="bot")
        identity = await self.plugin.persona.resolve(self.row["umo"])
        await self.plugin.proactive.compose(self.row, identity=identity)
        self.assertNotIn("绫", self.model_calls[-1]["prompt"])
        self.assertIn("大小姐", self.model_calls[-1]["system_prompt"])
        self.assertGreater(len(self.model_calls[-1]["system_prompt"]), 6000)

    async def test_three_modes_replace_only_framework_persona(self):
        manager = self.context.persona_manager
        old = manager.items["另一个"].system_prompt
        for mode in ("astrbot", "builtin", "none"):
            self.cfg.override("persona.mode", mode)
            request = SimpleNamespace(system_prompt=f"基础说明\n# Persona Instructions\n\n{old}\n\n# Skills\n工具说明",
                                      contexts=[{"role": "assistant", "content": "另一个开场"},
                                                {"role": "user", "content": "用户历史"}])
            event = SimpleNamespace(unified_msg_origin=self.row["umo"], get_sender_id=lambda: "u",
                                    get_group_id=lambda: "s", get_extra=lambda key: None)
            await self.plugin.persona.inject(event, request)
            self.assertIn("工具说明", request.system_prompt)
            self.assertNotIn(old, request.system_prompt)
            self.assertEqual(request.contexts[-1]["content"], "用户历史")
            self.assertEqual("你叫绫" in request.system_prompt, mode == "builtin")
            self.assertEqual("你是大小姐" in request.system_prompt, mode == "astrbot")
            self.assertEqual(await self.plugin.persona.display_name(),
                             {"astrbot": "大小姐", "builtin": "绫", "none": "bot"}[mode])

    async def test_missing_selected_persona_never_falls_back(self):
        self.cfg.override("persona.astrbot_persona_id", "不存在")
        with self.assertRaises(ValueError):
            await self.plugin.persona.resolve()
        self.assertFalse((await self.plugin.proactive.send_one(self.row))["ok"])
        self.assertEqual(self.sent, [])

    async def test_full_or_natural_wake_never_claims_sleepy_line(self):
        idle = self.night.wake_idle
        await self.store.kv_set("night.wake_idle.wake_at:s", int(time.time()) - 700)
        for reason, energy in (("natural", 40), ("energy_full", 100), ("message", 100)):
            self.night.state.state.last_wake_reason = reason
            self.night.state.state.energy = energy
            self.assertFalse((await idle.claim("s")).trigger)

    async def test_new_message_expiry_and_role_switch_clear_pending(self):
        idle = self.night.wake_idle
        for invalid in ("new_message", "expired", "role"):
            idle._pending_line["s"] = "想睡"
            idle._pending_at["s"] = time.time()
            idle._pending_persona["s"] = "A"
            if invalid == "new_message":
                await idle.note_message("s")
            if invalid == "expired":
                idle._pending_at["s"] -= 61
            self.assertEqual(idle.take_line("s", "B" if invalid == "role" else "A"), "")


if __name__ == "__main__":
    unittest.main()
