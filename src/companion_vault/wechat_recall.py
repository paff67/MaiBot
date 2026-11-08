"""Resolve native message identity against the ledger, never trust prompt metadata."""

from pathlib import Path
from typing import Any, Dict, Sequence
import sqlite3
import asyncio
import json
import os
import time

from src.platform_io.wechat.projection import native_message_id


def _wait_for_media_jobs(
    db_conn: sqlite3.Connection,
    ledger_conn: sqlite3.Connection,
    source: str,
    account: str,
    chat_id: str,
    msg_ids: Sequence[str],
    timeout_s: float = 8.0,
) -> None:
    if os.environ.get("MAIBOT_DISABLE_MEDIA_WAIT") or timeout_s <= 0 or not msg_ids:
        return
    if not db_conn.execute("SELECT 1 FROM sqlite_master WHERE name='message_media_jobs'").fetchone():
        return
    from src.phone_edge.media_references import message_media_refs

    now = time.time()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pending_live = False
        for mid in msg_ids:
            mid_str = str(mid)
            if db_conn.execute(
                "SELECT 1 FROM message_media_jobs WHERE source=? AND account=? AND chat_id=? AND msg_id=? AND state='ready'",
                (source, account, chat_id, mid_str),
            ).fetchone():
                continue
            active_row = db_conn.execute(
                "SELECT state, updated_at FROM message_media_jobs "
                "WHERE source=? AND account=? AND chat_id=? AND msg_id=? "
                "AND state IN ('pending','running','waiting_file','decode_required','retry') "
                "ORDER BY updated_at DESC LIMIT 1",
                (source, account, chat_id, mid_str),
            ).fetchone()
            if active_row is not None and (now - float(active_row[1] or 0)) < 15.0:
                pending_live = True
                break
            if active_row is None:
                msg_row = ledger_conn.execute(
                    "SELECT kind, row_json, updated_at_ms FROM messages WHERE source=? AND account=? AND chat_id=? AND msg_id=?",
                    (source, account, chat_id, mid_str),
                ).fetchone()
                if msg_row is not None:
                    updated_ms = float(msg_row[2] or 0)
                    if (now * 1000.0 - updated_ms) < 10_000.0:
                        try:
                            if message_media_refs(json.loads(msg_row[1] or "{}")):
                                pending_live = True
                                break
                        except Exception:
                            pass
        if not pending_live:
            return
        time.sleep(0.2)
        now = time.time()


def quote_binding(
    *, message_id: str, platform: str, account: str, chat_id: str, ledger_path: str = "data/message_ledger.db"
) -> Dict[str, Any]:
    if platform != "wechat" or not message_id.startswith("wechat:"):
        return {}
    pieces = message_id.split(":")
    if len(pieces) != 4 or pieces[2] != account or not pieces[3].isdigit():
        raise ValueError("Invalid native WeChat message identity")
    with sqlite3.connect(Path(ledger_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT q.source FROM message_quotes q JOIN messages m USING(source,account,msg_id) "
            "WHERE q.account=? AND q.chat_id=? AND q.msg_id=? AND q.revision=m.revision",
            (account, chat_id, pieces[3]),
        ).fetchall()
    db.close()
    matches = [r[0] for r in rows if native_message_id(r[0], account, pieces[3]) == message_id]
    if not matches:
        return {}
    if len(matches) != 1:
        raise ValueError("Ambiguous WeChat source")
    return dict(
        quote_message_ids=[pieces[3]],
        quote_account=account,
        quote_chat_id=chat_id,
        quote_source=matches[0],
        ledger_path=ledger_path,
    )


def memory_scope(*, message_id, platform, account, chat_id, ledger_path="data/message_ledger.db"):
    if platform != "wechat" or not message_id.startswith("wechat:"):
        return {}
    pieces = message_id.split(":")
    if len(pieces) != 4 or pieces[2] != account or not pieces[3].isdigit():
        raise ValueError("Invalid native identity")
    with sqlite3.connect(Path(ledger_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT source FROM messages WHERE account=? AND chat_id=? AND msg_id=?", (account, chat_id, pieces[3])
        ).fetchall()
    db.close()
    sources = [r[0] for r in rows if native_message_id(r[0], account, pieces[3]) == message_id]
    if len(sources) != 1:
        raise ValueError("Missing or ambiguous source scope")
    return dict(source=sources[0], account=account, chat_id=chat_id)


async def build_quote_context_async(
    messages,
    *,
    db_path="data/companion_truth.db",
    ledger_path="data/message_ledger.db",
    client_factory=None,
    exclude_direct_attachment_ids: Sequence[str] = (),
):
    """Current turn only; results are revalidated after I/O and never stored in chat history."""
    exclude_direct = {str(mid) for mid in (exclude_direct_attachment_ids or ())}
    identities = []
    for message in messages:
        if message.platform != "wechat":
            continue
        group = message.message_info.group_info
        identity = dict(
            message_id=message.message_id,
            platform=message.platform,
            account=message.account_id or "",
            chat_id=group.group_id if group else message.message_info.user_info.user_id,
            ledger_path=ledger_path,
        )
        if identity not in identities:
            identities.append(identity)
    if not identities:
        return "", []
    if len({(x["account"], x["chat_id"]) for x in identities}) != 1:
        raise ValueError("Cannot assemble quote evidence from multiple conversations")
    identities.sort(
        key=lambda item: (
            0,
            int(item["message_id"].rsplit(":", 1)[-1]),
        )
        if str(item.get("message_id") or "").startswith("wechat:")
        and str(item["message_id"]).rsplit(":", 1)[-1].isdigit()
        else (1, 0)
    )

    def build():
        from .truth_db import CompanionTruthDB
        from .asset_pipeline import AssetPipeline
        from .quote_recall import recall_quotes, recall_message_attachments, load_original_descriptors

        db = CompanionTruthDB(db_path)
        ledger = sqlite3.connect(Path(ledger_path).resolve().as_uri() + "?mode=ro", uri=True)
        assets = AssetPipeline(db, os.environ.get("MAIBOT_ASSET_SPOOL", "data/companion_assets"))
        client = None
        blocks = []
        descriptors = []

        def get_client():
            nonlocal client
            if client is None:
                if client_factory:
                    client = client_factory()
                elif os.environ.get("GEMINI_API_KEY"):
                    from google.genai import Client
                    from google.genai.types import HttpOptions

                    endpoint = os.environ.get("MAIBOT_FILES_BASE_URL", "")
                    client = Client(
                        api_key=os.environ["GEMINI_API_KEY"],
                        http_options=HttpOptions(base_url=endpoint) if endpoint else None,
                    )
            return client

        try:
            for identity in identities:
                binding = quote_binding(**identity)
                if binding:
                    from src.platform_io.wechat.message_quotes import latest_quote

                    args = dict(
                        source=binding["quote_source"],
                        account=binding["quote_account"],
                        chat_id=binding["quote_chat_id"],
                        quote_message_ids=binding["quote_message_ids"],
                    )
                    target_mids = []
                    for qmid in binding["quote_message_ids"]:
                        qitem = latest_quote(
                            ledger,
                            binding["quote_source"],
                            binding["quote_account"],
                            binding["quote_chat_id"],
                            str(qmid),
                        )
                        if qitem and qitem.get("resolution") == "resolved" and qitem.get("target"):
                            target_mids.append(str(qitem["target"]["msg_id"]))
                    if target_mids:
                        _wait_for_media_jobs(
                            db.conn,
                            ledger,
                            binding["quote_source"],
                            binding["quote_account"],
                            binding["quote_chat_id"],
                            target_mids,
                        )
                    block, files = recall_quotes(ledger, assets, **args)
                    initial_block = block
                    parts = []
                    if files:
                        try:
                            parts = load_original_descriptors(assets, files, client=get_client())
                        except Exception as exc:
                            parts = []
                            block = json.dumps(
                                {
                                    "evidence": json.loads(block),
                                    "attachment_error": type(exc).__name__,
                                    "attachments_loaded": False,
                                },
                                ensure_ascii=False,
                            )
                    current, current_files = recall_quotes(ledger, assets, **args)
                    if json.loads(current) != json.loads(initial_block) or [f["file_code"] for f in current_files] != [
                        f["file_code"] for f in files
                    ]:
                        block = json.dumps(
                            {
                                "quote_message_ids": args["quote_message_ids"],
                                "resolution": "changed_during_recall",
                                "attachments_loaded": False,
                            }
                        )
                        parts = []
                    blocks.append(block)
                    descriptors.extend(parts)
                    continue

                if identity["message_id"] in exclude_direct:
                    continue
                if identity["platform"] != "wechat" or not identity["message_id"].startswith("wechat:"):
                    continue
                pieces = identity["message_id"].split(":")
                if len(pieces) != 4 or pieces[2] != identity["account"] or not pieces[3].isdigit():
                    raise ValueError("Invalid native WeChat message identity")
                msg_id = pieces[3]
                rows = ledger.execute(
                    "SELECT source FROM messages WHERE account=? AND chat_id=? AND msg_id=?",
                    (identity["account"], identity["chat_id"], msg_id),
                ).fetchall()
                matches = [
                    r[0] for r in rows if native_message_id(r[0], identity["account"], msg_id) == identity["message_id"]
                ]
                if not matches:
                    continue
                if len(matches) != 1:
                    raise ValueError("Ambiguous WeChat source")
                att_args = dict(
                    source=matches[0],
                    account=identity["account"],
                    chat_id=identity["chat_id"],
                    msg_id=msg_id,
                )
                _wait_for_media_jobs(
                    db.conn,
                    ledger,
                    matches[0],
                    identity["account"],
                    identity["chat_id"],
                    [msg_id],
                )
                block, files = recall_message_attachments(ledger, assets, **att_args)
                if not files:
                    continue
                initial_block = block
                parts = []
                try:
                    parts = load_original_descriptors(assets, files, client=get_client())
                except Exception as exc:
                    parts = []
                    block = json.dumps(
                        {
                            "evidence": json.loads(block),
                            "attachment_error": type(exc).__name__,
                            "attachments_loaded": False,
                        },
                        ensure_ascii=False,
                    )
                current, current_files = recall_message_attachments(ledger, assets, **att_args)
                if (
                    not current
                    or json.loads(current) != json.loads(initial_block)
                    or [f["file_code"] for f in current_files] != [f["file_code"] for f in files]
                ):
                    block = json.dumps(
                        {
                            "msg_id": msg_id,
                            "resolution": "changed_during_recall",
                            "attachments_loaded": False,
                        }
                    )
                    parts = []
                blocks.append(block)
                descriptors.extend(parts)
            return "\n".join(blocks), descriptors
        finally:
            ledger.close()
            db.close()
            if client is not None and hasattr(client, "close"):
                client.close()

    return await asyncio.to_thread(build)


async def hydrate_wechat_history_media_async(
    history_messages: Sequence[Any],
    *,
    db_path: str = "data/companion_truth.db",
    ledger_path: str = "data/message_ledger.db",
    max_hydrated: int = 12,
    wait_recent_count: int = 4,
    client_factory: Any = None,
) -> set[str]:
    """Hydrate verified ready WeChat originals in-place onto SessionBackedMessage entries in chronological order."""
    wechat_entries = [
        msg
        for msg in history_messages
        if getattr(getattr(msg, "original_message", None), "platform", None) == "wechat"
        and str(getattr(msg, "message_id", "") or "").startswith("wechat:")
    ]
    if not wechat_entries:
        return set()
    if not Path(db_path).exists() or not Path(ledger_path).exists():
        return set()

    def hydrate() -> set[str]:
        from .truth_db import CompanionTruthDB
        from .asset_pipeline import AssetPipeline
        from .quote_recall import recall_message_attachments, load_original_descriptors

        hydrated_ids: set[str] = set()
        older_entries = wechat_entries[:-max_hydrated] if max_hydrated > 0 and len(wechat_entries) > max_hydrated else []
        active_entries = wechat_entries[-max_hydrated:] if max_hydrated > 0 else []
        wait_set = {id(m) for m in active_entries[-wait_recent_count:]} if wait_recent_count > 0 else set()

        for old_msg in older_entries:
            if getattr(old_msg, "vault_descriptors", ()):
                old_msg.vault_evidence_block = ""
                old_msg.vault_descriptors = ()

        db = CompanionTruthDB(db_path)
        ledger = sqlite3.connect(Path(ledger_path).resolve().as_uri() + "?mode=ro", uri=True)
        assets = AssetPipeline(db, os.environ.get("MAIBOT_ASSET_SPOOL", "data/companion_assets"))
        client = None

        def get_client():
            nonlocal client
            if client is None:
                if client_factory:
                    client = client_factory()
                elif os.environ.get("GEMINI_API_KEY"):
                    from google.genai import Client
                    from google.genai.types import HttpOptions

                    endpoint = os.environ.get("MAIBOT_FILES_BASE_URL", "")
                    client = Client(
                        api_key=os.environ["GEMINI_API_KEY"],
                        http_options=HttpOptions(base_url=endpoint) if endpoint else None,
                    )
            return client

        try:
            for entry in active_entries:
                orig = entry.original_message
                message_id = str(entry.message_id or "")
                pieces = message_id.split(":")
                account = str(orig.account_id or "")
                group = orig.message_info.group_info
                chat_id = group.group_id if group else orig.message_info.user_info.user_id
                if len(pieces) != 4 or pieces[2] != account or not pieces[3].isdigit():
                    entry.vault_evidence_block = ""
                    entry.vault_descriptors = ()
                    continue
                msg_id = pieces[3]
                rows = ledger.execute(
                    "SELECT source FROM messages WHERE account=? AND chat_id=? AND msg_id=?",
                    (account, chat_id, msg_id),
                ).fetchall()
                matches = [r[0] for r in rows if native_message_id(r[0], account, msg_id) == message_id]
                if len(matches) != 1:
                    entry.vault_evidence_block = ""
                    entry.vault_descriptors = ()
                    continue
                source = matches[0]
                if id(entry) in wait_set:
                    _wait_for_media_jobs(db.conn, ledger, source, account, chat_id, [msg_id])
                att_args = dict(source=source, account=account, chat_id=chat_id, msg_id=msg_id)
                block, files = recall_message_attachments(ledger, assets, **att_args)
                if not files:
                    entry.vault_evidence_block = ""
                    entry.vault_descriptors = ()
                    continue
                if getattr(entry, "vault_evidence_block", "") == block and getattr(entry, "vault_descriptors", ()):
                    hydrated_ids.add(message_id)
                    continue
                initial_block = block
                try:
                    parts = load_original_descriptors(assets, files, client=get_client())
                except Exception:
                    entry.vault_evidence_block = ""
                    entry.vault_descriptors = ()
                    continue
                current, current_files = recall_message_attachments(ledger, assets, **att_args)
                if (
                    not current
                    or json.loads(current) != json.loads(initial_block)
                    or [f["file_code"] for f in current_files] != [f["file_code"] for f in files]
                ):
                    entry.vault_evidence_block = ""
                    entry.vault_descriptors = ()
                    continue
                entry.vault_evidence_block = block
                entry.vault_descriptors = tuple(parts)
                hydrated_ids.add(message_id)
            return hydrated_ids
        finally:
            ledger.close()
            db.close()
            if client is not None and hasattr(client, "close"):
                client.close()

    return await asyncio.to_thread(hydrate)


def _append_descriptor_part(builder, part: Dict[str, Any]) -> None:
    if "text" in part:
        builder.add_text_content(part["text"])
    elif "image_base64" in part:
        builder.add_image_base64_part(
            image_format=part.get("image_format", "jpeg"),
            image_base64=part["image_base64"],
        )
    else:
        builder.add_file_uri_part(part["file_uri"], part["mime_type"], part["file_code"])


def quote_context_item(block, descriptors):
    from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType

    builder = ContextItemBuilder().set_role(RoleType.User)
    lines = [line for line in str(block or "").split("\n") if line.strip()]
    if len(lines) > 1 and descriptors:
        consumed_indices: set[int] = set()
        for line in lines:
            builder.add_text_content(line)
            line_codes: list[str] = []
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    if isinstance(parsed.get("file_codes"), list):
                        line_codes.extend(str(c) for c in parsed["file_codes"])
                    if isinstance(parsed.get("items"), list):
                        for qitem in parsed["items"]:
                            if isinstance(qitem, dict) and isinstance(qitem.get("file_codes"), list):
                                line_codes.extend(str(c) for c in qitem["file_codes"])
            except Exception:
                pass
            if line_codes:
                code_set = set(line_codes)
                for idx, part in enumerate(descriptors):
                    if idx not in consumed_indices and part.get("file_code") in code_set:
                        _append_descriptor_part(builder, part)
                        consumed_indices.add(idx)
        for idx, part in enumerate(descriptors):
            if idx not in consumed_indices:
                _append_descriptor_part(builder, part)
        return builder.build()

    builder.add_text_content(block)
    for part in descriptors:
        _append_descriptor_part(builder, part)
    return builder.build()
