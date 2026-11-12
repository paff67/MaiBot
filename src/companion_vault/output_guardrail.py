"""防线四：群聊发包前出口硬机密敏感锚点熔断（Output Guardrail）。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_TRUTH_DB_PATH = "data/companion_truth.db"


def _parse_env_anchors() -> list[str]:
    raw = os.environ.get("MAIBOT_GROUP_SECRET_ANCHORS", "")
    if not raw.strip():
        return []
    return [item.strip() for item in raw.split(",") if len(item.strip()) >= 2]


def _load_dynamic_balance_anchors(db_path: str = DEFAULT_TRUTH_DB_PATH) -> list[str]:
    """提取真值库中精确美元余额特征串（如 '$8.37 USD'），防止模型误将私聊账单发往群聊。"""
    path = Path(db_path)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(global_state)").fetchall()}
            if "fairy_remote_balance_usd" not in cols:
                return []
            row = conn.execute(
                "SELECT fairy_remote_balance_usd, fairy_unsettled_cost_usd, fairy_total_spent_usd "
                "FROM global_state WHERE row_id = 1"
            ).fetchone()
            if row is None:
                return []
            remote_bal = float(row["fairy_remote_balance_usd"] or 0.0)
            unsettled = float(row["fairy_unsettled_cost_usd"] or 0.0)
            eff_bal = max(0.0, remote_bal - unsettled)
            total_spent = float(row["fairy_total_spent_usd"] or 0.0)
            anchors = [
                f"${eff_bal:.2f} USD",
                f"${remote_bal:.2f} USD",
            ]
            if total_spent > 0:
                anchors.append(f"${total_spent:.2f} USD")
            return anchors
        finally:
            conn.close()
    except Exception:
        return []


def check_group_outbound_text(
    text: str,
    *,
    is_group_chat: bool,
    extra_secret_anchors: Sequence[str] | None = None,
    db_path: str = DEFAULT_TRUTH_DB_PATH,
) -> tuple[bool, str]:
    """
    校验群聊待发文本是否意外包含硬机密特征锚点。
    返回 (allowed, matched_anchor)：
    - 私聊消息始终放行 (True, "")；
    - 群聊消息一旦命中显式敏感词表或精确算力美元账单锚点，立即返回 (False, anchor) 触发熔断拦截。
    """
    if not is_group_chat or not text or not text.strip():
        return True, ""

    candidates: list[str] = []
    candidates.extend(_parse_env_anchors())
    if extra_secret_anchors:
        candidates.extend(a.strip() for a in extra_secret_anchors if a and len(a.strip()) >= 2)
    candidates.extend(_load_dynamic_balance_anchors(db_path=db_path))

    for anchor in candidates:
        if anchor and anchor in text:
            return False, anchor

    return True, ""


def collect_configured_secret_anchors(extra: Iterable[str] = ()) -> list[str]:
    seen: dict[str, None] = {}
    for item in [*_parse_env_anchors(), *extra]:
        clean = str(item or "").strip()
        if len(clean) >= 2:
            seen[clean] = None
    return list(seen.keys())
