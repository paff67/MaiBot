"""有界且持久化的新消息媒体批次；无路径证据时 pending，不上传历史目录。"""

from typing import Any, Dict, List, Sequence, Tuple
import hashlib
import json

from src.companion_vault.asset_pipeline import AssetPipeline

from .media_correlation import correlate_media_batch
from .wechat_screen_parser import WeChatBubble


class MediaBatchCollector:
    def __init__(self, assets: AssetPipeline, http: Any, edge_url: str) -> None:
        self.assets, self.http, self.edge_url = assets, http, edge_url.rstrip("/")
        with assets.db.conn:
            assets.db.conn.execute("CREATE TABLE IF NOT EXISTS media_batches ("
                "batch_id TEXT PRIMARY KEY, conversation_key TEXT NOT NULL, lower_ns INTEGER NOT NULL, "
                "upper_ns INTEGER NOT NULL, status TEXT NOT NULL, codes TEXT NOT NULL DEFAULT '[]', "
                "candidates TEXT NOT NULL DEFAULT '[]')")

    def collect(self, bubbles: Sequence[WeChatBubble], snapshot: Dict[str, Any], lower_ns: int) -> Tuple[List[str], str]:
        media = [b for b in bubbles if b.role == "OTHER" and b.msg_type in {"image", "voice", "file"}]
        if not media:
            return [], "no_new_media"
        key = snapshot["account"] + "/" + snapshot["chat_id"]
        upper_ns = snapshot["captured_at_ns"]
        batch_id = hashlib.sha256(json.dumps([key, [b.fingerprint for b in media]]).encode()).hexdigest()
        existing = self.assets.db.conn.execute("SELECT * FROM media_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if existing and existing["status"] == "verified":
            return json.loads(existing["codes"]), "verified"
        if lower_ns <= 0 or lower_ns > upper_ns:
            return [], "baseline_required"
        with self.assets.db.conn:
            self.assets.db.conn.execute("INSERT OR IGNORE INTO media_batches(batch_id,conversation_key,lower_ns,upper_ns,status) "
                                       "VALUES(?,?,?,?,'pending')", (batch_id, key, lower_ns, upper_ns))
        if existing:
            lower_ns, upper_ns = existing["lower_ns"], existing["upper_ns"]
        references = snapshot.get("media_reference_paths", [])
        items = []
        cursor, after = lower_ns, ""
        for _ in range(5):
            response = self.http.get(self.edge_url + "/wechat/media_since", params={
                "mtime_ns": cursor, "after": after, "until_ns": upper_ns, "limit": 100,
            })
            response.raise_for_status()
            page = response.json()
            if page.get("cursor_version") != 2 or page["account"] != snapshot["account"]:
                raise ValueError("手机媒体协议或账号不匹配")
            items.extend(item for item in page["items"] if lower_ns <= item["scan_ns"] <= upper_ns)
            if not page["has_more"]:
                break
            next_cursor = page["next_cursor"]
            if (next_cursor["mtime_ns"], next_cursor["after"]) <= (cursor, after):
                raise ValueError("媒体分页游标未前进")
            cursor, after = next_cursor["mtime_ns"], next_cursor["after"]
        else:
            return [], "pending_page_limit"
        selected = correlate_media_batch(media, items, references)
        with self.assets.db.conn:
            self.assets.db.conn.execute("UPDATE media_batches SET candidates=? WHERE batch_id=?",
                                       (json.dumps(items, ensure_ascii=False), batch_id))
        if not selected:
            return [], "pending_file_write" if references else "pending_association_evidence"
        self._check_identity(key)
        records = []
        for item in selected:
            source_id = "wechat:" + hashlib.sha256(json.dumps(
                [item["account"], item["key"], item["mtime_ns"], item["size"]], separators=(",", ":")
            ).encode()).hexdigest()
            params = {"account": item["account"], "kind": item["kind"], "path": item["relative_path"],
                      "mtime_ns": item["mtime_ns"], "ctime_ns": item["ctime_ns"], "size": item["size"]}
            row = self.assets.db.conn.execute("SELECT * FROM asset_sources WHERE source_id=?", (source_id,)).fetchone()
            if row is None:
                response = self.http.get(self.edge_url + "/wechat/media_file", params=params)
                response.raise_for_status()
                data = response.content
                if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != response.headers.get("X-Content-SHA256"):
                    raise ValueError("原件传输校验失败")
                row = self.assets.ingest(data, item, source_id=source_id)
            if item["mime_type"] == "audio/x-silk":
                derived_id = source_id + ":wav"
                if not self.assets.db.conn.execute("SELECT 1 FROM asset_sources WHERE source_id=?", (derived_id,)).fetchone():
                    response = self.http.get(self.edge_url + "/wechat/media_wav", params=params)
                    response.raise_for_status()
                    if hashlib.sha256(response.content).hexdigest() != response.headers.get("X-Content-SHA256"):
                        raise ValueError("WAV 无损解码派生校验失败")
                    self.assets.ingest(response.content, dict(item, mime_type="audio/wav", representation="decoded_lossless",
                        original_source_id=source_id), source_id=derived_id, category="ai_generated")
            records.append((source_id, row["file_code"]))
        self._check_identity(key)
        evidence = json.dumps({"source": "root_activity_exact_path", "paths": references,
                               "account": snapshot["account"], "chat_id": snapshot["chat_id"]})
        for source_id, _ in records:
            self.assets.bind_conversation(source_id, snapshot["account"], snapshot["chat_id"], evidence)
        codes = [code for _, code in records]
        with self.assets.db.conn:
            self.assets.db.conn.execute("UPDATE media_batches SET status='verified',codes=? WHERE batch_id=?",
                                       (json.dumps(codes), batch_id))
        return codes, "verified"

    def _check_identity(self, expected: str) -> None:
        response = self.http.get(self.edge_url + "/wechat/chat_identity")
        response.raise_for_status()
        current = response.json()
        if current["account"] + "/" + current["chat_id"] != expected:
            raise ValueError("媒体读取期间会话改变，拒绝绑定和入模")
