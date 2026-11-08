"""Structured WeChat replies; quoted data never grants authority or crosses chats."""

from typing import Any, Dict, Optional
import json
import re
import sqlite3
import xml.etree.ElementTree as ET


def app_message(content: str) -> Optional[ET.Element]:
    if len(content) > 4_000_000 or re.search(r"<!\s*(DOCTYPE|ENTITY)", content, re.I):
        raise ValueError("Unsafe quote XML")
    start, end = content.find("<msg"), content.rfind("</msg>")
    if start < 0 or end < start:
        raise ValueError("Missing message XML")
    return ET.fromstring(content[start : end + 6]).find("appmsg")


def safe_body(kind: str, content: str) -> Dict[str, Any]:
    """Never pass raw attachment XML, CDN tokens or encryption keys to prompts."""
    base = int(kind or 0) & 65535
    if base == 1:
        return {"text": content[:32000], "truncated": len(content) > 32000}
    if base == 49:
        try:
            app = app_message(content)
            if app is not None:
                return {"title": (app.findtext("title") or "")[:4000], "app_type": app.findtext("type")}
        except (ValueError, ET.ParseError):
            pass
    return {"media_type": kind, "raw_xml_omitted": True}


def parse_quote(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if int(row.get("type") or 0) & 65535 != 49:
        return None
    raw = row.get("content") or ""
    app = app_message(raw)
    if app is None or app.findtext("type") != "57":
        return None
    ref = app.find("refermsg")
    if ref is None:
        raise ValueError("Quoted reply missing refermsg")
    server_id = ref.findtext("svrid") or ""
    if not re.fullmatch(r"[0-9]+", server_id) or server_id == "0":
        raise ValueError("Quoted reply missing stable server ID")
    kind = ref.findtext("type") or "0"
    return {
        "schema": 1,
        "reply_text": app.findtext("title") or "",
        "target_server_id": server_id,
        "target_type": kind,
        "from_user": ref.findtext("fromusr") or "",
        "target_sender_id": ref.findtext("chatusr") or "",
        "display_name": ref.findtext("displayname") or "",
        "target_created_at": ref.findtext("createtime"),
        "quoted_snapshot": safe_body(kind, ref.findtext("content") or ""),
    }


def ensure_quote_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS message_quotes(
      source TEXT,account TEXT,msg_id TEXT,chat_id TEXT NOT NULL,revision TEXT NOT NULL,
      target_server_id TEXT,payload TEXT NOT NULL,parse_error TEXT,
      PRIMARY KEY(source,account,msg_id));
    CREATE INDEX IF NOT EXISTS quote_chat ON message_quotes(source,account,chat_id);
    CREATE INDEX IF NOT EXISTS message_server_lookup ON messages(source,account,chat_id,server_id);
    """)


def save_quote(db: sqlite3.Connection, source: str, account: str, row: Dict[str, Any], revision: str) -> None:
    try:
        quote = parse_quote(row)
        error = None
    except (ValueError, ET.ParseError, TypeError) as exc:
        if "refermsg" not in (row.get("content") or ""):
            quote = None
        else:
            quote = {"schema": 1, "reply_text": "", "quoted_snapshot": {"unavailable": True}}
        error = type(exc).__name__
    key = (source, account, row["msgId"])
    if quote is None:
        db.execute("DELETE FROM message_quotes WHERE source=? AND account=? AND msg_id=?", key)
        return
    db.execute(
        "INSERT INTO message_quotes VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source,account,msg_id) "
        "DO UPDATE SET chat_id=excluded.chat_id,revision=excluded.revision,target_server_id=excluded.target_server_id,payload=excluded.payload,parse_error=excluded.parse_error",
        (*key, row["talker"], revision, quote.get("target_server_id"), json.dumps(quote, ensure_ascii=False), error),
    )


def latest_quote(
    db: sqlite3.Connection, source: str, account: str, chat: str, msg_id: Optional[str] = None, allowed_chats=None
) -> Optional[Dict[str, Any]]:
    db.row_factory = sqlite3.Row
    record = db.execute(
        "SELECT q.*,m.sender_id FROM message_quotes q JOIN messages m USING(source,account,msg_id) "
        "WHERE q.source=? AND q.account=? AND q.chat_id=? AND m.direction='incoming' AND q.revision=m.revision "
        + ("AND q.msg_id=? " if msg_id is not None else "")
        + "ORDER BY CAST(q.msg_id AS INTEGER) DESC LIMIT 1",
        (source, account, chat, msg_id) if msg_id is not None else (source, account, chat),
    ).fetchone()
    if record is None:
        return None
    item = json.loads(record["payload"])
    item.update(
        quote_msg_id=record["msg_id"],
        quote_revision=record["revision"],
        quote_sender_id=record["sender_id"],
        chat_id=chat,
        account=account,
        file_codes=[],
        resolution="missing_target",
    )
    if record["parse_error"]:
        item["resolution"] = "invalid_quote"
        item["quoted_snapshot"] = {"unverified": True}
        return item
    allowed = {chat} if allowed_chats is None else set(allowed_chats)
    if chat.endswith("@chatroom"):
        allowed &= {chat}
    from_user = item["from_user"]
    if chat.endswith("@chatroom"):
        if from_user.endswith("@chatroom") and from_user != chat:
            item["resolution"] = "conversation_mismatch"
            item["quoted_snapshot"] = {"unverified": True}
            return item
        target_chat = chat
        if not item.get("target_sender_id") and from_user and not from_user.endswith("@chatroom"):
            item["target_sender_id"] = from_user
    else:
        target_chat = from_user if from_user.endswith("@chatroom") else chat
        if target_chat not in allowed:
            item["resolution"] = "conversation_mismatch"
            item["quoted_snapshot"] = {"unverified": True}
            return item
    if target_chat not in allowed:
        item["resolution"] = "conversation_mismatch"
        item["quoted_snapshot"] = {"unverified": True}
        return item
    matches = db.execute(
        "SELECT msg_id,revision,kind,body,sender_id FROM messages "
        "WHERE source=? AND account=? AND chat_id=? AND server_id=?",
        (source, account, target_chat, item["target_server_id"]),
    ).fetchall()
    if len(matches) != 1:
        item["resolution"] = "ambiguous_target" if matches else "missing_target"
        item["quoted_snapshot"] = {"unverified": True}
        return item
    target = dict(matches[0])
    target["chat_id"] = target_chat
    if item.get("target_sender_id") and target["sender_id"] and item["target_sender_id"] != target["sender_id"]:
        item["resolution"] = "sender_mismatch"
        item["quoted_snapshot"] = {"unverified": True}
        return item
    original_body = target.pop("body") or ""
    target["content"] = (
        {"text": original_body, "truncated": False}
        if (int(target["kind"] or 0) & 65535) == 1
        else safe_body(target["kind"], original_body)
    )
    if (int(target["kind"] or 0) & 65535) == 10000:
        item["resolution"] = "target_system_or_revoked"
        target["content"] = {"unavailable": True}
        item["quoted_snapshot"] = {"withheld_due_to_target_revision": True}
    else:
        item["resolution"] = "resolved"
    item["target"] = target
    return item
