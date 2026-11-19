"""原生多模态档案库检索与原件查看工具（受 access_policy 严格会话隔离保护）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar
import asyncio
import json
import os
import sqlite3

from src.common.logger import get_logger
from src.companion_vault.access_policy import authorized_file_rows
from src.companion_vault.asset_encoder import is_files_api_enabled
from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.quote_recall import load_original_descriptors
from src.companion_vault.runtime_bridge import (
    DEFAULT_TRUTH_DB_PATH,
    _open_truth_db,
    _resolve_active_memory_scope,
    _resolve_trust_role,
)
from src.core.tooling import ToolExecutionContext, ToolExecutionResult, ToolInvocation, ToolSpec

from .context import BuiltinToolRuntimeContext

logger = get_logger("maisaka_builtin_vault_files")

DEFAULT_LEDGER_DB_PATH = "data/message_ledger.db"
DEFAULT_ASSET_SPOOL = "data/companion_assets"
DEFAULT_VAULT_MOUNT = "/mnt/gdrive/companion_vault"

_T = TypeVar("_T")


def get_search_vault_files_tool_spec() -> ToolSpec:
    """获取 search_vault_files 工具声明。"""

    return ToolSpec(
        name="search_vault_files",
        description=(
            "按当前会话权限检索已归档并校验就绪的历史微信原图、表情、语音、PDF/文档或视频，"
            "返回 file_code、msg_id、文件名、MIME 类型及已同步的私有 Google Drive 链接。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "可选关键词，按文件名、file_code、msg_id、MIME 类型或特征模糊过滤。",
                    "default": "",
                },
                "kind": {
                    "type": "string",
                    "description": "可选媒体分类过滤：image、emoji、audio、video、document，留空表示全部。",
                    "enum": ["", "image", "emoji", "audio", "voice", "video", "document", "file"],
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 10,
                    "description": "最多返回的条数（1~20）。",
                },
            },
            "required": [],
        },
        provider_name="maisaka_builtin",
        provider_type="builtin",
    )


def get_inspect_vault_file_tool_spec() -> ToolSpec:
    """获取 inspect_vault_file 工具声明。"""

    return ToolSpec(
        name="inspect_vault_file",
        description=(
            "按 file_code（如 FM8405）调取当前会话已授权的原文件（图片/语音/PDF/文档/短视频），"
            "把原件多模态内容直接挂载进下一轮回复视野，同时返回其私有 Google Drive 链接。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "file_code": {
                    "type": "string",
                    "description": "待查看的原件编号（例如 FM8405）。",
                },
            },
            "required": ["file_code"],
        },
        provider_name="maisaka_builtin",
        provider_type="builtin",
    )


def _resolve_db_paths() -> tuple[str, str, str, str]:
    truth_db = str(os.environ.get("MAIBOT_TRUTH_DB_PATH", "") or "").strip() or DEFAULT_TRUTH_DB_PATH
    ledger_db = str(os.environ.get("MAIBOT_LEDGER_DB_PATH", "") or "").strip() or DEFAULT_LEDGER_DB_PATH
    spool_dir = str(os.environ.get("MAIBOT_ASSET_SPOOL", "") or "").strip() or DEFAULT_ASSET_SPOOL
    vault_mount = str(os.environ.get("MAIBOT_VAULT_MOUNT", "") or "").strip() or DEFAULT_VAULT_MOUNT
    return truth_db, ledger_db, spool_dir, vault_mount


def _extract_runtime_identity(tool_ctx: BuiltinToolRuntimeContext) -> Dict[str, Any]:
    """从运行时会话流与最近历史提取可信身份参数。"""

    stream = tool_ctx.runtime.chat_stream
    anchor_msg_id = ""
    anchor_account = ""
    history = getattr(tool_ctx.runtime, "_chat_history", None) or []
    for entry in reversed(history):
        orig = getattr(entry, "original_message", None)
        msg_id = str(getattr(entry, "message_id", "") or getattr(orig, "message_id", "") or "").strip()
        platform = str(getattr(orig, "platform", "") or "").strip().lower()
        account = str(getattr(orig, "account_id", "") or "").strip()
        if platform == "wechat" and msg_id.startswith("wechat:"):
            if not account:
                pieces = msg_id.split(":")
                if len(pieces) == 4:
                    account = pieces[2]
            anchor_msg_id = msg_id
            anchor_account = account
            break

    return {
        "platform": str(getattr(stream, "platform", "") or ""),
        "is_group_chat": bool(getattr(stream, "is_group_session", False)),
        "user_id": str(getattr(stream, "user_id", "") or ""),
        "group_id": str(getattr(stream, "group_id", "") or ""),
        "account_id": anchor_account or str(getattr(stream, "account_id", "") or "").strip(),
        "anchor_message_id": anchor_msg_id,
    }


def _resolve_scope_sync(
    *,
    db: Any,
    ledger: sqlite3.Connection,
    platform: str,
    is_group_chat: bool,
    user_id: str,
    group_id: str,
    account_id: str,
    anchor_message_id: str,
    ledger_path: str,
) -> Dict[str, str]:
    """严格解析当前会话的可信 `(source, account, chat_id)`；缺失或歧义时一律 fail-closed。"""

    clean_platform = str(platform or "").strip().lower()
    chat_id = str(group_id if is_group_chat else user_id or "").strip()
    sender_role = _resolve_trust_role(db, "", user_id, clean_platform)

    if clean_platform == "wechat" and anchor_message_id:
        try:
            scope = _resolve_active_memory_scope(
                db,
                platform=clean_platform,
                sender_role=sender_role,
                is_group_chat=is_group_chat,
                anchor_message_id=anchor_message_id,
                account=account_id,
                chat_id=chat_id,
                ledger_path=ledger_path,
            )
            if scope:
                return scope
        except Exception:
            pass

    if clean_platform == "wechat" and chat_id:
        if account_id:
            rows = ledger.execute(
                "SELECT DISTINCT source, account FROM messages WHERE account=? AND chat_id=?",
                (account_id, chat_id),
            ).fetchall()
        else:
            rows = ledger.execute(
                "SELECT DISTINCT source, account FROM messages WHERE chat_id=?",
                (chat_id,),
            ).fetchall()
        if len(rows) == 1 and rows[0][0] and rows[0][1]:
            return {"source": str(rows[0][0]), "account": str(rows[0][1]), "chat_id": chat_id}
        return {}

    return _resolve_active_memory_scope(
        db,
        platform=clean_platform,
        sender_role=sender_role,
        is_group_chat=is_group_chat,
        anchor_message_id=anchor_message_id,
        account=account_id,
        chat_id=chat_id,
        ledger_path=ledger_path,
    )


def _with_scoped_vault(
    identity: Dict[str, Any],
    callback: Callable[[Any, sqlite3.Connection, Dict[str, str], str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    truth_db_path, ledger_db_path, spool_dir, vault_mount = _resolve_db_paths()
    if not Path(truth_db_path).is_file() or not Path(ledger_db_path).is_file():
        return {"ok": False, "error": "档案真值库或消息账本尚未就绪。"}

    db = _open_truth_db(truth_db_path)
    ledger = sqlite3.connect(Path(ledger_db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        scope = _resolve_scope_sync(db=db, ledger=ledger, ledger_path=ledger_db_path, **identity)
        if not scope:
            return {"ok": False, "error": "当前会话缺少可验证的档案访问作用域，已拒绝操作。"}
        return callback(db, ledger, scope, spool_dir, vault_mount)
    finally:
        ledger.close()
        db.close()


def _matches_kind(row: Dict[str, Any], kind: str) -> bool:
    clean_kind = str(kind or "").strip().lower()
    if not clean_kind:
        return True
    mime = str(row.get("mime_type") or "").strip().lower()
    features = str(row.get("distinctive_features") or "")
    name = str(row.get("semantic_name") or "").strip().lower()
    is_emoji = "微信表情包" in features
    if clean_kind == "emoji":
        return is_emoji
    if clean_kind == "image":
        return mime.startswith("image/") and not is_emoji
    if clean_kind in {"audio", "voice"}:
        return mime.startswith("audio/")
    if clean_kind == "video":
        return mime.startswith("video/")
    if clean_kind in {"document", "file"}:
        if mime in {"application/pdf", "application/json"} or mime.startswith(
            ("text/", "application/vnd.", "application/msword")
        ):
            return True
        return name.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx", ".txt", ".md", ".csv"))
    return True


def _matches_query(entry: Dict[str, Any], features: str, query: str) -> bool:
    needle = str(query or "").strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        str(v or "")
        for v in (
            entry.get("file_code"),
            entry.get("msg_id"),
            entry.get("semantic_name"),
            entry.get("mime_type"),
            entry.get("representation"),
            features,
        )
    ).lower()
    return needle in haystack


def _search_vault_files_sync(identity: Dict[str, Any], *, query: str, kind: str, limit: int) -> Dict[str, Any]:
    def run(db: Any, ledger: sqlite3.Connection, scope: Dict[str, str], _spool: str, _mount: str) -> Dict[str, Any]:
        rows = authorized_file_rows(
            db.conn,
            ledger,
            source=scope["source"],
            account=scope["account"],
            destination_chat_id=scope["chat_id"],
        )
        has_links = (
            db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='message_asset_links'").fetchone() is not None
        )
        items: List[Dict[str, Any]] = []
        for row in rows:
            if not _matches_kind(row, kind):
                continue
            entry: Dict[str, Any] = {
                "file_code": str(row["file_code"]),
                "semantic_name": str(row.get("semantic_name") or ""),
                "mime_type": str(row.get("mime_type") or ""),
                "created_at": str(row.get("created_at") or ""),
                "drive_status": str(row.get("drive_status") or "pending"),
            }
            if row.get("drive_url"):
                entry["drive_url"] = str(row["drive_url"])
            if has_links:
                link_row = db.conn.execute(
                    "SELECT msg_id, representation FROM message_asset_links "
                    "WHERE file_code=? AND account=? AND active=1 "
                    "ORDER BY CAST(msg_id AS INTEGER) DESC LIMIT 1",
                    (row["file_code"], scope["account"]),
                ).fetchone()
                if link_row is not None:
                    if link_row[0]:
                        entry["msg_id"] = str(link_row[0])
                    if link_row[1]:
                        entry["representation"] = str(link_row[1])
            if not _matches_query(entry, str(row.get("distinctive_features") or ""), query):
                continue
            items.append(entry)

        items.sort(
            key=lambda x: (
                int(x["msg_id"]) if str(x.get("msg_id") or "").isdigit() else 0,
                int(x["file_code"][2:]) if str(x.get("file_code") or "")[2:].isdigit() else 0,
            ),
            reverse=True,
        )
        return {"ok": True, "count": len(items), "files": items[:limit]}

    return _with_scoped_vault(identity, run)


def _inspect_vault_file_sync(identity: Dict[str, Any], *, file_code: str) -> Dict[str, Any]:
    def run(db: Any, ledger: sqlite3.Connection, scope: Dict[str, str], spool_dir: str, vault_mount: str) -> Dict[str, Any]:
        authorized = authorized_file_rows(
            db.conn,
            ledger,
            source=scope["source"],
            account=scope["account"],
            destination_chat_id=scope["chat_id"],
            codes=[file_code],
        )
        if not authorized:
            return {"ok": False, "error": "无权访问或未找到该文件。"}

        target = authorized[0]
        assets = AssetPipeline(db, spool_dir, mount=vault_mount)
        client = None
        if is_files_api_enabled() and os.environ.get("GEMINI_API_KEY"):
            try:
                from google.genai import Client
                from google.genai.types import HttpOptions

                endpoint = os.environ.get("MAIBOT_FILES_BASE_URL", "")
                client = Client(
                    api_key=os.environ["GEMINI_API_KEY"],
                    http_options=HttpOptions(base_url=endpoint) if endpoint else None,
                )
            except Exception:
                client = None

        summary: Dict[str, Any] = {
            "file_code": target["file_code"],
            "semantic_name": str(target.get("semantic_name") or ""),
            "mime_type": str(target.get("mime_type") or ""),
            "drive_status": str(target.get("drive_status") or "pending"),
        }
        if target.get("drive_url"):
            summary["drive_url"] = str(target["drive_url"])

        try:
            descriptors = load_original_descriptors(assets, [target], client=client)
        except ValueError as exc:
            summary["loaded"] = False
            summary["loaded_parts_count"] = 0
            summary["note"] = f"原文件已安全归档但超出直读限制或格式暂不支持内联预览：{exc}"
            return {"ok": True, "summary": summary, "multimodal_parts": []}
        except FileNotFoundError:
            return {"ok": False, "error": "文件记录存在，但本地 Spool 与云盘挂载点均未找到可读原件。"}
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()

        summary["loaded"] = True
        summary["loaded_parts_count"] = len(descriptors)
        summary["note"] = "原件已挂载到本轮多模态上下文，请直接查看附带画面、音频或文档内容作答。"
        return {"ok": True, "summary": summary, "multimodal_parts": descriptors}

    return _with_scoped_vault(identity, run)


async def handle_search_vault_files_tool(
    tool_ctx: BuiltinToolRuntimeContext,
    invocation: ToolInvocation,
    context: Optional[ToolExecutionContext] = None,
) -> ToolExecutionResult:
    """执行 search_vault_files 内置工具。"""

    del context
    query = str(invocation.arguments.get("query") or "").strip()
    kind = str(invocation.arguments.get("kind") or "").strip().lower()
    try:
        limit = max(1, min(20, int(invocation.arguments.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10

    identity = _extract_runtime_identity(tool_ctx)
    result = await asyncio.to_thread(_search_vault_files_sync, identity, query=query, kind=kind, limit=limit)
    if not result.get("ok"):
        return tool_ctx.build_failure_result(invocation.tool_name, str(result.get("error") or "档案检索失败。"))

    payload = {"count": result["count"], "files": result["files"]}
    return tool_ctx.build_success_result(
        invocation.tool_name,
        json.dumps(payload, ensure_ascii=False),
        structured_content=payload,
        metadata={"web_result_preview": True},
    )


async def handle_inspect_vault_file_tool(
    tool_ctx: BuiltinToolRuntimeContext,
    invocation: ToolInvocation,
    context: Optional[ToolExecutionContext] = None,
) -> ToolExecutionResult:
    """执行 inspect_vault_file 内置工具。"""

    del context
    file_code = str(invocation.arguments.get("file_code") or "").strip().upper()
    if not file_code:
        return tool_ctx.build_failure_result(invocation.tool_name, "请提供有效的 `file_code` 参数（如 FM8405）。")

    identity = _extract_runtime_identity(tool_ctx)
    result = await asyncio.to_thread(_inspect_vault_file_sync, identity, file_code=file_code)
    if not result.get("ok"):
        return tool_ctx.build_failure_result(invocation.tool_name, str(result.get("error") or "无权访问或未找到该文件。"))

    summary = result["summary"]
    multimodal_parts = list(result.get("multimodal_parts") or [])
    logger.info(
        f"{tool_ctx.runtime.log_prefix} 调取档案原件 file_code={file_code} loaded={summary.get('loaded')} parts={len(multimodal_parts)}"
    )
    return tool_ctx.build_success_result(
        invocation.tool_name,
        json.dumps(summary, ensure_ascii=False),
        structured_content=summary,
        metadata={
            "web_result_preview": True,
            "_multimodal_parts": multimodal_parts,
        },
    )
