import hashlib
import json
import sqlite3

import httpx

from src.phone_edge.media_references import message_media_refs
from src.phone_edge.media_store import MediaStore
from src.phone_edge.message_media import MessageMediaResolver
from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.truth_db import CompanionTruthDB
from src.companion_vault.message_media_worker import MessageMediaQueue
from src.platform_io.wechat.message_ingestor import MessageLedger

ACCOUNT = "a" * 32


def test_server_id_media_ownership_and_placeholder():
    import pytest

    info = dict(
        table="ImgInfo2",
        msglocalid=None,
        msgSvrId="1776803465121087096",
        msgTalker="group@chatroom",
        bigImgPath="SERVERID://1776803465121087096",
        totalLen=None,
        offset=None,
        iscomplete="1",
    )
    row = dict(
        type="3",
        msgId="5",
        msgSvrId=info["msgSvrId"],
        talker="group@chatroom",
        imgPath="THUMBNAIL_DIRPATH://th_abc",
        content='<msg><img hdlength="100"/></msg>',
        _media_info=[info],
    )
    assert len(message_media_refs(row)) == 1
    info.update(bigImgPath="original.jpg", totalLen="100", offset="100")
    ref = message_media_refs(row)[1]
    assert ref["variant"] == "original" and ref["download_complete"]
    assert ref["evidence"] == "ImgInfo2_msgSvrId_talker"
    info.update(totalLen="50", offset="50")
    assert message_media_refs(row)[1]["variant"] == "medium"
    info["msgTalker"] = "other@chatroom"
    with pytest.raises(ValueError, match="ownership"):
        message_media_refs(row)


def test_downloaded_attachment_exact_path_and_checksum(tmp_path):
    import pytest

    account = tmp_path / "MicroMsg" / ACCOUNT
    target = account / "message/media/opendata/aa/bb/name.txt"
    target.parent.mkdir(parents=True)
    data = b"original unmodified text"
    target.write_bytes(data)
    digest = hashlib.md5(data).hexdigest()
    row = dict(
        type="1090519089",
        msgId="6",
        talker="group@chatroom",
        content=f"<msg><appmsg><type>6</type><title>name.txt</title><appattach><md5>{digest}</md5></appattach></appmsg></msg>",
        _media_info=[
            dict(
                table="appattach",
                msgInfoId="6",
                msgInfoTalker="group@chatroom",
                fileFullPath=target.as_posix(),
                totalLen=str(len(data)),
                offset=str(len(data)),
            )
        ],
    )
    refs = message_media_refs(row)
    assert refs[1]["expected_md5"] == digest and refs[1]["download_complete"]
    store = MediaStore(str(tmp_path / "MicroMsg"), str(tmp_path / "Download"), settle_seconds=0)
    assert store.roots(ACCOUNT)["opendata"] == (account / "message/media/opendata").resolve()
    resolver = MessageMediaResolver(store)
    resolver.message = lambda *args: dict(chat_id="group@chatroom", row=row)
    result = resolver.resolve(ACCOUNT, "6", "r", 1)
    assert result["state"] == "resolved" and result["file"]["checksum_verified"]
    row["_media_info"][0]["msgInfoId"] = "7"
    with pytest.raises(ValueError, match="ownership"):
        message_media_refs(row)


def test_image_thumbnail_and_file_subtype():
    refs = message_media_refs(
        dict(type="3", imgPath="THUMBNAIL_DIRPATH://th_abc", content='<msg><img md5="' + "b" * 32 + '"/></msg>')
    )
    assert [r["variant"] for r in refs] == ["thumbnail", "original"]
    assert (
        message_media_refs(dict(type="49", content="<msg><appmsg><type>5</type><title>link.pdf</title></appmsg></msg>"))
        == []
    )
    ref = message_media_refs(
        dict(
            type="49",
            content="<msg><appmsg><type>6</type><title>file.pdf</title><appattach><md5>"
            + "b" * 32
            + "</md5><totallen>42</totallen></appattach></appmsg></msg>",
        )
    )[0]
    assert ref["kind"] == "document" and ref["expected_size"] == 42


def test_exact_emoji_resolution_and_revision_guard(tmp_path):
    data = b"GIF89a-original"
    digest = hashlib.md5(data).hexdigest()
    account = tmp_path / "MicroMsg" / ACCOUNT
    (account / "image2").mkdir(parents=True)
    (account / "voice2").mkdir()
    (account / "emoji").mkdir()
    (account / "emoji" / digest).write_bytes(data)
    journal = tmp_path / "journal.db"
    event = dict(
        account=ACCOUNT,
        msg_id="1",
        revision="rev",
        chat_id="group@chatroom",
        row=dict(type="47", imgPath=digest, content=f'<msg><emoji md5="{digest}"/></msg>'),
    )
    with sqlite3.connect(journal) as db:
        db.executescript(
            "CREATE TABLE message_heads(account TEXT,msg_id TEXT,revision TEXT);CREATE TABLE message_events(seq INTEGER,account TEXT,msg_id TEXT,revision TEXT,payload TEXT);"
        )
        db.execute("INSERT INTO message_heads VALUES(?,?,?)", (ACCOUNT, "1", "rev"))
        db.execute("INSERT INTO message_events VALUES(1,?,?,?,?)", (ACCOUNT, "1", "rev", json.dumps(event)))
    resolver = MessageMediaResolver(
        MediaStore(str(tmp_path / "MicroMsg"), str(tmp_path / "Download"), settle_seconds=0), str(journal)
    )
    result = resolver.resolve(ACCOUNT, "1", "rev", 0)
    assert result["state"] == "resolved" and result["file"]["checksum_verified"]
    assert result["file"]["sha256"] == hashlib.sha256(data).hexdigest()
    (account / "emoji" / "decrypt").mkdir()
    (account / "emoji" / "decrypt" / digest).write_bytes(b"wxgf-opaque")
    resolver.cache.clear()
    assert resolver.resolve(ACCOUNT, "1", "rev", 0)["file"]["mime_type"] == "image/gif"
    import pytest

    with pytest.raises(ValueError):
        resolver.resolve(ACCOUNT, "1", "wrong", 0)


def test_queue_idempotence_and_cross_chat_source_isolation(tmp_path):
    data = b"\xff\xd8\xffthumbnail"
    ledger = MessageLedger(str(tmp_path / "ledger.db"))
    event = dict(
        seq=1,
        account=ACCOUNT,
        msg_id="1",
        revision="r1",
        chat_id="group@chatroom",
        observed_at_ms=10,
        row=dict(
            msgId="1", talker="group@chatroom", type="3", imgPath="THUMBNAIL_DIRPATH://th_abc", content="", isSend="0"
        ),
    )
    ledger.ingest("http://edge", "epoch", event)
    db = CompanionTruthDB(":memory:")
    assets = AssetPipeline(db, str(tmp_path / "assets"))

    def handle(request):
        if request.url.path.endswith("media_resolve"):
            return httpx.Response(
                200,
                json=dict(
                    chat_id="group@chatroom",
                    state="resolved",
                    file=dict(
                        account=ACCOUNT,
                        kind="image",
                        relative_path="ab/th_abc",
                        mtime_ns=1,
                        ctime_ns=1,
                        size=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                        checksum_verified=False,
                        representation="thumbnail",
                        mime_type="image/jpeg",
                    ),
                ),
            )
        return httpx.Response(200, content=data, headers={"X-Content-SHA256": hashlib.sha256(data).hexdigest()})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        worker = MessageMediaQueue(assets, ledger.db, "http://edge", client)
        assert worker.enqueue() == 1 and worker.enqueue() == 0
        assert worker.run_one()
        assert db.conn.execute("SELECT state FROM message_media_jobs").fetchone()[0] == "thumbnail_only"
        assert db.conn.execute("SELECT count(*) FROM message_asset_links").fetchone()[0] == 1
        with db.conn:
            db.conn.execute("UPDATE message_media_jobs SET state='pending',next_attempt=0")
        worker.run_one()
        assert db.conn.execute("SELECT count(*) FROM asset_sources").fetchone()[0] == 1
        updated = dict(event, seq=2, revision="metadata-r2", row=dict(event["row"], status="2"))
        ledger.ingest("http://edge", "epoch", updated)
        worker.enqueue()
        with db.conn:
            db.conn.execute("UPDATE message_media_jobs SET state='running',lease_until=1 WHERE revision='metadata-r2'")
        worker.run_one()
        assert db.conn.execute("SELECT count(*) FROM asset_sources").fetchone()[0] == 1
        assert db.conn.execute("SELECT count(*) FROM message_asset_links WHERE active=1").fetchone()[0] == 1
        event2 = dict(event, seq=3, revision="r2", row=dict(event["row"], type="268445456", content="revoked"))
        ledger.ingest("http://edge", "epoch", event2)
        worker.enqueue()
        assert db.conn.execute("SELECT count(*) FROM message_asset_links WHERE active=1").fetchone()[0] == 0
    ledger.db.close()
    db.close()
