"""STPresetEngine 与 MaisakaGeneratorBase 召回/工具调取/纯媒体末轮多模态闭合回归测试。"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator
from src.companion_vault.st_preset_engine import STCompileContext, STHistoryTurn, STPresetEngine
from src.llm_models.payload_content.context_item import (
    ContextAudioPart,
    ContextBinaryPart,
    ContextImagePart,
    ContextTextPart,
    FunctionCallItem,
    FunctionCallOutputItem,
    UserMessageItem,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PRESET_PATH = REPO_ROOT / "plugins" / "st_preset_engine" / "templates" / "Meowssiah-WeChat-IM.json"


def _assert_gemini_function_turn_order(items) -> None:
    """确认 spreset_tool_envelope 的 FunctionCall -> FunctionCallOutput 配对未被插队破坏。"""
    for idx, item in enumerate(items):
        if isinstance(item, FunctionCallItem):
            assert idx + 1 < len(items)
            nxt = items[idx + 1]
            assert isinstance(nxt, FunctionCallOutputItem)
            assert nxt.call_id == item.tool_call.call_id


def test_st_preset_preserves_recalled_and_popped_last_media_parts() -> None:
    engine = STPresetEngine.from_file(TEMPLATE_PRESET_PATH)
    img_b64 = base64.b64encode(b"\xff\xd8\xff\xe0fakejpeg").decode("ascii")
    pdf_b64 = base64.b64encode(b"%PDF-1.7-fake").decode("ascii")
    aud_b64 = base64.b64encode(b"RIFF_WAV").decode("ascii")

    # 1. spreset_tool_envelope: 纯媒体末轮（text=""）弹出后保留 media_parts，且 recalled_media_parts 也一并挂载在 call_sync_final 前
    ctx_envelope = STCompileContext(
        chat_history=[
            STHistoryTurn(role="user", text="", media_parts=[ContextImagePart("jpeg", img_b64)]),
        ],
        last_user_message="",
        protocol_mode="spreset_tool_envelope",
        client_type="gemini",
        output_mode="im_clean",
        recalled_media_parts=[
            {"file_code": "FM0010", "semantic_name": "doc.pdf", "binary_base64": pdf_b64, "mime_type": "application/pdf"},
            {"file_code": "FM0011", "semantic_name": "voice.wav", "audio_base64": aud_b64, "mime_type": "audio/wav"},
            {"file_code": "FM0012", "semantic_name": "sheet.xlsx", "text": "col1,col2\n1,2"},
        ],
    )
    compiled_env = engine.compile_to_context_items(ctx_envelope)
    assert compiled_env.protocol == "spreset_tool_envelope"
    _assert_gemini_function_turn_order(compiled_env.context_items)

    all_parts = [p for it in compiled_env.context_items if isinstance(it, UserMessageItem) for p in it.parts]
    assert any(isinstance(p, ContextImagePart) and p.image_base64 == img_b64 for p in all_parts)
    assert any(isinstance(p, ContextBinaryPart) and p.data_base64 == pdf_b64 and p.file_code == "FM0010" for p in all_parts)
    assert any(isinstance(p, ContextAudioPart) and p.audio_base64 == aud_b64 and p.file_code == "FM0011" for p in all_parts)
    assert any(isinstance(p, ContextTextPart) and "col1,col2" in p.text for p in all_parts)

    sync_idx = next(
        i for i, it in enumerate(compiled_env.context_items) if isinstance(it, FunctionCallItem) and it.tool_call.call_id == "call_sync_final"
    )
    media_idx = next(
        i
        for i, it in enumerate(compiled_env.context_items)
        if isinstance(it, UserMessageItem) and any(isinstance(p, ContextBinaryPart) for p in it.parts)
    )
    assert media_idx < sync_idx

    # 2. standard_chat: 同样保留 recalled_media_parts
    ctx_std = STCompileContext(
        chat_history=[STHistoryTurn(role="user", text="帮我看下引用文件")],
        last_user_message="帮我看下引用文件",
        protocol_mode="standard_chat",
        client_type="openai",
        recalled_media_parts=[
            {"file_code": "FM0020", "semantic_name": "ref.pdf", "binary_base64": pdf_b64, "mime_type": "application/pdf"},
        ],
    )
    compiled_std = engine.compile_to_context_items(ctx_std)
    assert compiled_std.protocol == "standard_chat"
    assert any(
        isinstance(p, ContextBinaryPart) and p.file_code == "FM0020"
        for it in compiled_std.context_items
        if isinstance(it, UserMessageItem)
        for p in it.parts
    )


def test_replyer_st_compile_forwards_recalled_and_tool_observation_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.companion_vault.st_preset_engine as st_mod

    presets_dir = tmp_path / "presets"
    st_mgr = STPresetRuntimeManager_for_test(presets_dir)
    monkeypatch.setattr(st_mod, "_GLOBAL_ST_MANAGER", st_mgr)

    gen = object.__new__(BaseMaisakaReplyGenerator)
    gen.express_model = SimpleNamespace(task_name="replyer")
    gen.chat_stream = None
    gen._personality_service = SimpleNamespace(get_personality_prompt=lambda: "测试人设")
    gen._last_persona_v2_turn = None
    gen._last_is_owner_private = False

    pdf_b64 = base64.b64encode(b"%PDF-1.7-obs").decode("ascii")
    img_b64 = base64.b64encode(b"\xff\xd8\xff\xe0obsjpeg").decode("ascii")

    # 刻意传入重复的 FM8405 验证去重逻辑生效
    items = gen._compile_st_preset_for_replyer(
        chat_history=[],
        reply_message=None,
        reply_reason="测试",
        enable_visual_message=True,
        reply_tool_args={
            "_recalled_file_parts": [
                {"file_code": "FM8405", "semantic_name": "a.jpg", "image_base64": img_b64, "image_format": "jpg", "mime_type": "image/jpeg", "part_index": 0},
            ],
            "_tool_observation_file_parts": [
                {"file_code": "FM8405", "semantic_name": "a.jpg", "image_base64": img_b64, "image_format": "jpg", "mime_type": "image/jpeg", "part_index": 0},
                {"file_code": "FM8406", "semantic_name": "b.pdf", "binary_base64": pdf_b64, "mime_type": "application/pdf", "part_index": 0},
            ],
        },
    )
    img_parts = [p for it in items if isinstance(it, UserMessageItem) for p in it.parts if isinstance(p, ContextImagePart)]
    bin_parts = [p for it in items if isinstance(it, UserMessageItem) for p in it.parts if isinstance(p, ContextBinaryPart)]
    assert len(img_parts) == 1
    assert len(bin_parts) == 1
    assert bin_parts[0].file_code == "FM8406"


def STPresetRuntimeManager_for_test(presets_dir: Path):
    from src.companion_vault.st_preset_engine import STPresetRuntimeManager

    mgr = STPresetRuntimeManager(presets_dir=presets_dir)
    mgr.save_config(
        {
            "enabled": True,
            "active_preset": "Meowssiah-WeChat-IM",
            "output_mode": "im_clean",
            "protocol_mode": "spreset_tool_envelope",
        }
    )
    return mgr
