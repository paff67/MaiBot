import asyncio
import json
import sqlite3

import httpx
import pytest

from src.companion_vault.truth_db import CompanionTruthDB
from src.companion_vault.access_policy import (
    allowed_conversations,
    replace_group_members,
    set_group_member,
    can_read_memory,
    authorized_file_rows,
)
from src.companion_vault.consolidator import MemoryConsolidator, read_scoped_memories
from src.companion_vault.group_members import sync_group
from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.message_media_worker import MessageMediaQueue
from src.companion_vault.quote_recall import recall_quotes
from src.companion_vault.runtime_bridge import build_planner_truth_catalog_async, load_reply_greenlights_async
from src.platform_io.wechat.message_ingestor import MessageLedger
from src.platform_io.wechat.projection import native_message_id

A = "a" * 32
S = "http://edge"
G = "g@chatroom"
H = "h@chatroom"
U = "wxid_u"
V = "wxid_v"


def event(seq, chat, kind="1", body="事实", mid=None, sid=None):
    mid = mid or str(seq)
    return dict(
        seq=seq,
        account=A,
        msg_id=mid,
        chat_id=chat,
        revision="r" + str(seq),
        observed_at_ms=seq,
        row=dict(
            msgId=mid,
            talker=chat,
            type=kind,
            content=(U + ":\n" + body if chat.endswith("@chatroom") else body),
            msgSvrId=sid or str(1000 + seq),
            isSend="0",
            createTime="1793880000000",
        ),
    )


def base(tmp_path):
    ledger = MessageLedger(str(tmp_path / "ledger.db"))
    db = CompanionTruthDB(str(tmp_path / "truth.db"))
    replace_group_members(db.conn, source=S, account=A, group_id=G, user_ids=[U])
    replace_group_members(db.conn, source=S, account=A, group_id=H, user_ids=[U, V])
    return ledger, db


def test_membership_policy_and_manual_removal(tmp_path):
    ledger, db = base(tmp_path)

    def allowed(dest, user):
        return allowed_conversations(db.conn, source=S, account=A, requester_id=user, destination_chat_id=dest)

    assert allowed(G, U) == {G} and allowed(U, U) == {U, G, H} and allowed(V, V) == {V, H}
    assert allowed(V, U) == set()
    set_group_member(db.conn, source=S, account=A, group_id=G, user_id=U, present=False)
    assert allowed(U, U) == {U, H}
    assert allowed_conversations(db.conn, source="other", account=A, requester_id=U, destination_chat_id=U) == {U}
    with pytest.raises(sqlite3.DatabaseError):
        db.execute_table_sql("INSERT INTO group_members VALUES('x','a','g','u')")
    ledger.db.close()
    db.close()


def test_memory_all_origins_must_be_authorized(tmp_path):
    ledger, db = base(tmp_path)
    for seq, chat in enumerate([G, H, U, V], 1):
        ledger.ingest(S, "epoch", event(seq, chat))
    for chat in [G, H, U, V]:
        worker = MemoryConsolidator(
            str(tmp_path / "truth.db"), str(tmp_path / "ledger.db"), source=S, account=A, chat_id=chat, backfill=True
        )
        worker.enqueue()
        job = worker.claim()
        mid = json.loads(job["sources"])[0]["msg_id"]
        worker.commit(
            job, {"memories": [dict(summary=chat, detail="事实", source_ids=[mid], quotes=["事实"], trigger="回忆")]}
        )
        worker.close()

    def read(chat):
        return read_scoped_memories(
            str(tmp_path / "truth.db"), str(tmp_path / "ledger.db"), source=S, account=A, chat_id=chat
        )

    assert {r["summary"] for r in read(U)} == {G, H, U}
    assert {r["summary"] for r in read(V)} == {H, V}
    assert {r["summary"] for r in read(G)} == {G}
    code = read(G)[0]["code_index"]
    with db.conn:
        db.conn.execute(
            "INSERT INTO memory_provenance VALUES(?,?,?,?,?,?,?,?)", (code, "mix", S, A, U, "3", "r3", "PRIVATE")
        )
    assert not can_read_memory(db.conn, code, source=S, account=A, conversations={G})
    assert read(G) == []  # mixed private provenance never flows back into a group
    set_group_member(db.conn, source=S, account=A, group_id=H, user_id=U, present=False)
    assert H not in {r["summary"] for r in read(U)}
    ledger.db.close()
    db.close()


def quote_xml(origin, sid):
    return f"<msg><appmsg><type>57</type><title>看这个</title><refermsg><type>49</type><svrid>{sid}</svrid><fromusr>{origin}</fromusr><content>snapshot</content></refermsg></appmsg></msg>"


def test_exact_group_file_quote_in_private_and_no_reverse_leak(tmp_path):
    ledger, db = base(tmp_path)
    body = "<msg><appmsg><type>6</type><title>file.txt</title></appmsg></msg>"
    ledger.ingest(S, "epoch", event(1, G, "49", body, sid="9999999999999999"))
    ledger.ingest(S, "epoch", event(2, U, "822083633", quote_xml(G, "9999999999999999")))
    ledger.ingest(S, "epoch", event(3, V, "822083633", quote_xml(G, "9999999999999999")))
    ledger.ingest(S, "epoch", event(4, H, "822083633", quote_xml(G, "9999999999999999")))
    assets = AssetPipeline(db, str(tmp_path / "assets"))
    MessageMediaQueue(assets, ledger.db, S, None)
    asset = assets.ingest(b"original", {"mime_type": "text/plain"}, source_id="asset", chat_id=A + "/" + G)
    with db.conn:
        db.conn.execute(
            "INSERT INTO message_media_jobs(job_id,source,account,msg_id,revision,chat_id,ref_index,reference,state,updated_at) VALUES('j',?,?,?,?,?,0,'{}','ready',0)",
            (S, A, "1", "r1", G),
        )
        db.conn.execute(
            "INSERT INTO message_asset_links VALUES('j',?,'original',?,?,?,?,1,'{}')",
            (asset["file_code"], A, "1", "r1", G),
        )

    def recall(chat, mid):
        return recall_quotes(ledger.db, assets, source=S, account=A, chat_id=chat, quote_message_ids=[mid])

    block, files = recall(U, "2")
    assert json.loads(block)["items"][0]["resolution"] == "resolved" and files[0]["file_code"] == asset["file_code"]
    assert recall(V, "3")[1] == [] and recall(H, "4")[1] == []
    assert authorized_file_rows(db.conn, ledger.db, source=S, account=A, destination_chat_id=V) == []
    set_group_member(db.conn, source=S, account=A, group_id=G, user_id=U, present=False)
    assert recall(U, "2")[1] == []
    assert "file.txt" not in recall(U, "2")[0]
    ledger.db.close()
    db.close()


def test_wechat_global_public_and_owner_cannot_bypass_scope(tmp_path):
    ledger, db = base(tmp_path)
    ledger.ingest(S, "epoch", event(1, G))
    with db.conn:
        db.conn.execute(
            "INSERT INTO chronicle_events(code_index,chronicle_text,summary,visibility) VALUES('AM999','private legacy secret','private legacy secret','PUBLIC')"
        )
    args = dict(
        is_group_chat=True,
        user_id=U,
        platform="wechat",
        anchor_message_id=native_message_id(S, A, "1"),
        account=A,
        chat_id=G,
        db_path=str(tmp_path / "truth.db"),
        ledger_path=str(tmp_path / "ledger.db"),
    )
    assert "private legacy secret" not in asyncio.run(build_planner_truth_catalog_async(session_id="s", **args))
    block, files = asyncio.run(load_reply_greenlights_async(memory_greenlights=["AM999"], file_greenlights=[], **args))
    assert "private legacy secret" not in block and files == []
    ledger.db.close()
    db.close()


def test_complete_sync_replaces_and_incomplete_sync_preserves(tmp_path):
    ledger, db = base(tmp_path)

    def handle(request):
        return httpx.Response(200, json=dict(ok=True, complete=True, account=A, group_id=G, user_ids=[V]))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert sync_group(db.conn, client, source=S, account=A, group_id=G) == 1
    assert db.conn.execute("SELECT user_id FROM group_members WHERE group_id=?", (G,)).fetchone()[0] == V
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=dict(ok=True, complete=False, account=A, group_id=G, user_ids=[]))
        )
    ) as client:
        with pytest.raises(ValueError):
            sync_group(db.conn, client, source=S, account=A, group_id=G)
    assert db.conn.execute("SELECT user_id FROM group_members WHERE group_id=?", (G,)).fetchone()[0] == V
    ledger.db.close()
    db.close()
