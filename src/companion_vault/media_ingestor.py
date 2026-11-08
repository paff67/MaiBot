"""独立于屏幕轮询的 Root 媒体归档服务，可恢复游标和云端重试。"""

from typing import Any, Dict
import argparse
import hashlib
import json
import os
import time

import httpx

from .asset_pipeline import AssetPipeline
from .runtime_bridge import _open_truth_db


def collect_page(pipeline: AssetPipeline, http: httpx.Client, edge_url: str) -> Dict[str, Any]:
    source = edge_url.rstrip("/")
    identity_response = http.get(source + "/wechat/active_dir")
    identity_response.raise_for_status()
    account = identity_response.json()["path"].rstrip("/").rsplit("/", 1)[-1]
    cursor_source = source + "/" + account
    cursor = pipeline.db.conn.execute("SELECT * FROM ingestion_cursors WHERE source=?", (cursor_source,)).fetchone()
    params = {"mtime_ns": cursor["mtime_ns"] if cursor else 0, "after": cursor["after_key"] if cursor else "", "limit": 5}
    response = http.get(source + "/wechat/media_since", params=params)
    response.raise_for_status()
    page = response.json()
    if page["account"] != account:
        raise ValueError("扫描时账号切换，等待下一轮重新绑定游标")
    for item in page["items"]:
        identity = json.dumps([item["account"], item["key"], item["mtime_ns"], item["size"]], separators=(",", ":"))
        source_id = "wechat:" + hashlib.sha256(identity.encode()).hexdigest()
        exists = pipeline.db.conn.execute("SELECT 1 FROM asset_sources WHERE source_id=?", (source_id,)).fetchone()
        if exists is None:
            response = http.get(source + "/wechat/media_file", params={
                "account": item["account"], "kind": item["kind"], "path": item["relative_path"],
                "mtime_ns": item["mtime_ns"], "ctime_ns": item["ctime_ns"], "size": item["size"],
            })
            response.raise_for_status()
            data = response.content
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != response.headers.get("X-Content-SHA256"):
                raise ValueError("手机到 VPS 原件传输校验失败")
            pipeline.ingest(data, item, source_id=source_id)
        if item["mime_type"] == "audio/x-silk":
            derived_id = source_id + ":wav"
            if pipeline.db.conn.execute("SELECT 1 FROM asset_sources WHERE source_id=?", (derived_id,)).fetchone() is None:
                response = http.get(source + "/wechat/media_wav", params={
                    "account": item["account"], "kind": item["kind"], "path": item["relative_path"],
                    "mtime_ns": item["mtime_ns"], "ctime_ns": item["ctime_ns"], "size": item["size"],
                })
                response.raise_for_status()
                if hashlib.sha256(response.content).hexdigest() != response.headers.get("X-Content-SHA256"):
                    raise ValueError("WAV 无损解码文件校验失败")
                pipeline.ingest(response.content, dict(item, mime_type="audio/wav",
                    relative_path=item["relative_path"] + ".wav", representation="decoded_lossless", original_source_id=source_id),
                    source_id=derived_id, category="ai_generated")
        # 原件已 fsync + SQLite 提交后才推进；Drive 失败由独立 pending 队列重试。
        with pipeline.db.conn:
            pipeline.db.conn.execute(
                "INSERT INTO ingestion_cursors VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET "
                "mtime_ns=excluded.mtime_ns,after_key=excluded.after_key",
                (cursor_source, item["scan_ns"], item["key"]),
            )
    return {"archived": len(page["items"]), "has_more": page["has_more"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--mode", choices=("all", "collect", "observe", "sync"), default="all")
    parser.add_argument("--db-path", default="data/companion_truth.db")
    parser.add_argument("--edge-url", default=os.environ.get("MAIBOT_EDGE_URL", "http://100.87.39.37:8765"))
    args = parser.parse_args()
    if args.mode in {"all", "collect"}:
        parser.error("全目录自动回填已停用；媒体由微信实时桥接器按新气泡批次采集，维护任务可用 observe/sync")
    db = _open_truth_db(args.db_path)
    pipeline = AssetPipeline(db, spool=os.environ.get("MAIBOT_ASSET_SPOOL", "data/companion_assets"),
                             remote=os.environ.get("MAIBOT_DRIVE_REMOTE", ""))
    token = os.environ.get("MAIBOT_EDGE_TOKEN", "")
    try:
        with httpx.Client(timeout=120, headers={"Authorization": "Bearer " + token} if token else {}) as http:
            while True:
                try:
                    if args.mode in {"all", "collect"}:
                        result = collect_page(pipeline, http, args.edge_url)
                        print(json.dumps(result), flush=True)
                except Exception as exc:
                    if args.once:
                        raise
                    print(json.dumps({"error": str(exc)}), flush=True)
                try:
                    if args.mode in {"all", "observe"}:
                        response = http.get(args.edge_url.rstrip("/") + "/wechat/chat_snapshot")
                        response.raise_for_status()
                        snapshot = response.json()
                        # 观察归档不触发回复，不依赖 LLM 是否接话，也不依赖媒体匹配是否完成。
                        pipeline.archive_transcript(snapshot["account"] + "/" + snapshot["chat_id"], snapshot)
                        print(json.dumps({"observation_archived": True}), flush=True)
                except Exception as exc:
                    print(json.dumps({"observation_error": str(exc)}), flush=True)
                try:
                    if args.mode in {"all", "sync"}:
                        print(json.dumps({"drive_verified": pipeline.sync_pending(limit=5)}), flush=True)
                except Exception as exc:
                    if args.once:
                        raise
                    print(json.dumps({"drive_error": str(exc)}), flush=True)
                if args.once:
                    break
                time.sleep(5)
    finally:
        db.close()


if __name__ == "__main__":
    main()
