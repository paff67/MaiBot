"""Message-driven asset jobs. Identity, byte integrity and cloud status stay distinct."""

import argparse
import hashlib
import io
import json
import os
import sqlite3
import time
import wave

import httpx

from src.phone_edge.media_references import message_media_refs

from .asset_pipeline import AssetPipeline
from .truth_db import CompanionTruthDB


class MessageMediaQueue:
    def __init__(self, assets: AssetPipeline, ledger: sqlite3.Connection, edge: str, client: httpx.Client):
        self.assets, self.db, self.ledger, self.edge, self.client = (
            assets,
            assets.db.conn,
            ledger,
            edge.rstrip("/"),
            client,
        )
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS media_job_cursor(name TEXT PRIMARY KEY,seq INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS message_media_jobs(
          job_id TEXT PRIMARY KEY, source TEXT NOT NULL,account TEXT NOT NULL,msg_id TEXT NOT NULL,
          revision TEXT NOT NULL,chat_id TEXT NOT NULL,ref_index INTEGER NOT NULL,reference TEXT NOT NULL,
          state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
          lease_until REAL NOT NULL DEFAULT 0,error TEXT,updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS message_asset_links(
          job_id TEXT NOT NULL, file_code TEXT NOT NULL,representation TEXT NOT NULL,
          account TEXT NOT NULL,msg_id TEXT NOT NULL,revision TEXT NOT NULL,chat_id TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1,evidence TEXT NOT NULL,PRIMARY KEY(job_id,file_code));
        CREATE INDEX IF NOT EXISTS media_job_due ON message_media_jobs(state,next_attempt,lease_until);
        CREATE INDEX IF NOT EXISTS media_job_message ON message_media_jobs(source,account,msg_id,revision);
        CREATE INDEX IF NOT EXISTS asset_message ON message_asset_links(account,msg_id,active);
        """)

    def enqueue(self, limit=500):
        name = "message_revisions_v2"
        row = self.db.execute("SELECT seq FROM media_job_cursor WHERE name=?", (name,)).fetchone()
        cursor = row[0] if row else 0
        events = self.ledger.execute(
            "SELECT rowid,source,payload FROM message_revisions WHERE rowid>? ORDER BY rowid LIMIT ?", (cursor, limit)
        ).fetchall()
        with self.db:
            for _rowid, source, payload in events:
                event = json.loads(payload)
                account = event["account"]
                mid = event["msg_id"]
                revision = event["revision"]
                chat = event["chat_id"]
                if not self.current(dict(source=source, account=account, msg_id=mid, revision=revision, chat_id=chat)):
                    continue
                self.db.execute(
                    "UPDATE message_media_jobs SET state='superseded' WHERE source=? AND account=? AND msg_id=? AND revision!=?",
                    (source, account, mid, revision),
                )
                self.db.execute(
                    "UPDATE message_asset_links SET active=0 WHERE job_id IN (SELECT job_id FROM message_media_jobs WHERE source=? AND account=? AND msg_id=? AND revision!=?)",
                    (source, account, mid, revision),
                )
                try:
                    refs = message_media_refs(event["row"])
                    error = None
                except (ValueError, TypeError) as exc:
                    refs = [dict(kind="unresolved", variant="unknown")]
                    error = type(exc).__name__ + ": " + str(exc)
                for index, ref in enumerate(refs):
                    job_id = hashlib.sha256(json.dumps([source, account, mid, revision, index]).encode()).hexdigest()
                    self.db.execute(
                        "INSERT INTO message_media_jobs(job_id,source,account,msg_id,revision,chat_id,ref_index,reference,state,error,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(job_id) DO UPDATE SET reference=excluded.reference,state=excluded.state,error=excluded.error,next_attempt=0,lease_until=0 "
                        "WHERE message_media_jobs.reference!=excluded.reference OR message_media_jobs.state IN ('needs_metadata','superseded')",
                        (
                            job_id,
                            source,
                            account,
                            mid,
                            revision,
                            chat,
                            index,
                            json.dumps(ref, ensure_ascii=False),
                            "needs_metadata" if error else "pending",
                            error,
                            time.time(),
                        ),
                    )
            if events:
                self.db.execute(
                    "INSERT INTO media_job_cursor VALUES(?,?) ON CONFLICT(name) DO UPDATE SET seq=excluded.seq",
                    (name, events[-1][0]),
                )
        return len(events)

    def current(self, job):
        row = self.ledger.execute(
            "SELECT revision,chat_id FROM messages WHERE source=? AND account=? AND msg_id=?",
            (job["source"], job["account"], job["msg_id"]),
        ).fetchone()
        return row is not None and tuple(row) == (job["revision"], job["chat_id"])

    def claim(self):
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM message_media_jobs WHERE state IN ('pending','retry','waiting_file','running','checksum_mismatch','ambiguous','decode_required') AND next_attempt<=? AND lease_until<=? ORDER BY CAST(msg_id AS INTEGER) DESC,ref_index LIMIT 1",
                (now, now),
            ).fetchone()
            if not row:
                return None
            job = dict(row)
            self.db.execute(
                "UPDATE message_media_jobs SET state='running',attempts=attempts+1,lease_until=?,updated_at=? WHERE job_id=?",
                (now + 600, now, job["job_id"]),
            )
        return job

    def finish(self, job, state, error=None, delay=0):
        with self.db:
            self.db.execute(
                "UPDATE message_media_jobs SET state=?,error=?,next_attempt=?,lease_until=0,updated_at=? WHERE job_id=?",
                (state, error, time.time() + delay, time.time(), job["job_id"]),
            )

    def link(self, job, asset, representation, evidence):
        if not self.current(job):
            raise ValueError("Message revision superseded during acquisition")
        with self.db:
            self.db.execute(
                "UPDATE message_asset_links SET active=0 WHERE job_id=? AND file_code!=?",
                (job["job_id"], asset["file_code"]),
            )
            self.db.execute(
                "INSERT INTO message_asset_links VALUES(?,?,?,?,?,?,?,1,?) ON CONFLICT(job_id,file_code) DO UPDATE SET active=1,representation=excluded.representation,evidence=excluded.evidence",
                (
                    job["job_id"],
                    asset["file_code"],
                    representation,
                    job["account"],
                    job["msg_id"],
                    job["revision"],
                    job["chat_id"],
                    json.dumps(evidence, ensure_ascii=False),
                ),
            )

    def run_one(self):
        job = self.claim()
        if not job:
            return False
        try:
            if not self.current(job):
                self.finish(job, "superseded")
                return True
            if job["source"].rstrip("/") != self.edge:
                raise ValueError("Job source does not match configured phone endpoint")
            ref = json.loads(job["reference"])
            peers = self.db.execute(
                "SELECT reference FROM message_media_jobs WHERE source=? AND account=? AND msg_id=? AND revision=? AND state='ready' AND job_id!=?",
                (job["source"], job["account"], job["msg_id"], job["revision"], job["job_id"]),
            ).fetchall()
            if ref.get("variant") == "original" and any(
                (other := json.loads(peer[0])).get("kind") == ref.get("kind") and other.get("variant") == "original"
                for peer in peers
            ):
                self.finish(job, "satisfied_by_peer")
                return True
            params = {k: job[k] for k in ("account", "msg_id", "revision", "ref_index")}
            response = self.client.get(self.edge + "/messages/media_resolve", params=params)
            response.raise_for_status()
            result = response.json()
            if result["chat_id"] != job["chat_id"]:
                raise ValueError("Resolver conversation mismatch")
            if result["state"] != "resolved":
                state = result["state"]
                self.finish(
                    job,
                    state if state in {"waiting_file", "ambiguous", "checksum_mismatch", "oversize"} else "retry",
                    delay=min(3600, 30 * 2 ** min(job["attempts"], 7)),
                )
                return True
            file = result["file"]
            ref = json.loads(job["reference"])
            if result.get("reference") is not None and result["reference"] != ref:
                ref = result["reference"]
                with self.db:
                    self.db.execute("UPDATE message_media_jobs SET reference=? WHERE job_id=?", (json.dumps(ref, ensure_ascii=False), job["job_id"]))
            if file["account"] != job["account"]:
                raise ValueError("Resolver account mismatch")
            read_params = dict(
                account=file["account"],
                kind=file["kind"],
                path=file["relative_path"],
                mtime_ns=file["mtime_ns"],
                ctime_ns=file["ctime_ns"],
                size=file["size"],
            )
            response = self.client.get(self.edge + "/wechat/media_file", params=read_params)
            response.raise_for_status()
            data = response.content
            digest = hashlib.sha256(data).hexdigest()
            if (
                len(data) != file["size"]
                or digest != file["sha256"]
                or digest != response.headers.get("X-Content-SHA256")
            ):
                raise ValueError("Original bytes failed transfer SHA256/size checks")
            if file["checksum_verified"] and hashlib.md5(data).hexdigest() != ref["expected_md5"]:
                raise ValueError("Message payload checksum mismatch")
            if not self.current(job):
                raise ValueError("Message superseded before archival")
            metadata = dict(
                file,
                msg_id=job["msg_id"],
                msg_revision=job["revision"],
                chat_id=job["chat_id"],
                evidence="message_reference_and_file_identity",
                variant=ref["variant"],
            )
            # Stable across metadata revisions and alternate paths; message identity remains distinct.
            identity = [job["source"], job["account"], job["msg_id"], file["representation"], digest]
            source_id = "message-media-v2:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            previous = self.db.execute(
                "SELECT a.source_id FROM message_asset_links l JOIN asset_sources a USING(file_code) JOIN message_media_jobs j USING(job_id) "
                "WHERE j.source=? AND l.account=? AND l.msg_id=? AND l.representation=? AND a.sha256=? LIMIT 1",
                (job["source"], job["account"], job["msg_id"], file["representation"], digest),
            ).fetchone()
            if previous:
                source_id = previous[0]
            asset = self.assets.ingest(
                data, metadata, source_id=source_id, chat_id=job["account"] + "/" + job["chat_id"]
            )
            self.link(job, asset, file["representation"], metadata)
            state = "ready"
            if file["representation"] == "opaque_original":
                state = "decode_required"
            elif file["representation"] == "thumbnail":
                state = "thumbnail_only"
            elif file["representation"] == "medium":
                state = "medium_only"
            elif file["mime_type"] == "audio/x-silk":
                response = self.client.get(self.edge + "/wechat/media_wav", params=read_params)
                response.raise_for_status()
                wav = response.content
                if hashlib.sha256(wav).hexdigest() != response.headers.get("X-Content-SHA256"):
                    raise ValueError("WAV checksum mismatch")
                with wave.open(io.BytesIO(wav), "rb") as decoded:
                    if decoded.getcomptype() != "NONE":
                        raise ValueError("Decoded audio must be uncompressed PCM")
                    properties = dict(
                        channels=decoded.getnchannels(),
                        sample_rate=decoded.getframerate(),
                        sample_width=decoded.getsampwidth(),
                    )
                derivative = self.assets.ingest(
                    wav,
                    dict(
                        metadata,
                        mime_type="audio/wav",
                        size=len(wav),
                        sha256=hashlib.sha256(wav).hexdigest(),
                        md5=hashlib.md5(wav).hexdigest(),
                        expected_md5=None,
                        checksum_verified=False,
                        source_sha256=digest,
                        representation="decoded_lossless",
                        original_file_code=asset["file_code"],
                        **properties,
                    ),
                    source_id=source_id + ":wav",
                    category="ai_generated",
                    chat_id=job["account"] + "/" + job["chat_id"],
                )
                self.link(job, derivative, "decoded_lossless", properties)
            if not self.current(job):
                raise ValueError("Message superseded before completion")
            self.finish(job, state, delay=3600 if state == "decode_required" else 0)
        except Exception as exc:
            if not self.current(job):
                self.finish(job, "superseded")
                with self.db:
                    self.db.execute("UPDATE message_asset_links SET active=0 WHERE job_id=?", (job["job_id"],))
            else:
                # Never log URLs, crypto material or raw message XML.
                self.finish(
                    job,
                    "retry",
                    type(exc).__name__ + ": " + str(exc).split(" for url ")[0][:300],
                    min(3600, 30 * 2 ** min(job["attempts"], 7)),
                )
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    ledger = sqlite3.connect("file:data/message_ledger.db?mode=ro", uri=True, timeout=30)
    db = CompanionTruthDB("data/companion_truth.db")
    assets = AssetPipeline(
        db, os.environ.get("MAIBOT_ASSET_SPOOL", "data/companion_assets"), os.environ.get("MAIBOT_DRIVE_REMOTE", "")
    )
    edge = os.environ.get("MAIBOT_EDGE_URL", "http://100.87.39.37:8765")
    token = os.environ.get("MAIBOT_EDGE_TOKEN", "")
    with httpx.Client(timeout=120, headers={"Authorization": "Bearer " + token} if token else {}) as client:
        worker = MessageMediaQueue(assets, ledger, edge, client)
        while True:
            try:
                scanned = worker.enqueue()
                # Catch the indexing cursor up before spending time on historical files.
                # Live arrivals are therefore scheduled ahead of bulk backfill.
                worked = worker.run_one() if scanned < 500 else False
                if scanned or worked:
                    print(json.dumps(dict(indexed_revisions=scanned, processed=worked)), flush=True)
            except Exception as exc:
                print(json.dumps(dict(error=type(exc).__name__)), flush=True)
                if args.once:
                    raise
            if args.once:
                break
            time.sleep(0.1 if scanned == 500 else 0.5)
    ledger.close()
    db.close()


if __name__ == "__main__":
    main()
