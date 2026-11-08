"""query_person_profile 内置工具。"""

from __future__ import annotations

import asyncio
import json
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


def _query_companion_truth_person(
    person_id: str,
    person_name: str,
    db_path: str = DEFAULT_TRUTH_DB_PATH,
) -> Optional[Dict[str, str]]:
    """从 companion_truth.db -> person_info 查询人物档案。"""

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
        rows = conn.execute("SELECT * FROM person_info").fetchall()
    except Exception:
        return None
    finally:
        conn.close()

    matched_row: Optional[sqlite3.Row] = None
    # 1. 精确匹配 person_id / platform_ids / person_name / wechat_remark
    for row in rows:
        row_keys = set(row.keys())
        row_pid = _clean_text(row["person_id"]) if "person_id" in row_keys else ""
        row_pname = _clean_text(row["person_name"]) if "person_name" in row_keys else ""
        row_remark = _clean_text(row["wechat_remark"]) if "wechat_remark" in row_keys else ""
        try:
            pids = json.loads(row["platform_ids"] or "{}") if "platform_ids" in row_keys else {}
            if not isinstance(pids, dict):
                pids = {}
        except (ValueError, TypeError):
            pids = {}
        pid_values = {_clean_text(v) for v in pids.values() if _clean_text(v)}
        if clean_id and (clean_id == row_pid or clean_id in pid_values or clean_id in {row_pname, row_remark}):
            matched_row = row
            break
        if clean_name and clean_name in {row_pname, row_remark, row_pid}:
            matched_row = row
            break

    # 2. 模糊匹配 (LIKE / 子串)
    if matched_row is None:
        keyword = (clean_name or clean_id).lower()
        if keyword:
            for row in rows:
                row_keys = set(row.keys())
                fields = [
                    _clean_text(row[col])
                    for col in ("person_id", "person_name", "wechat_remark", "one_line_intro", "relationship_text", "impression")
                    if col in row_keys and _clean_text(row[col])
                ]
                if any(keyword in field.lower() or field.lower() in keyword for field in fields):
                    matched_row = row
                    break

    if matched_row is None:
        return None

    row_keys = set(matched_row.keys())
    row_pid = _clean_text(matched_row["person_id"]) if "person_id" in row_keys else ""
    row_pname = _clean_text(matched_row["person_name"]) if "person_name" in row_keys else ""
    row_remark = _clean_text(matched_row["wechat_remark"]) if "wechat_remark" in row_keys else ""
    trust_role = _clean_text(matched_row["trust_role"]) if "trust_role" in row_keys else ""
    impression = _clean_text(matched_row["impression"]) if "impression" in row_keys else ""
    relationship_text = ""
    if "relationship_text" in row_keys:
        relationship_text = _clean_text(matched_row["relationship_text"])
    if not relationship_text and "one_line_intro" in row_keys:
        relationship_text = _clean_text(matched_row["one_line_intro"])

    profile_text = (
        f"person_id: {row_pid}\n"
        f"姓名/昵称: {row_pname}\n"
        f"备注: {row_remark}\n"
        f"身份角色: {trust_role}\n"
        f"印象: {impression}\n"
        f"关系与相处准则: {relationship_text}"
    )
    return {
        "person_id": row_pid,
        "person_name": row_pname or row_remark,
        "wechat_remark": row_remark,
        "trust_role": trust_role,
        "impression": impression,
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
    try:
        truth_record = await asyncio.to_thread(_query_companion_truth_person, person_id, person_name, db_path)
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
