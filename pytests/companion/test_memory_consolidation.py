import asyncio
import json
import sqlite3

import pytest

from src.platform_io.wechat.message_ingestor import MessageLedger
from src.companion_vault.consolidator import (
    MemoryConsolidator,
    build_consolidation_system_prompt,
    extract_table_sql,
    read_scoped_memories,
)

ACCOUNT = "a" * 32
SOURCE = "http://edge"
CHAT = "group@chatroom"


def ingest(ledger, seq, mid="1", revision=None, kind="1", chat=CHAT, body="原文事实"):
    event = dict(
        seq=seq,
        account=ACCOUNT,
        msg_id=mid,
        chat_id=chat,
        revision=revision or "r" + str(seq),
        observed_at_ms=seq,
        row=dict(
            msgId=mid,
            talker=chat,
            type=kind,
            content=body,
            msgSvrId="9007199254740993",
            isSend="0",
            createTime="1793880000000",
        ),
    )
    ledger.ingest(SOURCE, "epoch", event)


def setup(tmp_path, backfill=True, chat=CHAT):
    ledger = MessageLedger(str(tmp_path / "ledger.db"))
    ingest(ledger, 1, chat=chat)
    worker = MemoryConsolidator(
        str(tmp_path / "truth.db"),
        str(tmp_path / "ledger.db"),
        source=SOURCE,
        account=ACCOUNT,
        chat_id=chat,
        backfill=backfill,
    )
    return ledger, worker


def sql_output(code="AM0001", summary="事实", detail="用户陈述原文事实", quote="原文事实", trigger="回忆该事实时"):
    return (
        f"INSERT INTO chronicle_events(code_index, summary, chronicle_text, key_dialogue, trigger_when, visibility, status) "
        f"VALUES ('{code}', '{summary}', '{detail}', '[\"{quote}\"]', '{trigger}', 'PRIVATE', 'active');"
    )


def test_atomic_commit_idempotence_scope_and_model_permissions(tmp_path):
    ledger, worker = setup(tmp_path)
    assert worker.enqueue() == 1 and worker.enqueue() == 0
    job = worker.claim()
    assert worker.claim() is None
    bad_public = (
        "INSERT INTO chronicle_events(code_index, summary, chronicle_text, visibility) "
        "VALUES ('AM0001', '事实', '用户陈述原文事实', 'PUBLIC');"
    )
    with pytest.raises(ValueError, match="PRIVATE"):
        worker.commit(job, bad_public)
    assert worker.commit(job, sql_output()) and not worker.commit(job, sql_output())
    rows = worker.read_scoped()
    assert len(rows) == 1 and rows[0]["visibility"] == "PRIVATE"
    code = rows[0]["code_index"]
    assert worker.db.execute("SELECT count(*) FROM memory_provenance").fetchone()[0] == 1

    # Cross-scope worker cannot tamper with another conversation's chronicle_events row
    ingest(ledger, 2, mid="2", chat="other@chatroom", body="其他群消息")
    other_worker = MemoryConsolidator(
        str(tmp_path / "truth.db"),
        str(tmp_path / "ledger.db"),
        source=SOURCE,
        account=ACCOUNT,
        chat_id="other@chatroom",
        backfill=True,
    )
    other_worker.enqueue()
    other_job = other_worker.claim()
    with pytest.raises(ValueError, match="another conversation scope"):
        other_worker.commit(other_job, f"UPDATE chronicle_events SET summary='越权篡改' WHERE code_index='{code}';")
    other_worker.close()

    assert code not in worker.truth.build_planner_catalog_injection(is_group_chat=False, sender_trust_role="OWNER")
    assert (
        code
        not in worker.truth.load_greenlight_payload(
            memory_codes=[code], file_codes=[], sender_trust_role="OWNER", is_group_chat=False
        )[0]
    )
    assert not read_scoped_memories(
        str(tmp_path / "truth.db"), str(tmp_path / "ledger.db"), source=SOURCE, account=ACCOUNT, chat_id="other"
    )
    ledger.db.close()
    worker.close()


def test_multi_table_sql_consolidation_and_supersede(tmp_path):
    ledger, worker = setup(tmp_path, chat="wxid_u")
    with worker.db:
        worker.db.execute(
            "INSERT INTO person_info(person_id, person_name, trust_role, platform_ids) "
            "VALUES ('wxid_u', '阿伟', 'GROUP_MEMBER', '{\"wechat\":\"wxid_u\"}')"
        )
    prompt = build_consolidation_system_prompt(str(tmp_path / "truth.db"))
    assert "AM0001" in prompt and "CREATE TABLE chronicle_events" in prompt

    worker.enqueue()
    job = worker.claim()
    fenced_sql = """```sql
    INSERT INTO global_state(row_id, current_location, current_minor_region, current_major_region)
    VALUES (1, '自习室', '南校区', '上海')
    ON CONFLICT(row_id) DO UPDATE SET current_location=excluded.current_location;
    INSERT INTO user_status(key, category, current_value, visibility, updated_at)
    VALUES ('exam_prep', 'study', '准备高数期中', 'PRIVATE', '2026-11-06 18:00');
    INSERT INTO quests_events(quest_code, title, current_progress, status, visibility)
    VALUES ('Q0001', '高数期中复习', '已完成第三章', 'ongoing', 'PRIVATE');
    UPDATE person_info SET impression='认真复习的群友', intimacy_score=15.0 WHERE person_id='wxid_u';
    INSERT INTO chronicle_events(code_index, time_span, location, summary, chronicle_text, key_dialogue, trigger_when)
    VALUES ('AM0001', '2026-11-06 18:00 ~ 2026-11-06 18:05', '自习室', '阿伟在自习室复习高数', '阿伟提到自己在南校区自习室复习高数第三章。', '["原文事实"]', '聊起高数或复习进度时');
    ```"""
    assert extract_table_sql({"table_sql": fenced_sql}).startswith("INSERT INTO global_state")
    assert worker.commit(job, fenced_sql)

    assert worker.db.execute("SELECT current_location FROM global_state WHERE row_id=1").fetchone()[0] == "自习室"
    assert worker.db.execute("SELECT current_value FROM user_status WHERE key='exam_prep'").fetchone()[0] == "准备高数期中"
    assert worker.db.execute("SELECT current_progress FROM quests_events WHERE quest_code='Q0001'").fetchone()[0] == "已完成第三章"
    assert worker.db.execute("SELECT impression FROM person_info WHERE person_id='wxid_u'").fetchone()[0] == "认真复习的群友"
    scoped = worker.read_scoped()
    assert [r["code_index"] for r in scoped] == ["AM0001"]

    # Second turn supersedes AM0001 and inserts AM0002
    ingest(ledger, 2, mid="2", chat="wxid_u", body="已经复习完第四章了")
    worker.enqueue()
    job2 = worker.claim()
    built = worker.build_prompt(worker.prepare(job2))
    assert "AM0002" in built and "<recent_context_readonly>" in built
    supersede_sql = """
    UPDATE chronicle_events SET status='superseded', valid_until='2026-11-06 19:00' WHERE code_index='AM0001';
    INSERT INTO chronicle_events(code_index, summary, chronicle_text, key_dialogue, trigger_when, valid_from)
    VALUES ('AM0002', '阿伟完成第四章复习', '阿伟更新进度称已经复习完高数第四章。', '["已经复习完第四章了"]', '聊起高数进度时', '2026-11-06 19:00');
    """
    assert worker.commit(job2, {"table_sql": supersede_sql})
    assert [r["code_index"] for r in worker.read_scoped()] == ["AM0002"]
    assert worker.db.execute("SELECT status FROM chronicle_events WHERE code_index='AM0001'").fetchone()[0] == "superseded"

    # Group chat cannot modify owner user_status or global_state
    ingest(ledger, 3, mid="3", chat=CHAT, body="群聊消息")
    g_worker = MemoryConsolidator(
        str(tmp_path / "truth.db"),
        str(tmp_path / "ledger.db"),
        source=SOURCE,
        account=ACCOUNT,
        chat_id=CHAT,
        backfill=True,
    )
    g_worker.enqueue()
    g_job = g_worker.claim()
    with pytest.raises(ValueError, match="Group chat consolidation cannot modify owner user_status or global_state"):
        g_worker.commit(
            g_job,
            "INSERT INTO user_status(key, category, current_value) VALUES ('hack', 'test', 'bad');",
        )
    with pytest.raises(ValueError, match="Group chat consolidation cannot overwrite existing person_name"):
        g_worker.commit(
            g_job,
            "UPDATE person_info SET person_name='被群聊覆盖的名字' WHERE person_id='wxid_u';",
        )
    g_worker.close()
    ledger.db.close()
    worker.close()


def test_restart_expired_lease_fencing_and_retry(tmp_path):
    ledger, worker = setup(tmp_path)
    worker.enqueue()
    first = worker.claim()
    with worker.db:
        worker.db.execute("UPDATE memory_jobs SET lease_until=0")
    worker.close()
    worker = MemoryConsolidator(
        str(tmp_path / "truth.db"), str(tmp_path / "ledger.db"), source=SOURCE, account=ACCOUNT, chat_id=CHAT
    )
    second = worker.claim()
    assert second["lease_token"] != first["lease_token"]
    assert not worker.commit(first, sql_output())
    worker.fail(second, RuntimeError("sensitive raw data"))
    assert worker.db.execute("SELECT state,error FROM memory_jobs").fetchone()[:] == ("retry", "RuntimeError")
    with worker.db:
        worker.db.execute("UPDATE memory_jobs SET next_attempt=0")
    assert worker.commit(worker.claim(), sql_output())
    ledger.db.close()
    worker.close()


def test_source_update_and_revoke_before_and_after_commit(tmp_path):
    ledger, worker = setup(tmp_path)
    worker.enqueue()
    job = worker.claim()
    ingest(ledger, 2, kind="268445456", body="撤回")
    assert not worker.commit(job, sql_output())
    assert not worker.read_scoped()
    ingest(ledger, 3)
    worker.enqueue()
    assert worker.commit(worker.claim(), sql_output())
    ingest(ledger, 4, kind="268445456", body="撤回")
    assert not worker.read_scoped()  # read-time guard even before queue sees update
    worker.enqueue()
    assert worker.db.execute("SELECT status FROM chronicle_events").fetchone()[0] == "superseded"
    ledger.db.close()
    worker.close()


def test_model_failure_and_batch_rollback(tmp_path):
    ledger, worker = setup(tmp_path)
    worker.enqueue()

    async def broken(inputs):
        raise RuntimeError("transient")

    assert asyncio.run(worker.run_one(broken))
    with worker.db:
        worker.db.execute("UPDATE memory_jobs SET next_attempt=0")
    job = worker.claim()
    bad_batch_sql = (
        sql_output("AM0001")
        + " INSERT INTO person_info(person_id, trust_role) VALUES ('hacker', 'OWNER');"
    )
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        worker.commit(job, bad_batch_sql)
    assert worker.db.execute("SELECT count(*) FROM chronicle_events").fetchone()[0] == 0
    assert worker.db.execute("SELECT state FROM memory_jobs").fetchone()[0] == "running"
    ledger.db.close()
    worker.close()


def test_default_does_not_backfill_historical_messages(tmp_path):
    ledger, worker = setup(tmp_path, backfill=False)
    assert worker.enqueue() == 0
    ingest(ledger, 2, mid="2")
    assert worker.enqueue() == 1
    job = worker.claim()
    assert [x["msg_id"] for x in json.loads(job["sources"])] == ["2"]
    ledger.db.close()
    worker.close()


def test_mixed_batch_rebuild_does_not_lose_survivors(tmp_path):
    ledger, worker = setup(tmp_path)
    ingest(ledger, 2, mid="2")
    worker.enqueue()
    job = worker.claim()
    ingest(ledger, 3, kind="268445456", body="撤回")
    assert not worker.commit(job, sql_output())
    replacement = worker.claim()
    assert [x["msg_id"] for x in json.loads(replacement["sources"])] == ["2"]
    assert worker.commit(replacement, sql_output("AM0001"))
    ingest(ledger, 4, mid="2", revision="r2")
    worker.enqueue()
    next_job = worker.claim()
    if next_job:
        assert not worker.commit(next_job, sql_output("AM0002"))
    assert worker.db.execute("SELECT count(*) FROM chronicle_events").fetchone()[0] == 1
    ledger.db.close()
    worker.close()


def test_scoped_catalog_and_greenlight_reuse_existing_store(tmp_path):
    from src.companion_vault.runtime_bridge import build_planner_truth_catalog_async, load_reply_greenlights_async
    from src.platform_io.wechat.projection import native_message_id

    ledger, worker = setup(tmp_path)
    worker.enqueue()
    worker.commit(worker.claim(), sql_output())
    code = worker.read_scoped()[0]["code_index"]
    args = dict(
        is_group_chat=True,
        user_id="wxid",
        platform="wechat",
        db_path=str(tmp_path / "truth.db"),
        ledger_path=str(tmp_path / "ledger.db"),
        anchor_message_id=native_message_id(SOURCE, ACCOUNT, "1"),
        account=ACCOUNT,
        chat_id=CHAT,
    )
    catalog = asyncio.run(build_planner_truth_catalog_async(session_id="existing-session", **args))
    assert code in catalog
    block, _ = asyncio.run(load_reply_greenlights_async(memory_greenlights=[code], file_greenlights=[], **args))
    assert "用户陈述原文事实" in block
    ingest(ledger, 2, kind="268445456", body="撤回")
    block, _ = asyncio.run(load_reply_greenlights_async(memory_greenlights=[code], file_greenlights=[], **args))
    assert "用户陈述原文事实" not in block
    ledger.db.close()
    worker.close()


def test_committed_batch_rebuilds_unrevoked_facts(tmp_path):
    ledger, worker = setup(tmp_path)
    ingest(ledger, 2, mid="2")
    worker.enqueue()
    worker.commit(worker.claim(), sql_output("AM0001"))
    ingest(ledger, 3, kind="268445456", body="撤回")
    worker.enqueue()
    replacement = worker.claim()
    assert [x["msg_id"] for x in json.loads(replacement["sources"])] == ["2"]
    assert worker.commit(replacement, sql_output("AM0002"))
    assert len(worker.read_scoped()) == 1
    ledger.db.close()
    worker.close()


def test_min_batch_debounce_cst_and_speaker_role(tmp_path):
    import time

    ledger = MessageLedger(str(tmp_path / "ledger.db"))
    now_ms = int(time.time() * 1000)
    event1 = dict(
        seq=1,
        account=ACCOUNT,
        msg_id="100",
        chat_id="wxid_owner",
        revision="r1",
        observed_at_ms=now_ms,
        row=dict(
            msgId="100",
            talker="wxid_owner",
            type="1",
            content="111",
            msgSvrId="100",
            isSend="0",
            createTime="1793968104000",
        ),
    )
    event2 = dict(
        seq=2,
        account=ACCOUNT,
        msg_id="101",
        chat_id="wxid_owner",
        revision="r2",
        observed_at_ms=now_ms + 1000,
        row=dict(
            msgId="101",
            talker="wxid_owner",
            type="1",
            content="收到啦",
            msgSvrId="101",
            isSend="1",
            createTime="1793968110000",
        ),
    )
    ledger.ingest(SOURCE, "epoch", event1)
    ledger.ingest(SOURCE, "epoch", event2)
    worker = MemoryConsolidator(
        str(tmp_path / "truth.db"),
        str(tmp_path / "ledger.db"),
        source=SOURCE,
        account=ACCOUNT,
        chat_id="wxid_owner",
        backfill=True,
    )
    # With min_batch_size=10 and fresh messages (< 300s), enqueue defers job creation
    assert worker.enqueue(limit=20, min_batch_size=10, max_wait_seconds=300) == 0
    assert worker.claim() is None
    # Once max_wait_seconds=0 or min_batch_size=1, enqueue creates the batch
    assert worker.enqueue(limit=20, min_batch_size=1, max_wait_seconds=0) == 2
    job = worker.claim()
    inputs = worker.prepare(job)
    assert inputs[0]["speaker_role"] == "USER"
    assert inputs[0]["create_time_cst"] == "2026-11-06 20:28:24"
    assert inputs[1]["speaker_role"] == "BOT_SELF"
    assert inputs[1]["create_time_cst"] == "2026-11-06 20:28:30"
    ledger.db.close()
    worker.close()
