"""Durable multi-conversation notification inbox; never sends chat messages."""

from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import sqlite3
import time

import httpx


class NotificationInbox:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS notification_inbox (
          event_uuid TEXT PRIMARY KEY, edge TEXT NOT NULL, edge_seq INTEGER NOT NULL,
          route_key TEXT NOT NULL, chat_id TEXT, identity_status TEXT NOT NULL,
          title TEXT, body TEXT, event_type TEXT, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notification_cursors(edge TEXT PRIMARY KEY, seq INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS conversation_notifications (
          observation_id TEXT PRIMARY KEY, route_key TEXT NOT NULL, chat_id TEXT NOT NULL,
          source_message_id TEXT, title TEXT, body TEXT, coverage TEXT NOT NULL, last_event_uuid TEXT NOT NULL);
        """)

    def ingest(self, edge: str, event: dict) -> None:
        package = event["package"]
        fields = event.get("intent_fields", {})
        ids = {v for k, v in fields.items() if k in {"Chat_User", "talker", "Main_User", "Contact_User", "notification_username"}
               and isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.@-]{1,200}", v)}
        chat = next(iter(ids)) if len(ids) == 1 else None
        # Notification ID is stored independently; never relabel it as a WeChat ID.
        scope = f"{edge}|{event.get('android_user_id', 0)}|{package}|"
        summary = bool(event.get("notification_flags", 0) & 512) or event.get("tag") == "ranker_group"
        if summary:
            chat = None
        route = scope + ("summary:" + event["key"] if summary else "chat:" + chat if chat else "notification:" + event["key"])
        values = event.get("fields", {})
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO notification_inbox VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event["event_uuid"], edge, event["seq"], route, chat,
                 "system_summary" if summary else "intent_chat_user" if chat else "notification_only", values.get("android.title", ""),
                 values.get("android.text", ""), event["event"], json.dumps(event, ensure_ascii=False)))
            self.db.execute("INSERT INTO notification_cursors VALUES(?,?) ON CONFLICT(edge) DO UPDATE SET seq=max(seq,excluded.seq)",
                            (edge, event["seq"]))
            if chat and not summary and event["event"] != "onNotificationRemoved":
                message_id = fields.get("notification_msg_id")
                body = values.get("android.text", "")
                # Reconciliation is not a new message. Preserve callback journal but
                # coalesce identical visible observations across listener restarts.
                observation_id = hashlib.sha256(json.dumps([route, event.get("post_time"), body]).encode()).hexdigest()
                coverage = "merged_latest_only" if re.match(r"^\[\d+条\]", body) else "notification_text"
                self.db.execute("INSERT INTO conversation_notifications VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(observation_id) DO UPDATE SET source_message_id=coalesce(excluded.source_message_id,source_message_id),last_event_uuid=excluded.last_event_uuid",
                    (observation_id, route, chat, str(message_id) if message_id is not None else None,
                     values.get("android.title", ""), body, coverage, event["event_uuid"]))

    def cursor(self, edge: str) -> int:
        row = self.db.execute("SELECT seq FROM notification_cursors WHERE edge=?", (edge,)).fetchone()
        return row[0] if row else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--db", default="data/notification_inbox.db")
    args = parser.parse_args()
    inbox = NotificationInbox(args.db)
    edge = os.environ.get("MAIBOT_EDGE_URL", "http://100.87.39.37:8765").rstrip("/")
    token = os.environ.get("MAIBOT_EDGE_TOKEN", "")
    with httpx.Client(timeout=45, headers={"Authorization": "Bearer " + token} if token else {}) as client:
        while True:
            try:
                response = client.get(edge + "/notifications/events", params={"after": inbox.cursor(edge), "limit": 100})
                response.raise_for_status()
                events = response.json()["events"]
                for event in events:
                    inbox.ingest(edge, event)
                if events:
                    print(json.dumps({"ingested": len(events), "cursor": inbox.cursor(edge)}), flush=True)
            except Exception as exc:
                if args.once:
                    raise
                print(json.dumps({"error": str(exc)}), flush=True)
            if args.once:
                break
            time.sleep(2)
    inbox.db.close()


if __name__ == "__main__":
    main()
