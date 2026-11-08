"""Small operator CLI: synchronize complete group lists or add/remove a member."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .truth_db import CompanionTruthDB
from .access_policy import replace_group_members, set_group_member


def _upsert_contacts_into_person_info(conn, contacts: Dict[str, Any]) -> None:
    if not isinstance(contacts, dict) or not contacts:
        return
    rows = conn.execute("SELECT person_id, person_name, wechat_remark, platform_ids FROM person_info").fetchall()
    by_wxid: Dict[str, tuple[str, str, str]] = {}
    for person_id, person_name, wechat_remark, platform_ids_raw in rows:
        pid = str(person_id or "")
        by_wxid[pid] = (pid, str(person_name or ""), str(wechat_remark or ""))
        try:
            pids = json.loads(platform_ids_raw or "{}")
            if isinstance(pids, dict) and pids.get("wechat"):
                by_wxid[str(pids["wechat"])] = (pid, str(person_name or ""), str(wechat_remark or ""))
        except (ValueError, TypeError):
            pass
    with conn:
        for wxid, info in contacts.items():
            if not wxid or str(wxid).endswith("@chatroom") or not isinstance(info, dict):
                continue
            nickname = str(info.get("nickname") or "").strip()
            remark = str(info.get("remark") or "").strip()
            if not nickname and not remark:
                continue
            existing = by_wxid.get(wxid)
            if existing is None:
                conn.execute(
                    "INSERT OR IGNORE INTO person_info(person_id, person_name, wechat_remark, trust_role, platform_ids) "
                    "VALUES (?, ?, ?, 'GROUP_MEMBER', ?)",
                    (wxid, remark or nickname, remark, json.dumps({"wechat": wxid}, ensure_ascii=False)),
                )
            else:
                pid, cur_name, cur_remark = existing
                new_remark = remark or cur_remark
                new_name = cur_name if (cur_name and cur_name != pid) else (remark or nickname or cur_name)
                if new_remark != cur_remark or new_name != cur_name:
                    conn.execute(
                        "UPDATE person_info SET person_name=?, wechat_remark=? WHERE person_id=?",
                        (new_name, new_remark, pid),
                    )


def apply_directory_snapshot(
    truth_path: str,
    payload: Dict[str, Any],
    *,
    source: str,
    account: str,
    chat_id: str,
) -> Optional[str]:
    if not truth_path or not Path(truth_path).parent.exists():
        return None
    db = CompanionTruthDB(truth_path)
    try:
        if chat_id.endswith("@chatroom") and payload.get("group_id") == chat_id and isinstance(payload.get("user_ids"), list):
            replace_group_members(
                db.conn,
                source=source.rstrip("/"),
                account=account,
                group_id=chat_id,
                user_ids=payload["user_ids"],
            )
        contacts = payload.get("contacts")
        if isinstance(contacts, dict):
            _upsert_contacts_into_person_info(db.conn, contacts)
            if chat_id.endswith("@chatroom") and isinstance(contacts.get(chat_id), dict):
                g_info = contacts[chat_id]
                g_name = str(g_info.get("remark") or g_info.get("nickname") or "").strip()
                if g_name:
                    return g_name
        return None
    finally:
        db.close()


async def sync_chat_directory_async(
    truth_path: Optional[str],
    async_client: Any,
    *,
    source: str,
    account: str,
    chat_id: str,
) -> Optional[str]:
    if not truth_path or async_client is None or not chat_id:
        return None
    target_group = chat_id if chat_id.endswith("@chatroom") else "__contacts__"
    try:
        response = await async_client.get(
            source.rstrip("/") + "/wechat/group_members",
            params={"account": account, "group_id": target_group},
        )
        if response.status_code != 200:
            return None
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("account") != account:
            return None
        return await asyncio.to_thread(
            apply_directory_snapshot,
            truth_path,
            payload,
            source=source,
            account=account,
            chat_id=chat_id,
        )
    except Exception:
        return None


def sync_group(db, client, *, source, account, group_id):
    response = client.get(
        source.rstrip("/") + "/wechat/group_members", params={"account": account, "group_id": group_id}
    )
    response.raise_for_status()
    payload = response.json()
    if (
        payload.get("ok") is not True
        or payload.get("complete") is not True
        or payload.get("account") != account
        or payload.get("group_id") != group_id
    ):
        raise ValueError("Invalid group membership snapshot")
    users = payload.get("user_ids")
    if not isinstance(users, list):
        raise ValueError("Invalid member list")
    if isinstance(payload.get("contacts"), dict):
        _upsert_contacts_into_person_info(db, payload["contacts"])
    return replace_group_members(db, source=source, account=account, group_id=group_id, user_ids=users)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["sync", "add", "remove", "list"])
    parser.add_argument("--db", default="data/companion_truth.db")
    parser.add_argument("--source", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--user")
    args = parser.parse_args()
    source = args.source.rstrip("/")
    db = CompanionTruthDB(args.db)
    key = dict(source=source, account=args.account, group_id=args.group)
    try:
        if args.operation == "sync":
            import httpx

            token = os.environ.get("MAIBOT_EDGE_TOKEN", "")
            with httpx.Client(timeout=30, headers={"Authorization": "Bearer " + token} if token else {}) as client:
                print(json.dumps({"group": args.group, "members": sync_group(db.conn, client, **key)}))
        elif args.operation in ("add", "remove"):
            if not args.user:
                parser.error("--user required")
            set_group_member(db.conn, **key, user_id=args.user, present=args.operation == "add")
            print(json.dumps({"updated": True}))
        else:
            print(
                json.dumps(
                    [
                        r[0]
                        for r in db.conn.execute(
                            "SELECT user_id FROM group_members WHERE source=? AND account=? AND group_id=? ORDER BY user_id",
                            (source, args.account, args.group),
                        )
                    ]
                )
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
