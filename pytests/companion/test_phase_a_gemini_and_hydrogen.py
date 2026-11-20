"""Phase A protocol acceptance, including capture at the Gemini SDK boundary."""

from types import SimpleNamespace
import pytest

from src.chat.replyer.maisaka_generator import MaisakaReplyGenerator
from src.companion_vault.st_preset_engine import STPresetEngine
from src.llm_models.model_client.gemini_client import GeminiClient
from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType
from src.services.llm_service import LLMServiceClient
from pytests.companion.test_one_pass_st_empty_tool_args_regression import (
    _isolate_runtime_side_effects,
    _make_session_message,
    _make_st_preset_manager,
    _register_fake_client,
    _setup_sequential_model_config,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_envelope", [False, True])
async def test_A05_shared_hydrogen_sdk_wire_isolation(tmp_path, monkeypatch, empty_envelope):
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    _make_st_preset_manager(tmp_path, monkeypatch)
    cfg = _setup_sequential_model_config(monkeypatch, primary_client_type="gemini", backup_client_type="gemini")
    hydrogen = cfg.models[0]
    hydrogen.extra_params = {"thinking_level": "LOW", "temperature": 0.3}
    # Both logical tasks bind to this single model object.
    cfg.model_task_config.planner_flash.model_list = [hydrogen.name]
    cfg.model_task_config.replyer.model_list = [hydrogen.name]
    captured = []

    async def generate_content(**kwargs):
        captured.append(kwargs)
        part = SimpleNamespace(text="收到啦", thought=False, function_call=None, thought_signature=None)
        parts = [part]
        functions = []
        if empty_envelope and len(captured) == 2:
            function = SimpleNamespace(name="soliumbra_reply", args={})
            functions.append(function)
            parts.append(SimpleNamespace(text=None, thought=False, function_call=function, thought_signature=None))
        return SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts), finish_reason="STOP")],
            text="收到啦",
            function_calls=functions,
            usage_metadata=SimpleNamespace(
                prompt_token_count=10, candidates_token_count=3, thoughts_token_count=0, total_token_count=13
            ),
        )

    client = object.__new__(GeminiClient)
    client.api_provider = cfg.api_providers[0]
    client.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    _register_fake_client(monkeypatch, handlers={"prov_primary": client})
    planner = LLMServiceClient(task_name="planner_flash", request_type="maisaka.planner")
    items = [ContextItemBuilder().set_role(RoleType.User).add_text_content("判断是否回复").build()]
    await planner.generate_response_with_context(context_factory=lambda _c: items)
    stream = SimpleNamespace(
        session_id="wire", platform="wechat", is_group_session=False, group_id="", user_id="friend"
    )
    replyer = MaisakaReplyGenerator(chat_stream=stream)
    monkeypatch.setattr(
        "src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info", lambda _: (False, None)
    )
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(tmp_path / "truth.db"))
    await replyer.run_st_single_turn(
        stream_id="wire", reply_message=_make_session_message(session_id="wire", user_id="friend"), chat_history=[]
    )
    await planner.generate_response_with_context(context_factory=lambda _c: items)
    assert len(captured) == 3
    first, st, last = [c["config"].model_dump(exclude_none=True) for c in captured]
    assert first["thinking_config"]["thinking_level"] == "LOW"
    assert last["thinking_config"]["thinking_level"] == "LOW"
    assert st["thinking_config"]["thinking_level"] == "MINIMAL"
    assert st["top_p"] == 0.98
    assert st["top_k"] == 64
    assert st["temperature"] == 1.09
    assert st["max_output_tokens"] == 65535
    declaration = next(
        d for t in st["tools"] for d in t.get("function_declarations", []) if d["name"] == "soliumbra_reply"
    )
    schema = declaration.get("parameters_json_schema") or declaration.get("parameters")
    assert schema.get("propertyOrdering", schema.get("property_ordering")) == list(schema["properties"])
    assert hydrogen.extra_params["thinking_level"] == "LOW"


def test_A06_thinking_zero_and_reserved_parameters():
    assert GeminiClient.resolve_thinking_level({"thinking_level": "0"}).value == "MINIMAL"
    client = object.__new__(GeminiClient)
    config = client._build_generation_config(
        model_identifier="hydrogen",
        system_instruction=None,
        tool_options=None,
        response_format=None,
        max_tokens=77,
        temperature=None,
        extra_params={"temperature": 99, "max_output_tokens": 999, "thinking_level": "0"},
        enable_google_search=False,
    )
    assert config.temperature is None
    assert config.max_output_tokens == 77
    assert config.thinking_config.thinking_level.value == "MINIMAL"


@pytest.mark.parametrize("text", ["<game>一</game><game>二</game>", "你好"])
def test_A07_clean_visible_output(text):
    out = STPresetEngine.unwrap_response(response_text=text, output_mode="im_clean")
    assert out.visible_text == ("一\n\n二" if text.startswith("<game>") else text)
    assert not STPresetEngine.evaluate_wechat_direct_reply(response_text="<game>残缺").is_valid
    assert not STPresetEngine.unwrap_response(
        tool_calls=[{"name": "soliumbra_reply", "args": {"json_patch": []}}], output_mode="im_clean"
    ).visible_text
    assert (
        STPresetEngine.unwrap_response(
            response_text="有效正文", tool_calls=[{"name": "soliumbra_reply", "args": {}}], output_mode="im_clean"
        ).visible_text
        == "有效正文"
    )


@pytest.mark.asyncio
async def test_A13_planner_prose_gets_exactly_one_correction():
    from unittest.mock import AsyncMock
    from src.maisaka.reasoning_engine import MaisakaReasoningEngine, CycleRuntimeState
    from src.maisaka.chat_loop_service import ChatResponse
    from src.common.data_models.llm_service_data_models import LLMResponseResult

    runtime = SimpleNamespace(
        log_prefix="test",
        _chat_history=[],
        _end_planner_continuation=lambda: None,
        _reset_consecutive_wait_count=lambda _: None,
        _enter_stop_state=lambda: None,
    )
    engine = MaisakaReasoningEngine(runtime)
    engine._turn_passive_replyer = False
    engine._emit_planner_progress = AsyncMock()
    engine._should_replace_reasoning = lambda _: False
    engine._reply_from_planner_content = AsyncMock()

    def response():
        items = LLMResponseResult.from_portable_output(response="我想先观察一下大家").output_items
        return ChatResponse(
            output_items=items,
            request_messages=[],
            selected_history_count=0,
            tool_count=0,
            prompt_tokens=0,
            built_message_count=0,
            completion_tokens=0,
            total_tokens=0,
        )

    state = CycleRuntimeState(source_messages=[SimpleNamespace()], trigger_message=SimpleNamespace())

    async def retry(**kwargs):
        assert kwargs["correction_retry"] is True
        state.response = response()

    engine._run_planner_request = AsyncMock(side_effect=retry)
    assert await engine._handle_planner_response_actions(
        response=response(), cycle_detail=SimpleNamespace(time_records={}), state=state, planner_extra_lines=[]
    )
    assert engine._run_planner_request.await_count == 1
    assert state.cycle_end.reason == "planner_no_tool_failed"
    engine._reply_from_planner_content.assert_not_awaited()


def test_A08_monitor_live_and_replay_four_fields(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from sqlmodel import Session, create_engine
    from src.common.database.database_model import MaisakaMonitorEventRecord
    from src.maisaka.monitor import event_store
    from src.maisaka.monitor.events import _serialize_tool_results
    from src.maisaka.monitor.planner_delta import PlannerDeltaEncoder

    engine = create_engine("sqlite:///" + str(tmp_path / "monitor.db"))
    MaisakaMonitorEventRecord.__table__.create(engine)

    @contextmanager
    def sessions(**_):
        with Session(engine, expire_on_commit=False) as session:
            yield session
            session.commit()

    monkeypatch.setattr(event_store, "get_db_session", sessions)
    telemetry = {"thinking": "想法", "summary": "场记", "comment": "留言", "mvu_watch": {"status": "pending_delivery"}}
    payload = {
        "session_id": "s",
        "run_id": "r",
        "cycle_id": 1,
        "tools": _serialize_tool_results([{"tool_name": "reply", "success": True, "st_telemetry": telemetry}]),
    }
    saved = event_store.record_monitor_event("planner.finalized", payload)
    live = PlannerDeltaEncoder().encode({"domain": "maisaka_monitor", "event": "planner.finalized", "data": saved})
    replay = event_store.replay_monitor_events()
    for observed in [live["data"], replay[0]["data"]]:
        assert observed["tools"][0]["st_telemetry"] == telemetry


@pytest.mark.asyncio
async def test_steering_interrupt_limit_finishes_one_planner_request():
    from src.maisaka.reasoning_engine import MaisakaReasoningEngine
    from unittest.mock import AsyncMock

    step = AsyncMock(return_value="finished")
    runtime = SimpleNamespace(
        _chat_loop_service=SimpleNamespace(chat_loop_step=step), _chat_history=[], _max_context_size=50
    )
    engine = MaisakaReasoningEngine(runtime)
    engine._turn_interrupt_count = 5
    engine._active_logical_turn_id = "turn"
    assert (
        await engine._run_interruptible_planner(
            injected_user_messages=[], tail_user_messages=[], tool_definitions=[], task_override="planner_flash"
        )
        == "finished"
    )
    step.assert_awaited_once()
    assert step.call_args.kwargs["logical_turn_id"] == "turn"


@pytest.mark.parametrize(
    "body",
    ["<game>残缺", "你好<summary>不应外泄", "<thinking>机密", "<game>一</game><game", "<GAME>一</game><summary>缺口"],
)
def test_partial_internal_tags_fail_closed(body):
    assert not STPresetEngine.evaluate_wechat_direct_reply(response_text=body).is_valid


@pytest.mark.asyncio
async def test_cached_replyer_compiles_concurrent_requests_in_isolation(tmp_path, monkeypatch):
    import asyncio
    from src.common.data_models.llm_service_data_models import LLMResponseResult
    from src.llm_models.payload_content.context_item import FunctionCallOutputItem
    from pytests.companion.test_phase6_twins_and_wechat_preset import _setup_one_pass_st_harness
    import json

    _, runtime, _, target, _, _, _ = _setup_one_pass_st_harness(
        tmp_path,
        monkeypatch,
        is_group_chat=False,
        llm_responses=[],
    )
    from src.chat.replyer.replyer_manager import replyer_manager
    from src.config.config import global_config

    profiles = global_config.personality.profiles
    for profile, card_id in zip(profiles, ["maibot_xuyan", "maibot_xuyue"], strict=True):
        monkeypatch.setattr(profile, "persona_v2_card_id", card_id)
    generator = replyer_manager.get_replyer(chat_stream=runtime.chat_stream, replyer_type="maisaka")
    generator._reset_st_request_state()
    both_started = asyncio.Event()
    captured = []

    class ConcurrentClient:
        task_name = "replyer"

        async def generate_response_with_context(self, *, context_factory, options):
            items = await context_factory(SimpleNamespace(api_provider=SimpleNamespace(client_type="gemini")))
            payload = next(
                json.loads(item.output)
                for item in items
                if isinstance(item, FunctionCallOutputItem) and item.call_id == "call_sync_final"
            )
            captured.append(payload)
            if len(captured) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=5)
            return LLMResponseResult.from_portable_output(response="<game>收到</game>")

    generator.express_model = ConcurrentClient()
    generator.elder_express_model = ConcurrentClient()
    requests = []
    for slot, sentinel in [("sister_younger", "FACT_YOUNGER"), ("sister_elder", "FACT_ELDER")]:
        requests.append(
            generator.run_st_single_turn(
                stream_id=runtime.session_id,
                reply_message=target,
                chat_history=[],
                reply_tool_args={"persona_slot": slot, "_tool_observations": [{"result": sentinel}]},
            )
        )
    first, second = await asyncio.gather(*requests)
    assert first[1].persona_card_id == "maibot_xuyan"
    assert second[1].persona_card_id == "maibot_xuyue"
    assert {row["tool_observations"][0]["result"] for row in captured} == {"FACT_YOUNGER", "FACT_ELDER"}
    assert generator._last_st_compiled_request is None
    assert generator._last_is_owner_private is False
    assert generator._last_persona_v2_turn is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,enabled,abort",
    [
        ("dual_director", False, False),
        ("dual_director", True, False),
        ("one_pass_st", True, False),
        ("one_pass_st", False, True),
    ],
)
async def test_topology_and_replyer_st_four_state_contract(tmp_path, monkeypatch, mode, enabled, abort):
    from src.companion_vault.st_preset_engine import STPresetRuntimeManager
    from src.companion_vault.twin_runtime_router import resolve_turn_route
    from src.config.official_configs import TwinEconomyConfig
    import src.companion_vault.st_preset_engine as st

    manager = STPresetRuntimeManager(tmp_path / "presets")
    manager.save_config({"enabled": enabled})
    monkeypatch.setattr(st, "_GLOBAL_ST_MANAGER", manager)
    monkeypatch.delenv("MAIBOT_ST_PRESET_FORCE", raising=False)
    decision = await resolve_turn_route(
        session_id="four_state",
        platform="wechat",
        sender_id="friend",
        is_group_chat=False,
        truth_db_path=str(tmp_path / "truth.db"),
        eco_cfg=TwinEconomyConfig(enabled=False, chat_execution_mode=mode),
    )
    assert decision.execution_mode == mode
    assert decision.abort_turn is abort
    if abort:
        assert decision.abort_reason == "invalid_one_pass_st_without_preset"


def test_st_config_errors_force_and_hot_reload_are_visible(tmp_path, monkeypatch):
    import json
    from src.companion_vault.st_preset_engine import STPresetRuntimeManager, is_st_preset_enabled

    manager = STPresetRuntimeManager(tmp_path / "presets")
    monkeypatch.delenv("MAIBOT_ST_PRESET_FORCE", raising=False)
    manager.save_config({"enabled": False})
    assert manager.get_active_engine()[0] is None
    monkeypatch.setenv("MAIBOT_ST_PRESET_FORCE", "1")
    assert manager.get_active_engine()[0] is not None
    assert is_st_preset_enabled(manager.load_config())
    monkeypatch.setenv("MAIBOT_ST_PRESET_FORCE", "0")
    assert manager.get_active_engine()[0] is None
    manager.save_config({"enabled": True, "active_preset": "missing"})
    with pytest.raises(ValueError, match="not found"):
        manager.get_active_engine()
    manager.config_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        manager.load_config()
    manager.config_path.write_text('{"enabled":"false"}', encoding="utf-8")
    with pytest.raises(ValueError, match="boolean"):
        manager.load_config()
    manager.config_path.write_text('{"enabled":true}', encoding="utf-8")
    assert manager.get_active_engine()[0] is not None


@pytest.mark.parametrize(
    "arguments",
    [
        {"think_nya": "思考", "game_content": {"json_patch": []}},
        {"think_nya": 9, "game_content": "正文"},
        {"think_nya": "思考", "game_content": ["正文"]},
    ],
)
def test_wrong_envelope_types_are_rejected_before_delivery(arguments):
    from src.llm_models.payload_content.tool_option import ToolCall

    check = STPresetEngine.evaluate_wechat_direct_reply(
        response_text="",
        tool_calls=[ToolCall(call_id="bad", func_name="soliumbra_reply", args=arguments)],
    )
    assert not check.is_valid
    assert check.reason == "invalid_st_tool_arg_type"


def test_unknown_function_is_rejected_even_with_prose():
    from src.llm_models.payload_content.tool_option import ToolCall

    check = STPresetEngine.evaluate_wechat_direct_reply(
        response_text="看似可以发送的文字",
        tool_calls=[ToolCall(call_id="bad", func_name="invented", args={})],
    )
    assert not check.is_valid
    assert check.reason == "unknown_st_tool"
