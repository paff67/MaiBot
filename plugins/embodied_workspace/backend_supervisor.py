from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .cli_bridge import SANDBOX_WORKSPACE_PATH, build_bwrap_prefix, build_sandbox_env
from .resource_quota import ResourceQuotaWrapper
from .subdomain_router import SubdomainRoute


@dataclass
class ManagedBackend:
    token: str
    unit_name: str
    port: int
    process: asyncio.subprocess.Process
    last_active_at: float


class BackendSupervisor:
    """管理 mode='backend' 的受控 bwrap 子进程，支持预热启动、15 分钟无访问休眠（Scale-to-Zero）与请求触发冷启动（Auto-Wake）"""

    def __init__(
        self,
        workspaces_root: Path,
        port_min: int = 19001,
        port_max: int = 19199,
        idle_timeout_sec: float = 900.0,
        quota_wrapper: ResourceQuotaWrapper | None = None,
        cli_auth_dir: str = "~/.gemini",
    ) -> None:
        self.workspaces_root = workspaces_root
        self.port_min = port_min
        self.port_max = port_max
        self.idle_timeout_sec = idle_timeout_sec
        self.quota_wrapper = quota_wrapper
        self.cli_auth_dir = cli_auth_dir
        self._active: dict[str, ManagedBackend] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._janitor_task: asyncio.Task | None = None

    def start_janitor(self) -> None:
        if self._janitor_task is None:
            self._janitor_task = asyncio.create_task(self._janitor_loop())

    async def stop_all(self) -> None:
        if self._janitor_task:
            self._janitor_task.cancel()
            try:
                await self._janitor_task
            except asyncio.CancelledError:
                pass
            self._janitor_task = None
        for token in list(self._active.keys()):
            await self.stop_backend(token)

    def _allocate_free_port(self) -> int:
        used = {b.port for b in self._active.values()}
        for port in range(self.port_min, self.port_max + 1):
            if port in used:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return port
        raise RuntimeError("后端动态端口池已耗尽")

    async def ensure_running(self, route: SubdomainRoute, start_cmd_template: list[str]) -> int:
        """确保指定 token 的后端进程处于运行态并监听端口（若已休眠则自动冷启动）"""
        lock = self._locks.setdefault(route.token, asyncio.Lock())
        async with lock:
            existing = self._active.get(route.token)
            if existing and existing.process.returncode is None:
                existing.last_active_at = time.time()
                return existing.port

            port = self._allocate_free_port()
            project_path = Path(route.project_dir).resolve()
            chat_ws_dir = project_path.parents[1]
            rel_project_dir = f"{SANDBOX_WORKSPACE_PATH}/projects/{route.project_slug}"
            unit_name = f"maibot-fairy-be-{route.token}"

            env = build_sandbox_env(port=port, require_api_key=False)
            actual_cmd = [arg.replace("{PORT}", str(port)) for arg in start_cmd_template]

            if shutil.which("bwrap"):
                raw_bwrap_cmd = build_bwrap_prefix(
                    chat_ws_dir,
                    workdir=rel_project_dir,
                    cli_auth_dir=self.cli_auth_dir,
                    sandbox_env=env,
                )
                raw_bwrap_cmd.extend(actual_cmd)
                launch_cmd = (
                    self.quota_wrapper.wrap_command(raw_bwrap_cmd, unit_name=unit_name)
                    if self.quota_wrapper
                    else raw_bwrap_cmd
                )
                exec_cwd: str | None = None
            elif os.name == "nt":
                mapped_cmd = list(actual_cmd)
                if mapped_cmd and mapped_cmd[0] in ("python3", "python", "python.exe", "python3.exe"):
                    mapped_cmd[0] = sys.executable
                launch_cmd = mapped_cmd
                exec_cwd = str(project_path)
            else:
                raise RuntimeError("宿主机未安装 bwrap，拒绝裸机启动不可信后端进程")

            popen_kwargs: dict[str, object] = {
                "stdout": asyncio.subprocess.DEVNULL,
                "stderr": asyncio.subprocess.DEVNULL,
                "env": env,
            }
            if exec_cwd is not None:
                popen_kwargs["cwd"] = exec_cwd
            if os.name != "nt":
                popen_kwargs["start_new_session"] = True

            proc = await asyncio.create_subprocess_exec(*launch_cmd, **popen_kwargs)

            ready = await self._wait_port_ready(port, proc, timeout=8.0)
            if not ready:
                await self._terminate_unit(unit_name, proc)
                raise RuntimeError(f"后端服务未能及时监听端口 {port}")

            self._active[route.token] = ManagedBackend(
                token=route.token,
                unit_name=unit_name,
                port=port,
                process=proc,
                last_active_at=time.time(),
            )
            return port

    async def _wait_port_ready(self, port: int, proc: asyncio.subprocess.Process, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.returncode is not None:
                return False
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                return True
            except OSError:
                await asyncio.sleep(0.12)
        return False

    async def stop_backend(self, token: str) -> None:
        item = self._active.pop(token, None)
        if item:
            await self._terminate_unit(item.unit_name, item.process)

    async def _terminate_unit(self, unit_name: str, proc: asyncio.subprocess.Process) -> None:
        if self.quota_wrapper is not None:
            await self.quota_wrapper.stop_unit(unit_name, proc)
            return
        await self._kill_proc(proc)

    @staticmethod
    async def _kill_proc(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.5)
            except Exception:
                pass

    async def _janitor_loop(self) -> None:
        while True:
            await asyncio.sleep(60.0)
            now = time.time()
            for token, item in list(self._active.items()):
                if now - item.last_active_at > self.idle_timeout_sec or item.process.returncode is not None:
                    await self.stop_backend(token)
