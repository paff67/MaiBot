from pathlib import Path
import fcntl
import json
import os
import subprocess
import threading
import time

ROOT = Path('/data/adb/device_services')
STOP = threading.Event()
ENV = {k: v for k, v in os.environ.items() if k not in ('LD_LIBRARY_PATH', 'LD_PRELOAD')}
ENV['PATH'] = '/system/bin:/system/xbin'

def command(*args, timeout=15):
    p = subprocess.run(args, env=ENV, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError('system_command_failed')
    return p.stdout.strip()

def save(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        os.chmod(temp, 0o600)
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)

def status(name, state, **fields):
    save(ROOT / 'run' / (name + '.json'), dict(state=state, pid=os.getpid(), updated_at=time.time(), **fields))

def lock(name):
    handle = (ROOT / 'run' / (name + '.lock')).open('w')
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle

def config():
    return json.loads((ROOT / 'config.json').read_text())

def protect():
    Path('/proc/self/oom_score_adj').write_text('-1000')
