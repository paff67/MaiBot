import json

from src.platform_io.wechat.message_ingestor import MessageLedger
from src.platform_io.wechat.message_quotes import latest_quote
from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.message_media_worker import MessageMediaQueue
from src.companion_vault.quote_recall import recall_quotes
from src.companion_vault.truth_db import CompanionTruthDB


def test_quote_recall_exact_identity_and_revoke(tmp_path):
    ledger = MessageLedger(str(tmp_path / "ledger.db"))
    account = "a" * 32
    source = "http://edge"
    chat = "group@chatroom"

    def event(seq, mid, kind, content, sid):
        return dict(
            seq=seq,
            account=account,
            msg_id=mid,
            chat_id=chat,
            revision="r" + str(seq),
            observed_at_ms=seq,
            row=dict(msgId=mid, talker=chat, type=kind, content=content, msgSvrId=sid, isSend="0"),
        )

    ledger.ingest(
        source,
        "epoch",
        event(
            1, "1", "49", "<msg><appmsg><type>6</type><title>sample.pdf</title></appmsg></msg>", "8641217211407415614"
        ),
    )
    content = "<msg><appmsg><type>57</type><title>再看这个</title><refermsg><type>49</type><svrid>8641217211407415614</svrid><fromusr>group@chatroom</fromusr><content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;sample.pdf&lt;/title&gt;&lt;aeskey&gt;secret&lt;/aeskey&gt;&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>"
    ledger.ingest(source, "epoch", event(2, "2", "822083633", content, "9"))
    db = CompanionTruthDB(":memory:")
    assets = AssetPipeline(db, str(tmp_path / "assets"))
    MessageMediaQueue(assets, ledger.db, source, None)
    asset = assets.ingest(
        b"%PDF-original", {"mime_type": "application/pdf"}, source_id="sample", chat_id=account + "/" + chat
    )
    db.conn.execute(
        "INSERT INTO message_media_jobs(job_id,source,account,msg_id,revision,chat_id,ref_index,reference,state,updated_at) VALUES(?,?,?,?,?,?,0,?,'ready',0)",
        ("job", source, account, "1", "r1", chat, "{}"),
    )
    db.conn.execute(
        "INSERT INTO message_asset_links VALUES(?,?,?,?,?,?,?,1,?)",
        ("job", asset["file_code"], "original", account, "1", "r1", chat, "{}"),
    )
    db.conn.commit()
    block, files = recall_quotes(
        ledger.db, assets, source=source, account=account, chat_id=chat, quote_message_ids=["2"]
    )
    item = json.loads(block)["items"][0]
    assert item["target"]["msg_id"] == "1" and item["target_server_id"] == "8641217211407415614"
    assert [f["file_code"] for f in files] == [asset["file_code"]]
    assert "secret" not in block and "aeskey" not in block
    block, files = recall_quotes(
        ledger.db, assets, source=source, account=account, chat_id="other@chatroom", quote_message_ids=["2"]
    )
    assert not files and json.loads(block)["items"][0]["resolution"] == "quote_unavailable"
    ledger.ingest(source, "epoch", event(3, "1", "268445456", "revoked", "8641217211407415614"))
    block, files = recall_quotes(
        ledger.db, assets, source=source, account=account, chat_id=chat, quote_message_ids=["2"]
    )
    assert not files and json.loads(block)["items"][0]["resolution"] == "target_system_or_revoked"
    ledger.ingest(source, "epoch", event(4, "2", "268445456", "revoked", "9"))
    assert latest_quote(ledger.db, source, account, chat, "2") is None

    # Group chat quote where <fromusr> is a group member wxid instead of @chatroom
    ledger.ingest(source, "epoch", event(5, "5", "1", "wxid_member:\n被群友引用的消息", "8888888888888888"))
    member_quote_xml = (
        "<msg><appmsg><type>57</type><title>回复群友</title>"
        "<refermsg><type>1</type><svrid>8888888888888888</svrid>"
        "<fromusr>wxid_member</fromusr><chatusr>wxid_member</chatusr>"
        "<displayname>群友A</displayname><content>被群友引用的消息</content></refermsg></appmsg></msg>"
    )
    ledger.ingest(source, "epoch", event(6, "6", "822083633", "wxid_other:\n" + member_quote_xml, "8888888888888889"))
    resolved_member_quote = latest_quote(ledger.db, source, account, chat, "6")
    assert resolved_member_quote is not None
    assert resolved_member_quote["resolution"] == "resolved"
    assert resolved_member_quote["target"]["msg_id"] == "5"
    assert resolved_member_quote["target"]["sender_id"] == "wxid_member"
    ledger.db.close()
    db.close()
