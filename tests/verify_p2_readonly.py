"""Read-only production diagnostics. Never instantiate Store on the real DB."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

QUERIES = {
    "version": "PRAGMA user_version",
    "platforms": "SELECT platform, session_kind, COUNT(*) AS n FROM msg_archive GROUP BY platform, session_kind",
    "personas": "SELECT persona_id, COUNT(*) AS n FROM mem_atom GROUP BY persona_id",
    "group_subject": "SELECT COUNT(*) AS n FROM mem_atom WHERE scope='group' AND subject_id='577084969'",
    "group_session": "SELECT COUNT(*) AS n FROM mem_atom WHERE scope='group' AND session_id='577084969'",
    "group_eligible": "SELECT COUNT(*) AS n FROM mem_atom WHERE scope='group' AND session_id='577084969' AND status='active' AND decay>=0.15 AND COALESCE(unread,0)=0",
    "group_platforms": "SELECT platform, COUNT(*) AS n FROM msg_archive WHERE session_id='577084969' GROUP BY platform",
    "group_people": "SELECT subject_id, COUNT(*) AS n, MAX(mem_level) AS max_level FROM mem_atom WHERE session_id='577084969' AND scope='group' AND status='active' AND COALESCE(unread,0)=0 GROUP BY subject_id ORDER BY n DESC LIMIT 8",
    "private_count": "SELECT COUNT(*) AS n FROM mem_atom WHERE session_kind='private'",
    "cursor_schema": "PRAGMA table_info(inject_cursor)",
    "confirmed_board": "SELECT a.subject_key, a.score FROM affinity a JOIN mem_subject s ON s.persona_id=a.persona_id AND s.user_id=a.subject_key WHERE s.source='qq' AND s.user_id NOT IN ('bot','__self__','') AND s.user_id NOT LIKE 'bot:%' AND NOT EXISTS(SELECT 1 FROM msg_archive g WHERE g.session_kind='group' AND g.session_id=s.user_id) AND NOT EXISTS(SELECT 1 FROM mem_atom g WHERE g.persona_id=s.persona_id AND g.session_kind='group' AND g.session_id=s.user_id) ORDER BY a.score DESC LIMIT 10",
}


def inspect(path: Path) -> dict:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        result = {name: [dict(r) for r in conn.execute(sql)] for name, sql in QUERIES.items()}
        columns = {r[1] for r in conn.execute("PRAGMA table_info(mem_atom)")}
        result["refs_deployed"] = {c: c in columns for c in ("subject_ref", "session_ref", "actor_ref", "ref_status")}
        return result
    finally:
        conn.rollback()
        conn.close()


async def simulate(path: Path) -> dict:
    """Exercise migration/injection on RAM only; source connection stays read-only."""
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import types
    from astrbot_plugin_congling.tests import run_diagnosis_regressions as base
    from astrbot_plugin_congling.core.contracts import PerceivedMessage
    from astrbot_plugin_congling.memory.refs import session_ref, actor_ref

    source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    source.execute("PRAGMA query_only=ON")
    snapshot = sqlite3.connect(":memory:", check_same_thread=False)
    snapshot.row_factory = sqlite3.Row
    try:
        source.backup(snapshot)
    finally:
        source.close()
    store = base.Store(Path("unused-ram-only.db"), persona_id="ling")
    store._conn = snapshot
    snapshot.execute("BEGIN IMMEDIATE")
    try:
        store._migrate()
        snapshot.commit()
    except BaseException:
        snapshot.rollback()
        snapshot.close()
        raise
    store.ready = True
    cfg = base.Config({"memory": {"cross_session_recall": True}, "night": {"crawl_enabled": False}})
    plugin = types.SimpleNamespace(cfg=cfg, store=store, night=None, bus=base.EventBus(),
                                   probe=types.SimpleNamespace(should_yield=lambda _: False),
                                   is_admin=lambda _: False, context=types.SimpleNamespace(), diag=None,
                                   budget=base.Budget(cfg))
    mem = plugin.memory = base.MemoryService(plugin)
    result = {}
    try:
        result["migration"] = await store.fetch_all("SELECT * FROM migration_journal WHERE version=6")
        result["ref_status"] = await store.fetch_all("SELECT ref_status,COUNT(*) AS n FROM mem_atom GROUP BY ref_status")
        result["ref_counts"] = await store.fetch_one("SELECT SUM(subject_ref<>'') AS subjects, SUM(session_ref<>'') AS sessions, SUM(actor_ref<>'') AS actors FROM mem_atom")
        people = await store.fetch_all(
            "SELECT subject_id,COUNT(*) AS n FROM mem_atom WHERE session_id='577084969' "
            "AND scope='group' AND subject_id<>'' AND subject_id NOT GLOB '*[^0-9]*' "
            "AND status='active' AND COALESCE(unread,0)=0 GROUP BY subject_id ORDER BY n DESC LIMIT 2")
        prior_ids = set()
        for person in people:
            uid = person["subject_id"]
            msg = PerceivedMessage(text="你还记得我吗", session_id="577084969", session_kind="group",
                                   sender_id=uid, meta={"platform": "aiocqhttp"})
            event = types.SimpleNamespace(_congling_msg=msg, get_group_id=lambda: "577084969",
                                          get_sender_id=lambda: uid, get_platform_name=lambda: "aiocqhttp")
            req = types.SimpleNamespace(system_prompt="stable", extra_user_content_parts=[])
            await mem.inject(event, req)
            cursor = await mem.retriever.cursor("577084969")
            ids = json.loads(cursor.get("last_atom_ids") or "[]")
            assert ids, "meta recall must be nonempty for a known real user"
            rows = await store.fetch_all("SELECT id,subject_id,subject_ref,session_kind,session_id FROM mem_atom WHERE id IN (" +
                                          ",".join("?" for _ in ids) + ")", ids)
            assert all(r["subject_id"] == uid for r in rows), "meta subject boundary"
            assert not (set(ids) & prior_ids), "two actors must not share meta memories"
            assert req.system_prompt == "stable"
            assert req.extra_user_content_parts and all(p.no_save for p in req.extra_user_content_parts)
            result[uid] = {"cursor_ids": ids, "subjects": sorted({r["subject_id"] for r in rows}),
                           "prefix_unchanged": True, "temp_only": True}
            prior_ids.update(ids)
        if people:
            uid = people[0]["subject_id"]
            hits = await mem.retriever.search("qzxvplmkhjfgdwrtbn", session_ref=session_ref("qq", "group", "577084969"),
                                               actor_ref=actor_ref("qq", uid), cross_session=True)
            result["fallback"] = {"ids": [h.atom_id for h in hits], "weak": all(h.weak_related for h in hits)}
            assert hits and all(h.weak_related and h.session_id == "577084969" for h in hits)
            for mode in ("all", "same_user"):
                candidates = await mem.retriever._candidates("group", "577084969", "ling", True, mode,
                    session_ref=session_ref("qq", "group", "577084969"), actor_ref=actor_ref("qq", uid))
                leaks = [r["id"] for r in candidates if r["session_kind"] == "private" and r["session_id"] != uid]
                assert not leaks
                result["private_leaks_" + mode] = len(leaks)
        result["source_writes"] = 0
        return result
    finally:
        await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()
    result = inspect(args.database)
    if args.simulate:
        import asyncio
        result["ram_simulation"] = asyncio.run(simulate(args.database))
    print(json.dumps(result, ensure_ascii=True, indent=2))
