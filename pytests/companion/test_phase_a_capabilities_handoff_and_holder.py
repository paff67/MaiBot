"""Phase A capabilities, trusted evidence and confirmed state commits."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import copy
import sqlite3
import pytest

from src.core.tooling import (
    ToolAvailabilityContext,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
    ToolSpec,
)
from src.companion_vault.truth_db import CompanionTruthDB
from src.common.data_models.reply_generation_data_models import ReplyGenerationResult
from src.chat.replyer.maisaka_generator import MaisakaReplyGenerator


class Provider:
    provider_name = "test"
    provider_type = "builtin"

    def __init__(self):
        self.specs = [
            ToolSpec(
                name=name,
                parameters_schema={
                    "properties": {"attach_pic": {}, "attach_at": {}, "attach_emoji": {}, "reply_reference": {}},
                    "required": ["attach_pic", "reply_reference"],
                },
            )
            for name in ["reply", "send_emoji", "phone_action", "view_forward_message"]
        ]
        self.calls = []

    async def list_tools(self, _context=None):
        return self.specs

    async def invoke(self, invocation, _context=None):
        self.calls.append(invocation)
        return ToolExecutionResult(tool_name=invocation.tool_name, success=True)


@pytest.mark.asyncio
async def test_channel_projection_and_execution_are_consistent():
    registry = ToolRegistry()
    provider = Provider()
    registry.register_provider(provider)
    specs = await registry.list_tools(ToolAvailabilityContext(platform="wechat"))
    assert [s.name for s in specs] == ["reply"]
    schema = specs[0].parameters_schema
    assert "attach_pic" not in schema["properties"]
    assert "attach_emoji" not in schema["properties"]
    assert "attach_at" in schema["properties"]
    assert schema["required"] == ["reply_reference"]
    assert "attach_pic" in provider.specs[0].parameters_schema["properties"]
    assert len(await registry.list_tools(ToolAvailabilityContext(platform="qq"))) == 4
    for invocation in [
        ToolInvocation(tool_name="phone_action"),
        ToolInvocation(tool_name="reply", arguments={"attach_pic": ["x"]}),
    ]:
        assert not (await registry.invoke(invocation, ToolExecutionContext(platform="wechat"))).success
    assert provider.calls == []
    enabled = await registry.list_tools(
        ToolAvailabilityContext(platform="wechat", channel_capabilities={"read_forward_text": True})
    )
    assert "view_forward_message" in [s.name for s in enabled]


def pending_result(path, logical_id="turn", slot="sister_elder"):
    result = ReplyGenerationResult()
    result.pending_side_effects = {
        "delivery": {
            "db_path": str(path),
            "session_id": "session",
            "logical_turn_id": logical_id,
            "persona_slot": slot,
            "update_holder": True,
        }
    }
    return result


def test_confirmed_holder_commit_is_durable_and_idempotent(tmp_path):
    path = tmp_path / "truth.db"
    db = CompanionTruthDB(str(path))
    db.close()
    result = pending_result(path)
    duplicate = copy.deepcopy(result)
    replyer = object.__new__(MaisakaReplyGenerator)
    assert replyer.commit_successful_delivery(result)
    assert not replyer.commit_successful_delivery(duplicate)
    db = CompanionTruthDB(str(path))
    try:
        assert (
            db.conn.execute("SELECT active_persona_slot FROM global_state WHERE row_id=1").fetchone()[0]
            == "sister_elder"
        )
        assert db.conn.execute("SELECT COUNT(*) FROM confirmed_reply_turns").fetchone()[0] == 1
        assert (
            db.conn.execute("SELECT persona_slot FROM session_speakers WHERE session_id='session'").fetchone()[0]
            == "sister_elder"
        )
    finally:
        db.close()


def test_failed_commit_rolls_back_idempotency_and_mvu(tmp_path, monkeypatch):
    from src.companion_vault.mvu_engine import MVURuntimeEngine
    from src.companion_vault.persona_v2_engine import get_persona_v2_manager

    path = tmp_path / "truth.db"
    db = CompanionTruthDB(str(path))
    db.close()
    result = pending_result(path)
    result.pending_side_effects["mvu"] = {"card_id": "maibot_xuyan"}
    assert get_persona_v2_manager().get_card("maibot_xuyan") is not None

    def fail(db, *args, **kwargs):
        db.conn.execute("INSERT INTO user_status(key,current_value) VALUES('partial','bad')")
        raise RuntimeError("Injected transaction failure")

    monkeypatch.setattr(MVURuntimeEngine, "apply_turn_updates", fail)
    with pytest.raises(RuntimeError):
        object.__new__(MaisakaReplyGenerator).commit_successful_delivery(result)
    db = CompanionTruthDB(str(path))
    try:
        assert db.conn.execute("SELECT COUNT(*) FROM confirmed_reply_turns").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM user_status WHERE key='partial'").fetchone()[0] == 0
        assert result.pending_side_effects
    finally:
        db.close()


def test_metering_preserves_confirmed_holder(tmp_path):
    from src.companion_vault.compute_ledger import ComputeLedger

    path = tmp_path / "truth.db"
    ledger = ComputeLedger(truth_db_path=str(path))
    ledger._persist_snapshot(ledger.get_snapshot())
    replyer = object.__new__(MaisakaReplyGenerator)
    assert replyer.commit_successful_delivery(pending_result(path))
    ledger.record_local_cost(0.01)
    db = CompanionTruthDB(str(path))
    try:
        assert db.conn.execute("SELECT active_persona_slot FROM global_state").fetchone()[0] == "sister_elder"
    finally:
        db.close()


def forward_xml(text="你好 https://cdn.example/a?token=SECRET&auth=KEY"):
    import html

    record = (
        '<recordinfo><datalist><dataitem datatype="1" dataid="1"><sourcename>公开名</sourcename><datadesc>'
        + html.escape(text)
        + "</datadesc></dataitem></datalist></recordinfo>"
    )
    return "<msg><appmsg><type>19</type><recorditem><![CDATA[" + record + "]]></recorditem></appmsg></msg>"


def test_forward19_secure_parser_and_projection_gate():
    import json
    from src.platform_io.wechat.message_quotes import parse_forward_recorditem
    from src.platform_io.wechat.projection import project_message
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    from src.common.data_models.message_component_data_model import ForwardNodeComponent

    xml = forward_xml()
    nodes = parse_forward_recorditem(xml)
    assert nodes[0]["user_nickname"] == "公开名"
    assert nodes[0]["content"][0]["data"] == "你好 https://cdn.example/a"
    with pytest.raises(ValueError):
        parse_forward_recorditem('<!DOCTYPE foo [<!ENTITY x SYSTEM "file:///etc/passwd">]><recordinfo/>')
    with pytest.raises(ValueError):
        parse_forward_recorditem("x" * 262145)
    with pytest.raises(ValueError):
        parse_forward_recorditem("<recordinfo>" + "<dataitem/>" * 65 + "</recordinfo>")
    record = {
        "source": "edge",
        "account": "a" * 32,
        "msg_id": "1",
        "chat_id": "room@chatroom",
        "direction": "incoming",
        "sender_id": "friend",
        "row_json": json.dumps(
            {"msgId": "1", "talker": "room@chatroom", "type": "49", "content": xml, "createTime": "1000"}
        ),
        "body": xml,
        "revision": "r1",
    }
    disabled = project_message(record)
    assert disabled["raw_message"][0]["type"] == "text"
    enabled = project_message(record, channel_capabilities={"read_forward_text": True})
    sequence = PluginMessageUtils._message_sequence_from_dict(enabled["raw_message"])
    assert isinstance(sequence.components[0], ForwardNodeComponent)
    assert sequence.components[0].forward_components[0].content[0].text == "你好 https://cdn.example/a"
    empty = {
        **record,
        "row_json": json.dumps(
            {
                "msgId": "1",
                "talker": "room@chatroom",
                "type": "49",
                "content": "<msg><appmsg><type>19</type></appmsg></msg>",
                "createTime": "1000",
            }
        ),
    }
    assert (
        "不可用" in project_message(empty, channel_capabilities={"read_forward_text": True})["raw_message"][0]["data"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_sender,expected_prefix", [("friend", ""), ("other", "@公开名 ")])
async def test_native_quote_and_public_text_mention(tmp_path, monkeypatch, quote_sender, expected_prefix):
    import json
    from src.companion_vault.access_policy import replace_group_members
    from src.common.data_models.message_component_data_model import (
        AtComponent,
        MessageSequence,
        ReplyComponent,
        TextComponent,
    )
    from src.platform_io.wechat.driver import WeChatRootDriver
    from src.platform_io.wechat.message_ingestor import MessageLedger
    from src.platform_io.wechat.projection import native_message_id
    from src.platform_io.types import DeliveryStatus

    ledger = MessageLedger(str(tmp_path / "ledger.db"))
    account = "a" * 32
    chat = "room@chatroom"
    ledger.ingest(
        "edge",
        "epoch",
        {
            "account": account,
            "msg_id": "1",
            "chat_id": chat,
            "seq": 1,
            "revision": "r1",
            "observed_at_ms": 1000,
            "row": {
                "msgId": "1",
                "talker": chat,
                "isSend": "0",
                "type": "1",
                "content": quote_sender + ":\n原文",
                "createTime": "1000",
                "msgSvrId": "100",
            },
        },
    )
    truth = CompanionTruthDB(str(tmp_path / "truth.db"))
    with truth.conn:
        truth.conn.execute(
            "INSERT INTO person_info(person_id,platform_ids,person_name,wechat_remark) VALUES(?,?,?,?)",
            ("p", json.dumps({"wechat": "friend"}), "公开名", "SECRET_REMARK"),
        )
    replace_group_members(truth.conn, source="edge", account=account, group_id=chat, user_ids=["friend", "other"])
    truth.close()
    edge = SimpleNamespace(
        source="edge",
        account=account,
        send_text=AsyncMock(return_value={"status": "confirmed", "msg_id": "900"}),
        send_quote=AsyncMock(return_value={"status": "confirmed", "msg_id": "900"}),
    )
    driver = WeChatRootDriver(
        edge, send_enabled=True, ledger_path=str(tmp_path / "ledger.db"), truth_path=str(tmp_path / "truth.db")
    )
    message = SimpleNamespace(
        message_id="out1",
        message_info=SimpleNamespace(group_info=SimpleNamespace(group_id=chat), additional_config={}, user_info=None),
        raw_message=MessageSequence(
            [
                AtComponent("friend", target_user_cardname="SECRET_REMARK"),
                AtComponent("friend"),
                ReplyComponent(native_message_id("edge", account, "1")),
                TextComponent("收到"),
            ]
        ),
    )
    receipt = await driver.send_message(message, driver.descriptor.route_key)
    assert receipt.status == DeliveryStatus.SENT
    edge.send_quote.assert_awaited_once()
    args = edge.send_quote.call_args.args
    assert args[1] == expected_prefix + "收到"
    assert "SECRET_REMARK" not in str(args)
    assert not driver._resolve_quote_target(ReplyComponent(native_message_id("other-edge", account, "1")), chat)
    with ledger.db:
        ledger.db.execute("UPDATE messages SET kind=10000 WHERE msg_id='1'")
    assert not driver._resolve_quote_target(ReplyComponent("1"), chat)
    ledger.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_mode", ["failed", "success", "partial"])
async def test_execute_reply_commits_mvu_summary_and_holder_only_after_delivery(tmp_path, monkeypatch, delivery_mode):
    delivered = delivery_mode != "failed"
    from src.common.data_models.llm_service_data_models import LLMResponseResult
    from src.companion_vault.episodic_beats import read_beats
    from src.companion_vault.persona_v2_engine import get_persona_v2_manager
    from src.companion_vault.mvu_engine import MVURuntimeEngine
    from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
    from src.maisaka.builtin_tool.reply import execute_reply
    from src.maisaka.context.messages import SessionBackedMessage
    from src.platform_io.wechat.message_ingestor import MessageLedger
    from src.platform_io.wechat.projection import native_message_id
    from pytests.companion.test_phase6_twins_and_wechat_preset import _setup_one_pass_st_harness

    response = LLMResponseResult.from_portable_output(
        response=(
            "<game>收到啦</game><summary>姐姐记下周末约定</summary>"
            "<UpdateVariable><Analysis>互动</Analysis><JSONPatch>"
            '[{"op":"delta","path":"/affection","value":3}]</JSONPatch></UpdateVariable>'
        )
    )
    engine, runtime, old, target, calls, sent, states = _setup_one_pass_st_harness(
        tmp_path, monkeypatch, is_group_chat=False, llm_responses=[response]
    )
    from src.config.config import global_config

    elder_profile = next(profile for profile in global_config.personality.profiles if profile.name == "姐姐")
    monkeypatch.setattr(elder_profile, "persona_v2_card_id", "maibot_xuyue")
    account = "a" * 32
    ledger_path = tmp_path / "ledger.db"
    monkeypatch.setenv("MAIBOT_MESSAGE_LEDGER_PATH", str(ledger_path))
    ledger = MessageLedger(str(ledger_path))
    for index, message in enumerate([old, target], 1):
        message.account_id = account
        message.message_id = native_message_id("edge", account, str(index))
        ledger.ingest(
            "edge",
            "epoch",
            {
                "account": account,
                "msg_id": str(index),
                "chat_id": "wxid_owner",
                "seq": index,
                "revision": "r1",
                "observed_at_ms": 1000,
                "row": {
                    "msgId": str(index),
                    "talker": "wxid_owner",
                    "isSend": "0",
                    "type": "1",
                    "content": message.processed_plain_text,
                    "createTime": "1000",
                    "msgSvrId": str(100 + index),
                },
            },
        )
    runtime.chat_stream.account_id = account
    runtime._chat_history = [
        SessionBackedMessage.from_session_message(
            message, raw_message=message.raw_message, visible_text=message.processed_plain_text, source_kind="user"
        )
        for message in [old, target]
    ]
    engine._turn_source_messages = [old, target]
    engine._active_logical_turn_id = "confirmed_turn"
    card = get_persona_v2_manager().get_card("maibot_xuyue")
    assert card is not None
    db_path = tmp_path / "truth.db"
    db = CompanionTruthDB(str(db_path))
    db.ensure_mvu_defaults(card.card_id, MVURuntimeEngine._schema_defs_for_db(card))
    db.upsert_mvu_variable(
        card.card_id,
        scope_key="OWNER_PRIVATE",
        var_key="affection",
        var_type="number",
        var_value=25,
        visibility="PRIVATE",
    )
    db.close()

    send_count = 0

    async def send(**kwargs):
        nonlocal send_count
        send_count += 1
        if delivery_mode == "partial" and send_count == 2:
            raise RuntimeError("Injected second bubble delivery failure")
        during = CompanionTruthDB(str(db_path))
        try:
            assert MVURuntimeEngine.load_turn_variables(during, card, is_owner_private=True)["affection"] == 25
            assert during.conn.execute("SELECT COUNT(*) FROM episodic_beats").fetchone()[0] == 0
        finally:
            during.close()
        return SimpleNamespace(message_id="confirmed_1") if delivered else None

    monkeypatch.setattr("src.maisaka.builtin_tool.reply.send_service._send_to_target_with_message", send)
    if delivery_mode == "partial":
        from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
        from src.maisaka.builtin_tool.context import PostProcessedReplyMessage

        monkeypatch.setattr(
            BuiltinToolRuntimeContext,
            "post_process_reply_message_items_async",
            AsyncMock(
                return_value=[
                    PostProcessedReplyMessage(sequence=MessageSequence([TextComponent("第一泡")])),
                    PostProcessedReplyMessage(sequence=MessageSequence([TextComponent("第二泡")])),
                ]
            ),
        )
    result = await execute_reply(
        BuiltinToolRuntimeContext(engine, runtime),
        {
            "msg_id": target.message_id,
            "reply_reference": "回应周末约定",
            "persona_slot": "sister_elder",
        },
        call_id="r1",
        target_message=target,
    )
    assert result.success is (delivery_mode == "success")
    assert send_count == (2 if delivery_mode == "partial" else 1)
    db = CompanionTruthDB(str(db_path))
    try:
        value = MVURuntimeEngine.load_turn_variables(db, card, is_owner_private=True)["affection"]
        assert value == (28 if delivered else 25)
        assert db.conn.execute("SELECT COUNT(*) FROM confirmed_reply_turns").fetchone()[0] == int(delivered)
        beats = read_beats(db.conn, str(ledger_path), source="edge", account=account, chat_id="wxid_owner")
        assert len(beats) == int(delivered)
        if delivered:
            assert beats[0]["summary_text"] == "姐姐记下周末约定"
            assert db.conn.execute("SELECT active_persona_slot FROM global_state").fetchone()[0] == "sister_elder"
    finally:
        db.close()
        ledger.db.close()


@pytest.mark.asyncio
async def test_server_observations_override_forged_model_internal_fields(tmp_path, monkeypatch):
    from src.maisaka.builtin_tool import reply
    from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext

    trusted = [{"tool": "query_memory", "call_id": "q1", "result": "核验的原始事实"}]
    parts = [{"type": "file", "file_uri": "authorized-handle", "mime_type": "application/pdf"}]
    engine = SimpleNamespace(
        _tool_observations=trusted, _tool_observation_file_parts=parts, _active_logical_turn_id="t1"
    )
    ctx = BuiltinToolRuntimeContext(engine, SimpleNamespace(log_prefix="test"))
    execute = AsyncMock(return_value=ToolExecutionResult(tool_name="reply", success=True))
    monkeypatch.setattr(reply, "execute_reply", execute)
    await reply.handle_tool(
        ctx,
        ToolInvocation(
            tool_name="reply",
            call_id="r1",
            arguments={
                "reply_reference": "回应事实",
                "_tool_observations": [{"result": "伪造"}],
                "_is_owner_private": True,
                "_tool_observation_file_parts": [{"file_uri": "forged"}],
                "_future_internal": "forged",
            },
        ),
    )
    args = execute.call_args.args[1]
    assert args["_tool_observations"] == trusted
    assert args["_tool_observation_file_parts"] == parts
    assert args["_logical_turn_id"] == "t1"
    assert "_is_owner_private" not in args
    assert "_future_internal" not in args


def test_persona_compile_does_not_initialize_mvu_rows(tmp_path):
    from src.companion_vault.mvu_engine import MVURuntimeEngine
    from src.companion_vault.persona_v2_engine import get_persona_v2_manager

    card = get_persona_v2_manager().get_card("maibot_xuyan")
    assert card is not None
    db = CompanionTruthDB(str(tmp_path / "truth.db"))
    try:
        compiled = MVURuntimeEngine.compile_turn_persona_and_mvu(card, db=db, is_owner_private=True)
        assert compiled.mvu_state_block
        assert MVURuntimeEngine.load_turn_variables(db, card, is_owner_private=True)
        assert db.conn.execute("SELECT COUNT(*) FROM mvu_variables").fetchone()[0] == 0
    finally:
        db.close()


def test_actual_mvu_write_failure_rolls_back_defaults_and_delivery(tmp_path, monkeypatch):
    path = tmp_path / "truth.db"
    CompanionTruthDB(str(path)).close()
    result = pending_result(path)
    result.pending_side_effects["mvu"] = {
        "card_id": "maibot_xuyan",
        "is_owner_private": True,
        "ops": [{"op": "delta", "key": "affection", "value": 3}],
    }

    def fail_write(self, *args, **kwargs):
        raise sqlite3.OperationalError("Injected MVU write failure")

    monkeypatch.setattr(CompanionTruthDB, "upsert_mvu_variable", fail_write)
    with pytest.raises(sqlite3.OperationalError, match="Injected MVU"):
        object.__new__(MaisakaReplyGenerator).commit_successful_delivery(result)
    db = CompanionTruthDB(str(path))
    try:
        assert db.conn.execute("SELECT COUNT(*) FROM confirmed_reply_turns").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM mvu_variables").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM session_speakers").fetchone()[0] == 0
        assert result.pending_side_effects
    finally:
        db.close()


def test_forward19_nested_cdata_and_depth_bound():
    import html
    from src.platform_io.wechat.message_quotes import parse_forward_recorditem

    def wrap(inner):
        return (
            '<recordinfo><datalist><dataitem datatype="19"><recorditem>'
            + html.escape(inner)
            + "</recorditem></dataitem></datalist></recordinfo>"
        )

    inner = (
        '<recordinfo><datalist><dataitem datatype="1"><datadesc>嵌套原话</datadesc></dataitem></datalist></recordinfo>'
    )
    nested = wrap(inner)
    nodes = parse_forward_recorditem(nested)
    assert nodes[0]["content"][0]["data"][0]["content"][0]["data"] == "嵌套原话"
    assert parse_forward_recorditem(wrap(wrap(wrap(inner))))
    with pytest.raises(ValueError, match="nesting|depth"):
        parse_forward_recorditem(wrap(wrap(wrap(wrap(inner)))))
    with pytest.raises(ValueError, match="Unsafe"):
        parse_forward_recorditem(wrap('<!DOCTYPE foo [<!ENTITY x "bad">]><recordinfo/>'))
    with pytest.raises(ValueError, match="depth"):
        parse_forward_recorditem("<recordinfo><a><b><c><d/></c></b></a></recordinfo>")


def test_late_old_delivery_cannot_replace_confirmed_holder(tmp_path):
    path = tmp_path / "truth.db"
    CompanionTruthDB(str(path)).close()
    old = pending_result(path)
    old.pending_side_effects["delivery"].update(logical_turn_id="old", turn_order=10)
    latest = pending_result(path)
    latest.pending_side_effects["delivery"].update(
        logical_turn_id="latest",
        turn_order=20,
        persona_slot="sister_younger",
    )
    replyer = object.__new__(MaisakaReplyGenerator)
    assert replyer.commit_successful_delivery(latest)
    assert replyer.commit_successful_delivery(old)
    db = CompanionTruthDB(str(path))
    try:
        holder = db.conn.execute("SELECT active_persona_slot,last_confirmed_reply_order FROM global_state").fetchone()
        assert tuple(holder) == ("sister_younger", 20)
        speaker = db.conn.execute("SELECT persona_slot,logical_turn_id FROM session_speakers").fetchone()
        assert tuple(speaker) == ("sister_younger", "latest")
        assert db.conn.execute("SELECT COUNT(*) FROM confirmed_reply_turns").fetchone()[0] == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_multihop_tool_results_reach_real_replyer_sync_payload(tmp_path, monkeypatch):
    import json
    from src.common.data_models.llm_service_data_models import LLMResponseResult
    from src.llm_models.payload_content.context_item import ContextFileUriPart, FunctionCallOutputItem
    from src.llm_models.payload_content.tool_option import ToolCall
    from src.maisaka.reasoning_engine import CycleRuntimeState
    from pytests.companion.test_phase6_twins_and_wechat_preset import _setup_one_pass_st_harness

    class EvidenceProvider:
        provider_name = "evidence"
        provider_type = "builtin"

        def __init__(self):
            self.calls = []

        async def list_tools(self, context=None):
            return [ToolSpec(name=name) for name in ["lookup_first", "lookup_second"]]

        async def invoke(self, invocation, context=None):
            self.calls.append(invocation.tool_name)
            index = len(self.calls)
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=True,
                structured_content={"fact": f"原始事实{index}", "api_key": "SECRET"},
                metadata={
                    "_multimodal_parts": [{"file_uri": "gs://authorized/fact.pdf", "mime_type": "application/pdf"}]
                }
                if index == 2
                else {},
            )

    provider = EvidenceProvider()
    response = LLMResponseResult.from_portable_output(response="<game>已根据两次检索回答</game>")
    engine, runtime, _, target, llm_calls, sent, _ = _setup_one_pass_st_harness(
        tmp_path,
        monkeypatch,
        is_group_chat=False,
        llm_responses=[response],
        extra_tools=[provider],
    )
    engine._turn_passive_replyer = False
    engine._active_logical_turn_id = "multi_hop"
    await engine._handle_tool_calls(
        [
            ToolCall(call_id="q1", func_name="lookup_first", args={}),
            ToolCall(call_id="q2", func_name="lookup_second", args={}),
            ToolCall(
                call_id="r",
                func_name="reply",
                args={"msg_id": target.message_id, "reply_reference": "依据已核验事实回应"},
            ),
        ],
        "",
        SimpleNamespace(),
        CycleRuntimeState(),
    )
    assert provider.calls == ["lookup_first", "lookup_second"]
    assert len(sent) == 1
    items = llm_calls[0]["context_items"]
    sync = next(
        json.loads(item.output)
        for item in items
        if isinstance(item, FunctionCallOutputItem) and item.call_id == "call_sync_final"
    )
    observations = sync["tool_observations"]
    assert [row["call_id"] for row in observations] == ["q1", "q2"]
    assert [json.loads(row["result"])["fact"] for row in observations] == ["原始事实1", "原始事实2"]
    assert "SECRET" not in json.dumps(observations)
    assert any(
        isinstance(part, ContextFileUriPart) and part.file_uri == "gs://authorized/fact.pdf"
        for item in items
        for part in getattr(item, "parts", ())
    )
