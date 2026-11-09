from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from .subdomain_router import SubdomainRoute


class GroupRealtimeHub:
    """
    Phase 3 群组全双工实时同步总线。
    [v3.1 修正] 严格对齐 src/platform_io/wechat/message_ingestor.py 的生产 messages 表字段：
    优先读取 body / raw_content 与 updated_at_ms，兼容测试库字段。
    """

    def __init__(
        self,
        ledger_db_path: Path,
        poll_interval_sec: float = 1.0,
    ) -> None:
        self.ledger_db_path = ledger_db_path
        self.poll_interval_sec = poll_interval_sec
        self._chat_clients: dict[str, set[web.WebSocketResponse]] = {}
        self._token_clients: dict[str, set[web.WebSocketResponse]] = {}
        self._chat_cursors: dict[str, int] = {}
        self._poll_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._ledger_watch_loop())

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        for clients in list(self._token_clients.values()):
            for ws in list(clients):
                if not ws.closed:
                    await ws.close()
        self._chat_clients.clear()
        self._token_clients.clear()

    def _get_max_rowid_for_chat(self, chat_id: str) -> int:
        if not self.ledger_db_path.exists():
            return 0
        try:
            conn = sqlite3.connect(f"file:{self.ledger_db_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM messages WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception:
            return 0

    def _fetch_new_chat_messages(self, chat_id: str, after_rowid: int) -> list[dict[str, Any]]:
        if not self.ledger_db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{self.ledger_db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                cols = {
                    str(r["name"])
                    for r in conn.execute("PRAGMA table_info(messages)").fetchall()
                }
                if not cols:
                    return []

                # [v3.1 修正] 优先匹配生产 message_ledger.db 的真实字段名 (sender_id, body, updated_at_ms)
                sender_col = (
                    "sender_name" if "sender_name" in cols
                    else ("sender_id" if "sender_id" in cols else "''")
                )
                text_col = (
                    "body" if "body" in cols
                    else ("raw_message" if "raw_message" in cols
                    else ("content" if "content" in cols
                    else ("raw_content" if "raw_content" in cols else "''")))
                )
                ts_col = (
                    "updated_at_ms" if "updated_at_ms" in cols
                    else ("timestamp" if "timestamp" in cols
                    else ("create_time" if "create_time" in cols else "0"))
                )

                rows = conn.execute(
                    f"""
                    SELECT rowid, {sender_col} AS sender, {text_col} AS content, {ts_col} AS ts
                    FROM messages
                    WHERE chat_id = ? AND rowid > ?
                    ORDER BY rowid ASC LIMIT 20
                    """,
                    (chat_id, after_rowid),
                ).fetchall()
                return [
                    {
                        "rowid": int(r["rowid"]),
                        "sender": str(r["sender"] or ""),
                        "content": str(r["content"] or "")[:500],
                        "timestamp": r["ts"],
                    }
                    for r in rows
                ]
            finally:
                conn.close()
        except Exception:
            return []

    async def handle_ws_connection(
        self,
        request: web.Request,
        route: SubdomainRoute,
        actor_wxid: str | None,
    ) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=25.0)
        await ws.prepare(request)

        chat_id = route.chat_id
        token = route.token

        if chat_id not in self._chat_cursors:
            self._chat_cursors[chat_id] = await asyncio.to_thread(self._get_max_rowid_for_chat, chat_id)

        self._chat_clients.setdefault(chat_id, set()).add(ws)
        self._token_clients.setdefault(token, set()).add(ws)

        await ws.send_json(
            {
                "type": "connected",
                "token": token,
                "project_slug": route.project_slug,
                "actor_wxid": actor_wxid or "",
                "online_count": len(self._token_clients.get(token, set())),
            }
        )

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except Exception:
                        continue
                    action = str(payload.get("action") or "broadcast")
                    if action == "ping":
                        await ws.send_json({"type": "pong", "ts": time.time()})
                    elif action == "broadcast":
                        if actor_wxid or route.access_policy == "member_collect":
                            await self.broadcast_to_token(
                                token,
                                {
                                    "type": "peer_broadcast",
                                    "actor_wxid": actor_wxid or "anonymous",
                                    "event": str(payload.get("event") or "state_sync")[:80],
                                    "data": payload.get("data"),
                                    "ts": time.time(),
                                },
                                exclude_ws=ws if not payload.get("echo", True) else None,
                            )
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            self._chat_clients.get(chat_id, set()).discard(ws)
            self._token_clients.get(token, set()).discard(ws)

        return ws

    async def broadcast_to_token(
        self,
        token: str,
        message: dict[str, Any],
        exclude_ws: web.WebSocketResponse | None = None,
    ) -> int:
        clients = list(self._token_clients.get(token, set()))
        sent = 0
        for ws in clients:
            if ws is exclude_ws or ws.closed:
                continue
            try:
                await ws.send_json(message)
                sent += 1
            except Exception:
                self._token_clients.get(token, set()).discard(ws)
        return sent

    async def broadcast_to_chat(self, chat_id: str, message: dict[str, Any]) -> int:
        clients = list(self._chat_clients.get(chat_id, set()))
        sent = 0
        for ws in clients:
            if ws.closed:
                continue
            try:
                await ws.send_json(message)
                sent += 1
            except Exception:
                self._chat_clients.get(chat_id, set()).discard(ws)
        return sent

    async def push_chat_message_event(
        self,
        chat_id: str,
        sender: str,
        content: str,
        timestamp: float | None = None,
    ) -> int:
        eff_ts = timestamp or time.time()
        return await self.broadcast_to_chat(
            chat_id,
            {
                "type": "wechat_message",
                "chat_id": chat_id,
                "sender": sender,
                "content": content[:500],
                "ts": eff_ts,
                "timestamp": eff_ts,
            },
        )

    async def _ledger_watch_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval_sec)
            active_chats = [
                cid for cid, clients in self._chat_clients.items() if len(clients) > 0
            ]
            for chat_id in active_chats:
                last_rowid = self._chat_cursors.get(chat_id, 0)
                new_msgs = await asyncio.to_thread(
                    self._fetch_new_chat_messages, chat_id, last_rowid
                )
                for item in new_msgs:
                    self._chat_cursors[chat_id] = max(
                        self._chat_cursors.get(chat_id, 0), int(item["rowid"])
                    )
                    await self.broadcast_to_chat(
                        chat_id,
                        {
                            "type": "wechat_message",
                            "chat_id": chat_id,
                            "sender": item["sender"],
                            "content": item["content"],
                            "ts": item["timestamp"],
                            "timestamp": item["timestamp"],
                        },
                    )
