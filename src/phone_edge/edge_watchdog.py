"""Root HTTP 进程看门狗；独占锁防止重复运行，每五秒检查退出状态。"""

from pathlib import Path
from typing import Optional
import fcntl
import logging
import os
import subprocess
import sys
import time


ROOT = Path("/data/adb/maibot_edge")


def find_edge_pid() -> Optional[int]:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(bytes([0]))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if b"-m" in args and b"maibot_edge.edge_server" in args:
            return int(entry.name)
    return None


def protect(pid: int) -> None:
    try:
        Path(f"/proc/{pid}/oom_score_adj").write_text("-1000")
    except (OSError, ProcessLookupError):
        logging.exception("无法设置进程 %s 的 oom_score_adj", pid)


def main() -> None:
    (ROOT / "run").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO)
    with (ROOT / "run/http-watchdog.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        protect(os.getpid())
        with (ROOT / "edge_server.log").open("ab", buffering=0) as output:
            child = None
            supervisors = {}
            while True:
                if child is not None:
                    child.poll()
                pid = find_edge_pid()
                if pid is None:
                    env = dict(os.environ, PYTHONPATH="/data/adb")
                    child = subprocess.Popen([sys.executable, "-m", "maibot_edge.edge_server"],
                                             stdin=subprocess.DEVNULL, stdout=output, stderr=output, env=env)
                    pid = child.pid
                    logging.info("Root HTTP 服务启动 pid=%s", pid)
                protect(pid)
                (ROOT / "run/edge_server.pid").write_text(str(pid))
                for module in ("message_daemon",):
                    if module == "message_daemon" and not (ROOT / "state/message_capture.enabled").exists():
                        continue
                    if module in supervisors:
                        supervisors[module].poll()
                    present = False
                    for proc in Path('/proc').iterdir():
                        if not proc.name.isdigit():
                            continue
                        try:
                            args=(proc/'cmdline').read_bytes().split(bytes([0]))
                            if ('maibot_edge.'+module).encode() in args:
                                present=True
                                break
                        except (FileNotFoundError,PermissionError,ProcessLookupError):
                            continue
                    if not present:
                        supervisors[module]=subprocess.Popen([sys.executable,'-m','maibot_edge.'+module],
                            stdin=subprocess.DEVNULL,stdout=output,stderr=output,env=dict(os.environ,PYTHONPATH='/data/adb'))
                time.sleep(5)


if __name__ == "__main__":
    main()
