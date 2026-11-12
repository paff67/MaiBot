"""Maisaka 推理引擎测试。"""

from types import SimpleNamespace
from typing import Optional

import pytest

from src.common.data_models.llm_service_data_models import LLMResponseResult
from src.llm_models.model_client.base_client import GenerationAttempt, GenerationTrace
from src.llm_models.payload_content.context_item import (
    ContextItemMeta,
    ProviderActivityItem,
)
from src.llm_models.payload_content.native_tool import NativeToolCallSummary
from src.maisaka.chat_loop_service import ChatResponse, MaisakaChatLoopService
from src.maisaka.context.usage import (
    SECTION_INSTANT_NOTICE,
    SECTION_MESSAGES,
    SECTION_SYSTEM_PROMPT,
    ContextSectionUsage,
)
from src.maisaka.display.prompt_cli_renderer import PromptCLIVisualizer
from src.maisaka.monitor.events import (
    _serialize_planner_block,
    _serialize_request_block,
    _serialize_tool_results,
)
from src.maisaka.reasoning_engine import MaisakaReasoningEngine


def _build_chat_response(content: Optional[str], reasoning: str) -> ChatResponse:
    """构造仅包含 Planner 思考字段的响应。"""

    result = LLMResponseResult.from_portable_output(
        response=content or "",
        reasoning=reasoning,
    )
    return ChatResponse(
        output_items=result.output_items,
        request_messages=[],
        selected_history_count=0,
        tool_count=0,
        prompt_tokens=0,
        built_message_count=0,
        completion_tokens=0,
        total_tokens=0,
    )


@pytest.mark.parametrize(
    ("content", "reasoning", "expected"),
    [
        (" Planner 工具正文 ", " Provider 原生推理 ", "Planner 工具正文"),
        ("", " Provider 原生推理 ", ""),
        (None, " Provider 原生推理 ", ""),
        ("   ", "   ", ""),
    ],
)
def test_planner_content_does_not_fall_back_to_reasoning(
    content: Optional[str],
    reasoning: str,
    expected: str,
) -> None:
    response = _build_chat_response(content, reasoning)

    result = MaisakaReasoningEngine._get_planner_content(response)

    assert result == expected


def test_native_tool_summary_is_serialized_without_provider_state() -> None:
    summary = NativeToolCallSummary(
        tool_type="web_search",
        call_id="ws_test",
        status="completed",
        action_type="search",
        details=["查询：Responses API"],
        source_count=2,
    )

    block = _serialize_planner_block("完成", [], [summary], 10, 5, 15, 100.0)

    assert block is not None
    assert block["native_tool_calls"] == [
        {
            "tool_type": "web_search",
            "call_id": "ws_test",
            "status": "completed",
            "action_type": "search",
            "details": ["查询：Responses API"],
            "source_count": 2,
        }
    ]
    assert "provider_state" not in block


def test_tool_stop_request_is_serialized_for_monitor() -> None:
    tools = _serialize_tool_results(
        [
            {
                "tool_call_id": "call-test",
                "tool_name": "test_tool",
                "success": True,
                "stop_after_execution": True,
                "started_at": 100.25,
                "ended_at": 102.75,
            }
        ]
    )

    assert tools[0]["stop_after_execution"] is True
    assert tools[0]["started_at"] == 100.25
    assert tools[0]["ended_at"] == 102.75


def test_cache_tokens_and_context_sections_are_serialized_for_monitor() -> None:
    planner_block = _serialize_planner_block("完成", [], [], 100, 5, 105, 100.0, "", 80, 20)
    request_block = _serialize_request_block(
        [{"role": "system", "content": "系统提示词"}],
        1,
        2,
        [ContextSectionUsage("messages", 12, 1)],
    )

    assert planner_block is not None
    assert planner_block["prompt_cache_hit_tokens"] == 80
    assert planner_block["prompt_cache_miss_tokens"] == 20
    assert request_block is not None
    assert request_block["context_sections"] == [{"key": "messages", "chars": 12, "count": 1}]


@pytest.mark.asyncio
async def test_chat_loop_reports_context_sections_and_cache_tokens(monkeypatch) -> None:
    """chat_loop_step 应回报提示词分段用量与缓存命中 token。"""

    class FakeLLMClient:
        async def generate_response_with_context(self, context_factory, options) -> LLMResponseResult:
            del options
            context_factory(None)
            result = LLMResponseResult.from_portable_output(response="好的", model_name="test-model")
            result.prompt_tokens = 1000
            result.completion_tokens = 20
            result.total_tokens = 1020
            result.prompt_cache_hit_tokens = 800
            result.prompt_cache_miss_tokens = 200
            return result

    class PassthroughRuntimeManager:
        async def invoke_hook(self, hook_name: str, **kwargs: object) -> SimpleNamespace:
            del hook_name
            return SimpleNamespace(kwargs=kwargs)

    service = MaisakaChatLoopService(chat_system_prompt="测试系统提示词")
    monkeypatch.setattr(service, "_get_llm_chat_client", lambda request_kind: FakeLLMClient())
    monkeypatch.setattr(
        MaisakaChatLoopService,
        "_get_runtime_manager",
        staticmethod(lambda: PassthroughRuntimeManager()),
    )
    monkeypatch.setattr(
        PromptCLIVisualizer,
        "build_prompt_section_result",
        staticmethod(
            lambda *args, **kwargs: SimpleNamespace(
                panel=None,
                preview_access=SimpleNamespace(preview_web_uri=""),
            )
        ),
    )

    response = await service.chat_loop_step([], tool_definitions=[])

    sections = {section.key: section.chars for section in response.context_sections}
    # 本轮没有历史消息：系统提示词单独成段，末尾的当前时间与提醒归入即时提示
    assert sections[SECTION_SYSTEM_PROMPT] == len("测试系统提示词")
    assert sections[SECTION_INSTANT_NOTICE] > 0
    assert SECTION_MESSAGES not in sections
    assert response.prompt_cache_hit_tokens == 800
    assert response.prompt_cache_miss_tokens == 200


@pytest.mark.asyncio
async def test_chat_loop_keeps_reasoning_separate_from_content(monkeypatch) -> None:
    """Provider 仅返回 reasoning 时，不应将其回填为 Planner 正文。"""

    class FakeLLMClient:
        async def generate_response_with_context(self, context_factory, options) -> LLMResponseResult:
            del context_factory, options
            result = LLMResponseResult.from_portable_output(
                reasoning="Provider 原生推理",
                model_name="test-model",
            )
            logical_turn_id = result.output_items[0].meta.logical_turn_id
            assert logical_turn_id is not None
            result.output_items = (
                *result.output_items,
                ProviderActivityItem(
                    meta=ContextItemMeta.create(
                        logical_turn_id=logical_turn_id,
                    ),
                    provider_type="web_search",
                    call_id="ws_test",
                    status="completed",
                    action_type="search",
                    details=("查询：Responses API",),
                ),
            )
            result.generation_trace = GenerationTrace(
                provider="test-provider",
                endpoint="responses",
                model="test-model",
                response_id="resp_test",
                status="completed",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                prompt_cache_hit_tokens=0,
                prompt_cache_miss_tokens=0,
                output_item_ids=tuple(item.meta.item_id for item in result.output_items),
            )
            result.provider_response = {
                "id": "resp_test",
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "rs_test",
                        "summary": [{"type": "summary_text", "text": "Provider 原生推理"}],
                    },
                    {
                        "type": "web_search_call",
                        "id": "ws_test",
                        "status": "completed",
                        "action": {"type": "search", "queries": ["Responses API"]},
                    },
                ],
            }
            result.generation_attempts = (
                GenerationAttempt(
                    attempt_id="planner-attempt-1",
                    workflow_purpose="planner",
                    workflow_attempt=1,
                    provider_attempt=1,
                    model_attempt=1,
                    status="succeeded",
                    started_at="2026-08-05T00:00:00.000",
                    duration_ms=1.0,
                    provider="test-provider",
                    endpoint="responses",
                    model="test-model",
                    client_type="openai_responses",
                    operation="response",
                    wire_protocol="responses",
                ),
            )
            return result

    class PassthroughRuntimeManager:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def invoke_hook(self, hook_name: str, **kwargs: object) -> SimpleNamespace:
            self.calls.append((hook_name, kwargs))
            return SimpleNamespace(kwargs=kwargs)

    runtime_manager = PassthroughRuntimeManager()
    service = MaisakaChatLoopService(chat_system_prompt="测试系统提示词")
    monkeypatch.setattr(service, "_get_llm_chat_client", lambda request_kind: FakeLLMClient())
    monkeypatch.setattr(
        MaisakaChatLoopService,
        "_get_runtime_manager",
        staticmethod(lambda: runtime_manager),
    )
    prompt_preview_kwargs: dict[str, object] = {}

    def build_prompt_section_result(*args: object, **kwargs: object) -> SimpleNamespace:
        del args
        prompt_preview_kwargs.update(kwargs)
        return SimpleNamespace(
            panel=None,
            preview_access=SimpleNamespace(preview_web_uri=""),
        )

    monkeypatch.setattr(
        PromptCLIVisualizer,
        "build_prompt_section_result",
        build_prompt_section_result,
    )

    response = await service.chat_loop_step([])

    after_response_kwargs = next(
        kwargs for hook_name, kwargs in runtime_manager.calls if hook_name == "maisaka.planner.after_response"
    )
    assert len(after_response_kwargs["output_items"]) == 2
    assert response.content is None
    assert all(message.content == "" for message in response.raw_messages)
    assert response.reasoning == "Provider 原生推理"
    assert response.native_tool_calls[0].call_id == "ws_test"
    assert all(not hasattr(message, "native_tool_calls") for message in response.raw_messages)
    preview_output_items = prompt_preview_kwargs["output_items"]
    assert isinstance(preview_output_items, tuple)
    assert len(preview_output_items) == 2
    assert preview_output_items[0].__class__.__name__ == "ReasoningItem"
    assert preview_output_items[1].__class__.__name__ == "ProviderActivityItem"
    generation_attempts = prompt_preview_kwargs["generation_attempts"]
    assert isinstance(generation_attempts, tuple)
    assert generation_attempts[0].attempt_id == "planner-attempt-1"
    assert not hasattr(generation_attempts[0], "wire_response")


def _make_session_backed_message(
    msg_id: str,
    timestamp_sec: float,
    text: str,
    *,
    source_kind: str = "user",
):
    from datetime import datetime
    from src.chat.message_receive.message import SessionMessage
    from src.common.data_models.mai_message_data_model import MessageInfo, UserInfo
    from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
    from src.maisaka.context.messages import SessionBackedMessage

    dt = datetime.fromtimestamp(timestamp_sec)
    session_msg = SessionMessage(msg_id, dt, "wechat", received_at=dt)
    session_msg.session_id = "session-1"
    session_msg.message_info = MessageInfo(
        user_info=UserInfo(user_id="u1", user_nickname="测试用户", user_cardname=""),
        group_info=None,
    )
    session_msg.raw_message = MessageSequence([TextComponent(text)])
    session_msg.processed_plain_text = text
    return SessionBackedMessage.from_session_message(
        session_msg,
        raw_message=session_msg.raw_message,
        visible_text=text,
        source_kind=source_kind,
    )


def test_insert_chat_history_message_orders_user_before_later_guided_reply() -> None:
    """当 _chat_history 末尾存在时间戳更晚的已发送消息时，新入站用户消息应按时间顺序插在其前。"""
    from datetime import datetime
    from src.maisaka.context.messages import ToolResultMessage

    earlier_user = _make_session_backed_message("m_earlier", 1000.0, "第一条", source_kind="user")
    bot_reply = _make_session_backed_message("m_bot", 1046.0, "机器人回复", source_kind="guided_reply")
    tool_result = ToolResultMessage(
        content="已成功发送回复",
        timestamp=datetime.fromtimestamp(1046.0),
        tool_call_id="call_1",
        logical_turn_id="turn_1",
        tool_name="reply",
        success=True,
    )
    in_flight_user = _make_session_backed_message("m_inflight", 1010.0, "生成期间到达的消息", source_kind="user")

    # 场景 1: ToolResultMessage 在前，guided_reply 在末尾
    runtime1 = SimpleNamespace(_chat_history=[earlier_user, tool_result, bot_reply])
    engine1 = MaisakaReasoningEngine(runtime1)
    idx1 = engine1._insert_chat_history_message(in_flight_user)
    assert idx1 == 2
    assert [getattr(m, "message_id", getattr(m, "tool_name", "")) for m in runtime1._chat_history] == [
        "m_earlier",
        "reply",
        "m_inflight",
        "m_bot",
    ]

    # 场景 2: guided_reply 在 ToolResultMessage 之前（尚未经过 normalize_tool_result_order）
    runtime2 = SimpleNamespace(_chat_history=[earlier_user, bot_reply, tool_result])
    engine2 = MaisakaReasoningEngine(runtime2)
    idx2 = engine2._insert_chat_history_message(in_flight_user)
    assert idx2 == 1
    assert [getattr(m, "message_id", getattr(m, "tool_name", "")) for m in runtime2._chat_history] == [
        "m_earlier",
        "m_inflight",
        "m_bot",
        "reply",
    ]


def test_append_sent_message_to_chat_history_attributes_quote_from_last_reply_target() -> None:
    """guided_reply 即使未触发平台层显式引用，也应在内部历史中记录 quote=<target_msg_id>。"""
    from datetime import datetime
    from unittest.mock import Mock
    from src.chat.message_receive.message import SessionMessage
    from src.common.data_models.mai_message_data_model import MessageInfo, UserInfo
    from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
    from src.maisaka.runtime import MaisakaHeartFlowChatting

    fake_runtime = SimpleNamespace(
        _chat_history=[],
        _focus_group=None,
        _last_reply_target_message_id="mehc26n",
        _is_focus_mode_active_for_current_chat=lambda: False,
        _quote_counter=SimpleNamespace(record=Mock()),
        _schedule_sent_image_recognition=Mock(),
        _emit_monitor_message_sent=Mock(),
        log_prefix="[test]",
    )
    dt = datetime(2026, 11, 7, 5, 3, 46)
    sent_msg = SessionMessage("143031", dt, "wechat", received_at=dt)
    sent_msg.session_id = "session-1"
    sent_msg.message_info = MessageInfo(
        user_info=UserInfo(user_id="bot_1", user_nickname="许妍", user_cardname="许妍"),
        group_info=None,
    )
    sent_msg.raw_message = MessageSequence([TextComponent("连发两个滚蛋还用猜？")])
    sent_msg.processed_plain_text = "连发两个滚蛋还用猜？"

    ok = MaisakaHeartFlowChatting.append_sent_message_to_chat_history(
        fake_runtime,
        sent_msg,
        source_kind="guided_reply",
    )
    assert ok is True
    assert len(fake_runtime._chat_history) == 1
    first_text = fake_runtime._chat_history[0].raw_message.components[0].text
    assert 'msg_id="143031"' in first_text
    assert 'quote="mehc26n"' in first_text
    assert 'is_self_message="true"' in first_text


def test_handle_tool_calls_auto_stops_after_reply_only_when_no_pending_messages() -> None:
    """reply 成功且无新待处理消息时自动结束本轮思考；若有新消息待处理则继续下一轮。"""
    import asyncio
    from unittest.mock import AsyncMock, Mock
    from src.core.tooling import ToolExecutionResult, ToolSpec
    from src.llm_models.payload_content.tool_option import ToolCall
    from src.maisaka.reasoning_engine import CycleRuntimeState, STOP_AFTER_EXECUTION_PAUSE_REASON

    async def _run(has_pending: bool):
        registry = SimpleNamespace(
            list_tools=AsyncMock(
                return_value=[
                    ToolSpec(
                        name="reply",
                        description="reply",
                        parameters_schema={"type": "object", "properties": {}},
                        provider_name="maisaka_builtin",
                        provider_type="builtin",
                    )
                ]
            ),
            invoke=AsyncMock(
                return_value=ToolExecutionResult(
                    tool_name="reply",
                    success=True,
                    content="已成功发送回复。",
                )
            ),
        )
        runtime = SimpleNamespace(
            _tool_registry=registry,
            _chat_history=[],
            session_id="test-reply-stop",
            chat_stream=SimpleNamespace(is_group_session=True, group_id="g1", user_id="", platform="wechat"),
            is_action_tool_currently_available=lambda name: True,
            expand_message_id_aliases=lambda arguments: arguments,
            _has_pending_messages=lambda: has_pending,
            _update_stage_status=Mock(),
            _reset_consecutive_wait_count=Mock(),
            _end_planner_continuation=Mock(),
            _enter_stop_state=Mock(),
            log_prefix="[test]",
        )
        engine = MaisakaReasoningEngine(runtime)
        engine._record_tool_execution_effects = AsyncMock()
        engine._append_tool_execution_result = Mock()
        engine._emit_planner_progress = AsyncMock()

        return await engine._handle_tool_calls(
            [ToolCall(call_id="reply-1", func_name="reply", args={"msg_id": "m1", "reply_reference": "简短回应"})],
            "简短思考",
            SimpleNamespace(),
            CycleRuntimeState(),
        ), runtime

    (should_pause_no_pending, reason_no_pending, _, _), rt_no_pending = asyncio.run(_run(False))
    assert should_pause_no_pending is True
    assert reason_no_pending == STOP_AFTER_EXECUTION_PAUSE_REASON
    rt_no_pending._enter_stop_state.assert_called_once_with()

    (should_pause_with_pending, reason_with_pending, _, _), rt_with_pending = asyncio.run(_run(True))
    assert should_pause_with_pending is False
    assert reason_with_pending == ""
    rt_with_pending._enter_stop_state.assert_not_called()


def test_gateway_error_placeholder_retry_and_strip(monkeypatch) -> None:
    """上游网关返回 [网关提示] 时自动重试一次；若仍为网关提示则清空正文，绝不写入 _chat_history。"""
    import asyncio
    from unittest.mock import AsyncMock, Mock
    from src.maisaka.reasoning_engine import CycleRuntimeState

    gateway_text = (
        "[网关提示] 模型未返回可见文本内容（finishReason=MAX_TOKENS）。\n"
        "可能原因：输出全部为思考过程（thought）但无最终回答。\n"
        "（此提示由网关生成，不是模型回答）"
    )

    class AlwaysGatewayLLMClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_response_with_context(self, context_factory, options) -> LLMResponseResult:
            del options
            context_factory(None)
            self.calls += 1
            return LLMResponseResult.from_portable_output(response=gateway_text, model_name="test-model")

    class PassthroughRuntimeManager:
        async def invoke_hook(self, hook_name: str, **kwargs: object) -> SimpleNamespace:
            del hook_name
            return SimpleNamespace(kwargs=kwargs)

    async def _run():
        client = AlwaysGatewayLLMClient()
        service = MaisakaChatLoopService(chat_system_prompt="测试系统提示词")
        monkeypatch.setattr(service, "_get_llm_chat_client", lambda request_kind: client)
        monkeypatch.setattr(
            MaisakaChatLoopService,
            "_get_runtime_manager",
            staticmethod(lambda: PassthroughRuntimeManager()),
        )
        monkeypatch.setattr(
            PromptCLIVisualizer,
            "build_prompt_section_result",
            staticmethod(
                lambda *args, **kwargs: SimpleNamespace(
                    panel=None,
                    preview_access=SimpleNamespace(preview_web_uri=""),
                )
            ),
        )

        response = await service.chat_loop_step([], tool_definitions=[])
        assert client.calls == 2
        assert response.content is None
        assert all("[网关提示]" not in m.content for m in response.raw_messages)

        with pytest.raises(RuntimeError, match="上游网关返回截断提示文本"):
            await service.chat_loop_step([], request_kind="sub_agent", tool_definitions=[])

        # 验证 _handle_planner_response_actions 直接收到网关提示时也不会写入 _chat_history
        raw_gateway_resp = _build_chat_response(gateway_text, "")
        runtime = SimpleNamespace(
            _chat_history=[],
            _end_planner_continuation=Mock(),
            _reset_consecutive_wait_count=Mock(),
            _enter_stop_state=Mock(),
            log_prefix="[test]",
        )
        engine = MaisakaReasoningEngine(runtime)
        engine._emit_planner_progress = AsyncMock()
        state = CycleRuntimeState()
        await engine._handle_planner_response_actions(
            response=raw_gateway_resp,
            cycle_detail=SimpleNamespace(),
            state=state,
            planner_extra_lines=state.planner_extra_lines,
        )
        assert runtime._chat_history == []

    asyncio.run(_run())


def test_companion_truth_person_profile_unification(tmp_path) -> None:
    """验证 person_profile.py 与 query_person_profile.py 优先读取 companion_truth.db，且工具默认设为 deferred。"""
    import asyncio
    import sqlite3
    from src.core.tooling import ToolInvocation
    from src.maisaka.builtin_tool import BUILTIN_TOOL_ENTRIES
    from src.maisaka.builtin_tool.query_person_profile import handle_tool as handle_query_person_profile
    from src.maisaka.memory.person_profile import _query_companion_truth_profile

    entry = next(e for e in BUILTIN_TOOL_ENTRIES if e.name == "query_person_profile")
    assert entry.visibility == "deferred"

    db_file = tmp_path / "companion_truth.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE person_info ("
        "person_id TEXT PRIMARY KEY, platform_ids TEXT, person_name TEXT, "
        "wechat_remark TEXT, trust_role TEXT, one_line_intro TEXT, impression TEXT)"
    )
    conn.execute(
        "INSERT INTO person_info VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "wxid_test_owner",
            '{"wechat": "wxid_test_owner"}',
            "paff",
            "paff备注",
            "OWNER",
            "麦麦的创造者与主人",
            "亲近信任的创造者",
        ),
    )
    conn.commit()
    conn.close()

    # 默认 Fail-Closed (is_group_chat=True) 屏蔽私密 impression，仅暴露公开简介
    hit_group_default = _query_companion_truth_profile("wechat", "wxid_test_owner", "", db_path=str(db_file))
    assert hit_group_default is not None
    display_name_g, profile_text_g = hit_group_default
    assert display_name_g == "paff"
    assert "OWNER" in profile_text_g
    assert "麦麦的创造者与主人" in profile_text_g
    assert "亲近信任的创造者" not in profile_text_g

    # OWNER 私聊 (is_group_chat=False) 正常挂载私密 impression
    hit = _query_companion_truth_profile(
        "wechat",
        "wxid_test_owner",
        "",
        db_path=str(db_file),
        is_group_chat=False,
        requester_user_id="wxid_test_owner",
    )
    assert hit is not None
    display_name, profile_text = hit
    assert display_name == "paff"
    assert "OWNER" in profile_text
    assert "亲近信任的创造者" in profile_text
    assert "麦麦的创造者与主人" in profile_text

    class DummyToolCtx:
        def __init__(self, *, is_group_session: bool = True, platform: str = "", user_id: str = ""):
            self.runtime = SimpleNamespace(
                chat_stream=SimpleNamespace(
                    is_group_session=is_group_session,
                    platform=platform,
                    user_id=user_id,
                )
            )

        def build_success_result(self, tool_name, content, *, structured_content=None):
            return SimpleNamespace(success=True, tool_name=tool_name, content=content, structured_content=structured_content)

        def build_failure_result(self, tool_name, error_message, *, structured_content=None):
            return SimpleNamespace(success=False, tool_name=tool_name, error_message=error_message, structured_content=structured_content)

    # 群聊 / 未鉴权默认上下文：物理屏蔽私密 impression
    res_group = asyncio.run(
        handle_query_person_profile(
            DummyToolCtx(is_group_session=True, platform="wechat", user_id="wxid_other"),
            ToolInvocation(
                tool_name="query_person_profile",
                arguments={"person_name": "paff", "db_path": str(db_file)},
            ),
        )
    )
    assert res_group.success is True
    assert "亲近信任的创造者" not in res_group.content

    # OWNER 本人私聊上下文：允许读取私密 impression
    res = asyncio.run(
        handle_query_person_profile(
            DummyToolCtx(is_group_session=False, platform="wechat", user_id="wxid_test_owner"),
            ToolInvocation(
                tool_name="query_person_profile",
                arguments={"person_name": "paff", "db_path": str(db_file)},
            ),
        )
    )
    assert res.success is True
    assert res.structured_content["profile_source"] == "companion_truth"
    assert res.structured_content["trust_role"] == "OWNER"
    assert "亲近信任的创造者" in res.content


def test_compaction_failure_retains_history_until_5x_and_2x_overload(monkeypatch) -> None:
    """验证 P1-1：上下文压缩失败时绝不静默丢弃原文，仅在连续失败满 5 次且上下文 > max_context_size * 2 时启用本地确定性降级摘要。"""
    import asyncio
    from unittest.mock import AsyncMock, Mock
    from src.maisaka import reasoning_engine as re_module
    from src.maisaka.context.compaction import ContextSummaryMessage, PlannerRequestSnapshot

    async def always_fail_summary(*args, **kwargs):
        raise RuntimeError("上游网关返回截断提示文本")

    monkeypatch.setattr(re_module, "build_context_summary", always_fail_summary)
    monkeypatch.setattr(re_module, "emit_flow_step", AsyncMock())

    # 构造 15 条消息，设 max_context_size = 6，则 max_context_size * 2 = 12，15 > 12 满足超限条件
    messages = [
        _make_session_backed_message(f"m{i}", float(1000 + i), f"历史消息内容_{i}")
        for i in range(1, 16)
    ]
    runtime = SimpleNamespace(
        _chat_history=list(messages),
        _max_context_size=6,
        _get_pending_wait_tool_call_ids=lambda: set(),
        _chat_loop_service=SimpleNamespace(
            planner_request_snapshot=PlannerRequestSnapshot(request=SimpleNamespace(), history=tuple(messages))
        ),
        _monitor_run_id="run-1",
        _log_history_trimmed=Mock(),
        _trigger_trimmed_history_learning=AsyncMock(),
        session_id="test-session",
        log_prefix="[test]",
    )
    engine = MaisakaReasoningEngine(runtime)

    async def _run():
        # 前 4 次失败：即使总消息数 (15) > max_context_size * 2 (12)，由于连续失败未满 5 次，必须完整保留 15 条原文
        for attempt in range(1, 5):
            cycle_detail = SimpleNamespace(time_records={}, cycle_id=attempt)
            await engine._post_process_chat_history_after_cycle(cycle_detail)
            assert engine._compaction_failure_count == attempt
            assert len(runtime._chat_history) == 15
            assert not any(isinstance(m, ContextSummaryMessage) for m in runtime._chat_history)
            assert runtime._chat_loop_service.planner_request_snapshot is not None

        # 第 5 次失败且 15 > 12：触发本地确定性降级摘要，旧消息被归档入 ContextSummaryMessage 而非静默丢失
        cycle_detail = SimpleNamespace(time_records={}, cycle_id=5)
        await engine._post_process_chat_history_after_cycle(cycle_detail)
        assert engine._compaction_failure_count == 0
        assert isinstance(runtime._chat_history[0], ContextSummaryMessage)
        assert "本地确定性归档" in runtime._chat_history[0].content
        assert "历史消息内容_1" in runtime._chat_history[0].content
        assert len(runtime._chat_history) == 6  # 1 条降级摘要 + 5 条近期保护原文

    asyncio.run(_run())


