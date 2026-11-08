"""Friend-group access: private chats may read joined groups; groups never read DMs."""

from typing import Iterable
import re


GROUP_MEMBERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_members(
 source TEXT NOT NULL,account TEXT NOT NULL,group_id TEXT NOT NULL,user_id TEXT NOT NULL,
 PRIMARY KEY(source,account,group_id,user_id));
"""


def _check_group(source, account, group_id):
    if not source or not re.fullmatch(r"[a-f0-9]{32}", account) or not group_id.endswith("@chatroom"):
        raise ValueError("Explicit source/account/group required")


def replace_group_members(db, *, source, account, group_id, user_ids: Iterable[str]):
    _check_group(source, account, group_id)
    users = sorted(set(user_ids))
    if any(not isinstance(u, str) or not u or u.endswith("@chatroom") for u in users):
        raise ValueError("Members must be real user IDs")
    with db:
        db.execute("DELETE FROM group_members WHERE source=? AND account=? AND group_id=?", (source, account, group_id))
        db.executemany("INSERT INTO group_members VALUES(?,?,?,?)", [(source, account, group_id, u) for u in users])
    return len(users)


def set_group_member(db, *, source, account, group_id, user_id, present):
    _check_group(source, account, group_id)
    if not user_id or user_id.endswith("@chatroom"):
        raise ValueError("Real user ID required")
    with db:
        if present:
            db.execute("INSERT OR IGNORE INTO group_members VALUES(?,?,?,?)", (source, account, group_id, user_id))
        else:
            db.execute(
                "DELETE FROM group_members WHERE source=? AND account=? AND group_id=? AND user_id=?",
                (source, account, group_id, user_id),
            )


def allowed_conversations(db, *, source, account, requester_id, destination_chat_id):
    if destination_chat_id.endswith("@chatroom"):
        return {destination_chat_id}
    if not requester_id or requester_id != destination_chat_id:
        return set()
    groups = db.execute(
        "SELECT group_id FROM group_members WHERE source=? AND account=? AND user_id=?", (source, account, requester_id)
    ).fetchall()
    return {destination_chat_id, *(r[0] for r in groups)}


def can_read_memory(db, code, *, source, account, conversations):
    provenance = db.execute(
        "SELECT source,account,chat_id FROM memory_provenance WHERE code_index=?", (code,)
    ).fetchall()
    return bool(provenance) and all(r[0] == source and r[1] == account and r[2] in conversations for r in provenance)


def authorized_file_rows(db, ledger, *, source, account, destination_chat_id, codes=None):
    """Only verified live message links can authorize file content or its index."""
    chats = allowed_conversations(
        db, source=source, account=account, requester_id=destination_chat_id, destination_chat_id=destination_chat_id
    )
    if not chats or codes == []:
        return []
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='message_asset_links'").fetchone():
        return []
    sql = (
        "SELECT DISTINCT a.file_code FROM message_asset_links l JOIN message_media_jobs j USING(job_id) "
        "JOIN asset_sources a USING(file_code) WHERE j.source=? AND l.account=? "
        "AND l.active=1 AND j.state='ready' AND a.association='verified'"
    )
    params = [source, account]
    if codes is not None:
        sql += " AND a.file_code IN (" + ",".join("?" for _ in codes) + ")"
        params.extend(codes)
    result = []
    for (code,) in db.execute(sql, params):
        links = db.execute(
            "SELECT j.source,l.account,l.chat_id,l.msg_id,l.revision,l.active,j.state "
            "FROM message_asset_links l JOIN message_media_jobs j USING(job_id) WHERE l.file_code=? AND l.active=1",
            (code,),
        ).fetchall()
        valid = bool(links)
        for s, a, chat, mid, revision, _active, state in links:
            if s != source or a != account or chat not in chats or state != "ready":
                valid = False
                break
            current = ledger.execute(
                "SELECT revision,chat_id,kind FROM messages WHERE source=? AND account=? AND msg_id=?", (s, a, mid)
            ).fetchone()
            if not current or current[0] != revision or current[1] != chat or (int(current[2] or 0) & 65535) == 10000:
                valid = False
                break
        origin = db.execute("SELECT chat_id FROM asset_sources WHERE file_code=?", (code,)).fetchone()
        if not origin or origin[0] not in {account + "/" + chat for chat in chats}:
            valid = False
        if valid:
            row = db.execute("SELECT * FROM media_vault WHERE file_code=?", (code,)).fetchone()
            if row is not None:
                result.append(dict(row))
    return result
