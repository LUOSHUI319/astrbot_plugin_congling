"""P5 全量清除：服务端四重确认、可校验备份和单事务删除。

只接受经过页面层认证的管理员标识。确认票据仅存服务端、逐步换新、
15 分钟过期；进程重启后失效。VACUUM 是 SQLite 要求的提交后维护。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..core import paths

PHRASE = "删除所有记忆"
TABLES = (
    "mem_atom_tag", "mem_edge", "mem_atom", "mem_episode", "mem_entity",
    "mem_profile", "mem_subject", "affinity", "persona_state", "mem_diary",
    "event_ledger", "night_dream_plan",
)
KEEP = ["人格设定（persona:*）", "插件配置", "msg_archive", "头像库", "qzone:auth"]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _encode(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__sqlite_blob__": base64.b64encode(value).decode("ascii")}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__sqlite_blob__"}:
        return base64.b64decode(value["__sqlite_blob__"], validate=True)
    return value


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _backup_path() -> Path:
    return paths.memory_backup_dir() / (
        f"memories_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(6)}.json")


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """结构驱动、全库计数：包含软删除/归档数据，不以 persona_id 过滤。"""
    catalog = {row[0]: row[1] for row in conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table'")}
    cursors = sorted(n for n in catalog if n == "inject_cursor"
                     or (n.startswith("memory_") and n.endswith("_cursor")))
    result: dict[str, Any] = {"tables": {}, "kv": [], "counts": {}, "cursors": cursors}
    for name in (*TABLES, *cursors):
        if name not in catalog:
            result["counts"][name] = 0
            continue
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(name)})")]
        rows = [[_encode(v) for v in row] for row in conn.execute(f"SELECT * FROM {_quote(name)}")]
        # 无主键的旧表也可稳定比较；不依赖隐式行序。
        rows.sort(key=lambda row: _json(row))
        result["tables"][name] = {"columns": columns, "rows": rows, "schema": catalog[name]}
        result["counts"][name] = len(rows)
    for row in conn.execute("SELECT k,v,updated_at FROM kv ORDER BY k"):
        key = row[0]
        if key == "night.dreams" or (key.startswith("memory_") and key.endswith("_cursor")):
            result["kv"].append(list(row))
    dream_rows = [r for r in result["kv"] if r[0] == "night.dreams"]
    dreams = json.loads(dream_rows[0][1]) if dream_rows else []
    if isinstance(dreams, str):
        dreams = json.loads(dreams)
    if not isinstance(dreams, list):
        raise ValueError("梦库格式异常，请先修复；不能显示不真实的条数")
    result["counts"]["night.dreams"] = len(dreams)
    result["counts"]["memory_kv_cursors"] = len(result["kv"]) - len(dream_rows)
    # 保留消息的水位也参与确认，避免两次确认之间的新消息被静默跳过。
    result["archive_watermarks"] = [list(r) for r in conn.execute(
        "SELECT session_id,MAX(id) FROM msg_archive GROUP BY session_id ORDER BY session_id")]
    return result


def write_backup(path: Path, data: dict[str, Any]) -> str:
    """独占创建 + fsync + 回读校验；失败绝不开始删除。"""
    payload = _json({"format": "congling.memories", "format_version": 1,
                     "created_at": int(time.time()), "snapshot": data})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    expected = hashlib.sha256(payload).hexdigest()
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("备份回读校验失败")
    return expected


def read_backup(path: Path, expected: str) -> dict[str, Any]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("备份文件已变化，请重新开始确认")
    data = json.loads(raw)
    if data.get("format") != "congling.memories" or data.get("format_version") != 1:
        raise ValueError("备份格式不支持")
    return data["snapshot"]


def restore_backup(conn: sqlite3.Connection, path: Path, expected: str) -> None:
    """离线恢复工具（不暴露 Web 接口）；只允许导入空的目标表，不覆盖新记忆。

    调用者提供已初始化且无活动事务的 SQLite 连接；失败整体回滚。
    包含 BLOB、refs、旧 persona_id、KV 和游标的逐值恢复。
    """
    data = read_backup(path, expected)
    if conn.in_transaction:
        raise ValueError("恢复不能嵌套事务")
    allowed = set(TABLES) | set(data["cursors"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        for name, table in data["tables"].items():
            if name not in allowed or (name not in TABLES and name != "inject_cursor"
                                      and not (name.startswith("memory_") and name.endswith("_cursor"))):
                raise ValueError("备份包含非记忆表")
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(name)})")]
            if columns != table["columns"]:
                raise ValueError(f"{name} 结构不一致，请使用匹配的插件初始化空库")
            if conn.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0]:
                raise ValueError(f"{name} 非空，拒绝覆盖")
            cols = ",".join(_quote(c) for c in columns)
            marks = ",".join("?" for _ in columns)
            conn.executemany(f"INSERT INTO {_quote(name)}({cols}) VALUES({marks})",
                             [[_decode(v) for v in r] for r in table["rows"]])
        for key, value, updated in data["kv"]:
            if key != "night.dreams" and not (key.startswith("memory_") and key.endswith("_cursor")):
                raise ValueError("备份包含非记忆 KV")
            if conn.execute("SELECT 1 FROM kv WHERE k=?", (key,)).fetchone():
                raise ValueError("目标 KV 非空，拒绝覆盖")
            conn.execute("INSERT INTO kv(k,v,updated_at) VALUES(?,?,?)", (key, value, updated))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


class MemoryReset:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.store = plugin.store
        self.lock = asyncio.Lock()
        self.tickets: dict[str, dict[str, Any]] = {}
        self.clock = time.monotonic

    def _issue(self, state: dict[str, Any], stage: str) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        state["stage"] = stage
        self.tickets[token] = state
        return {"token": token, "stage": stage}

    def _require(self, owner: str, body: dict[str, Any], stage: str) -> tuple[str, dict[str, Any]]:
        token = body.get("token")
        state = self.tickets.get(token) if isinstance(token, str) else None
        if (not state or state["owner"] != owner or state["stage"] != stage
                or self.clock() >= state["expires"]):
            raise ValueError("确认已失效、顺序不正确或不属于当前管理员，请重新开始")
        return token, state

    async def handle(self, owner: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        if not owner:
            raise PermissionError("仅管理员可以删除所有记忆")
        async with self.lock:
            self.tickets = {t: s for t, s in self.tickets.items() if self.clock() < s["expires"]}
            if action == "preview":
                data = await self.store.transaction(snapshot)
                # 同一管理员新预览使其旧流程失效，避免多个页签误确认。
                self.tickets = {t: s for t, s in self.tickets.items() if s["owner"] != owner}
                state = {"owner": owner, "expires": self.clock() + 900,
                         "digest": _digest(data), "counts": data["counts"]}
                return {**self._issue(state, "preview"), "counts": data["counts"],
                        "preserved": KEEP, "warning": "删除后不可恢复。备份仅供管理员离线恢复，无撤销按钮。"}
            if action == "confirm":
                token, state = self._require(owner, body, "preview")
                if body.get("confirmed") is not True:
                    raise ValueError("必须确认已查看真实条数与不可恢复说明")
                del self.tickets[token]
                return self._issue(state, "phrase")
            if action == "phrase":
                token, state = self._require(owner, body, "phrase")
                if body.get("phrase") != PHRASE:
                    raise ValueError("必须逐字输入：删除所有记忆")
                del self.tickets[token]
                return self._issue(state, "backup")
            if action == "backup":
                token, state = self._require(owner, body, "backup")
                path = _backup_path()
                def export(conn):
                    data = snapshot(conn)
                    if _digest(data) != state["digest"]:
                        raise ValueError("数据已变化，请重新查看条数并确认")
                    return write_backup(path, data)
                checksum = await self.store.transaction(export)
                state.update(backup_path=str(path), backup_sha256=checksum)
                del self.tickets[token]
                return {**self._issue(state, "acknowledge"), "backup_path": str(path),
                        "backup_sha256": checksum, "backup_ready": True}
            if action == "acknowledge":
                token, state = self._require(owner, body, "acknowledge")
                if body.get("acknowledged") is not True:
                    raise ValueError("必须勾选：我已知晓不可恢复")
                read_backup(Path(state["backup_path"]), state["backup_sha256"])
                state["not_before"] = self.clock() + 10
                del self.tickets[token]
                return {**self._issue(state, "execute"), "wait_seconds": 10}
            if action != "execute":
                raise ValueError("不支持的清除操作")
            token, state = self._require(owner, body, "execute")
            remaining = state["not_before"] - self.clock()
            if remaining > 0:
                raise ValueError(f"服务端倒计时尚未结束，至少再等 {remaining:.1f} 秒")
            if body.get("final_confirmed") is not True:
                raise ValueError("必须点击最终删除按钮")
            # 消费票据后执行：并发、断线重试、重复点击都不能执行第二次。
            del self.tickets[token]
            task = asyncio.create_task(self._execute(state))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # HTTP 断线不能使已提交删除漏掉缓存失效/VACUUM。
                await task
                raise

    async def _execute(self, state: dict[str, Any]) -> dict[str, Any]:
        async with self.store.memory_reset_guard():
            return await self._execute_guarded(state)

    async def _execute_guarded(self, state: dict[str, Any]) -> dict[str, Any]:
        night = getattr(self.plugin, "night", None)
        dreamer = getattr(night, "dreamer", None)
        # 与梦库的缓存保存/发放用同一把锁，提交后立即清空缓存。
        lock = dreamer.write_lock if dreamer else asyncio.Lock()
        async with lock:
            result = await self.store.transaction(lambda conn: self._delete(conn, state))
            memory = getattr(self.plugin, "memory", None)
            if memory:
                memory._buffers.clear()
                memory._last_activity.clear()
                memory._recall = {"last_hits": 0, "at": int(time.time()), "error": None}
                memory._subject_diag = {"unresolved": 0, "at": int(time.time()), "error": None}
            if dreamer:
                dreamer._dreams.clear()
                dreamer._last_dream_at = 0
                dreamer._loaded = True
                night.morning_dream = None
                night._dream_event_id = ""
            persona = getattr(self.plugin, "persona", None)
            if persona:
                persona.get().state.clear()
        vacuum = await self.store.vacuum()
        return {**result, **vacuum}

    def _delete(self, conn: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
        before = snapshot(conn)
        if _digest(before) != state["digest"]:
            raise ValueError("数据已变化，未删除任何数据；请重新查看条数并确认")
        saved = read_backup(Path(state["backup_path"]), state["backup_sha256"])
        if saved != before:
            raise ValueError("备份不覆盖当前数据，拒绝删除")
        # 第三步备份供人确认；最终备份在本次删除事务内再次导出。
        # 不覆写已确认文件；导出/fsync/回读任何一步失败都回滚且不删数据。
        final_path = _backup_path()
        final_checksum = write_backup(final_path, before)
        # 结构检查而非 user_version；旧库没有审计表时在同一事务内创建。
        conn.execute("CREATE TABLE IF NOT EXISTS repair_journal("
                     "run_id TEXT PRIMARY KEY, kind TEXT, at INTEGER, detail TEXT)")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(repair_journal)")}
        for name, kind in (("run_id", "TEXT"), ("kind", "TEXT"), ("at", "INTEGER"), ("detail", "TEXT")):
            if name not in columns:
                conn.execute(f"ALTER TABLE repair_journal ADD COLUMN {name} {kind}")
        for name in TABLES:
            if name in before["tables"]:
                conn.execute(f"DELETE FROM {_quote(name)}")
        for key, _, _ in before["kv"]:
            conn.execute("DELETE FROM kv WHERE k=?", (key,))
        for name in before["cursors"]:
            conn.execute(f"DELETE FROM {_quote(name)}")
        # 清空召回 ids；消费水位重置至保留消息末尾，防止旧原文立刻重建已删除记忆。
        for session, water in before["archive_watermarks"]:
            conn.execute("INSERT INTO inject_cursor(persona_id,session_id,last_msg_row,last_atom_ids,updated_at) "
                         "VALUES(?,?,?,?,?)", (self.plugin.cfg.persona_id, session, water, "[]", int(time.time())))
        # 旧合并撤销持有完整前像；保留审计记录，但禁止用它复活已清除的主体。
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mem_repair_log'").fetchone():
            conn.execute("UPDATE mem_repair_log SET reversed=1")
        result = {"counts": before["counts"], "backup_path": str(final_path),
                  "backup_sha256": final_checksum, "confirmed_backup_path": state["backup_path"],
                  "operator": state["owner"],
                  "run_id": secrets.token_hex(16), "cursor_policy": "archive_high_watermark"}
        conn.execute("INSERT INTO repair_journal(run_id,kind,at,detail) VALUES(?,?,?,?)",
                     (result["run_id"], "delete_all_memories", int(time.time()), _json(result).decode()))
        return result
