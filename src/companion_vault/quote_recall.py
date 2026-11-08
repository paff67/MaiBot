"""Resolve explicit reply references through the same-chat memory retrieval boundary."""

from typing import Any, Dict, List, Sequence, Tuple
import base64
import json
import sqlite3
import io
import time

from .asset_pipeline import AssetPipeline


def recall_quotes(
    ledger: sqlite3.Connection,
    assets: AssetPipeline,
    *,
    source: str,
    account: str,
    chat_id: str,
    quote_message_ids: Sequence[str],
) -> Tuple[str, List[Dict[str, Any]]]:
    """Explicit IDs prevent an old quote being carried into unrelated future turns."""
    from src.platform_io.wechat.message_quotes import latest_quote
    from .access_policy import allowed_conversations, authorized_file_rows

    recalled, files, seen = [], [], set()
    for msg_id in dict.fromkeys(quote_message_ids):
        allowed = allowed_conversations(
            assets.db.conn, source=source, account=account, requester_id=chat_id, destination_chat_id=chat_id
        )
        item = latest_quote(ledger, source, account, chat_id, str(msg_id), allowed_chats=allowed)
        if item is None:
            recalled.append({"quote_msg_id": str(msg_id), "resolution": "quote_unavailable"})
            continue
        if item["resolution"] == "resolved":
            target = item["target"]
            rows = (
                assets.db.conn.execute(
                    "SELECT DISTINCT l.file_code FROM message_asset_links l JOIN message_media_jobs j USING(job_id) "
                    "WHERE j.source=? AND l.account=? AND l.chat_id=? AND l.msg_id=? AND l.revision=? "
                    "AND l.active=1 AND j.state='ready'",
                    (source, account, target["chat_id"], target["msg_id"], target["revision"]),
                ).fetchall()
                if assets.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='message_asset_links'").fetchone()
                else []
            )
            authorized = authorized_file_rows(
                assets.db.conn,
                ledger,
                source=source,
                account=account,
                destination_chat_id=chat_id,
                codes=[r[0] for r in rows],
            )
            codes = [r["file_code"] for r in authorized]
            item["file_codes"] = codes
            for code in codes:
                if code not in seen:
                    seen.add(code)
                    files.append(
                        dict(assets.db.conn.execute("SELECT * FROM media_vault WHERE file_code=?", (code,)).fetchone())
                    )
        recalled.append(item)
    return json.dumps({"recall_type": "explicit_message_reference", "items": recalled}, ensure_ascii=False), files


def recall_message_attachments(
    ledger: sqlite3.Connection,
    assets: AssetPipeline,
    *,
    source: str,
    account: str,
    chat_id: str,
    msg_id: str,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Resolve direct verified ready attachments on a current-turn WeChat message."""
    from .access_policy import authorized_file_rows

    msg = ledger.execute(
        "SELECT revision, kind FROM messages WHERE source=? AND account=? AND chat_id=? AND msg_id=?",
        (source, account, chat_id, str(msg_id)),
    ).fetchone()
    if not msg or (int(msg[1] or 0) & 65535) == 10000:
        return "", []
    revision = msg[0]
    if not assets.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='message_asset_links'").fetchone():
        return "", []
    rows = assets.db.conn.execute(
        "SELECT DISTINCT l.file_code FROM message_asset_links l JOIN message_media_jobs j USING(job_id) "
        "WHERE j.source=? AND l.account=? AND l.chat_id=? AND l.msg_id=? AND l.revision=? "
        "AND l.active=1 AND j.state='ready'",
        (source, account, chat_id, str(msg_id), revision),
    ).fetchall()
    if not rows:
        return "", []
    files = authorized_file_rows(
        assets.db.conn,
        ledger,
        source=source,
        account=account,
        destination_chat_id=chat_id,
        codes=[r[0] for r in rows],
    )
    if not files:
        return "", []
    block = json.dumps(
        {
            "recall_type": "current_turn_message_attachment",
            "msg_id": str(msg_id),
            "revision": revision,
            "file_codes": [f["file_code"] for f in files],
        },
        ensure_ascii=False,
    )
    return block, files


def load_original_descriptors(assets, file_rows, *, client=None):
    """Use the existing original decoder; never reinterpret thumbnail/opaque files."""
    from .model_assets import build_original_parts

    descriptors = []
    for row in file_rows:
        code = row["file_code"]
        # Cached remote handles still require verification of the stored original bytes.
        path = assets.original_path(code)
        db_row = assets.db.conn.execute("SELECT * FROM media_vault WHERE file_code=?", (code,)).fetchone()
        source_row = dict(db_row) if db_row is not None else dict(row)
        semantic_name = row.get("semantic_name") or source_row.get("semantic_name") or ""
        uri = source_row.get("gemini_file_uri") or row.get("gemini_file_uri")
        expires_at = float(source_row.get("uri_expires_at") or row.get("uri_expires_at") or 0)
        mime = source_row.get("mime_type") or row.get("mime_type") or "application/octet-stream"
        suffix = path.suffix.lower()
        if (
            uri
            and expires_at > time.time() + 600
            and not (
                mime.startswith("text/")
                or mime in {"application/json", "image/gif", "audio/x-silk"}
                or suffix in {".xlsx", ".xlsm", ".docx", ".pptx"}
            )
        ):
            descriptors.append(
                {
                    "file_code": code,
                    "semantic_name": semantic_name,
                    "file_uri": uri,
                    "mime_type": mime,
                }
            )
            continue
        parts = build_original_parts(assets, code, client)
        for index, part in enumerate(parts):
            if part.text is not None:
                descriptors.append({"file_code": code, "semantic_name": semantic_name, "text": part.text})
            elif part.file_data:
                descriptors.append(
                    {
                        "file_code": code,
                        "semantic_name": semantic_name,
                        "file_uri": part.file_data.file_uri,
                        "mime_type": part.file_data.mime_type,
                    }
                )
            elif part.inline_data:
                img_data = part.inline_data.data
                img_mime = part.inline_data.mime_type
                if img_mime.startswith("image/"):
                    fmt = img_mime.split("/")[-1].lower()
                    if fmt == "jpeg":
                        fmt = "jpg"
                    descriptors.append(
                        {
                            "file_code": code,
                            "semantic_name": semantic_name,
                            "image_format": fmt,
                            "image_base64": base64.b64encode(img_data).decode("ascii"),
                            "mime_type": img_mime,
                            "part_index": index,
                        }
                    )
                    continue
                if client is None:
                    raise ValueError("Original attachment requires configured Files client")
                uploaded = client.files.upload(
                    file=io.BytesIO(part.inline_data.data), config={"mime_type": part.inline_data.mime_type}
                )
                deadline = time.monotonic() + 120
                while uploaded.state and uploaded.state.name == "PROCESSING":
                    if time.monotonic() > deadline:
                        raise TimeoutError("Attachment processing timeout")
                    time.sleep(1)
                    uploaded = client.files.get(name=uploaded.name)
                if not uploaded.uri or (uploaded.state and uploaded.state.name == "FAILED"):
                    raise ValueError("Attachment processing failed")
                if len(parts) == 1 and mime == part.inline_data.mime_type:
                    expiry = (
                        uploaded.expiration_time.timestamp()
                        if getattr(uploaded, "expiration_time", None)
                        else time.time() + 47 * 3600
                    )
                    with assets.db.conn:
                        assets.db.conn.execute(
                            "UPDATE media_vault SET gemini_file_uri=?, uri_expires_at=? WHERE file_code=?",
                            (uploaded.uri, expiry, code),
                        )
                descriptors.append(
                    {
                        "file_code": code,
                        "semantic_name": semantic_name,
                        "file_uri": uploaded.uri,
                        "mime_type": part.inline_data.mime_type,
                        "part_index": index,
                    }
                )
            else:
                raise ValueError("Unsupported original part")
    return descriptors
