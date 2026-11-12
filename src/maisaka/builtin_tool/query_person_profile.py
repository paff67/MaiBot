"""query_person_profile 内置工具。"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from typing import Any, Dict, Optional

from src.core.tooling import ToolExecutionContext, ToolExecutionResult, ToolInvocation, ToolSpec
from src.services.memory_service import memory_service

from .context import BuiltinToolRuntimeContext

DEFAULT_PROFILE_LIMIT = 8
DEFAULT_TRUTH_DB_PATH = "data/companion_truth.db"


def get_tool_spec(*, enabled: bool = True) -> ToolSpec:
    """获取 query_person_profile 工具声明。"""

    return ToolSpec(
        name="query_person_profile",
        description="查询人物画像。当需要查询与某人的关系时使用。",
        parameters_schema={
            "type": "object",
            "properties": {
                "person_id": {
                    "type": "string",
                    "description": "内部人物ID；明确给出时填。",
                },
                "person_name": {
                    "type": "string",
                    "description": "名称/昵称/关键词；通常填这个。",
                },
                "limit": {
                    "type": "integer",
                    "description": "证据上限。",
                    "default": DEFAULT_PROFILE_LIMIT,
                },
            },
        },
        provider_name="maisaka_builtin",
        provider_type="builtin",
        enabled=enabled,
    )


def _normalize_limit(raw_limit: Any) -> int:
    try:
        limit = int(raw_limit or DEFAULT_PROFILE_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_PROFILE_LIMIT
    return max(1, min(limit, 20))


def _extract_profile_text(payload: Dict[str, Any]) -> str:
    return str(payload.get("profile_text") or payload.get("summary") or "").strip()


def _extract_traits(profile_text: str) -> list[str]:
    traits: list[str] = []
    for line in profile_text.splitlines():
        clean_line = line.strip().strip("-").strip()
        if clean_line:
            traits.append(clean_line)
        if len(traits) >= 8:
            break
    return traits


def _build_structured_content(
    *,
    payload: Dict[str, Any],
    requested_person_id: str,
    requested_person_name: str,
    limit: int,
) -> Dict[str, Any]:
    profile_text = _extract_profile_text(payload)
    return {
        "success": bool(payload.get("success")),
        "summary": profile_text,
        "traits": _extract_traits(profile_text),
        "person_id": str(payload.get("person_id") or requested_person_id or "").strip(),
        "person_name": str(payload.get("person_name") or requested_person_name or "").strip(),
        "profile_source": str(payload.get("profile_source") or "").strip(),
        "has_manual_override": bool(payload.get("has_manual_override", False)),
        "from_cache": bool(payload.get("from_cache", False)),
        "limit": limit,
    }


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _is_requester_owner(conn: sqlite3.Connection, platform: str, user_id: str) -> bool:
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


def _query_companion_truth_person(
    person_id: str,
    person_name: str,
    db_path: str = DEFAULT_TRUTH_DB_PATH,
    *,
    is_group_chat: bool = True,
    requester_platform: str = "",
    requester_user_id: str = "",
) -> Optional[Dict[str, str]]:
    """从 companion_truth.db -> person_info 查询人物档案（防线一 SQL 投影级物理硬裁剪）。"""
    clean_id = _clean_text(person_id)
    clean_name = _clean_text(person_name)
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

        allow_owner_private = (not is_group_chat) and _is_requester_owner(conn, requester_platform, requester_user_id)
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
        matched_row: Optional[sqlite3.Row] = None

        if clean_id:
            matched_row = conn.execute(
                f"SELECT {projection} FROM person_info "
                f"WHERE person_id = ? OR json_extract(platform_ids, '$.wechat') = ? OR person_name = ? LIMIT 1",
                (clean_id, clean_id, clean_id),
            ).fetchone()
        if matched_row is None and clean_name:
            if not is_group_chat and "wechat_remark" in existing_cols:
                matched_row = conn.execute(
                    f"SELECT {projection} FROM person_info "
                    f"WHERE person_name = ? OR wechat_remark = ? OR person_id = ? LIMIT 1",
                    (clean_name, clean_name, clean_name),
                ).fetchone()
            else:
                matched_row = conn.execute(
                    f"SELECT {projection} FROM person_info WHERE person_name = ? OR person_id = ? LIMIT 1",
                    (clean_name, clean_name),
                ).fetchone()

        if matched_row is None:
            keyword = (clean_name or clean_id).strip()
            if keyword:
                like_pat = f"%{keyword}%"
                search_cols = ["person_id", "person_name"]
                if "one_line_intro" in existing_cols:
                    search_cols.append("one_line_intro")
                if "public_impression" in existing_cols:
                    search_cols.append("public_impression")
                if not is_group_chat and "wechat_remark" in existing_cols:
                    search_cols.append("wechat_remark")
                where_like = " OR ".join(f"COALESCE({c}, '') LIKE ?" for c in search_cols)
                matched_row = conn.execute(
                    f"SELECT {projection} FROM person_info WHERE {where_like} LIMIT 1",
                    tuple(like_pat for _ in search_cols),
                ).fetchone()
    except Exception:
        return None
    finally:
        conn.close()

    if matched_row is None:
        return None

    row_keys = set(matched_row.keys())
    row_pid = _clean_text(matched_row["person_id"]) if "person_id" in row_keys else ""
    row_pname = _clean_text(matched_row["person_name"]) if "person_name" in row_keys else ""
    row_remark = _clean_text(matched_row["wechat_remark"]) if "wechat_remark" in row_keys else ""
    trust_role = _clean_text(matched_row["trust_role"]) if "trust_role" in row_keys else ""
    pub_imp = _clean_text(matched_row["public_impression"]) if "public_impression" in row_keys else ""
    priv_notes = ""
    if allow_owner_private:
        priv_notes = _clean_text(matched_row["owner_private_notes"]) if "owner_private_notes" in row_keys else ""
        if not priv_notes and "impression" in row_keys:
            priv_notes = _clean_text(matched_row["impression"])

    eff_impression = "\n".join(x for x in (priv_notes, pub_imp) if x) if allow_owner_private else pub_imp
    relationship_text = (_clean_text(matched_row["relationship_text"]) if "relationship_text" in row_keys and not is_group_chat else "") or (
        _clean_text(matched_row["one_line_intro"]) if "one_line_intro" in row_keys else ""
    )

    lines = [f"person_id: {row_pid}", f"姓名/昵称: {row_pname}"]
    if not is_group_chat and row_remark:
        lines.append(f"备注: {row_remark}")
    lines.extend([f"身份角色: {trust_role}", f"印象: {eff_impression}", f"关系与相处准则: {relationship_text}"])
    profile_text = "\n".join(lines)
    return {
        "person_id": row_pid,
        "person_name": row_pname or row_remark,
        "wechat_remark": "" if is_group_chat else row_remark,
        "trust_role": trust_role,
        "impression": eff_impression,
        "relationship_text": relationship_text,
        "profile_text": profile_text,
    }


async def handle_tool(
    tool_ctx: BuiltinToolRuntimeContext,
    invocation: ToolInvocation,
    context: Optional[ToolExecutionContext] = None,
) -> ToolExecutionResult:
    """执行 query_person_profile 内置工具。"""

    del context
    person_id = str(invocation.arguments.get("person_id") or "").strip()
    person_name = str(invocation.arguments.get("person_name") or "").strip()
    if not person_id and not person_name:
        return tool_ctx.build_failure_result(
            invocation.tool_name,
            "query_person_profile 需要提供 person_id 或 person_name 中的一个。",
        )

    limit = _normalize_limit(invocation.arguments.get("limit"))
    db_path = str(invocation.arguments.get("db_path") or DEFAULT_TRUTH_DB_PATH).strip() or DEFAULT_TRUTH_DB_PATH
    chat_stream = getattr(getattr(tool_ctx, "runtime", None), "chat_stream", None)
    is_group_chat = bool(getattr(chat_stream, "is_group_session", True)) if chat_stream is not None else True
    req_platform = _clean_text(getattr(chat_stream, "platform", "")) if chat_stream is not None else ""
    req_user_id = _clean_text(getattr(chat_stream, "user_id", "")) if chat_stream is not None else ""

    try:
        truth_record = await asyncio.to_thread(
            _query_companion_truth_person,
            person_id,
            person_name,
            db_path,
            is_group_chat=is_group_chat,
            requester_platform=req_platform,
            requester_user_id=req_user_id,
        )
    except Exception:
        truth_record = None

    if truth_record is not None:
        profile_text = truth_record["profile_text"]
        structured_content = {
            "success": True,
            "summary": profile_text,
            "traits": _extract_traits(profile_text),
            "person_id": truth_record["person_id"],
            "person_name": truth_record["person_name"],
            "wechat_remark": truth_record["wechat_remark"],
            "trust_role": truth_record["trust_role"],
            "impression": truth_record["impression"],
            "relationship_text": truth_record["relationship_text"],
            "profile_source": "companion_truth",
            "has_manual_override": False,
            "from_cache": False,
            "limit": limit,
        }
        return tool_ctx.build_success_result(
            invocation.tool_name,
            profile_text,
            structured_content=structured_content,
        )

    # 微信具身模式或群聊会话禁止回退 A_memorix 读取未按群私隔离的跨聊事实
    if req_platform.lower() == "wechat" or is_group_chat:
        return tool_ctx.build_failure_result(
            invocation.tool_name,
            "未在当前会话可见范围内找到该人物的公开档案。",
        )
    try:
        if person_id:
            payload = await memory_service.profile_admin(
                action="query",
                person_id=person_id,
                limit=limit,
            )
        else:
            payload = await memory_service.profile_admin(
                action="query",
                person_keyword=person_name,
                limit=limit,
            )
    except Exception as exc:
        return tool_ctx.build_failure_result(
            invocation.tool_name,
            f"人物画像查询失败：{exc}",
        )

    if not isinstance(payload, dict):
        return tool_ctx.build_failure_result(
            invocation.tool_name,
            "人物画像查询失败：invalid_payload",
        )

    structured_content = _build_structured_content(
        payload=payload,
        requested_person_id=person_id,
        requested_person_name=person_name,
        limit=limit,
    )
    if not bool(payload.get("success")):
        error_message = str(payload.get("error") or "未找到人物画像。").strip()
        return tool_ctx.build_failure_result(
            invocation.tool_name,
            error_message,
            structured_content=structured_content,
        )

    profile_text = structured_content["summary"]
    if not profile_text:
        profile_text = "未找到可用的人物画像。"
    return tool_ctx.build_success_result(
        invocation.tool_name,
        profile_text,
        structured_content=structured_content,
    )
