from __future__ import annotations

import datetime
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


class UserScopedTruthWriter:
    """
    严格对齐 src/companion_vault/truth_db.py 的真实 8 表结构：
    1. person_info: (person_id, platform_ids, person_name, wechat_remark, trust_role, one_line_intro, impression, intimacy_score, shared_episodes)
       通过 json_extract(platform_ids, '$.wechat') 匹配 actor_wxid，将用户偏好与状态追加写入 impression，主脑投影器可直接读取。
    2. user_status: (key PRIMARY KEY, category, current_value, visibility, updated_at)
       仅当操作者为 OWNER (trust_role='OWNER') 时同步更新此表。
    3. quests_events: (quest_code PRIMARY KEY, title, current_progress, deadline_or_condition, status, visibility, updated_at)
       在 current_progress 头部写入 [owner:<actor_wxid>] 归属标签，严格校验非本人不可修改他人 quest_code。
    4. memory_provenance: (code_index, job_id, source, account, chat_id, msg_id, revision, visibility CHECK(visibility='PRIVATE'))
       仅在私聊 (visibility='PRIVATE') 下写入溯源记录。
    """

    def __init__(
        self,
        truth_db_path: Path,
        wechat_source: str = "",
        wechat_account: str = "",
    ) -> None:
        self.truth_db_path = truth_db_path
        self.wechat_source = wechat_source
        self.wechat_account = wechat_account

    def _connect_rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.truth_db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _now_iso() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    @staticmethod
    def _allocate_next_code(conn: sqlite3.Connection, table: str, pk_col: str, prefix: str) -> str:
        rows = conn.execute(
            f"SELECT {pk_col} FROM {table} WHERE {pk_col} LIKE ?",
            (f"{prefix}%",),
        ).fetchall()
        max_num = 0
        for r in rows:
            raw = str(r[0] or "")
            m = re.match(rf"^{re.escape(prefix)}(\d+)$", raw)
            if m:
                max_num = max(max_num, int(m.group(1)))
        return f"{prefix}{max_num + 1:04d}"

    @staticmethod
    def _find_person_row_by_wxid(conn: sqlite3.Connection, wxid: str) -> sqlite3.Row | None:
        try:
            row = conn.execute(
                "SELECT * FROM person_info WHERE json_extract(platform_ids, '$.wechat') = ? LIMIT 1",
                (wxid,),
            ).fetchone()
            if row:
                return row
        except sqlite3.OperationalError:
            pass
        return conn.execute(
            "SELECT * FROM person_info WHERE platform_ids LIKE ? LIMIT 1",
            (f'%"{wxid}"%',),
        ).fetchone()

    def apply_user_scoped_patch(
        self,
        *,
        actor_wxid: str,
        origin_chat_id: str,
        patch: dict[str, Any],
    ) -> list[str]:
        clean_wxid = actor_wxid.strip()
        if not clean_wxid or not isinstance(patch, dict) or not self.truth_db_path.exists():
            return []

        applied: list[str] = []
        now_str = self._now_iso()
        is_group = origin_chat_id.endswith("@chatroom")
        visibility = "PUBLIC" if is_group else "PRIVATE"

        with self._connect_rw() as conn:
            conn.execute("BEGIN IMMEDIATE")

            person_row = self._find_person_row_by_wxid(conn, clean_wxid)
            is_owner = bool(person_row and str(person_row["trust_role"]).upper() == "OWNER")
            display_name = (
                str(person_row["wechat_remark"] or person_row["person_name"] or clean_wxid)
                if person_row
                else clean_wxid
            )

            # 1. 处理个人状态与个人偏好 -> 写入该用户专属的 person_info.impression
            status_text = str(patch.get("current_status") or "").strip()[:200]
            avail_text = str(patch.get("availability") or "").strip()[:60]
            pref_patch = patch.get("preference_item")

            impression_notes: list[str] = []
            if status_text:
                impression_notes.append(f"近期动态：{status_text}")
            if isinstance(pref_patch, dict) and pref_patch:
                pref_pairs = [f"{str(k)[:30]}={str(v)[:60]}" for k, v in pref_patch.items()]
                impression_notes.append(f"偏好记录：{'; '.join(pref_pairs)}")

            if impression_notes:
                note_line = f"[{now_str[:10]}] " + " | ".join(impression_notes)
                if person_row:
                    old_imp = str(person_row["impression"] or "").strip()
                    new_imp = f"{old_imp}\n{note_line}".strip()[-2000:]
                    conn.execute(
                        "UPDATE person_info SET impression = ? WHERE person_id = ?",
                        (new_imp, str(person_row["person_id"])),
                    )
                else:
                    new_pid = self._allocate_next_code(conn, "person_info", "person_id", "P")
                    platform_json = json.dumps({"wechat": clean_wxid}, ensure_ascii=False)
                    conn.execute(
                        """
                        INSERT INTO person_info (
                            person_id, platform_ids, person_name, wechat_remark,
                            trust_role, one_line_intro, impression, intimacy_score, shared_episodes
                        ) VALUES (?, ?, ?, '', 'GROUP_MEMBER', '', ?, 0, '[]')
                        """,
                        (new_pid, platform_json, clean_wxid, note_line),
                    )
                    if visibility == "PRIVATE":
                        self._upsert_private_provenance(conn, new_pid, origin_chat_id)
                applied.append("已更新个人档案印象/偏好")

            # 若操作者为 OWNER 本人，同步写入真实表 user_status
            if is_owner and (status_text or avail_text):
                if status_text:
                    conn.execute(
                        """
                        INSERT INTO user_status (key, category, current_value, visibility, updated_at)
                        VALUES ('current_status', 'activity', ?, 'PRIVATE', ?)
                        ON CONFLICT(key) DO UPDATE SET
                            current_value = excluded.current_value,
                            updated_at = excluded.updated_at
                        """,
                        (status_text, now_str),
                    )
                if avail_text:
                    conn.execute(
                        """
                        INSERT INTO user_status (key, category, current_value, visibility, updated_at)
                        VALUES ('availability', 'state', ?, 'PRIVATE', ?)
                        ON CONFLICT(key) DO UPDATE SET
                            current_value = excluded.current_value,
                            updated_at = excluded.updated_at
                        """,
                        (avail_text, now_str),
                    )
                applied.append("已同步更新主人全局状态(user_status)")

            # 2. 处理个人待办约定 -> 写入真实 quests_events 表，通过 [owner:<wxid>] 锁死归属权
            quest_title = str(patch.get("quest_title") or "").strip()[:120]
            if quest_title:
                req_code = str(patch.get("quest_code") or "").strip().upper()
                deadline = str(patch.get("deadline_or_condition") or patch.get("trigger_time") or "").strip()[:120]
                progress_body = str(patch.get("current_progress") or patch.get("context_notes") or "网页端提交").strip()[:400]
                status_val = str(patch.get("status") or "ongoing").strip().lower()
                owner_tag = f"[owner:{clean_wxid}]"
                tagged_progress = f"{owner_tag} ({display_name}) {progress_body}"

                if req_code:
                    existing = conn.execute(
                        "SELECT current_progress FROM quests_events WHERE quest_code = ?",
                        (req_code,),
                    ).fetchone()
                else:
                    existing = None

                if existing is None:
                    q_code = self._allocate_next_code(conn, "quests_events", "quest_code", "QE")
                    conn.execute(
                        """
                        INSERT INTO quests_events (
                            quest_code, title, current_progress, deadline_or_condition,
                            status, visibility, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (q_code, quest_title, tagged_progress, deadline, status_val, visibility, now_str),
                    )
                    if visibility == "PRIVATE":
                        self._upsert_private_provenance(conn, q_code, origin_chat_id)
                    applied.append(f"新增个人待办({q_code}: {quest_title[:30]})")
                else:
                    # 严格校验该待办是否归属于当前 actor_wxid
                    cur_prog = str(existing["current_progress"] or "")
                    if owner_tag in cur_prog or is_owner:
                        conn.execute(
                            """
                            UPDATE quests_events
                            SET title = ?, current_progress = ?, deadline_or_condition = ?, status = ?, updated_at = ?
                            WHERE quest_code = ?
                            """,
                            (quest_title, tagged_progress, deadline, status_val, now_str, req_code),
                        )
                        applied.append(f"更新个人待办({req_code})")

            conn.commit()
        return applied

    def _upsert_private_provenance(self, conn: sqlite3.Connection, code_index: str, chat_id: str) -> None:
        """严格遵循 memory_provenance 的真实列结构与 CHECK(visibility='PRIVATE') 约束"""
        has_prov = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_provenance'"
        ).fetchone()
        if not has_prov:
            return
        conn.execute(
            """
            INSERT OR REPLACE INTO memory_provenance (
                code_index, job_id, source, account, chat_id, msg_id, revision, visibility
            ) VALUES (?, 'web_workspace', ?, ?, ?, 'web_notify', '1', 'PRIVATE')
            """,
            (code_index, self.wechat_source, self.wechat_account, chat_id),
        )

    def list_known_members(self) -> list[dict[str, str]]:
        """从真实 person_info 表提取 (wxid, 称谓) 列表供前端 PIN 登录框选择"""
        if not self.truth_db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{self.truth_db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT person_name, wechat_remark, platform_ids FROM person_info LIMIT 120"
                ).fetchall()
                members: list[dict[str, str]] = []
                for r in rows:
                    try:
                        pids = json.loads(str(r["platform_ids"] or "{}"))
                    except Exception:
                        pids = {}
                    wxid = str(pids.get("wechat") or "").strip()
                    if not wxid:
                        continue
                    label = str(r["wechat_remark"] or r["person_name"] or wxid).strip()
                    members.append({"wxid": wxid, "nickname": label})
                return members
            finally:
                conn.close()
        except Exception:
            return []
