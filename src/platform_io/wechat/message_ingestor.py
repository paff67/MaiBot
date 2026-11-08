"""Exactly-once local application of a replayable phone journal (not exactly-once transport)."""

from pathlib import Path
import argparse
import json
import os
import re
import sqlite3
import time

import httpx

from .message_quotes import ensure_quote_schema, save_quote


class MessageLedger:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS source_cursors(source TEXT,epoch TEXT,seq INTEGER NOT NULL,PRIMARY KEY(source,epoch));
        CREATE TABLE IF NOT EXISTS message_revisions(source TEXT,epoch TEXT,seq INTEGER,payload TEXT NOT NULL,PRIMARY KEY(source,epoch,seq));
        CREATE TABLE IF NOT EXISTS messages(source TEXT,account TEXT,msg_id TEXT,chat_id TEXT NOT NULL,
            server_id TEXT,sender_id TEXT,direction TEXT,kind TEXT,body TEXT,raw_content TEXT,
            revision TEXT,row_json TEXT NOT NULL,updated_at_ms INTEGER,PRIMARY KEY(source,account,msg_id));
        CREATE TABLE IF NOT EXISTS source_health(source TEXT PRIMARY KEY,epoch TEXT,last_ok_ms INTEGER,error TEXT);
        """)
        ensure_quote_schema(self.db)
        # Backfill only quote candidates; raw ledger messages remain the source of truth.
        with self.db:
            for source, account, data, revision in self.db.execute(
                "SELECT source,account,row_json,revision FROM messages WHERE raw_content LIKE '%refermsg%'"
            ).fetchall():
                save_quote(self.db, source, account, json.loads(data), revision)

    def cursor(self, source: str, epoch: str) -> int:
        row = self.db.execute("SELECT seq FROM source_cursors WHERE source=? AND epoch=?", (source, epoch)).fetchone()
        return row[0] if row else 0

    def ingest(self, source: str, epoch: str, event: dict) -> bool:
        row = event["row"]
        account = event["account"]
        msg_id = event["msg_id"]
        chat = row["talker"]
        seq = event["seq"]
        if type(seq) is not int or seq <= 0 or not epoch or not source:
            raise ValueError("Invalid journal identity")
        if not re.fullmatch(r"[0-9a-f]{32}", account) or not re.fullmatch(r"[0-9]+", msg_id):
            raise ValueError("Invalid stable message identity")
        if chat != event["chat_id"] or row["msgId"] != msg_id or not chat:
            raise ValueError("Conflicting message ownership")
        raw = row.get("content") or ""
        sender = None
        body = raw
        direction = "outgoing" if row.get("isSend") == "1" else "incoming"
        if direction == "incoming" and chat.endswith("@chatroom"):
            match = re.match(r"^([A-Za-z0-9_.@-]+):\n", raw)
            if match:
                sender = match.group(1)
                body = raw[match.end() :]
            else:
                for pattern in (
                    r'<emoji\b[^>]*\bfromusername="([A-Za-z0-9_.@-]+)"',
                    r"<fromusername>\s*([A-Za-z0-9_.@-]+)\s*</fromusername>",
                    r'\bfromusername="([A-Za-z0-9_.@-]+)"',
                    r"<chatusr>\s*([A-Za-z0-9_.@-]+)\s*</chatusr>",
                ):
                    xml_match = re.search(pattern, raw)
                    if xml_match:
                        candidate = xml_match.group(1).strip()
                        if candidate and not candidate.endswith("@chatroom"):
                            sender = candidate
                            break
        elif direction == "incoming":
            sender = chat
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            duplicate = self.db.execute(
                "SELECT payload FROM message_revisions WHERE source=? AND epoch=? AND seq=?", (source, epoch, seq)
            ).fetchone()
            if duplicate:
                if json.loads(duplicate[0]) != event:
                    raise ValueError("Same journal sequence has different data; source integrity failure")
                return False
            if seq != self.cursor(source, epoch) + 1:
                raise ValueError("Journal sequence gap; do not acknowledge")
            existing = self.db.execute(
                "SELECT chat_id FROM messages WHERE source=? AND account=? AND msg_id=?", (source, account, msg_id)
            ).fetchone()
            if existing and existing[0] != chat:
                raise ValueError("Message ID moved to another conversation; manual reconciliation required")
            self.db.execute(
                "INSERT INTO message_revisions VALUES(?,?,?,?)",
                (source, epoch, seq, json.dumps(event, ensure_ascii=False)),
            )
            self.db.execute(
                """INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source,account,msg_id) DO UPDATE SET server_id=excluded.server_id,sender_id=excluded.sender_id,
                direction=excluded.direction,kind=excluded.kind,body=excluded.body,raw_content=excluded.raw_content,
                revision=excluded.revision,row_json=excluded.row_json,updated_at_ms=excluded.updated_at_ms""",
                (
                    source,
                    account,
                    msg_id,
                    chat,
                    row.get("msgSvrId"),
                    sender,
                    direction,
                    row.get("type"),
                    body,
                    raw,
                    event["revision"],
                    json.dumps(row, ensure_ascii=False),
                    event["observed_at_ms"],
                ),
            )
            self.db.execute(
                "INSERT INTO source_cursors VALUES(?,?,?) ON CONFLICT(source,epoch) DO UPDATE SET seq=excluded.seq",
                (source, epoch, seq),
            )
            save_quote(self.db, source, account, row, event["revision"])
        return True


def poll(ledger: MessageLedger, client: httpx.Client, edge: str, epoch: str) -> str:
    response = client.get(
        edge + "/messages/events", params={"after": ledger.cursor(edge, epoch) if epoch else 0, "limit": 500}
    )
    response.raise_for_status()
    page = response.json()
    scanner_error = None
    if not page.get("last_scan_ms") or time.time() * 1000 - page["last_scan_ms"] > 120_000:
        scanner_error = "Phone scanner heartbeat stale; persisted backlog remains replayable"
    if ledger.cursor(edge, page["epoch"]) > page["max_cursor"]:
        raise RuntimeError("Phone journal rolled back within the same epoch; refusing silent skip")
    if page["epoch"] != epoch and (epoch or ledger.cursor(edge, page["epoch"]) > 0):
        return page["epoch"]  # Next poll uses this epoch's own persisted cursor, never after=0 or old offset.
    epoch = page["epoch"]
    for event in page["events"]:
        ledger.ingest(edge, epoch, event)
    with ledger.db:
        ledger.db.execute(
            "INSERT INTO source_health VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE SET epoch=excluded.epoch,last_ok_ms=excluded.last_ok_ms,error=excluded.error",
            (edge, epoch, int(time.time() * 1000), scanner_error),
        )
    return epoch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--db", default="data/message_ledger.db")
    args = parser.parse_args()
    ledger = MessageLedger(args.db)
    edge = os.environ.get("MAIBOT_EDGE_URL", "http://100.87.39.37:8765").rstrip("/")
    token = os.environ.get("MAIBOT_EDGE_TOKEN", "")
    epoch = ""
    with httpx.Client(timeout=60, headers={"Authorization": "Bearer " + token} if token else {}) as client:
        while True:
            try:
                epoch = poll(ledger, client, edge, epoch)
                print(json.dumps({"cursor": ledger.cursor(edge, epoch), "epoch": epoch}), flush=True)
            except Exception as exc:
                with ledger.db:
                    ledger.db.execute(
                        "INSERT INTO source_health VALUES(?,?,NULL,?) ON CONFLICT(source) DO UPDATE SET error=excluded.error",
                        (edge, epoch, str(exc)),
                    )
                if args.once:
                    raise
                print(json.dumps({"error": str(exc)}), flush=True)
            if args.once:
                break
            time.sleep(2)
    ledger.db.close()


if __name__ == "__main__":
    main()
