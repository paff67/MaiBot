import sqlite3
import time

import httpx
import pytest

from src.platform_io.wechat.message_ingestor import MessageLedger, poll
from src.phone_edge.message_journal import read_message_events
from src.companion_vault.message_archive import archive_page
from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.truth_db import CompanionTruthDB


ACCOUNT = "a" * 32


def event(seq=1, msg_id="1", chat="123@chatroom", body="wxid_sender:\nhello", revision="rev1"):
    return dict(
        seq=seq,
        account=ACCOUNT,
        msg_id=msg_id,
        chat_id=chat,
        revision=revision,
        observed_at_ms=100,
        row=dict(msgId=msg_id, talker=chat, content=body, isSend="0", type="1", msgSvrId="7384716752816422326"),
    )


def test_replay_restart_and_distinct_identical_messages(tmp_path):
    path = str(tmp_path / "ledger.db")
    db = MessageLedger(path)
    assert db.ingest("phone", "epoch", event())
    assert not db.ingest("phone", "epoch", event())
    db.db.close()
    db = MessageLedger(path)
    assert db.cursor("phone", "epoch") == 1
    assert db.ingest("phone", "epoch", event(2, "2"))
    rows = db.db.execute("SELECT sender_id,body,server_id FROM messages").fetchall()
    assert len(rows) == 2 and rows[0] == ("wxid_sender", "hello", "7384716752816422326")
    db.db.close()


def test_gap_and_wrong_ownership_never_acknowledged(tmp_path):
    db = MessageLedger(str(tmp_path / "ledger.db"))
    db.ingest("phone", "e", event())
    with pytest.raises(ValueError, match="gap"):
        db.ingest("phone", "e", event(3, "3"))
    with pytest.raises(ValueError, match="another"):
        db.ingest("phone", "e", event(2, "1", "other@chatroom"))
    assert db.cursor("phone", "e") == 1
    assert db.db.execute("SELECT count(*) FROM message_revisions").fetchone()[0] == 1
    db.db.close()


def test_update_epoch_and_account_isolation(tmp_path):
    db = MessageLedger(str(tmp_path / "ledger.db"))
    db.ingest("phone", "old", event())
    db.ingest("phone", "old", event(2, revision="rev2", body="revoked"))
    assert db.db.execute("SELECT body FROM messages").fetchone()[0] == "revoked"
    db.ingest("phone", "new", event(1, revision="rev2", body="revoked"))
    assert db.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    e = event(2)
    e["account"] = "b" * 32
    db.ingest("phone", "new", e)
    assert db.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
    db.db.close()


def test_database_failure_rolls_back_message_and_cursor(tmp_path):
    db = MessageLedger(str(tmp_path / "ledger.db"))
    db.db.execute("CREATE TRIGGER fail_ack BEFORE INSERT ON source_cursors BEGIN SELECT RAISE(ABORT,'disk-test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.ingest("phone", "e", event())
    assert db.cursor("phone", "e") == 0
    assert db.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert db.db.execute("SELECT count(*) FROM message_revisions").fetchone()[0] == 0
    db.db.close()


def test_phone_empty_journal_cursor_is_not_advanced(tmp_path):
    path = str(tmp_path / "journal.db")
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE journal_meta(key TEXT,value TEXT); INSERT INTO journal_meta VALUES('epoch','e');"
            "CREATE TABLE message_events(seq INTEGER,payload TEXT);CREATE TABLE capture_state(account TEXT,highwater INTEGER,sweep INTEGER);"
        )
    result = read_message_events(0, 10, path)
    assert result["events"] == [] and result["next_cursor"] == 0 and result["epoch"] == "e"


def test_stale_scanner_still_drains_persisted_journal(tmp_path):
    ledger = MessageLedger(str(tmp_path / "ledger.db"))
    page = dict(epoch="e", events=[event()], max_cursor=1, last_scan_ms=1)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=page))) as client:
        assert poll(ledger, client, "http://phone", "") == "e"
    assert ledger.cursor("http://phone", "e") == 1
    assert "stale" in ledger.db.execute("SELECT error FROM source_health").fetchone()[0]
    ledger.db.close()


def test_epoch_reset_uses_own_offset_and_conflicting_replay_rejected(tmp_path):
    ledger = MessageLedger(str(tmp_path / "ledger.db"))
    ledger.ingest("http://phone", "old", event())
    page = dict(epoch="new", events=[], max_cursor=0, last_scan_ms=int(time.time() * 1000))
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=page))) as client:
        assert poll(ledger, client, "http://phone", "old") == "new"
    assert ledger.cursor("http://phone", "new") == 0
    with pytest.raises(ValueError, match="different data"):
        ledger.ingest("http://phone", "old", event(body="tampered"))
    ledger.db.close()


def test_archive_separates_conversations_and_replay_is_idempotent(tmp_path):
    ledger = MessageLedger(str(tmp_path / "ledger.db"))
    ledger.ingest("phone", "e", event())
    ledger.ingest("phone", "e", event(2, "2", "wxid_private", "private text"))
    db = CompanionTruthDB(":memory:")
    assets = AssetPipeline(db, str(tmp_path / "assets"))
    assert archive_page(ledger.db, assets) == 2
    with ledger.db:
        ledger.db.execute("DELETE FROM archive_cursors")
    assert archive_page(ledger.db, assets) == 2
    assert db.conn.execute("SELECT count(*) FROM asset_sources").fetchone()[0] == 2
    assert db.conn.execute("SELECT count(DISTINCT chat_id) FROM asset_sources").fetchone()[0] == 2
    assert db.conn.execute("SELECT count(*) FROM media_vault WHERE visibility!='PRIVATE'").fetchone()[0] == 0
    assert archive_page(ledger.db, assets) == 0
    db.close()
    ledger.db.close()


def test_emoji_sender_is_extracted_without_changing_raw(tmp_path):
    ledger = MessageLedger(str(tmp_path / "ledger.db"))
    raw = 'wxid_sender:0:0:hash:wxid_sender*#*\n<msg><emoji fromusername="wxid_sender"/></msg>'
    e = event(body=raw)
    e["row"]["type"] = "47"
    ledger.ingest("phone", "e", e)
    assert ledger.db.execute("SELECT sender_id,raw_content FROM messages").fetchone() == ("wxid_sender", raw)
    ledger.db.close()
