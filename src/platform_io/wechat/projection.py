"""Project a committed ledger row to native SessionMessage input without inference."""

from pathlib import Path
from typing import Any, Dict, Optional
import hashlib
import json
import re
import sqlite3

from .message_quotes import latest_quote, parse_quote, safe_body


def native_message_id(source: str, account: str, msg_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", account) or not re.fullmatch(r"[0-9]+", msg_id):
        raise ValueError("Invalid WeChat identity")
    return "wechat:" + hashlib.sha256(source.rstrip("/").encode()).hexdigest()[:24] + ":" + account + ":" + msg_id


def enrich_record_from_truth_db(
    record: Dict[str, Any], truth_path: Optional[str], ledger_path: Optional[str] = None
) -> Dict[str, Any]:
    enriched = dict(record)
    sender = enriched.get("sender_id")
    if truth_path and sender:
        db_file = Path(truth_path).resolve()
        if db_file.is_file():
            try:
                with sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True) as db:
                    db.row_factory = sqlite3.Row
                    rows = db.execute(
                        "SELECT person_id, platform_ids, person_name, wechat_remark FROM person_info"
                    ).fetchall()
                db.close()
                matched = None
                for row in rows:
                    if row["person_id"] == sender:
                        matched = row
                        break
                    try:
                        platform_ids = json.loads(row["platform_ids"] or "{}")
                    except (ValueError, TypeError):
                        platform_ids = {}
                    if isinstance(platform_ids, dict) and platform_ids.get("wechat") == sender:
                        matched = row
                        break
                if matched is not None:
                    person_name = (matched["person_name"] or "").strip()
                    wechat_remark = (matched["wechat_remark"] or "").strip()
                    if not enriched.get("sender_nickname"):
                        enriched["sender_nickname"] = wechat_remark or person_name or sender
                    if wechat_remark and not enriched.get("sender_cardname"):
                        enriched["sender_cardname"] = wechat_remark
            except sqlite3.Error:
                pass
    try:
        kind = int(enriched.get("kind") or json.loads(enriched.get("row_json") or "{}").get("type") or 0) & 65535
    except (ValueError, TypeError):
        kind = 0
    if (
        kind == 49
        and enriched.get("source")
        and enriched.get("account")
        and enriched.get("chat_id")
        and enriched.get("msg_id")
    ):
        candidates = []
        if ledger_path:
            candidates.append(Path(ledger_path))
        if enriched.get("ledger_path"):
            candidates.append(Path(str(enriched["ledger_path"])))
        if truth_path:
            t_dir = Path(truth_path).resolve().parent
            candidates.extend([t_dir / "message_ledger.db", t_dir / "ledger.db"])
        for cand in candidates:
            if cand.is_file():
                try:
                    with sqlite3.connect(cand.resolve().as_uri() + "?mode=ro", uri=True) as ldb:
                        item = latest_quote(
                            ldb,
                            str(enriched["source"]).rstrip("/"),
                            str(enriched["account"]),
                            str(enriched["chat_id"]),
                            str(enriched["msg_id"]),
                        )
                    ldb.close()
                    if item is not None and item.get("resolution") == "resolved" and item.get("target"):
                        target = item["target"]
                        enriched["resolved_quote_msg_id"] = str(target["msg_id"])
                        enriched["resolved_quote_display_name"] = (
                            item.get("display_name") or target.get("sender_id") or ""
                        )
                        target_content = target.get("content") or {}
                        enriched["resolved_quote_text"] = (
                            target_content.get("text") or target_content.get("title") or ""
                        )
                    break
                except Exception:
                    pass
    return enriched


def project_message(record: Dict[str, Any]) -> Dict[str, Any]:
    row = json.loads(record["row_json"])
    if row["msgId"] != record["msg_id"] or row["talker"] != record["chat_id"]:
        raise ValueError("Ledger ownership mismatch")
    if record["direction"] != "incoming":
        raise ValueError("Outgoing messages must not trigger a new incoming turn")
    kind = int(row["type"]) & 65535
    if kind == 10000:
        raise ValueError("System/revoke revisions belong to reconciliation, not new turns")
    sender = record["sender_id"]
    if not sender:
        raise ValueError("Unknown sender; never substitute a group ID or nickname")
    quote = parse_quote(row) if kind == 49 else None
    body = (
        quote["reply_text"]
        if quote
        else record["body"]
        if kind == 1
        else "[图片]"
        if kind == 3
        else "[表情包]"
        if kind == 47
        else json.dumps(safe_body(row["type"], record["body"]), ensure_ascii=False)
    )
    raw_message = []
    if quote is not None:
        if record.get("resolved_quote_msg_id"):
            raw_message.append(
                {
                    "type": "reply",
                    "data": native_message_id(
                        record["source"], record["account"], str(record["resolved_quote_msg_id"])
                    ),
                }
            )
        elif quote.get("display_name") or record.get("resolved_quote_text"):
            ref_author = quote.get("display_name") or quote.get("target_sender_id") or "群友"
            body = f"[引用 @{ref_author}] {body}"
    raw_message.append({"type": "text", "data": body})
    user_nickname = record.get("sender_nickname") or sender
    user_cardname = record.get("sender_cardname")
    group_name = record.get("group_name") or record["chat_id"]
    # No implicit image-to-caption or audio-to-text conversion. Original assets remain in the vault.
    return {
        "message_id": native_message_id(record["source"], record["account"], record["msg_id"]),
        "platform": "wechat",
        "account_id": record["account"],
        "scope": hashlib.sha256(record["source"].rstrip("/").encode()).hexdigest()[:24],
        "timestamp": int(row["createTime"]) / 1000,
        "message_info": {
            "user_info": {"user_id": sender, "user_nickname": user_nickname, "user_cardname": user_cardname},
            "group_info": {"group_id": record["chat_id"], "group_name": group_name}
            if record["chat_id"].endswith("@chatroom")
            else None,
            "additional_config": {
                "wechat_msg_id": record["msg_id"],
                "wechat_revision": record["revision"],
                "wechat_media_state": "resolve_from_vault" if kind != 1 else "text",
            },
        },
        "raw_message": raw_message,
    }
