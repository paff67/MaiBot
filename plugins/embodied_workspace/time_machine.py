from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SnapshotRecord:
    commit_hash: str
    short_hash: str
    timestamp_iso: str
    subject: str


@dataclass
class RollbackResult:
    ok: bool
    project_slug: str
    target_commit: str = ""
    restored_subject: str = ""
    restored_meta: dict | None = None
    error: str = ""


class WorkspaceTimeMachine:
    """
    在宿主机侧（bwrap 外部）管理每个项目的 Git 增量快照仓库。
    [v3.1 修正] 回滚时先通过 git ls-tree 嗅探目标提交中实际存在的子目录，
    解决从全栈版本回滚至仅含 dist/<slug> 的纯静态版本时抛出 pathspec did not match 的 P0 缺陷。
    """

    def __init__(self, workspaces_root: Path) -> None:
        self.workspaces_root = workspaces_root

    @staticmethod
    def _git_dir(chat_ws_dir: Path, slug: str) -> Path:
        snap_root = chat_ws_dir / "snapshots"
        snap_root.mkdir(parents=True, exist_ok=True)
        return snap_root / f"{slug}.git"

    async def _run_git(self, chat_ws_dir: Path, slug: str, args: list[str]) -> tuple[int, str, str]:
        git_dir = self._git_dir(chat_ws_dir, slug)
        cmd = [
            "git",
            f"--git-dir={git_dir}",
            f"--work-tree={chat_ws_dir}",
            "-c", "user.name=MaiBot TimeMachine",
            "-c", "user.email=workspace@maibot.local",
            "-c", "commit.gpgsign=false",
            *args,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="ignore").strip(),
            stderr.decode("utf-8", errors="ignore").strip(),
        )

    async def ensure_repo(self, chat_ws_dir: Path, slug: str) -> None:
        git_dir = self._git_dir(chat_ws_dir, slug)
        if not (git_dir / "HEAD").exists():
            await self._run_git(chat_ws_dir, slug, ["init", "-q"])

    async def capture_snapshot(
        self,
        chat_ws_dir: Path,
        slug: str,
        *,
        job_id: str,
        action: str,
        summary: str = "",
    ) -> SnapshotRecord | None:
        await self.ensure_repo(chat_ws_dir, slug)

        tracked_targets = [f"projects/{slug}", f"dist/{slug}"]
        existing_targets = [sub for sub in tracked_targets if (chat_ws_dir / sub).exists()]
        if not existing_targets:
            return None

        # 使用 add -A 捕获新增、修改与被删除的文件
        await self._run_git(chat_ws_dir, slug, ["add", "-A", "--", *existing_targets])
        _, status_out, _ = await self._run_git(
            chat_ws_dir, slug, ["status", "--porcelain", "--", *tracked_targets]
        )

        if status_out:
            clean_summary = (summary or action).replace("\n", " ").strip()[:72]
            msg = f"[{action}] {job_id}: {clean_summary}"
            code, _, _ = await self._run_git(chat_ws_dir, slug, ["commit", "-q", "-m", msg])
            if code != 0:
                return None

        history = await self.list_snapshots(chat_ws_dir, slug, limit=1)
        return history[0] if history else None

    async def get_latest_diff_stat(self, chat_ws_dir: Path, slug: str) -> str:
        history = await self.list_snapshots(chat_ws_dir, slug, limit=2)
        if len(history) < 2:
            return "initial snapshot"
        code, out, _ = await self._run_git(
            chat_ws_dir, slug, ["diff", "--shortstat", "HEAD~1", "HEAD"]
        )
        return out if (code == 0 and out) else "updated"

    async def list_snapshots(
        self, chat_ws_dir: Path, slug: str, limit: int = 10
    ) -> list[SnapshotRecord]:
        git_dir = self._git_dir(chat_ws_dir, slug)
        if not (git_dir / "HEAD").exists():
            return []
        code, out, _ = await self._run_git(
            chat_ws_dir,
            slug,
            ["log", f"-n{int(limit)}", "--pretty=format:%H|%h|%cI|%s"],
        )
        if code != 0 or not out:
            return []
        records: list[SnapshotRecord] = []
        for line in out.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                records.append(
                    SnapshotRecord(
                        commit_hash=parts[0],
                        short_hash=parts[1],
                        timestamp_iso=parts[2],
                        subject=parts[3],
                    )
                )
        return records

    async def rollback(
        self,
        chat_ws_dir: Path,
        slug: str,
        target_spec: str = "1",
    ) -> RollbackResult:
        history = await self.list_snapshots(chat_ws_dir, slug, limit=30)
        if not history:
            return RollbackResult(ok=False, project_slug=slug, error="该项目尚无任何历史快照记录")

        spec = target_spec.strip() or "1"
        target_commit = ""
        target_subject = ""

        if spec.isdigit():
            steps = int(spec)
            if steps < 1 or steps >= len(history):
                return RollbackResult(
                    ok=False,
                    project_slug=slug,
                    error=f"可回退步数超出范围（当前共有 {len(history)} 个快照版本，最多可回退 {len(history) - 1} 步）",
                )
            target_commit = history[steps].commit_hash
            target_subject = history[steps].subject
        else:
            for rec in history:
                if rec.commit_hash.startswith(spec) or rec.short_hash == spec:
                    target_commit = rec.commit_hash
                    target_subject = rec.subject
                    break
            if not target_commit:
                return RollbackResult(ok=False, project_slug=slug, error=f"未找到匹配的快照版本号: {spec}")

        # 1. 先从暂存区和物理工作树清理当前目录
        tracked_targets = [f"projects/{slug}", f"dist/{slug}"]
        for sub in tracked_targets:
            await self._run_git(chat_ws_dir, slug, ["rm", "-rf", "--ignore-unmatch", "-q", "--", sub])
            phys_dir = chat_ws_dir / sub
            if phys_dir.exists():
                await asyncio.to_thread(shutil.rmtree, phys_dir, True)

        # 2. [v3.1 修正] 通过 git ls-tree 检查目标 commit 中实际包含哪些目录，仅检出存在的路径
        checkout_paths: list[str] = []
        for sub in tracked_targets:
            ls_code, tree_out, _ = await self._run_git(
                chat_ws_dir, slug, ["ls-tree", "--name-only", target_commit, "--", sub]
            )
            if ls_code == 0 and tree_out.strip():
                checkout_paths.append(sub)

        if checkout_paths:
            code, _, err = await self._run_git(
                chat_ws_dir,
                slug,
                ["checkout", target_commit, "--", *checkout_paths],
            )
            if code != 0:
                return RollbackResult(ok=False, project_slug=slug, error=f"Git 恢复失败: {err}")

        # 确保两个标准子目录在磁盘上至少存在空目录结构，防止后续读取异常
        for sub in tracked_targets:
            (chat_ws_dir / sub).mkdir(parents=True, exist_ok=True)

        await self.capture_snapshot(
            chat_ws_dir,
            slug,
            job_id=f"rb_{target_commit[:7]}",
            action="rollback",
            summary=f"回滚至 {target_commit[:7]} ({target_subject})",
        )

        result_file = chat_ws_dir / "dist" / slug / "result.json"
        restored_meta: dict = {}
        if result_file.exists():
            try:
                restored_meta = json.loads(result_file.read_text(encoding="utf-8"))
            except Exception:
                restored_meta = {}

        return RollbackResult(
            ok=True,
            project_slug=slug,
            target_commit=target_commit[:7],
            restored_subject=target_subject,
            restored_meta=restored_meta,
        )
