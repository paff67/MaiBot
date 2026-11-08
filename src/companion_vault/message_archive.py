"""Archive committed message revisions by account/conversation without blocking capture."""

from collections import defaultdict
import hashlib
import json
import os
import sqlite3
import time

from .asset_pipeline import AssetPipeline
from .truth_db import CompanionTruthDB


def archive_page(ledger: sqlite3.Connection, assets: AssetPipeline, limit: int = 500) -> int:
    ledger.execute(
        "CREATE TABLE IF NOT EXISTS archive_cursors(source TEXT,epoch TEXT,seq INTEGER,PRIMARY KEY(source,epoch))"
    )
    ledger.commit()
    for source, epoch in ledger.execute("SELECT DISTINCT source,epoch FROM message_revisions").fetchall():
        row = ledger.execute("SELECT seq FROM archive_cursors WHERE source=? AND epoch=?", (source, epoch)).fetchone()
        cursor = row[0] if row else 0
        rows = ledger.execute(
            "SELECT seq,payload FROM message_revisions WHERE source=? AND epoch=? AND seq>? ORDER BY seq LIMIT ?",
            (source, epoch, cursor, limit),
        ).fetchall()
        if not rows:
            continue
        groups = defaultdict(list)
        for seq, payload in rows:
            event = json.loads(payload)
            groups[(event["account"], event["chat_id"])].append(dict(event, seq=seq))
        for (account, chat), events in groups.items():
            body = dict(schema=1, source=source, epoch=epoch, account=account, chat_id=chat, events=events)
            data = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
            digest = hashlib.sha256(data).hexdigest()
            assets.ingest(
                data,
                dict(
                    mime_type="application/json",
                    relative_path=f"messages-{rows[0][0]}-{rows[-1][0]}.json",
                    account=account,
                    chat_id=chat,
                    representation="source_message_records",
                    event_count=len(events),
                ),
                source_id="message-archive:" + digest,
                category="workspace_docs",
                chat_id=account + "/" + chat,
            )
        # All originals are fsynced and indexed before checkpoint; retry yields the same source IDs.
        with ledger:
            ledger.execute(
                "INSERT INTO archive_cursors VALUES(?,?,?) ON CONFLICT(source,epoch) DO UPDATE SET seq=excluded.seq",
                (source, epoch, rows[-1][0]),
            )
        return len(rows)
    return 0


def main() -> None:
    ledger = sqlite3.connect("data/message_ledger.db", timeout=30)
    db = CompanionTruthDB("data/companion_truth.db")
    assets = AssetPipeline(
        db,
        spool=os.environ.get("MAIBOT_ASSET_SPOOL", "data/companion_assets"),
        remote=os.environ.get("MAIBOT_DRIVE_REMOTE", ""),
    )
    try:
        while True:
            try:
                count = archive_page(ledger, assets)
                if count:
                    print(json.dumps({"archived_revisions": count}), flush=True)
            except Exception as exc:
                print(json.dumps({"error": str(exc)}), flush=True)
            time.sleep(5)
    finally:
        ledger.close()
        db.close()


if __name__ == "__main__":
    main()
