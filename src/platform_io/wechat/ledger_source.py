"""Read-only selection and persistent incremental consumption of explicit ledger messages."""

from pathlib import Path
from typing import Any, Dict, Optional
import asyncio
import json
import sqlite3
import time


_CONSUMER_SCHEMA = """
CREATE TABLE IF NOT EXISTS wechat_driver_cursors(
    driver_id TEXT NOT NULL,
    source TEXT NOT NULL,
    account TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY(driver_id, source, account, chat_id)
);
CREATE TABLE IF NOT EXISTS wechat_driver_delivered(
    driver_id TEXT NOT NULL,
    source TEXT NOT NULL,
    account TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    PRIMARY KEY(driver_id, source, account, msg_id)
);
CREATE TABLE IF NOT EXISTS wechat_driver_quarantine(
    driver_id TEXT NOT NULL,
    source TEXT NOT NULL,
    account TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    error TEXT NOT NULL,
    quarantined_at_ms INTEGER NOT NULL,
    PRIMARY KEY(driver_id, source, account, msg_id)
);
"""


class LedgerSource:
    def __init__(
        self,
        path: str,
        source: str,
        account: str,
        *,
        backfill: bool = False,
        state_db_path: Optional[str] = None,
    ):
        self.path = Path(path).resolve()
        self.source = source.rstrip("/")
        self.account = account
        self.backfill = backfill
        self.state_path = Path(state_db_path).resolve() if state_db_path else self.path

    def get(self, chat_id: str, msg_id: str) -> Dict[str, Any]:
        with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM messages WHERE source=? AND account=? AND chat_id=? AND msg_id=?",
                (self.source, self.account, chat_id, msg_id),
            ).fetchone()
        db.close()
        if row is None:
            raise ValueError("Message not found within bound device/account/conversation")
        return dict(row)

    def _open_state_db(self) -> sqlite3.Connection:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.state_path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        db.executescript(_CONSUMER_SCHEMA)
        if self.state_path != self.path:
            db.execute("ATTACH DATABASE ? AS ledger", (self.path.as_uri() + "?mode=ro",))
        return db

    def _table_prefix(self) -> str:
        return "ledger." if self.state_path != self.path else ""

    def _prepare_batch(self, driver_id: str, allowed_chats: list[str], backfill: bool, limit: int):
        db = self._open_state_db()
        prefix = self._table_prefix()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                start_seq = (
                    0
                    if backfill
                    else db.execute(
                        f"SELECT COALESCE(MAX(rowid), 0) FROM {prefix}message_revisions WHERE source=?",
                        (self.source,),
                    ).fetchone()[0]
                )
                for chat_id in allowed_chats:
                    db.execute(
                        "INSERT OR IGNORE INTO wechat_driver_cursors(driver_id, source, account, chat_id, seq) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (driver_id, self.source, self.account, chat_id, start_seq),
                    )
                placeholders = ",".join("?" for _ in allowed_chats)
                cursor_rows = db.execute(
                    f"SELECT chat_id, seq FROM wechat_driver_cursors "
                    f"WHERE driver_id=? AND source=? AND account=? AND chat_id IN ({placeholders})",
                    (driver_id, self.source, self.account, *allowed_chats),
                ).fetchall()
                chat_cursors = {row["chat_id"]: int(row["seq"]) for row in cursor_rows}
                min_seq = min(chat_cursors.values())
                revisions = db.execute(
                    f"SELECT rowid, payload FROM {prefix}message_revisions "
                    f"WHERE source=? AND rowid>? ORDER BY rowid LIMIT ?",
                    (self.source, min_seq, limit),
                ).fetchall()
                items = []
                for rev_row in revisions:
                    rowid = int(rev_row["rowid"])
                    event = json.loads(rev_row["payload"])
                    chat_id = event.get("chat_id")
                    msg_id = str(event.get("msg_id") or "")
                    message_row = None
                    already_delivered = False
                    if (
                        event.get("account") == self.account
                        and (chat_id in chat_cursors or "*" in chat_cursors)
                        and msg_id
                    ):
                        msg_res = db.execute(
                            f"SELECT * FROM {prefix}messages WHERE source=? AND account=? AND chat_id=? AND msg_id=?",
                            (self.source, self.account, chat_id, msg_id),
                        ).fetchone()
                        if msg_res is not None:
                            message_row = dict(msg_res)
                        already_delivered = (
                            db.execute(
                                "SELECT 1 FROM wechat_driver_delivered "
                                "WHERE driver_id=? AND source=? AND account=? AND msg_id=?",
                                (driver_id, self.source, self.account, msg_id),
                            ).fetchone()
                            is not None
                        )
                    items.append(
                        {
                            "rowid": rowid,
                            "event": event,
                            "message": message_row,
                            "already_delivered": already_delivered,
                        }
                    )
                return chat_cursors, items
        finally:
            db.close()

    def _advance_cursor(
        self,
        driver_id: str,
        allowed_chats: list[str],
        rowid: int,
        delivered_msg_id: Optional[str] = None,
    ) -> None:
        db = self._open_state_db()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if delivered_msg_id is not None:
                    db.execute(
                        "INSERT OR IGNORE INTO wechat_driver_delivered(driver_id, source, account, msg_id) "
                        "VALUES (?, ?, ?, ?)",
                        (driver_id, self.source, self.account, delivered_msg_id),
                    )
                placeholders = ",".join("?" for _ in allowed_chats)
                db.execute(
                    f"UPDATE wechat_driver_cursors SET seq=? "
                    f"WHERE driver_id=? AND source=? AND account=? AND chat_id IN ({placeholders}) AND seq<?",
                    (rowid, driver_id, self.source, self.account, *allowed_chats, rowid),
                )
        finally:
            db.close()

    def _quarantine_and_advance(
        self,
        driver_id: str,
        allowed_chats: list[str],
        rowid: int,
        msg_id: str,
        revision: str,
        error: str,
    ) -> None:
        db = self._open_state_db()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR REPLACE INTO wechat_driver_quarantine("
                    "driver_id, source, account, msg_id, revision, error, quarantined_at_ms"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (driver_id, self.source, self.account, msg_id, revision, error, int(time.time() * 1000)),
                )
                placeholders = ",".join("?" for _ in allowed_chats)
                db.execute(
                    f"UPDATE wechat_driver_cursors SET seq=? "
                    f"WHERE driver_id=? AND source=? AND account=? AND chat_id IN ({placeholders}) AND seq<?",
                    (rowid, driver_id, self.source, self.account, *allowed_chats, rowid),
                )
        finally:
            db.close()

    async def consume_once(self, driver, limit: int = 50) -> int:
        if not 1 <= limit <= 500:
            raise ValueError("Invalid batch limit")
        allowed_chats = sorted(driver.allowed_chats)
        if not allowed_chats:
            return 0
        backfill = bool(getattr(driver, "backfill", self.backfill) or self.backfill)
        chat_cursors, items = await asyncio.to_thread(
            self._prepare_batch, driver.driver_id, allowed_chats, backfill, limit
        )
        delivered = 0
        delivered_in_batch: set[str] = set()
        for item in items:
            rowid = item["rowid"]
            event = item["event"]
            chat_id = event.get("chat_id")
            msg_id = str(event.get("msg_id") or "")
            if event.get("account") != self.account or (chat_id not in chat_cursors and "*" not in chat_cursors):
                await asyncio.to_thread(self._advance_cursor, driver.driver_id, allowed_chats, rowid)
                for c in allowed_chats:
                    if chat_cursors[c] < rowid:
                        chat_cursors[c] = rowid
                continue
            effective_seq = chat_cursors.get(chat_id, chat_cursors.get("*", rowid))
            if effective_seq >= rowid:
                await asyncio.to_thread(self._advance_cursor, driver.driver_id, allowed_chats, rowid)
                for c in allowed_chats:
                    if chat_cursors[c] < rowid:
                        chat_cursors[c] = rowid
                continue
            row = item["message"]
            is_revoked = row is not None and (int(row.get("kind") or 0) & 65535) == 10000
            if is_revoked and msg_id and hasattr(driver, "revoke_committed"):
                try:
                    await driver.revoke_committed(chat_id=str(chat_id or ""), msg_id=msg_id)
                except Exception:
                    pass
            if (
                row is None
                or row.get("direction") != "incoming"
                or is_revoked
                or item["already_delivered"]
                or msg_id in delivered_in_batch
            ):
                await asyncio.to_thread(self._advance_cursor, driver.driver_id, allowed_chats, rowid)
                for c in allowed_chats:
                    if chat_cursors[c] < rowid:
                        chat_cursors[c] = rowid
                continue
            before_tasks = set(asyncio.all_tasks())
            try:
                accepted = await driver.deliver_committed(dict(row))
                spawned = [
                    t
                    for t in (set(asyncio.all_tasks()) - before_tasks)
                    if not t.get_name().startswith("wechat_ledger_poll:")
                ]
                if spawned:
                    done, _pending = await asyncio.wait(spawned, timeout=0.05)
                    for task in done:
                        if not task.cancelled() and task.exception() is not None:
                            raise task.exception()
            except ValueError as exc:
                await asyncio.to_thread(
                    self._quarantine_and_advance,
                    driver.driver_id,
                    allowed_chats,
                    rowid,
                    msg_id,
                    str(row.get("revision") or ""),
                    f"{type(exc).__name__}: {exc}",
                )
                for c in allowed_chats:
                    if chat_cursors[c] < rowid:
                        chat_cursors[c] = rowid
                continue
            except Exception:
                break
            if not accepted:
                break
            delivered_in_batch.add(msg_id)
            await asyncio.to_thread(
                self._advance_cursor, driver.driver_id, allowed_chats, rowid, msg_id
            )
            for c in allowed_chats:
                if chat_cursors[c] < rowid:
                    chat_cursors[c] = rowid
            delivered += 1
        return delivered


WeChatLedgerConsumer = LedgerSource
