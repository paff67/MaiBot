#!/usr/bin/env python3
import hashlib
import hmac
import json
import os
import random
import re
import subprocess
import time
import urllib.parse
import urllib.request
from collections import OrderedDict

CONFIG = os.path.expanduser("~/.campus/config.json")

SRUN_B64_ALPHA = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"


def ms():
    return int(time.time() * 1000)


def load_cfg():
    with open(CONFIG, "r", encoding="utf-8") as f:
        c = json.load(f)

    c.setdefault("portal_base", "http://10.210.0.2")
    c.setdefault("ac_id", "1")
    c.setdefault("theme", "pro")
    c.setdefault("force_ip", "")
    c.setdefault("check_url", "http://connect.rom.miui.com/generate_204")
    c.setdefault("check_code", 204)
    c.setdefault("timeout", 8)
    c.setdefault("os", "Windows 10")
    c.setdefault("name", "Windows")
    c.setdefault("double_stack", "0")
    c.setdefault("n", "200")
    c.setdefault("type", "1")
    c.setdefault(
        "ua",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/149 Safari/537.36",
    )

    return c


def get(o, url, timeout=8):
    if hasattr(o, "get"):
        return o.get(url, timeout)
    req = urllib.request.Request(url)
    with o.open(req, timeout=timeout) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or "utf-8"
        return r.getcode(), raw.decode(enc, errors="replace")


def qs(d):
    return urllib.parse.urlencode(d, safe="")


def cb():
    return f"jQuery1124{random.randint(10**15, 10**16 - 1)}_{ms()}"


def parse_jsonp(s):
    s = s.strip()
    if s.startswith("{"):
        return json.loads(s)

    m = re.search(r"^[^(]+\((.*)\)\s*;?\s*$", s, re.S)
    if not m:
        raise RuntimeError("invalid_jsonp")

    return json.loads(m.group(1))


def srun_b64(raw):
    """
    SRUN 自定义 Base64。
    不能使用 Python 标准 base64，否则会 auth_info_error。
    """
    if isinstance(raw, str):
        raw = raw.encode("latin1")

    out = []
    imax = len(raw) - len(raw) % 3

    for i in range(0, imax, 3):
        b10 = (raw[i] << 16) | (raw[i + 1] << 8) | raw[i + 2]
        out.append(SRUN_B64_ALPHA[b10 >> 18])
        out.append(SRUN_B64_ALPHA[(b10 >> 12) & 63])
        out.append(SRUN_B64_ALPHA[(b10 >> 6) & 63])
        out.append(SRUN_B64_ALPHA[b10 & 63])

    rem = len(raw) - imax

    if rem == 1:
        b10 = raw[imax] << 16
        out.append(SRUN_B64_ALPHA[b10 >> 18])
        out.append(SRUN_B64_ALPHA[(b10 >> 12) & 63])
        out.append("=")
        out.append("=")
    elif rem == 2:
        b10 = (raw[imax] << 16) | (raw[imax + 1] << 8)
        out.append(SRUN_B64_ALPHA[b10 >> 18])
        out.append(SRUN_B64_ALPHA[(b10 >> 12) & 63])
        out.append(SRUN_B64_ALPHA[(b10 >> 6) & 63])
        out.append("=")

    return "".join(out)


def sencode(msg, include_len):
    """
    按 SRUN 前端 JS 的 charCodeAt 逻辑编码。
    账号密码通常是 ASCII，所以这里足够匹配。
    """
    v = []
    c = len(msg)

    for i in range(0, c, 4):
        a0 = ord(msg[i]) if i < c else 0
        a1 = ord(msg[i + 1]) if i + 1 < c else 0
        a2 = ord(msg[i + 2]) if i + 2 < c else 0
        a3 = ord(msg[i + 3]) if i + 3 < c else 0
        v.append(a0 | (a1 << 8) | (a2 << 16) | (a3 << 24))

    if include_len:
        v.append(c)

    return v


def lencode(v, include_len):
    d = len(v)
    c = (d - 1) << 2

    if include_len:
        m = v[d - 1]
        if m < c - 3 or m > c:
            return ""
        c = m

    chars = []
    for x in v:
        x &= 0xFFFFFFFF
        chars.append(chr(x & 0xFF))
        chars.append(chr((x >> 8) & 0xFF))
        chars.append(chr((x >> 16) & 0xFF))
        chars.append(chr((x >> 24) & 0xFF))

    s = "".join(chars)

    if include_len:
        return s[:c]

    return s


def xencode(msg, key):
    if msg == "":
        return ""

    v = sencode(msg, True)
    k = sencode(key, False)

    if len(k) < 4:
        k += [0] * (4 - len(k))

    n = len(v) - 1
    z = v[n]
    y = v[0]
    c = 0x86014019 | 0x183639A0
    q = int(6 + 52 / (n + 1))
    d = 0

    while q > 0:
        q -= 1
        d = (d + c) & 0xFFFFFFFF
        e = (d >> 2) & 3

        for p in range(n):
            y = v[p + 1]
            m = (z >> 5) ^ ((y << 2) & 0xFFFFFFFF)
            m += ((y >> 3) ^ ((z << 4) & 0xFFFFFFFF)) ^ (d ^ y)
            m += k[(p & 3) ^ e] ^ z
            v[p] = (v[p] + m) & 0xFFFFFFFF
            z = v[p]

        y = v[0]
        m = (z >> 5) ^ ((y << 2) & 0xFFFFFFFF)
        m += ((y >> 3) ^ ((z << 4) & 0xFFFFFFFF)) ^ (d ^ y)
        m += k[(n & 3) ^ e] ^ z
        v[n] = (v[n] + m) & 0xFFFFFFFF
        z = v[n]

    return lencode(v, False)


def srun_info(username, password, ip, ac_id, token):
    data = OrderedDict(
        [
            ("username", username),
            ("password", password),
            ("ip", ip),
            ("acid", str(ac_id)),
            ("enc_ver", "srun_bx1"),
        ]
    )

    # JS 里是 JSON.stringify(info)，默认会带冒号/逗号后无空格
    j = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    encoded = xencode(j, token)
    return "{SRBX1}" + srun_b64(encoded)


def extract_ip(text):
    patterns = [
        r'"online_ip"\s*:\s*"(\d+\.\d+\.\d+\.\d+)"',
        r'"client_ip"\s*:\s*"(\d+\.\d+\.\d+\.\d+)"',
        r"online_ip['\"]?\s*[:=]\s*['\"](\d+\.\d+\.\d+\.\d+)['\"]",
        r"client_ip['\"]?\s*[:=]\s*['\"](\d+\.\d+\.\d+\.\d+)['\"]",
        r"(?:wlanuserip|userip|ip)=(\d+\.\d+\.\d+\.\d+)",
        r"ip\s*:\s*['\"](\d+\.\d+\.\d+\.\d+)['\"]",
        r'"ip"\s*:\s*"(\d+\.\d+\.\d+\.\d+)"',
    ]

    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)

    return ""


def is_portal_online(o, cfg):
    base = cfg["portal_base"].rstrip("/")
    p = OrderedDict([("callback", cb()), ("_", str(ms()))])
    try:
        _, text = get(o, f"{base}/cgi-bin/rad_user_info?{qs(p)}", cfg["timeout"])
        data = parse_jsonp(text)
        return data.get("error") == "ok" and bool(data.get("online_ip"))
    except Exception:
        return False


def _local_interface_ip(iface="wlan0"):
    try:
        res = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", iface],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        m = re.search(r"inet\s+(10\.\d+\.\d+\.\d+)", res.stdout or "")
        if m:
            return m.group(1)
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", res.stdout or "")
        if m and not m.group(1).startswith("127."):
            return m.group(1)
    except Exception:
        pass
    return ""


def get_ip(o, cfg):
    if cfg.get("force_ip"):
        return cfg["force_ip"]

    base = cfg["portal_base"].rstrip("/")
    try:
        p = OrderedDict([("callback", cb()), ("_", str(ms()))])
        _, text = get(o, f"{base}/cgi-bin/rad_user_info?{qs(p)}", cfg["timeout"])
        try:
            data = parse_jsonp(text)
            ip = data.get("online_ip") or data.get("client_ip") or ""
            if ip:
                return ip
        except Exception:
            pass
        ip = extract_ip(text)
        if ip:
            return ip
    except Exception:
        pass

    urls = [
        f"{base}/srun_portal_pc?ac_id={cfg['ac_id']}&theme={cfg['theme']}",
        f"{base}/srun_portal_success?ac_id={cfg['ac_id']}&theme={cfg['theme']}",
        f"{base}/index_1.html",
    ]

    for u in urls:
        try:
            _, text = get(o, u, cfg["timeout"])
            ip = extract_ip(text)
            if ip:
                return ip
        except Exception:
            pass

    ip = _local_interface_ip(cfg.get("interface", "wlan0"))
    if ip:
        return ip

    raise RuntimeError("无法解析 portal ip；可在 config.json 设置 force_ip")


def get_challenge(o, cfg, ip):
    base = cfg["portal_base"].rstrip("/")
    p = OrderedDict(
        [
            ("callback", cb()),
            ("username", cfg["username"]),
            ("ip", ip),
            ("_", str(ms())),
        ]
    )

    _, text = get(o, f"{base}/cgi-bin/get_challenge?{qs(p)}", cfg["timeout"])
    data = parse_jsonp(text)

    token = data.get("challenge")
    if not token:
        raise RuntimeError("challenge_rejected")

    return token


def do_login(o, cfg, ip, token):
    username = cfg["username"]
    password = cfg["password"]
    ac_id = str(cfg["ac_id"])
    n = str(cfg["n"])
    typ = str(cfg["type"])

    hmd5 = hmac.new(token.encode(), password.encode(), hashlib.md5).hexdigest()
    info = srun_info(username, password, ip, ac_id, token)

    chksum_str = (
        token
        + username
        + token
        + hmd5
        + token
        + ac_id
        + token
        + ip
        + token
        + n
        + token
        + typ
        + token
        + info
    )

    chksum = hashlib.sha1(chksum_str.encode()).hexdigest()

    p = OrderedDict(
        [
            ("callback", cb()),
            ("action", "login"),
            ("username", username),
            ("password", "{MD5}" + hmd5),
            ("os", cfg["os"]),
            ("name", cfg["name"]),
            ("double_stack", cfg["double_stack"]),
            ("chksum", chksum),
            ("info", info),
            ("ac_id", ac_id),
            ("ip", ip),
            ("n", n),
            ("type", typ),
            ("_", str(ms())),
        ]
    )

    base = cfg["portal_base"].rstrip("/")
    _, text = get(o, f"{base}/cgi-bin/srun_portal?{qs(p)}", cfg["timeout"])
    return parse_jsonp(text)
