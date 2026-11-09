from __future__ import annotations

import asyncio
import datetime
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp


@dataclass
class ComputeEconomySnapshot:
    balance_usd: float                # 有效可用余额: max(0, remote_balance - unsettled_local_cost)
    remote_balance_usd: float         # 远端权威结算余额
    unsettled_local_cost_usd: float   # 本轮询周期内尚未被远端覆盖的本地预估消耗
    is_available: bool
    fairy_total_spent_usd: float      # 累计总投入: settled_total_spent + unsettled_local_cost
    tier_code: str                    # "SUPERCHARGED" | "STANDARD" | "THRIFTY" | "HIBERNATING"
    tier_label: str                   # "充裕超频态" | "日常标准态" | "吃紧省流态" | "断粮休眠态"
    active_sister: str                # 当前激活模型/调度槽位标识
    updated_at_iso: str


class ComputeEconomyManager:
    """
    对接 https://api.0-0.pro/v1/user/balance 权威美元余额接口：
    1. 严格使用 global_state 主键 row_id = 1 与 UPSERT 语法；
    2. 远端扣费差额 (prev_remote - current_remote) 作为 _settled_total_spent_usd 唯一增量来源；
       本地 record_local_cost() 仅更新瞬态未结算缓存 _unsettled_local_cost_usd，远端同步成功后自动清零覆写。
    """

    def __init__(
        self,
        truth_db_path: Path,
        api_base_url: str = "https://api.0-0.pro",
        api_key: str = "",
        poll_interval_sec: float = 90.0,
        recharge_threshold_usd: float = 1.0,
        on_recharge_detected: Callable[[float, float], Awaitable[None]] | None = None,
    ) -> None:
        self.truth_db_path = truth_db_path
        self.api_base_url = api_base_url.strip().rstrip("/")
        self.api_key = (
            api_key.strip()
            or os.environ.get("MAIBOT_UNIAPI_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
            or os.environ.get("GEMINI_API_KEY", "").strip()
        )
        self.poll_interval_sec = poll_interval_sec
        self.recharge_threshold_usd = recharge_threshold_usd
        self.on_recharge_detected = on_recharge_detected

        self._remote_balance_usd: float = 5.0
        self._settled_total_spent_usd: float = 0.0
        self._unsettled_local_cost_usd: float = 0.0
        self._is_available: bool = True
        self._last_known_remote_balance: float | None = None
        self._poll_task: asyncio.Task[None] | None = None

        self.ensure_global_state_columns()
        self._load_persisted_state()

    def _connect_rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.truth_db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def ensure_global_state_columns(self) -> None:
        self.truth_db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect_rw() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS global_state (
                    row_id INTEGER PRIMARY KEY CHECK(row_id = 1),
                    current_mood TEXT NOT NULL DEFAULT '平静',
                    energy_level TEXT NOT NULL DEFAULT '正常',
                    doing_now TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(global_state)").fetchall()
            }
            additions = {
                "fairy_remote_balance_usd": "REAL NOT NULL DEFAULT 5.0",
                "fairy_unsettled_cost_usd": "REAL NOT NULL DEFAULT 0.0",
                "fairy_total_spent_usd": "REAL NOT NULL DEFAULT 0.0",
                "fairy_compute_tier": "TEXT NOT NULL DEFAULT 'STANDARD'",
                "active_sister": "TEXT NOT NULL DEFAULT 'flash_primary'",
                "balance_updated_at": "TEXT NOT NULL DEFAULT ''",
            }
            for col_name, col_def in additions.items():
                if col_name not in cols:
                    conn.execute(f"ALTER TABLE global_state ADD COLUMN {col_name} {col_def}")

    def _load_persisted_state(self) -> None:
        if not self.truth_db_path.exists():
            return
        try:
            with self._connect_rw() as conn:
                row = conn.execute(
                    """
                    SELECT fairy_remote_balance_usd, fairy_unsettled_cost_usd, fairy_total_spent_usd
                    FROM global_state WHERE row_id = 1
                    """
                ).fetchone()
                if row:
                    self._remote_balance_usd = float(row["fairy_remote_balance_usd"] or 5.0)
                    self._unsettled_local_cost_usd = float(row["fairy_unsettled_cost_usd"] or 0.0)
                    persisted_total = float(row["fairy_total_spent_usd"] or 0.0)
                    self._settled_total_spent_usd = max(
                        0.0, round(persisted_total - self._unsettled_local_cost_usd, 4)
                    )
                    self._last_known_remote_balance = self._remote_balance_usd
        except Exception:
            pass

    @staticmethod
    def evaluate_tier(balance_usd: float, is_available: bool = True) -> tuple[str, str, str]:
        if not is_available or balance_usd < 0.5:
            return ("HIBERNATING", "断粮休眠态", "flash_low_power")
        if balance_usd < 3.0:
            return ("THRIFTY", "吃紧省流态", "flash_primary")
        if balance_usd < 15.0:
            return ("STANDARD", "日常标准态", "flash_primary_argon_on_demand")
        return ("SUPERCHARGED", "充裕超频态", "dual_argon_flash")

    def record_local_cost(self, estimated_cost_usd: float) -> ComputeEconomySnapshot:
        cost = max(0.0, float(estimated_cost_usd))
        self._unsettled_local_cost_usd = round(self._unsettled_local_cost_usd + cost, 4)
        snap = self.get_snapshot()
        self._persist_snapshot(snap)
        return snap

    async def apply_remote_balance_reading(
        self, remote_remaining_usd: float, is_available: bool = True
    ) -> ComputeEconomySnapshot:
        rem = max(0.0, round(float(remote_remaining_usd), 4))
        prev = self._last_known_remote_balance

        if prev is not None:
            if rem < prev:
                real_delta = round(prev - rem, 4)
                self._settled_total_spent_usd = round(
                    self._settled_total_spent_usd + real_delta, 4
                )
            elif (rem - prev) >= self.recharge_threshold_usd and self.on_recharge_detected is not None:
                await self.on_recharge_detected(prev, rem)

        self._remote_balance_usd = rem
        self._unsettled_local_cost_usd = 0.0
        self._is_available = bool(is_available)
        self._last_known_remote_balance = rem

        snap = self.get_snapshot()
        self._persist_snapshot(snap)
        return snap

    def get_snapshot(self, override_sister: str = "") -> ComputeEconomySnapshot:
        effective_balance = max(
            0.0, round(self._remote_balance_usd - self._unsettled_local_cost_usd, 4)
        )
        effective_total_spent = round(
            self._settled_total_spent_usd + self._unsettled_local_cost_usd, 4
        )
        tier_code, tier_label, default_sister = self.evaluate_tier(
            effective_balance, self._is_available
        )
        tokyo_tz = datetime.timezone(datetime.timedelta(hours=9))
        now_iso = datetime.datetime.now(tokyo_tz).isoformat(timespec="seconds")
        return ComputeEconomySnapshot(
            balance_usd=effective_balance,
            remote_balance_usd=round(self._remote_balance_usd, 4),
            unsettled_local_cost_usd=round(self._unsettled_local_cost_usd, 4),
            is_available=self._is_available,
            fairy_total_spent_usd=effective_total_spent,
            tier_code=tier_code,
            tier_label=tier_label,
            active_sister=override_sister or default_sister,
            updated_at_iso=now_iso,
        )

    def _persist_snapshot(self, snap: ComputeEconomySnapshot) -> None:
        if not self.truth_db_path.exists():
            return
        try:
            with self._connect_rw() as conn:
                conn.execute("INSERT OR IGNORE INTO global_state (row_id) VALUES (1)")
                conn.execute(
                    """
                    INSERT INTO global_state (
                        row_id,
                        fairy_remote_balance_usd,
                        fairy_unsettled_cost_usd,
                        fairy_total_spent_usd,
                        fairy_compute_tier,
                        active_sister,
                        balance_updated_at
                    ) VALUES (1, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(row_id) DO UPDATE SET
                        fairy_remote_balance_usd = excluded.fairy_remote_balance_usd,
                        fairy_unsettled_cost_usd = excluded.fairy_unsettled_cost_usd,
                        fairy_total_spent_usd = excluded.fairy_total_spent_usd,
                        fairy_compute_tier = excluded.fairy_compute_tier,
                        active_sister = excluded.active_sister,
                        balance_updated_at = excluded.balance_updated_at
                    """,
                    (
                        snap.remote_balance_usd,
                        snap.unsettled_local_cost_usd,
                        snap.fairy_total_spent_usd,
                        snap.tier_code,
                        snap.active_sister,
                        snap.updated_at_iso,
                    ),
                )
        except Exception:
            pass

    async def sync_remote_balance_once(self) -> ComputeEconomySnapshot:
        if not self.api_key:
            snap = self.get_snapshot()
            self._persist_snapshot(snap)
            return snap

        url = f"{self.api_base_url}/v1/user/balance"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            timeout = aiohttp.ClientTimeout(total=8.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rem = float(data.get("remaining", self._remote_balance_usd))
                        avail = bool(data.get("is_available", True))
                        return await self.apply_remote_balance_reading(rem, avail)
        except Exception:
            pass

        snap = self.get_snapshot()
        self._persist_snapshot(snap)
        return snap

    def start_background_sync(self) -> None:
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._sync_loop())

    async def stop_background_sync(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def _sync_loop(self) -> None:
        while True:
            try:
                await self.sync_remote_balance_once()
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(self.poll_interval_sec)
