"""模型读取原件；私有 Drive 分享页不是 Gemini Files URI。"""

from pathlib import Path
from typing import Any, List
import csv
import io
import subprocess
import tempfile
import time

from .asset_pipeline import AssetPipeline


def build_original_parts(pipeline: AssetPipeline, file_code: str, client: Any) -> List[Any]:
    from google.genai import types

    row = pipeline.db.conn.execute("SELECT * FROM media_vault WHERE file_code=?", (file_code,)).fetchone()
    if row is None:
        raise KeyError(file_code)
    path = pipeline.original_path(file_code)
    mime = row["mime_type"]
    if mime.startswith("text/") or mime == "application/json":
        return [types.Part.from_text(text=path.read_text(encoding="utf-8-sig"))]
    suffix = path.suffix.lower()
    if mime == "image/gif":
        from PIL import Image, ImageSequence
        # GIF 源文件保留。逐帧保存原尺寸 RGBA PNG；压缩级别 0，禁止 JPEG 重编码/缩放。
        source = pipeline.db.conn.execute("SELECT chat_id FROM asset_sources WHERE file_code=?", (file_code,)).fetchone()
        with Image.open(path) as animation:
            if animation.n_frames > 120:
                raise ValueError(f"{file_code}: 动画超过单次 120 帧预算，原件保留，不能静默丢帧")
            frames = []
            for index, frame in enumerate(ImageSequence.Iterator(animation)):
                buffer = io.BytesIO()
                frame.convert("RGBA").save(buffer, format="PNG", compress_level=0, optimize=False)
                decoded = pipeline.ingest(buffer.getvalue(), {
                    "mime_type": "image/png", "relative_path": f"{file_code}-frame-{index}.png",
                    "representation": "decoded_lossless", "original_file_code": file_code,
                    "frame_index": index, "duration_ms": frame.info.get("duration", 0),
                    "pixel_size": list(frame.size), "png_compress_level": 0,
                }, source_id=f"decoded-png:{file_code}:{index}", category="ai_generated",
                    chat_id=source[0] if source else None)
                frames.extend(build_original_parts(pipeline, decoded["file_code"], client))
            return frames
    if suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook
        workbook = load_workbook(path, read_only=True, data_only=False)
        output = []
        try:
            for sheet in workbook:
                buffer = io.StringIO()
                csv.writer(buffer).writerows(sheet.values)
                output.append(types.Part.from_text(text=f"{file_code} / {sheet.title}\n" + buffer.getvalue()))
        finally:
            workbook.close()
        return output
    if suffix in {".docx", ".pptx"}:
        with tempfile.TemporaryDirectory(prefix="maibot-office-") as directory:
            subprocess.run(["libreoffice", f"-env:UserInstallation={Path(directory).as_uri()}/profile",
                            "--headless", "--convert-to", "pdf", "--outdir", directory, str(path)],
                           check=True, capture_output=True, timeout=120)
            return [types.Part.from_bytes(data=(Path(directory) / (path.stem + ".pdf")).read_bytes(), mime_type="application/pdf")]
    if mime == "audio/x-silk":
        derived = pipeline.db.conn.execute(
            "SELECT d.file_code FROM asset_sources a JOIN asset_sources d ON d.source_id=a.source_id || ':wav' WHERE a.file_code=?",
            (file_code,),
        ).fetchone()
        if derived is None:
            raise ValueError(f"{file_code}: SILK 原件已保存，WAV 无损解码文件尚未就绪")
        return build_original_parts(pipeline, derived[0], client)
    if not (mime in {"image/jpeg", "image/png", "image/webp", "application/pdf"} or mime.startswith(("audio/", "video/"))):
        raise ValueError(f"{file_code}: {mime} 原件已归档，模型不支持直接读取")
    if path.stat().st_size <= 15 * 1024 * 1024:
        return [types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)]
    if not row["gemini_file_uri"] or row["uri_expires_at"] <= time.time() + 600:
        if client is None:
            raise ValueError("Original attachment requires configured Files client")
        uploaded = client.files.upload(file=str(path), config={"mime_type": mime})
        deadline = time.monotonic() + 120
        while uploaded.state and uploaded.state.name == "PROCESSING":
            if time.monotonic() >= deadline:
                raise TimeoutError("Files API 处理超时")
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)
        if uploaded.state and uploaded.state.name == "FAILED":
            raise ValueError("Files API 处理失败")
        if not uploaded.uri:
            raise ValueError("Files API 未返回 URI")
        expiry = uploaded.expiration_time.timestamp() if uploaded.expiration_time else time.time() + 47 * 3600
        with pipeline.db.conn:
            pipeline.db.conn.execute("UPDATE media_vault SET gemini_file_uri=?,uri_expires_at=? WHERE file_code=?",
                                     (uploaded.uri, expiry, file_code))
        uri = uploaded.uri
    else:
        uri = row["gemini_file_uri"]
    return [types.Part.from_uri(file_uri=uri, mime_type=mime)]
