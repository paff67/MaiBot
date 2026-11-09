"""结构化真值库；同步接口应由异步调用方放入工作线程。"""

from typing import Any, Dict, List, Sequence, Tuple
import json
import re
import sqlite3

from .sql_normalizer import normalize_sql_structure
from .access_policy import GROUP_MEMBERS_SCHEMA


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_provenance(
 code_index TEXT NOT NULL,job_id TEXT NOT NULL,source TEXT NOT NULL,account TEXT NOT NULL,chat_id TEXT NOT NULL,
 msg_id TEXT NOT NULL,revision TEXT NOT NULL,visibility TEXT NOT NULL CHECK(visibility='PRIVATE'),
 PRIMARY KEY(code_index,source,account,msg_id));
CREATE TABLE IF NOT EXISTS global_state (
    row_id INTEGER PRIMARY KEY CHECK(row_id = 1),
    current_location TEXT, current_minor_region TEXT, current_major_region TEXT,
    prev_scene_time TEXT, elapsed_time TEXT, cur_time TEXT,
    body_state TEXT DEFAULT 'IDLE', current_app TEXT DEFAULT '',
    live_observation TEXT DEFAULT '', latest_screen_file_code TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS user_status (
    key TEXT PRIMARY KEY, category TEXT, current_value TEXT,
    visibility TEXT DEFAULT 'PRIVATE', updated_at TEXT
);
CREATE TABLE IF NOT EXISTS quests_events (
    quest_code TEXT PRIMARY KEY, title TEXT, current_progress TEXT,
    deadline_or_condition TEXT, status TEXT DEFAULT 'ongoing',
    visibility TEXT DEFAULT 'PRIVATE', updated_at TEXT
);
CREATE TABLE IF NOT EXISTS chronicle_events (
    code_index TEXT PRIMARY KEY, time_span TEXT, location TEXT, summary TEXT,
    chronicle_text TEXT, key_dialogue TEXT, trigger_when TEXT,
    tk_estimate INTEGER DEFAULT 120, related_files TEXT DEFAULT '[]',
    visibility TEXT DEFAULT 'PRIVATE', status TEXT DEFAULT 'active',
    valid_from TEXT, valid_until TEXT
);
CREATE TABLE IF NOT EXISTS media_vault (
    file_code TEXT PRIMARY KEY, semantic_name TEXT, mime_type TEXT, source_type TEXT,
    gdrive_path TEXT, gemini_file_uri TEXT, uri_expires_at REAL DEFAULT 0,
    chat_context TEXT, distinctive_features TEXT, extracted_entities TEXT,
    trigger_when TEXT, tk_estimate INTEGER DEFAULT 258,
    visibility TEXT DEFAULT 'PRIVATE', created_at TEXT
);
CREATE TABLE IF NOT EXISTS phone_surfing_log (
    surf_code TEXT PRIMARY KEY, app_name TEXT, post_summary TEXT, my_thought TEXT,
    screenshot_file_code TEXT, trigger_when TEXT, is_shared INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS app_sop_skills (
    sop_id TEXT PRIMARY KEY, app_name TEXT, feature_name TEXT, package_name TEXT,
    description TEXT, trigger_when TEXT, step_by_step_prompt TEXT
);
CREATE TABLE IF NOT EXISTS person_info (
    person_id TEXT PRIMARY KEY, platform_ids TEXT DEFAULT '{}', person_name TEXT,
    wechat_remark TEXT, trust_role TEXT DEFAULT 'GROUP_MEMBER', one_line_intro TEXT,
    impression TEXT, intimacy_score REAL DEFAULT 0, shared_episodes TEXT DEFAULT '[]'
);
"""


def _render_catalog(title: str, rows: Sequence[Dict[str, Any]], columns: Sequence[str]) -> str:
    # 只投影白名单索引列，并通过 JSON 转义字段中的换行。
    lines = [" | ".join(columns)]
    lines.extend(" | ".join(json.dumps(row[column], ensure_ascii=False) for column in columns) for row in rows)
    return f"<{title}>\n" + "\n".join(lines) + f"\n</{title}>"


class CompanionTruthDB:
    """每个实例由创建它的线程使用；调用方负责关闭连接。"""

    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.executescript(GROUP_MEMBERS_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def next_code(self, prefix: str, table: str, col: str) -> str:
        """读取下一个编号；并发写入方需在同一 IMMEDIATE 事务中分配并插入。"""
        targets = {
            "AM": ("chronicle_events", "code_index"),
            "FM": ("media_vault", "file_code"),
            "SF": ("phone_surfing_log", "surf_code"),
        }
        if targets.get(prefix) != (table, col):
            raise ValueError("编号前缀与目标表主键不匹配")
        row = self.conn.execute(
            f"SELECT COALESCE(MAX(CAST(SUBSTR({col}, 3) AS INTEGER)), 0) FROM {table} "
            f"WHERE SUBSTR({col}, 1, 2) = ? AND SUBSTR({col}, 3) != '' "
            f"AND SUBSTR({col}, 3) NOT GLOB '*[^0-9]*'", (prefix,),
        ).fetchone()
        return f"{prefix}{row[0] + 1:04d}"

    def execute_table_sql(self, raw_sql: str, *, manage_tx: bool = True) -> int:
        """只允许八张真值表上的 INSERT/UPDATE/DELETE，并以单事务原子提交。"""
        sql = normalize_sql_structure(raw_sql)
        if manage_tx and self.conn.in_transaction:
            raise RuntimeError("table_sql 必须在独立事务中执行")
        statements = self._split_write_statements(sql)
        if not statements:
            raise ValueError("table_sql 不能为空")
        changes_before = self.conn.total_changes
        allowed_tables = {"global_state", "user_status", "quests_events", "chronicle_events",
                          "media_vault", "phone_surfing_log", "app_sop_skills", "person_info"}
        current_target_table: List[str] = [""]

        def authorize(action: int, arg1: str, arg2: str, database: str, source: str) -> int:
            if source is not None or database not in {None, "main"}:
                return sqlite3.SQLITE_DENY
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_OK if arg1 in allowed_tables else sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ:
                return (
                    sqlite3.SQLITE_OK
                    if arg1 in allowed_tables and (not current_target_table[0] or arg1 == current_target_table[0])
                    else sqlite3.SQLITE_DENY
                )
            if action == sqlite3.SQLITE_FUNCTION:
                return sqlite3.SQLITE_DENY if (arg2 or "").lower() in {"load_extension", "readfile", "writefile"} else sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_DENY

        if manage_tx:
            self.conn.execute("BEGIN IMMEDIATE")
        protected_identities = {row[0]: (row[1], row[2]) for row in self.conn.execute(
            "SELECT person_id,trust_role,platform_ids FROM person_info"
        )}
        protected_owner_names = {
            row[0]: row[1]
            for row in self.conn.execute(
                "SELECT person_id,person_name FROM person_info WHERE trust_role='OWNER' AND COALESCE(TRIM(person_name),'')!=''"
            )
        }
        protected_wechat_remarks = {
            row[0]: row[1]
            for row in self.conn.execute(
                "SELECT person_id,wechat_remark FROM person_info WHERE COALESCE(TRIM(wechat_remark),'')!=''"
            )
        }
        self.conn.set_progress_handler(lambda: 1, 100_000)
        self.conn.set_authorizer(authorize)
        try:
            for statement in statements:
                cleaned_stmt = re.sub(r"\A(?:\s|--[^\n]*(?:\n|\Z)|/\*[\s\S]*?\*/)*", "", statement)
                target_match = re.match(
                    r"(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+([A-Za-z_][A-Za-z0-9_]*)",
                    cleaned_stmt,
                    flags=re.IGNORECASE,
                )
                current_target_table[0] = (target_match.group(1).lower() if target_match else "")
                self.conn.execute(statement)
            self.conn.set_authorizer(None)
            self.conn.set_progress_handler(None, 0)
            identities = {row[0]: (row[1], row[2]) for row in self.conn.execute(
                "SELECT person_id,trust_role,platform_ids FROM person_info"
            )}
            if any(identities.get(key) != value for key, value in protected_identities.items()) or any(
                value != ("GROUP_MEMBER", "{}") for key, value in identities.items() if key not in protected_identities
            ):
                raise ValueError("table_sql 不允许创建、修改或删除可信身份绑定")
            owner_names = {
                row[0]: row[1]
                for row in self.conn.execute("SELECT person_id,person_name FROM person_info WHERE trust_role='OWNER'")
            }
            if any(owner_names.get(key) != value for key, value in protected_owner_names.items()):
                raise ValueError("table_sql 不允许覆盖 OWNER 的 person_name")
            wechat_remarks = {
                row[0]: row[1]
                for row in self.conn.execute("SELECT person_id,wechat_remark FROM person_info")
            }
            if any(wechat_remarks.get(key) != value for key, value in protected_wechat_remarks.items()):
                raise ValueError("table_sql 不允许覆盖已有的微信备注 wechat_remark")
            if manage_tx:
                self.conn.commit()
        except BaseException:
            self.conn.set_authorizer(None)
            self.conn.set_progress_handler(None, 0)
            if manage_tx:
                self.conn.rollback()
            raise
        return self.conn.total_changes - changes_before

    @staticmethod
    def _split_write_statements(sql: str) -> List[str]:
        """由 SQLite 自己识别语句边界，避免自制引号解析与 SQLite 不一致。"""
        statements: List[str] = []
        start = 0
        for index, char in enumerate(sql):
            if char == ";" and sqlite3.complete_statement(sql[start:index + 1]):
                statements.append(sql[start:index + 1].strip())
                start = index + 1
        trailing = sql[start:].strip()
        if trailing:
            prefix = re.sub(r"\A(?:\s|--[^\n]*(?:\n|\Z)|/\*[\s\S]*?\*/)*", "", trailing)
            if prefix:
                if not sqlite3.complete_statement(trailing + ";"):
                    raise ValueError("table_sql 引号或注释未闭合")
                statements.append(trailing)
        for statement in statements:
            prefix = re.sub(r"\A(?:\s|--[^\n]*(?:\n|\Z)|/\*[\s\S]*?\*/)*", "", statement)
            match = re.match(r"([A-Za-z]+)", prefix)
            if match is None or match.group(1).upper() not in {"INSERT", "UPDATE", "DELETE"}:
                raise ValueError("table_sql 仅接受 INSERT、UPDATE、DELETE")
        return statements

    def _catalog_rows(
        self, table: str, columns: str, condition: str, order: str,
    ) -> List[Dict[str, Any]]:
        # 标识符来自固定模板；权限过滤后全量投影 index_only 索引列，不做相似度预筛。
        return [dict(row) for row in self.conn.execute(
            f"SELECT {columns} FROM {table} WHERE {condition} ORDER BY {order}"
        )]

    def _render_self_identity_anchor(self, self_wxid: str = "wxid_w5d0efdxczt322") -> str:
        """[动态人设与身份排他准则] 明确告知模型自身底层微信 ID、核心人设名与别名，并严格排他"""
        try:
            from src.config.config import global_config
            bot_name = str(getattr(global_config.bot, "nickname", "") or "许妍").strip()
            aliases = [str(a).strip() for a in (getattr(global_config.bot, "alias_names", []) or []) if str(a).strip()]
        except Exception:
            bot_name = "许妍"
            aliases = ["妍妍", "小妍"]

        alias_str = "、".join(aliases) if aliases else ""
        alias_prompt = f"（别名：{alias_str}）" if alias_str else ""
        at_tokens = [f"'@{bot_name}'"] + [f"'@{a}'" for a in aliases]
        at_rules = " 或 ".join(at_tokens)

        return (
            "<自身账号与提及判定准则>\n"
            f"- 你的唯一核心名字/当前人设：{bot_name}{alias_prompt} | 你的底层微信ID：{self_wxid}\n"
            f"- 提及(@)严格判定规则：仅当消息包含指向 '{self_wxid}' 的 @提及 或明确文本 {at_rules} 时，才是在 @ 你本人。\n"
            "- 当消息中出现 '@其他名字'（例如 '@ì _eun' 等）时，那是群成员在 @ 群内的其他群友（可通过下方<重要角色索引>核对其微信昵称与备注名的对应关系），绝不是你的底层代号，切勿将他人的微信昵称误认为自己！\n"
            "- 如果群内有群友昵称与你相似或重名，以底层微信ID及下方<重要角色索引>为准，严禁将他人发言视作对你的指令。\n"
            "</自身账号与提及判定准则>"
        )

    def _build_group_desensitized_roster(
        self,
        chat_id: str = "",
        active_sender_ids: set[str] | None = None,
        limit: int = 150,
    ) -> list[str]:
        """
        [v1.1 修正 P1 - 严格遵守 RBAC 红线]
        群聊场景下显式声明仅查询 5 个安全公开字段：person_id, person_name, wechat_remark, platform_ids, trust_role。
        优先对齐群内真实成员与活跃发言人，严禁查询或输出 impression（角色印象）、birthday（生日）及 tags（私密标签）！
        """
        active_set = set(active_sender_ids or [])
        group_member_ids: set[str] = set()
        if chat_id:
            try:
                cur = self.conn.execute("SELECT user_id FROM group_members WHERE group_id = ?", (chat_id,))
                group_member_ids = {str(r[0]).strip() for r in cur.fetchall() if r[0]}
            except Exception:
                pass

        all_candidate_rows = self.conn.execute(
            """
            SELECT person_id, person_name, wechat_remark, platform_ids, trust_role
            FROM person_info
            """
        ).fetchall()

        def sort_priority(r: Any) -> tuple[int, str]:
            pid = str(r["person_id"] or "").strip()
            role = str(r["trust_role"] or "").strip()
            if role == "OWNER":
                return (0, pid)
            if pid in active_set:
                return (1, pid)
            if pid in group_member_ids:
                return (2, pid)
            return (3, pid)

        sorted_rows = sorted(all_candidate_rows, key=sort_priority)[:int(limit)]

        lines: list[str] = []
        for r in sorted_rows:
            person_id = str(r["person_id"] or "").strip()
            person_name = str(r["person_name"] or "").strip() or "未知"
            wechat_remark = str(r["wechat_remark"] or "").strip()
            trust_role = str(r["trust_role"] or "GROUP_MEMBER").strip()
            lines.append(
                f"[角色ID: {person_id}] 备注名: {wechat_remark or '无'} | 微信昵称: {person_name} | 身份: {trust_role}"
            )
        return lines

    def build_planner_catalog_injection(
        self,
        *,
        is_group_chat: bool,
        sender_trust_role: str,
        include_unscoped_media_and_events: bool = True,
        chat_id: str = "",
        active_sender_ids: set[str] | None = None,
    ) -> str:
        """
        [v1.1 修正 P1] 保留全向向下兼容签名（chat_id 与 active_sender_ids 均带默认值），
        确保现网 test_quote_context.py、test_memory_consolidation.py 等测试模块零改动通过。
        """
        blocks: list[str] = [self._render_self_identity_anchor()]

        owner = not is_group_chat and sender_trust_role == "OWNER"
        visibility = "1=1" if owner else "visibility = 'PUBLIC'"
        # 全局屏幕和位置没有 visibility 字段，不能作为公共状态披露。
        state = {
            "global_state": [dict(row) for row in self.conn.execute("SELECT * FROM global_state")] if owner else [],
            "user_status": [dict(row) for row in self.conn.execute(f"SELECT * FROM user_status WHERE {visibility} ORDER BY key")],
            "quests_events": [dict(row) for row in self.conn.execute(
                f"SELECT * FROM quests_events WHERE status = 'ongoing' AND {visibility} ORDER BY quest_code"
            )],
        }
        blocks.append("<全局与实时状态>\n" + json.dumps(state, ensure_ascii=False) + "\n</全局与实时状态>")

        if owner:
            people = self._catalog_rows(
                "person_info", "person_id, person_name, wechat_remark, trust_role, one_line_intro",
                "1=1", "person_id",
            )
            blocks.append(_render_catalog("重要角色索引", people, (
                "person_id", "person_name", "wechat_remark", "trust_role", "one_line_intro",
            )))
        else:
            roster_lines = self._build_group_desensitized_roster(
                chat_id=chat_id,
                active_sender_ids=active_sender_ids,
            )
            roster_body = "\n".join(roster_lines) if roster_lines else "（暂无记录）"
            blocks.append(f"<重要角色索引>\n{roster_body}\n</重要角色索引>")
        events = self._catalog_rows(
            "chronicle_events", "code_index, time_span, summary, trigger_when, related_files, tk_estimate",
            f"status = 'active' AND {visibility} AND code_index NOT IN (SELECT code_index FROM memory_provenance)", "CAST(SUBSTR(code_index, 3) AS INTEGER) DESC, code_index DESC",
        ) if include_unscoped_media_and_events else []
        files = self._catalog_rows(
            "media_vault",
            "file_code, semantic_name, mime_type, chat_context, trigger_when",
            visibility, "CAST(SUBSTR(file_code, 3) AS INTEGER) DESC, file_code DESC",
        ) if include_unscoped_media_and_events else []
        blocks.append(_render_catalog("已发生的事件概览", events, (
            "code_index", "time_span", "summary", "trigger_when", "related_files", "tk_estimate",
        )))
        blocks.append(_render_catalog("文件身份证目录", files, (
            "file_code", "semantic_name", "mime_type", "chat_context", "trigger_when",
        )))
        sops = [dict(row) for row in self.conn.execute(
            "SELECT sop_id, app_name, feature_name, description, trigger_when FROM app_sop_skills ORDER BY sop_id"
        )] if owner else []
        blocks.append(_render_catalog("手机操作SOP目录", sops, ("sop_id", "app_name", "feature_name", "description", "trigger_when")))
        if owner:
            surfing = [dict(row) for row in self.conn.execute(
                "SELECT surf_code, app_name, post_summary, screenshot_file_code, trigger_when "
                "FROM phone_surfing_log WHERE is_shared = 0 ORDER BY CAST(SUBSTR(surf_code, 3) AS INTEGER) DESC"
            )]
            blocks.append(_render_catalog("手机冲浪待分享库存", surfing, (
                "surf_code", "app_name", "post_summary", "screenshot_file_code", "trigger_when",
            )))
        return "\n\n".join(blocks)

    def load_greenlight_payload(
        self, *, memory_codes: Sequence[str], file_codes: Sequence[str],
        sender_trust_role: str, is_group_chat: bool, person_ids: Sequence[str] = (),
        include_unscoped_media_and_events: bool = True,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        owner = not is_group_chat and sender_trust_role == "OWNER"
        visibility = "" if owner else " AND visibility = 'PUBLIC'"
        memories: List[str] = []
        files: List[Dict[str, Any]] = []
        # 读出冲浪详情与标记已分享处于同一事务；重复绿灯只装载一次。
        with self.conn:
            for code in dict.fromkeys(memory_codes):
                if code.startswith("AM") and include_unscoped_media_and_events:
                    row = self.conn.execute(
                        "SELECT code_index, time_span, location, chronicle_text, key_dialogue "
                        "FROM chronicle_events WHERE code_index = ? AND status = 'active' AND code_index NOT IN (SELECT code_index FROM memory_provenance)" + visibility, (code,),
                    ).fetchone()
                elif code.startswith("SF") and owner:
                    row = self.conn.execute("SELECT * FROM phone_surfing_log WHERE surf_code = ?", (code,)).fetchone()
                    if row is not None:
                        self.conn.execute("UPDATE phone_surfing_log SET is_shared = 1 WHERE surf_code = ?", (code,))
                else:
                    continue
                if row is not None:
                    memories.append(json.dumps(dict(row), ensure_ascii=False))
            if owner:
                people: List[str] = []
                for person_id in dict.fromkeys(person_ids):
                    row = self.conn.execute(
                        "SELECT * FROM person_info WHERE person_id = ?", (person_id,),
                    ).fetchone()
                    if row is not None:
                        people.append(json.dumps(dict(row), ensure_ascii=False))
                if people:
                    memories.append("<重要角色详情>\n" + "\n".join(people) + "\n</重要角色详情>")
            if include_unscoped_media_and_events:
                for code in dict.fromkeys(file_codes):
                    row = self.conn.execute("SELECT * FROM media_vault WHERE file_code = ?" + visibility, (code,)).fetchone()
                    if row is not None:
                        files.append(dict(row))
        return "<记忆回溯>\n" + "\n".join(memories) + "\n</记忆回溯>", files
