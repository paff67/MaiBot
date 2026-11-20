"""Private scene summaries with exact conversation and live source revision checks."""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence
from zoneinfo import ZoneInfo
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodic_beats(
    beat_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    account TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    logical_turn_id TEXT NOT NULL,
    persona_slot TEXT NOT NULL,
    occurred_at_jst TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    visibility TEXT NOT NULL DEFAULT 'PRIVATE' CHECK(visibility='PRIVATE'),
    UNIQUE(source, account, chat_id, logical_turn_id)
);
CREATE TABLE IF NOT EXISTS beat_sources(
    beat_id TEXT NOT NULL REFERENCES episodic_beats(beat_id),
    source TEXT NOT NULL,
    account TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    PRIMARY KEY(beat_id, source, account, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS beat_scope ON episodic_beats(source, account, chat_id, occurred_at_jst);
"""


def confirm_beat(
    db: sqlite3.Connection,
    *,
    source: str,
    account: str,
    chat_id: str,
    logical_turn_id: str,
    persona_slot: str,
    summary_text: str,
    sources: Sequence[Dict[str, str]],
) -> str:
    """Caller owns BEGIN IMMEDIATE and the successful-delivery idempotency guard."""
    if not summary_text.strip() or not sources or not all((source, account, chat_id, logical_turn_id)):
        return ""
    existing = db.execute(
        "SELECT beat_id FROM episodic_beats WHERE source=? AND account=? AND chat_id=? AND logical_turn_id=?",
        (source, account, chat_id, logical_turn_id),
    ).fetchone()
    if existing:
        return existing[0]
    number = db.execute("SELECT COALESCE(MAX(CAST(SUBSTR(beat_id,3) AS INTEGER)),0)+1 FROM episodic_beats").fetchone()[
        0
    ]
    code = f"BE{number:06d}"
    db.execute(
        "INSERT INTO episodic_beats(beat_id,source,account,chat_id,logical_turn_id,persona_slot,"
        "occurred_at_jst,summary_text) VALUES(?,?,?,?,?,?,?,?)",
        (
            code,
            source,
            account,
            chat_id,
            logical_turn_id,
            persona_slot,
            datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
            summary_text.strip()[:6000],
        ),
    )
    unique_sources = {str(item["msg_id"]): str(item["revision"]) for item in sources}
    db.executemany(
        "INSERT INTO beat_sources VALUES(?,?,?,?,?,?)",
        [(code, source, account, chat_id, mid, revision) for mid, revision in unique_sources.items()],
    )
    return code


def normalize_time_bound(value: str, *, end: bool = False) -> str:
    """Normalize supported ISO dates and timestamps to the stored JST representation."""
    parsed = datetime.fromisoformat(value.replace("/", "-"))
    if len(value) == 10 and end:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    return parsed.astimezone(ZoneInfo("Asia/Tokyo")).isoformat()


def read_beats(
    db: sqlite3.Connection,
    ledger_path: str,
    *,
    source: str,
    account: str,
    chat_id: str,
    codes: Sequence[str] | None = None,
    active_msg_ids: Sequence[str] = (),
    query: str = "",
    limit: int = 20,
    time_start: str = "",
    time_end: str = "",
) -> List[Dict[str, Any]]:
    """Read private summaries only while all of their source revisions remain current."""
    if not all((source, account, chat_id)) or codes == [] or not Path(ledger_path).is_file():
        return []
    sql = (
        "SELECT * FROM episodic_beats WHERE source=? AND account=? AND chat_id=? "
        "AND status='confirmed' AND visibility='PRIVATE'"
    )
    params: List[Any] = [source, account, chat_id]
    if codes is not None:
        if not codes:
            return []
        sql += " AND beat_id IN (" + ",".join("?" for _ in codes) + ")"
        params.extend(codes)
    if query:
        sql += " AND (summary_text LIKE ? OR beat_id=?)"
        params.extend(("%" + query + "%", query))
    if time_start:
        sql += " AND occurred_at_jst>=?"
        params.append(normalize_time_bound(time_start))
    if time_end:
        sql += " AND occurred_at_jst<=?"
        params.append(normalize_time_bound(time_end, end=True))
    sql += " ORDER BY occurred_at_jst DESC,beat_id DESC"
    rows = []
    active_ids = set(active_msg_ids)
    with sqlite3.connect(Path(ledger_path).resolve().as_uri() + "?mode=ro", uri=True) as ledger:
        # One consistent ledger view covers every source in each summary.
        ledger.execute("BEGIN")
        for row in db.execute(sql, params):
            item = dict(row)
            refs = db.execute(
                "SELECT source,account,chat_id,msg_id,revision FROM beat_sources WHERE beat_id=?",
                (item["beat_id"],),
            ).fetchall()
            if not refs or any(ref[3] in active_ids for ref in refs):
                continue
            valid = True
            for ref_source, ref_account, ref_chat, mid, revision in refs:
                current = ledger.execute(
                    "SELECT revision,kind FROM messages WHERE source=? AND account=? AND chat_id=? AND msg_id=?",
                    (ref_source, ref_account, ref_chat, mid),
                ).fetchone()
                if (
                    (ref_source, ref_account, ref_chat) != (source, account, chat_id)
                    or not current
                    or current[0] != revision
                    or (int(current[1]) & 65535) == 10000
                ):
                    valid = False
                    break
            if valid:
                rows.append(item)
                if len(rows) >= max(1, min(int(limit), 50)):
                    break
    return rows


def capture_turn_sources(
    messages: Sequence[Any],
    *,
    source: str,
    account: str,
    chat_id: str,
    ledger_path: str,
) -> List[Dict[str, str]]:
    """Pin revisions of the real input batch before generation."""
    from src.platform_io.wechat.projection import native_message_id

    refs = {}
    with sqlite3.connect(Path(ledger_path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        for message in messages:
            mid = str(message.message_id or "")
            native = mid.rsplit(":", 1)[-1]
            if not native.isdigit() or native_message_id(source, account, native) != mid:
                continue
            row = db.execute(
                "SELECT revision,kind FROM messages WHERE source=? AND account=? AND chat_id=? AND msg_id=?",
                (source, account, chat_id, native),
            ).fetchone()
            if row and (int(row[1]) & 65535) != 10000:
                refs[native] = {"msg_id": native, "revision": row[0]}
    return list(refs.values())
