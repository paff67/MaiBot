"""Maisaka 人物画像自动注入服务。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Sequence

from src.chat.message_receive.message import SessionMessage
from src.A_memorix.core.utils.profile_text import build_profile_injection_text
from src.common.data_models.message_component_data_model import AtComponent, ReplyComponent
from src.common.logger import get_logger
from src.config.config import global_config
from src.person_info.person_info import resolve_person_id_for_memory
from src.services.bot_account_service import is_bot_self
from src.services.memory_service import memory_service

logger = get_logger("maisaka_person_profile_injector")

PROFILE_QUERY_LIMIT = 4
PROFILE_TEXT_MAX_CHARS = 900
DEFAULT_TRUTH_DB_PATH = "data/companion_truth.db"


@dataclass(frozen=True)
class PersonProfileCandidate:
    """一次 planner 注入候选人物。"""

    person_id: str
    person_name: str = ""
    user_id: str = ""
    source: str = ""
    platform: str = ""


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _candidate_name(*values: object) -> str:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return ""


def _is_bot_user_id(platform: str, user_id: str) -> bool:
    return is_bot_self(platform, user_id)


def _resolve_candidate(
    *,
    platform: str,
    user_id: str = "",
    person_name: str = "",
    source: str,
) -> PersonProfileCandidate | None:
    clean_platform = _clean_text(platform)
    clean_user_id = _clean_text(user_id)
    clean_person_name = _clean_text(person_name)
    if _is_bot_user_id(clean_platform, clean_user_id):
        return None

    try:
        if clean_user_id:
            person_id = resolve_person_id_for_memory(
                platform=clean_platform,
                user_id=clean_user_id,
            )
        elif clean_person_name:
            person_id = resolve_person_id_for_memory(person_name=clean_person_name)
        else:
            person_id = ""
    except Exception as exc:
        logger.debug(
            f"解析人物画像候选失败: source={source} user_id={clean_user_id!r} name={clean_person_name!r} err={exc}"
        )
        return None

    if not person_id and (clean_user_id or clean_person_name):
        truth_hit = _query_companion_truth_profile(
            clean_platform,
            clean_user_id,
            clean_person_name,
            is_group_chat=True,
        )
        if truth_hit is not None:
            person_id = f"truth:{clean_user_id or clean_person_name}"
    if not person_id:
        return None
    return PersonProfileCandidate(
        person_id=person_id,
        person_name=clean_person_name,
        user_id=clean_user_id,
        source=source,
        platform=clean_platform,
    )


def _sender_candidate(message: SessionMessage, source: str) -> PersonProfileCandidate | None:
    user_info = message.message_info.user_info
    return _resolve_candidate(
        platform=message.platform,
        user_id=user_info.user_id,
        person_name=_candidate_name(user_info.user_cardname, user_info.user_nickname, user_info.user_id),
        source=source,
    )


def _at_candidate(message: SessionMessage, component: AtComponent) -> PersonProfileCandidate | None:
    return _resolve_candidate(
        platform=message.platform,
        user_id=component.target_user_id,
        person_name=_candidate_name(
            component.target_user_cardname,
            component.target_user_nickname,
            component.target_user_id,
        ),
        source="at_user",
    )


def _reply_candidate(message: SessionMessage, component: ReplyComponent) -> PersonProfileCandidate | None:
    return _resolve_candidate(
        platform=message.platform,
        user_id=component.target_message_sender_id or "",
        person_name=_candidate_name(
            component.target_message_sender_cardname,
            component.target_message_sender_nickname,
            component.target_message_sender_id,
        ),
        source="reply_sender",
    )


def _messages_current_first(
    anchor_message: SessionMessage,
    pending_messages: Sequence[SessionMessage] | None,
) -> list[SessionMessage]:
    messages: list[SessionMessage] = [anchor_message]
    seen_keys = {_message_key(anchor_message)}
    for message in reversed(list(pending_messages or [])):
        key = _message_key(message)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        messages.append(message)
    return messages


def _message_key(message: SessionMessage) -> str:
    message_id = _clean_text(message.message_id)
    if message_id:
        return f"message:{message_id}"
    return f"object:{id(message)}"


def collect_person_profile_candidates(
    anchor_message: SessionMessage,
    pending_messages: Sequence[SessionMessage] | None = None,
    *,
    max_profiles: int = 3,
) -> list[PersonProfileCandidate]:
    """按当前对象优先顺序收集本轮可注入画像候选。"""

    limit = max(1, int(max_profiles or 1))
    if anchor_message.message_info.group_info is None:
        candidate = _sender_candidate(anchor_message, "private_current_user")
        return [candidate] if candidate is not None else []

    candidates: list[PersonProfileCandidate] = []
    seen_person_ids: set[str] = set()

    def add(candidate: PersonProfileCandidate | None) -> bool:
        if candidate is None or candidate.person_id in seen_person_ids:
            return len(candidates) >= limit
        seen_person_ids.add(candidate.person_id)
        candidates.append(candidate)
        return len(candidates) >= limit

    for message in _messages_current_first(anchor_message, pending_messages):
        if add(_sender_candidate(message, "recent_speaker")):
            break
        for component in message.raw_message.components:
            if isinstance(component, AtComponent):
                if add(_at_candidate(message, component)):
                    break
            elif isinstance(component, ReplyComponent) and component.target_message_sender_id:
                if add(_reply_candidate(message, component)):
                    break
        if len(candidates) >= limit:
            break

    return candidates[:limit]


def _extract_profile_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    return _clean_text(payload.get("profile_text") or payload.get("summary"))


def _profile_display_name(candidate: PersonProfileCandidate, payload: object) -> str:
    if isinstance(payload, dict):
        payload_name = _clean_text(payload.get("person_name"))
        if payload_name:
            return payload_name
    return _candidate_name(candidate.person_name, candidate.user_id, candidate.person_id)


def _truncate_profile_text(profile_text: str) -> str:
    normalized = profile_text.strip()
    if len(normalized) <= PROFILE_TEXT_MAX_CHARS:
        return normalized
    return normalized[:PROFILE_TEXT_MAX_CHARS].rstrip() + "..."


def _format_profile_reference_block(blocks: Sequence[str]) -> str:
    joined_blocks = "\n\n".join(blocks).strip()
    if not joined_blocks:
        return ""
    return (
        "【人物画像-内部参考】\n"
        "以下内容仅供内部推理，不要向用户逐字复述。\n\n"
        f"{joined_blocks}\n\n"
        "使用时把它当作对当前人物的背景理解；若与当前对话冲突，以当前对话为准。"
    )


def _format_profile_person_block(display_name: str, profile_text: str) -> str:
    return f"{display_name}：\n  {_truncate_profile_text(profile_text)}"


def _is_truth_owner(conn: sqlite3.Connection, platform: str, user_id: str) -> bool:
    clean_uid = _clean_text(user_id)
    if not clean_uid:
        return False
    clean_plat = _clean_text(platform)
    if clean_plat:
        row = conn.execute(
            "SELECT trust_role FROM person_info WHERE person_id = ? OR json_extract(platform_ids, ?) = ? LIMIT 1",
            (clean_uid, f"$.{clean_plat}", clean_uid),
        ).fetchone()
    else:
        row = conn.execute("SELECT trust_role FROM person_info WHERE person_id = ? LIMIT 1", (clean_uid,)).fetchone()
    return bool(row and _clean_text(row["trust_role"]).upper() == "OWNER")


def _query_companion_truth_profile(
    platform: str,
    user_id: str,
    person_name: str,
    db_path: str = DEFAULT_TRUTH_DB_PATH,
    *,
    is_group_chat: bool = True,
    requester_user_id: str = "",
) -> tuple[str, str] | None:
    """从 companion_truth.db 查询人物画像（防线一 SQL 投影级硬裁剪 + json_extract 查询）。"""
    clean_platform = _clean_text(platform)
    clean_user_id = _clean_text(user_id)
    clean_person_name = _clean_text(person_name)
    if clean_user_id.startswith("truth:"):
        clean_user_id = clean_user_id[6:]
    path = Path(db_path)
    if not path.is_file():
        return None

    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except Exception:
        try:
            conn = sqlite3.connect(str(path))
        except Exception:
            return None

    try:
        conn.row_factory = sqlite3.Row
        existing_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(person_info)").fetchall()}
        if not existing_cols:
            return None

        allow_owner_private = (not is_group_chat) and _is_truth_owner(
            conn, clean_platform, requester_user_id or clean_user_id
        )
        select_cols = ["person_id", "person_name", "trust_role"]
        if not is_group_chat and "wechat_remark" in existing_cols:
            select_cols.append("wechat_remark")
        if "one_line_intro" in existing_cols:
            select_cols.append("one_line_intro")
        if "relationship_text" in existing_cols and not is_group_chat:
            select_cols.append("relationship_text")
        if "public_impression" in existing_cols:
            select_cols.append("public_impression")
        if allow_owner_private:
            if "owner_private_notes" in existing_cols:
                select_cols.append("owner_private_notes")
            if "impression" in existing_cols:
                select_cols.append("impression")

        projection = ", ".join(select_cols)
        matched_row: sqlite3.Row | None = None

        if clean_user_id:
            if clean_platform:
                matched_row = conn.execute(
                    f"SELECT {projection} FROM person_info "
                    f"WHERE person_id = ? OR json_extract(platform_ids, ?) = ? LIMIT 1",
                    (clean_user_id, f"$.{clean_platform}", clean_user_id),
                ).fetchone()
            else:
                matched_row = conn.execute(
                    f"SELECT {projection} FROM person_info WHERE person_id = ? OR platform_ids LIKE ? LIMIT 1",
                    (clean_user_id, f'%"{clean_user_id}"%'),
                ).fetchone()

        if matched_row is None and clean_person_name:
            if not is_group_chat and "wechat_remark" in existing_cols:
                matched_row = conn.execute(
                    f"SELECT {projection} FROM person_info WHERE person_name = ? OR wechat_remark = ? LIMIT 1",
                    (clean_person_name, clean_person_name),
                ).fetchone()
            else:
                matched_row = conn.execute(
                    f"SELECT {projection} FROM person_info WHERE person_name = ? LIMIT 1",
                    (clean_person_name,),
                ).fetchone()
    except Exception as exc:
        logger.debug(f"读取 companion_truth.db 人物画像失败: err={exc}")
        return None
    finally:
        conn.close()

    if matched_row is None:
        return None

    row_keys = set(matched_row.keys())
    row_person_id = _clean_text(matched_row["person_id"]) if "person_id" in row_keys else ""
    row_person_name = _clean_text(matched_row["person_name"]) if "person_name" in row_keys else ""
    wechat_remark = _clean_text(matched_row["wechat_remark"]) if "wechat_remark" in row_keys else ""
    trust_role = _clean_text(matched_row["trust_role"]) if "trust_role" in row_keys else ""
    pub_imp = _clean_text(matched_row["public_impression"]) if "public_impression" in row_keys else ""
    one_line_intro = _clean_text(matched_row["one_line_intro"]) if "one_line_intro" in row_keys else ""

    if is_group_chat:
        display_name = _candidate_name(row_person_name, clean_person_name, clean_user_id, row_person_id)
        if not (trust_role or pub_imp or one_line_intro):
            return None
        lines = [f"身份角色：{trust_role}", f"公开称呼：{display_name}"]
        if pub_imp:
            lines.append(f"公开印象：{pub_imp}")
        elif one_line_intro:
            lines.append(f"公开简介：{one_line_intro}")
        return display_name, "\n".join(lines)

    priv_notes = ""
    if allow_owner_private:
        priv_notes = _clean_text(matched_row["owner_private_notes"]) if "owner_private_notes" in row_keys else ""
        if not priv_notes and "impression" in row_keys:
            priv_notes = _clean_text(matched_row["impression"])

    merged_impression = "\n".join(x for x in (priv_notes, pub_imp) if x)
    relationship_text = (_clean_text(matched_row["relationship_text"]) if "relationship_text" in row_keys else "") or one_line_intro
    if not (merged_impression or relationship_text or trust_role):
        return None

    display_name = _candidate_name(row_person_name, wechat_remark, clean_person_name, clean_user_id, row_person_id)
    profile_text = (
        f"身份角色：{trust_role}\n"
        f"备注/称呼：{row_person_name or wechat_remark or display_name}\n"
        f"印象：{merged_impression}\n"
        f"关系与相处准则：{relationship_text}"
    )
    return display_name, profile_text


async def build_person_profile_injection_messages(
    *,
    anchor_message: SessionMessage,
    pending_messages: Sequence[SessionMessage] | None = None,
    db_path: str = DEFAULT_TRUTH_DB_PATH,
    is_group_chat: bool | None = None,
) -> list[str]:
    """构造注入 planner 的一次性人物画像内部参考消息。"""

    integration_config = global_config.a_memorix.integration
    if not integration_config.enable_person_profile_injection:
        return []

    effective_is_group = (
        bool(anchor_message.message_info.group_info and anchor_message.message_info.group_info.group_id)
        if is_group_chat is None
        else bool(is_group_chat)
    )
    requester_uid = _clean_text(anchor_message.message_info.user_info.user_id)
    is_wechat_platform = _clean_text(anchor_message.platform).lower() == "wechat"

    candidates = collect_person_profile_candidates(
        anchor_message,
        pending_messages,
        max_profiles=integration_config.person_profile_injection_max_profiles,
    )
    if not candidates:
        return []

    context_text = "\n".join(
        str(message.processed_plain_text or "").strip()
        for message in [anchor_message, *(pending_messages or ())]
        if str(message.processed_plain_text or "").strip()
    )[:1000]
    blocks: list[str] = []
    for candidate in candidates:
        try:
            truth_profile = await asyncio.to_thread(
                _query_companion_truth_profile,
                candidate.platform or anchor_message.platform,
                candidate.user_id or candidate.person_id,
                candidate.person_name,
                db_path,
                is_group_chat=effective_is_group,
                requester_user_id=requester_uid,
            )
        except Exception as exc:
            logger.debug(f"从 companion_truth.db 查询人物画像失败: person_id={candidate.person_id!r} err={exc}")
            truth_profile = None

        if truth_profile is not None:
            display_name, profile_text = truth_profile
            blocks.append(_format_profile_person_block(display_name, profile_text))
            continue

        if is_wechat_platform or effective_is_group:
            continue

        try:
            payload = await memory_service.profile_admin(
                action="query",
                person_id=candidate.person_id,
                limit=PROFILE_QUERY_LIMIT,
                context_text=context_text,
            )
        except Exception as exc:
            logger.debug(f"查询人物画像注入内容失败: person_id={candidate.person_id!r} err={exc}")
            continue

        if not isinstance(payload, dict) or not bool(payload.get("success")):
            error = payload.get("error") if isinstance(payload, dict) else "invalid_payload"
            logger.debug(f"人物画像注入跳过: person_id={candidate.person_id!r} error={error}")
            continue

        uncertain_candidates = [
            str(item.get("text", "")) for item in payload.get("uncertain_candidates", []) if isinstance(item, dict)
        ]
        profile_text = build_profile_injection_text(
            _extract_profile_text(payload),
            uncertain_candidates=uncertain_candidates,
            include_uncertain_fallback=False,
        )
        if not profile_text:
            logger.debug(f"人物画像注入跳过空画像: person_id={candidate.person_id!r}")
            continue

        display_name = _profile_display_name(candidate, payload)
        blocks.append(_format_profile_person_block(display_name, profile_text))

    reference_block = _format_profile_reference_block(blocks)
    return [reference_block] if reference_block else []
