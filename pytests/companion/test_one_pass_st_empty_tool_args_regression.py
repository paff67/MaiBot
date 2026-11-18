"""one_pass_st 空参 soliumbra_reply 与无可见正文直答误记成功回归测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import datetime
import json

import pytest

from src.chat.message_receive.message import SessionMessage
from src.chat.replyer.maisaka_generator import MaisakaReplyGenerator
from src.chat.replyer.replyer_manager import replyer_manager
from src.common.data_models.llm_service_data_models import LLMGenerationOptions, LLMResponseResult
from src.common.data_models.mai_message_data_model import GroupInfo, MessageInfo, UserInfo
from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
from src.companion_vault.st_preset_engine import STPresetEngine, STPresetRuntimeManager
from src.companion_vault.twin_runtime_router import TwinTurnDecision
from src.config.config import ModelConfig, config_manager, global_config
from src.config.model_configs import APIProvider, ModelInfo, ModelTaskConfig, TaskConfig
from src.core.tooling import ToolExecutionResult, ToolProvider, ToolRegistry, ToolSpec
from src.llm_models.exceptions import RespParseException
from src.llm_models.model_client.base_client import APIResponse, BaseClient, ResponseRequest, client_registry
from src.llm_models.model_client.gemini_client import GeminiClient
from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType
from src.llm_models.payload_content.tool_option import ToolCall, ToolOption
from src.llm_models.request_snapshot import read_request_snapshot, serialize_response_request_snapshot
from src.llm_models.utils_model import LLMOrchestrator
from src.maisaka.builtin_tool.provider import MaisakaBuiltinToolProvider
from src.maisaka.context.messages import SessionBackedMessage
from src.maisaka.reasoning_engine import MaisakaReasoningEngine
from src.services.llm_service import LLMServiceClient

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PRESET_PATH = REPO_ROOT / "plugins" / "st_preset_engine" / "templates" / "Meowssiah-WeChat-IM.json"


def _make_session_message(
    *,
    platform: str = "wechat",
    user_id: str = "wxid_owner",
    nickname: str = "星河",
    group_id: str = "",
    text: str = "在干嘛",
    msg_id: str = "msg_001",
    session_id: str = "priv_owner_st",
) -> SessionMessage:
    msg = SessionMessage(
        message_id=msg_id,
        timestamp=datetime.datetime.now(),
        platform=platform,
    )
    msg.session_id = session_id
    msg.message_info = MessageInfo(
        user_info=UserInfo(user_id=user_id, user_nickname=nickname),
        group_info=GroupInfo(group_id=group_id, group_name="测试群") if group_id else None,
    )
    msg.raw_message = MessageSequence([TextComponent(text)])
    msg.processed_plain_text = text
    msg.initialized = True
    return msg


def _make_st_preset_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, output_mode: str = "im_clean") -> STPresetRuntimeManager:
    import src.companion_vault.st_preset_engine as st_mod

    presets_dir = tmp_path / "presets"
    st_mgr = STPresetRuntimeManager(presets_dir=presets_dir)
    st_mgr.save_config(
        {
            "enabled": True,
            "active_preset": "Meowssiah-WeChat-IM",
            "output_mode": output_mode,
            "protocol_mode": "spreset_tool_envelope",
        }
    )
    monkeypatch.setattr(st_mod, "_GLOBAL_ST_MANAGER", st_mgr)
    monkeypatch.setenv("MAIBOT_ST_PRESETS_DIR", str(presets_dir))
    return st_mgr


def _isolate_runtime_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.llm_models.request_snapshot as snapshot_mod
    import src.llm_models.utils_model as utils_model_mod
    import src.maisaka.display.prompt_preview_logger as preview_mod
    import src.services.llm_service as llm_service_mod
    import src.services.model_task_usage_service as usage_service_mod

    preview_root = tmp_path / "maisaka_prompt"
    monkeypatch.setattr(preview_mod.PromptPreviewLogger, "_BASE_DIR", preview_root)
    monkeypatch.setattr(preview_mod.PromptPreviewLogger, "_IMAGE_DIR", tmp_path / "prompt_imgs")
    monkeypatch.setattr(preview_mod.PromptPreviewLogger, "_image_index_ready", False)
    monkeypatch.setattr(preview_mod.PromptPreviewLogger, "_images_by_preview", {})
    monkeypatch.setattr(preview_mod.PromptPreviewLogger, "_previews_by_image", {})
    monkeypatch.setattr(preview_mod.PromptPreviewLogger, "_record_signatures", {})
    monkeypatch.setattr(snapshot_mod, "LLM_REQUEST_LOG_DIR", preview_root / "llm_error")
    monkeypatch.setattr(utils_model_mod.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(utils_model_mod.llm_usage_recorder, "record_usage_to_database", lambda **_kw: None)
    monkeypatch.setattr(llm_service_mod, "record_llm_cache_usage", lambda **_kw: None)
    monkeypatch.setattr(llm_service_mod.LLMServiceClient, "_record_compute_usage", staticmethod(lambda _res: None))
    monkeypatch.setattr(usage_service_mod, "record_model_task_usage", lambda *_a, **_kw: None)


def _setup_sequential_model_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    primary_max_retry: int = 3,
    backup_max_retry: int = 2,
    primary_client_type: str = "fake_seq_primary",
    backup_client_type: str = "fake_seq_backup",
    primary_stream: bool = False,
    backup_stream: bool = False,
) -> ModelConfig:
    primary_provider = APIProvider(
        name="prov_primary",
        base_url="https://primary.example.invalid/v1",
        api_key="sk-secret-primary-do-not-log",
        client_type=primary_client_type,
        max_retry=primary_max_retry,
        retry_interval=1,
    )
    backup_provider = APIProvider(
        name="prov_backup",
        base_url="https://backup.example.invalid/v1",
        api_key="sk-secret-backup-do-not-log",
        client_type=backup_client_type,
        max_retry=backup_max_retry,
        retry_interval=1,
    )
    primary_model = ModelInfo(
        name="gemini-4-hydrogen",
        model_identifier="gemini-4-hydrogen",
        api_provider="prov_primary",
        model_type="llm",
        force_stream_mode=primary_stream,
    )
    backup_model = ModelInfo(
        name="gemini-4-hydrogen-aistudio",
        model_identifier="gemini-4-hydrogen-aistudio",
        api_provider="prov_backup",
        model_type="llm",
        force_stream_mode=backup_stream,
    )
    task_cfg = TaskConfig(
        model_list=["gemini-4-hydrogen", "gemini-4-hydrogen-aistudio"],
        selection_strategy="sequential",
        max_tokens=1024,
        temperature=0.7,
        hard_timeout=30.0,
    )
    model_task_cfg = ModelTaskConfig(
        replyer=task_cfg,
        replyer_elder=task_cfg,
        planner=task_cfg,
        planner_flash=task_cfg,
    )
    fake_cfg = ModelConfig(
        api_providers=[primary_provider, backup_provider],
        models=[primary_model, backup_model],
        model_task_config=model_task_cfg,
    )
    monkeypatch.setattr(config_manager, "get_model_config", lambda: fake_cfg)
    return fake_cfg


def _build_one_pass_st_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_group_chat: bool = False,
    extra_tools: list[ToolProvider] | None = None,
) -> tuple[MaisakaReasoningEngine, Any, SessionMessage, SessionMessage, list[dict[str, Any]], list[Any]]:
    monkeypatch.setattr(global_config.personality, "twin_mode_enabled", False)
    monkeypatch.setattr(global_config.bot, "nickname", "许妍")
    monkeypatch.setattr(global_config.chat.reply_style, "group_quote_frequency", "never")
    monkeypatch.setattr(global_config.chat.reply_style, "private_quote_frequency", "never")
    monkeypatch.setattr(global_config.debug, "save_maisaka_reasoning", False)
    monkeypatch.setattr(global_config.debug, "replyer_retro_prompt", False)
    monkeypatch.setattr(global_config.a_memorix.integration, "enable_person_profile_injection", False)
    monkeypatch.setattr(global_config.chinese_typo, "enable", False)
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(tmp_path / "missing_truth.db"))

    session_id = "grp_st_reg" if is_group_chat else "priv_st_reg"
    group_id = "room@chatroom" if is_group_chat else ""
    chat_stream = SimpleNamespace(
        session_id=session_id,
        platform="wechat",
        account_id="bot_acc_001",
        scope="wechat",
        is_group_session=is_group_chat,
        group_id=group_id,
        user_id="" if is_group_chat else "wxid_owner",
    )

    replyer = MaisakaReplyGenerator(
        chat_stream=chat_stream,
        request_type="maisaka.replyer",
    )
    monkeypatch.setitem(replyer_manager._repliers, f"maisaka:{session_id}", replyer)
    monkeypatch.setattr(
        "src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info",
        lambda _sid: (is_group_chat, None),
    )

    msg_old = _make_session_message(
        group_id=group_id,
        text="早点休息",
        msg_id="msg_old_001",
        session_id=session_id,
    )
    msg_new = _make_session_message(
        group_id=group_id,
        text="刚才说什么呢",
        msg_id="msg_new_002",
        session_id=session_id,
    )
    hist_messages = [
        SessionBackedMessage.from_session_message(
            m,
            raw_message=m.raw_message,
            visible_text=m.processed_plain_text,
            source_kind="user",
        )
        for m in (msg_old, msg_new)
    ]

    sent_payloads: list[dict[str, Any]] = []

    async def _fake_send_to_target_with_message(**kwargs: Any) -> SimpleNamespace:
        sent_payloads.append(kwargs)
        return SimpleNamespace(message_id=f"sent_{len(sent_payloads)}")

    monkeypatch.setattr(
        "src.maisaka.builtin_tool.reply.send_service._send_to_target_with_message",
        _fake_send_to_target_with_message,
    )

    registry = ToolRegistry()

    class _Runtime:
        log_prefix = "[RegTestST]"
        _tool_registry = registry
        _chat_history = list(hist_messages)
        _max_context_size = 50
        _last_reply_target_message_id = ""
        _agent_state = "running"
        _STATE_RUNNING = "running"
        _STATE_WAIT = "wait"
        _STATE_STOP = "stop"
        _current_action_tool_names: set[str] = set()
        deferred_tool_specs_by_name: dict[str, object] = {}
        _update_stage_status = MagicMock()
        _reset_consecutive_wait_count = MagicMock()
        _end_planner_continuation = MagicMock()
        _enter_stop_state = MagicMock()
        _enter_counted_wait_state = MagicMock(return_value=1)
        record_planner_reply = MagicMock()
        _has_pending_messages = staticmethod(lambda: False)
        get_reply_quote_context = staticmethod(lambda _target: None)

        def __init__(self) -> None:
            self.session_id = session_id
            self.chat_stream = chat_stream
            self._current_action_tool_names: set[str] = set()
            self.deferred_tool_specs_by_name: dict[str, Any] = {}
            self._chat_loop_service = SimpleNamespace(
                select_llm_context_messages=lambda hist, **_kw: (list(hist), "test")
            )

        def find_source_message_by_id(self, message_id: str) -> SessionMessage | None:
            for item in self._chat_history:
                if getattr(item, "message_id", "") == message_id:
                    return item.original_message
            return None

        @staticmethod
        def expand_message_id_aliases(args: dict[str, Any]) -> dict[str, Any]:
            return dict(args)

        def set_current_action_tool_names(self, names: Any) -> None:
            self._current_action_tool_names = set(names)

        def is_action_tool_currently_available(self, name: str) -> bool:
            return name in self._current_action_tool_names

        def update_deferred_tool_specs(self, specs: Any) -> None:
            self.deferred_tool_specs_by_name = {spec.name: spec for spec in (specs or ())}

        @staticmethod
        def sync_discovered_deferred_tools_with_context(_hist: Any) -> None:
            return None

        @staticmethod
        def get_discovered_deferred_tool_specs() -> list[Any]:
            return []

        @staticmethod
        def build_deferred_tools_reminder() -> str:
            return ""

        def _is_focus_mode_active_for_current_chat(self) -> bool:
            return False

    runtime = _Runtime()
    engine = MaisakaReasoningEngine(runtime)  # type: ignore[arg-type]
    runtime._reasoning_engine = engine
    engine._start_cycle_round = AsyncMock(return_value=(SimpleNamespace(time_records={}, cycle_id=1), "R1"))  # type: ignore[method-assign]
    engine._refresh_visual_placeholders_for_cycle = AsyncMock()  # type: ignore[method-assign]
    engine._post_process_chat_history_after_cycle = AsyncMock()  # type: ignore[method-assign]
    engine._record_tool_execution_effects = AsyncMock()  # type: ignore[method-assign]
    engine._emit_planner_progress = AsyncMock()  # type: ignore[method-assign]

    captured_states: list[Any] = []

    async def _fake_finalize(*, cycle_detail: Any, round_index: int, state: Any) -> Any:
        del cycle_detail, round_index
        captured_states.append(state)
        return state.cycle_end

    engine._finalize_cycle = _fake_finalize  # type: ignore[method-assign]

    registry.register_provider(MaisakaBuiltinToolProvider(engine.build_builtin_tool_handlers()))
    for provider in extra_tools or ():
        registry.register_provider(provider)

    return engine, runtime, msg_old, msg_new, sent_payloads, captured_states


def _register_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    handlers: dict[str, Any],
) -> None:
    async def _fake_get_client(api_provider: APIProvider) -> BaseClient:
        return handlers[api_provider.name]

    monkeypatch.setattr(client_registry, "get_client_class_instance_async", _fake_get_client)


class _ScriptedResponseClient(BaseClient):
    """按队列返回预设 APIResponse 的最小测试客户端。"""

    def __init__(
        self,
        api_provider: APIProvider,
        responses: list[APIResponse],
        call_log: list[dict[str, Any]],
    ) -> None:
        super().__init__(api_provider)
        self._responses = list(responses)
        self._call_log = call_log

    async def get_response(self, request: ResponseRequest) -> APIResponse:
        self._call_log.append(
            {
                "provider": self.api_provider.name,
                "model": request.model_info.name,
                "extra_params": dict(request.extra_params),
                "tool_options": list(request.tool_options or []),
                "context_items": list(request.context_items),
            }
        )
        idx = min(len([c for c in self._call_log if c["provider"] == self.api_provider.name]) - 1, len(self._responses) - 1)
        resp = self._responses[idx]
        resp.bind_logical_turn(request.logical_turn_id)
        resp.attach_generation_trace(
            provider=self.api_provider.name,
            endpoint=self.api_provider.base_url,
            model=request.model_info.model_identifier,
        )
        return resp

    async def get_embedding(self, request: Any) -> APIResponse:
        raise NotImplementedError

    async def get_audio_transcriptions(self, request: Any) -> APIResponse:
        raise NotImplementedError

    def get_support_image_formats(self) -> list[str]:
        return []


def _api_response(
    *,
    text: str = "",
    reasoning: str = "",
    tool_calls: list[ToolCall] | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 12,
) -> APIResponse:
    res = LLMResponseResult.from_portable_output(
        response=text,
        reasoning=reasoning,
        tool_calls=tool_calls,
    )
    from src.llm_models.model_client.base_client import UsageRecord

    return APIResponse(
        output_items=res.output_items,
        usage=UsageRecord(
            model_name="test",
            provider_name="test",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


def _soliumbra_tool_option(required: list[str] | None = None) -> ToolOption:
    req = ["think_nya", "game_content"] if required is None else list(required)
    return ToolOption.from_definition(
        {
            "name": "soliumbra_reply",
            "description": "reply",
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "think_nya": {"type": "string"},
                    "game_content": {"type": "string"},
                    "story_body": {"type": "string"},
                },
                "required": req,
            },
        }
    )


# ---------------------------------------------------------------------------
# 1. 生产故障形状：Gemini 非流式 & 流式空参 soliumbra_reply({}) 在验收边界失败
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_mode", [False, True])
async def test_production_empty_soliumbra_reply_fails_for_gemini_normal_and_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream_mode: bool,
) -> None:
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    _make_st_preset_manager(tmp_path, monkeypatch)
    _setup_sequential_model_config(
        monkeypatch,
        primary_max_retry=1,
        backup_max_retry=1,
        primary_client_type="gemini",
        backup_client_type="gemini",
        primary_stream=stream_mode,
        backup_stream=stream_mode,
    )

    def _make_gemini_sdk_empty_call() -> SimpleNamespace:
        fc = SimpleNamespace(name="soliumbra_reply", id=None, args={})
        part = SimpleNamespace(text=None, thought=False, function_call=fc, thought_signature=None)
        return SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason="STOP")],
            text=None,
            function_calls=[fc],
            usage_metadata=SimpleNamespace(prompt_token_count=50, candidates_token_count=4, thoughts_token_count=0, total_token_count=54),
        )

    class _FakeAioModels:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_content(self, **_kwargs: Any) -> SimpleNamespace:
            self.calls += 1
            return _make_gemini_sdk_empty_call()

        async def generate_content_stream(self, **_kwargs: Any) -> Any:
            self.calls += 1

            async def _gen():
                yield _make_gemini_sdk_empty_call()

            return _gen()

    fake_aio = _FakeAioModels()
    provider_primary = config_manager.get_model_config().api_providers[0]
    provider_backup = config_manager.get_model_config().api_providers[1]

    def _build_gemini(prov: APIProvider) -> GeminiClient:
        cli = object.__new__(GeminiClient)
        cli.api_provider = prov
        cli.client = SimpleNamespace(aio=SimpleNamespace(models=fake_aio))  # type: ignore[assignment]
        return cli

    clients = {
        "prov_primary": _build_gemini(provider_primary),
        "prov_backup": _build_gemini(provider_backup),
    }
    _register_fake_client(monkeypatch, handlers=clients)

    chat_stream = SimpleNamespace(session_id="priv_gem", platform="wechat", is_group_session=False, group_id="", user_id="wxid_owner")
    replyer = MaisakaReplyGenerator(chat_stream=chat_stream, request_type="maisaka.replyer")
    monkeypatch.setattr("src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info", lambda _s: (False, None))
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(tmp_path / "missing.db"))
    msg = _make_session_message(session_id="priv_gem")

    with pytest.raises(RespParseException) as exc_info:
        await replyer.run_st_single_turn(
            stream_id="priv_gem",
            reply_message=msg,
            chat_history=[],
            reply_tool_args={"msg_id": msg.message_id, "reply_reference": "直接回复"},
        )

    assert "missing_required_tool_args" in str(exc_info.value)
    assert "soliumbra_reply" in str(exc_info.value)
    assert fake_aio.calls == 2
    trace_ctx = getattr(exc_info.value, "generation_trace_context", None)
    assert trace_ctx is not None
    assert all(attempt.status != "succeeded" for attempt in trace_ctx.generation_attempts)


@pytest.mark.asyncio
async def test_streaming_chunks_are_validated_only_after_full_aggregation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """流式分片过程中临时缺字段或仅思考分片不能按 chunk 拒绝，只在完整组装后校验。"""
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    _make_st_preset_manager(tmp_path, monkeypatch)
    _setup_sequential_model_config(
        monkeypatch,
        primary_max_retry=1,
        backup_max_retry=1,
        primary_client_type="gemini",
        backup_client_type="gemini",
        primary_stream=True,
        backup_stream=True,
    )

    class _FakeStreamAioModels:
        async def generate_content_stream(self, **_kwargs: Any) -> Any:
            async def _gen():
                yield SimpleNamespace(
                    candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text="<think_nya>先想想", thought=False, function_call=None)]), finish_reason=None)],
                    text=None,
                    function_calls=None,
                    usage_metadata=None,
                )
                yield SimpleNamespace(
                    candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text="</think_nya><game>流式完整正文</game>", thought=False, function_call=None)]), finish_reason="STOP")],
                    text=None,
                    function_calls=None,
                    usage_metadata=SimpleNamespace(prompt_token_count=20, candidates_token_count=10, thoughts_token_count=0, total_token_count=30),
                )

            return _gen()

    provider_primary = config_manager.get_model_config().api_providers[0]
    cli = object.__new__(GeminiClient)
    cli.api_provider = provider_primary
    cli.client = SimpleNamespace(aio=SimpleNamespace(models=_FakeStreamAioModels()))  # type: ignore[assignment]
    _register_fake_client(monkeypatch, handlers={"prov_primary": cli, "prov_backup": cli})

    chat_stream = SimpleNamespace(session_id="priv_stream_ok", platform="wechat", is_group_session=False, group_id="", user_id="wxid_owner")
    replyer = MaisakaReplyGenerator(chat_stream=chat_stream, request_type="maisaka.replyer")
    monkeypatch.setattr("src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info", lambda _s: (False, None))
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(tmp_path / "missing.db"))
    msg = _make_session_message(session_id="priv_stream_ok")

    res, _ = await replyer.run_st_single_turn(
        stream_id="priv_stream_ok",
        reply_message=msg,
        chat_history=[],
        reply_tool_args={"msg_id": msg.message_id, "reply_reference": "直接回复"},
    )
    unwrapped = STPresetEngine.unwrap_response(response_text=res.response, tool_calls=res.tool_calls, output_mode="im_clean")
    assert unwrapped.visible_text == "流式完整正文"


# ---------------------------------------------------------------------------
# 2. 同渠道恢复：[空参, 有效直答] -> 主调2次, 备调0次, 首尝试未留 succeeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_channel_recovery_after_empty_soliumbra_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    _make_st_preset_manager(tmp_path, monkeypatch)
    cfg = _setup_sequential_model_config(monkeypatch, primary_max_retry=3, backup_max_retry=2)

    call_log: list[dict[str, Any]] = []
    primary_client = _ScriptedResponseClient(
        cfg.api_providers[0],
        [
            _api_response(tool_calls=[ToolCall(call_id="c1", func_name="soliumbra_reply", args={})]),
            _api_response(
                tool_calls=[
                    ToolCall(
                        call_id="c2",
                        func_name="soliumbra_reply",
                        args={"think_nya": "第二次好了", "game_content": "<game>同渠道恢复成功</game>"},
                    )
                ]
            ),
        ],
        call_log,
    )
    backup_client = _ScriptedResponseClient(
        cfg.api_providers[1],
        [_api_response(tool_calls=[ToolCall(call_id="b1", func_name="soliumbra_reply", args={"think_nya": "备", "game_content": "不该用备"})])],
        call_log,
    )
    _register_fake_client(monkeypatch, handlers={"prov_primary": primary_client, "prov_backup": backup_client})

    chat_stream = SimpleNamespace(session_id="priv_same", platform="wechat", is_group_session=False, group_id="", user_id="wxid_owner")
    replyer = MaisakaReplyGenerator(chat_stream=chat_stream, request_type="maisaka.replyer")
    monkeypatch.setattr("src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info", lambda _s: (False, None))
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(tmp_path / "missing.db"))
    msg = _make_session_message(session_id="priv_same")

    res, _ = await replyer.run_st_single_turn(
        stream_id="priv_same",
        reply_message=msg,
        chat_history=[],
        reply_tool_args={"msg_id": msg.message_id, "reply_reference": "直接回复"},
    )

    assert [c["model"] for c in call_log] == ["gemini-4-hydrogen", "gemini-4-hydrogen"]
    assert len(res.generation_attempts) == 2
    assert res.generation_attempts[0].status != "succeeded"
    assert res.generation_attempts[1].status == "succeeded"
    unwrapped = STPresetEngine.unwrap_response(response_text=res.response, tool_calls=res.tool_calls, output_mode="im_clean")
    assert unwrapped.visible_text == "同渠道恢复成功"


# ---------------------------------------------------------------------------
# 3. 真正的 sequential 回退：[主, 主, 主, 备] + 端到端仅发送一次
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequential_failover_primary_3_times_then_backup_once_and_sends_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    _make_st_preset_manager(tmp_path, monkeypatch)
    cfg = _setup_sequential_model_config(monkeypatch, primary_max_retry=3, backup_max_retry=2)

    call_log: list[dict[str, Any]] = []
    primary_client = _ScriptedResponseClient(
        cfg.api_providers[0],
        [_api_response(tool_calls=[ToolCall(call_id="p_empty", func_name="soliumbra_reply", args={})])],
        call_log,
    )
    backup_client = _ScriptedResponseClient(
        cfg.api_providers[1],
        [
            _api_response(
                tool_calls=[
                    ToolCall(
                        call_id="b_ok",
                        func_name="soliumbra_reply",
                        args={"think_nya": "", "game_content": "备用渠道接手成功"},
                    )
                ]
            )
        ],
        call_log,
    )
    _register_fake_client(monkeypatch, handlers={"prov_primary": primary_client, "prov_backup": backup_client})

    engine, _runtime, msg_old, msg_new, sent_payloads, captured_states = _build_one_pass_st_runtime(
        tmp_path,
        monkeypatch,
        is_group_chat=False,
    )
    decision = TwinTurnDecision(
        abort_turn=False,
        allowed_slots=("sister_younger",),
        recommended_slot="sister_younger",
        sender_trust_role="OWNER",
        is_owner_private=True,
        execution_mode="one_pass_st",
    )
    engine._current_turn_decision = decision

    await engine._execute_one_pass_st_turn(
        trigger_message=msg_new,
        source_messages=[msg_old, msg_new],
        decision=decision,
    )

    assert [c["model"] for c in call_log] == [
        "gemini-4-hydrogen",
        "gemini-4-hydrogen",
        "gemini-4-hydrogen",
        "gemini-4-hydrogen-aistudio",
    ]
    assert len(sent_payloads) == 1
    assert sent_payloads[0]["processed_plain_text"] == "备用渠道接手成功"
    state = captured_states[0]
    assert state.cycle_end.reason == "one_pass_st_end"
    attempts = state.tool_monitor_results[0]["detail"]["generation_attempts"]
    assert len(attempts) == 4
    assert [a["status"] != "succeeded" for a in attempts[:3]] == [True, True, True]
    assert attempts[3]["status"] == "succeeded"
    assert attempts[3]["model"] == "gemini-4-hydrogen-aistudio"


# ---------------------------------------------------------------------------
# 4. 全部耗尽：主备均无效 -> 按预算终止，无 succeeded，发送 0 次
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_models_exhausted_terminates_cleanly_with_zero_sends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    _make_st_preset_manager(tmp_path, monkeypatch)
    cfg = _setup_sequential_model_config(monkeypatch, primary_max_retry=3, backup_max_retry=2)

    call_log: list[dict[str, Any]] = []
    primary_client = _ScriptedResponseClient(
        cfg.api_providers[0],
        [_api_response(tool_calls=[ToolCall(call_id="p_bad", func_name="soliumbra_reply", args={})])],
        call_log,
    )
    backup_client = _ScriptedResponseClient(
        cfg.api_providers[1],
        [_api_response(tool_calls=[ToolCall(call_id="b_bad", func_name="soliumbra_reply", args={"think_nya": "有思考", "game_content": "   "})])],
        call_log,
    )
    _register_fake_client(monkeypatch, handlers={"prov_primary": primary_client, "prov_backup": backup_client})

    engine, _runtime, msg_old, msg_new, sent_payloads, captured_states = _build_one_pass_st_runtime(
        tmp_path,
        monkeypatch,
        is_group_chat=False,
    )
    decision = TwinTurnDecision(execution_mode="one_pass_st")
    engine._current_turn_decision = decision

    with pytest.raises(RespParseException) as exc_info:
        await engine._execute_one_pass_st_turn(
            trigger_message=msg_new,
            source_messages=[msg_old, msg_new],
            decision=decision,
        )

    assert [c["model"] for c in call_log] == [
        "gemini-4-hydrogen",
        "gemini-4-hydrogen",
        "gemini-4-hydrogen",
        "gemini-4-hydrogen-aistudio",
        "gemini-4-hydrogen-aistudio",
    ]
    assert sent_payloads == []
    assert captured_states[0].cycle_end.reason == "cycle_error"
    trace_ctx = getattr(exc_info.value, "generation_trace_context", None)
    assert trace_ctx is not None
    assert all(attempt.status != "succeeded" for attempt in trace_ctx.generation_attempts)


# ---------------------------------------------------------------------------
# 5. 区分结构缺失 vs ST 清洗后空正文，且绝不泄露内部标记
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_response", "expected_reason"),
    [
        (
            _api_response(tool_calls=[ToolCall(call_id="c1", func_name="soliumbra_reply", args={"think_nya": "缺正文键"})]),
            "missing_required_tool_args",
        ),
        (
            _api_response(tool_calls=[ToolCall(call_id="c2", func_name="soliumbra_reply", args={"think_nya": "思考", "game_content": "  \n\t "})]),
            "empty_st_visible_reply",
        ),
        (
            _api_response(
                tool_calls=[
                    ToolCall(
                        call_id="c3",
                        func_name="soliumbra_reply",
                        args={
                            "think_nya": "secret_thought_token_xyz",
                            "game_content": "<think_nya>内部思考</think_nya><summary>场记</summary><comment>猫猫</comment><UpdateVariable><Analyze>ok</Analyze></UpdateVariable>",
                        },
                    )
                ]
            ),
            "empty_st_visible_reply",
        ),
        (
            _api_response(text="<think_nya>只有纯标签思考</think_nya><summary>摘要</summary>"),
            "empty_st_visible_reply",
        ),
        (
            _api_response(tool_calls=[ToolCall(call_id="c_ctx", func_name="context", args={"query": "sync"})]),
            "empty_st_visible_reply",
        ),
    ],
)
async def test_distinguishes_missing_keys_vs_empty_st_visible_reply_without_leaking_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_response: APIResponse,
    expected_reason: str,
) -> None:
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    _make_st_preset_manager(tmp_path, monkeypatch)
    cfg = _setup_sequential_model_config(monkeypatch, primary_max_retry=1, backup_max_retry=1)

    call_log: list[dict[str, Any]] = []
    good_response = _api_response(
        tool_calls=[ToolCall(call_id="ok", func_name="soliumbra_reply", args={"think_nya": "t", "game_content": "干净正文"})]
    )
    primary_client = _ScriptedResponseClient(cfg.api_providers[0], [bad_response], call_log)
    backup_client = _ScriptedResponseClient(cfg.api_providers[1], [good_response], call_log)
    _register_fake_client(monkeypatch, handlers={"prov_primary": primary_client, "prov_backup": backup_client})

    chat_stream = SimpleNamespace(session_id="priv_diff", platform="wechat", is_group_session=False, group_id="", user_id="wxid_owner")
    replyer = MaisakaReplyGenerator(chat_stream=chat_stream, request_type="maisaka.replyer")
    monkeypatch.setattr("src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info", lambda _s: (False, None))
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(tmp_path / "missing.db"))
    msg = _make_session_message(session_id="priv_diff")

    res, _ = await replyer.run_st_single_turn(
        stream_id="priv_diff",
        reply_message=msg,
        chat_history=[],
        reply_tool_args={"msg_id": msg.message_id, "reply_reference": "直接回复"},
    )

    assert [c["model"] for c in call_log] == ["gemini-4-hydrogen", "gemini-4-hydrogen-aistudio"]
    err_msg = str((res.generation_attempts[0].error or {}).get("message") or "")
    assert expected_reason in err_msg
    assert "secret_thought_token_xyz" not in err_msg
    assert "sk-secret" not in err_msg


@pytest.mark.asyncio
async def test_replyer_unwrap_never_retains_raw_internal_tags_when_visible_text_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replyer 解包后若 visible_text 为空，绝不能因保留旧 response_text 而把内部标签当成正文发出。"""
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    _make_st_preset_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(global_config.debug, "save_maisaka_reasoning", False)
    monkeypatch.setattr(global_config.debug, "replyer_retro_prompt", False)
    monkeypatch.setattr("src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info", lambda _s: (False, None))

    chat_stream = SimpleNamespace(session_id="priv_leak_check", platform="wechat", is_group_session=False, group_id="", user_id="wxid_owner")
    replyer = MaisakaReplyGenerator(chat_stream=chat_stream, request_type="maisaka.replyer")
    msg = _make_session_message(session_id="priv_leak_check")

    tag_only_precomputed = LLMResponseResult.from_portable_output(
        response="<think_nya>绝不能发出去的思考</think_nya>\n<summary>绝不能发出去的场记</summary>\n<UpdateVariable><Analyze>x</Analyze></UpdateVariable>",
        model_name="gemini-4-hydrogen",
    )
    success, reply_res = await replyer.generate_reply_with_context(
        stream_id="priv_leak_check",
        reply_message=msg,
        chat_history=[],
        reply_tool_args={
            "msg_id": msg.message_id,
            "reply_reference": "测试",
            "_precomputed_st_response": tag_only_precomputed,
        },
        log_reply=False,
    )

    assert success is False
    assert reply_res.success is False
    assert reply_res.completion.response_text == ""


# ---------------------------------------------------------------------------
# 6. 正常功能不退化：空 think_nya、纯文本直答、无 required 零参工具、非 game_content schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_direct_reply_variants_and_dynamic_register_part_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    st_mgr = _make_st_preset_manager(tmp_path, monkeypatch)
    cfg = _setup_sequential_model_config(monkeypatch, primary_max_retry=2, backup_max_retry=1)

    # 6a. 允许 think_nya 为空串、无顶层正文、无 thought_signature；以及纯文本直答
    for valid_resp, expected_text in [
        (
            _api_response(tool_calls=[ToolCall(call_id="c1", func_name="soliumbra_reply", args={"think_nya": "", "game_content": "没签名也能发"})]),
            "没签名也能发",
        ),
        (
            _api_response(text="纯文本直答也没问题"),
            "纯文本直答也没问题",
        ),
    ]:
        call_log: list[dict[str, Any]] = []
        primary_client = _ScriptedResponseClient(cfg.api_providers[0], [valid_resp], call_log)
        backup_client = _ScriptedResponseClient(cfg.api_providers[1], [valid_resp], call_log)
        _register_fake_client(monkeypatch, handlers={"prov_primary": primary_client, "prov_backup": backup_client})

        engine, _runtime, msg_old, msg_new, sent_payloads, captured_states = _build_one_pass_st_runtime(
            tmp_path,
            monkeypatch,
            is_group_chat=False,
        )
        decision = TwinTurnDecision(execution_mode="one_pass_st")
        engine._current_turn_decision = decision
        await engine._execute_one_pass_st_turn(trigger_message=msg_new, source_messages=[msg_old, msg_new], decision=decision)
        assert [c["model"] for c in call_log] == ["gemini-4-hydrogen"]
        assert len(sent_payloads) == 1
        assert sent_payloads[0]["processed_plain_text"] == expected_text
        assert captured_states[0].cycle_end.reason == "one_pass_st_end"

    # 6b. 动态非默认 schema：使用 registerPart:story_body，无 game_content 键；证明非硬编码
    custom_preset = {
        "temperature": 0.9,
        "prompts": [
            {
                "identifier": "init_tok",
                "name": "init",
                "enabled": True,
                "role": "system",
                "content": "<run_tool_replacement><<<|catgodml|registerPart:story_body:剧场正文>>>",
            },
            {"identifier": "chatHistory", "name": "Chat History", "enabled": True, "role": "system", "marker": True},
        ],
        "prompt_order": [
            {
                "character_id": 100001,
                "order": [
                    {"identifier": "init_tok", "enabled": True},
                    {"identifier": "chatHistory", "enabled": True},
                ],
            }
        ],
    }
    (tmp_path / "presets" / "CustomStoryBody.json").write_text(json.dumps(custom_preset, ensure_ascii=False), encoding="utf-8")
    st_mgr.save_config(
        {
            "enabled": True,
            "active_preset": "CustomStoryBody",
            "output_mode": "im_clean",
            "protocol_mode": "spreset_tool_envelope",
        }
    )

    call_log2: list[dict[str, Any]] = []
    # 主渠道误用默认 game_content 而缺少实际的必填字段 story_body -> 必须失败；备用使用 story_body -> 成功
    wrong_schema_resp = _api_response(
        tool_calls=[ToolCall(call_id="w1", func_name="soliumbra_reply", args={"think_nya": "t", "game_content": "错字段"})]
    )
    right_schema_resp = _api_response(
        tool_calls=[ToolCall(call_id="r1", func_name="soliumbra_reply", args={"think_nya": "t", "story_body": "动态字段正文"})]
    )
    primary_client2 = _ScriptedResponseClient(cfg.api_providers[0], [wrong_schema_resp], call_log2)
    backup_client2 = _ScriptedResponseClient(cfg.api_providers[1], [right_schema_resp], call_log2)
    _register_fake_client(monkeypatch, handlers={"prov_primary": primary_client2, "prov_backup": backup_client2})

    engine2, _runtime2, msg_old2, msg_new2, sent_payloads2, _states2 = _build_one_pass_st_runtime(
        tmp_path,
        monkeypatch,
        is_group_chat=False,
    )
    decision2 = TwinTurnDecision(execution_mode="one_pass_st")
    engine2._current_turn_decision = decision2
    await engine2._execute_one_pass_st_turn(trigger_message=msg_new2, source_messages=[msg_old2, msg_new2], decision=decision2)

    assert [c["model"] for c in call_log2] == [
        "gemini-4-hydrogen",
        "gemini-4-hydrogen",
        "gemini-4-hydrogen-aistudio",
    ]
    assert len(sent_payloads2) == 1
    assert sent_payloads2[0]["processed_plain_text"] == "动态字段正文"


# ---------------------------------------------------------------------------
# 7. 工具副作用不重放、并发/共存分派优先级、暂停工具、未知工具
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_step_tool_does_not_replay_on_final_generation_retry_and_failover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    _make_st_preset_manager(tmp_path, monkeypatch)
    cfg = _setup_sequential_model_config(monkeypatch, primary_max_retry=2, backup_max_retry=1)

    tool_invocations: list[dict[str, Any]] = []

    class _FakeMemoryProvider(ToolProvider):
        provider_name = "test_memory_reg"
        provider_type = "builtin"

        async def list_tools(self, context: Any = None) -> list[ToolSpec]:
            return [
                ToolSpec(
                    name="query_memory",
                    description="查询长期记忆",
                    parameters_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                    provider_name=self.provider_name,
                    provider_type=self.provider_type,
                )
            ]

        async def invoke(self, invocation: Any, context: Any = None) -> ToolExecutionResult:
            tool_invocations.append({"name": invocation.tool_name, "args": dict(invocation.arguments)})
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=True,
                content="命中记忆：根津巷口买过埃塞手冲豆。",
            )

        async def close(self) -> None:
            return None

    call_log: list[dict[str, Any]] = []
    # Round 0: 主渠道返回外部工具 + 同时携带一个空 game_content 的 soliumbra_reply（工具优先分派）
    # Pass 2: 主渠道连续2次返回空参 soliumbra_reply({}) -> 切备用返回有效正文
    round0_tool_resp = _api_response(
        tool_calls=[
            ToolCall(call_id="qm_1", func_name="query_memory", args={"query": "根津"}),
            ToolCall(call_id="sr_co", func_name="soliumbra_reply", args={"think_nya": "查一下", "game_content": ""}),
        ]
    )
    pass2_bad_resp = _api_response(tool_calls=[ToolCall(call_id="p2_bad", func_name="soliumbra_reply", args={})])
    pass2_ok_resp = _api_response(
        tool_calls=[ToolCall(call_id="p2_ok", func_name="soliumbra_reply", args={"think_nya": "拿到观察啦", "game_content": "记得根津那家手冲豆！"})]
    )
    primary_client = _ScriptedResponseClient(
        cfg.api_providers[0],
        [round0_tool_resp, pass2_bad_resp, pass2_bad_resp],
        call_log,
    )
    backup_client = _ScriptedResponseClient(cfg.api_providers[1], [pass2_ok_resp], call_log)
    _register_fake_client(monkeypatch, handlers={"prov_primary": primary_client, "prov_backup": backup_client})

    engine, _runtime, msg_old, msg_new, sent_payloads, captured_states = _build_one_pass_st_runtime(
        tmp_path,
        monkeypatch,
        is_group_chat=False,
        extra_tools=[_FakeMemoryProvider()],
    )
    decision = TwinTurnDecision(execution_mode="one_pass_st")
    engine._current_turn_decision = decision

    await engine._execute_one_pass_st_turn(
        trigger_message=msg_new,
        source_messages=[msg_old, msg_new],
        decision=decision,
    )

    assert len(tool_invocations) == 1
    assert [c["model"] for c in call_log] == [
        "gemini-4-hydrogen",
        "gemini-4-hydrogen",
        "gemini-4-hydrogen",
        "gemini-4-hydrogen-aistudio",
    ]
    # 确认备用渠道在 Pass 2 重试时仍收到第一次执行的 tool_observations
    backup_sync_output = str(getattr(call_log[-1]["context_items"][-1], "output", ""))
    assert "tool_observations" in backup_sync_output
    assert "根津巷口买过埃塞手冲豆" in backup_sync_output
    assert len(sent_payloads) == 1
    assert sent_payloads[0]["processed_plain_text"] == "记得根津那家手冲豆！"
    assert captured_states[0].cycle_end.reason == "one_pass_st_end"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "tool_args", "expected_reason"),
    [
        ("stop", {}, "planner_no_tool_end"),
        ("wait", {"seconds": 15}, "tool_pause:wait"),
    ],
)
async def test_pause_tools_without_text_succeed_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    tool_args: dict[str, Any],
    expected_reason: str,
) -> None:
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    _make_st_preset_manager(tmp_path, monkeypatch)
    cfg = _setup_sequential_model_config(monkeypatch, primary_max_retry=2, backup_max_retry=1)

    call_log: list[dict[str, Any]] = []
    pause_resp = _api_response(tool_calls=[ToolCall(call_id="p1", func_name=tool_name, args=tool_args)])
    primary_client = _ScriptedResponseClient(cfg.api_providers[0], [pause_resp], call_log)
    backup_client = _ScriptedResponseClient(cfg.api_providers[1], [pause_resp], call_log)
    _register_fake_client(monkeypatch, handlers={"prov_primary": primary_client, "prov_backup": backup_client})

    engine, _runtime, msg_old, msg_new, sent_payloads, captured_states = _build_one_pass_st_runtime(
        tmp_path,
        monkeypatch,
        is_group_chat=True,
    )
    decision = TwinTurnDecision(execution_mode="one_pass_st")
    engine._current_turn_decision = decision

    await engine._execute_one_pass_st_turn(
        trigger_message=msg_new,
        source_messages=[msg_old, msg_new],
        decision=decision,
    )

    assert [c["model"] for c in call_log] == ["gemini-4-hydrogen"]
    assert sent_payloads == []
    assert captured_states[0].cycle_end.reason == expected_reason


# ---------------------------------------------------------------------------
# 8. 下游防错边界、非 ST 隔离与校验策略不泄露到 wire / snapshot / 缓存键
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_pass_st_downstream_boundary_blocks_invalid_precomputed_without_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    _make_st_preset_manager(tmp_path, monkeypatch)

    engine, runtime, msg_old, msg_new, sent_payloads, captured_states = _build_one_pass_st_runtime(
        tmp_path,
        monkeypatch,
        is_group_chat=False,
    )
    replyer = replyer_manager._repliers[f"maisaka:{runtime.session_id}"]
    bad_llm_res = LLMResponseResult.from_portable_output(
        tool_calls=[ToolCall(call_id="bad_stub", func_name="soliumbra_reply", args={})],
        model_name="gemini-4-hydrogen",
    )
    compiled_stub = SimpleNamespace(
        tool_definitions=[
            {
                "name": "soliumbra_reply",
                "description": "reply",
                "parameters_schema": {"type": "object", "properties": {"think_nya": {"type": "string"}, "game_content": {"type": "string"}}, "required": ["think_nya", "game_content"]},
            }
        ]
    )
    run_single_mock = AsyncMock(return_value=(bad_llm_res, compiled_stub))
    monkeypatch.setattr(replyer, "run_st_single_turn", run_single_mock)
    fake_execute_reply = AsyncMock()
    monkeypatch.setattr("src.maisaka.reasoning_engine.execute_reply", fake_execute_reply)

    decision = TwinTurnDecision(execution_mode="one_pass_st")
    engine._current_turn_decision = decision
    await engine._execute_one_pass_st_turn(trigger_message=msg_new, source_messages=[msg_old, msg_new], decision=decision)

    run_single_mock.assert_awaited_once()
    fake_execute_reply.assert_not_awaited()
    assert sent_payloads == []
    assert captured_states[0].cycle_end.reason == "one_pass_st_failed"
    assert "missing_required_tool_args" in captured_states[0].tool_result_summaries[0]


@pytest.mark.asyncio
async def test_isolation_planner_lenient_raw_preset_and_no_wire_or_snapshot_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_runtime_side_effects(tmp_path, monkeypatch)
    cfg = _setup_sequential_model_config(monkeypatch, primary_max_retry=1, backup_max_retry=1)

    # 8a. 普通请求（未启用严格必填检查）允许 Planner reply 缺省 msg_id 到达下游回退
    call_log: list[dict[str, Any]] = []
    lenient_reply_resp = _api_response(
        tool_calls=[ToolCall(call_id="pl1", func_name="reply", args={"reply_reference": "只提供参考"})]
    )
    primary_client = _ScriptedResponseClient(cfg.api_providers[0], [lenient_reply_resp], call_log)
    backup_client = _ScriptedResponseClient(cfg.api_providers[1], [lenient_reply_resp], call_log)
    _register_fake_client(monkeypatch, handlers={"prov_primary": primary_client, "prov_backup": backup_client})

    planner_client = LLMServiceClient(task_name="planner", request_type="maisaka.planner", session_id="s_planner")
    reply_tool_def = {
        "name": "reply",
        "description": "reply",
        "parameters_schema": {
            "type": "object",
            "properties": {"msg_id": {"type": "string"}, "reply_reference": {"type": "string"}},
            "required": ["msg_id", "reply_reference"],
        },
    }
    res_planner = await planner_client.generate_response_with_context(
        context_factory=lambda _c: [ContextItemBuilder().set_role(RoleType.User).add_text_content("hi").build()],
        options=LLMGenerationOptions(tool_options=[reply_tool_def]),
    )
    assert res_planner.tool_calls is not None
    assert res_planner.tool_calls[0].func_name == "reply"

    # 8b. raw_preset ST 模式允许只返回 <think_nya>，不误伤其他 ST 输出模式
    _make_st_preset_manager(tmp_path, monkeypatch, output_mode="raw_preset")
    raw_preset_resp = _api_response(text="<think_nya>原始模式允许</think_nya>")
    primary_raw = _ScriptedResponseClient(cfg.api_providers[0], [raw_preset_resp], call_log)
    _register_fake_client(monkeypatch, handlers={"prov_primary": primary_raw, "prov_backup": primary_raw})
    chat_stream = SimpleNamespace(session_id="priv_raw", platform="wechat", is_group_session=False, group_id="", user_id="wxid_owner")
    replyer = MaisakaReplyGenerator(chat_stream=chat_stream, request_type="maisaka.replyer")
    monkeypatch.setattr("src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info", lambda _s: (False, None))
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(tmp_path / "missing.db"))
    msg = _make_session_message(session_id="priv_raw")
    res_raw, _ = await replyer.run_st_single_turn(
        stream_id="priv_raw",
        reply_message=msg,
        chat_history=[],
        reply_tool_args={"msg_id": msg.message_id, "reply_reference": "raw"},
    )
    assert "<think_nya>原始模式允许</think_nya>" in res_raw.response

    # 8c. 校验策略不进入 extra_params、快照、缓存键，也不污染后续普通请求
    _make_st_preset_manager(tmp_path, monkeypatch, output_mode="im_clean")
    call_log.clear()
    bad_resp = _api_response(tool_calls=[ToolCall(call_id="s1", func_name="soliumbra_reply", args={})])
    ok_resp = _api_response(
        tool_calls=[ToolCall(call_id="s2", func_name="soliumbra_reply", args={"think_nya": "t", "game_content": "ok"})]
    )
    _register_fake_client(
        monkeypatch,
        handlers={
            "prov_primary": _ScriptedResponseClient(cfg.api_providers[0], [bad_resp], call_log),
            "prov_backup": _ScriptedResponseClient(cfg.api_providers[1], [ok_resp], call_log),
        },
    )
    await replyer.run_st_single_turn(
        stream_id="priv_raw",
        reply_message=msg,
        chat_history=[],
        reply_tool_args={"msg_id": msg.message_id, "reply_reference": "st"},
    )
    for sent in call_log:
        assert "validate_required_tool_args" not in sent["extra_params"]
        assert "response_validator" not in sent["extra_params"]

    snapshot_files = list((tmp_path / "maisaka_prompt" / "llm_error").rglob("*.json"))
    assert snapshot_files
    snap_data = read_request_snapshot(snapshot_files[0])
    snap_text = json.dumps(snap_data, ensure_ascii=False)
    assert "validate_required_tool_args" not in snap_text
    assert "response_validator" not in snap_text

    req = ResponseRequest(
        model_info=cfg.models[0],
        context_items=[ContextItemBuilder().set_role(RoleType.User).add_text_content("hi").build()],
        tool_options=[_soliumbra_tool_option()],
        validate_required_tool_args=True,
        response_validator=lambda _req, _resp: None,
    )
    snap = serialize_response_request_snapshot(req)
    assert "validate_required_tool_args" not in snap
    assert "response_validator" not in snap
    assert req.detached_snapshot().response_validator is None

    # 同一个 replyer 切回非 ST 普通请求后不会残留上一次 ST 校验策略
    import src.companion_vault.st_preset_engine as st_mod

    st_mod._GLOBAL_ST_MANAGER.save_config({"enabled": False})
    call_log.clear()
    normal_client = _ScriptedResponseClient(
        cfg.api_providers[0],
        [_api_response(text="<think>普通模式允许</think>")],
        call_log,
    )
    _register_fake_client(monkeypatch, handlers={"prov_primary": normal_client, "prov_backup": normal_client})
    orchestrator = LLMOrchestrator(task_name="replyer", request_type="maisaka.replyer", session_id="priv_raw")
    res_plain = await orchestrator.generate_response_async("hi")
    assert res_plain.response == "<think>普通模式允许</think>"
