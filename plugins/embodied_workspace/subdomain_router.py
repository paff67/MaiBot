from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SubdomainRoute:
    token: str
    chat_id: str
    stream_id: str
    project_slug: str
    title: str
    mode: str                 # "static" | "backend"
    dist_dir: str
    project_dir: str
    start_cmd_json: str       # JSON list of str for backend mode
    created_at: float
    expire_at: float
    revoked: int = 0
    last_notify_at: float = 0.0


class SubdomainRouter:
    def __init__(self, db_path: Path, base_domain: str) -> None:
        self.db_path = db_path
        self.base_domain = base_domain.strip().lstrip(".")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subdomain_routes (
                    token TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    project_slug TEXT NOT NULL,
                    title TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'static',
                    dist_dir TEXT NOT NULL,
                    project_dir TEXT NOT NULL,
                    start_cmd_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    expire_at REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    last_notify_at REAL NOT NULL DEFAULT 0.0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_routes_chat_slug ON subdomain_routes(chat_id, project_slug)"
            )

    def format_url(self, token: str) -> str:
        return f"https://{token}.{self.base_domain}"

    def upsert_route(
        self,
        *,
        token: str,
        chat_id: str,
        stream_id: str,
        project_slug: str,
        title: str,
        mode: str,
        dist_dir: Path,
        project_dir: Path,
        start_cmd: list[str] | None = None,
        ttl_hours: int = 24,
    ) -> str:
        now = time.time()
        expire_at = now + ttl_hours * 3600.0
        cmd_json = json.dumps(start_cmd or [], ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO subdomain_routes (
                    token, chat_id, stream_id, project_slug, title, mode,
                    dist_dir, project_dir, start_cmd_json, created_at, expire_at, revoked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(token) DO UPDATE SET
                    title=excluded.title,
                    mode=excluded.mode,
                    dist_dir=excluded.dist_dir,
                    project_dir=excluded.project_dir,
                    start_cmd_json=excluded.start_cmd_json,
                    expire_at=excluded.expire_at,
                    revoked=0
                """,
                (
                    token, chat_id, stream_id, project_slug, title, mode,
                    str(dist_dir), str(project_dir), cmd_json, now, expire_at,
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
            return SubdomainRoute(**dict(row)) if row else None

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
