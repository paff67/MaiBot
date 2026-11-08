"""Supervise local Frida injection; never expose a Frida TCP server."""

from pathlib import Path
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import time

ROOT = Path("/data/adb/maibot_edge")
LIB_HASH = "2ff2b9dfa31da533c0265ccf9d79fe392c4f6b019b0783693d087934e5e65353"
DEX_HASH = "d0fbb514273f380a9a9579be44d46f504366c2f623e862188f30c6083caa9a80"


def main() -> None:
    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    with (ROOT / "run/message-daemon.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        Path(f"/proc/{os.getpid()}/oom_score_adj").write_text("-1000")
        # A SIGKILL of the Python supervisor must not leave a second injector alive.
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                args = (proc / "cmdline").read_bytes().split(bytes([0]))
                if (
                    str(ROOT / "bin/frida-inject").encode() in args
                    and str(ROOT / "message_capture.js").encode() in args
                ):
                    os.kill(int(proc.name), signal.SIGTERM)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        time.sleep(2)
        child = None
        target = None
        attached_at = 0.0
        try:
            while True:
                if not (ROOT / "state/message_capture.enabled").exists():
                    break
                result = subprocess.run(["/system/bin/pidof", "com.tencent.mm"], capture_output=True, text=True)
                pid = result.stdout.strip().split()[0] if result.stdout.strip() else None
                if child and child.poll() is None and time.time() - attached_at > 90:
                    try:
                        with sqlite3.connect(
                            "file:/data/user/0/com.tencent.mm/files/maibot_message_journal.db?mode=ro",
                            uri=True,
                            timeout=5,
                        ) as db:
                            heartbeat = db.execute("SELECT value FROM journal_meta WHERE key='last_scan_ms'").fetchone()
                        if not heartbeat or time.time() * 1000 - int(heartbeat[0]) > 90_000:
                            print(json.dumps({"event": "stale_scanner_restart"}), flush=True)
                            child.terminate()
                            child.wait(timeout=15)
                    except sqlite3.Error as exc:
                        print(json.dumps({"event": "journal_unavailable", "error": str(exc)}), flush=True)
                if child and (child.poll() is not None or pid != target):
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=15)
                    child = None
                if pid and child is None:
                    maps = Path(f"/proc/{pid}/maps").read_text()
                    libs = {line.split()[-1] for line in maps.splitlines() if line.endswith("/libWCDB.so")}
                    if len(libs) != 1 or hashlib.sha256(Path(next(iter(libs))).read_bytes()).hexdigest() != LIB_HASH:
                        print(json.dumps({"event": "unsupported_wcdb", "pid": pid}), flush=True)
                        time.sleep(30)
                        continue
                    lib = Path(next(iter(libs)))
                    dex = lib.parents[3] / "dex/tinker_classN.apk"
                    if not dex.is_file() or hashlib.sha256(dex.read_bytes()).hexdigest() != DEX_HASH:
                        print(json.dumps({"event": "unsupported_dex", "pid": pid}), flush=True)
                        time.sleep(30)
                        continue
                    log = ROOT / "run/message-capture.log"
                    if log.exists() and log.stat().st_size > 4_000_000:
                        log.replace(log.with_suffix(".previous.log"))
                    with log.open("ab") as output:
                        env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
                        child = subprocess.Popen(
                            [str(ROOT / "bin/frida-inject"), "-p", pid, "-s", str(ROOT / "message_capture.js")],
                            env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=output,
                            stderr=output,
                        )
                    target = pid
                    attached_at = time.time()
                    (ROOT / "run/message-injector.pid").write_text(str(child.pid))
                    print(json.dumps({"event": "attached", "wechat_pid": pid, "injector_pid": child.pid}), flush=True)
                time.sleep(5)
        finally:
            if child and child.poll() is None:
                child.terminate()
                child.wait(timeout=15)


if __name__ == "__main__":
    main()
