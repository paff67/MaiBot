from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

RESERVED_SUBDOMAINS = {
    "www", "api", "mail", "smtp", "imap", "pop", "ftp", "ssh",
    "admin", "panel", "cdn", "static", "assets", "s", "ns1", "ns2",
}


@dataclass
class SubdomainRoute:
    token: str
    chat_id: str
    stream_id: str
    creator_wxid: str
    access_policy: str        # "creator_only" | "member_collect"
    project_slug: str
    title: str
    summary: str
    mode: str                 # "static" | "backend"
    dist_dir: str
    project_dir: str
    start_cmd_json: str
    created_at: float
    expire_at: float
    revoked: int = 0
    last_notify_at: float = 0.0


class SubdomainRouter:
    def __init__(
        self,
        db_path: Path,
        base_domain: str = "paff-67.top",
        short_link_base_url: str = "",
    ) -> None:
        self.db_path = db_path
        self.base_domain = base_domain.strip().lstrip(".")
        self.short_link_base_url = short_link_base_url.strip().rstrip("/")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS subdomain_routes (
                    token TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    creator_wxid TEXT NOT NULL DEFAULT '',
                    access_policy TEXT NOT NULL DEFAULT 'creator_only',
                    project_slug TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT 'static',
                    dist_dir TEXT NOT NULL,
                    project_dir TEXT NOT NULL,
                    start_cmd_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    expire_at REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    last_notify_at REAL NOT NULL DEFAULT 0.0
                );

                CREATE INDEX IF NOT EXISTS idx_routes_chat_slug
                ON subdomain_routes(chat_id, project_slug);

                CREATE TABLE IF NOT EXISTS web_user_pins (
                    wxid TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL DEFAULT '',
                    pin_hash TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS web_identity_claims (
                    claim_code TEXT PRIMARY KEY,
                    browser_nonce TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    verified_wxid TEXT NOT NULL DEFAULT '',
                    pin_to_set TEXT NOT NULL DEFAULT '',
                    expire_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS web_handoff_tickets (
                    ticket TEXT PRIMARY KEY,
                    wxid TEXT NOT NULL,
                    expire_at REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            existing_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(subdomain_routes)").fetchall()
            }
            for col_name, col_def in {
                "creator_wxid": "TEXT NOT NULL DEFAULT ''",
                "access_policy": "TEXT NOT NULL DEFAULT 'creator_only'",
                "summary": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE subdomain_routes ADD COLUMN {col_name} {col_def}")

    @staticmethod
    def generate_short_token(length: int = 6) -> str:
        alphabet = "23456789abcdefghjkmnpqrstuvwxyz"
        while True:
            candidate = "".join(secrets.choice(alphabet) for _ in range(length))
            if candidate not in RESERVED_SUBDOMAINS:
                return candidate

    def format_url(self, token: str) -> str:
        if self.short_link_base_url:
            return f"{self.short_link_base_url}/{token}"
        return f"https://{token}.{self.base_domain}"

    def format_direct_url(self, token: str) -> str:
        return f"https://{token}.{self.base_domain}"

    def upsert_route(
        self,
        *,
        token: str,
        chat_id: str,
        stream_id: str,
        creator_wxid: str = "",
        access_policy: str = "creator_only",
        project_slug: str,
        title: str,
        summary: str = "",
        mode: str,
        dist_dir: Path,
        project_dir: Path,
        start_cmd: list[str] | None = None,
        ttl_hours: int = 24,
    ) -> str:
        now = time.time()
        expire_at = now + ttl_hours * 3600.0
        cmd_json = json.dumps(start_cmd or [], ensure_ascii=False)
        policy = "member_collect" if access_policy == "member_collect" else "creator_only"

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO subdomain_routes (
                    token, chat_id, stream_id, creator_wxid, access_policy,
                    project_slug, title, summary, mode,
                    dist_dir, project_dir, start_cmd_json,
                    created_at, expire_at, revoked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(token) DO UPDATE SET
                    title=excluded.title,
                    summary=excluded.summary,
                    access_policy=excluded.access_policy,
                    mode=excluded.mode,
                    dist_dir=excluded.dist_dir,
                    project_dir=excluded.project_dir,
                    start_cmd_json=excluded.start_cmd_json,
                    expire_at=excluded.expire_at,
                    revoked=0
                """,
                (
                    token, chat_id, stream_id, creator_wxid, policy,
                    project_slug, title, summary, mode,
                    str(dist_dir), str(project_dir), cmd_json,
                    now, expire_at,
                ),
            )
        return self.format_url(token)

    def find_active_token_by_slug(self, chat_id: str, project_slug: str) -> str | None:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT token FROM subdomain_routes
                WHERE chat_id = ? AND project_slug = ? AND revoked = 0 AND expire_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (chat_id, project_slug, now),
            ).fetchone()
            return str(row["token"]) if row else None

    def get_route(self, token: str) -> SubdomainRoute | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subdomain_routes WHERE token = ?", (token,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d.pop("first_visit_auto_claim", None)
            return SubdomainRoute(**d)

    def revoke_token(self, token: str, chat_id: str | None = None) -> bool:
        with self._connect() as conn:
            if chat_id:
                cur = conn.execute(
                    "UPDATE subdomain_routes SET revoked = 1 WHERE token = ? AND chat_id = ?",
                    (token, chat_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE subdomain_routes SET revoked = 1 WHERE token = ?", (token,)
                )
            return cur.rowcount > 0

    def check_and_touch_notify_cooldown(self, token: str, cooldown_sec: float = 45.0) -> bool:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_notify_at FROM subdomain_routes WHERE token = ?", (token,)
            ).fetchone()
            if not row:
                return False
            if now - float(row["last_notify_at"]) < cooldown_sec:
                return False
            conn.execute(
                "UPDATE subdomain_routes SET last_notify_at = ? WHERE token = ?",
                (now, token),
            )
            return True

    def create_claim(self, chat_id: str, browser_nonce: str, pin_to_set: str = "") -> str:
        now = time.time()
        code = str(secrets.randbelow(9000) + 1000)
        with self._connect() as conn:
            conn.execute("DELETE FROM web_identity_claims WHERE expire_at < ?", (now,))
            conn.execute(
                """
                INSERT OR REPLACE INTO web_identity_claims
                (claim_code, browser_nonce, chat_id, verified_wxid, pin_to_set, expire_at)
                VALUES (?, ?, ?, '', ?, ?)
                """,
                (code, browser_nonce, chat_id, pin_to_set.strip(), now + 300.0),
            )
        return code

    def fulfill_claim_by_wechat(self, claim_code: str, sender_wxid: str, sender_nickname: str = "") -> bool:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM web_identity_claims WHERE claim_code = ? AND expire_at > ?",
                (claim_code.strip(), now),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE web_identity_claims SET verified_wxid = ? WHERE claim_code = ?",
                (sender_wxid, claim_code.strip()),
            )
            pin_raw = str(row["pin_to_set"] or "").strip()
            if len(pin_raw) >= 4:
                self._upsert_pin_in_conn(conn, sender_wxid, pin_raw, sender_nickname, now)
            return True

    def poll_claim(self, claim_code: str, browser_nonce: str) -> str | None:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT verified_wxid FROM web_identity_claims
                WHERE claim_code = ? AND browser_nonce = ? AND expire_at > ?
                """,
                (claim_code.strip(), browser_nonce.strip(), now),
            ).fetchone()
            if row and row["verified_wxid"]:
                wxid = str(row["verified_wxid"])
                conn.execute("DELETE FROM web_identity_claims WHERE claim_code = ?", (claim_code.strip(),))
                return wxid
            return None

    @staticmethod
    def _upsert_pin_in_conn(
        conn: sqlite3.Connection, wxid: str, pin_raw: str, nickname: str, now: float
    ) -> None:
        pin_hash = hashlib.sha256(f"{wxid}:{pin_raw.strip()}".encode("utf-8")).hexdigest()
        conn.execute(
            """
            INSERT INTO web_user_pins (wxid, nickname, pin_hash, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(wxid) DO UPDATE SET
                nickname = COALESCE(NULLIF(excluded.nickname, ''), web_user_pins.nickname),
                pin_hash = excluded.pin_hash,
                updated_at = excluded.updated_at
            """,
            (wxid, nickname, pin_hash, now),
        )

    def set_user_pin(self, wxid: str, pin_raw: str, nickname: str = "") -> None:
        with self._connect() as conn:
            self._upsert_pin_in_conn(conn, wxid, pin_raw, nickname, time.time())

    def verify_user_pin(self, wxid: str, pin_raw: str) -> bool:
        pin_hash = hashlib.sha256(f"{wxid}:{pin_raw.strip()}".encode("utf-8")).hexdigest()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT pin_hash FROM web_user_pins WHERE wxid = ?", (wxid,)
            ).fetchone()
            return bool(row and hmac.compare_digest(str(row["pin_hash"]), pin_hash))

    def list_pin_users(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT wxid, nickname FROM web_user_pins ORDER BY updated_at DESC"
            ).fetchall()
            return [{"wxid": str(r["wxid"]), "nickname": str(r["nickname"] or r["wxid"])} for r in rows]

    def create_handoff_ticket(self, wxid: str, ttl_sec: float = 90.0) -> str:
        ticket = secrets.token_urlsafe(16)
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM web_handoff_tickets WHERE expire_at < ?", (now,))
            conn.execute(
                "INSERT INTO web_handoff_tickets (ticket, wxid, expire_at, used) VALUES (?, ?, ?, 0)",
                (ticket, wxid, now + ttl_sec),
            )
        return ticket

    def consume_handoff_ticket(self, ticket: str) -> str | None:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT wxid FROM web_handoff_tickets WHERE ticket = ? AND used = 0 AND expire_at > ?",
                (ticket.strip(), now),
            ).fetchone()
            if not row:
                return None
            conn.execute("UPDATE web_handoff_tickets SET used = 1 WHERE ticket = ?", (ticket.strip(),))
            return str(row["wxid"])
