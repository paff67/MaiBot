"""Shared Root HTTP transport. No model client, chat history or reply policy."""

from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional
import asyncio
import hashlib
import json
import random
import time

import httpx


class WeChatEdgeClient:
    def __init__(
        self,
        source: str,
        account: str,
        *,
        token: str = "",
        client=None,
        use_hook_send: bool = False,
        max_sends_per_minute: int = 6,
        max_sends_per_chat_per_minute: Optional[int] = None,
        rate_limit_wait_seconds: float = 0.0,
    ):
        self.source, self.account = source.rstrip("/"), account
        self.client = client or httpx.AsyncClient(
            timeout=120, headers={"Authorization": "Bearer " + token} if token else {}
        )
        self.owns_client = client is None
        self.input_lock = asyncio.Lock()
        self.use_hook_send = use_hook_send
        self.max_sends_per_minute = max(1, int(max_sends_per_minute))
        self.max_sends_per_chat_per_minute = (
            max(1, int(max_sends_per_chat_per_minute))
            if max_sends_per_chat_per_minute is not None
            else None
        )
        self.rate_limit_wait_seconds = max(0.0, float(rate_limit_wait_seconds))
        self._send_timestamps: Deque[float] = deque()
        self._chat_send_timestamps: Dict[str, Deque[float]] = defaultdict(deque)
        self._seen_request_ids: set[str] = set()

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    def _prune_rate_windows(self, now: float, chat_id: str = "") -> float:
        while self._send_timestamps and now - self._send_timestamps[0] >= 60.0:
            self._send_timestamps.popleft()
        wait_needed = 0.0
        if len(self._send_timestamps) >= self.max_sends_per_minute:
            wait_needed = max(wait_needed, 60.0 - (now - self._send_timestamps[0]))
        if chat_id and self.max_sends_per_chat_per_minute is not None:
            chat_win = self._chat_send_timestamps[chat_id]
            while chat_win and now - chat_win[0] >= 60.0:
                chat_win.popleft()
            if len(chat_win) >= self.max_sends_per_chat_per_minute:
                wait_needed = max(wait_needed, 60.0 - (now - chat_win[0]))
        return max(0.0, wait_needed)

    def _record_rate_slot(self, now: float, request_id: str, chat_id: str = "") -> None:
        self._send_timestamps.append(now)
        if chat_id:
            self._chat_send_timestamps[chat_id].append(now)
        self._seen_request_ids.add(request_id)

    def _check_rate_limit(self, request_id: str, chat_id: str = "") -> None:
        if request_id in self._seen_request_ids:
            return
        now = time.monotonic()
        wait_needed = self._prune_rate_windows(now, chat_id)
        if wait_needed > 0.0:
            raise ValueError(
                f"WeChat client per-minute rate limit exceeded ({len(self._send_timestamps)}/{self.max_sends_per_minute})"
            )
        self._record_rate_slot(now, request_id, chat_id)

    async def _acquire_rate_limit(self, request_id: str, chat_id: str = "") -> None:
        if request_id in self._seen_request_ids:
            return
        deadline = time.monotonic() + self.rate_limit_wait_seconds
        while True:
            now = time.monotonic()
            wait_needed = self._prune_rate_windows(now, chat_id)
            if wait_needed <= 0.0:
                self._record_rate_slot(now, request_id, chat_id)
                return
            remaining_budget = deadline - now
            if self.rate_limit_wait_seconds <= 0.0 or wait_needed > remaining_budget:
                raise ValueError(
                    f"WeChat client per-minute rate limit exceeded ({len(self._send_timestamps)}/{self.max_sends_per_minute})"
                )
            jitter = random.uniform(0.08, 0.25)
            await asyncio.sleep(min(remaining_budget, wait_needed + jitter))

    async def snapshot(self, chat_id: str) -> Dict[str, Any]:
        response = await self.client.get(self.source + "/wechat/chat_snapshot")
        response.raise_for_status()
        data = response.json()
        if data.get("protocol_version") != 2 or data.get("account") != self.account or data.get("chat_id") != chat_id:
            raise ValueError("Phone account/conversation/protocol mismatch")
        return data

    async def send_text_via_hook(
        self, chat_id: str, text: str, internal_id: str, quote: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Send text or quote message in background via Frida hook without requiring foreground UI."""
        if not internal_id or not text.strip():
            raise ValueError("Sending requires stable internal message ID and nonempty text")
        request_id = hashlib.sha256(
            json.dumps([self.source, self.account, chat_id, internal_id]).encode()
        ).hexdigest()
        async with self.input_lock:
            await self._acquire_rate_limit(request_id, chat_id)
            payload = dict(
                text=text,
                expected_account=self.account,
                expected_chat_id=chat_id,
                request_id=request_id,
                max_per_minute=self.max_sends_per_minute,
            )
            if quote:
                payload["quote"] = quote
                endpoint = "/wechat/send_quote"
            else:
                endpoint = "/wechat/send_text"
            response = await self.client.post(self.source + endpoint, json=payload)
            if response.status_code == 429:
                body = response.json()
                raise ValueError(body.get("error") or "Edge per-minute send rate limit exceeded")
            response.raise_for_status()
            ack = response.json()
            if not ack.get("ok") or not ack.get("sent"):
                raise RuntimeError("Phone did not acknowledge Frida hook submission")
            return {
                "status": ack.get("status", "submitted_unconfirmed"),
                "request_id": request_id,
                "msg_id": ack.get("msg_id"),
                "msg_svr_id": ack.get("msg_svr_id"),
                "create_time": ack.get("create_time"),
                "send_layer": ack.get("send_layer"),
                "chatting_context_talker": ack.get("chatting_context_talker"),
                "typing_delay_ms": ack.get("typing_delay_ms"),
            }

    async def send_quote(
        self, chat_id: str, text: str, quote: Dict[str, Any], internal_id: str
    ) -> Dict[str, Any]:
        """Send a quote reply message via Frida hook."""
        return await self.send_text_via_hook(chat_id, text, internal_id, quote=quote)

    async def send_text(
        self, chat_id: str, text: str, internal_id: str, quote: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if self.use_hook_send:
            return await self.send_text_via_hook(chat_id, text, internal_id, quote=quote)
        from .screen_fallback import WeChatScreenParser

        if not internal_id or not text.strip():
            raise ValueError("Sending requires stable internal message ID and nonempty text")
        request_id = hashlib.sha256(
            json.dumps([self.source, self.account, chat_id, internal_id]).encode()
        ).hexdigest()
        async with self.input_lock:
            parser = WeChatScreenParser()
            before = await self.snapshot(chat_id)
            state = parser.parse_screen(
                before["xml"], conversation_id=self.account + "/" + chat_id, is_group_chat=chat_id.endswith("@chatroom")
            )
            if state.input_box_center is None:
                raise ValueError("Chat input unavailable; no automatic navigation")
            self._check_rate_limit(request_id)
            payload = dict(
                text=text,
                auto_send=True,
                expected_account=self.account,
                expected_chat_id=chat_id,
                request_id=request_id,
            )
            payload["tap_x"], payload["tap_y"] = state.input_box_center
            response = await self.client.post(self.source + "/touch/input_text", json=payload)
            response.raise_for_status()
            ack = response.json()
            if not ack.get("ok") or not ack.get("sent"):
                raise RuntimeError("Phone did not acknowledge submission")
            after = await self.snapshot(chat_id)
            confirmed = parser.parse_screen(
                after["xml"], conversation_id=self.account + "/" + chat_id, is_group_chat=chat_id.endswith("@chatroom")
            )
            old = {b.fingerprint for b in state.bubbles if b.role == "SELF"}
            seen = any(
                b.role == "SELF" and b.content.strip() == text.strip() and b.fingerprint not in old
                for b in confirmed.bubbles
            )
            return {"status": "confirmed" if seen else "submitted_unconfirmed", "request_id": payload["request_id"]}
