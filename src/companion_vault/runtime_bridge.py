"""Planner 目录与 Expressor 绿灯装载的非阻塞桥接。"""

from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple
import asyncio
import json
import os
import sqlite3

from .truth_db import CompanionTruthDB
from .asset_pipeline import AssetPipeline
from .quote_recall import load_original_descriptors, recall_quotes

DEFAULT_TRUTH_DB_PATH = "data/companion_truth.db"


def _open_truth_db(db_path: str) -> CompanionTruthDB:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return CompanionTruthDB(db_path)


def _resolve_trust_role(db: CompanionTruthDB, person_id: str, user_id: str, platform: str) -> str:
    if person_id:
        row = db.conn.execute("SELECT trust_role FROM person_info WHERE person_id = ?", (person_id,)).fetchone()
        if row is not None:
            return row["trust_role"]
        return "GROUP_MEMBER"
    # platform_ids 为平台到账号 ID 的映射；若 person_id 直接存为平台账号 ID 也做匹配。
    for row in db.conn.execute("SELECT person_id, platform_ids, trust_role FROM person_info"):
        try:
            pids = json.loads(row["platform_ids"] or "{}")
        except (ValueError, TypeError):
            pids = {}
        if pids.get(platform) == user_id or (user_id and row["person_id"] == user_id):
            return row["trust_role"]
    return "GROUP_MEMBER"


def _resolve_trust_role_sync(db_path: str, person_id: str, user_id: str, platform: str) -> str:
    db = _open_truth_db(db_path)
    try:
        return _resolve_trust_role(db, person_id, user_id, platform)
    finally:
        db.close()


def build_owner_persona_overlay(
    db: CompanionTruthDB,
    *,
    is_group_chat: bool,
    sender_trust_role: str,
    user_id: str,
    platform: str,
    overlay_text: str = "",
) -> str:
    """
    OwnerPersonaOverlay：仅在 OWNER 本人私聊（not is_group_chat and sender_trust_role == 'OWNER'）时挂载，
    与普通好友私聊及一切群聊保持物理隔离。
    """
    if is_group_chat or str(sender_trust_role or "").upper() != "OWNER":
        return ""

    clean_uid = str(user_id or "").strip()
    clean_plat = str(platform or "").strip()
    owner_notes = ""
    try:
        if clean_uid and clean_plat:
            row = db.conn.execute(
                "SELECT owner_private_notes, impression FROM person_info "
                "WHERE trust_role = 'OWNER' AND (person_id = ? OR json_extract(platform_ids, ?) = ?) LIMIT 1",
                (clean_uid, f"$.{clean_plat}", clean_uid),
            ).fetchone()
        else:
            row = db.conn.execute(
                "SELECT owner_private_notes, impression FROM person_info WHERE trust_role = 'OWNER' LIMIT 1"
            ).fetchone()
        if row is not None:
            owner_notes = str(row["owner_private_notes"] or row["impression"] or "").strip()
    except Exception:
        owner_notes = ""

    configured_overlay = (overlay_text or os.environ.get("MAIBOT_OWNER_PERSONA_OVERLAY", "")).strip()
    if not owner_notes and not configured_overlay:
        return ""

    parts: list[str] = ["<Owner专属关系与私密记忆层>"]
    if owner_notes:
        parts.append(f"- 专属关系印象：{owner_notes}")
    if configured_overlay:
        parts.append(configured_overlay)
    parts.append("</Owner专属关系与私密记忆层>")
    return "\n".join(parts)


def get_twin_routing_state(db_path: str = DEFAULT_TRUTH_DB_PATH) -> Dict[str, Any]:
    """读取 global_state 中的双子角色槽位、离场弧光状态与对话执行模式预留状态。"""
    default_state: Dict[str, Any] = {
        "timezone_name": "Asia/Tokyo",
        "active_persona_slot": "sister_younger",
        "elder_away_arc": "",
        "chat_execution_mode": "dual_director",
        "fairy_compute_tier": "STANDARD",
        "force_younger_planner": False,
    }
    if not Path(db_path).is_file():
        return default_state
    db = _open_truth_db(db_path)
    try:
        row = db.conn.execute(
            "SELECT timezone_name, active_persona_slot, elder_away_arc, chat_execution_mode, "
            "fairy_compute_tier, fairy_remote_balance_usd, fairy_unsettled_cost_usd "
            "FROM global_state WHERE row_id = 1"
        ).fetchone()
        if row is None:
            return default_state
        tier = str(row["fairy_compute_tier"] or "STANDARD")
        eff_bal = max(0.0, float(row["fairy_remote_balance_usd"] or 5.0) - float(row["fairy_unsettled_cost_usd"] or 0.0))
        force_younger = tier in ("THRIFTY", "HIBERNATING") or eff_bal < 3.0
        slot = "sister_younger" if force_younger else (str(row["active_persona_slot"] or "sister_younger").strip() or "sister_younger")
        mode = str(row["chat_execution_mode"] or "dual_director").strip()
        if mode not in ("dual_director", "one_pass_st"):
            mode = "dual_director"
        return {
            "timezone_name": str(row["timezone_name"] or "Asia/Tokyo"),
            "active_persona_slot": slot,
            "elder_away_arc": str(row["elder_away_arc"] or ""),
            "chat_execution_mode": mode,
            "fairy_compute_tier": tier,
            "force_younger_planner": force_younger,
        }
    finally:
        db.close()


async def build_planner_truth_catalog_async(
    *,
    session_id: str,
    is_group_chat: bool,
    user_id: str,
    platform: str,
    db_path: str = DEFAULT_TRUTH_DB_PATH,
    anchor_message_id: str = "",
    account: str = "",
    chat_id: str = "",
    ledger_path: str = "data/message_ledger.db",
    owner_overlay_text: str = "",
) -> str:
    def build() -> str:
        db = _open_truth_db(db_path)
        try:
            sender_role = _resolve_trust_role(db, "", user_id, platform)
            catalog = db.build_planner_catalog_injection(
                is_group_chat=is_group_chat,
                sender_trust_role=sender_role,
                include_unscoped_media_and_events=(platform != "wechat"),
                chat_id=chat_id,
            )
            overlay_block = build_owner_persona_overlay(
                db,
                is_group_chat=is_group_chat,
                sender_trust_role=sender_role,
                user_id=user_id,
                platform=platform,
                overlay_text=owner_overlay_text,
            )
            if overlay_block:
                catalog = f"{catalog}\n\n{overlay_block}"
            if platform == "wechat" and anchor_message_id:
                from .wechat_recall import memory_scope
                from .consolidator import read_scoped_memories

                scope = memory_scope(
                    message_id=anchor_message_id,
                    platform=platform,
                    account=account,
                    chat_id=chat_id,
                    ledger_path=ledger_path,
                )
                if scope:
                    rows = read_scoped_memories(db_path, ledger_path, **scope)
                    from .access_policy import authorized_file_rows

                    with sqlite3.connect(Path(ledger_path).resolve().as_uri() + "?mode=ro", uri=True) as ledger:
                        file_rows = authorized_file_rows(
                            db.conn, ledger, source=scope["source"], account=account, destination_chat_id=chat_id
                        )
                    ledger.close()
                    has_links = (
                        db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='message_asset_links'").fetchone()
                        is not None
                    )
                    enriched_files = []
                    for r in file_rows:
                        entry = {k: r[k] for k in ("file_code", "semantic_name", "mime_type")}
                        if has_links:
                            link_row = db.conn.execute(
                                "SELECT msg_id FROM message_asset_links WHERE file_code=? AND account=? AND active=1 "
                                "ORDER BY CAST(msg_id AS INTEGER) DESC LIMIT 1",
                                (r["file_code"], account),
                            ).fetchone()
                            if link_row is not None and link_row[0]:
                                entry["msg_id"] = str(link_row[0])
                        enriched_files.append(entry)
                    enriched_files.sort(
                        key=lambda x: (
                            int(x["msg_id"]) if str(x.get("msg_id") or "").isdigit() else 0,
                            int(x["file_code"][2:]) if str(x.get("file_code") or "")[2:].isdigit() else 0,
                        )
                    )
                    catalog += "\n" + json.dumps(
                        {
                            "authorized_files": enriched_files[-20:],
                            "scoped_memories": [
                                {k: r[k] for k in ("code_index", "summary", "trigger_when")} for r in rows
                            ],
                        },
                        ensure_ascii=False,
                    )
            return catalog
        finally:
            db.close()

    return await asyncio.to_thread(build)


async def load_reply_greenlights_async(
    *,
    is_group_chat: bool,
    user_id: str,
    platform: str,
    person_id: str = "",
    memory_greenlights: Sequence[Any],
    file_greenlights: Sequence[Any],
    person_greenlights: Sequence[str] = (),
    quick_state_patch: Sequence[Dict[str, Any]] = (),
    db_path: str = DEFAULT_TRUTH_DB_PATH,
    vault_root: str = "/mnt/gdrive/companion_vault",
    gemini_api_key: str = "",
    quote_message_ids: Sequence[str] = (),
    quote_account: str = "",
    quote_chat_id: str = "",
    quote_source: str = "",
    ledger_path: str = "data/message_ledger.db",
    anchor_message_id: str = "",
    account: str = "",
    chat_id: str = "",
    client_factory: Any = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    memory_codes = [item if isinstance(item, str) else item["code"] for item in memory_greenlights]
    file_codes = [item if isinstance(item, str) else item["code"] for item in file_greenlights]

    def load() -> Tuple[str, List[Dict[str, Any]]]:
        db = _open_truth_db(db_path)
        try:
            trust_role = _resolve_trust_role(db, person_id, user_id, platform)
            if not is_group_chat and trust_role == "OWNER" and quick_state_patch:
                with db.conn:
                    db.conn.executemany(
                        "INSERT INTO user_status (key, category, current_value, updated_at) "
                        "VALUES (?, ?, ?, datetime('now', 'localtime')) "
                        "ON CONFLICT(key) DO UPDATE SET category=excluded.category, "
                        "current_value=excluded.current_value, updated_at=excluded.updated_at",
                        [(patch["key"], patch.get("category", ""), patch["value"]) for patch in quick_state_patch],
                    )
            memory_block, file_rows = db.load_greenlight_payload(
                memory_codes=memory_codes,
                file_codes=file_codes,
                person_ids=person_greenlights,
                sender_trust_role=trust_role,
                is_group_chat=is_group_chat,
                include_unscoped_media_and_events=(platform != "wechat"),
            )
            if platform == "wechat" and anchor_message_id:
                from .wechat_recall import memory_scope
                from .consolidator import read_scoped_memories

                scope = memory_scope(
                    message_id=anchor_message_id,
                    platform=platform,
                    account=account,
                    chat_id=chat_id,
                    ledger_path=ledger_path,
                )
                if scope:
                    rows = read_scoped_memories(db_path, ledger_path, codes=memory_codes, **scope)
                    memory_block += "\n" + json.dumps({"scoped_memories": rows}, ensure_ascii=False)
                    from .access_policy import authorized_file_rows

                    with sqlite3.connect(Path(ledger_path).resolve().as_uri() + "?mode=ro", uri=True) as ledger:
                        wechat_files = authorized_file_rows(
                            db.conn,
                            ledger,
                            source=scope["source"],
                            account=account,
                            destination_chat_id=chat_id,
                            codes=file_codes,
                        )
                    ledger.close()
                    known_files = {row["file_code"] for row in file_rows}
                    file_rows.extend(row for row in wechat_files if row["file_code"] not in known_files)
            assets = AssetPipeline(
                db,
                os.environ.get("MAIBOT_ASSET_SPOOL", "data/companion_assets"),
                mount=vault_root,
            )
            if quote_message_ids:
                if not all((quote_account, quote_chat_id, quote_source)):
                    raise ValueError("Quote recall requires trusted account, chat and source binding")
                with sqlite3.connect(Path(ledger_path).resolve().as_uri() + "?mode=ro", uri=True) as ledger:
                    quote_block, quote_files = recall_quotes(
                        ledger,
                        assets,
                        source=quote_source,
                        account=quote_account,
                        chat_id=quote_chat_id,
                        quote_message_ids=quote_message_ids,
                    )
                ledger.close()
                memory_block += "\n" + quote_block
                known = {row["file_code"] for row in file_rows}
                file_rows.extend(row for row in quote_files if row["file_code"] not in known)
            parts: List[Dict[str, Any]] = []
            if file_rows:
                client = None
                api_key = gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
                try:
                    if client_factory is not None:
                        client = client_factory()
                    elif api_key:
                        from google.genai import Client
                        from google.genai.types import HttpOptions

                        endpoint = os.environ.get("MAIBOT_FILES_BASE_URL", "")
                        client = Client(
                            api_key=api_key,
                            http_options=HttpOptions(base_url=endpoint) if endpoint else None,
                        )
                    parts = load_original_descriptors(assets, file_rows, client=client)
                finally:
                    if client is not None and hasattr(client, "close"):
                        client.close()
            return memory_block, parts
        finally:
            db.close()

    return await asyncio.to_thread(load)
