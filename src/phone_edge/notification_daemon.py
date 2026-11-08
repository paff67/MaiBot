"""Supervise zero-APK Binder listener; callbacks commit directly to SQLite."""

from pathlib import Path
import fcntl
import os
import subprocess
import time


ROOT = Path("/data/adb/maibot_edge")


def main() -> None:
    (ROOT / "run").mkdir(exist_ok=True)
    with (ROOT / "run/notification-daemon.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        Path(f"/proc/{os.getpid()}/oom_score_adj").write_text("-1000")
        env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
        env.update(PATH="/system/bin:/system/xbin", CLASSPATH=str(ROOT / "bin/NotificationTap.dex"))
        while True:
            # Rotate diagnostic output; events are in the persistent DB, never in this log.
            logfile = ROOT / "run/notification-listener.log"
            if logfile.exists() and logfile.stat().st_size > 2_000_000:
                logfile.replace(logfile.with_suffix(".previous.log"))
            with logfile.open("ab") as output:
                child = subprocess.Popen(["/system/bin/app_process", "/system/bin", "NotificationTap",
                                          str(ROOT / "state/notifications.db")], env=env,
                                         stdin=subprocess.DEVNULL, stdout=output, stderr=output)
                (ROOT / "run/notification-listener.pid").write_text(str(child.pid))
                try:
                    Path(f"/proc/{child.pid}/oom_score_adj").write_text("-1000")
                except FileNotFoundError:
                    pass
                child.wait()
            time.sleep(5)


if __name__ == "__main__":
    main()
