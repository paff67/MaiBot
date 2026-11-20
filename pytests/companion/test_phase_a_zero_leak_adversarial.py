"""Phase A authorization and cross-domain adversarial acceptance."""

import json
from types import SimpleNamespace
import pytest

from src.companion_vault.access_policy import replace_group_members
from src.companion_vault.paths import resolve_truth_db_path
from src.companion_vault.runtime_bridge import _resolve_trust_role, load_reply_greenlights_async
from src.companion_vault.truth_db import CompanionTruthDB
from src.companion_vault.output_guardrail import check_group_outbound_text
from src.platform_io.wechat.projection import enrich_sender_identity, parse_wechat_text_to_raw_segments


def seed(tmp_path):
    db = CompanionTruthDB(str(tmp_path / "truth.db"))
    with db.conn:
        for pid, uid, role in [
            ("p1", "owner", "OWNER"),
            ("p2", "friend", "GROUP_MEMBER"),
            ("p3", "stranger", "GROUP_MEMBER"),
        ]:
            db.conn.execute(
                "INSERT INTO person_info(person_id,platform_ids,person_name,wechat_remark,trust_role) VALUES(?,?,?,?,?)",
                (pid, json.dumps({"wechat": uid}), uid, "SECRET_REMARK", role),
            )
    return db


def test_A01_identity_cross_check_and_path(tmp_path, monkeypatch):
    db = seed(tmp_path)
    try:
        assert _resolve_trust_role(db, "p1", "friend", "wechat") == "GROUP_MEMBER"
        assert _resolve_trust_role(db, "p1", "owner", "wechat") == "OWNER"
        assert _resolve_trust_role(db, "p2", "owner", "webui") == "GROUP_MEMBER"
        monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(tmp_path / "truth.db"))
        assert resolve_truth_db_path() == str(tmp_path / "truth.db")
        assert "$" not in db._render_twin_and_compute_economy_block(
            is_group_chat=False, sender_trust_role="GROUP_MEMBER"
        )
    finally:
        db.close()


def test_A02_group_and_friend_row_filter(tmp_path):
    db = seed(tmp_path)
    try:
        replace_group_members(db.conn, source="edge", account="a" * 32, group_id="room@chatroom", user_ids=["friend"])
        for group, chat in [(True, "room@chatroom"), (False, "friend")]:
            catalog = db.build_planner_catalog_injection(
                is_group_chat=group,
                sender_trust_role="GROUP_MEMBER",
                source="edge",
                account="a" * 32,
                chat_id=chat,
                requester_user_id="friend",
                requester_platform="wechat",
            )
            assert '"p2" | "friend"' in catalog
            assert '"p3" | "stranger"' not in catalog
            assert "SECRET_REMARK" not in catalog
            assert '"trust_role": "OWNER"' not in catalog
    finally:
        db.close()


@pytest.mark.asyncio
async def test_A03_quick_state_scope_and_visibility(tmp_path):
    db = seed(tmp_path)
    with db.conn:
        db.conn.execute(
            "INSERT INTO user_status(key,current_value,visibility,scope_chat_id) VALUES('foreign','old','PUBLIC','friend')"
        )
        db.conn.execute("INSERT INTO user_status(key,current_value,visibility) VALUES('legacy','old','PUBLIC')")
    db.close()
    await load_reply_greenlights_async(
        is_group_chat=False,
        user_id="owner",
        platform="wechat",
        chat_id="owner",
        db_path=str(tmp_path / "truth.db"),
        memory_greenlights=[],
        file_greenlights=[],
        quick_state_patch=[{"key": "foreign", "value": "new"}, {"key": "legacy", "value": "new"}],
    )
    db = CompanionTruthDB(str(tmp_path / "truth.db"))
    try:
        assert db.conn.execute("SELECT current_value FROM user_status WHERE key='foreign'").fetchone()[0] == "old"
        assert tuple(
            db.conn.execute(
                "SELECT current_value,visibility,scope_chat_id FROM user_status WHERE key='legacy'"
            ).fetchone()
        ) == ("new", "PRIVATE", "owner")
        catalog = db.build_planner_catalog_injection(is_group_chat=False, sender_trust_role="OWNER", chat_id="owner")
        assert '"key": "foreign"' not in catalog
    finally:
        db.close()


def test_A04_private_remark_never_projects():
    profiles = {"friend": {"person_name": "公开名", "wechat_remark": "SECRET_REMARK"}}
    record = {}
    enrich_sender_identity(record, "friend", profiles)
    assert record["sender_cardname"] == "公开名"
    assert "SECRET_REMARK" not in str(record)
    segments = parse_wechat_text_to_raw_segments(
        "@公开名\u2005你好",
        sender_id="owner",
        is_group=True,
        wxid_to_profile=profiles,
        name_to_wxid={"公开名": "friend"},
    )
    assert "SECRET_REMARK" not in str(segments)


def test_A15_guardrail_covers_non_owner_dm(monkeypatch):
    monkeypatch.setenv("MAIBOT_GROUP_SECRET_ANCHORS", "SECRET")
    assert not check_group_outbound_text("SECRET", is_group_chat=True)[0]
    assert not check_group_outbound_text("SECRET", is_group_chat=False, is_owner_private=False)[0]
    assert check_group_outbound_text("SECRET", is_group_chat=False, is_owner_private=True)[0]


@pytest.mark.parametrize("nested", ["refermsg", "recorditem"])
def test_nested_xml_cannot_impersonate_outer_group_author(tmp_path, nested):
    from src.platform_io.wechat.message_ingestor import MessageLedger

    ledger = MessageLedger(str(tmp_path / "ledger.db"))
    try:
        for index, outer in enumerate(["", "<fromusername>friend</fromusername>"], 1):
            xml = f"<msg>{outer}<appmsg><{nested}><fromusername>owner</fromusername><chatusr>owner</chatusr></{nested}></appmsg></msg>"
            ledger.ingest(
                "edge",
                "epoch",
                {
                    "account": "a" * 32,
                    "msg_id": str(index),
                    "chat_id": "room@chatroom",
                    "seq": index,
                    "revision": "r1",
                    "observed_at_ms": 1000,
                    "row": {
                        "msgId": str(index),
                        "talker": "room@chatroom",
                        "isSend": "0",
                        "type": "49",
                        "content": xml,
                    },
                },
            )
        authors = [row[0] for row in ledger.db.execute("SELECT sender_id FROM messages ORDER BY msg_id")]
        assert authors == [None, "friend"]
    finally:
        ledger.db.close()


@pytest.mark.asyncio
async def test_verified_turn_scope_rearbitrates_sender_and_named_slot(tmp_path, monkeypatch):
    import hashlib
    from src.maisaka.reasoning_engine import MaisakaReasoningEngine
    from src.platform_io.wechat.message_ingestor import MessageLedger
    from src.platform_io.wechat.projection import native_message_id
    from src.companion_vault.twin_runtime_router import TwinTurnDecision
    from unittest.mock import AsyncMock
    from pytests.companion.test_phase6_twins_and_wechat_preset import _make_session_message

    db = seed(tmp_path)
    db.close()
    ledger_path = tmp_path / "ledger.db"
    ledger = MessageLedger(str(ledger_path))
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(tmp_path / "truth.db"))
    monkeypatch.setenv("MAIBOT_MESSAGE_LEDGER_PATH", str(ledger_path))
    account = "a" * 32
    stream = SimpleNamespace(
        platform="wechat",
        session_id="session",
        is_group_session=True,
        account_id=account,
        group_id="room@chatroom",
        user_id="",
        scope=hashlib.sha256(b"edge").hexdigest()[:24],
    )
    runtime = SimpleNamespace(chat_stream=stream, session_id="session", _chat_history=[], log_prefix="test")
    engine = MaisakaReasoningEngine(runtime)
    route = AsyncMock(
        side_effect=[
            TwinTurnDecision(recommended_slot="sister_elder"),
            TwinTurnDecision(recommended_slot="sister_younger"),
        ]
    )
    monkeypatch.setattr("src.companion_vault.twin_runtime_router.resolve_turn_route", route)
    try:
        for index, (sender, text) in enumerate([("owner", "姐姐来看"), ("friend", "妹妹来看")], 1):
            ledger.ingest(
                "edge",
                "epoch",
                {
                    "account": account,
                    "msg_id": str(index),
                    "chat_id": stream.group_id,
                    "seq": index,
                    "revision": "r1",
                    "observed_at_ms": 1000,
                    "row": {
                        "msgId": str(index),
                        "talker": stream.group_id,
                        "isSend": "0",
                        "type": "1",
                        "content": sender + ":\n" + text,
                    },
                },
            )
            message = _make_session_message(
                user_id=sender,
                group_id=stream.group_id,
                text=text,
                session_id="session",
                msg_id=native_message_id("edge", account, str(index)),
            )
            message.account_id = account
            decision = await engine._resolve_current_turn_decision(trigger_message=message, source_messages=[message])
            assert not decision.abort_turn
            assert decision.scope.requester_id == sender
            assert decision.scope.source == "edge"
            assert decision.scope.chat_id == stream.group_id
            assert not decision.scope.is_owner_private
            assert decision.recommended_slot == ("sister_elder" if index == 1 else "sister_younger")
        assert route.await_count == 2
        assert route.call_args.kwargs["sender_id"] == "friend"
        message.message_info.user_info.user_id = "owner"
        denied = await engine._resolve_current_turn_decision(trigger_message=message, source_messages=[message])
        assert denied.abort_turn
        assert denied.abort_reason == "invalid_turn_scope"
        assert route.await_count == 2
    finally:
        ledger.db.close()


def test_unknown_platform_binding_and_ambiguous_owner_fail_closed(tmp_path):
    db = seed(tmp_path)
    try:
        with db.conn:
            db.conn.execute("INSERT INTO person_info(person_id,platform_ids,trust_role) VALUES('unbound','{}','OWNER')")
        assert _resolve_trust_role(db, "unbound", "unbound", "wechat") == "GROUP_MEMBER"
        assert _resolve_trust_role(db, "p1", "owner", "unknown") == "GROUP_MEMBER"
        with db.conn:
            db.conn.execute(
                "INSERT INTO person_info(person_id,platform_ids,trust_role) VALUES('duplicate',?,'GROUP_MEMBER')",
                (json.dumps({"wechat": "owner"}),),
            )
        assert _resolve_trust_role(db, "p1", "owner", "wechat") == "GROUP_MEMBER"
    finally:
        db.close()
