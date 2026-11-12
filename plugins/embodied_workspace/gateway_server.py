from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import mimetypes
import os
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import WSMsgType, web

from .backend_supervisor import BackendSupervisor
from .realtime_hub import GroupRealtimeHub
from .subdomain_router import SubdomainRoute, SubdomainRouter
from .user_truth_writer import UserScopedTruthWriter

DEFAULT_COOKIE_MAX_AGE_SEC = 90 * 86400  # 90 天设备免登上限


def render_expired_html(issuer_name: str = "") -> str:
    safe_issuer = html.escape((issuer_name or "").strip() or "AI 助手")
    return (
        '<!DOCTYPE html>\n'
        '<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<title>链接已过期</title><style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:90vh;margin:0;background:#f8fafc;color:#334155;text-align:center}.card{background:#fff;padding:2rem;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,.06);max-width:340px}</style></head>\n'
        f'<body><div class="card"><h3>该临时页面已过期或已撤销</h3><p>为保护隐私与释放算力，限时工作区链接已下线。你可以在微信里让 {safe_issuer} 重新生成或唤醒它。</p></div></body></html>'
    )


ROOT_HOME_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>个人技术笔记与归档</title></head><body style="font-family:system-ui,sans-serif;padding:40px;max-width:640px;margin:auto;color:#334155">
<h2>个人技术研究与静态归档节点</h2><p>本站点用于托管个人日常学习笔记与数据可视化归档图表。</p></body></html>"""

MAIBOT_SDK_JS = r"""(function() {
  const state = { wxid: null, nickname: null, isCreator: false, isGroup: false, issuerName: "AI 助手", accessPolicy: "creator_only", members: [] };

  async function fetchWhoAmI() {
    try {
      const r = await fetch("/_maibot/auth/whoami", { credentials: "include" });
      if (r.ok) Object.assign(state, await r.json());
    } catch (e) {}
    renderPill();
  }

  function renderPill() {
    let pill = document.getElementById("maibot-id-pill");
    if (!pill) {
      pill = document.createElement("div");
      pill.id = "maibot-id-pill";
      pill.style.cssText = "position:fixed;bottom:14px;right:14px;z-index:99998;background:rgba(15,23,42,.88);color:#f8fafc;padding:6px 12px;border-radius:999px;font-size:12px;font-family:system-ui,sans-serif;box-shadow:0 4px 12px rgba(0,0,0,.25);display:flex;gap:8px;align-items:center;backdrop-filter:blur(6px)";
      document.body.appendChild(pill);
    }
    pill.replaceChildren();
    if (state.wxid) {
      const label = state.nickname || state.wxid.slice(0, 8);
      const userSpan = document.createElement("span");
      userSpan.textContent = "👤 " + label + (state.isCreator ? " (创建者)" : "");
      const handoffBtn = document.createElement("span");
      handoffBtn.id = "mb-handoff-btn";
      handoffBtn.style.cssText = "color:#818cf8;cursor:pointer;border-left:1px solid #334155;padding-left:8px";
      handoffBtn.textContent = "↗ 换浏览器免登";
      handoffBtn.onclick = copyHandoffUrl;
      const switchBtn = document.createElement("span");
      switchBtn.id = "mb-switch-btn";
      switchBtn.style.cssText = "color:#94a3b8;cursor:pointer";
      switchBtn.textContent = "切换";
      switchBtn.onclick = () => showAuthModal(null);
      pill.appendChild(userSpan);
      pill.appendChild(handoffBtn);
      pill.appendChild(switchBtn);
    } else {
      const loginBtn = document.createElement("span");
      loginBtn.id = "mb-login-btn";
      loginBtn.style.cssText = "cursor:pointer;color:#fbbf24";
      loginBtn.textContent = "👤 绑定微信身份（支持跨设备/PIN码）";
      loginBtn.onclick = () => showAuthModal(null);
      pill.appendChild(loginBtn);
    }
  }

  async function copyHandoffUrl() {
    const r = await fetch("/_maibot/auth/handoff", { method: "POST", credentials: "include" });
    const d = await r.json();
    if (d.url) {
      try { await navigator.clipboard.writeText(d.url); } catch (_) {}
      prompt("已生成 90 秒单次免登链接（复制后粘贴到 Safari/Chrome/电脑即可直接继承登录）：", d.url);
    }
  }

  function showAuthModal(onSuccessRetry) {
    let modal = document.getElementById("maibot-auth-modal");
    if (modal) modal.remove();
    modal = document.createElement("div");
    modal.id = "maibot-auth-modal";
    modal.style.cssText = "position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;padding:16px;font-family:system-ui,sans-serif";

    modal.innerHTML = `
      <div style="background:#1e293b;color:#f8fafc;padding:22px;border-radius:16px;max-width:350px;width:100%;box-shadow:0 12px 32px rgba(0,0,0,.4)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <strong style="font-size:16px">验证你的微信身份</strong>
          <span id="mb-close" style="cursor:pointer;color:#94a3b8;font-size:18px">✕</span>
        </div>
        <div style="background:#0f172a;padding:12px;border-radius:10px;margin-bottom:12px">
          <div style="font-size:13px;font-weight:600;margin-bottom:8px;color:#818cf8">方式 A：用个人 4 位 PIN 码秒登（跨设备推荐）</div>
          <select id="mb-wxid-select" style="width:100%;padding:8px;border-radius:6px;background:#1e293b;color:#fff;border:1px solid #334155;margin-bottom:8px">
          </select>
          <div style="display:flex;gap:8px">
            <input id="mb-pin-input" type="tel" maxlength="8" placeholder="输入4位PIN码" style="flex:1;padding:8px;border-radius:6px;border:1px solid #334155;background:#1e293b;color:#fff">
            <button id="mb-pin-submit" style="padding:8px 14px;background:#6366f1;color:#fff;border:none;border-radius:6px;font-weight:600;cursor:pointer">登录</button>
          </div>
        </div>
        <div style="background:#0f172a;padding:12px;border-radius:10px">
          <div style="font-size:13px;font-weight:600;margin-bottom:6px;color:#34d399">方式 B：微信发口令跨端秒绑（首次使用）</div>
          <input id="mb-new-pin" type="tel" maxlength="8" placeholder="顺便设置4位PIN码供以后换设备用(可选)" style="width:100%;box-sizing:border-box;padding:7px;border-radius:6px;border:1px solid #334155;background:#1e293b;color:#fff;margin-bottom:8px;font-size:12px">
          <button id="mb-get-code" style="width:100%;padding:9px;background:#10b981;color:#fff;border:none;border-radius:6px;font-weight:600;cursor:pointer">获取 4 位微信绑定口令</button>
          <div id="mb-code-box" style="display:none;text-align:center;margin-top:10px">
            <div id="mb-code-hint" style="font-size:12px;color:#cbd5e1"></div>
            <div id="mb-code-text" style="font-size:21px;font-weight:800;color:#34d399;letter-spacing:2px;margin:6px 0">认领 ----</div>
            <div style="font-size:11px;color:#94a3b8">发送后本页面将在 1 秒内自动登录...</div>
          </div>
        </div>
      </div>`;
    document.body.appendChild(modal);

    const selectEl = document.getElementById("mb-wxid-select");
    const members = Array.isArray(state.members) ? state.members : [];
    if (members.length > 0) {
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "-- 选择你的微信昵称 --";
      selectEl.appendChild(placeholder);
      members.forEach((m) => {
        const opt = document.createElement("option");
        opt.value = String(m.wxid || "");
        opt.textContent = String(m.nickname || m.wxid || "");
        selectEl.appendChild(opt);
      });
    } else {
      selectEl.style.display = "none";
    }
    const codeHint = document.getElementById("mb-code-hint");
    codeHint.textContent = "请在微信会话中向 " + (state.issuerName || "AI 助手") + " 发送：";

    let pollTimer = null;
    const cleanup = () => { if (pollTimer) clearInterval(pollTimer); modal.remove(); };
    document.getElementById("mb-close").onclick = cleanup;

    document.getElementById("mb-pin-submit").onclick = async () => {
      const wxid = document.getElementById("mb-wxid-select").value;
      const pin = document.getElementById("mb-pin-input").value;
      const r = await fetch("/_maibot/auth/pin_login", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "include",
        body: JSON.stringify({ wxid, pin })
      });
      const d = await r.json();
      if (d.ok) { cleanup(); await fetchWhoAmI(); if (onSuccessRetry) onSuccessRetry(); }
      else { alert(d.error || "PIN 码错误或尚未设置"); }
    };

    document.getElementById("mb-get-code").onclick = async () => {
      const pinToSet = document.getElementById("mb-new-pin").value.trim();
      const r = await fetch("/_maibot/auth/start_claim", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin_to_set: pinToSet })
      });
      const d = await r.json();
      if (!d.ok) return;
      document.getElementById("mb-code-box").style.display = "block";
      document.getElementById("mb-code-text").textContent = "认领 " + d.claim_code;
      pollTimer = setInterval(async () => {
        const pr = await fetch(`/_maibot/auth/poll_claim?code=${d.claim_code}&nonce=${d.nonce}`, { credentials: "include" });
        const pd = await pr.json();
        if (pd.ok && pd.verified) { cleanup(); await fetchWhoAmI(); if (onSuccessRetry) onSuccessRetry(); }
      }, 1500);
    };
  }

  let _rtSocket = null;
  const _rtListeners = [];
  function ensureRealtimeSocket() {
    if (_rtSocket && (_rtSocket.readyState === 0 || _rtSocket.readyState === 1)) return _rtSocket;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    _rtSocket = new WebSocket(`${proto}//${location.host}/_maibot/ws`);
    _rtSocket.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        _rtListeners.forEach(fn => { try { fn(data); } catch (e) {} });
      } catch (e) {}
    };
    _rtSocket.onclose = () => { setTimeout(() => { if (_rtListeners.length > 0) ensureRealtimeSocket(); }, 3000); };
    return _rtSocket;
  }

  window.MaiBot = {
    getState: () => state,
    openAuth: () => showAuthModal(null),
    subscribeRealtime: function(callback) {
      if (typeof callback === "function") {
        _rtListeners.push(callback);
        ensureRealtimeSocket();
      }
    },
    broadcast: function(event, data) {
      const ws = ensureRealtimeSocket();
      const payload = JSON.stringify({ action: "broadcast", event: event, data: data });
      if (ws.readyState === 1) ws.send(payload);
      else ws.addEventListener("open", () => ws.send(payload), { once: true });
    },
    notify: async function(payload) {
      const send = async () => fetch("/_maibot/notify", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "include",
        body: JSON.stringify(payload)
      });
      let resp = await send();
      if (resp.status === 401) {
        return new Promise((resolve) => {
          showAuthModal(async () => {
            const retryResp = await send();
            resolve(await retryResp.json());
          });
        });
      }
      return await resp.json();
    }
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fetchWhoAmI);
  else fetchWhoAmI();
})();"""


def resolve_or_create_cookie_secret(configured_secret: str, workspaces_root: Path | None = None) -> str:
    clean = (configured_secret or "").strip()
    if clean:
        return clean
    target_root = workspaces_root or Path("data/workspaces")
    target_root.mkdir(parents=True, exist_ok=True)
    secret_file = target_root / ".cookie_secret"
    if secret_file.is_file():
        existing = secret_file.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    generated = secrets.token_hex(32)
    secret_file.write_text(generated, encoding="utf-8")
    try:
        os.chmod(secret_file, 0o600)
    except OSError:
        pass
    return generated


class IdentityVerifier:
    def __init__(
        self,
        secret_key: str,
        base_domain: str,
        max_age_sec: int = DEFAULT_COOKIE_MAX_AGE_SEC,
    ) -> None:
        if not (secret_key or "").strip():
            raise ValueError("IdentityVerifier 需要非空 secret_key")
        self.secret_key = secret_key.strip().encode("utf-8")
        self.max_age_sec = int(max_age_sec)
        parts = base_domain.strip().lstrip(".").split(".")
        self.cookie_domain = "." + ".".join(parts[-2:]) if len(parts) >= 2 else base_domain

    def sign_wxid(self, wxid: str, *, issued_at: int | None = None) -> str:
        ts = str(int(time.time()) if issued_at is None else int(issued_at))
        payload = f"{wxid}.{ts}"
        sig = hmac.new(self.secret_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
        return f"{payload}.{sig}"

    def verify_cookie(self, cookie_val: str | None, *, now: float | None = None) -> str | None:
        if not cookie_val or cookie_val.count(".") != 2:
            return None
        wxid, ts_str, sig = cookie_val.split(".")
        if not wxid or not ts_str.isdigit():
            return None
        issued_ts = int(ts_str)
        eff_now = time.time() if now is None else now
        if issued_ts > eff_now + 300 or (eff_now - issued_ts) > self.max_age_sec:
            return None
        payload = f"{wxid}.{ts_str}"
        expected = hmac.new(self.secret_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(sig, expected):
            return None
        return wxid

    def attach_cookie(self, response: web.StreamResponse, wxid: str) -> None:
        signed = self.sign_wxid(wxid)
        response.set_cookie(
            "maibot_wxid",
            signed,
            domain=self.cookie_domain,
            max_age=self.max_age_sec,
            httponly=True,
            secure=True,
            samesite="Lax",
        )


class WorkspaceGatewayServer:
    def __init__(
        self,
        router: SubdomainRouter,
        supervisor: BackendSupervisor,
        truth_writer: UserScopedTruthWriter | None = None,
        cookie_secret: str = "",
        workspaces_root: Path | None = None,
        default_issuer_name: str = "AI 助手",
        on_web_notify: Callable[[SubdomainRoute, str, str, str, list[str], bool], Awaitable[None]] | None = None,
        listen_port: int = 18790,
        realtime_hub: GroupRealtimeHub | None = None,
    ) -> None:
        self.router = router
        self.supervisor = supervisor
        self.truth_writer = truth_writer or UserScopedTruthWriter(truth_db_path=Path("/dev/null"))
        resolved_secret = resolve_or_create_cookie_secret(
            cookie_secret,
            workspaces_root or supervisor.workspaces_root,
        )
        self.identity = IdentityVerifier(resolved_secret, router.base_domain)
        self.default_issuer_name = (default_issuer_name or "").strip() or "AI 助手"
        self.on_web_notify = on_web_notify
        self.listen_port = listen_port
        self.realtime_hub = realtime_hub
        self._runner: web.AppRunner | None = None
        self._client_session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._client_session is None or self._client_session.closed:
            self._client_session = aiohttp.ClientSession(auto_decompress=False)
        app = web.Application(client_max_size=50 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._handle_request)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.listen_port)
        await site.start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._client_session and not self._client_session.closed:
            await self._client_session.close()
        self._client_session = None

    def _extract_token(self, host: str) -> str | None:
        host_clean = host.split(":")[0].strip().lower()
        if host_clean == self.router.base_domain:
            return "__root__"
        suffix = f".{self.router.base_domain}"
        if not host_clean.endswith(suffix):
            return None
        token = host_clean[: -len(suffix)]
        return token if token and "." not in token else None

    @staticmethod
    def _inject_sdk_into_html(html_text: str) -> str:
        tag = '<script src="/_maibot/sdk.js" defer></script>'
        if "/_maibot/sdk.js" in html_text:
            return html_text
        if "</body>" in html_text:
            return html_text.replace("</body>", f"{tag}</body>", 1)
        return html_text + tag

    async def _handle_request(self, request: web.Request) -> web.StreamResponse:
        token = self._extract_token(request.headers.get("Host", ""))
        if token in ("__root__", None):
            return web.Response(status=200, text=ROOT_HOME_HTML, content_type="text/html")

        route = self.router.get_route(token)
        if not route or route.revoked or route.expire_at < time.time():
            issuer = (route.issuer_name if route and route.issuer_name else self.default_issuer_name)
            return web.Response(
                status=410,
                text=render_expired_html(issuer),
                content_type="text/html",
            )

        # 1. 拦截跨浏览器单次接力票据 ?_ho=<ticket>，兑换 Cookie 后 302 擦除地址栏参数
        ho_ticket = request.query.get("_ho")
        if ho_ticket:
            wxid = self.router.consume_handoff_ticket(ho_ticket)
            clean_url = f"https://{request.host}{request.path}"
            resp = web.HTTPFound(location=clean_url)
            if wxid:
                self.identity.attach_cookie(resp, wxid)
            return resp

        # 2. 拦截网关内置系统端点 /_maibot/*
        if request.path == "/_maibot/meta" and request.method == "GET":
            return web.json_response(
                {
                    "ok": True,
                    "title": route.title,
                    "summary": route.summary or route.title,
                    "mode": route.mode,
                    "issuer_name": route.issuer_name or self.default_issuer_name,
                }
            )
        if request.path == "/_maibot/sdk.js" and request.method == "GET":
            return web.Response(text=MAIBOT_SDK_JS, content_type="application/javascript")
        if request.path.startswith("/_maibot/auth/"):
            return await self._handle_auth_endpoints(request, route)
        if request.path == "/_maibot/notify" and request.method == "POST":
            return await self._handle_maibot_notify(request, route)
        if request.path == "/_maibot/ws" and self.realtime_hub is not None:
            actor_wxid = self.identity.verify_cookie(request.cookies.get("maibot_wxid"))
            if route.access_policy == "creator_only":
                if route.creator_wxid and actor_wxid != route.creator_wxid:
                    return web.json_response({"ok": False, "error": "仅创建者可订阅该独占页面的实时总线"}, status=403)
            return await self.realtime_hub.handle_ws_connection(request, route, actor_wxid)

        if route.mode == "backend":
            return await self._proxy_to_backend(request, route)
        return self._serve_static_file(request, route)

    def _resolve_actor_public_nickname(self, actor_wxid: str | None, members: list[dict[str, str]]) -> str | None:
        if not actor_wxid:
            return None
        for m in members:
            if m.get("wxid") == actor_wxid and m.get("nickname"):
                return m["nickname"]
        pub = self.truth_writer.get_public_nickname(actor_wxid)
        if pub and pub != actor_wxid:
            return pub
        pin_nick = self.router.get_pin_user_nickname(actor_wxid)
        return pin_nick or actor_wxid

    async def _handle_auth_endpoints(self, request: web.Request, route: SubdomainRoute) -> web.Response:
        actor_wxid = self.identity.verify_cookie(request.cookies.get("maibot_wxid"))
        is_group_chat = route.chat_id.endswith("@chatroom")

        if request.path == "/_maibot/auth/whoami":
            members = self.truth_writer.list_group_public_members(route.chat_id) if is_group_chat else []
            nickname = self._resolve_actor_public_nickname(actor_wxid, members)
            return web.json_response(
                {
                    "ok": True,
                    "wxid": actor_wxid,
                    "nickname": nickname,
                    "isCreator": bool(actor_wxid and actor_wxid == route.creator_wxid),
                    "isGroup": is_group_chat,
                    "issuerName": route.issuer_name or self.default_issuer_name,
                    "accessPolicy": route.access_policy,
                    "members": members,
                }
            )

        if request.path == "/_maibot/auth/start_claim" and request.method == "POST":
            body = await request.json()
            nonce = secrets.token_urlsafe(12)
            code = self.router.create_claim(
                chat_id=route.chat_id,
                browser_nonce=nonce,
                pin_to_set=str(body.get("pin_to_set") or ""),
            )
            return web.json_response({"ok": True, "claim_code": code, "nonce": nonce})

        if request.path == "/_maibot/auth/poll_claim" and request.method == "GET":
            code = request.query.get("code", "")
            nonce = request.query.get("nonce", "")
            verified_wxid = self.router.poll_claim(code, nonce)
            if not verified_wxid:
                return web.json_response({"ok": True, "verified": False})
            resp = web.json_response({"ok": True, "verified": True, "wxid": verified_wxid})
            self.identity.attach_cookie(resp, verified_wxid)
            return resp

        if request.path == "/_maibot/auth/pin_login" and request.method == "POST":
            body = await request.json()
            wxid = str(body.get("wxid") or "").strip()
            if not wxid and not is_group_chat and route.creator_wxid:
                wxid = route.creator_wxid
            pin = str(body.get("pin") or "").strip()
            if not wxid:
                return web.json_response({"ok": False, "error": "请先选择你的微信身份或使用口令绑定"}, status=400)
            if is_group_chat:
                group_wxids = self.truth_writer.get_group_member_wxids(route.chat_id)
                if group_wxids and wxid not in group_wxids:
                    return web.json_response({"ok": False, "error": "仅当前群聊成员可登录此页面"}, status=403)
            if self.router.is_pin_locked_out(wxid):
                return web.json_response(
                    {"ok": False, "error": "PIN 连续错误次数过多，请冷却 5 分钟后再试"},
                    status=429,
                )
            if not self.router.verify_user_pin(wxid, pin):
                if self.router.is_pin_locked_out(wxid):
                    return web.json_response(
                        {"ok": False, "error": "PIN 连续错误已达 5 次，已临时冷却 5 分钟"},
                        status=429,
                    )
                return web.json_response({"ok": False, "error": "PIN 码不正确或该用户尚未设置 PIN"}, status=403)
            resp = web.json_response({"ok": True, "wxid": wxid})
            self.identity.attach_cookie(resp, wxid)
            return resp

        if request.path == "/_maibot/auth/handoff" and request.method == "POST":
            if not actor_wxid:
                return web.json_response({"ok": False, "error": "当前未登录"}, status=401)
            ticket = self.router.create_handoff_ticket(actor_wxid, ttl_sec=90.0)
            direct_url = f"{self.router.format_direct_url(route.token)}/?_ho={ticket}"
            return web.json_response({"ok": True, "url": direct_url})

        return web.json_response({"ok": False, "error": "Unknown auth endpoint"}, status=404)

    async def _handle_maibot_notify(self, request: web.Request, route: SubdomainRoute) -> web.Response:
        actor_wxid = self.identity.verify_cookie(request.cookies.get("maibot_wxid"))

        if not actor_wxid:
            return web.json_response(
                {"ok": False, "require_auth": True, "error": "请先验证你的微信身份后再提交"},
                status=401,
            )

        if route.chat_id.endswith("@chatroom"):
            group_wxids = self.truth_writer.get_group_member_wxids(route.chat_id)
            if group_wxids and actor_wxid not in group_wxids and actor_wxid != route.creator_wxid:
                return web.json_response(
                    {"ok": False, "error": "仅当前群聊成员可提交交互修改"},
                    status=403,
                )

        if route.access_policy == "creator_only" and route.creator_wxid and actor_wxid != route.creator_wxid:
            return web.json_response(
                {"ok": False, "error": "该页面为创建者独占模式，仅创建者本人可提交修改"},
                status=403,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        event_name = str(body.get("event") or "网页交互事件")[:120]
        detail = str(body.get("detail") or "")[:1000]
        want_reply = bool(body.get("trigger_reply", True))
        memory_patch = body.get("memory_patch")

        applied_changes: list[str] = []
        if isinstance(memory_patch, dict) and memory_patch:
            applied_changes = self.truth_writer.apply_user_scoped_patch(
                actor_wxid=actor_wxid,
                origin_chat_id=route.chat_id,
                patch=memory_patch,
            )

        allowed_proactive = want_reply and self.router.check_and_touch_notify_cooldown(route.token)
        if self.on_web_notify:
            await self.on_web_notify(route, actor_wxid, event_name, detail, applied_changes, allowed_proactive)

        if self.realtime_hub is not None:
            await self.realtime_hub.broadcast_to_token(
                route.token,
                {
                    "type": "web_notify",
                    "actor_wxid": actor_wxid,
                    "event": event_name,
                    "detail": detail,
                    "applied_memory_changes": applied_changes,
                    "ts": time.time(),
                },
            )

        return web.json_response(
            {
                "ok": True,
                "actor_wxid": actor_wxid,
                "applied_memory_changes": applied_changes,
                "proactive_triggered": allowed_proactive,
            }
        )

    def _serve_static_file(self, request: web.Request, route: SubdomainRoute) -> web.StreamResponse:
        dist_root = Path(route.dist_dir).resolve()
        raw_rel = request.path.lstrip("/")
        rel_path = raw_rel or "index.html"
        target = (dist_root / rel_path).resolve()
        if not target.is_relative_to(dist_root):
            return web.Response(status=403, text="Forbidden")
        if not target.exists() or target.is_dir():
            # 仅对无扩展名的前端路由回退 SPA index.html，显式静态资源缺失返回 404
            if not raw_rel or "." not in Path(raw_rel).name:
                target = (dist_root / "index.html").resolve()
            else:
                return web.Response(status=404, text="File Not Found")
        if not target.is_file():
            return web.Response(status=404, text="File Not Found")

        ctype, _ = mimetypes.guess_type(str(target))
        if target.suffix.lower() in (".html", ".htm"):
            raw_html = target.read_text(encoding="utf-8", errors="ignore")
            injected = self._inject_sdk_into_html(raw_html)
            return web.Response(
                text=injected,
                content_type="text/html",
                headers={"X-Robots-Tag": "noindex, nofollow, noarchive"},
            )
        return web.FileResponse(
            target,
            headers={"X-Robots-Tag": "noindex, nofollow, noarchive", "Content-Type": ctype or "application/octet-stream"},
        )

    def _ensure_client_session(self) -> aiohttp.ClientSession:
        if self._client_session is None or self._client_session.closed:
            self._client_session = aiohttp.ClientSession(auto_decompress=False)
        return self._client_session

    async def _proxy_to_backend(self, request: web.Request, route: SubdomainRoute) -> web.StreamResponse:
        start_cmd = json.loads(route.start_cmd_json or "[]")
        try:
            port = await self.supervisor.ensure_running(route, start_cmd)
        except Exception as exc:
            return web.Response(status=502, text=f"Backend Auto-Wake Failed: {exc}")

        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._proxy_websocket(request, port)

        target_url = f"http://127.0.0.1:{port}{request.path_qs}"
        stripped = {"host", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip"}
        headers = {k: v for k, v in request.headers.items() if k.lower() not in stripped}
        headers["X-Forwarded-Host"] = request.host
        headers["X-Forwarded-Proto"] = request.scheme
        if request.remote:
            headers["X-Forwarded-For"] = request.remote

        body_stream: Any = request.content if request.can_read_body else None
        session = self._ensure_client_session()
        async with session.request(
            request.method,
            target_url,
            headers=headers,
            data=body_stream,
            allow_redirects=False,
        ) as resp:
            ctype = resp.headers.get("Content-Type", "")
            out_headers = {
                k: v for k, v in resp.headers.items()
                if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
            }
            out_headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"

            if "text/html" in ctype.lower():
                raw_text = await resp.text(errors="ignore")
                return web.Response(
                    status=resp.status,
                    text=self._inject_sdk_into_html(raw_text),
                    headers=out_headers,
                )

            stream_resp = web.StreamResponse(status=resp.status, headers=out_headers)
            await stream_resp.prepare(request)
            async for chunk in resp.content.iter_chunked(65536):
                await stream_resp.write(chunk)
            await stream_resp.write_eof()
            return stream_resp

    async def _proxy_websocket(self, request: web.Request, port: int) -> web.WebSocketResponse:
        ws_server = web.WebSocketResponse()
        await ws_server.prepare(request)
        target_ws_url = f"http://127.0.0.1:{port}{request.path_qs}"

        session = self._ensure_client_session()
        async with session.ws_connect(target_ws_url) as ws_client:
            async def forward(src: Any, dst: Any) -> None:
                async for msg in src:
                    if msg.type == WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    elif msg.type == WSMsgType.CLOSE:
                        await dst.close()

            await asyncio.gather(
                forward(ws_server, ws_client),
                forward(ws_client, ws_server),
                return_exceptions=True,
            )
        return ws_server
