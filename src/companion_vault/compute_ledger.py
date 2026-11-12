"""双轨制算力账本与双子离场/回归弧光状态机预留服务（ComputeLedger）。"""

from __future__ import annotations

import asyncio
import datetime
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import aiohttp

from .truth_db import CompanionTruthDB

TOKYO_TZ = ZoneInfo("Asia/Tokyo")

PERSONA_SLOT_YOUNGER = "sister_younger"
PERSONA_SLOT_ELDER = "sister_elder"
THRIFTY_BALANCE_THRESHOLD_USD = 3.0

# 每百万 Token 默认预估折算单价 (prompt_per_1m_usd, completion_per_1m_usd)
DEFAULT_MODEL_TOKEN_RATES: dict[str, tuple[float, float]] = {
    "argon": (2.50, 10.00),
    "pro": (1.25, 5.00),
    "flash": (0.15, 0.60),
}


@dataclass
class ComputeLedgerSnapshot:
    balance_usd: float
    remote_balance_usd: float
    unsettled_local_cost_usd: float
    is_available: bool
    fairy_total_spent_usd: float
    tier_code: str                    # "SUPERCHARGED" | "STANDARD" | "THRIFTY" | "HIBERNATING"
    tier_label: str
    active_persona_slot: str          # "sister_younger" | "sister_elder"
    elder_away_arc: str
    chat_execution_mode: str          # "dual_director" | "one_pass_st"
    updated_at_iso: str

    @property
    def active_sister(self) -> str:
        """向后兼容别名"""
        return self.active_persona_slot


def _tokyo_now_iso() -> str:
    return datetime.datetime.now(TOKYO_TZ).isoformat(timespec="seconds")


def _estimate_token_cost_usd(model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    lowered = (model_name or "").lower()
    in_rate, out_rate = (0.50, 2.00)
    for keyword, rates in DEFAULT_MODEL_TOKEN_RATES.items():
        if keyword in lowered:
            in_rate, out_rate = rates
            break
    cost = (max(0, int(prompt_tokens)) * in_rate + max(0, int(completion_tokens)) * out_rate) / 1_000_000.0
    return round(cost, 6)


class ComputeLedger:
    """
    宿主与插件共用的干净算力账本服务：
    1. 绝不自行执行 ALTER TABLE DDL，全部 Schema 由 CompanionTruthDB 统一保障；
    2. 叙事时间戳严格对齐 Asia/Tokyo；
    3. 预留 on_token_usage 本地瞬时折算钩子、< 3.0 USD 离场弧光状态钩子与充值回归回调。
    """

    def __init__(
        self,
        truth_db_path: Path | str,
        api_base_url: str = "https://api.0-0.pro",
        api_key: str = "",
        poll_interval_sec: float = 90.0,
        recharge_threshold_usd: float = 1.0,
        on_recharge_detected: Callable[[float, float], Awaitable[None]] | None = None,
        on_elder_away_enter: Callable[[float], Awaitable[str] | str] | None = None,
        on_elder_return: Callable[[float, str], Awaitable[None]] | None = None,
    ) -> None:
        self.truth_db_path = Path(truth_db_path)
        self.api_base_url = api_base_url.strip().rstrip("/")
        self.api_key = (
            api_key.strip()
            or os.environ.get("MAIBOT_UNIAPI_KEY", "").strip()
            or os.environ.get("FAIRY_SANDBOX_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
            or os.environ.get("GEMINI_API_KEY", "").strip()
        )
        self.poll_interval_sec = poll_interval_sec
        self.recharge_threshold_usd = recharge_threshold_usd
        self.on_recharge_detected = on_recharge_detected
        self.on_elder_away_enter = on_elder_away_enter
        self.on_elder_return = on_elder_return

        self._remote_balance_usd: float = 5.0
        self._settled_total_spent_usd: float = 0.0
        self._unsettled_local_cost_usd: float = 0.0
        self._is_available: bool = True
        self._active_persona_slot: str = PERSONA_SLOT_YOUNGER
        self._elder_away_arc: str = ""
        self._chat_execution_mode: str = "dual_director"
        self._last_known_remote_balance: float | None = None
        self._poll_task: asyncio.Task[None] | None = None

        self._ensure_truth_db_initialized()
        self._load_persisted_state()

    def _ensure_truth_db_initialized(self) -> None:
        self.truth_db_path.parent.mkdir(parents=True, exist_ok=True)
        db = CompanionTruthDB(str(self.truth_db_path))
        db.close()

    def _load_persisted_state(self) -> None:
        if not self.truth_db_path.exists():
            return
        db = CompanionTruthDB(str(self.truth_db_path))
        try:
            row = db.conn.execute("SELECT * FROM global_state WHERE row_id = 1").fetchone()
            if row is not None:
                keys = set(row.keys())
                if "fairy_remote_balance_usd" in keys and row["fairy_remote_balance_usd"] is not None:
                    self._remote_balance_usd = float(row["fairy_remote_balance_usd"])
                    self._last_known_remote_balance = self._remote_balance_usd
                if "fairy_unsettled_cost_usd" in keys and row["fairy_unsettled_cost_usd"] is not None:
                    self._unsettled_local_cost_usd = float(row["fairy_unsettled_cost_usd"])
                if "fairy_total_spent_usd" in keys and row["fairy_total_spent_usd"] is not None:
                    total = float(row["fairy_total_spent_usd"])
                    self._settled_total_spent_usd = max(0.0, total - self._unsettled_local_cost_usd)
                if "active_persona_slot" in keys and str(row["active_persona_slot"] or "").strip():
                    self._active_persona_slot = str(row["active_persona_slot"]).strip()
                if "elder_away_arc" in keys and row["elder_away_arc"] is not None:
                    self._elder_away_arc = str(row["elder_away_arc"])
                if "chat_execution_mode" in keys and str(row["chat_execution_mode"] or "").strip():
                    self._chat_execution_mode = str(row["chat_execution_mode"]).strip()
        finally:
            db.close()

    @staticmethod
    def _compute_tier(effective_balance: float, is_available: bool) -> tuple[str, str]:
        if not is_available or effective_balance <= 0.05:
            return "HIBERNATING", "断粮休眠态"
        if effective_balance < THRIFTY_BALANCE_THRESHOLD_USD:
            return "THRIFTY", "吃紧省流态"
        if effective_balance >= 20.0:
            return "SUPERCHARGED", "充裕超频态"
        return "STANDARD", "日常标准态"

    def get_snapshot(self, *, override_persona_slot: str = "") -> ComputeLedgerSnapshot:
        effective = max(0.0, round(self._remote_balance_usd - self._unsettled_local_cost_usd, 6))
        total_spent = round(self._settled_total_spent_usd + self._unsettled_local_cost_usd, 6)
        tier_code, tier_label = self._compute_tier(effective, self._is_available)
        if tier_code in ("THRIFTY", "HIBERNATING"):
            slot = PERSONA_SLOT_YOUNGER
        else:
            slot = (override_persona_slot or self._active_persona_slot or PERSONA_SLOT_YOUNGER).strip()
        return ComputeLedgerSnapshot(
            balance_usd=effective,
            remote_balance_usd=round(self._remote_balance_usd, 6),
            unsettled_local_cost_usd=round(self._unsettled_local_cost_usd, 6),
            is_available=self._is_available,
            fairy_total_spent_usd=total_spent,
            tier_code=tier_code,
            tier_label=tier_label,
            active_persona_slot=slot,
            elder_away_arc=self._elder_away_arc,
            chat_execution_mode=self._chat_execution_mode,
            updated_at_iso=_tokyo_now_iso(),
        )

    def _persist_snapshot(self, snap: ComputeLedgerSnapshot) -> None:
        if not self.truth_db_path.exists():
            return
        db = CompanionTruthDB(str(self.truth_db_path))
        try:
            with db.conn:
                db.conn.execute(
                    """
                    INSERT INTO global_state (
                        row_id, timezone_name, active_persona_slot, active_sister,
                        elder_away_arc, chat_execution_mode,
                        fairy_remote_balance_usd, fairy_unsettled_cost_usd,
                        fairy_total_spent_usd, fairy_compute_tier, balance_updated_at
                    ) VALUES (1, 'Asia/Tokyo', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(row_id) DO UPDATE SET
                        timezone_name = 'Asia/Tokyo',
                        active_persona_slot = excluded.active_persona_slot,
                        active_sister = excluded.active_sister,
                        elder_away_arc = excluded.elder_away_arc,
                        chat_execution_mode = excluded.chat_execution_mode,
                        fairy_remote_balance_usd = excluded.fairy_remote_balance_usd,
                        fairy_unsettled_cost_usd = excluded.fairy_unsettled_cost_usd,
                        fairy_total_spent_usd = excluded.fairy_total_spent_usd,
                        fairy_compute_tier = excluded.fairy_compute_tier,
                        balance_updated_at = excluded.balance_updated_at
                    """,
                    (
                        snap.active_persona_slot,
                        snap.active_persona_slot,
                        snap.elder_away_arc,
                        snap.chat_execution_mode,
                        snap.remote_balance_usd,
                        snap.unsettled_local_cost_usd,
                        snap.fairy_total_spent_usd,
                        snap.tier_code,
                        snap.updated_at_iso,
                    ),
                )
        finally:
            db.close()

    def record_local_cost(self, estimated_usd: float) -> ComputeLedgerSnapshot:
        if estimated_usd > 0:
            self._unsettled_local_cost_usd = round(self._unsettled_local_cost_usd + float(estimated_usd), 6)
        if max(0.0, self._remote_balance_usd - self._unsettled_local_cost_usd) < THRIFTY_BALANCE_THRESHOLD_USD:
            self._active_persona_slot = PERSONA_SLOT_YOUNGER
        snap = self.get_snapshot()
        self._persist_snapshot(snap)
        return snap

    def on_token_usage(self, model_name: str, prompt_tokens: int, completion_tokens: int) -> ComputeLedgerSnapshot:
        """预留的模型调用瞬时折算钩子：实时将 Token 消耗计入本地未结算缓存。"""
        cost = _estimate_token_cost_usd(model_name, prompt_tokens, completion_tokens)
        return self.record_local_cost(cost)

    async def apply_remote_balance_reading(self, remote_balance: float, is_available: bool = True) -> ComputeLedgerSnapshot:
        prev_remote = self._last_known_remote_balance
        prev_effective = (
            max(0.0, prev_remote - self._unsettled_local_cost_usd)
            if prev_remote is not None
            else self._remote_balance_usd
        )
        new_remote = max(0.0, float(remote_balance))
        recharged = False

        if prev_remote is not None:
            delta = prev_remote - new_remote
            if delta > 0:
                self._settled_total_spent_usd = round(self._settled_total_spent_usd + delta, 6)
            elif (-delta) >= self.recharge_threshold_usd:
                recharged = True

        self._remote_balance_usd = new_remote
        self._last_known_remote_balance = new_remote
        self._unsettled_local_cost_usd = 0.0
        self._is_available = bool(is_available)

        if new_remote < THRIFTY_BALANCE_THRESHOLD_USD:
            self._active_persona_slot = PERSONA_SLOT_YOUNGER
            if prev_effective >= THRIFTY_BALANCE_THRESHOLD_USD or not self._elder_away_arc:
                if self.on_elder_away_enter is not None:
                    res = self.on_elder_away_enter(new_remote)
                    self._elder_away_arc = str((await res) if asyncio.iscoroutine(res) else res).strip()
        elif prev_remote is not None and prev_effective < THRIFTY_BALANCE_THRESHOLD_USD <= new_remote:
            if self.on_elder_return is not None:
                await self.on_elder_return(new_remote, self._elder_away_arc)

        snap = self.get_snapshot()
        self._persist_snapshot(snap)

        if recharged and prev_remote is not None and self.on_recharge_detected is not None:
            await self.on_recharge_detected(prev_remote, new_remote)

        return snap

    async def sync_remote_balance_once(self) -> ComputeLedgerSnapshot:
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


