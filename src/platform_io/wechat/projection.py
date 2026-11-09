"""Project a committed ledger row to native SessionMessage input without inference."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
import hashlib
import html
import json
import re
import sqlite3

from .message_quotes import latest_quote, parse_quote, safe_body

# 1. 匹配微信群聊消息行首的 "<wxid>:\n" 前缀
_GROUP_SENDER_PREFIX_RE = re.compile(r"^([a-zA-Z0-9_@-]{4,64}):\n")
# 2. 匹配微信原生 @提及（以 \u2005 Four-Per-Em Space 结尾，或空白/行尾边界）
_WECHAT_AT_TOKEN_RE = re.compile(r"@([^\u2005\n@]{1,40})(?:\u2005|(?=\s|$))")
# 3. 兼容 CDATA 包裹与多行空白的非贪婪 <atuserlist> 正则
_AT_USERLIST_RE = re.compile(
    r"<atuserlist>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</atuserlist>",
    re.IGNORECASE | re.DOTALL,
)

DEFAULT_SELF_WXID = "wxid_w5d0efdxczt322"
BOT_SELF_WXID = DEFAULT_SELF_WXID


def get_dynamic_bot_aliases() -> set[str]:
    """从 global_config.bot 动态读取当前人设名称与别名，彻底解除硬编码（保持模块轻量延迟加载）"""
    aliases: set[str] = set()
    try:
        from src.config.config import global_config
        bot_cfg = getattr(global_config, "bot", None)
        if bot_cfg:
            nick = str(getattr(bot_cfg, "nickname", "") or "").strip()
            if nick:
                aliases.add(nick)
            for a in getattr(bot_cfg, "alias_names", []) or []:
                clean_a = str(a or "").strip()
                if clean_a:
                    aliases.add(clean_a)
    except Exception:
        pass
    return aliases or {"许妍", "妍妍", "小妍"}


def strip_group_sender_prefix(raw_text: str, sender_id: str = "", is_group: bool = False) -> str:
    """剥离微信 EnMicroMsg.db 群聊消息体头部的 'wxid_xxx:\n' 前缀"""
    if not raw_text:
        return ""
    m = _GROUP_SENDER_PREFIX_RE.match(raw_text)
    if not m:
        return raw_text
    prefix_id = m.group(1)
    if is_group or prefix_id == sender_id or prefix_id.startswith("wxid_"):
        return raw_text[m.end():]
    return raw_text


def extract_xml_at_wxids(
    raw_content: str = "",
    row_json: str | dict[str, Any] | None = None,
) -> list[str]:
    """从 message_ledger.db 的 raw_content 及 row_json (msg_source/raw_content/content) 多源提取 <atuserlist>"""
    candidates: list[str] = []
    if raw_content:
        candidates.append(str(raw_content))

    row_dict: dict[str, Any] = {}
    if isinstance(row_json, dict):
        row_dict = row_json
    elif isinstance(row_json, str) and row_json.strip():
        try:
            parsed = json.loads(row_json)
            if isinstance(parsed, dict):
                row_dict = parsed
        except Exception:
            candidates.append(row_json)

    for key in ("msg_source", "raw_content", "content"):
        val = row_dict.get(key)
        if val:
            candidates.append(str(val))

    if not candidates:
        return []

    combined = html.unescape("\n".join(candidates))
    wxids: list[str] = []
    for match in _AT_USERLIST_RE.finditer(combined):
        inner = match.group(1) or ""
        for item in inner.split(","):
            clean_id = item.strip()
            if clean_id and clean_id not in wxids:
                wxids.append(clean_id)
    return wxids


def build_identity_lookup_maps(
    truth_conn: sqlite3.Connection | None,
    self_wxid: str = DEFAULT_SELF_WXID,
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """
    从 companion_truth.db 的 person_info 构建只读身份映射：
    1. wxid_to_profile: {wxid: {"person_id", "person_name", "wechat_remark", "trust_role"}}
    2. name_to_wxid: {转小写的昵称或备注: wxid}（含动态读取的机器人自身人设名与别名映射）
    """
    wxid_to_profile: dict[str, dict[str, str]] = {}
    name_to_wxid: dict[str, str] = {}

    for alias in get_dynamic_bot_aliases():
        name_to_wxid[alias.lower()] = self_wxid

    if truth_conn is None:
        return wxid_to_profile, name_to_wxid

    try:
        cursor = truth_conn.execute(
            "SELECT person_id, person_name, wechat_remark, platform_ids, trust_role FROM person_info"
        )
        col_names = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
    except sqlite3.Error:
        return wxid_to_profile, name_to_wxid

    for r in rows:
        if isinstance(r, dict):
            row_dict = r
        elif isinstance(r, sqlite3.Row):
            row_dict = dict(r)
        else:
            row_dict = dict(zip(col_names, r))
        try:
            pids = json.loads(str(row_dict.get("platform_ids") or "{}"))
        except Exception:
            pids = {}
        wxid = str(pids.get("wechat") or "").strip()
        person_id = str(row_dict.get("person_id") or "").strip()
        p_name = str(row_dict.get("person_name") or "").strip()
        remark = str(row_dict.get("wechat_remark") or "").strip()
        trust_role = str(row_dict.get("trust_role") or "GROUP_MEMBER")

        prof = {
            "person_id": person_id,
            "person_name": p_name,
            "wechat_remark": remark,
            "trust_role": trust_role,
        }

        if wxid:
            wxid_to_profile[wxid] = prof
        if person_id:
            wxid_to_profile[person_id] = prof

        if p_name:
            name_to_wxid[p_name.lower()] = wxid or person_id
        if remark:
            name_to_wxid[remark.lower()] = wxid or person_id

    return wxid_to_profile, name_to_wxid


def enrich_sender_identity(
    enriched: dict[str, Any],
    sender_wxid: str,
    wxid_to_profile: dict[str, dict[str, str]],
) -> None:
    """
    解耦微信公开昵称 (person_name) 与号主单向备注 (wechat_remark)：
    - sender_nickname: 优先使用 person_name（如 'ì _eun'），确保群友 @ì _eun 时名字在上下文中可对齐；
    - sender_cardname: 承载 wechat_remark（如 'R'），使消息头渲染为 <message user="ì _eun" group_card="R">。
    """
    profile = wxid_to_profile.get(sender_wxid, {})
    person_name = profile.get("person_name", "").strip()
    wechat_remark = profile.get("wechat_remark", "").strip()

    enriched["sender_nickname"] = person_name or wechat_remark or enriched.get("sender_nickname") or sender_wxid
    enriched["sender_cardname"] = wechat_remark or person_name or enriched.get("sender_cardname") or ""
    enriched["sender_person_name"] = person_name
    enriched["sender_wechat_remark"] = wechat_remark


def parse_wechat_text_to_raw_segments(
    raw_text: str,
    *,
    sender_id: str,
    is_group: bool,
    wxid_to_profile: dict[str, dict[str, str]],
    name_to_wxid: dict[str, str],
    xml_at_wxids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    产出与 PluginMessageUtils._message_sequence_from_dict 兼容的中间字典列表：
    - 文本段: {"type": "text", "data": "..."}
    - 提及段: {"type": "at", "data": {"user_id": "...", "user_nickname": "..."}}
    """
    cleaned = strip_group_sender_prefix(raw_text, sender_id=sender_id, is_group=is_group)
    if not cleaned:
        return [{"type": "text", "data": ""}]

    raw_message_segments: list[dict[str, Any]] = []
    xml_wxid_queue = list(xml_at_wxids or [])
    last_idx = 0

    for match in _WECHAT_AT_TOKEN_RE.finditer(cleaned):
        start, end = match.span()
        at_display_name = match.group(1).strip()

        resolved_wxid = name_to_wxid.get(at_display_name.lower(), "")
        if not resolved_wxid and xml_wxid_queue:
            resolved_wxid = xml_wxid_queue.pop(0)
        elif resolved_wxid and xml_wxid_queue and xml_wxid_queue[0] == resolved_wxid:
            xml_wxid_queue.pop(0)

        has_u2005 = cleaned[end - 1 : end] == "\u2005"
        if not has_u2005 and not resolved_wxid:
            continue

        if start > last_idx:
            leading_text = cleaned[last_idx:start]
            if leading_text:
                raw_message_segments.append({"type": "text", "data": leading_text})

        target_wxid = resolved_wxid or f"unmapped_name:{at_display_name}"
        prof = wxid_to_profile.get(target_wxid, {})
        remark = prof.get("wechat_remark", "")
        p_name = prof.get("person_name", "") or at_display_name
        display_name = f"{p_name}(备注:{remark})" if (remark and remark != p_name) else p_name

        raw_message_segments.append(
            {
                "type": "at",
                "data": {
                    "user_id": target_wxid,
                    "user_nickname": display_name,
                },
            }
        )
        last_idx = end

    if last_idx < len(cleaned):
        tail_text = cleaned[last_idx:]
        if tail_text:
            raw_message_segments.append({"type": "text", "data": tail_text})

    return raw_message_segments or [{"type": "text", "data": cleaned}]


def native_message_id(source: str, account: str, msg_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", account) or not re.fullmatch(r"[0-9]+", msg_id):
        raise ValueError("Invalid WeChat identity")
    return "wechat:" + hashlib.sha256(source.rstrip("/").encode()).hexdigest()[:24] + ":" + account + ":" + msg_id


def enrich_record_from_truth_db(
    record: Dict[str, Any], truth_path: Optional[str], ledger_path: Optional[str] = None
) -> Dict[str, Any]:
    enriched = dict(record)
    sender = enriched.get("sender_id")
    if truth_path:
        db_file = Path(truth_path).resolve()
        if db_file.is_file():
            try:
                with sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True) as db:
                    db.row_factory = sqlite3.Row
                    wxid_to_profile, name_to_wxid = build_identity_lookup_maps(db)
                    enriched["_wxid_to_profile"] = wxid_to_profile
                    enriched["_name_to_wxid"] = name_to_wxid
                    if sender:
                        enrich_sender_identity(enriched, sender, wxid_to_profile)
                db.close()
            except sqlite3.Error:
                pass
    try:
        kind = int(enriched.get("kind") or json.loads(enriched.get("row_json") or "{}").get("type") or 0) & 65535
    except (ValueError, TypeError):
        kind = 0
    if (
        kind == 49
        and enriched.get("source")
        and enriched.get("account")
        and enriched.get("chat_id")
        and enriched.get("msg_id")
    ):
        candidates = []
        if ledger_path:
            candidates.append(Path(ledger_path))
        if enriched.get("ledger_path"):
            candidates.append(Path(str(enriched["ledger_path"])))
        if truth_path:
            t_dir = Path(truth_path).resolve().parent
            candidates.extend([t_dir / "message_ledger.db", t_dir / "ledger.db"])
        for cand in candidates:
            if cand.is_file():
                try:
                    with sqlite3.connect(cand.resolve().as_uri() + "?mode=ro", uri=True) as ldb:
                        item = latest_quote(
                            ldb,
                            str(enriched["source"]).rstrip("/"),
                            str(enriched["account"]),
                            str(enriched["chat_id"]),
                            str(enriched["msg_id"]),
                        )
                    ldb.close()
                    if item is not None and item.get("resolution") == "resolved" and item.get("target"):
                        target = item["target"]
                        enriched["resolved_quote_msg_id"] = str(target["msg_id"])
                        enriched["resolved_quote_display_name"] = (
                            item.get("display_name") or target.get("sender_id") or ""
                        )
                        target_content = target.get("content") or {}
                        enriched["resolved_quote_text"] = (
                            target_content.get("text") or target_content.get("title") or ""
                        )
                    break
                except Exception:
                    pass
    return enriched


def project_message(record: Dict[str, Any]) -> Dict[str, Any]:
    row = json.loads(record["row_json"])
    if row["msgId"] != record["msg_id"] or row["talker"] != record["chat_id"]:
        raise ValueError("Ledger ownership mismatch")
    if record["direction"] != "incoming":
        raise ValueError("Outgoing messages must not trigger a new incoming turn")
    kind = int(row["type"]) & 65535
    if kind == 10000:
        raise ValueError("System/revoke revisions belong to reconciliation, not new turns")
    sender = record["sender_id"]
    if not sender:
        raise ValueError("Unknown sender; never substitute a group ID or nickname")

    quote = parse_quote(row) if kind == 49 else None
    body = (
        quote["reply_text"]
        if quote
        else record["body"]
        if kind == 1
        else "[图片]"
        if kind == 3
        else "[表情包]"
        if kind == 47
        else json.dumps(safe_body(row["type"], record["body"]), ensure_ascii=False)
    )

    wxid_to_profile = record.get("_wxid_to_profile")
    name_to_wxid = record.get("_name_to_wxid")
    if wxid_to_profile is None or name_to_wxid is None:
        truth_p = record.get("truth_path")
        if truth_p and Path(truth_p).is_file():
            try:
                with sqlite3.connect(Path(truth_p).resolve().as_uri() + "?mode=ro", uri=True) as db:
                    db.row_factory = sqlite3.Row
                    w_prof, n_map = build_identity_lookup_maps(db)
                    if wxid_to_profile is None:
                        wxid_to_profile = w_prof
                    if name_to_wxid is None:
                        name_to_wxid = n_map
                db.close()
            except Exception:
                pass
    if wxid_to_profile is None:
        wxid_to_profile = {}
    if name_to_wxid is None:
        _, name_to_wxid = build_identity_lookup_maps(None)

    xml_at_wxids = record.get("xml_at_wxids")
    if xml_at_wxids is None:
        raw_c = record.get("raw_content", "")
        if not raw_c and isinstance(record.get("raw_message"), str):
            raw_c = record.get("raw_message", "")
        xml_at_wxids = extract_xml_at_wxids(raw_c, record.get("row_json"))

    is_group = str(record.get("chat_id") or "").endswith("@chatroom")

    raw_message = []
    if quote is not None:
        if record.get("resolved_quote_msg_id"):
            raw_message.append(
                {
                    "type": "reply",
                    "data": native_message_id(
                        record["source"], record["account"], str(record["resolved_quote_msg_id"])
                    ),
                }
            )
        elif quote.get("display_name") or record.get("resolved_quote_text"):
            ref_author = quote.get("display_name") or quote.get("target_sender_id") or "群友"
            body = f"[引用 @{ref_author}] {body}"

    if kind in (1, 49) or "@" in body or xml_at_wxids:
        segments = parse_wechat_text_to_raw_segments(
            body,
            sender_id=sender,
            is_group=is_group,
            wxid_to_profile=wxid_to_profile,
            name_to_wxid=name_to_wxid,
            xml_at_wxids=xml_at_wxids,
        )
        raw_message.extend(segments)
    else:
        raw_message.append({"type": "text", "data": body})

    user_nickname = record.get("sender_nickname") or sender
    user_cardname = record.get("sender_cardname")
    group_name = record.get("group_name") or record["chat_id"]
    return {
        "message_id": native_message_id(record["source"], record["account"], record["msg_id"]),
        "platform": "wechat",
        "account_id": record["account"],
        "scope": hashlib.sha256(record["source"].rstrip("/").encode()).hexdigest()[:24],
        "timestamp": int(row["createTime"]) / 1000,
        "message_info": {
            "user_info": {"user_id": sender, "user_nickname": user_nickname, "user_cardname": user_cardname},
            "group_info": {"group_id": record["chat_id"], "group_name": group_name}
            if record["chat_id"].endswith("@chatroom")
            else None,
            "additional_config": {
                "wechat_msg_id": record["msg_id"],
                "wechat_revision": record["revision"],
                "wechat_media_state": "resolve_from_vault" if kind != 1 else "text",
            },
        },
        "raw_message": raw_message,
    }
