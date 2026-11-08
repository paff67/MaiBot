"""先登记再发送；未知发送结果不能通过重试再次触发，并强制执行单分钟发包上限与随机打字延迟。"""

from pathlib import Path
from typing import Any, Dict, Optional
import hashlib
import json
import random
import sqlite3
import time


class RateLimitExceededError(ValueError):
    """单分钟发包频率超限异常。"""

    def __init__(self, message: str, *, count: int, limit: int, retry_after_ms: int) -> None:
        super().__init__(message)
        self.count = count
        self.limit = limit
        self.retry_after_ms = retry_after_ms


def compute_typing_delay_ms(
    text: str,
    *,
    base_min_ms: int = 650,
    base_max_ms: int = 1250,
    per_char_min_ms: int = 75,
    per_char_max_ms: int = 150,
    jitter_std_ms: float = 120.0,
    min_delay_ms: int = 800,
    max_delay_ms: int = 10000,
    rng: Optional[random.Random] = None,
) -> int:
    """根据消息长度 + 高斯/均匀随机抖动计算拟人打字延迟（毫秒）。"""
    r = rng or random
    char_len = max(1, len((text or "").strip()))
    base = r.uniform(base_min_ms, base_max_ms)
    typing = char_len * r.uniform(per_char_min_ms, per_char_max_ms)
    jitter = r.gauss(0.0, jitter_std_ms)
    total = int(round(base + typing + jitter))
    return max(min_delay_ms, min(max_delay_ms, total))


class SendLedger:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS send_requests ("
                "request_id TEXT PRIMARY KEY, "
                "digest TEXT NOT NULL, status TEXT NOT NULL, result TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS send_rate_window ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "request_id TEXT UNIQUE NOT NULL, "
                "chat_id TEXT NOT NULL, "
                "sent_at_ms INTEGER NOT NULL)"
            )

    @staticmethod
    def _digest(payload: Dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def lookup(self, request_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """只检查幂等状态，不写入 reserved（便于在消耗限流配额前识别已完成或未知状态请求）。"""
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
            raise ValueError("自动发送需要 8~128 字符 request_id")
        digest = self._digest(payload)
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT digest,status,result FROM send_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("request_id 已用于不同的发送内容或会话")
                if row[1] == "submitted":
                    return json.loads(row[2])
                raise ValueError("该发送请求结果未知，禁止自动重试触屏")
        return None

    def check_and_acquire_rate_slot(
        self,
        request_id: str,
        chat_id: str,
        *,
        max_per_minute: int = 6,
        window_ms: int = 60_000,
        now_ms: Optional[int] = None,
    ) -> Dict[str, int]:
        """滑动时间窗（默认 60s 最多 6 条）发包频率上限校验与配额占用。"""
        if max_per_minute < 1:
            raise ValueError("max_per_minute 必须 >= 1")
        current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        cutoff_ms = current_ms - window_ms
        with sqlite3.connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM send_rate_window WHERE sent_at_ms <= ?", (cutoff_ms,))
            rows = conn.execute(
                "SELECT sent_at_ms FROM send_rate_window WHERE sent_at_ms > ? ORDER BY sent_at_ms ASC",
                (cutoff_ms,),
            ).fetchall()
            count = len(rows)
            if count >= max_per_minute:
                oldest_ms = int(rows[0][0])
                retry_after_ms = max(1, (oldest_ms + window_ms) - current_ms)
                raise RateLimitExceededError(
                    f"单分钟发送频率超限 ({count}/{max_per_minute} 条/分钟)，请等待 {retry_after_ms}ms",
                    count=count,
                    limit=max_per_minute,
                    retry_after_ms=retry_after_ms,
                )
            conn.execute(
                "INSERT OR IGNORE INTO send_rate_window(request_id, chat_id, sent_at_ms) VALUES(?,?,?)",
                (request_id, chat_id, current_ms),
            )
        return {"window_count": count + 1, "max_per_minute": max_per_minute, "window_ms": window_ms}

    def reserve(self, request_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
            raise ValueError("自动发送需要 8~128 字符 request_id")
        digest = self._digest(payload)
        with sqlite3.connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT digest,status,result FROM send_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("request_id 已用于不同的发送内容或会话")
                if row[1] == "submitted":
                    return json.loads(row[2])
                raise ValueError("该发送请求结果未知，禁止自动重试触屏")
            conn.execute("INSERT INTO send_requests VALUES(?,?,'reserved',NULL)", (request_id, digest))
        return None

    def submitted(self, request_id: str, result: Dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE send_requests SET status='submitted',result=? WHERE request_id=?",
                (json.dumps(result, ensure_ascii=False), request_id),
            )

    def update_submitted(self, request_id: str, result: Dict[str, Any]) -> None:
        self.submitted(request_id, result)


