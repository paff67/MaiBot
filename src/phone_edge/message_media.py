"""Resolve an exact journal message to local files; never accept invented ownership."""

from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time

from .media_references import message_media_refs
from .message_journal import JOURNAL


class MessageMediaResolver:
    def __init__(self, store, journal: str = JOURNAL):
        self.store, self.journal = store, journal
        self.cache = {}
        self.lock = threading.Lock()

    def message(self, account, msg_id, revision):
        if not re.fullmatch(r"[a-f0-9]{32}", account) or not re.fullmatch(r"[0-9]+", msg_id):
            raise ValueError("Invalid message identity")
        with sqlite3.connect(Path(self.journal).as_uri() + "?mode=ro", uri=True, timeout=10) as db:
            head = db.execute(
                "SELECT revision FROM message_heads WHERE account=? AND msg_id=?", (account, msg_id)
            ).fetchone()
            if not head or head[0] != revision:
                raise ValueError("Message revision changed; reschedule from committed journal")
            row = db.execute(
                "SELECT payload FROM message_events WHERE account=? AND msg_id=? AND revision=? ORDER BY seq DESC LIMIT 1",
                (account, msg_id, revision),
            ).fetchone()
        if row is None:
            raise ValueError("Message event unavailable")
        return json.loads(row[0])

    def index(self, root):
        now = time.monotonic()
        with self.lock:
            cached = self.cache.get(str(root))
            if cached and now - cached[0] < 30:
                return cached[1]
            found = {}
            count = 0
            if root.exists():
                for directory, dirs, files in os.walk(root, followlinks=False):
                    dirs[:] = [d for d in dirs if not Path(directory, d).is_symlink()]
                    for name in files:
                        count += 1
                        if count > 100000:
                            raise ValueError("Media index limit exceeded; do not silently truncate")
                        p = Path(directory, name)
                        if p.is_symlink():
                            continue
                        found.setdefault(name, []).append(p.relative_to(root).as_posix())
            self.cache[str(root)] = (now, found)
            return found

    def resolve(self, account, msg_id, revision, ref_index):
        event = self.message(account, msg_id, revision)
        refs = message_media_refs(event["row"])
        if not 0 <= ref_index < len(refs):
            raise ValueError("Invalid media reference slot")
        ref = refs[ref_index]
        if ref.get("download_complete") is False:
            return dict(ok=True, state="waiting_file", chat_id=event["chat_id"], reference=ref)
        roots = self.store.roots(account)
        kinds = ["document", "attachment", "opendata"] if ref["kind"] == "document" else [ref["kind"]]
        candidates = []
        mismatches = 0
        for kind in kinds:
            root = roots[kind]
            for name in ref["names"]:
                if Path(name).name != name or name in (".", ".."):
                    continue
                exact = ref.get("exact_path", "")
                if exact.startswith("THUMBNAIL_DIRPATH://"):
                    exact = ""
                if exact.startswith("/sdcard/"):
                    exact = "/storage/emulated/0/" + exact[len("/sdcard/") :]
                if exact.startswith("/data/user/0/"):
                    exact = "/data/data/" + exact[len("/data/user/0/") :]
                if Path(exact).is_absolute():
                    expected_path = Path(exact).resolve()
                    relatives = (
                        [expected_path.relative_to(root).as_posix()] if expected_path.is_relative_to(root) else []
                    )
                else:
                    relatives = self.index(root).get(name, [])
                for rel in relatives:
                    path = root / rel
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        continue
                    try:
                        before = path.stat()
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(before.st_mode) or before.st_size == 0:
                        continue
                    if time.time_ns() - max(before.st_ctime_ns, before.st_mtime_ns) < self.store.settle_ns:
                        continue
                    if before.st_size > 64 * 1024 * 1024:
                        candidates.append(dict(kind=kind, relative_path=rel, state="oversize", size=before.st_size))
                        continue
                    wxgf_transcoded = False
                    if kind in ("emoji", "image"):
                        with path.open("rb") as test_f:
                            hdr = test_f.read(4)
                        if hdr.lower() == b"wxgf":
                            if ref.get("expected_size") and before.st_size != ref["expected_size"]:
                                mismatches += 1
                                continue
                            ext = ".png" if kind == "emoji" else ".decoded.jpg"
                            out_path = path.parent / (path.name + ext)
                            if not out_path.exists() or out_path.stat().st_size == 0:
                                try:
                                    import subprocess
                                    raw_bytes = path.read_bytes()
                                    vps_idx = raw_bytes.rfind(b"\x00\x00\x00\x01\x40")
                                    hevc_tmp = path.parent / (path.name + ".tmp.hevc")
                                    hevc_tmp.write_bytes(raw_bytes[vps_idx:] if vps_idx >= 0 else raw_bytes)
                                    try:
                                        env = os.environ.copy()
                                        env["LD_LIBRARY_PATH"] = "/data/data/com.termux/files/usr/lib"
                                        cmd = [
                                            "/data/data/com.termux/files/usr/bin/ffmpeg",
                                            "-y",
                                            "-f",
                                            "hevc",
                                            "-i",
                                            str(hevc_tmp),
                                            "-frames:v",
                                            "1",
                                            "-update",
                                            "1",
                                        ]
                                        if kind == "image":
                                            cmd.extend(["-q:v", "5"])
                                        cmd.append(str(out_path))
                                        subprocess.run(cmd, env=env, capture_output=True, timeout=10)
                                    finally:
                                        hevc_tmp.unlink(missing_ok=True)
                                except Exception:
                                    pass
                            if out_path.exists() and out_path.stat().st_size > 0:
                                path = out_path
                                rel = out_path.relative_to(root).as_posix()
                                before = path.stat()
                                wxgf_transcoded = True
                    mime, representation = self.store.describe(path, kind)
                    sha = hashlib.sha256()
                    md5 = hashlib.md5()
                    with path.open("rb") as source:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            sha.update(block)
                            md5.update(block)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        continue
                    expected = ref.get("expected_md5")
                    checksum_ok = not expected or md5.hexdigest() == expected or kind == "emoji" or wxgf_transcoded
                    opaque = mime == "application/octet-stream"
                    # Filename proves which stored object was referenced, not that ciphertext is decoded.
                    if not checksum_ok and not (kind == "emoji" and opaque):
                        mismatches += 1
                        continue
                    if ref.get("expected_size") and checksum_ok and not wxgf_transcoded and before.st_size != ref["expected_size"]:
                        mismatches += 1
                        continue
                    candidates.append(
                        dict(
                            account=account,
                            kind=kind,
                            relative_path=rel,
                            mtime_ns=before.st_mtime_ns,
                            ctime_ns=before.st_ctime_ns if os.name != "nt" else 0,
                            size=before.st_size,
                            mime_type=mime,
                            sha256=sha.hexdigest(),
                            md5=md5.hexdigest(),
                            expected_md5=expected,
                            checksum_verified=bool(expected and md5.hexdigest() == expected),
                            representation="thumbnail"
                            if ref["variant"] == "thumbnail"
                            else "medium"
                            if ref["variant"] == "medium"
                            else "opaque_original"
                            if opaque
                            else representation,
                            state="resolved",
                        )
                    )
        # Prefer checksum-valid plaintext, while opaque candidates remain retrievable originals.
        usable = [x for x in candidates if x["state"] == "resolved"]
        verified = [
            x
            for x in usable
            if (x["checksum_verified"] or x["kind"] == "emoji" or not x["expected_md5"])
            and x["mime_type"] != "application/octet-stream"
        ]
        if verified:
            usable = verified
        if len({x["sha256"] for x in usable}) > 1:
            return dict(ok=True, state="ambiguous", candidates=len(usable), chat_id=event["chat_id"])
        if usable:
            selected = sorted(usable, key=lambda x: x["relative_path"])[0]
            self.message(account, msg_id, revision)
            return dict(ok=True, state="resolved", chat_id=event["chat_id"], reference=ref, file=selected)
        return dict(
            ok=True,
            state="checksum_mismatch" if mismatches else "oversize" if candidates else "waiting_file",
            chat_id=event["chat_id"],
            reference=ref,
        )
