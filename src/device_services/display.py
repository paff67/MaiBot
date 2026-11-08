"""Keep the actual Android display awake at a low user-configured brightness."""
import json

from .common import ROOT, STOP, command, config, lock, protect, save, status

SETTINGS = [('system', 'screen_brightness'), ('system', 'screen_brightness_mode'),
            ('system', 'screen_off_timeout'), ('global', 'stay_on_while_plugged_in')]

def step(cfg):
    brightness = int(cfg.get('brightness', 8))
    if not 1 <= brightness <= 40:
        raise ValueError('brightness_not_in_low_range')
    backup = ROOT / 'display-original.json'
    if not backup.exists():
        save(backup, {ns + '/' + key: command('settings', 'get', ns, key) for ns, key in SETTINGS})
    desired = {'system/screen_brightness_mode': '0', 'system/screen_brightness': str(brightness),
               'system/screen_off_timeout': '2147483647', 'global/stay_on_while_plugged_in': '7'}
    for full, value in desired.items():
        ns, key = full.split('/')
        if command('settings', 'get', ns, key) != value:
            command('settings', 'put', ns, key, value)
    awake = 'mWakefulness=Awake' in command('dumpsys', 'power')
    if not awake:
        command('input', 'keyevent', 'KEYCODE_WAKEUP')
    status('display', 'maintaining', brightness=brightness, awake_before_check=awake)

def restore():
    p = ROOT / 'display-original.json'
    if p.exists():
        for full, value in json.loads(p.read_text()).items():
            ns, key = full.split('/')
            if value == 'null':
                command('settings', 'delete', ns, key)
            else:
                command('settings', 'put', ns, key, str(value))
    status('display', 'stopped_restored')

def run():
    with lock('display'):
        protect()
        try:
            while not STOP.is_set():
                try:
                    step(config()['display'])
                except Exception as exc:
                    status('display', 'error', error_type=type(exc).__name__)
                STOP.wait(15)
        finally:
            restore()
