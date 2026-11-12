from types import SimpleNamespace
import asyncio
import json

from src.companion_vault.truth_db import CompanionTruthDB
from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.message_media_worker import MessageMediaQueue
from src.companion_vault.wechat_recall import build_quote_context_async
from src.platform_io.wechat.message_ingestor import MessageLedger
from src.platform_io.wechat.projection import native_message_id

ACCOUNT = "a" * 32
SOURCE = "http://edge"
CHAT = "group@chatroom"


def fixtures(tmp_path, kind="1", body="完整原文"):
    path = tmp_path / "ledger.db"
    ledger = MessageLedger(str(path))

    def put(seq, mid, typ, content, sid):
        ledger.ingest(
            SOURCE,
            "epoch",
            dict(
                seq=seq,
                account=ACCOUNT,
                msg_id=mid,
                chat_id=CHAT,
                revision="r" + str(seq),
                observed_at_ms=seq,
                row=dict(
                    msgId=mid,
                    talker=CHAT,
                    type=typ,
                    content=content,
                    msgSvrId=sid,
                    isSend="0",
                    createTime="1793880000000",
                ),
            ),
        )

    put(1, "1", kind, body, "9007199254740993")
    put(
        2,
        "2",
        "822083633",
        "<msg><appmsg><type>57</type><title>再看这个</title><refermsg><type>"
        + kind
        + "</type><svrid>9007199254740993</svrid><fromusr>"
        + CHAT
        + "</fromusr><content>不能当原文的快照</content></refermsg></appmsg></msg>",
        "2",
    )
    db = CompanionTruthDB(str(tmp_path / "truth.db"))
    assets = AssetPipeline(db, str(tmp_path / "assets"))
    MessageMediaQueue(assets, ledger.db, SOURCE, None)
    message = SimpleNamespace(
        platform="wechat",
        account_id=ACCOUNT,
        message_id=native_message_id(SOURCE, ACCOUNT, "2"),
        message_info=SimpleNamespace(
            group_info=SimpleNamespace(group_id=CHAT), user_info=SimpleNamespace(user_id="wxid")
        ),
    )
    return ledger, db, assets, message, put


def test_current_quote_full_original_no_model_selection(tmp_path):
    original = "原文" * 17000
    ledger, db, assets, message, put = fixtures(tmp_path, body=original)

    async def run():
        return await build_quote_context_async(
            [message], db_path=str(tmp_path / "truth.db"), ledger_path=str(tmp_path / "ledger.db")
        )

    block, parts = asyncio.run(run())
    item = json.loads(block)["items"][0]
    assert item["target"]["content"]["text"] == original and not item["target"]["content"]["truncated"]
    assert not parts
    message.message_info.group_info.group_id = "other@chatroom"
    assert asyncio.run(run()) == ("", [])
    ledger.db.close()
    db.close()


def test_verified_file_original_read_and_revoke_during_io(tmp_path, monkeypatch):
    ledger, db, assets, message, put = fixtures(
        tmp_path, kind="49", body="<msg><appmsg><type>6</type><title>test.txt</title></appmsg></msg>"
    )
    asset = assets.ingest(
        "原始文件，不压缩".encode(), {"mime_type": "text/plain"}, source_id="file", chat_id=ACCOUNT + "/" + CHAT
    )
    with db.conn:
        db.conn.execute(
            "INSERT INTO message_media_jobs(job_id,source,account,msg_id,revision,chat_id,ref_index,reference,state,updated_at) VALUES(?,?,?,?,?,?,0,'{}','ready',0)",
            ("j", SOURCE, ACCOUNT, "1", "r1", CHAT),
        )
        db.conn.execute(
            "INSERT INTO message_asset_links VALUES(?,?,?,?,?,?,?,1,'{}')",
            ("j", asset["file_code"], "original", ACCOUNT, "1", "r1", CHAT),
        )

    async def run():
        return await build_quote_context_async(
            [message], db_path=str(tmp_path / "truth.db"), ledger_path=str(tmp_path / "ledger.db")
        )

    block, parts = asyncio.run(run())
    assert parts[0]["text"] == "原始文件，不压缩"
    from src.companion_vault import quote_recall

    original = quote_recall.load_original_descriptors

    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        # A separate ingestion connection can update while file work is running.
        other = MessageLedger(str(tmp_path / "ledger.db"))
        other.ingest(
            SOURCE,
            "epoch",
            dict(
                seq=3,
                account=ACCOUNT,
                msg_id="1",
                chat_id=CHAT,
                revision="r3",
                observed_at_ms=3,
                row=dict(
                    msgId="1", talker=CHAT, type="268445456", content="revoked", msgSvrId="9007199254740993", isSend="0"
                ),
            ),
        )
        other.db.close()
        return result

    monkeypatch.setattr(quote_recall, "load_original_descriptors", changing)
    block, parts = asyncio.run(run())
    assert json.loads(block)["resolution"] == "changed_during_recall" and not parts
    ledger.db.close()
    db.close()


def test_native_request_assembler_preserves_quote_file_item():
    # Compile the real method without importing the entire configured application.
    import ast
    from pathlib import Path
    from datetime import datetime
    from src.llm_models.payload_content.context_item import (
        ContextItemBuilder,
        RoleType,
        UserMessageItem,
        FunctionCallOutputItem,
        ContextFileUriPart,
    )
    from src.companion_vault.wechat_recall import quote_context_item

    tree = ast.parse(Path("src/maisaka/chat_loop_service.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MaisakaChatLoopService")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_build_request_messages")
    method.decorator_list = []
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method],
        type_ignores=[],
    )
    namespace = dict(
        ContextItemBuilder=ContextItemBuilder,
        RoleType=RoleType,
        UserMessageItem=UserMessageItem,
        FunctionCallOutputItem=FunctionCallOutputItem,
        datetime=datetime,
    )
    exec(compile(ast.fix_missing_locations(module), "<native-request-assembler>", "exec"), namespace)
    service = SimpleNamespace(
        _custom_chat_system_prompt="system",
        _build_chat_system_prompt=lambda: "system",
        _build_bot_nickname_notice=lambda *a, **kw: "",
        _build_current_chat_attention_tail_message=lambda: "",
        _build_current_time_user_message=lambda: "now",
        _validate_function_call_context_anchors=lambda items: None,
    )
    evidence = quote_context_item(
        "exact quote", [dict(file_uri="https://example.invalid/file", mime_type="application/pdf", file_code="FM1")]
    )
    items, count = namespace["_build_request_messages"](
        service, [], enable_visual_message=False, injected_user_messages=[evidence], system_prompt="system"
    )
    assert evidence in items and count == 0
    assert any(isinstance(part, ContextFileUriPart) for part in evidence.parts)


def test_current_turn_direct_attachment_and_revoke_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIBOT_ASSET_SPOOL", str(tmp_path / "assets"))
    ledger, db, assets, _quote_msg, _put = fixtures(
        tmp_path, kind="49", body="<msg><appmsg><type>6</type><title>direct.txt</title></appmsg></msg>"
    )
    asset = assets.ingest(
        "当前轮直接附件原文".encode(),
        {"mime_type": "text/plain", "relative_path": "direct.txt"},
        source_id="direct-file",
        chat_id=ACCOUNT + "/" + CHAT,
    )
    with db.conn:
        db.conn.execute(
            "INSERT INTO message_media_jobs(job_id,source,account,msg_id,revision,chat_id,ref_index,reference,state,updated_at) VALUES(?,?,?,?,?,?,0,'{}','ready',0)",
            ("j-direct", SOURCE, ACCOUNT, "1", "r1", CHAT),
        )
        db.conn.execute(
            "INSERT INTO message_asset_links VALUES(?,?,?,?,?,?,?,1,'{}')",
            ("j-direct", asset["file_code"], "original", ACCOUNT, "1", "r1", CHAT),
        )
    direct_message = SimpleNamespace(
        platform="wechat",
        account_id=ACCOUNT,
        message_id=native_message_id(SOURCE, ACCOUNT, "1"),
        message_info=SimpleNamespace(
            group_info=SimpleNamespace(group_id=CHAT), user_info=SimpleNamespace(user_id="wxid")
        ),
    )

    async def run():
        return await build_quote_context_async(
            [direct_message], db_path=str(tmp_path / "truth.db"), ledger_path=str(tmp_path / "ledger.db")
        )

    block, parts = asyncio.run(run())
    payload = json.loads(block)
    assert payload["recall_type"] == "current_turn_message_attachment"
    assert payload["file_codes"] == [asset["file_code"]]
    assert parts[0]["text"] == "当前轮直接附件原文"
    assert parts[0]["semantic_name"] == "direct.txt"

    from src.companion_vault import quote_recall

    original = quote_recall.load_original_descriptors

    def revoking(*args, **kwargs):
        result = original(*args, **kwargs)
        other = MessageLedger(str(tmp_path / "ledger.db"))
        other.ingest(
            SOURCE,
            "epoch",
            dict(
                seq=3,
                account=ACCOUNT,
                msg_id="1",
                chat_id=CHAT,
                revision="r3",
                observed_at_ms=3,
                row=dict(
                    msgId="1", talker=CHAT, type="268445456", content="revoked", msgSvrId="9007199254740993", isSend="0"
                ),
            ),
        )
        other.db.close()
        return result

    monkeypatch.setattr(quote_recall, "load_original_descriptors", revoking)
    block, parts = asyncio.run(run())
    assert json.loads(block)["resolution"] == "changed_during_recall" and not parts
    ledger.db.close()
    db.close()


def test_unified_greenlight_asset_pipeline_descriptors(tmp_path, monkeypatch):
    from src.companion_vault.runtime_bridge import load_reply_greenlights_async

    spool = tmp_path / "assets"
    monkeypatch.setenv("MAIBOT_ASSET_SPOOL", str(spool))
    db_file = str(tmp_path / "truth.db")
    db = CompanionTruthDB(db_file)
    assets = AssetPipeline(db, str(spool))

    txt = assets.ingest(
        "纯文本原件内容".encode(),
        {"mime_type": "text/plain", "relative_path": "note.txt"},
        source_id="src-txt",
        chat_id=ACCOUNT + "/" + CHAT,
    )
    silk = assets.ingest(
        b"SILK_RAW",
        {"mime_type": "audio/x-silk", "relative_path": "voice.amr"},
        source_id="src-voice",
        chat_id=ACCOUNT + "/" + CHAT,
    )
    assets.ingest(
        b"RIFF_WAV_LOSSLESS",
        {"mime_type": "audio/wav", "relative_path": "voice.wav", "representation": "decoded_lossless"},
        source_id="src-voice:wav",
        chat_id=ACCOUNT + "/" + CHAT,
    )
    pdf = assets.ingest(
        b"%PDF-1.7-original",
        {"mime_type": "application/pdf", "relative_path": "spec.pdf"},
        source_id="src-pdf",
        chat_id=ACCOUNT + "/" + CHAT,
    )
    with db.conn:
        db.conn.execute("UPDATE media_vault SET visibility='PUBLIC'")

    catalog = db.build_planner_catalog_injection(is_group_chat=True, sender_trust_role="GROUP_MEMBER")
    assert "file_code | semantic_name | mime_type | chat_context | trigger_when" in catalog
    assert "distinctive_features" not in catalog and "extracted_entities" not in catalog
    db.close()

    uploads = []

    class FakeFiles:
        def upload(self, *, file, config):
            data = file.read() if hasattr(file, "read") else open(file, "rb").read()
            uploads.append((data, config["mime_type"]))
            idx = len(uploads)
            return SimpleNamespace(
                uri=f"https://generativelanguage.googleapis.com/v1beta/files/mock-{idx}",
                state=SimpleNamespace(name="ACTIVE"),
                expiration_time=None,
            )

    closed = []

    def factory():
        return SimpleNamespace(files=FakeFiles(), close=lambda: closed.append(True))

    async def load(codes, use_factory=True):
        return await load_reply_greenlights_async(
            is_group_chat=True,
            user_id="u1",
            platform="qq",
            memory_greenlights=[],
            file_greenlights=codes,
            db_path=db_file,
            client_factory=factory if use_factory else None,
        )

    _, parts = asyncio.run(load([txt["file_code"], silk["file_code"], pdf["file_code"]]))
    assert len(parts) == 3
    assert parts[0] == {"file_code": txt["file_code"], "semantic_name": "note.txt", "text": "纯文本原件内容"}
    assert parts[1]["file_code"] == silk["file_code"]
    assert parts[1]["semantic_name"] == "voice.amr"
    assert parts[1]["mime_type"] == "audio/wav"
    assert parts[1]["file_uri"].endswith("/mock-1")
    assert parts[2]["file_code"] == pdf["file_code"]
    assert parts[2]["semantic_name"] == "spec.pdf"
    assert parts[2]["mime_type"] == "application/pdf"
    assert parts[2]["file_uri"].endswith("/mock-2")
    assert len(uploads) == 2 and closed == [True]

    # Second recall of PDF hits cached gemini_file_uri without needing client upload, while still checking local SHA256
    _, cached_parts = asyncio.run(load([pdf["file_code"]], use_factory=False))
    assert len(uploads) == 2
    assert cached_parts == [
        {
            "file_code": pdf["file_code"],
            "semantic_name": "spec.pdf",
            "file_uri": parts[2]["file_uri"],
            "mime_type": "application/pdf",
        }
    ]
