from __future__ import annotations

import os
import shutil
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
    为 bwrap 沙盒进程提供基于 systemd cgroups v2 的细粒度资源硬配额。
    [v3.1 修正] 仅在 system (root) 模式下附加 OOMScoreAdjust，避免 user 模式因权限不足启动失败。
    """

    def __init__(self, limits: QuotaLimits | None = None) -> None:
        self.limits = limits or QuotaLimits()
        self._systemd_mode: str | None = None

    def _detect_systemd_scope_mode(self) -> str:
        if self._systemd_mode is not None:
            return self._systemd_mode

        if not shutil.which("systemd-run"):
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

    def wrap_command(self, base_cmd: list[str], unit_prefix: str = "maibot-ws") -> list[str]:
        if not self.limits.enabled:
            return list(base_cmd)

        mode = self._detect_systemd_scope_mode()
        if mode in ("system", "user"):
            cmd = ["systemd-run"]
            if mode == "user":
                cmd.append("--user")
            cmd.extend([
                "--scope",
                "--quiet",
                "--collect",
                f"--property=CPUQuota={self.limits.cpu_quota_percent}%",
                f"--property=MemoryMax={self.limits.memory_max_mb}M",
                "--property=MemorySwapMax=0",
                f"--property=TasksMax={self.limits.tasks_max}",
            ])
            # [v3.1 修正] 仅在 root (system) 模式下设置正向 OOMScoreAdjust
            if mode == "system":
                cmd.append(f"--property=OOMScoreAdjust={self.limits.oom_score_adjust}")
            cmd.append("--")
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
