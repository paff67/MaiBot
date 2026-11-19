"""ContextBinaryPart 序列化、客户端转换与 asset_encoder 小文件/PDF/短视频内联测试。"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest

from src.companion_vault.asset_encoder import SMALL_INLINE_THRESHOLD_BYTES, encode_inline_descriptor
from src.companion_vault.wechat_recall import quote_context_item
from src.llm_models.model_client.gemini_client import _build_non_tool_parts
from src.llm_models.model_client.openai_client import _convert_user_message_content
from src.llm_models.model_client.openai_responses_client import _convert_user_content
from src.llm_models.payload_content.context_item import ContextBinaryPart, ContextItemBuilder, RoleType
from src.llm_models.payload_content.context_snapshot import _deserialize_content_parts, _serialize_content_parts


def test_context_binary_part_roundtrip_and_client_conversions() -> None:
    raw_pdf = b"%PDF-1.7-test-payload"
    b64_pdf = base64.b64encode(raw_pdf).decode("ascii")

    item = (
        ContextItemBuilder()
        .set_role(RoleType.User)
        .add_text_content("请读取这份文档")
        .add_binary_base64_part(b64_pdf, mime_type="application/pdf", file_code="FM0042", semantic_name="report.pdf")
        .build()
    )
    assert any(isinstance(p, ContextBinaryPart) for p in item.parts)

    serialized = _serialize_content_parts(item.parts)
    deserialized = _deserialize_content_parts(serialized)
    restored = next(p for p in deserialized if isinstance(p, ContextBinaryPart))
    assert restored.data_base64 == b64_pdf
    assert restored.mime_type == "application/pdf"
    assert restored.file_code == "FM0042"
    assert restored.semantic_name == "report.pdf"

    gemini_parts = _build_non_tool_parts(item)
    assert len(gemini_parts) == 2
    assert gemini_parts[1].inline_data is not None
    assert gemini_parts[1].inline_data.mime_type == "application/pdf"
    assert gemini_parts[1].inline_data.data == raw_pdf

    openai_content = _convert_user_message_content(item)
    assert isinstance(openai_content, list)
    assert any("report.pdf" in part["text"] and "application/pdf" in part["text"] for part in openai_content)

    responses_content = _convert_user_content(item)
    assert any("report.pdf" in part["text"] and "application/pdf" in part["text"] for part in responses_content)


def test_encode_inline_descriptor_pdf_video_and_small_file_fastpath(monkeypatch: pytest.MonkeyPatch) -> None:
    fail_client = MagicMock()
    fail_client.files.upload.side_effect = AssertionError("不应在此分支调用 Files API")

    # 1. MAIBOT_FILES_API_ENABLED=0 时，较大的 PDF 和 MP4（<=15MB）均走 binary_base64
    monkeypatch.setenv("MAIBOT_FILES_API_ENABLED", "0")
    large_pdf = b"%PDF-1.7" + (b"\x00" * (SMALL_INLINE_THRESHOLD_BYTES + 1024))
    pdf_desc, pdf_uri, _ = encode_inline_descriptor(
        file_code="FM0101",
        semantic_name="large.pdf",
        raw_bytes=large_pdf,
        mime_type="application/pdf",
        client=fail_client,
    )
    assert pdf_uri is None
    assert pdf_desc["mime_type"] == "application/pdf"
    assert base64.b64decode(pdf_desc["binary_base64"]) == large_pdf

    large_mp4 = b"\x00\x00\x00\x18ftypmp42" + (b"\x01" * (SMALL_INLINE_THRESHOLD_BYTES + 1024))
    mp4_desc, mp4_uri, _ = encode_inline_descriptor(
        file_code="FM0102",
        semantic_name="clip.mp4",
        raw_bytes=large_mp4,
        mime_type="video/mp4",
        client=fail_client,
    )
    assert mp4_uri is None
    assert mp4_desc["mime_type"] == "video/mp4"
    assert base64.b64decode(mp4_desc["binary_base64"]) == large_mp4
    fail_client.files.upload.assert_not_called()

    # 2. 即使 MAIBOT_FILES_API_ENABLED=1 且传入 client，<=512KB 小文件仍直接走内联不调 Files API
    monkeypatch.setenv("MAIBOT_FILES_API_ENABLED", "1")
    small_audio = b"RIFF" + (b"\x02" * 1024)
    aud_desc, _, _ = encode_inline_descriptor(
        file_code="FM0103",
        semantic_name="short.wav",
        raw_bytes=small_audio,
        mime_type="audio/wav",
        client=fail_client,
    )
    assert "audio_base64" in aud_desc

    small_pdf = b"%PDF-1.4-small"
    spdf_desc, _, _ = encode_inline_descriptor(
        file_code="FM0104",
        semantic_name="tiny.pdf",
        raw_bytes=small_pdf,
        mime_type="application/pdf",
        client=fail_client,
    )
    assert base64.b64decode(spdf_desc["binary_base64"]) == small_pdf
    fail_client.files.upload.assert_not_called()


def test_quote_context_item_handles_binary_legacy_pdf_and_unknown_without_keyerror() -> None:
    pdf_b64 = base64.b64encode(b"%PDF-1.7-quote").decode("ascii")
    mp4_b64 = base64.b64encode(b"\x00\x00\x00\x18ftypisom").decode("ascii")
    item = quote_context_item(
        "引用与未知描述符测试",
        [
            {"file_code": "FM1", "semantic_name": "new.pdf", "binary_base64": pdf_b64, "mime_type": "application/pdf"},
            {"file_code": "FM2", "semantic_name": "legacy.pdf", "pdf_base64": pdf_b64, "mime_type": "application/pdf"},
            {"file_code": "FM3", "semantic_name": "clip.mp4", "binary_base64": mp4_b64, "mime_type": "video/mp4"},
            {"file_code": "FM4", "semantic_name": "opaque.bin", "mime_type": "application/octet-stream"},
        ],
    )
    binaries = [p for p in item.parts if isinstance(p, ContextBinaryPart)]
    assert len(binaries) == 3
    assert binaries[0].data_base64 == pdf_b64
    assert binaries[1].file_code == "FM2"
    assert binaries[2].mime_type == "video/mp4"
    assert any("opaque.bin" in getattr(p, "text", "") for p in item.parts)
