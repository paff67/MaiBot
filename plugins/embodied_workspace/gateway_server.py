from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
from aiohttp import web

from .backend_supervisor import BackendSupervisor
from .subdomain_router import SubdomainRoute, SubdomainRouter

EXPIRED_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>链接已过期</title><style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:90vh;margin:0;background:#f8fafc;color:#334155;text-align:center}.card{background:#fff;padding:2rem;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,.06);max-width:340px}</style></head>
<body><div class="card"><h3>该临时页面已过期或已撤销</h3><p>为保护隐私与释放算力，限时工作区链接已下线。你可以在微信里让许妍重新生成或唤醒它。</p></div></body></html>"""


class WorkspaceGatewayServer:
    """监听 127.0.0.1:18790，由 Caddy 泛域名反代接入，根据 Host 子域名分流静态目录、按需唤醒后端及微信通知回调"""

    def __init__(
        self,
        router: SubdomainRouter,
        supervisor: BackendSupervisor,
        on_web_notify: Callable[[SubdomainRoute, str, str, bool], Awaitable[None]],
        listen_port: int = 18790,
    ) -> None:
        self.router = router
        self.supervisor = supervisor
        self.on_web_notify = on_web_notify
        self.listen_port = listen_port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=25 * 1024 * 1024)
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
        suffix = f".{self.router.base_domain}"
        if not host_clean.endswith(suffix):
            return None
        token = host_clean[: -len(suffix)]
        return token if token and "." not in token else None

    async def _handle_request(self, request: web.Request) -> web.StreamResponse:
        token = self._extract_token(request.headers.get("Host", ""))
        if not token:
            return web.Response(status=404, text="Invalid Subdomain")

        route = self.router.get_route(token)
        if not route or route.revoked or route.expire_at < time.time():
            return web.Response(status=410, text=EXPIRED_HTML, content_type="text/html")

        if request.path == "/_maibot/notify" and request.method == "POST":
            return await self._handle_maibot_notify(request, route)

        if route.mode == "backend":
            return await self._proxy_to_backend(request, route)
        return self._serve_static_file(request, route)

    async def _handle_maibot_notify(self, request: web.Request, route: SubdomainRoute) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        event_name = str(body.get("event") or "网页交互事件")[:120]
        detail = str(body.get("detail") or "")[:1000]
        want_reply = bool(body.get("trigger_reply", True))

        allowed_proactive = want_reply and self.router.check_and_touch_notify_cooldown(route.token)
        await self.on_web_notify(route, event_name, detail, allowed_proactive)
        return web.json_response({"ok": True, "proactive_triggered": allowed_proactive})

    def _serve_static_file(self, request: web.Request, route: SubdomainRoute) -> web.StreamResponse:
        dist_root = Path(route.dist_dir).resolve()
        rel_path = request.path.lstrip("/") or "index.html"
        target = (dist_root / rel_path).resolve()
        if not str(target).startswith(str(dist_root)) or not target.exists():
            target = dist_root / "index.html"
        if not target.is_file():
            return web.Response(status=404, text="File Not Found")
        ctype, _ = mimetypes.guess_type(str(target))
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

        target_url = f"http://127.0.0.1:{port}{request.path_qs}"
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        body = await request.read()
        async with aiohttp.ClientSession() as session:
            async with session.request(
                request.method, target_url, headers=headers, data=body, allow_redirects=False
            ) as resp:
                raw = await resp.read()
                out_headers = {
                    k: v for k, v in resp.headers.items()
                    if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
                }
                out_headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
                return web.Response(status=resp.status, body=raw, headers=out_headers)
