"""Read-only paginated access to the durable Android notification journal."""

from pathlib import Path
import json
import sqlite3


def read_notifications(path: str, after: int = 0, limit: int = 100) -> dict:
    if after < 0 or not 1 <= limit <= 500:
        raise ValueError("invalid notification cursor/limit")
    if not Path(path).is_file():
        raise RuntimeError("notification collector has not initialized its journal")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT seq,payload FROM notification_events WHERE seq>? ORDER BY seq LIMIT ?",
                          (after, limit)).fetchall()
    events = [{"seq": row[0], **json.loads(row[1])} for row in rows]
    return {"ok": True, "events": events, "next_cursor": rows[-1][0] if rows else after}
