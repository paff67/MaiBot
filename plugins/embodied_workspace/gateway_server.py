from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import WSMsgType, web

from .backend_supervisor import BackendSupervisor
from .subdomain_router import SubdomainRoute, SubdomainRouter
from .user_truth_writer import UserScopedTruthWriter

EXPIRED_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>链接已过期</title><style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:90vh;margin:0;background:#f8fafc;color:#334155;text-align:center}.card{background:#fff;padding:2rem;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,.06);max-width:340px}</style></head>
<body><div class="card"><h3>该临时页面已过期或已撤销</h3><p>为保护隐私与释放算力，限时工作区链接已下线。你可以在微信里让许妍重新生成或唤醒它。</p></div></body></html>"""

ROOT_HOME_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>个人技术笔记与归档</title></head><body style="font-family:system-ui,sans-serif;padding:40px;max-width:640px;margin:auto;color:#334155">
<h2>个人技术研究与静态归档节点</h2><p>本站点用于托管个人日常学习笔记与数据可视化归档图表。</p></body></html>"""

MAIBOT_SDK_JS = r"""(function() {
  const state = { wxid: null, nickname: null, isCreator: false, accessPolicy: "creator_only", members: [] };

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
    if (state.wxid) {
      const label = state.nickname || state.wxid.slice(0, 8);
      pill.innerHTML = `<span>👤 ${label}${state.isCreator ? " (创建者)" : ""}</span>
        <span id="mb-handoff-btn" style="color:#818cf8;cursor:pointer;border-left:1px solid #334155;padding-left:8px">↗ 换浏览器免登</span>
        <span id="mb-switch-btn" style="color:#94a3b8;cursor:pointer">切换</span>`;
      document.getElementById("mb-handoff-btn").onclick = copyHandoffUrl;
      document.getElementById("mb-switch-btn").onclick = () => showAuthModal(null);
    } else {
      pill.innerHTML = `<span style="cursor:pointer;color:#fbbf24" id="mb-login-btn">👤 绑定微信身份（支持跨设备/PIN码）</span>`;
      document.getElementById("mb-login-btn").onclick = () => showAuthModal(null);
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

    const memberOptions = (state.members || []).map(m => `<option value="${m.wxid}">${m.nickname}</option>`).join("");

    modal.innerHTML = `
      <div style="background:#1e293b;color:#f8fafc;padding:22px;border-radius:16px;max-width:350px;width:100%;box-shadow:0 12px 32px rgba(0,0,0,.4)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <strong style="font-size:16px">验证你的微信身份</strong>
          <span id="mb-close" style="cursor:pointer;color:#94a3b8;font-size:18px">✕</span>
        </div>
        <div style="background:#0f172a;padding:12px;border-radius:10px;margin-bottom:12px">
          <div style="font-size:13px;font-weight:600;margin-bottom:8px;color:#818cf8">方式 A：用个人 4 位 PIN 码秒登（跨设备推荐）</div>
          <select id="mb-wxid-select" style="width:100%;padding:8px;border-radius:6px;background:#1e293b;color:#fff;border:1px solid #334155;margin-bottom:8px">
            <option value="">-- 选择你的名字 --</option>${memberOptions}
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
            <div style="font-size:12px;color:#cbd5e1">请在微信群（或私聊许妍）发送：</div>
            <div id="mb-code-text" style="font-size:21px;font-weight:800;color:#34d399;letter-spacing:2px;margin:6px 0">认领 ----</div>
            <div style="font-size:11px;color:#94a3b8">发送后本页面将在 1 秒内自动登录...</div>
          </div>
        </div>
      </div>`;
    document.body.appendChild(modal);

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

  window.MaiBot = {
    getState: () => state,
    openAuth: () => showAuthModal(null),
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


class IdentityVerifier:
    def __init__(self, secret_key: str, base_domain: str) -> None:
        self.secret_key = secret_key.encode("utf-8")
        parts = base_domain.strip().lstrip(".").split(".")
        self.cookie_domain = "." + ".".join(parts[-2:]) if len(parts) >= 2 else base_domain

    def sign_wxid(self, wxid: str) -> str:
        ts = str(int(time.time()))
        payload = f"{wxid}.{ts}"
        sig = hmac.new(self.secret_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
        return f"{payload}.{sig}"

    def verify_cookie(self, cookie_val: str | None) -> str | None:
        if not cookie_val or cookie_val.count(".") != 2:
            return None
        wxid, ts_str, sig = cookie_val.split(".")
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
            max_age=365 * 86400,
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
        cookie_secret: str = "maibot-embodied-workspace-hmac-secret-2026",
        on_web_notify: Any = None,
        listen_port: int = 18790,
    ) -> None:
        self.router = router
        self.supervisor = supervisor
        self.truth_writer = truth_writer or UserScopedTruthWriter(truth_db_path=Path("/dev/null"))
        self.identity = IdentityVerifier(cookie_secret, router.base_domain)
        self.on_web_notify = on_web_notify
        self.listen_port = listen_port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=50 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._handle_request)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.listen_port)
        await site.start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

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
            return web.Response(status=410, text=EXPIRED_HTML, content_type="text/html")

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
                {"ok": True, "title": route.title, "summary": route.summary or route.title, "mode": route.mode}
            )
        if request.path == "/_maibot/sdk.js" and request.method == "GET":
            return web.Response(text=MAIBOT_SDK_JS, content_type="application/javascript")
        if request.path.startswith("/_maibot/auth/"):
            return await self._handle_auth_endpoints(request, route)
        if request.path == "/_maibot/notify" and request.method == "POST":
            return await self._handle_maibot_notify(request, route)

        if route.mode == "backend":
            return await self._proxy_to_backend(request, route)
        return self._serve_static_file(request, route)

    async def _handle_auth_endpoints(self, request: web.Request, route: SubdomainRoute) -> web.Response:
        actor_wxid = self.identity.verify_cookie(request.cookies.get("maibot_wxid"))
        is_private_chat = not route.chat_id.endswith("@chatroom")

        if request.path == "/_maibot/auth/whoami":
            auto_bind_private = False
            if not actor_wxid and is_private_chat and route.creator_wxid:
                actor_wxid = route.creator_wxid
                auto_bind_private = True

            members = self.truth_writer.list_known_members()
            pin_users = self.router.list_pin_users()
            seen = {m["wxid"] for m in members}
            for pu in pin_users:
                if pu["wxid"] not in seen:
                    members.append(pu)
            nick_map = {m["wxid"]: m["nickname"] for m in members}
            resp = web.json_response(
                {
                    "ok": True,
                    "wxid": actor_wxid,
                    "nickname": nick_map.get(actor_wxid or "", actor_wxid),
                    "isCreator": bool(actor_wxid and actor_wxid == route.creator_wxid),
                    "accessPolicy": route.access_policy,
                    "members": members,
                }
            )
            if auto_bind_private and actor_wxid:
                self.identity.attach_cookie(resp, actor_wxid)
            return resp

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
            pin = str(body.get("pin") or "").strip()
            if not wxid or not self.router.verify_user_pin(wxid, pin):
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
        if not actor_wxid and not route.chat_id.endswith("@chatroom") and route.creator_wxid:
            actor_wxid = route.creator_wxid

        if not actor_wxid:
            return web.json_response(
                {"ok": False, "require_auth": True, "error": "请先验证你的微信身份后再提交"},
                status=401,
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
            try:
                await self.on_web_notify(route, actor_wxid, event_name, detail, applied_changes, allowed_proactive)
            except TypeError:
                await self.on_web_notify(route, event_name, detail, allowed_proactive)
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
        rel_path = request.path.lstrip("/") or "index.html"
        target = (dist_root / rel_path).resolve()
        if not str(target).startswith(str(dist_root)) or not target.exists():
            target = dist_root / "index.html"
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

    async def _proxy_to_backend(self, request: web.Request, route: SubdomainRoute) -> web.StreamResponse:
        start_cmd = json.loads(route.start_cmd_json or "[]")
        try:
            port = await self.supervisor.ensure_running(route, start_cmd)
        except Exception as exc:
            return web.Response(status=502, text=f"Backend Auto-Wake Failed: {exc}")

        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._proxy_websocket(request, port)

        target_url = f"http://127.0.0.1:{port}{request.path_qs}"
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        data = await request.read()

        async with aiohttp.ClientSession() as session:
            async with session.request(
                request.method, target_url, headers=headers, data=data, allow_redirects=False
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

        async with aiohttp.ClientSession() as session:
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
