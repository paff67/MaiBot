from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass


@dataclass
class QuotaLimits:
    enabled: bool = True
    cpu_quota_percent: int = 150      # 150% = 1.5 核上限
    memory_max_mb: int = 1024         # 1024MB 物理内存硬上限
    tasks_max: int = 256              # 最大子进程/线程数
    oom_score_adjust: int = 500       # 优先被内核 OOM Killer 回收，保护主脑


class ResourceQuotaWrapper:
    """
    为 bwrap 沙盒进程提供基于 systemd cgroups v2 的细粒度资源硬配额与确定性单元回收。
    """

    def __init__(self, limits: QuotaLimits | None = None) -> None:
        self.limits = limits or QuotaLimits()
        self._systemd_mode: str | None = None

    @staticmethod
    def sanitize_unit_name(unit_name: str) -> str:
        raw = unit_name.strip()
        if raw.endswith(".scope"):
            raw = raw[:-6]
        cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "-", raw)
        cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
        return cleaned[:96] or "maibot-fairy-scope"

    def _detect_systemd_scope_mode(self) -> str:
        if self._systemd_mode is not None:
            return self._systemd_mode

        if not shutil.which("systemd-run") or not hasattr(os, "geteuid"):
            self._systemd_mode = "none"
            return self._systemd_mode

        is_root = os.geteuid() == 0
        probe_args = ["systemd-run"]
        if not is_root:
            probe_args.append("--user")
        probe_args.extend([
            "--scope",
            "--quiet",
            "--collect",
            f"--property=TasksMax={self.limits.tasks_max}",
            "--",
            "true",
        ])

        try:
            res = subprocess.run(
                probe_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
            )
            if res.returncode == 0:
                self._systemd_mode = "system" if is_root else "user"
                return self._systemd_mode
        except Exception:
            pass

        self._systemd_mode = "none"
        return self._systemd_mode

    def wrap_command(self, base_cmd: list[str], unit_name: str = "maibot-fairy-scope") -> list[str]:
        if not self.limits.enabled:
            return list(base_cmd)

        clean_unit = self.sanitize_unit_name(unit_name)
        mode = self._detect_systemd_scope_mode()
        if mode in ("system", "user"):
            cmd = ["systemd-run"]
            if mode == "user":
                cmd.append("--user")

            mem_high_mb = int(self.limits.memory_max_mb * 0.85)
            cmd.extend([
                "--scope",
                f"--unit={clean_unit}",
                "--quiet",
                "--collect",
                f"--property=CPUQuota={self.limits.cpu_quota_percent}%",
                f"--property=MemoryHigh={mem_high_mb}M",
                f"--property=MemoryMax={self.limits.memory_max_mb}M",
                "--property=MemorySwapMax=0",
                f"--property=TasksMax={self.limits.tasks_max}",
            ])
            cmd.append("--")

            if hasattr(os, "geteuid") and os.geteuid() == 0 and self.limits.oom_score_adjust > 0 and shutil.which("choom"):
                cmd.extend(["choom", "-n", str(self.limits.oom_score_adjust), "--"])

            cmd.extend(base_cmd)
            return cmd

        if shutil.which("prlimit"):
            return [
                "prlimit",
                f"--nproc={self.limits.tasks_max}",
                "--",
                *base_cmd,
            ]

        return list(base_cmd)

    async def stop_unit(
        self,
        unit_name: str,
        proc: asyncio.subprocess.Process | None = None,
    ) -> None:
        """
        优先通过 systemctl kill --kill-who=all <unit>.scope 原子清理整个 cgroup v2 进程树，
        配合 os.killpg / proc.kill 兜底，确保 setsid 脱离的子进程也被彻底回收。
        """
        clean_unit = self.sanitize_unit_name(unit_name)
        scope_unit = f"{clean_unit}.scope"
        mode = self._detect_systemd_scope_mode() if self.limits.enabled else "none"

        if mode in ("system", "user") and shutil.which("systemctl"):
            kill_cmd = ["systemctl"]
            if mode == "user":
                kill_cmd.append("--user")
            kill_cmd.extend(["kill", "--kill-who=all", "--signal=SIGKILL", scope_unit])
            try:
                killer = await asyncio.create_subprocess_exec(
                    *kill_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=3.0)
            except Exception:
                pass

        if proc is not None and proc.returncode is None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=1.5)
            except Exception:
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.5)
                except Exception:
                    pass

