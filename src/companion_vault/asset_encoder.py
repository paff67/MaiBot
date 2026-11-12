"""统一多模态原件双通道编码器（Files API 轮询 + <=15MB 零延迟内联直通）。"""

from __future__ import annotations

import base64
import io
import os
import time
from pathlib import Path
from typing import Any

INLINE_MAX_BYTES = 15 * 1024 * 1024
SMALL_INLINE_THRESHOLD_BYTES = 512 * 1024


def is_files_api_enabled(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    raw = os.environ.get("MAIBOT_FILES_API_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def upload_to_files_api_with_polling(
    client: Any,
    source: str | Path | bytes,
    *,
    mime_type: str,
    timeout_sec: float = 120.0,
    poll_interval_sec: float = 1.5,
) -> tuple[str, float]:
    file_arg: Any = io.BytesIO(source) if isinstance(source, bytes) else str(source)
    uploaded = client.files.upload(file=file_arg, config={"mime_type": mime_type})
    deadline = time.monotonic() + timeout_sec
    while getattr(uploaded, "state", None) and uploaded.state.name == "PROCESSING":
        if time.monotonic() >= deadline:
            raise TimeoutError("Files API 处理超时")
        time.sleep(poll_interval_sec)
        uploaded = client.files.get(name=uploaded.name)
    if getattr(uploaded, "state", None) and uploaded.state.name == "FAILED":
        raise ValueError("Files API 处理失败")
    uri = getattr(uploaded, "uri", None)
    if not uri:
        raise ValueError("Files API 未返回有效 URI")
    exp_dt = getattr(uploaded, "expiration_time", None)
    expiry = exp_dt.timestamp() if exp_dt is not None else (time.time() + 47 * 3600)
    return str(uri), float(expiry)


def encode_media_part(
    path: Path,
    *,
    mime_type: str,
    client: Any = None,
    cached_uri: str = "",
    uri_expires_at: float = 0.0,
    files_api_enabled: bool | None = None,
) -> tuple[Any, str | None, float | None]:
    from google.genai import types

    size = path.stat().st_size
    allow_files = is_files_api_enabled(files_api_enabled)

    if size <= SMALL_INLINE_THRESHOLD_BYTES or (not allow_files and size <= INLINE_MAX_BYTES):
        return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type), None, None

    if allow_files and cached_uri and uri_expires_at > time.time() + 600:
        return types.Part.from_uri(file_uri=cached_uri, mime_type=mime_type), None, None

    if allow_files and client is not None:
        try:
            uri, expiry = upload_to_files_api_with_polling(client, path, mime_type=mime_type)
            return types.Part.from_uri(file_uri=uri, mime_type=mime_type), uri, expiry
        except Exception:
            if size <= INLINE_MAX_BYTES:
                return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type), None, None
            raise

    if size <= INLINE_MAX_BYTES:
        return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type), None, None

    raise ValueError("Original attachment requires configured Files client")


def encode_inline_descriptor(
    *,
    file_code: str,
    semantic_name: str,
    raw_bytes: bytes,
    mime_type: str,
    part_index: int = 0,
    client: Any = None,
    files_api_enabled: bool | None = None,
) -> tuple[dict[str, Any], str | None, float | None]:
    if mime_type.startswith("image/"):
        fmt = mime_type.split("/")[-1].lower()
        if fmt == "jpeg":
            fmt = "jpg"
        return (
            {
                "file_code": file_code,
                "semantic_name": semantic_name,
                "image_format": fmt,
                "image_base64": base64.b64encode(raw_bytes).decode("ascii"),
                "mime_type": mime_type,
                "part_index": part_index,
            },
            None,
            None,
        )

    allow_files = is_files_api_enabled(files_api_enabled)
    size = len(raw_bytes)
    b64_str = base64.b64encode(raw_bytes).decode("ascii") if size <= INLINE_MAX_BYTES else ""

    if allow_files and client is not None:
        try:
            uri, expiry = upload_to_files_api_with_polling(client, raw_bytes, mime_type=mime_type)
            return (
                {
                    "file_code": file_code,
                    "semantic_name": semantic_name,
                    "file_uri": uri,
                    "mime_type": mime_type,
                    "part_index": part_index,
                },
                uri,
                expiry,
            )
        except Exception:
            if mime_type.startswith("audio/") and size <= INLINE_MAX_BYTES:
                return (
                    {
                        "file_code": file_code,
                        "semantic_name": semantic_name,
                        "audio_base64": b64_str,
                        "mime_type": mime_type,
                        "part_index": part_index,
                    },
                    None,
                    None,
                )
            raise

    if mime_type.startswith("audio/") and size <= INLINE_MAX_BYTES:
        return (
            {
                "file_code": file_code,
                "semantic_name": semantic_name,
                "audio_base64": b64_str,
                "mime_type": mime_type,
                "part_index": part_index,
            },
            None,
            None,
        )
    if not allow_files and mime_type == "application/pdf" and size <= INLINE_MAX_BYTES:
        return (
            {
                "file_code": file_code,
                "semantic_name": semantic_name,
                "pdf_base64": b64_str,
                "mime_type": mime_type,
                "part_index": part_index,
            },
            None,
            None,
        )

    raise ValueError("Original attachment requires configured Files client")

