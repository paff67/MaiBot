from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from src.companion_vault.access_policy import authorized_file_rows
from .resource_quota import ResourceQuotaWrapper
from .time_machine import WorkspaceTimeMachine

SANDBOX_WORKSPACE_PATH = "/tmp/workspace"
SANDBOX_HOME_PATH = "/tmp/home"


@dataclass
class WorkspaceJobRequest:
    job_id: str
    stream_id: str
    chat_id: str
    creator_wxid: str
    access_policy: str        # "creator_only" | "member_collect"
    project_slug: str
    action: str               # "create" | "update"
    task_prompt: str
    staged_files: list[Path]
    ttl_hours: int


@dataclass
class WorkspaceJobResult:
    ok: bool
    title: str = ""
    summary_for_replyer: str = ""
    mode: str = "static"      # "static" | "backend"
    start_cmd: list[str] | None = None
    archive_files: list[str] = field(default_factory=list)
    dist_dir: Path | None = None
    project_dir: Path | None = None
    error_message: str = ""
    snapshot_hash: str = ""
    diff_stat: str = ""


def build_bwrap_prefix(chat_ws_dir: Path, workdir: str = SANDBOX_WORKSPACE_PATH) -> list[str]:
    cmd = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--dir", SANDBOX_WORKSPACE_PATH,
        "--bind", str(chat_ws_dir.resolve()), SANDBOX_WORKSPACE_PATH,
        "--tmpfs", SANDBOX_HOME_PATH,
        "--setenv", "HOME", SANDBOX_HOME_PATH,
        "--setenv", "XDG_CACHE_HOME", f"{SANDBOX_HOME_PATH}/.cache",
        "--setenv", "XDG_CONFIG_HOME", f"{SANDBOX_HOME_PATH}/.config",
    ]

    # 透传宿主机已安装的 Antigravity 插件、配置与授权
    gemini_config_path = Path("/root/.gemini/config")
    gemini_settings_path = Path("/root/.gemini/antigravity-cli/settings.json")
    if gemini_config_path.is_dir():
        cmd.extend([
            "--dir", f"{SANDBOX_HOME_PATH}/.gemini/config",
            "--ro-bind", str(gemini_config_path), f"{SANDBOX_HOME_PATH}/.gemini/config",
        ])
    if gemini_settings_path.is_file():
        cmd.extend([
            "--dir", f"{SANDBOX_HOME_PATH}/.gemini/antigravity-cli",
            "--ro-bind", str(gemini_settings_path), f"{SANDBOX_HOME_PATH}/.gemini/antigravity-cli/settings.json",
        ])

    for masked in ("/opt/maibot-brain", "/etc/maibot-mtls", "/etc/maibot-media.env"):
        p = Path(masked)
        if p.is_dir():
            cmd.extend(["--tmpfs", masked])
        elif p.is_file():
            cmd.extend(["--ro-bind", "/dev/null", masked])

    cmd.extend([
        "--share-net",
        "--chdir", workdir,
        "--die-with-parent",
        "--",
    ])
    return cmd


def build_sandbox_env() -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = SANDBOX_HOME_PATH
    env["XDG_CACHE_HOME"] = f"{SANDBOX_HOME_PATH}/.cache"
    env["XDG_CONFIG_HOME"] = f"{SANDBOX_HOME_PATH}/.config"
    if "GOOGLE_GEMINI_BASE_URL" not in env:
        env["GOOGLE_GEMINI_BASE_URL"] = "https://api.0-0.pro"
    if "GEMINI_API_KEY" not in env:
        env["GEMINI_API_KEY"] = "sk-OGOA7rsiaALlVaZV-JMwBMIwe603yvB6mQraWDW1XI4"
    return env


class CLIBridge:
    def __init__(
        self,
        workspaces_root: Path,
        truth_db_path: Path,
        ledger_db_path: Path,
        wechat_source: str,
        wechat_account: str,
        cli_backend: str = "agy",
        timeout_seconds: int = 180,
        quota_wrapper: ResourceQuotaWrapper | None = None,
        time_machine: WorkspaceTimeMachine | None = None,
    ) -> None:
        self.workspaces_root = workspaces_root
        self.truth_db_path = truth_db_path
        self.ledger_db_path = ledger_db_path
        self.wechat_source = wechat_source
        self.wechat_account = wechat_account
        self.cli_backend = cli_backend
        self.timeout_seconds = timeout_seconds
        self.quota_wrapper = quota_wrapper
        self.time_machine = time_machine
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self._project_locks: dict[tuple[str, str], asyncio.Lock] = {}

    @staticmethod
    def safe_id(raw: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "_", raw.strip())
        return cleaned[:64] or "default"

    def get_chat_workspace(self, chat_id: str) -> Path:
        ws = self.workspaces_root / self.safe_id(chat_id)
        for sub in ("inbox", "projects", "dist"):
            (ws / sub).mkdir(parents=True, exist_ok=True)
        return ws

    def is_project_locked(self, chat_id: str, project_slug: str) -> bool:
        key = (self.safe_id(chat_id), self.safe_id(project_slug))
        lock = self._project_locks.get(key)
        return bool(lock and lock.locked())

    def stage_authorized_files(
        self,
        destination_chat_id: str,
        file_codes: list[str],
        source: str | None = None,
        account: str | None = None,
    ) -> list[Path]:
        if not file_codes:
            return []

        eff_source = source or self.wechat_source
        eff_account = account or self.wechat_account

        conn_truth = sqlite3.connect(f"file:{self.truth_db_path}?mode=ro", uri=True)
        conn_truth.row_factory = sqlite3.Row
        conn_ledger = sqlite3.connect(f"file:{self.ledger_db_path}?mode=ro", uri=True)
        conn_ledger.row_factory = sqlite3.Row

        try:
            allowed_rows = authorized_file_rows(
                conn_truth,
                conn_ledger,
                source=eff_source,
                account=eff_account,
                destination_chat_id=destination_chat_id,
                codes=file_codes,
            )
            allowed_map: dict[str, dict] = {}
            for r in allowed_rows:
                row_dict = dict(r)
                code = str(row_dict["file_code"]).upper()
                source_row = conn_truth.execute(
                    "SELECT local_path, sha256 FROM asset_sources WHERE file_code = ? AND association = 'verified' ORDER BY rowid DESC LIMIT 1",
                    (code,),
                ).fetchone()
                if source_row:
                    allowed_map[code] = {**row_dict, **dict(source_row)}
        finally:
            conn_ledger.close()
            conn_truth.close()

        denied = [c for c in file_codes if c not in allowed_map]
        if denied:
            raise PermissionError(f"当前会话无权访问或不存在附件: {', '.join(denied)}")

        inbox_dir = self.get_chat_workspace(destination_chat_id) / "inbox"
        staged: list[Path] = []
        for code in file_codes:
            info = allowed_map[code]
            src_path = Path(str(info.get("local_path") or ""))
            if not src_path.is_file():
                raise FileNotFoundError(f"附件本地原件未就绪: {code}")
            ext = src_path.suffix or ""
            raw_label = str(
                info.get("original_name")
                or info.get("file_name")
                or info.get("title")
                or "attachment"
            )
            orig_name = self.safe_id(Path(raw_label).stem)
            dest_path = inbox_dir / f"{code}_{orig_name}{ext}"
            if not dest_path.exists():
                shutil.copy2(src_path, dest_path)
            staged.append(dest_path)
        return staged

    def _write_agents_md(self, ws_dir: Path, slug: str, access_policy: str) -> None:
        ws_root = SANDBOX_WORKSPACE_PATH
        content = f"""# MaiBot Session Workspace Agent Contract (Phase 2)

你运行在当前微信会话的专属隔离工作区 `{ws_root}` 中。当前页面权限策略为：`{access_policy}`。

## 1. 目录与 Google Drive 自动归档约定
- `{ws_root}/inbox/`：当前会话已授权的微信附件原件（只读）。
- `{ws_root}/projects/{slug}/`：源码、Python 脚本、SQLite 数据库与后端工程目录（持久化保留，支持多轮增量迭代）。
- `{ws_root}/dist/{slug}/`：静态网页构建产物输出目录。
- `{ws_root}/dist/{slug}/files/`：**Google Drive 自动归档目录**。任何放入此目录的衍生文件（如生成的 Excel、PDF、高清图表 PNG），任务结束时会被宿主自动归档至 Google Drive 独立文件夹并在网页提供下载。

## 2. 动静双模交付契约（二选一）
### 模式 A：纯静态模式 (`"mode": "static"`，默认优先)
- 必须生成 `{ws_root}/dist/{slug}/index.html`。
### 模式 B：受控临时后端模式 (`"mode": "backend"`)
- 适用于多人实时互动（投票、排行榜、信息收集表）、网页上传文件服务端处理或 WebSocket 应用。
- **严禁自行用 `nohup` 或 `&` 后台启动服务**。请将后端写在 `{ws_root}/projects/{slug}/`，并在 `{ws_root}/dist/{slug}/result.json` 中声明 `start_cmd`（端口使用占位符 `{{PORT}}`）。

## 3. 前端身份 SDK (`window.MaiBot`) 与实名真值库回调
宿主网关会自动向所有 HTML 页面注入 `/_maibot/sdk.js`（无需你手写登录弹窗，未登录用户调用时 SDK 会自动弹出微信口令/PIN 登录框）：
- 获取当前访问者身份：`const user = window.MaiBot.getState(); // {{ wxid, nickname, isCreator }}`
- 触发微信回调并更新访问者本人名下的真值库记忆：
  ```javascript
  await window.MaiBot.notify({{
    event: "提交了露营报名与忌口",
    detail: "带天幕，不吃香菜",
    trigger_reply: true,
    memory_patch: {{
      current_status: "准备周末露营",
      preference_item: {{ "饮食忌口": "不吃香菜" }},
      quest_title: "周末露营携带天幕"
    }}
  }});
  ```

- 订阅微信群实时新消息与同页面多人实时广播（Phase 3 全双工总线）：
  ```javascript
  window.MaiBot.subscribeRealtime((msg) => {{
    if (msg.type === "wechat_message") {{
      // 收到当前微信群的新消息: {{ sender, content, ts }}
    }} else if (msg.type === "peer_broadcast" || msg.type === "web_notify") {{
      // 收到其他正在浏览本网页的群友操作广播: {{ actor_wxid, event, data }}
    }}
  }});
  // 向所有正在打开本网页的群友实时广播状态
  window.MaiBot.broadcast("vote_updated", {{ option: "A" }});
  ```

- **微信防拦截规范**：严禁在 HTML 中使用 `<input type="password">`（如需输入码请用 `type="tel"` 或 `type="text"`），严禁出现“输入银行卡/身份证”等敏感字样。

## 4. 必须生成的回执文件 `{ws_root}/dist/{slug}/result.json`
```json
{{
  "title": "简短中文页面标题",
  "summary_for_replyer": "两句话总结核心结论或已实现的功能（供微信卡片摘要与许妍回复使用）",
  "mode": "static",
  "start_cmd": [],
  "archive_files": []
}}
```
"""
        (ws_dir / "AGENTS.md").write_text(content, encoding="utf-8")

    async def run_cli_job(self, req: WorkspaceJobRequest) -> WorkspaceJobResult:
        ws_dir = self.get_chat_workspace(req.chat_id)
        slug = self.safe_id(req.project_slug)
        lock_key = (self.safe_id(req.chat_id), slug)
        lock = self._project_locks.setdefault(lock_key, asyncio.Lock())

        async with lock:
            project_dir = ws_dir / "projects" / slug
            dist_dir = ws_dir / "dist" / slug
            project_dir.mkdir(parents=True, exist_ok=True)
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "files").mkdir(parents=True, exist_ok=True)

            self._write_agents_md(ws_dir, slug, req.access_policy)

            ws_root = SANDBOX_WORKSPACE_PATH
            files_note = (
                "可用微信附件：" + ", ".join(f"`{ws_root}/inbox/{p.name}`" for p in req.staged_files)
                if req.staged_files else "无关联微信附件。"
            )
            full_prompt = (
                f"操作类型: {req.action} | 权限模式: {req.access_policy} | "
                f"项目目录: `{ws_root}/projects/{slug}` | 产物目录: `{ws_root}/dist/{slug}`\n"
                f"{files_note}\n\n"
                f"用户需求:\n{req.task_prompt}\n\n"
                f"请严格遵守 `{ws_root}/AGENTS.md` 规范，完成后务必写入 `{ws_root}/dist/{slug}/result.json`。"
            )

            if self.time_machine and req.action == "update":
                await self.time_machine.capture_snapshot(
                    ws_dir, slug, job_id=f"pre_{req.job_id}", action="pre-update", summary="更新前基线保护"
                )

            raw_cmd = build_bwrap_prefix(ws_dir, workdir=ws_root)
            if self.cli_backend == "codex":
                raw_cmd.extend([
                    "codex", "exec", "--full-auto",
                    "--sandbox", "workspace-write",
                    "-C", ws_root,
                    full_prompt,
                ])
            else:
                raw_cmd.extend([
                    "agy",
                    "--dangerously-skip-permissions",
                    "--model", "gemini-4-argon-high",
                    "-p", full_prompt,
                ])

            cmd = (
                self.quota_wrapper.wrap_command(raw_cmd, unit_prefix=f"maibot-cli-{slug}")
                if self.quota_wrapper
                else raw_cmd
            )

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    env=build_sandbox_env(),
                )
            except FileNotFoundError:
                return WorkspaceJobResult(
                    ok=False,
                    error_message=f"沙箱或构建工具未安装（无法找到执行文件: {cmd[0]} 或 {self.cli_backend}）",
                )
            except Exception as exc:
                return WorkspaceJobResult(ok=False, error_message=f"启动沙箱执行失败: {exc}")

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return WorkspaceJobResult(ok=False, error_message=f"构建超时（超过 {self.timeout_seconds} 秒）")

            result_file = dist_dir / "result.json"
            index_file = dist_dir / "index.html"

            meta: dict = {}
            if result_file.exists():
                try:
                    meta = json.loads(result_file.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}

            mode = str(meta.get("mode") or "static").strip().lower()
            start_cmd = meta.get("start_cmd") if isinstance(meta.get("start_cmd"), list) else []
            archive_files = meta.get("archive_files") if isinstance(meta.get("archive_files"), list) else []

            if mode == "static" and not index_file.exists():
                tail = stderr.decode("utf-8", errors="ignore")[-300:]
                return WorkspaceJobResult(ok=False, error_message=f"静态模式未产出 index.html (code={proc.returncode}): {tail}")
            if mode == "backend" and not start_cmd:
                return WorkspaceJobResult(ok=False, error_message="后端模式未在 result.json 中声明有效的 start_cmd")

            snap_hash = ""
            diff_stat = ""
            if self.time_machine:
                snap = await self.time_machine.capture_snapshot(
                    ws_dir,
                    slug,
                    job_id=req.job_id,
                    action=req.action,
                    summary=str(meta.get("title") or req.project_slug),
                )
                if snap:
                    snap_hash = snap.short_hash
                    diff_stat = await self.time_machine.get_latest_diff_stat(ws_dir, slug)

            return WorkspaceJobResult(
                ok=True,
                title=str(meta.get("title") or req.project_slug),
                summary_for_replyer=str(meta.get("summary_for_replyer") or "已完成应用构建并挂载上线。"),
                mode=mode,
                start_cmd=[str(x) for x in start_cmd],
                archive_files=[str(x) for x in archive_files],
                dist_dir=dist_dir,
                project_dir=project_dir,
                snapshot_hash=snap_hash,
                diff_stat=diff_stat,
            )
