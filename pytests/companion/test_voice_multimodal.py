import base64
import io
import time
from types import SimpleNamespace
from google.genai import types

from src.companion_vault.truth_db import CompanionTruthDB
from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.model_assets import build_original_parts
from src.companion_vault.quote_recall import load_original_descriptors
from src.llm_models.payload_content.context_item import (
    ContextItemBuilder,
    ContextAudioPart,
    RoleType,
    UserMessageItem,
)
from src.llm_models.payload_content.context_snapshot import (
    _serialize_content_parts,
    _deserialize_content_parts,
)
from src.llm_models.model_client.gemini_client import _build_non_tool_parts
from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator


def test_audio_inline_multimodal_pipeline(tmp_path):
    # 1. Setup DB & Asset Pipeline
    db = CompanionTruthDB(str(tmp_path / "truth.db"))
    assets = AssetPipeline(db, str(tmp_path / "assets"))

    # Create dummy WAV bytes (500KB - 2MB simulating WeChat voice message)
    wav_payload = b"RIFF" + b"\x00" * 600000

    # Ingest silk original
    silk_asset = assets.ingest(
        b"SILK_RAW_BYTES",
        {"mime_type": "audio/x-silk", "relative_path": "voice/msg.amr"},
        source_id="msg-1",
    )
    # Ingest decoded lossless WAV derivative
    wav_asset = assets.ingest(
        wav_payload,
        {
            "mime_type": "audio/wav",
            "relative_path": "voice/msg.wav",
            "representation": "decoded_lossless",
            "original_file_code": silk_asset["file_code"],
        },
        source_id="msg-1:wav",
    )

    # 2. Test build_original_parts returns inline bytes part without Files API
    parts = build_original_parts(assets, silk_asset["file_code"], client=None)
    assert len(parts) == 1
    assert parts[0].inline_data is not None
    assert parts[0].inline_data.mime_type == "audio/wav"
    assert parts[0].inline_data.data == wav_payload

    # 3. Test load_original_descriptors produces audio_base64 descriptor without client.files.upload
    descriptors = load_original_descriptors(assets, [wav_asset], client=None)
    assert len(descriptors) == 1
    desc = descriptors[0]
    assert desc["file_code"] == wav_asset["file_code"]
    assert desc["mime_type"] == "audio/wav"
    assert "audio_base64" in desc
    assert base64.b64decode(desc["audio_base64"]) == wav_payload

    # 3b. Test failing client (e.g. 404 from third party proxy) gracefully falls back to audio_base64
    class FailingFiles:
        def upload(self, **kwargs):
            raise RuntimeError("404 Not Found from proxy")

    fallback_desc = load_original_descriptors(assets, [wav_asset], client=SimpleNamespace(files=FailingFiles()))
    assert len(fallback_desc) == 1
    assert "audio_base64" in fallback_desc[0]
    assert base64.b64decode(fallback_desc[0]["audio_base64"]) == wav_payload

    # 4. Test ContextItemBuilder with audio
    builder = ContextItemBuilder().set_role(RoleType.User)
    builder.add_text_content("这是一条语音消息：")
    builder.add_audio_base64_part(desc["audio_base64"], mime_type=desc["mime_type"], file_code=desc["file_code"])
    item = builder.build()
    assert isinstance(item, UserMessageItem)
    assert any(isinstance(p, ContextAudioPart) for p in item.parts)

    # 5. Test context snapshot roundtrip
    serialized = _serialize_content_parts(item.parts)
    deserialized = _deserialize_content_parts(serialized)
    assert len(deserialized) == len(item.parts)
    audio_part = next(p for p in deserialized if isinstance(p, ContextAudioPart))
    assert audio_part.audio_base64 == desc["audio_base64"]
    assert audio_part.mime_type == "audio/wav"

    # 6. Test Gemini client _build_non_tool_parts converts ContextAudioPart to Part.from_bytes
    gemini_parts = _build_non_tool_parts(item)
    assert len(gemini_parts) == 2
    assert gemini_parts[1].inline_data.mime_type == "audio/wav"
    assert gemini_parts[1].inline_data.data == wav_payload

    # 7. Test _build_recalled_messages handles audio_base64 without KeyError
    recalled_items = BaseMaisakaReplyGenerator._build_recalled_messages(
        {"_recalled_file_parts": descriptors}
    )
    assert len(recalled_items) == 1
    assert any(isinstance(p, ContextAudioPart) for p in recalled_items[0].parts)

    db.close()
