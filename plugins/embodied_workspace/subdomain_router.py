from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass, fields
from pathlib import Path

RESERVED_SUBDOMAINS = {
    "www", "api", "mail", "smtp", "imap", "pop", "ftp", "ssh",
    "admin", "panel", "cdn", "static", "assets", "s", "ns1", "ns2",
}

PIN_MAX_FAILURES = 5
PIN_LOCKOUT_SECONDS = 300.0


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
    issuer_name: str = ""


_ROUTE_FIELD_NAMES = {f.name for f in fields(SubdomainRoute)}


class SubdomainRouter:
    def __init__(
        self,
        db_path: Path,
        base_domain: str = "example.com",
        short_link_base_url: str = "",
    ) -> None:
        self.db_path = db_path
        self.base_domain = base_domain.strip().lstrip(".")
        self.short_link_base_url = short_link_base_url.strip().rstrip("/")
        self._pin_failures: dict[str, tuple[int, float]] = {}
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
                    last_notify_at REAL NOT NULL DEFAULT 0.0,
                    issuer_name TEXT NOT NULL DEFAULT ''
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
                "issuer_name": "TEXT NOT NULL DEFAULT ''",
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

    @staticmethod
    def _row_to_route(row: sqlite3.Row) -> SubdomainRoute:
        raw = dict(row)
        filtered = {k: v for k, v in raw.items() if k in _ROUTE_FIELD_NAMES}
        return SubdomainRoute(**filtered)

    def upsert_route(
        self,
        *,
        token: str,
        chat_id: str,
        stream_id: str,
        creator_wxid: str = "",
        issuer_name: str = "",
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
        clean_issuer = issuer_name.strip()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO subdomain_routes (
                    token, chat_id, stream_id, creator_wxid, issuer_name, access_policy,
                    project_slug, title, summary, mode,
                    dist_dir, project_dir, start_cmd_json,
                    created_at, expire_at, revoked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(token) DO UPDATE SET
                    stream_id=COALESCE(NULLIF(excluded.stream_id, ''), subdomain_routes.stream_id),
                    creator_wxid=COALESCE(NULLIF(excluded.creator_wxid, ''), subdomain_routes.creator_wxid),
                    issuer_name=COALESCE(NULLIF(excluded.issuer_name, ''), subdomain_routes.issuer_name),
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
                    token, chat_id, stream_id, creator_wxid, clean_issuer, policy,
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

    def list_chat_routes(self, chat_id: str, limit: int = 10) -> list[SubdomainRoute]:
        """查询当前微信会话下最近创建的活跃工作区路由列表"""
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM subdomain_routes
                WHERE chat_id = ? AND revoked = 0 AND expire_at > ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (chat_id, now, int(limit)),
            ).fetchall()
            return [self._row_to_route(r) for r in rows]

    def get_route(self, token: str) -> SubdomainRoute | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subdomain_routes WHERE token = ?", (token,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_route(row)

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
        with self._connect() as conn:
            conn.execute("DELETE FROM web_identity_claims WHERE expire_at < ?", (now,))
            code = ""
            for attempt in range(32):
                candidate = (
                    str(secrets.randbelow(9000) + 1000)
                    if attempt < 24
                    else str(secrets.randbelow(900000) + 100000)
                )
                exists = conn.execute(
                    "SELECT 1 FROM web_identity_claims WHERE claim_code = ? AND expire_at > ?",
                    (candidate, now),
                ).fetchone()
                if not exists:
                    code = candidate
                    break
            if not code:
                raise RuntimeError("无法分配唯一的微信绑定口令，请稍后重试")

            conn.execute(
                """
                INSERT INTO web_identity_claims
                (claim_code, browser_nonce, chat_id, verified_wxid, pin_to_set, expire_at)
                VALUES (?, ?, ?, '', ?, ?)
                """,
                (code, browser_nonce, chat_id, pin_to_set.strip(), now + 300.0),
            )
        return code

    def fulfill_claim_by_wechat(
        self,
        claim_code: str,
        sender_wxid: str,
        sender_nickname: str = "",
        *,
        source_chat_id: str = "",
        allowed_group_members: set[str] | None = None,
    ) -> bool:
        clean_sender = sender_wxid.strip()
        clean_code = claim_code.strip()
        clean_source_chat = source_chat_id.strip()
        if not clean_sender or not clean_code:
            return False

        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM web_identity_claims
                WHERE claim_code = ? AND verified_wxid = '' AND expire_at > ?
                """,
                (clean_code, now),
            ).fetchone()
            if not row:
                return False

            claim_chat_id = str(row["chat_id"] or "").strip()
            if claim_chat_id.endswith("@chatroom"):
                if clean_source_chat.endswith("@chatroom") and clean_source_chat != claim_chat_id:
                    return False
                if allowed_group_members is not None and clean_source_chat != claim_chat_id:
                    if clean_sender not in allowed_group_members:
                        return False
            else:
                if clean_source_chat.endswith("@chatroom"):
                    return False

            cur = conn.execute(
                "UPDATE web_identity_claims SET verified_wxid = ? WHERE claim_code = ? AND verified_wxid = ''",
                (clean_sender, clean_code),
            )
            if cur.rowcount <= 0:
                return False
            pin_raw = str(row["pin_to_set"] or "").strip()
            if len(pin_raw) >= 4:
                self._upsert_pin_in_conn(conn, clean_sender, pin_raw, sender_nickname, now)
                self._pin_failures.pop(clean_sender, None)
            return True

    def get_claim_chat_id(self, claim_code: str) -> str:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT chat_id FROM web_identity_claims WHERE claim_code = ? AND expire_at > ?",
                (claim_code.strip(), now),
            ).fetchone()
            return str(row["chat_id"] or "") if row else ""

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
        clean_wxid = wxid.strip()
        with self._connect() as conn:
            self._upsert_pin_in_conn(conn, clean_wxid, pin_raw, nickname, time.time())
        self._pin_failures.pop(clean_wxid, None)

    def is_pin_locked_out(self, wxid: str, *, now: float | None = None) -> bool:
        clean_wxid = wxid.strip()
        state = self._pin_failures.get(clean_wxid)
        if not state:
            return False
        _, locked_until = state
        eff_now = time.time() if now is None else now
        if locked_until > eff_now:
            return True
        if locked_until > 0:
            self._pin_failures.pop(clean_wxid, None)
        return False

    def verify_user_pin(self, wxid: str, pin_raw: str) -> bool:
        clean_wxid = wxid.strip()
        if not clean_wxid or not pin_raw.strip():
            return False
        now = time.time()
        if self.is_pin_locked_out(clean_wxid, now=now):
            return False

        pin_hash = hashlib.sha256(f"{clean_wxid}:{pin_raw.strip()}".encode("utf-8")).hexdigest()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT pin_hash FROM web_user_pins WHERE wxid = ?", (clean_wxid,)
            ).fetchone()
            ok = bool(row and hmac.compare_digest(str(row["pin_hash"]), pin_hash))

        if ok:
            self._pin_failures.pop(clean_wxid, None)
            return True

        prev_fails, _ = self._pin_failures.get(clean_wxid, (0, 0.0))
        new_fails = prev_fails + 1
        if new_fails >= PIN_MAX_FAILURES:
            self._pin_failures[clean_wxid] = (new_fails, now + PIN_LOCKOUT_SECONDS)
        else:
            self._pin_failures[clean_wxid] = (new_fails, 0.0)
        return False

    def get_pin_user_nickname(self, wxid: str) -> str:
        clean_wxid = wxid.strip()
        if not clean_wxid:
            return ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT nickname FROM web_user_pins WHERE wxid = ?", (clean_wxid,)
            ).fetchone()
            return str(row["nickname"] or "").strip() if row else ""

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
