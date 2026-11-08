"""原件先落 VPS，再确认 Drive 副本；来源不明的媒体保持 PRIVATE。"""

from pathlib import Path
from typing import Any, Dict, List, Optional
import hashlib
import json
import mimetypes
import os
import subprocess
import tempfile
import time

from .truth_db import CompanionTruthDB


class AssetPipeline:
    def __init__(self, db: CompanionTruthDB, spool: str = "data/companion_assets",
                 remote: str = "", mount: str = "/mnt/gdrive/companion_vault") -> None:
        self.db = db
        self.spool = Path(spool).resolve()
        self.spool.mkdir(parents=True, exist_ok=True)
        self.remote = remote.rstrip("/")
        self.mount = Path(mount)
        self.db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS asset_sources (
          source_id TEXT PRIMARY KEY, file_code TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL,
          metadata TEXT NOT NULL, local_path TEXT NOT NULL, relative_path TEXT NOT NULL,
          drive_status TEXT NOT NULL DEFAULT 'pending', drive_id TEXT, drive_url TEXT,
          chat_id TEXT, association TEXT NOT NULL DEFAULT 'unresolved');
        CREATE TABLE IF NOT EXISTS ingestion_cursors (
          source TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, after_key TEXT NOT NULL);
        """)

    def ingest(self, data: bytes, metadata: Dict[str, Any], *, source_id: str,
               category: str = "user_uploads", chat_id: Optional[str] = None) -> Dict[str, Any]:
        digest = hashlib.sha256(data).hexdigest()
        existing = self.db.conn.execute("SELECT * FROM asset_sources WHERE source_id=?", (source_id,)).fetchone()
        if existing is not None:
            if existing["sha256"] != digest:
                raise ValueError("同一来源版本的 SHA256 发生变化")
            return dict(existing)
        if category not in {"user_uploads", "phone_captures", "ai_generated", "workspace_docs"}:
            raise ValueError("未知归档分类")
        mime = metadata.get("mime_type", "application/octet-stream")
        suffix = {"image/jpeg": ".jpg", "audio/x-silk": ".silk"}.get(mime) or mimetypes.guess_extension(mime) or Path(metadata.get("relative_path", "")).suffix or ".bin"
        if not suffix[1:].isalnum() or len(suffix) > 12:
            suffix = ".bin"
        scope = hashlib.sha256(chat_id.encode()).hexdigest()[:24] if chat_id else "unassigned"
        relative = f"{category}/{scope}/{time.strftime('%Y-%m')}/{digest}{suffix}"
        local = self.spool / relative
        local.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=local.parent, prefix=".asset-", delete=False) as target:
            temporary = Path(target.name)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, local)
        with self.db.conn:
            self.db.conn.execute("BEGIN IMMEDIATE")
            existing = self.db.conn.execute("SELECT * FROM asset_sources WHERE source_id=?", (source_id,)).fetchone()
            if existing is not None:
                if existing["sha256"] != digest:
                    raise ValueError("并发归档的来源版本摘要不一致")
                return dict(existing)
            code = self.db.next_code("FM", "media_vault", "file_code")
            self.db.conn.execute(
                "INSERT INTO media_vault(file_code,semantic_name,mime_type,source_type,gdrive_path,"
                "chat_context,distinctive_features,extracted_entities,trigger_when,visibility,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'PRIVATE',datetime('now'))",
                (code, Path(metadata.get("relative_path", "transcript.json")).name, mime, category, str(local),
                 json.dumps({"chat_id": chat_id, "association": "verified" if chat_id else "unresolved"}, ensure_ascii=False),
                 f"SHA256:{digest}; {metadata.get('representation', 'original')}; "
                 f"{'微信表情包; ' if metadata.get('kind') == 'emoji' else ''}{len(data)} bytes",
                 "[]", "按文件编号、原文件名、表情包或已核实会话召回"),
            )
            self.db.conn.execute(
                "INSERT INTO asset_sources(source_id,file_code,sha256,metadata,local_path,relative_path,chat_id,association) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (source_id, code, digest, json.dumps(metadata, ensure_ascii=False), str(local), relative, chat_id,
                 "verified" if chat_id else "unresolved"),
            )
        return dict(self.db.conn.execute("SELECT * FROM asset_sources WHERE source_id=?", (source_id,)).fetchone())

    def sync_pending(self, limit: int = 20, file_codes: Optional[List[str]] = None) -> int:
        if not self.remote:
            return 0
        if file_codes is not None:
            if not file_codes:
                return 0
            marks = ','.join('?' for _ in file_codes)
            rows = self.db.conn.execute(
                f"SELECT * FROM asset_sources WHERE drive_status!='verified' AND file_code IN ({marks}) LIMIT ?",
                (*file_codes, limit),
            ).fetchall()
        else:
            rows = self.db.conn.execute("SELECT * FROM asset_sources WHERE drive_status!='verified' LIMIT ?", (limit,)).fetchall()
        for row in rows:
            destination = self.remote + "/" + row["relative_path"]
            subprocess.run(["rclone", "copyto", row["local_path"], destination, "--checksum", "--retries", "3"],
                           check=True, capture_output=True, timeout=300)
            response = subprocess.run(["rclone", "lsjson", destination, "--stat", "--hash"],
                                      check=True, capture_output=True, text=True, timeout=60)
            info = json.loads(response.stdout)
            local = Path(row["local_path"])
            md5 = hashlib.md5(local.read_bytes()).hexdigest()
            if info["Size"] != local.stat().st_size or info.get("Hashes", {}).get("md5") != md5:
                raise ValueError("Google Drive 大小/MD5 校验失败，保留待同步状态")
            file_id = info.get("ID", "")
            url = f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""
            with self.db.conn:
                self.db.conn.execute("UPDATE asset_sources SET drive_status='verified',drive_id=?,drive_url=? WHERE source_id=?",
                                     (file_id, url, row["source_id"]))
                # 私有 Drive 链接供人追溯；模型使用真实 bytes 或 Files API URI。
                self.db.conn.execute("UPDATE media_vault SET gdrive_path=? WHERE file_code=?",
                                     (str(self.mount / row["relative_path"]), row["file_code"]))
        return len(rows)

    def archive_transcript(self, chat_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return self.ingest(data, {"relative_path": "transcript.json", "mime_type": "application/json"},
                           source_id="transcript:" + hashlib.sha256(data).hexdigest(),
                           category="workspace_docs", chat_id=chat_id)

    def attachable_codes(self, chat_id: str, *, include_private: bool = False, limit: int = 12) -> List[str]:
        visibility = "" if include_private else " AND m.visibility='PUBLIC'"
        return [row[0] for row in self.db.conn.execute(
            "SELECT a.file_code FROM asset_sources a JOIN media_vault m USING(file_code) "
            "WHERE a.chat_id=? AND a.association='verified' AND m.source_type='user_uploads'" + visibility +
            " ORDER BY a.rowid DESC LIMIT ?", (chat_id, limit))]

    def bind_conversation(self, source_id: str, account: str, chat_id: str, evidence: str) -> None:
        if not account or not chat_id or not evidence.strip():
            raise ValueError("绑定必须提供账号、真实 Chat_User 和关联证据")
        row = self.db.conn.execute("SELECT * FROM asset_sources WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            raise KeyError(source_id)
        metadata = json.loads(row["metadata"])
        if metadata.get("account") != account:
            raise ValueError("拒绝跨账号绑定文件")
        key = account + "/" + chat_id
        if row["chat_id"] and row["chat_id"] != key:
            raise ValueError("文件已绑定其他会话，不允许静默改写")
        metadata["association_evidence"] = evidence
        with self.db.conn:
            self.db.conn.execute("UPDATE asset_sources SET chat_id=?,association='verified',metadata=? WHERE source_id=?",
                                 (key, json.dumps(metadata, ensure_ascii=False), source_id))
            self.db.conn.execute("UPDATE media_vault SET chat_context=? WHERE file_code=?",
                                 (json.dumps({"chat_id": key, "evidence": evidence}, ensure_ascii=False), row["file_code"]))

    def scoped_file_codes(self, conversation_key: str, codes: List[str]) -> List[str]:
        """会话自己的 PRIVATE 文件只在同一会话内开放；不能放宽全局 PUBLIC 条件。"""
        result = []
        for code in dict.fromkeys(codes):
            row = self.db.conn.execute(
                "SELECT file_code FROM asset_sources WHERE file_code=? AND chat_id=? AND association='verified'",
                (code, conversation_key),
            ).fetchone()
            if row:
                if self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='message_asset_links' AND type='table'").fetchone():
                    linked = self.db.conn.execute('SELECT 1 FROM message_asset_links WHERE file_code=?',(code,)).fetchone()
                    allowed = self.db.conn.execute(
                        "SELECT 1 FROM message_asset_links l JOIN message_media_jobs j USING(job_id) "
                        "WHERE l.file_code=? AND l.active=1 AND j.state='ready' LIMIT 1", (code,),
                    ).fetchone()
                    if linked and not allowed:
                        continue
                result.append(code)
        return result

    def message_asset_catalog(self, conversation_key: str, limit: int = 40) -> str:
        if not self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='message_asset_links' AND type='table'").fetchone():
            return ''
        account, separator, chat = conversation_key.partition('/')
        if not separator:
            return ''
        rows = self.db.conn.execute(
            "SELECT l.msg_id,l.file_code,m.semantic_name,m.mime_type,l.representation "
            "FROM message_asset_links l JOIN message_media_jobs j USING(job_id) JOIN media_vault m USING(file_code) "
            "WHERE l.account=? AND l.chat_id=? AND l.active=1 AND j.state='ready' "
            "ORDER BY CAST(l.msg_id AS INTEGER) DESC,l.file_code LIMIT ?", (account,chat,limit),
        ).fetchall()
        return '<本会话已验证媒体>\n'+json.dumps([dict(r) for r in rows],ensure_ascii=False)+'\n</本会话已验证媒体>'

    def original_path(self, file_code: str) -> Path:
        row = self.db.conn.execute("SELECT local_path,sha256 FROM asset_sources WHERE file_code=?", (file_code,)).fetchone()
        if row is not None and Path(row["local_path"]).is_file():
            path = Path(row["local_path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("VPS 原件校验失败")
            return path
        asset = self.db.conn.execute("SELECT gdrive_path FROM media_vault WHERE file_code=?", (file_code,)).fetchone()
        if asset is None:
            raise KeyError(file_code)
        path = Path(asset[0])
        if not path.is_file():
            raise FileNotFoundError(path)
        return path
