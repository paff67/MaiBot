"""Use owner's SRUN algorithm on sockets bound to the selected Wi-Fi only."""
from pathlib import Path
import http.client
import re
import socket
import ssl
import urllib.parse
import xml.etree.ElementTree as ET

from . import srun
from .common import ROOT, STOP, command, config, lock, protect, status

class WifiHTTP:
    def __init__(self, cfg):
        self.cfg = cfg
        self.allowed = {self.origin(cfg['portal_base']), self.origin(cfg['check_url'])}

    @staticmethod
    def origin(url):
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            raise ValueError('invalid_endpoint')
        return p.scheme, p.hostname, p.port or (443 if p.scheme == 'https' else 80)

    def get(self, url, timeout=8):
        current_url = url
        redirect_locations = []
        for _ in range(4):
            origin = self.origin(current_url)
            if origin not in self.allowed:
                raise ValueError('unexpected_endpoint')
            p = urllib.parse.urlsplit(current_url)
            host, port = origin[1:]
            address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            conn = None
            try:
                sock.settimeout(timeout)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                               self.cfg.get('interface', 'wlan0').encode() + bytes([0]))
                sock.connect(address)
                if p.scheme == 'https':
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
                conn = http.client.HTTPConnection(host, port, timeout=timeout)
                conn.sock = sock
                target = urllib.parse.urlunsplit(('', '', p.path or '/', p.query, ''))
                conn.request('GET', target, headers={'User-Agent': self.cfg['ua'], 'Connection': 'close'})
                response = conn.getresponse()
                location = response.getheader('Location') or ''
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError('response_too_large')
                body = raw.decode('utf-8', errors='replace')
                if response.status in (301, 302, 303, 307, 308) and location:
                    redirect_locations.append(location)
                    next_url = urllib.parse.urljoin(current_url, location)
                    try:
                        next_origin = self.origin(next_url)
                    except ValueError:
                        next_origin = None
                    if next_origin in self.allowed and current_url != self.cfg.get('check_url'):
                        current_url = next_url
                        continue
                if redirect_locations and '/cgi-bin/' not in current_url:
                    body = body + '\n' + '\n'.join(redirect_locations)
                return response.status, body
            finally:
                if conn:
                    conn.close()
                sock.close()
        return response.status, body

def wifi_ssid(text):
    match = re.search('Wifi is connected to ' + chr(34) + '([^' + chr(34) + ']+)' + chr(34), text)
    return match.group(1) if match else ''

def saved_passphrase(ssid):
    stores = [Path('/data/misc/apexdata/com.android.wifi/WifiConfigStore.xml'),
              Path('/data/misc_ce/0/apexdata/com.android.wifi/WifiConfigStore.xml'),
              Path('/data/misc_ce/0/wifi/WifiConfigStore.xml')]
    for path in stores:
        if not path.exists():
            continue
        for network in ET.parse(path).getroot().iter('WifiConfiguration'):
            values = {e.get('name'): e.text for e in network.findall('string')}
            if (values.get('SSID') or '').strip('"') == ssid:
                key = values.get('PreSharedKey') or ''
                if key.startswith('"') and key.endswith('"'):
                    return key[1:-1]
    raise ValueError('saved_wifi_key_unavailable')

def reconnect(cfg, text):
    if 'Wifi is disabled' in text:
        command('cmd', 'wifi', 'set-wifi-enabled', 'enabled')
        return 'wifi_enabling'
    if cfg['ssid'] not in command('cmd', 'wifi', 'list-scan-results'):
        command('cmd', 'wifi', 'start-scan')
        return 'waiting_for_access_point'
    password = saved_passphrase(cfg['ssid'])
    command('cmd', 'wifi', 'connect-network', cfg['ssid'], 'wpa2', password, timeout=25)
    return 'wifi_connecting'

def online(client, cfg):
    try:
        code, _ = client.get(cfg['check_url'], cfg['timeout'])
        if code == int(cfg['check_code']):
            return True
    except (OSError, ValueError, http.client.HTTPException):
        pass
    return srun.is_portal_online(client, cfg)

def step(settings, force=False):
    if not (ROOT / 'campus.json').exists():
        return 'missing_credentials'
    srun.CONFIG = str(ROOT / 'campus.json')
    cfg = srun.load_cfg()
    cfg.update(settings)
    if not cfg.get('username') or not cfg.get('password'):
        return 'missing_credentials'
    current = command('cmd', 'wifi', 'status')
    ssid = wifi_ssid(current)
    if ssid != cfg['ssid']:
        return 'other_wifi' if ssid else reconnect(cfg, current)
    client = WifiHTTP(cfg)
    if not force and online(client, cfg):
        return 'online'
    # Portal advertises the NAT/WAN address. Stored force_ip can become stale.
    # Only use a fixed address when explicitly requested in the service configuration.
    ip = srun.get_ip(client, dict(cfg, force_ip=cfg.get('force_ip', '') if cfg.get('use_fixed_ip', False) else ''))
    token = srun.get_challenge(client, cfg, ip)
    result = srun.do_login(client, cfg, ip, token)
    err_text = str(result.get('error', '')).lower()
    suc_text = str(result.get('suc_msg', '')).lower()
    emsg_text = str(result.get('error_msg', '')).lower()
    already_online = 'already_online' in err_text or 'already_online' in suc_text or 'already_online' in emsg_text
    if not already_online and (result.get('error') != 'ok' or result.get('ecode') not in (0, '0', None)):
        return 'authentication_rejected'
    STOP.wait(3)
    return 'authenticated_online' if (already_online or online(client, cfg)) else 'authentication_sent_offline'

def run():
    with lock('campus'):
        protect()
        failures = 0
        while not STOP.is_set():
            try:
                state = step(config()['campus'])
                status('campus', state)
                failures = 0 if state in ('online', 'authenticated_online', 'other_wifi') else failures + 1
            except Exception as exc:
                failures += 1
                status('campus', 'error', error_type=type(exc).__name__)
            delay = 30 if failures == 0 else min(900, 30 * 2 ** min(failures, 5))
            if 'state' in locals() and state in ('wifi_enabling', 'wifi_connecting'):
                delay = 5
            STOP.wait(delay)
