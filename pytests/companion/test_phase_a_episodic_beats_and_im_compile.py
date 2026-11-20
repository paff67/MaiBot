"""Phase A IM chronology, frozen registry and revision-gated scene memory."""

from dataclasses import replace
from pathlib import Path
import json
import sqlite3
import pytest

from src.companion_vault.episodic_beats import confirm_beat, read_beats
from src.companion_vault.truth_db import CompanionTruthDB
from src.companion_vault.st_preset_engine import STCompileContext, STHistoryTurn, STPresetEngine
from src.llm_models.payload_content.context_item import FunctionCallOutputItem, get_item_text
from src.platform_io.wechat.message_ingestor import MessageLedger

TEMPLATE = Path(__file__).resolve().parents[2] / "plugins/st_preset_engine/templates/Meowssiah-WeChat-IM.json"


def ledger_with_message(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = MessageLedger(str(path))
    event = {
        "account": "a" * 32,
        "msg_id": "1",
        "chat_id": "friend",
        "seq": 1,
        "revision": "r1",
        "observed_at_ms": 1000,
        "row": {
            "msgId": "1",
            "talker": "friend",
            "isSend": "0",
            "type": "1",
            "content": "约好一起看樱花",
            "createTime": "1000",
            "msgSvrId": "100",
        },
    }
    ledger.ingest("edge", "epoch", event)
    return ledger, path


def test_scene_revision_scope_archive_and_sql_firewall(tmp_path):
    ledger, path = ledger_with_message(tmp_path)
    db = CompanionTruthDB(str(tmp_path / "truth.db"))
    scope = {"source": "edge", "account": "a" * 32, "chat_id": "friend"}
    try:
        with db.conn:
            db.conn.execute("BEGIN IMMEDIATE")
            code = confirm_beat(
                db.conn,
                **scope,
                logical_turn_id="t1",
                persona_slot="sister_elder",
                summary_text="姐姐和熟人约好看樱花",
                sources=[{"msg_id": "1", "revision": "r1"}],
            )
        assert code == "BE000001"
        assert read_beats(db.conn, str(path), **scope)[0]["summary_text"] == "姐姐和熟人约好看樱花"
        assert not read_beats(db.conn, str(path), **{**scope, "chat_id": "other"})
        assert not read_beats(db.conn, str(path), **scope, codes=[])
        assert not read_beats(db.conn, str(path), **scope, active_msg_ids=["1"])
        assert read_beats(db.conn, str(path), **scope, codes=[code])
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute("UPDATE episodic_beats SET visibility='PUBLIC'")
        db.conn.rollback()
        with pytest.raises((ValueError, sqlite3.DatabaseError)):
            db.execute_table_sql("UPDATE episodic_beats SET summary_text='改写'")
        with ledger.db:
            ledger.db.execute("UPDATE messages SET revision='r2' WHERE msg_id='1'")
        assert not read_beats(db.conn, str(path), **scope)
        with ledger.db:
            ledger.db.execute("UPDATE messages SET revision='r1',kind='10000' WHERE msg_id='1'")
        assert not read_beats(db.conn, str(path), **scope)
        assert db.conn.execute("SELECT COUNT(*) FROM person_info").fetchone()[0] == 0
    finally:
        db.close()
        ledger.db.close()


def registry_and_sync(compiled):
    outputs = {
        item.call_id: json.loads(item.output)
        for item in compiled.context_items
        if isinstance(item, FunctionCallOutputItem)
    }
    return outputs["call_init_context_00"], outputs["call_sync_final"]


def test_im_grouping_preserves_bubbles_speakers_and_exact_dedup():
    engine = STPresetEngine.from_file(TEMPLATE)
    history = [
        STHistoryTurn("assistant", "姐姐第一泡", msg_id="a1", persona_slot="sister_elder"),
        STHistoryTurn("assistant", "姐姐第二泡", msg_id="a2", persona_slot="sister_elder"),
        STHistoryTurn("user", "同文", msg_id="u1", speaker_id="f1", speaker_name="甲"),
        STHistoryTurn("user", "同文", msg_id="u2", speaker_id="f2", speaker_name="乙"),
        STHistoryTurn("assistant", "尾部助手气泡", msg_id="a3"),
        STHistoryTurn("user", "本轮目标", msg_id="u3", speaker_name="丙"),
    ]
    compiled = engine.compile_to_context_items(
        STCompileContext(
            protocol_mode="spreset_tool_envelope",
            output_mode="im_clean",
            chat_history=history,
            last_user_message="本轮目标",
            target_msg_ids=["u3"],
        )
    )
    texts = [get_item_text(item) for item in compiled.context_items]
    assistant = next(text for text in texts if "姐姐第一泡" in text)
    assert assistant.index("姐姐第一泡") < assistant.index("姐姐第二泡")
    group = next(text for text in texts if '"msg_id": "u1"' in text)
    assert group.index('"speaker_name": "甲"') < group.index('"speaker_name": "乙"')
    assert group.count("同文") == 2
    assert any("尾部助手气泡" in text for text in texts)
    assert "剧情继续推进" not in json.dumps(texts, ensure_ascii=False)
    _, sync = registry_and_sync(compiled)
    assert sync["user_input"] == "本轮目标"
    assert not any('"msg_id": "u3"' in text for text in texts)


def test_turn_zero_is_frozen_and_recall_lives_in_sync_tail(monkeypatch):
    import src.companion_vault.st_preset_engine as st

    engine = STPresetEngine.from_file(TEMPLATE)
    ctx = STCompileContext(
        protocol_mode="spreset_tool_envelope",
        output_mode="im_clean",
        persona_v2_static="PUBLIC_STATIC",
        user_name="甲",
        last_user_message="FIRST_INPUT",
        persona_v2_dynamic="PRIVATE_DYNAMIC_1",
        director_note="DIRECTOR_1",
        shujuku_recalled="RECALL_WITHOUT_WORLDINFO",
        archived_scenes=[{"beat_id": "BE000001", "summary_text": "过去的约定"}],
    )
    monkeypatch.setattr(st, "_tokyo_time_str", lambda: "TIME_1")
    first = engine.compile_to_context_items(ctx)
    monkeypatch.setattr(st, "_tokyo_time_str", lambda: "TIME_2")
    second = engine.compile_to_context_items(
        replace(
            ctx,
            user_name="乙",
            last_user_message="SECOND_INPUT",
            persona_v2_dynamic="PRIVATE_DYNAMIC_2",
            director_note="DIRECTOR_2",
        )
    )
    first_init, first_sync = registry_and_sync(first)
    second_init, second_sync = registry_and_sync(second)
    assert first_init == second_init
    assert "PUBLIC_STATIC" in first_init["registry"]
    for token in [
        "TIME_1",
        "TIME_2",
        "FIRST_INPUT",
        "SECOND_INPUT",
        "RECALL_WITHOUT_WORLDINFO",
        "PRIVATE_DYNAMIC_1",
        "DIRECTOR_1",
        "过去的约定",
    ]:
        assert token not in first_init["registry"]
    assert first_sync["tokyo_time"] == "TIME_1"
    assert second_sync["tokyo_time"] == "TIME_2"
    assert second_sync["director_note"] == "DIRECTOR_2"
    assert first_sync["recalled_memory"] == "RECALL_WITHOUT_WORLDINFO"
    assert first_sync["archived_scenes"][0]["beat_id"] == "BE000001"


@pytest.mark.asyncio
@pytest.mark.parametrize("codes", [None, [], ["BE000001"]])
async def test_be_query_entry_and_greenlights_survive_restart_and_revoke(tmp_path, monkeypatch, codes):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.companion_vault.runtime_bridge import load_reply_greenlights_async
    from src.core.tooling import ToolInvocation
    from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
    from src.maisaka.builtin_tool.query_memory import handle_tool, memory_service
    from src.platform_io.wechat.projection import native_message_id
    from pytests.companion.test_phase6_twins_and_wechat_preset import _make_session_message

    ledger, path = ledger_with_message(tmp_path)
    truth_path = tmp_path / "truth.db"
    db = CompanionTruthDB(str(truth_path))
    scope = {"source": "edge", "account": "a" * 32, "chat_id": "friend"}
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        confirm_beat(
            db.conn,
            **scope,
            logical_turn_id="t1",
            persona_slot="sister_elder",
            summary_text="第1轮约好一起看樱花",
            sources=[{"msg_id": "1", "revision": "r1"}],
        )
    db.close()
    # Reopen both stores as after a process restart and an outer context compaction.
    ledger.db.close()
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(truth_path))
    monkeypatch.setenv("MAIBOT_MESSAGE_LEDGER_PATH", str(path))
    message = _make_session_message(user_id="friend", msg_id=native_message_id("edge", "a" * 32, "1"), session_id="s")
    message.account_id = "a" * 32
    stream = SimpleNamespace(
        platform="wechat",
        session_id="s",
        account_id="a" * 32,
        scope="",
        is_group_session=False,
        user_id="friend",
        group_id="",
    )
    runtime = SimpleNamespace(chat_stream=stream, session_id="s", _chat_history=[])
    engine = SimpleNamespace(_turn_source_messages=[message])
    ctx = BuiltinToolRuntimeContext(engine, runtime)
    host_search = AsyncMock(side_effect=AssertionError("WeChat must never call host memory_service"))
    monkeypatch.setattr(memory_service, "search", host_search)
    arguments = {"query": "樱花"}
    if codes is not None:
        arguments["codes"] = codes
    result = await handle_tool(ctx, ToolInvocation(tool_name="query_memory", arguments=arguments))
    assert result.success
    assert result.structured_content["episodic_beats"][0]["beat_id"] == "BE000001"
    host_search.assert_not_awaited()
    greenlight, _ = await load_reply_greenlights_async(
        is_group_chat=False,
        user_id="friend",
        platform="wechat",
        memory_greenlights=["BE000001"],
        file_greenlights=[],
        db_path=str(truth_path),
        ledger_path=str(path),
        anchor_message_id=message.message_id,
        account="a" * 32,
        chat_id="friend",
    )
    assert "第1轮约好一起看樱花" in greenlight
    # A later revoke must invalidate both search and direct greenlight reads immediately.
    with sqlite3.connect(path) as ledger_db:
        ledger_db.execute("UPDATE messages SET kind=10000 WHERE msg_id='1'")
    result = await handle_tool(ctx, ToolInvocation(tool_name="query_memory", arguments=arguments))
    assert not result.success or result.structured_content["episodic_beats"] == []
    greenlight, _ = await load_reply_greenlights_async(
        is_group_chat=False,
        user_id="friend",
        platform="wechat",
        memory_greenlights=["BE000001"],
        file_greenlights=[],
        db_path=str(truth_path),
        ledger_path=str(path),
        anchor_message_id=message.message_id,
        account="a" * 32,
        chat_id="friend",
    )
    assert "第1轮约好一起看樱花" not in greenlight


def test_archived_beats_return_after_fifteenth_round(tmp_path):
    ledger, path = ledger_with_message(tmp_path)
    truth = tmp_path / "truth.db"
    db = CompanionTruthDB(str(truth))
    scope = {"source": "edge", "account": "a" * 32, "chat_id": "friend"}
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        confirm_beat(
            db.conn,
            **scope,
            logical_turn_id="first",
            persona_slot="sister_elder",
            summary_text="首轮的约定",
            sources=[{"msg_id": "1", "revision": "r1"}],
        )
    db.close()
    db = CompanionTruthDB(str(truth))
    history = [STHistoryTurn("user", f"第{index}轮", msg_id=str(index)) for index in range(12, 16)]
    scenes = read_beats(db.conn, str(path), **scope, active_msg_ids=[turn.msg_id for turn in history])
    engine = STPresetEngine.from_file(TEMPLATE)
    compiled = engine.compile_to_context_items(
        STCompileContext(
            protocol_mode="spreset_tool_envelope",
            output_mode="im_clean",
            chat_history=history,
            last_user_message="第15轮",
            target_msg_ids=["15"],
            archived_scenes=scenes,
        )
    )
    registry, sync = registry_and_sync(compiled)
    assert "首轮的约定" not in registry["registry"]
    assert sync["archived_scenes"][0]["summary_text"] == "首轮的约定"
    db.close()
    ledger.db.close()


def test_current_target_media_and_history_media_keep_source_order():
    from src.llm_models.payload_content.context_item import ContextFileUriPart, ContextTextPart

    history = [
        STHistoryTurn(
            "user",
            "第一张",
            msg_id="u1",
            speaker_name="甲",
            media_parts=[{"file_uri": "gs://first.png", "mime_type": "image/png"}],
        ),
        STHistoryTurn(
            "user",
            "第二张",
            msg_id="u2",
            speaker_name="乙",
            media_parts=[{"file_uri": "gs://second.png", "mime_type": "image/png"}],
        ),
        STHistoryTurn(
            "user",
            "本轮目标正文",
            msg_id="u3",
            speaker_name="丙",
            media_parts=[{"file_uri": "gs://target.png", "mime_type": "image/png"}],
        ),
    ]
    engine = STPresetEngine.from_file(TEMPLATE)
    compiled = engine.compile_to_context_items(
        STCompileContext(
            protocol_mode="spreset_tool_envelope",
            output_mode="im_clean",
            chat_history=history,
            target_msg_ids=["u3"],
            last_user_message="本轮目标正文",
        )
    )
    sequence = []
    for item in compiled.context_items:
        for part in getattr(item, "parts", ()):
            if isinstance(part, ContextTextPart):
                sequence.append(part.text)
            elif isinstance(part, ContextFileUriPart):
                sequence.append(part.file_uri)
    for mid, uri in [("u1", "gs://first.png"), ("u2", "gs://second.png"), ("u3", "gs://target.png")]:
        position = sequence.index(uri)
        assert f'"msg_id": "{mid}"' in sequence[position - 1]
        assert sequence.count(uri) == 1
    assert sum("本轮目标正文" in text for text in sequence) == 1
    _, sync = registry_and_sync(compiled)
    assert sync["user_input"] == "本轮目标正文"


def test_vault_time_range_filters_scoped_am_source_timestamps(tmp_path):
    from pytests.companion.test_memory_consolidation import setup, sql_output, SOURCE, ACCOUNT, CHAT
    from src.companion_vault.consolidator import read_scoped_memories

    ledger, worker = setup(tmp_path)
    try:
        worker.enqueue()
        assert worker.commit(worker.claim(), sql_output())
        bound = {"source": SOURCE, "account": ACCOUNT, "chat_id": CHAT}
        assert read_scoped_memories(
            str(tmp_path / "truth.db"),
            str(tmp_path / "ledger.db"),
            **bound,
            time_start="2026/01/01",
            time_end="2026/12/31",
        )
        assert not read_scoped_memories(
            str(tmp_path / "truth.db"), str(tmp_path / "ledger.db"), **bound, time_start="2027/01/01"
        )
        assert not read_scoped_memories(
            str(tmp_path / "truth.db"), str(tmp_path / "ledger.db"), **bound, time_end="2025/12/31"
        )
    finally:
        worker.close()
        ledger.db.close()
