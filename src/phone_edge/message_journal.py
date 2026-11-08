"""Root read-only access to the app-side committed message journal and spool-based outbound send bridge."""

from pathlib import Path
from typing import Any, Dict, Optional
import json
import os
import sqlite3


JOURNAL = "/data/user/0/com.tencent.mm/files/maibot_message_journal.db"
SPOOL_DIR = "/data/user/0/com.tencent.mm/files/maibot_send_spool"


def enqueue_send_spool(payload: Dict[str, Any], spool_dir: str = SPOOL_DIR) -> Path:
    """Write an atomic outbound send command file into the app-owned spool directory.

    Only message_capture.js inside com.tencent.mm writes to maibot_message_journal.db,
    preventing multi-process SQLite WAL lock contention or UID ownership mismatch.
    """
    request_id = str(payload["request_id"])
    spool = Path(spool_dir)
    files_root = spool.parent
    spool.mkdir(parents=True, exist_ok=True)
    try:
        st = files_root.stat()
        if hasattr(os, "chown"):
            os.chown(spool, st.st_uid, st.st_gid)
        os.chmod(spool, 0o770)
        if hasattr(os, "getxattr") and hasattr(os, "setxattr"):
            try:
                ctx = os.getxattr(files_root, "security.selinux")
                os.setxattr(spool, "security.selinux", ctx)
            except OSError:
                pass
    except OSError:
        st = None

    tmp_path = spool / f".{request_id}.tmp"
    final_path = spool / f"{request_id}.json"
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if st is not None:
        try:
            if hasattr(os, "chown"):
                os.chown(tmp_path, st.st_uid, st.st_gid)
            os.chmod(tmp_path, 0o660)
            if hasattr(os, "getxattr") and hasattr(os, "setxattr"):
                try:
                    ctx = os.getxattr(files_root, "security.selinux")
                    os.setxattr(tmp_path, "security.selinux", ctx)
                except OSError:
                    pass
        except OSError:
            pass
    os.replace(tmp_path, final_path)
    return final_path


def read_send_outbox(request_id: str, path: str = JOURNAL) -> Optional[Dict[str, Any]]:
    """Read outbound send state from send_outbox table in read-only mode."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        with sqlite3.connect(p.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            row = db.execute(
                "SELECT request_id,account,chat_id,status,created_at_ms,updated_at_ms,"
                "msg_id,msg_svr_id,create_time,send_layer,chatting_context_talker,error "
                "FROM send_outbox WHERE request_id=?",
                (request_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return {
        "request_id": row[0],
        "account": row[1],
        "chat_id": row[2],
        "status": row[3],
        "created_at_ms": row[4],
        "updated_at_ms": row[5],
        "msg_id": row[6],
        "msg_svr_id": row[7],
        "create_time": row[8],
        "send_layer": row[9],
        "chatting_context_talker": row[10],
        "error": row[11],
    }


def read_group_members(account, group_id, path=JOURNAL):
    with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=10) as db:
        row = db.execute(
            "SELECT payload FROM group_member_snapshots WHERE account=? AND group_id=?", (account, group_id)
        ).fetchone()
    if row is None:
        raise ValueError("Group membership snapshot not available")
    return dict(ok=True, **json.loads(row[0]))


def read_message_events(after: int = 0, limit: int = 100, path: str = JOURNAL) -> dict:
    if after < 0 or not 1 <= limit <= 500:
        raise ValueError("Invalid message cursor")
    with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=10) as db:
        epoch = db.execute("SELECT value FROM journal_meta WHERE key='epoch'").fetchone()[0]
        rows = db.execute(
            "SELECT seq,payload FROM message_events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)
        ).fetchall()
        maximum = db.execute("SELECT coalesce(max(seq),0) FROM message_events").fetchone()[0]
        state = db.execute("SELECT account,highwater,sweep FROM capture_state").fetchall()
        heartbeat = db.execute("SELECT value FROM journal_meta WHERE key='last_scan_ms'").fetchone()
    return dict(
        ok=True,
        epoch=epoch,
        events=[dict(seq=seq, **json.loads(payload)) for seq, payload in rows],
        next_cursor=rows[-1][0] if rows else after,
        max_cursor=maximum,
        state=state,
        last_scan_ms=int(heartbeat[0]) if heartbeat else None,
    )
