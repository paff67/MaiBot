import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import campus, display
from .common import ROOT, STOP, config, lock, protect, status

def supervise():
    with lock('supervisor'):
        protect()
        # A killed supervisor can leave workers alive. Retire only our exact worker PIDs.
        for name in ('display', 'campus'):
            saved = ROOT / 'run' / (name + '.json')
            if not saved.exists():
                continue
            pid = int(json.loads(saved.read_text())['pid'])
            try:
                args = Path('/proc/' + str(pid) + '/cmdline').read_bytes().split(bytes([0]))
                if b'device_services.daemon' not in args or name.encode() not in args:
                    continue
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 60
                while Path('/proc/' + str(pid)).exists() and time.monotonic() < deadline:
                    time.sleep(0.2)
                if Path('/proc/' + str(pid)).exists():
                    current = Path('/proc/' + str(pid) + '/cmdline').read_bytes().split(bytes([0]))
                    if b'device_services.daemon' in current and name.encode() in current:
                        os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, FileNotFoundError):
                pass
        children = {}
        try:
            while not STOP.is_set() and not (ROOT / 'disabled').exists():
                cfg = config()
                for name in ('display', 'campus'):
                    child = children.get(name)
                    enabled = cfg[name].get('enabled', True) and not (ROOT / (name + '.disabled')).exists()
                    if not enabled and child and child.poll() is None:
                        child.terminate()
                        child.wait(timeout=60)
                    elif enabled and (child is None or child.poll() is not None):
                        children[name] = subprocess.Popen([sys.executable, '-m', 'device_services.daemon', name],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                status('supervisor', 'running', workers={n: c.pid for n, c in children.items() if c.poll() is None})
                STOP.wait(5)
        finally:
            for child in children.values():
                if child.poll() is None:
                    child.terminate()
            for child in children.values():
                try:
                    child.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            display.restore()
            status('supervisor', 'stopped')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['keeper','supervisor','display','campus','status','stop','restore','check','authenticate'])
    mode = parser.parse_args().mode
    os.umask(0o077)
    (ROOT / 'run').mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    if mode == 'status':
        for p in sorted((ROOT / 'run').glob('*.json')):
            print(p.stem, p.read_text())
    elif mode == 'stop':
        (ROOT / 'disabled').touch()
    elif mode == 'restore':
        display.restore()
    elif mode in ('check','authenticate'):
        with lock('campus'):
            try:
                print(json.dumps({'state': campus.step(config()['campus'], force=mode == 'authenticate')}))
            except Exception as exc:
                print(json.dumps({'state': 'error', 'error_type': type(exc).__name__}))
                raise SystemExit(1) from None
    elif mode == 'keeper':
        with lock('keeper'):
            protect()
            child = None
            try:
                while not STOP.is_set() and not (ROOT / 'disabled').exists():
                    if child is None or child.poll() is not None:
                        child = subprocess.Popen([sys.executable, '-m', 'device_services.daemon', 'supervisor'])
                    STOP.wait(5)
            finally:
                if child and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=120)
    elif mode == 'supervisor':
        supervise()
    elif mode == 'display':
        display.run()
    else:
        campus.run()

if __name__ == '__main__':
    main()
