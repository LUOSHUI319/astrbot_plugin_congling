"""P5 接口攻击路径、真实 SQLite 回滚、备份往返、并发闸门验收。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot_plugin_congling.tests import run_diagnosis_regressions as base
if "astrbot.api.web" not in sys.modules:
    base.module("astrbot.api.web").request = types.SimpleNamespace()
from astrbot_plugin_congling.core import paths
from astrbot_plugin_congling.core.config import Config
from astrbot_plugin_congling.core.store import SCHEMA_VERSION
from astrbot_plugin_congling.humanize.persona import PersonaService
from astrbot_plugin_congling.memory.reset import (
    MemoryReset, TABLES, PHRASE, snapshot, read_backup, restore_backup)
from astrbot_plugin_congling.night import NightModule
from astrbot_plugin_congling.page_api import PageApi

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "p5-test-admin"


class Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await base.Regression.asyncSetUp(self)
        self.now = 100.0
        self.addCleanup(patch.stopall)
        self.backups = Path(self.tmp.name) / "plugin_data" / "congling" / "backups"
        patch("astrbot_plugin_congling.core.paths.memory_backup_dir", return_value=self.backups).start()
        self.p.context.get_config = lambda: {"dashboard": {"username": ADMIN}}
        self.p.raw_config = {}
        self.p.night = NightModule(self.p)
        self.p.persona = PersonaService(self.p)
        self.reset = MemoryReset(self.p)
        self.reset.clock = lambda: self.now
        self.api = PageApi(self.p)
        self.api._reset_service = self.reset
        self.req = types.SimpleNamespace(username=ADMIN)
        patch("astrbot_plugin_congling.page_api.memory_reset.request", self.req).start()
        self.body = {}
        patch("astrbot_plugin_congling.page_api._body",
              AsyncMock(side_effect=lambda: self.body)).start()
        await self.seed()

    async def asyncTearDown(self):
        await base.Regression.asyncTearDown(self)

    async def seed(self):
        sqls = [
            "INSERT INTO mem_atom(text,embedding,subject_ref,session_ref,actor_ref,status) "
            "VALUES('旧记忆',X'0001FF','user:qq:123','private:qq:123','user:qq:123','deleted')",
            "INSERT INTO mem_atom(persona_id,text,status) VALUES('legacy','另一历史标识','archived')",
            "INSERT INTO mem_episode(summary) VALUES('一段往事')",
            "INSERT INTO mem_entity(name) VALUES('小明')",
            "INSERT INTO mem_edge(src,dst,rel) VALUES(1,1,'认识')",
            "INSERT INTO mem_subject(user_id,display_name,source) VALUES('123','小明','qq')",
            "INSERT INTO mem_profile(subject_key,summary) VALUES('123','画像')",
            "INSERT INTO mem_atom_tag(atom_id,subject_key) VALUES(1,'123')",
            "INSERT INTO affinity(subject_key,score) VALUES('123',100)",
            "INSERT INTO persona_state(subject_id) VALUES('123')",
            "INSERT INTO mem_diary(day_key,text) VALUES('2026-11-01','日记原文')",
            "INSERT INTO event_ledger(event_id,persona_id,event_key,event_type,subject_ref,actor_ref,"
            "audience,occurred_at,recorded_at,status) "
            "VALUES('e','ling','ek','diary','bot:ling','bot:ling','owner:__private__',1,1,'succeeded')",
            "INSERT INTO night_dream_plan(plan_key,dream_json) VALUES('plan','{\"text\":\"排产梦\"}')",
            "INSERT INTO msg_archive(session_id,ts,text) VALUES('123',1,'保留原始消息')",
            "INSERT INTO inject_cursor(persona_id,session_id,last_atom_ids) VALUES('ling','123','[1]')",
            "INSERT INTO qq_avatar_pool(path,sha1) VALUES('avatar.png','abc')",
            "CREATE TABLE memory_extract_cursor(session TEXT,water INTEGER)",
            "INSERT INTO memory_extract_cursor VALUES('123',10)",
            "CREATE TABLE mem_repair_log(run_id TEXT PRIMARY KEY,kind TEXT,at INTEGER,reversed INTEGER,detail TEXT)",
            "INSERT INTO mem_repair_log VALUES('old','merge',1,0,'{\"pairs\":[]}')",
        ]
        for sql in sqls:
            await self.store.execute(sql)
        for key, value in {"night.dreams": [{"text": "梦的正文"}],
                           "persona:ling": {"name": "原来设定"},
                           "persona:ling:versions": [{"identity": "历史版本"}],
                           "qzone:auth": {"token": "test-only"},
                           "memory_extract_cursor": 20, "unrelated": "保留"}.items():
            await self.store.kv_set(key, value)
        await self.p.night.dreamer.load()
        self.before = await self.store.transaction(snapshot)
        self.config_before = self.cfg.snapshot()

    async def call(self, action, token=None, **payload):
        self.body = {"action": action, "token": token, **payload}
        return await self.api.memory_reset()

    async def step(self, action, token=None, **payload):
        result = await self.call(action, token, **payload)
        self.assertTrue(result["ok"], result)
        return result["data"]

    async def ready(self):
        p = await self.step("preview")
        p = await self.step("confirm", p["token"], confirmed=True)
        p = await self.step("phrase", p["token"], phrase=PHRASE)
        self.backup = await self.step("backup", p["token"])
        p = await self.step("acknowledge", self.backup["token"], acknowledged=True)
        return p["token"]

    async def erase(self):
        token = await self.ready()
        self.now += 10
        return await self.step("execute", token, final_confirmed=True)

    async def unchanged(self):
        self.assertEqual(await self.store.transaction(snapshot), self.before)


class Confirmation(Fixture):
    async def test_real_counts_all_statuses_and_identifiers(self):
        data = await self.step("preview")
        self.assertEqual(data["counts"]["mem_atom"], 2)
        for table in TABLES:
            self.assertGreaterEqual(data["counts"][table], 1)
        self.assertEqual(data["counts"]["night.dreams"], 1)
        self.assertIn("不可恢复", data["warning"])
        await self.unchanged()

    async def test_unauthenticated_and_forged_admin_fields_fail_every_action(self):
        for user in (None, "", "ordinary-user", "apikey:123"):
            self.req.username = user
            for action in ("preview", "confirm", "phrase", "backup", "acknowledge", "execute"):
                result = await self.call(action, is_admin=True, username=ADMIN, role="admin",
                                         phrase=PHRASE, acknowledged=True, final_confirmed=True)
                self.assertFalse(result["ok"], (user, action))
                self.assertEqual(result["code"], "forbidden")
        await self.unchanged()

    async def test_missing_admin_config_fails_closed(self):
        self.p.context.get_config = lambda: {}
        self.assertEqual((await self.call("preview"))["code"], "forbidden")

    async def test_direct_final_with_all_client_flags_cannot_skip(self):
        for token in (None, "invented", [], {"token": "x"}):
            result = await self.call("execute", token, confirmed=True, phrase=PHRASE,
                                     acknowledged=True, final_confirmed=True, backup_ready=True,
                                     elapsed=999, not_before=0, backup_path="fake.json")
            self.assertFalse(result["ok"])
        await self.unchanged()

    async def test_each_stage_rejects_final_even_when_flags_supplied(self):
        p = await self.step("preview")
        for action, payload in (("confirm", {"confirmed": True}),
                                ("phrase", {"phrase": PHRASE}),
                                ("backup", {}), ("acknowledge", {"acknowledged": True})):
            self.assertFalse((await self.call("execute", p["token"], final_confirmed=True,
                                             confirmed=True, phrase=PHRASE, acknowledged=True,
                                             backup_ready=True, elapsed=999))["ok"])
            p = await self.step(action, p["token"], **payload)
        self.assertFalse((await self.call("execute", p["token"], final_confirmed=True, elapsed=999))["ok"])
        await self.unchanged()

    async def test_first_confirmation_requires_strict_boolean(self):
        p = await self.step("preview")
        for wrong in (None, False, "true", 1):
            self.assertFalse((await self.call("confirm", p["token"], confirmed=wrong))["ok"])

    async def test_exact_phrase_no_whitespace_or_partial(self):
        p = await self.step("preview")
        p = await self.step("confirm", p["token"], confirmed=True)
        for wrong in ("", "删除", " 删除所有记忆", "删除所有记忆 ", True):
            self.assertFalse((await self.call("phrase", p["token"], phrase=wrong))["ok"])
        await self.unchanged()

    async def test_checkbox_cannot_skip_backup_or_use_truthy_string(self):
        p = await self.step("preview")
        p = await self.step("confirm", p["token"], confirmed=True)
        p = await self.step("phrase", p["token"], phrase=PHRASE)
        self.assertFalse((await self.call("acknowledge", p["token"], acknowledged=True,
                                         backup_ready=True, backup_path="anywhere"))["ok"])
        p = await self.step("backup", p["token"])
        for wrong in (None, False, "true", 1):
            self.assertFalse((await self.call("acknowledge", p["token"], acknowledged=wrong))["ok"])
        await self.unchanged()

    async def test_server_exact_ten_seconds_ignores_client_clock(self):
        token = await self.ready()
        self.now += 9.999
        result = await self.call("execute", token, final_confirmed=True, elapsed=10000,
                                 not_before=0, wait_seconds=0)
        self.assertFalse(result["ok"])
        await self.unchanged()
        self.now += .001
        self.assertFalse((await self.call("execute", token, final_confirmed="true"))["ok"])
        self.assertTrue((await self.call("execute", token, final_confirmed=True))["ok"])

    async def test_ticket_owner_binding(self):
        token = await self.ready()
        self.req.username = "other-admin"
        self.p.context.get_config = lambda: {"dashboard": {"username": "other-admin"}}
        self.now += 10
        self.assertFalse((await self.call("execute", token, final_confirmed=True))["ok"])
        await self.unchanged()

    async def test_expiration_and_restart_invalidate_tickets(self):
        token = await self.ready()
        self.now += 900
        self.assertFalse((await self.call("execute", token, final_confirmed=True))["ok"])
        token = await self.ready()
        self.api._reset_service = MemoryReset(self.p)
        self.assertFalse((await self.call("execute", token, final_confirmed=True))["ok"])
        await self.unchanged()

    async def test_new_preview_and_stage_replay_invalidate_old_tokens(self):
        p = await self.step("preview")
        await self.step("preview")
        self.assertFalse((await self.call("confirm", p["token"], confirmed=True))["ok"])
        p = await self.step("preview")
        await self.step("confirm", p["token"], confirmed=True)
        self.assertFalse((await self.call("confirm", p["token"], confirmed=True))["ok"])

    async def test_concurrent_final_only_commits_once(self):
        token = await self.ready()
        self.now += 10
        # 服务直接并发；不共享 fixture 的请求体。
        body = {"token": token, "final_confirmed": True}
        results = await asyncio.gather(self.reset.handle(ADMIN, "execute", body),
                                       self.reset.handle(ADMIN, "execute", body), return_exceptions=True)
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM repair_journal"), 1)

    async def test_route_registered_post_only_and_calls_auth(self):
        routes = []
        self.p.context.register_web_api = lambda *args: routes.append(args)
        self.api.register_routes()
        route = [r for r in routes if r[0].endswith("/memory/reset")]
        self.assertEqual(len(route), 1)
        self.assertEqual(route[0][2], ["POST"])
        self.req.username = None
        self.assertEqual((await route[0][1]())["code"], "forbidden")


class BackupAndTransaction(Fixture):
    async def test_success_exact_scope_caches_journal_and_restart(self):
        self.mem._buffers["123"].append({"text": "旧缓存"})
        self.mem._last_activity["123"] = 1
        result = await self.erase()
        for name in TABLES:
            self.assertEqual(await self.store.scalar(f"SELECT COUNT(*) FROM {name}"), 0, name)
        self.assertIsNone(await self.store.kv_get("night.dreams"))
        self.assertIsNone(await self.store.kv_get("memory_extract_cursor"))
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM memory_extract_cursor"), 0)
        for key in ("persona:ling", "persona:ling:versions", "qzone:auth", "unrelated"):
            self.assertIsNotNone(await self.store.kv_get(key))
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM msg_archive"), 1)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM qq_avatar_pool"), 1)
        self.assertEqual(self.cfg.snapshot(), self.config_before)
        self.assertEqual(self.mem._buffers, {})
        self.assertEqual(self.p.night.dreamer._dreams, [])
        self.assertIsNone(self.p.night.morning_dream)
        self.assertEqual(await self.store.scalar("SELECT reversed FROM mem_repair_log"), 1)
        audit = json.loads((await self.store.fetch_one("SELECT detail FROM repair_journal"))["detail"])
        self.assertEqual(audit["counts"], self.before["counts"])
        self.assertEqual(audit["backup_path"], result["backup_path"])
        self.assertTrue(result["vacuum_ok"])
        await self.p.night.dreamer.save()
        self.assertEqual(await self.store.kv_get("night.dreams"), [])
        await self.store.close()
        await self.store.open()
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom"), 0)

    async def test_reset_cursor_does_not_reextract_preserved_messages(self):
        await self.erase()
        cursor = await self.store.fetch_one("SELECT * FROM inject_cursor")
        self.assertEqual(cursor["last_atom_ids"], "[]")
        self.assertEqual(cursor["last_msg_row"], 1)
        self.assertEqual(await self.mem._pending_rows("123"), [])
        await self.store.execute("INSERT INTO msg_archive(session_id,ts,text) VALUES('123',2,'新消息')")
        self.assertEqual(len(await self.mem._pending_rows("123")), 1)

    async def test_backup_restores_blob_refs_and_all_tables(self):
        result = await self.erase()
        path = Path(result["backup_path"])
        self.assertEqual(path.parent, self.backups)
        self.assertRegex(path.name, r"^memories_\d{8}_\d{6}_[a-f0-9]+\.json$")
        self.assertEqual(read_backup(path, result["backup_sha256"]), self.before)
        target = base.Store(Path(self.tmp.name) / "restore.sqlite")
        await target.open()
        try:
            await target.execute("CREATE TABLE memory_extract_cursor(session TEXT,water INTEGER)")
            conn = sqlite3.connect(target.path)
            try:
                restore_backup(conn, path, result["backup_sha256"])
            finally:
                conn.close()
            restored = await target.transaction(snapshot)
            self.assertEqual(restored["tables"], self.before["tables"])
            self.assertEqual(restored["kv"], self.before["kv"])
            self.assertEqual((await target.fetch_one("SELECT embedding FROM mem_atom WHERE id=1"))["embedding"],
                             b"\x00\x01\xff")
        finally:
            await target.close()

    async def test_restore_refuses_nonempty_and_rolls_back(self):
        await self.ready()
        conn = sqlite3.connect(self.store.path)
        try:
            with self.assertRaises(ValueError):
                restore_backup(conn, Path(self.backup["backup_path"]), self.backup["backup_sha256"])
        finally:
            conn.close()
        await self.unchanged()

    async def test_backup_write_and_fsync_failure_prevent_delete(self):
        for target in ("write_backup", "os.fsync"):
            p = await self.step("preview")
            p = await self.step("confirm", p["token"], confirmed=True)
            p = await self.step("phrase", p["token"], phrase=PHRASE)
            with patch(f"astrbot_plugin_congling.memory.reset.{target}", side_effect=OSError("disk full")):
                self.assertFalse((await self.call("backup", p["token"]))["ok"])
            self.assertFalse((await self.call("execute", p["token"], final_confirmed=True))["ok"])
            await self.unchanged()

    async def test_missing_backup_rejected_at_third_confirmation(self):
        p = await self.step("preview")
        p = await self.step("confirm", p["token"], confirmed=True)
        p = await self.step("phrase", p["token"], phrase=PHRASE)
        p = await self.step("backup", p["token"])
        Path(p["backup_path"]).unlink()
        self.assertFalse((await self.call("acknowledge", p["token"], acknowledged=True))["ok"])
        await self.unchanged()

    async def test_backup_tampering_after_countdown_rejected(self):
        token = await self.ready()
        Path(self.backup["backup_path"]).write_text("{}", encoding="utf-8")
        self.now += 10
        self.assertFalse((await self.call("execute", token, final_confirmed=True))["ok"])
        await self.unchanged()

    async def test_same_count_content_change_rejected(self):
        token = await self.ready()
        await self.store.execute("UPDATE mem_atom SET text='更新内容' WHERE id=1")
        expected = await self.store.transaction(snapshot)
        self.now += 10
        self.assertFalse((await self.call("execute", token, final_confirmed=True))["ok"])
        self.assertEqual(await self.store.transaction(snapshot), expected)

    async def test_changes_before_backup_require_fresh_counts(self):
        p = await self.step("preview")
        p = await self.step("confirm", p["token"], confirmed=True)
        p = await self.step("phrase", p["token"], phrase=PHRASE)
        await self.store.execute("INSERT INTO mem_atom(text) VALUES('后来一条')")
        self.assertFalse((await self.call("backup", p["token"]))["ok"])
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom"), 3)

    async def test_mid_delete_failure_rolls_back_every_table_and_audit(self):
        token = await self.ready()
        await self.store.execute("CREATE TRIGGER reject_diary_delete BEFORE DELETE ON mem_diary "
                                 "BEGIN SELECT RAISE(ABORT,'fault after earlier tables deleted'); END")
        self.now += 10
        self.assertFalse((await self.call("execute", token, final_confirmed=True))["ok"])
        await self.unchanged()
        self.assertIsNone(await self.store.fetch_one(
            "SELECT name FROM sqlite_master WHERE name='repair_journal'"))
        self.assertTrue(Path(self.backup["backup_path"]).exists())
        self.assertTrue(self.store._memory_open.is_set())

    async def test_journal_failure_rolls_back_delete_and_cursor(self):
        await self.store.execute("CREATE TABLE repair_journal(run_id TEXT,kind TEXT,at INTEGER,detail TEXT)")
        await self.store.execute("CREATE TRIGGER reject_journal BEFORE INSERT ON repair_journal "
                                 "BEGIN SELECT RAISE(ABORT,'journal fault'); END")
        token = await self.ready()
        self.now += 10
        self.assertFalse((await self.call("execute", token, final_confirmed=True))["ok"])
        await self.unchanged()
        self.assertEqual(await self.store.scalar("SELECT reversed FROM mem_repair_log"), 0)

    async def test_vacuum_shrinks_real_database(self):
        await self.store.executemany("INSERT INTO mem_atom(text) VALUES(?)", [("大记忆" * 1000,) for _ in range(300)])
        await self.store.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size_before = self.store.path.stat().st_size
        result = await self.erase()
        size_after = self.store.path.stat().st_size
        self.assertTrue(result["vacuum_ok"])
        self.assertLess(size_after, size_before / 2)
        print(f"P5 VACUUM bytes: {size_before} -> {size_after}")

    async def test_vacuum_failure_reports_committed_not_false_rollback(self):
        def deny(action, arg1, arg2, db, trigger):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK
        token = await self.ready()
        self.store._conn.set_authorizer(deny)
        self.now += 10
        try:
            result = await self.step("execute", token, final_confirmed=True)
        finally:
            self.store._conn.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
        self.assertFalse(result["vacuum_ok"])
        self.assertIn("已提交", result["vacuum_error"])
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom"), 0)
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM repair_journal"), 1)

    async def test_active_producer_blocks_delete_without_cancellation(self):
        token = await self.ready()
        self.now += 10
        async with self.store.memory_work():
            self.assertFalse((await self.call("execute", token, final_confirmed=True))["ok"])
        await self.unchanged()

    async def test_inflight_extractor_is_tracked_by_actual_method(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def extract(_):
            entered.set()
            await release.wait()
            return []
        self.mem.extractor.extract = extract
        token = await self.ready()
        task = asyncio.create_task(self.mem._extract_session("123", force=True))
        try:
            await entered.wait()
            self.assertTrue(self.store._memory_workers)
            self.now += 10
            self.assertFalse((await self.call("execute", token, final_confirmed=True))["ok"])
            self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom"), 2)
        finally:
            release.set()
            await task

    async def test_new_producer_waits_until_clear_guard_reopens(self):
        entered = asyncio.Event()
        async def writer():
            async with self.store.memory_work():
                entered.set()
        async with self.store.memory_reset_guard():
            task = asyncio.create_task(writer())
            await asyncio.sleep(0)
            self.assertFalse(entered.is_set())
        await task
        self.assertTrue(entered.is_set())

    async def test_disconnect_during_commit_still_finishes_cache_and_vacuum(self):
        token = await self.ready()
        self.now += 10
        entered, release = threading.Event(), threading.Event()
        original = self.reset._delete
        def pause(conn, state):
            result = original(conn, state)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            return result
        with patch.object(self.reset, "_delete", side_effect=pause):
            task = asyncio.create_task(self.reset.handle(ADMIN, "execute",
                                                        {"token": token, "final_confirmed": True}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                task.cancel()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(await self.store.scalar("SELECT COUNT(*) FROM mem_atom"), 0)
        self.assertEqual(self.p.night.dreamer._dreams, [])
        self.assertTrue(self.store._memory_open.is_set())

    async def test_user_versions_5_6_7_and_partial_journal_use_structure(self):
        # 各版本分别重新播种，P5 本身不写 user_version。
        for version in (5, 6, 7):
            await self.store.execute(f"PRAGMA user_version={version}")
            await self.store.execute("DROP TABLE IF EXISTS repair_journal")
            await self.store.execute("CREATE TABLE repair_journal(note TEXT)")
            await self.erase()
            self.assertEqual(await self.store.scalar("PRAGMA user_version"), version)
            columns = {r["name"] for r in await self.store.fetch_all("PRAGMA table_info(repair_journal)")}
            self.assertTrue({"run_id", "kind", "at", "detail"} <= columns)
        self.assertEqual(SCHEMA_VERSION, 6)


class Persona(Fixture):
    async def test_defaults_legacy_modes_no_builtin_identity(self):
        self.assertEqual(Config().str("persona.mode"), "astrbot")
        for mode in ("builtin", "none", "invalid", "astrbot"):
            self.cfg.override("persona.mode", mode)
            self.assertEqual(self.p.persona.mode, "astrbot")
            req = types.SimpleNamespace(system_prompt="FRAMEWORK", extra_user_content_parts=[])
            event = types.SimpleNamespace(get_sender_id=lambda: "123", get_group_id=lambda: "")
            await self.p.persona.inject(event, req)
            self.assertNotIn("你叫绫", req.system_prompt)
            self.assertNotIn("当前状态", req.system_prompt)

    async def test_current_framework_persona_ignores_old_selection(self):
        manager = types.SimpleNamespace(
            get_default_persona_v3=AsyncMock(return_value={"name": "当前小雪", "prompt": "框架人设"}),
            get_all_personas=AsyncMock(side_effect=AssertionError("must not enumerate")))
        self.p.context.persona_manager = manager
        self.cfg.override("persona.astrbot_persona_id", "旧选择")
        self.assertEqual(await self.p.persona.display_name(), "当前小雪")
        data = (await self.api.get_persona())["data"]
        self.assertEqual(data["name"], "当前小雪")
        self.assertEqual(data["identity"], "框架人设")
        self.assertEqual(data["persona_id"], "ling")
        manager.get_all_personas.assert_not_called()

    async def test_persona_id_payload_and_settings_bypasses_rejected(self):
        for changes in ({"persona.persona_id": "other"}, {"persona.mode": "builtin"},
                        {"persona.astrbot_persona_id": "other"},
                        {"persona": {"persona_id": "other"}}):
            self.body = {"changes": changes}
            self.assertFalse((await self.api.save_settings())["ok"], changes)
        for body in ({"persona_id": "other"}, {"mode": "builtin"}, {"astrbot_persona_id": "other"}):
            self.body = body.copy()
            self.assertFalse((await self.api.save_persona())["ok"])
        self.assertEqual(self.cfg.persona_id, "ling")
        await self.unchanged()

    async def test_foreign_id_inside_old_kv_does_not_switch_identity(self):
        await self.store.kv_set("persona:ling", {"persona_id": "foreign", "name": "old"})
        await self.p.persona.load()
        self.assertEqual(self.p.persona.get().persona_id, "ling")
        self.assertEqual(self.p.persona.key, "persona:ling")

    async def test_backup_directory_contract_and_static_guards(self):
        with patch.object(paths, "data_dir", return_value=Path("plugin_data/astrbot_plugin_congling")):
            # Fixture 替换了 memory_backup_dir；调用其原始函数见下方保存的引用。
            self.assertEqual(MEMORY_BACKUP_DIR(), Path("plugin_data/congling/backups"))
        self.assertEqual((ROOT / "pages/fromling/night.js").read_bytes(),
                         (ROOT / "pages/fromling/night.1029c.js").read_bytes())
        # 版本号从 1.0.1 提到 1.1.0，这里的哈希随之更新；改动元数据时同步改它
        self.assertEqual(hashlib.sha256((ROOT / "metadata.yaml").read_bytes()).hexdigest(),
                         "79dbdd355b377cb6359d36d19594a6f3f17471c283f67b7df7ad477fb7b280a3")


MEMORY_BACKUP_DIR = paths.memory_backup_dir
