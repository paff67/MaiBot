"""table_sql 整合与躯体状态更新；异步调用方必须使用 asyncio.to_thread。"""

from pathlib import Path
import asyncio
import datetime
import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
import zoneinfo

from .sql_normalizer import normalize_sql_structure
from .truth_db import CompanionTruthDB

_CST = zoneinfo.ZoneInfo("Asia/Shanghai")


def _format_cst(create_time_val) -> str:
    if create_time_val is None or not str(create_time_val).strip():
        return ""
    try:
        ts = float(create_time_val)
        if ts > 1e11:
            ts /= 1000.0
        return datetime.datetime.fromtimestamp(ts, tz=_CST).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return ""


def _open_truth_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return CompanionTruthDB(path)


SHUJUKU_CONSOLIDATION_SYSTEM_PROMPT = """
把对话整理为 table_sql 的 INSERT / UPDATE / DELETE；禁止输出 DDL、SELECT 子查询和事务控制。
以下 DDL 仅说明现有结构，不得修改其他表：
CREATE TABLE global_state (
 row_id INTEGER PRIMARY KEY CHECK(row_id=1), current_location TEXT,
 current_minor_region TEXT, current_major_region TEXT, prev_scene_time TEXT,
 elapsed_time TEXT, cur_time TEXT, body_state TEXT DEFAULT 'IDLE',
 current_app TEXT DEFAULT '', live_observation TEXT DEFAULT '', latest_screen_file_code TEXT DEFAULT ''
);
CREATE TABLE user_status (key TEXT PRIMARY KEY, category TEXT, current_value TEXT,
 visibility TEXT DEFAULT 'PRIVATE', updated_at TEXT);
CREATE TABLE quests_events (quest_code TEXT PRIMARY KEY, title TEXT, current_progress TEXT,
 deadline_or_condition TEXT, status TEXT DEFAULT 'ongoing',
 visibility TEXT DEFAULT 'PRIVATE', updated_at TEXT);
CREATE TABLE chronicle_events (code_index TEXT PRIMARY KEY, time_span TEXT, location TEXT,
 summary TEXT, chronicle_text TEXT, key_dialogue TEXT, trigger_when TEXT,
 tk_estimate INTEGER DEFAULT 120, related_files TEXT DEFAULT '[]',
 visibility TEXT DEFAULT 'PRIVATE', status TEXT DEFAULT 'active', valid_from TEXT, valid_until TEXT);
CREATE TABLE person_info (person_id TEXT PRIMARY KEY, platform_ids TEXT DEFAULT '{{}}', person_name TEXT,
 wechat_remark TEXT, trust_role TEXT DEFAULT 'GROUP_MEMBER', one_line_intro TEXT,
 impression TEXT, intimacy_score REAL DEFAULT 0, shared_episodes TEXT DEFAULT '[]');
本批自动分配的起始编号为 {next_am_code}，新纪要从该 AMXXXX 编号递增，禁止复用旧编号。
所有时间字段（time_span、cur_time、valid_from、valid_until、updated_at）必须严格使用输入中的 create_time_cst（北京时间 Asia/Shanghai，格式 YYYY-MM-DD HH:MM），严禁使用 UTC 或美西时间。
time_span 格式 YYYY-MM-DD HH:MM ~ YYYY-MM-DD HH:MM；summary ≤30字；
chronicle_text 为240~480字去修辞、中立第三方客观纪要；key_dialogue ≤5句双方原话，
总计≤150 Tokens，不能编造或改写台词；trigger_when 写因果触发条件；tk_estimate 估计详情 Token 数。
related_files 为 FMXXXX 的 JSON 数组。chronicle_events 必须保持 visibility='PRIVATE'。
身份与权限铁律：
1. 输入中 direction="outgoing" 或 speaker_role="BOT_SELF" 的消息是 AI 伴侣自己发出的回复，严禁把 AI 伴侣自己的名字、年龄、职业、人设或自称写入 user_status 或 person_info。
2. user_status 仅记录主人（OWNER）本人的个人高频硬状态，按 key 插入或覆写 current_value，更新 category、updated_at。
3. global_state 仅 row_id=1：维护三级地点和时间流逝，不改手机实时躯体字段。群聊（chat_id 以 @chatroom 结尾）严禁修改 global_state 和 user_status。
4. quests_events 维护约定、待办与长期事项进度；person_info 仅更新印象、简介与亲密度，禁止修改 trust_role、platform_ids 及已有 person_name。
5. <recent_context_readonly> 仅提供前文语境参考，只对本批新输入做整合。
新事件 INSERT，纠错 UPDATE；被替代事件置 status='superseded' 并填 valid_until，
然后 INSERT 新事件并填 valid_from；保留旧事件与原话，不用 DELETE 擦除历史。
只写有明确对话依据的实质事实，不虚构时间地点；寒暄、玩笑、简单测试消息（如 "111"、"222"、"333"）或无实质新事实可记时直接输出 -- NONE；SQL 字符串的单引号用两个单引号转义。
""".strip()

CONSOLIDATION_PROMPT = SHUJUKU_CONSOLIDATION_SYSTEM_PROMPT


def _strip_sql_fence(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"\A```(?:sql)?\s*([\s\S]*?)\s*```\Z", stripped, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return stripped


def extract_table_sql(model_output: str | dict) -> str:
    """从模型输出（原始 SQL、Markdown 代码块或包含 table_sql 的 JSON）提取 SQL。"""
    if isinstance(model_output, dict):
        if set(model_output) == {"table_sql"} and isinstance(model_output["table_sql"], str):
            return _strip_sql_fence(model_output["table_sql"])
        raise ValueError("Invalid consolidation result: expected table_sql")
    if not isinstance(model_output, str):
        raise ValueError("Invalid consolidation result: expected SQL string or table_sql dict")
    cleaned = _strip_sql_fence(model_output)
    if cleaned.startswith("{"):
        try:
            parsed = json.loads(cleaned)
        except ValueError as exc:
            raise ValueError("Invalid JSON consolidation output") from exc
        if isinstance(parsed, dict) and set(parsed) == {"table_sql"} and isinstance(parsed["table_sql"], str):
            return _strip_sql_fence(parsed["table_sql"])
        raise ValueError("JSON consolidation output must contain only table_sql")
    return cleaned


def _is_noop_table_sql(raw_sql: str) -> bool:
    normalized = normalize_sql_structure(raw_sql)
    stripped = re.sub(r"(?:\s|--[^\n]*(?:\n|\Z)|/\*[\s\S]*?\*/)+", "", normalized)
    return not stripped


def build_consolidation_system_prompt(db_path: str) -> str:
    """提供当前起始编号；并发冲突由主键约束暴露，由调用方重新整合。"""
    db = _open_truth_db(db_path)
    try:
        return SHUJUKU_CONSOLIDATION_SYSTEM_PROMPT.format(
            next_am_code=db.next_code("AM", "chronicle_events", "code_index"),
        )
    finally:
        db.close()


def apply_consolidation_sql(db_path: str, raw_sql: str) -> int:
    db = _open_truth_db(db_path)
    try:
        return db.execute_table_sql(raw_sql)
    finally:
        db.close()


def update_body_state(
    db_path: str,
    *,
    body_state: str,
    current_app: str,
    live_observation: str,
    latest_screen_file_code: str = "",
) -> None:
    db = _open_truth_db(db_path)
    try:
        with db.conn:
            db.conn.execute("INSERT INTO global_state (row_id) VALUES (1) ON CONFLICT(row_id) DO NOTHING")
            db.conn.execute(
                "UPDATE global_state SET body_state=?, current_app=?, live_observation=?, "
                "latest_screen_file_code=? WHERE row_id=1",
                (body_state, current_app, live_observation, latest_screen_file_code),
            )
    finally:
        db.close()


# The durable host implementation of shujuku's scoped update/guard/commit stages.
# Keep chronicle_events as the memory store; these tables hold jobs and provenance only.
MEMORY_JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_job_cursor(scope TEXT PRIMARY KEY,seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS memory_jobs(
 job_id TEXT PRIMARY KEY,scope TEXT NOT NULL,source TEXT NOT NULL,account TEXT NOT NULL,chat_id TEXT NOT NULL,
 sources TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,lease_token TEXT,error TEXT);
CREATE INDEX IF NOT EXISTS memory_due ON memory_jobs(scope,state,next_attempt,lease_until);
CREATE TABLE IF NOT EXISTS memory_provenance(
 code_index TEXT NOT NULL,job_id TEXT NOT NULL,source TEXT NOT NULL,account TEXT NOT NULL,chat_id TEXT NOT NULL,
 msg_id TEXT NOT NULL,revision TEXT NOT NULL,visibility TEXT NOT NULL CHECK(visibility='PRIVATE'),
 PRIMARY KEY(code_index,source,account,msg_id));
CREATE INDEX IF NOT EXISTS memory_source ON memory_provenance(source,account,chat_id,msg_id);
CREATE TABLE IF NOT EXISTS memory_coverage(
 scope TEXT NOT NULL,msg_id TEXT NOT NULL,revision TEXT NOT NULL,job_id TEXT NOT NULL,
 PRIMARY KEY(scope,msg_id,revision));
"""


def _source_current(db, source, account, chat, identity):
    row = db.execute(
        "SELECT revision,chat_id,kind FROM ledger.messages WHERE source=? AND account=? AND msg_id=?",
        (source, account, identity["msg_id"]),
    ).fetchone()
    return bool(row and row[0] == identity["revision"] and row[1] == chat and (int(row[2] or 0) & 65535) != 10000)


class MemoryConsolidator:
    """Short SQLite transactions; no model/network work while locks are held."""

    def __init__(self, truth_path, ledger_path, *, source, account, chat_id, backfill=False, lease_seconds=300):
        if not source or not re.fullmatch(r"[a-f0-9]{32}", account) or not chat_id or lease_seconds <= 0:
            raise ValueError("Explicit source/account/conversation and positive lease required")
        self.truth = _open_truth_db(truth_path)
        self.db = self.truth.conn
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=10000")
        ledger_file = Path(ledger_path).resolve()
        if not ledger_file.is_file():
            self.close()
            raise FileNotFoundError(ledger_file)
        self.db.execute("ATTACH DATABASE ? AS ledger", (str(ledger_file),))
        self.db.executescript(MEMORY_JOB_SCHEMA)
        self.source, self.account, self.chat = source, account, chat_id
        self.scope = hashlib.sha256(json.dumps([source, account, chat_id, "memory-v1"]).encode()).hexdigest()
        self.lease_seconds = lease_seconds
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            start = (
                0
                if backfill
                else self.db.execute("SELECT COALESCE(MAX(rowid),0) FROM ledger.message_revisions").fetchone()[0]
            )
            self.db.execute("INSERT OR IGNORE INTO memory_job_cursor VALUES(?,?)", (self.scope, start))

    def close(self):
        self.truth.close()

    def build_prompt(self, inputs=None) -> str:
        from src.platform_io.wechat.message_quotes import safe_body

        prompt = SHUJUKU_CONSOLIDATION_SYSTEM_PROMPT.format(
            next_am_code=self.truth.next_code("AM", "chronicle_events", "code_index"),
        )
        if inputs is not None:
            context_items = []
            if inputs and isinstance(inputs, list) and inputs[0].get("msg_id"):
                try:
                    first_mid = int(inputs[0]["msg_id"])
                    prev_rows = self.db.execute(
                        "SELECT msg_id,kind,body,sender_id,direction,row_json FROM ledger.messages "
                        "WHERE source=? AND account=? AND chat_id=? AND CAST(msg_id AS INTEGER)<? "
                        "AND (CAST(kind AS INTEGER)&65535)!=10000 "
                        "ORDER BY CAST(msg_id AS INTEGER) DESC LIMIT 5",
                        (self.source, self.account, self.chat, first_mid),
                    ).fetchall()
                    for prow in reversed(prev_rows):
                        pbody = safe_body(prow[1], prow[2] or "")
                        prow_data = json.loads(prow[5]) if prow[5] else {}
                        pct = prow_data.get("createTime")
                        pdir = prow[4]
                        psender = prow[3]
                        prole = "BOT_SELF" if pdir == "outgoing" or psender in (None, "", "SELF") else "USER"
                        context_items.append(
                            dict(
                                msg_id=prow[0],
                                content=pbody,
                                sender_id=psender,
                                direction=pdir,
                                speaker_role=prole,
                                create_time=pct,
                                create_time_cst=_format_cst(pct),
                            )
                        )
                except Exception:
                    context_items = []
            if context_items:
                prompt += (
                    "\n<recent_context_readonly>\n"
                    + json.dumps(context_items, ensure_ascii=False)
                    + "\n</recent_context_readonly>"
                )
            prompt = prompt + "\n" + json.dumps(inputs, ensure_ascii=False)
        return prompt

    def read_scoped(self, codes=None):
        # Read-time revision guard closes the gap before enqueue observes a revoke/update.
        from .access_policy import allowed_conversations, can_read_memory

        allowed = allowed_conversations(
            self.db, source=self.source, account=self.account, requester_id=self.chat, destination_chat_id=self.chat
        )
        if not allowed:
            return []
        params = [self.source, self.account, *sorted(allowed)]
        extra = ""
        if codes is not None:
            if not codes:
                return []
            extra = " AND c.code_index IN (" + ",".join("?" for _ in codes) + ")"
            params.extend(codes)
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT c.* FROM chronicle_events c WHERE c.status='active' AND c.visibility='PRIVATE' "
                "AND EXISTS(SELECT 1 FROM memory_provenance p WHERE p.code_index=c.code_index AND p.source=? AND p.account=? AND p.chat_id IN ("
                + ",".join("?" for _ in allowed)
                + ")) "
                "AND NOT EXISTS(SELECT 1 FROM memory_provenance p LEFT JOIN ledger.messages m ON m.source=p.source AND m.account=p.account AND m.msg_id=p.msg_id "
                "WHERE p.code_index=c.code_index AND (m.msg_id IS NULL OR m.revision!=p.revision OR m.chat_id!=p.chat_id "
                "OR (CAST(m.kind AS INTEGER)&65535)=10000))" + extra + " ORDER BY c.code_index",
                params,
            )
            if can_read_memory(
                self.db, r["code_index"], source=self.source, account=self.account, conversations=allowed
            )
        ]

    def retry_failed(self):
        with self.db:
            return self.db.execute(
                "UPDATE memory_jobs SET state='retry',attempts=0,next_attempt=0,lease_until=0 WHERE scope=? AND state='failed'",
                (self.scope,),
            ).rowcount

    def enqueue(self, limit=20, min_batch_size=1, max_wait_seconds=0):
        if not 1 <= limit <= 100:
            raise ValueError("Invalid batch size")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            cursor = self.db.execute("SELECT seq FROM memory_job_cursor WHERE scope=?", (self.scope,)).fetchone()[0]
            events = self.db.execute(
                "SELECT rowid,payload FROM ledger.message_revisions WHERE source=? AND rowid>? ORDER BY rowid LIMIT ?",
                (self.source, cursor, limit),
            ).fetchall()
            identities = {}
            had_affected = False
            first_match_rowid = None
            earliest_sec = None
            for rowid, payload in events:
                event = json.loads(payload)
                if event["account"] != self.account or event["chat_id"] != self.chat:
                    continue
                if first_match_rowid is None:
                    first_match_rowid = rowid
                raw_ts = event.get("observed_at_ms") or (event.get("row") or {}).get("createTime") or 0
                try:
                    ts_val = float(raw_ts)
                    ts_sec = ts_val / 1000.0 if ts_val > 1e11 else ts_val
                    if ts_sec > 0 and (earliest_sec is None or ts_sec < earliest_sec):
                        earliest_sec = ts_sec
                except (ValueError, TypeError):
                    pass
                identity = dict(msg_id=event["msg_id"], revision=event["revision"])
                affected = self.db.execute(
                    "SELECT DISTINCT p.job_id FROM memory_provenance p JOIN chronicle_events c USING(code_index) "
                    "LEFT JOIN ledger.messages m ON m.source=p.source AND m.account=p.account AND m.msg_id=p.msg_id "
                    "WHERE c.status='active' AND p.source=? AND p.account=? AND p.chat_id=? AND p.msg_id=? "
                    "AND (m.msg_id IS NULL OR m.revision!=p.revision OR m.chat_id!=p.chat_id OR (CAST(m.kind AS INTEGER)&65535)=10000)",
                    (self.source, self.account, self.chat, event["msg_id"]),
                ).fetchall()
                if affected:
                    had_affected = True
                for affected_job in affected:
                    # A summary depended on every input. Requeue all surviving current inputs,
                    # not just the edited message, to avoid losing unrelated facts in that summary.
                    old_sources = self.db.execute(
                        "SELECT sources FROM memory_jobs WHERE job_id=?", (affected_job[0],)
                    ).fetchone()
                    self.db.execute("DELETE FROM memory_coverage WHERE job_id=?", (affected_job[0],))
                    if old_sources:
                        for dependency in json.loads(old_sources[0]):
                            current = self.db.execute(
                                "SELECT msg_id,revision,chat_id,kind FROM ledger.messages WHERE source=? AND account=? AND msg_id=?",
                                (self.source, self.account, dependency["msg_id"]),
                            ).fetchone()
                            if current and current[2] == self.chat and (int(current[3] or 0) & 65535) != 10000:
                                identities[current[0]] = dict(msg_id=current[0], revision=current[1])
                # Old memory never remains active merely because a new model request failed.
                superseded_rows = self.db.execute(
                    "UPDATE chronicle_events SET status='superseded' WHERE code_index IN "
                    "(SELECT p.code_index FROM memory_provenance p LEFT JOIN ledger.messages m "
                    "ON m.source=p.source AND m.account=p.account AND m.msg_id=p.msg_id "
                    "WHERE p.source=? AND p.account=? AND p.chat_id=? AND p.msg_id=? "
                    "AND (m.msg_id IS NULL OR m.revision!=p.revision OR m.chat_id!=p.chat_id OR (CAST(m.kind AS INTEGER)&65535)=10000))",
                    (self.source, self.account, self.chat, event["msg_id"]),
                ).rowcount
                if superseded_rows > 0:
                    had_affected = True
                if _source_current(self.db, self.source, self.account, self.chat, identity):
                    identities[event["msg_id"]] = identity
            if (
                min_batch_size > 1
                and max_wait_seconds > 0
                and not had_affected
                and 0 < len(identities) < min_batch_size
                and earliest_sec is not None
                and (time.time() - earliest_sec) < max_wait_seconds
            ):
                if first_match_rowid is not None and first_match_rowid - 1 > cursor:
                    self.db.execute(
                        "UPDATE memory_job_cursor SET seq=? WHERE scope=?", (first_match_rowid - 1, self.scope)
                    )
                return 0
            if identities:
                sources = json.dumps(sorted(identities.values(), key=lambda r: int(r["msg_id"])), sort_keys=True)
                job_id = hashlib.sha256((self.scope + sources).encode()).hexdigest()
                self.db.execute(
                    "INSERT INTO memory_jobs(job_id,scope,source,account,chat_id,sources) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(job_id) DO UPDATE SET state='pending',next_attempt=0,lease_until=0 "
                    "WHERE memory_jobs.state='superseded'",
                    (job_id, self.scope, self.source, self.account, self.chat, sources),
                )
            if events:
                self.db.execute("UPDATE memory_job_cursor SET seq=? WHERE scope=?", (events[-1][0], self.scope))
        return len(events)

    def claim(self):
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute(
                "SELECT 1 FROM memory_jobs WHERE scope=? AND state='running' AND lease_until>?", (self.scope, now)
            ).fetchone():
                return None
            row = self.db.execute(
                "SELECT * FROM memory_jobs WHERE scope=? AND state IN ('pending','retry','running') "
                "AND next_attempt<=? AND lease_until<=? ORDER BY rowid LIMIT 1",
                (self.scope, now, now),
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            job["lease_token"] = uuid.uuid4().hex
            self.db.execute(
                "UPDATE memory_jobs SET state='running',attempts=attempts+1,lease_token=?,lease_until=? WHERE job_id=?",
                (job["lease_token"], now + self.lease_seconds, job["job_id"]),
            )
            return job

    def prepare(self, job):
        from src.platform_io.wechat.message_quotes import safe_body

        inputs = []
        for identity in json.loads(job["sources"]):
            if self.db.execute(
                "SELECT 1 FROM memory_coverage WHERE scope=? AND msg_id=? AND revision=?",
                (self.scope, identity["msg_id"], identity["revision"]),
            ).fetchone():
                return None
            if not _source_current(self.db, self.source, self.account, self.chat, identity):
                return None
            row = self.db.execute(
                "SELECT kind,body,sender_id,direction,row_json FROM ledger.messages WHERE source=? AND account=? AND msg_id=?",
                (self.source, self.account, identity["msg_id"]),
            ).fetchone()
            body = safe_body(row[0], row[1] or "")
            if body.get("truncated"):
                raise ValueError("Source exceeds unabridged input budget")
            ct = json.loads(row[4]).get("createTime")
            sender_id = row[2]
            direction = row[3]
            speaker_role = "BOT_SELF" if direction == "outgoing" or sender_id in (None, "", "SELF") else "USER"
            inputs.append(
                dict(
                    identity,
                    content=body,
                    sender_id=sender_id,
                    direction=direction,
                    speaker_role=speaker_role,
                    create_time=ct,
                    create_time_cst=_format_cst(ct),
                )
            )
        if len(json.dumps(inputs, ensure_ascii=False)) > 48000:
            raise ValueError("Batch exceeds input budget; requires smaller batch")
        return inputs

    def fail(self, job, error):
        with self.db:
            self.db.execute(
                "UPDATE memory_jobs SET state=CASE WHEN attempts>=5 THEN 'failed' ELSE 'retry' END, "
                "error=?,next_attempt=?,lease_until=0 WHERE job_id=? AND lease_token=? AND state='running'",
                (
                    type(error).__name__,
                    time.time() + min(3600, 30 * 2 ** min(job["attempts"], 7)),
                    job["job_id"],
                    job["lease_token"],
                ),
            )

    def _validate_legacy_memories(self, memories):
        if not isinstance(memories, list) or len(memories) > 10:
            raise ValueError("Invalid consolidation result")
        for memory in memories:
            if not isinstance(memory, dict) or set(memory) != {"summary", "detail", "source_ids", "quotes", "trigger"}:
                raise ValueError("Model cannot write scope, permissions, SQL or arbitrary fields")
            if any(not isinstance(memory[k], str) or len(memory[k]) > 16000 for k in ("summary", "detail", "trigger")):
                raise ValueError("Invalid memory text")
            if not memory["summary"].strip() or not memory["detail"].strip():
                raise ValueError("Empty memory")
            if (
                not isinstance(memory["source_ids"], list)
                or not memory["source_ids"]
                or any(not isinstance(x, str) for x in memory["source_ids"])
            ):
                raise ValueError("Memory requires source IDs")
            if not isinstance(memory["quotes"], list) or any(not isinstance(q, str) for q in memory["quotes"]):
                raise ValueError("Invalid quotations")

    def commit(self, job, result):
        legacy_mode = isinstance(result, dict) and set(result) == {"memories"}
        raw_sql = ""
        is_noop = False
        if legacy_mode:
            self._validate_legacy_memories(result["memories"])
        else:
            raw_sql = extract_table_sql(result)
            is_noop = _is_noop_table_sql(raw_sql)
            if not is_noop:
                self.truth._split_write_statements(normalize_sql_structure(raw_sql))

        with self.db:
            # BEGIN IMMEDIATE locks the attached source against concurrent ingestion until commit.
            # Only main DB is modified: job + truth tables + provenance commit together even under WAL.
            self.db.execute("BEGIN IMMEDIATE")
            lease = self.db.execute(
                "SELECT state,lease_token,lease_until FROM memory_jobs WHERE job_id=?", (job["job_id"],)
            ).fetchone()
            if not lease or tuple(lease[:2]) != ("running", job["lease_token"]) or lease[2] <= time.time():
                return False
            inputs = self.prepare(job)
            if inputs is None:
                # Rebuild the batch from current surviving inputs so one revoked message
                # does not drop unrelated facts that were waiting in the same job.
                survivors = []
                for old in json.loads(job["sources"]):
                    row = self.db.execute(
                        "SELECT msg_id,revision,chat_id,kind FROM ledger.messages WHERE source=? AND account=? AND msg_id=?",
                        (self.source, self.account, old["msg_id"]),
                    ).fetchone()
                    if (
                        row
                        and row[2] == self.chat
                        and (int(row[3] or 0) & 65535) != 10000
                        and not self.db.execute(
                            "SELECT 1 FROM memory_coverage WHERE scope=? AND msg_id=? AND revision=?",
                            (self.scope, row[0], row[1]),
                        ).fetchone()
                    ):
                        survivors.append(dict(msg_id=row[0], revision=row[1]))
                if survivors:
                    payload = json.dumps(sorted(survivors, key=lambda x: int(x["msg_id"])), sort_keys=True)
                    replacement = hashlib.sha256((self.scope + payload).encode()).hexdigest()
                    self.db.execute(
                        "INSERT OR IGNORE INTO memory_jobs(job_id,scope,source,account,chat_id,sources) VALUES(?,?,?,?,?,?)",
                        (replacement, self.scope, self.source, self.account, self.chat, payload),
                    )
                self.db.execute(
                    "UPDATE memory_jobs SET state='superseded',lease_until=0 WHERE job_id=?", (job["job_id"],)
                )
                return False

            if legacy_mode:
                sources = {x["msg_id"]: x for x in inputs}
                for memory in result["memories"]:
                    if not set(memory["source_ids"]) <= set(sources):
                        raise ValueError("Invented source ID")
                    originals = [sources[mid]["content"].get("text", "") for mid in memory["source_ids"]]
                    if any(not q or not any(q in text for text in originals) for q in memory["quotes"]):
                        raise ValueError("Invented quotation")
                    code = self.truth.next_code("AM", "chronicle_events", "code_index")
                    self.db.execute(
                        "INSERT INTO chronicle_events(code_index,summary,chronicle_text,key_dialogue,trigger_when,visibility,status) "
                        "VALUES(?,?,?,?,?,'PRIVATE','active')",
                        (
                            code,
                            memory["summary"],
                            memory["detail"],
                            json.dumps(memory["quotes"], ensure_ascii=False),
                            memory["trigger"],
                        ),
                    )
                    self.db.executemany(
                        "INSERT INTO memory_provenance VALUES(?,?,?,?,?,?,?,?)",
                        [
                            (
                                code,
                                job["job_id"],
                                self.source,
                                self.account,
                                self.chat,
                                x["msg_id"],
                                x["revision"],
                                "PRIVATE",
                            )
                            for x in inputs
                        ],
                    )
            elif not is_noop:
                before_events = {
                    row["code_index"]: tuple(row)
                    for row in self.db.execute("SELECT * FROM chronicle_events")
                }
                is_group_scope = self.chat.endswith("@chatroom")
                if is_group_scope:
                    before_user_status = [tuple(row) for row in self.db.execute("SELECT * FROM user_status ORDER BY key")]
                    before_global_state = [tuple(row) for row in self.db.execute("SELECT * FROM global_state ORDER BY row_id")]
                    before_person_names = {
                        row["person_id"]: row["person_name"]
                        for row in self.db.execute(
                            "SELECT person_id, person_name FROM person_info WHERE COALESCE(TRIM(person_name), '') != ''"
                        )
                    }
                foreign_codes = {
                    row[0]
                    for row in self.db.execute(
                        "SELECT DISTINCT code_index FROM memory_provenance WHERE source!=? OR account!=? OR chat_id!=?",
                        (self.source, self.account, self.chat),
                    )
                }
                self.truth.execute_table_sql(raw_sql, manage_tx=False)
                if is_group_scope:
                    after_user_status = [tuple(row) for row in self.db.execute("SELECT * FROM user_status ORDER BY key")]
                    after_global_state = [tuple(row) for row in self.db.execute("SELECT * FROM global_state ORDER BY row_id")]
                    if before_user_status != after_user_status or before_global_state != after_global_state:
                        raise ValueError("Group chat consolidation cannot modify owner user_status or global_state")
                    after_person_names = {
                        row["person_id"]: row["person_name"]
                        for row in self.db.execute("SELECT person_id, person_name FROM person_info")
                    }
                    if any(after_person_names.get(pid) != pname for pid, pname in before_person_names.items()):
                        raise ValueError("Group chat consolidation cannot overwrite existing person_name")
                after_rows = [dict(row) for row in self.db.execute("SELECT * FROM chronicle_events")]
                after_events = {row["code_index"]: tuple(row.values()) for row in after_rows}
                for fcode in foreign_codes:
                    if before_events.get(fcode) != after_events.get(fcode):
                        raise ValueError("table_sql cannot modify chronicle_events from another conversation scope")
                for code_index in set(before_events) - set(after_events):
                    if code_index in foreign_codes:
                        raise ValueError("table_sql cannot delete chronicle_events from another conversation scope")
                for row in after_rows:
                    code = row["code_index"]
                    if before_events.get(code) == tuple(row.values()):
                        continue
                    if row["visibility"] != "PRIVATE":
                        raise ValueError("Chat consolidation memories must be PRIVATE")
                    if not (row["summary"] or "").strip() or not (row["chronicle_text"] or "").strip():
                        raise ValueError("Empty memory summary or chronicle_text")
                    self.db.executemany(
                        "INSERT OR REPLACE INTO memory_provenance VALUES(?,?,?,?,?,?,?,?)",
                        [
                            (
                                code,
                                job["job_id"],
                                self.source,
                                self.account,
                                self.chat,
                                x["msg_id"],
                                x["revision"],
                                "PRIVATE",
                            )
                            for x in inputs
                        ],
                    )

            self.db.executemany(
                "INSERT INTO memory_coverage VALUES(?,?,?,?)",
                [(self.scope, x["msg_id"], x["revision"], job["job_id"]) for x in inputs],
            )
            self.db.execute(
                "UPDATE memory_jobs SET state='done',lease_until=0,error=NULL WHERE job_id=?", (job["job_id"],)
            )
        return True

    async def run_one(self, generate):
        job = self.claim()
        if job is None:
            return False
        try:
            inputs = self.prepare(job)
            if inputs is None:
                self.commit(job, "-- NONE")
            else:
                self.commit(job, await generate(inputs))
        except asyncio.CancelledError:
            raise  # lease expiry makes interrupted work recoverable
        except Exception as exc:
            self.fail(job, exc)
        return True


def read_scoped_memories(truth_path, ledger_path, *, source, account, chat_id, codes=None):
    # Reuse the guarded read, without initializing a cursor or writing a model-generated table.
    worker = object.__new__(MemoryConsolidator)
    worker.source, worker.account, worker.chat = source, account, chat_id
    worker.db = sqlite3.connect(Path(truth_path).resolve().as_uri() + "?mode=ro", uri=True)
    worker.db.row_factory = sqlite3.Row
    try:
        worker.db.execute("ATTACH DATABASE ? AS ledger", (Path(ledger_path).resolve().as_uri() + "?mode=ro",))
        return worker.read_scoped(codes)
    finally:
        worker.db.close()


def _load_driver_allowed_chats(driver_config_path: str) -> dict[tuple[str, str], list[str]]:
    import tomllib

    path = Path(driver_config_path)
    if not path.is_file():
        return {}
    with path.open("rb") as stream:
        drv_cfg = tomllib.load(stream)
    mapping: dict[tuple[str, str], list[str]] = {}
    for item in drv_cfg.get("drivers", []):
        if not item.get("enabled", False):
            continue
        source = str(item.get("source", "")).rstrip("/")
        account = str(item.get("account", ""))
        if not source or not account:
            continue
        raw_chats = [str(c).strip() for c in item.get("allowed_chats", []) if str(c).strip()]
        mapping.setdefault((source, account), []).extend(raw_chats or ["*"])
    return mapping


def _discover_ledger_chat_ids(ledger_path: str, source: str, account: str) -> list[str]:
    from src.platform_io.wechat.driver import is_supported_wechat_chat_id

    path = Path(ledger_path)
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10) as db:
            rows = db.execute(
                "SELECT chat_id, MAX(COALESCE(updated_at_ms, 0)) AS latest_ts "
                "FROM messages WHERE source=? AND account=? GROUP BY chat_id ORDER BY latest_ts DESC",
                (source.rstrip("/"), account),
            ).fetchall()
        return [str(r[0]) for r in rows if is_supported_wechat_chat_id(str(r[0] or ""))]
    except Exception:
        return []


def configured_consolidation_tasks(config_path):
    """Per-account/chat or dynamic account-wide ('*') consolidation configuration."""
    import tomllib

    with Path(config_path).open("rb") as stream:
        config = tomllib.load(stream)
    if config.get("version") != 1:
        raise ValueError("Unsupported consolidation configuration")
    driver_cfg_path = str(config.get("driver_config_path") or "").strip()
    if not driver_cfg_path and config.get("sync_wechat_drivers", False):
        driver_cfg_path = os.environ.get("MAIBOT_WECHAT_CONFIG", "").strip()
    driver_chats_map = _load_driver_allowed_chats(driver_cfg_path) if driver_cfg_path else {}

    tasks = []
    seen = set()
    for item in config.get("scopes", []):
        if not item.get("enabled", False):
            continue
        source = str(item["source"]).rstrip("/")
        account = str(item["account"])
        explicit_chats: list[str] = []
        raw_chat_id = str(item.get("chat_id") or "*").strip()
        if raw_chat_id and raw_chat_id != "*":
            explicit_chats.append(raw_chat_id)
        for extra_chat in item.get("chat_ids", []):
            c = str(extra_chat).strip()
            if c and c != "*":
                explicit_chats.append(c)

        for chat_id in explicit_chats:
            key = (source, account, chat_id)
            if key in seen:
                raise ValueError("Duplicate consolidation scope")
            seen.add(key)
            tasks.append(
                dict(
                    truth_path=config["truth_path"],
                    ledger_path=config["ledger_path"],
                    source=source,
                    account=account,
                    chat_id=chat_id,
                    task_name=item["model_task"],
                    batch_size=item.get("batch_size", 20),
                    backfill=item.get("backfill", False),
                )
            )

        sync_driver = bool(
            item.get("sync_allowed_chats", False)
            or raw_chat_id == "*"
            or config.get("sync_wechat_drivers", False)
            or bool(driver_cfg_path)
        )
        if sync_driver:
            item_drv_path = str(item.get("driver_config_path") or "").strip()
            scoped_map = _load_driver_allowed_chats(item_drv_path) if item_drv_path else driver_chats_map
            drv_chats = scoped_map.get((source, account), [])
            non_wildcard = [c for c in drv_chats if c != "*"]
            if non_wildcard:
                for chat_id in non_wildcard:
                    key = (source, account, chat_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    tasks.append(
                        dict(
                            truth_path=config["truth_path"],
                            ledger_path=config["ledger_path"],
                            source=source,
                            account=account,
                            chat_id=chat_id,
                            task_name=item["model_task"],
                            batch_size=item.get("batch_size", 20),
                            backfill=item.get("backfill", False),
                        )
                    )
            elif raw_chat_id == "*" or "*" in drv_chats:
                key = (source, account, "*")
                if key not in seen:
                    seen.add(key)
                    tasks.append(
                        dict(
                            truth_path=config["truth_path"],
                            ledger_path=config["ledger_path"],
                            source=source,
                            account=account,
                            chat_id="*",
                            task_name=item["model_task"],
                            batch_size=item.get("batch_size", 20),
                            backfill=item.get("backfill", False),
                        )
                    )
    return tasks


async def run_consolidation_service(
    *, truth_path, ledger_path, source, account, chat_id, task_name, backfill=False, once=False, batch_size=20
):
    from src.services.llm_service import LLMServiceClient

    if not 1 <= batch_size <= 100:
        raise ValueError("Invalid batch size")
    client = LLMServiceClient(task_name=task_name, request_type="memory.consolidation")

    def operation_for(target_chat: str, action: str, *args):
        worker = MemoryConsolidator(
            truth_path, ledger_path, source=source, account=account, chat_id=target_chat, backfill=backfill
        )
        try:
            return getattr(worker, action)(*args)
        finally:
            worker.close()

    while True:
        any_job = False
        target_chats = (
            await asyncio.to_thread(_discover_ledger_chat_ids, ledger_path, source, account)
            if chat_id == "*"
            else [chat_id]
        )
        for target_chat in target_chats:
            try:
                if once:
                    await asyncio.to_thread(operation_for, target_chat, "enqueue", batch_size)
                else:
                    await asyncio.to_thread(operation_for, target_chat, "enqueue", batch_size, 10, 300)
                job = await asyncio.to_thread(operation_for, target_chat, "claim")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if once:
                    raise
                logging.getLogger(__name__).error("Memory queue unavailable: %s", type(exc).__name__)
                continue
            if job:
                any_job = True
                try:
                    inputs = await asyncio.to_thread(operation_for, target_chat, "prepare", job)
                    if inputs is None:
                        result = "-- NONE"
                    else:
                        prompt = await asyncio.to_thread(operation_for, target_chat, "build_prompt", inputs)
                        response = await asyncio.wait_for(
                            client.generate_response(prompt),
                            timeout=240,
                        )
                        result = extract_table_sql(response.response)
                    await asyncio.to_thread(operation_for, target_chat, "commit", job, result)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    try:
                        await asyncio.to_thread(operation_for, target_chat, "fail", job, exc)
                    except Exception as persist_error:
                        logging.getLogger(__name__).error(
                            "Memory retry persistence unavailable: %s", type(persist_error).__name__
                        )
                        if once:
                            raise
        if once:
            return
        await asyncio.sleep(2 if any_job else 10)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Persistent source-bound memory consolidation using native LLM service"
    )
    parser.add_argument("--truth", default="data/companion_truth.db")
    parser.add_argument("--ledger", default="data/message_ledger.db")
    parser.add_argument("--source", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--chat", required=True)
    parser.add_argument("--model-task", required=True)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()
    asyncio.run(
        run_consolidation_service(
            truth_path=args.truth,
            ledger_path=args.ledger,
            source=args.source,
            account=args.account,
            chat_id=args.chat,
            task_name=args.model_task,
            backfill=args.backfill,
            once=args.once,
            batch_size=args.batch_size,
        )
    )


if __name__ == "__main__":
    main()
