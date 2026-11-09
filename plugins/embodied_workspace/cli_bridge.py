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
from .editorial_kit import ensure_maibot_kit_assets
from .resource_quota import ResourceQuotaWrapper
from .time_machine import WorkspaceTimeMachine

SANDBOX_WORKSPACE_PATH = "/tmp/workspace"
SANDBOX_HOME_PATH = "/tmp/home"


@dataclass
class WorkspaceJobRequest:
    job_id: str
    stream_id: str
    chat_id: str
    target_id: str = ""
    source_message_id: str = ""
    creator_wxid: str = ""
    access_policy: str = "creator_only"        # "creator_only" | "member_collect"
    project_slug: str = "app"
    action: str = "create"               # "create" | "update"
    task_type: str = "webapp"            # "document" | "webapp" | "backend"
    task_prompt: str = ""
    context_notes: str = ""
    staged_files: list[Path] = field(default_factory=list)
    ttl_hours: int = 24


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
        timeout_seconds: int = 10800,
        progress_report_interval_seconds: int = 180,
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
        self.progress_report_interval_seconds = progress_report_interval_seconds
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

    def resolve_latest_source_message_id(self, chat_id: str, sender_wxid: str = "") -> str:
        """从 message_ledger.db 只读反查触发本次慢轨任务的最新入站 msg_id"""
        if not self.ledger_db_path.exists():
            return ""
        try:
            conn = sqlite3.connect(f"file:{self.ledger_db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                cols = {
                    str(r["name"])
                    for r in conn.execute("PRAGMA table_info(messages)").fetchall()
                }
                if "msg_id" not in cols:
                    return ""
                if sender_wxid and "sender_id" in cols:
                    row = conn.execute(
                        "SELECT msg_id FROM messages WHERE chat_id = ? AND sender_id = ? ORDER BY rowid DESC LIMIT 1",
                        (chat_id, sender_wxid),
                    ).fetchone()
                    if row and row["msg_id"]:
                        return str(row["msg_id"])
                row = conn.execute(
                    "SELECT msg_id FROM messages WHERE chat_id = ? ORDER BY rowid DESC LIMIT 1",
                    (chat_id,),
                ).fetchone()
                return str(row["msg_id"]) if (row and row["msg_id"]) else ""
            finally:
                conn.close()
        except Exception:
            return ""

    def _fetch_recent_chat_excerpt(self, chat_id: str, limit: int = 15) -> str:
        """只读提取当前会话最近 15 条消息摘要写入 TASK_CONTEXT.md，严守跨会话隔离"""
        if not self.ledger_db_path.exists():
            return "（无近期对话记录）"
        try:
            conn = sqlite3.connect(f"file:{self.ledger_db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                cols = {
                    str(r["name"])
                    for r in conn.execute("PRAGMA table_info(messages)").fetchall()
                }
                if not cols:
                    return "（无近期对话记录）"
                sender_col = "sender_name" if "sender_name" in cols else ("sender_id" if "sender_id" in cols else "''")
                text_col = (
                    "body" if "body" in cols
                    else ("raw_message" if "raw_message" in cols
                    else ("content" if "content" in cols else "''"))
                )
                dir_col = "direction" if "direction" in cols else "''"
                rows = conn.execute(
                    f"""
                    SELECT {sender_col} AS sender, {dir_col} AS direction, {text_col} AS body
                    FROM messages
                    WHERE chat_id = ? AND {text_col} != ''
                    ORDER BY rowid DESC LIMIT ?
                    """,
                    (chat_id, int(limit)),
                ).fetchall()
                lines: list[str] = []
                for r in reversed(rows):
                    role = "Bot" if str(r["direction"]) == "outbound" else str(r["sender"] or "User")
                    body = str(r["body"] or "").replace("\n", " ").strip()[:280]
                    lines.append(f"- [{role}]: {body}")
                return "\n".join(lines) if lines else "（无近期对话记录）"
            finally:
                conn.close()
        except Exception:
            return "（读取近期对话记录失败）"

    def _write_task_context_md(self, ws_dir: Path, req: WorkspaceJobRequest) -> None:
        recent_chat = self._fetch_recent_chat_excerpt(req.chat_id, limit=15)
        content = f"""# Workspace Task Context (TASK_CONTEXT.md)

- **job_id**: `{req.job_id}`
- **task_type**: `{req.task_type}` (`document` | `webapp` | `backend`)
- **action**: `{req.action}`
- **project_slug**: `{req.project_slug}`
- **access_policy**: `{req.access_policy}`

## 1. 补充说明与上下文备注 (`context_notes`)
{req.context_notes or "（无额外备注）"}

## 2. 当前会话近期对话摘录（供理解任务背景与上下文指代）
{recent_chat}

## 3. 核心任务需求 (`task_prompt`)
{req.task_prompt}
"""
        (ws_dir / "TASK_CONTEXT.md").write_text(content, encoding="utf-8")

    def _write_agents_md(self, ws_dir: Path, slug: str, access_policy: str, task_type: str = "webapp") -> None:
        ws_root = SANDBOX_WORKSPACE_PATH
        content = f"""# MaiBot Embodied Workspace Agent Contract (Slow Track Builder)

你运行在当前微信会话的隔离工作区 `{ws_root}` 中，负责执行全部慢轨交付任务（长文撰写与排版、静态交互网页构建、数据分析及受控全栈应用）。
当前任务类型：`{task_type}` | 页面权限策略：`{access_policy}`。

## 一、 上下文读取与目录结构
1. **请先读取 `{ws_root}/TASK_CONTEXT.md`**：获取当前会话近期对话摘录、补充说明与核心任务需求。
2. **目录约定**：
   - `{ws_root}/inbox/`：当前会话已授权的微信附件原件（只读）。
   - `{ws_root}/projects/{slug}/`：源码、Python 数据处理脚本、Markdown 原稿与后端工程目录（持久化保留，支持多轮增量迭代与 Git 快照回滚）。
   - `{ws_root}/dist/{slug}/`：最终静态或前端交付物输出目录（入口固定为 `{ws_root}/dist/{slug}/index.html`）。
   - `{ws_root}/dist/{slug}/files/`：**Google Drive 自动归档目录**。放入此目录的衍生文件（如长文 `.md` 原稿、Excel/PDF、图表 PNG）会在任务结束时自动归档至 Google Drive `workspace_archives/` 独立目录。
   - `{ws_root}/.maibot_kit/`（及 `{ws_root}/dist/{slug}/_kit/`）：预置的杂志级排版样式表 `editorial_theme.css` 与交互增强脚本 `editorial_kit.js`。

## 二、 三类交付物的构建与渲染规范

### 1. 长文本与深度排版 (`task_type == "document"`)
当任务是撰写或整理长文章、攻略、报告、复盘或文档时：
- **双格式产出**：将完整的 Markdown 原稿保存至 `{ws_root}/dist/{slug}/files/{slug}.md`，同时生成排版精美的 `{ws_root}/dist/{slug}/index.html`。
- **样式与组件引用**：在 `index.html` 中可直接引用 `<link rel="stylesheet" href="./_kit/editorial_theme.css">` 与 `<script src="./_kit/editorial_kit.js" defer></script>`（自动提供顶部阅读进度条、代码块一键复制与多章节浮动目录 TOC）。
- **富文本支持**：数学公式使用 KaTeX CDN 渲染，流程图/时序图使用 Mermaid CDN 渲染，并在页面底部提供 `./files/{slug}.md` 原稿下载入口。

### 2. 交互式单页与数据可视化 (`task_type == "webapp"`)
- 生成 `{ws_root}/dist/{slug}/index.html`，所有资源使用相对路径或公共 CDN（Tailwind / ECharts / Chart.js）。
- 若涉及附件数据处理，先在 `{ws_root}/projects/{slug}/` 下编写并运行 Python 脚本得出真实计算结果，再注入前端展示，严禁捏造虚假数据。

### 3. 受控临时后端应用 (`task_type == "backend"`)
- **严禁自行使用 `nohup` 或 `&` 在后台常驻进程**。
- 将后端代码（FastAPI / Node）写在 `{ws_root}/projects/{slug}/`，并在 `{ws_root}/dist/{slug}/result.json` 中声明 `"mode": "backend"` 与 `"start_cmd": ["python3", "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "{{PORT}}"]`。

## 三、 移动端适配与宿主前端 SDK (`window.MaiBot`)
1. 页面必须适配 `375px~430px` 手机视口（无横向溢出滚动条），包含 `<meta name="viewport" content="width=device-width, initial-scale=1.0">` 与 `<title>`；**严禁使用 `<input type="password">`**（如需输入码请用 `type="tel"` 或 `type="text"`）。
2. 所有 HTML 页面已由网关自动注入 `/_maibot/sdk.js`：
   - 实名回调与记忆更新：`await window.MaiBot.notify({{ event: "...", detail: "...", trigger_reply: true, memory_patch: {{...}} }})`
   - 群聊全双工实时消息与同屏广播：`window.MaiBot.subscribeRealtime((msg) => {{...}})` 与 `window.MaiBot.broadcast(event, data)`

## 四、 必须生成的回执文件 `{ws_root}/dist/{slug}/result.json`
```json
{{
  "title": "页面中文标题",
  "summary_for_replyer": "两句话概括核心内容、分析结论或已实现的功能",
  "mode": "static",
  "start_cmd": [],
  "archive_files": []
}}
```
"""
        (ws_dir / "AGENTS.md").write_text(content, encoding="utf-8")

    @staticmethod
    def inspect_project_progress(
        project_dir: Path,
        dist_dir: Path,
        recent_lines: list[str],
        elapsed_sec: int,
    ) -> str:
        """根据沙箱目录产物与最新日志摘要客观提炼当前任务进度。"""
        if (dist_dir / "index.html").exists():
            return "已生成初版页面 (index.html)，正在完善样式排版与检查交付项"

        if project_dir.exists():
            entries = [
                p for p in project_dir.iterdir()
                if p.name not in ("build.log", ".git") and not p.name.startswith(".")
            ]
            if entries:
                entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                newest = entries[0]

                data_files = [p for p in entries if p.suffix.lower() in (".json", ".csv", ".xml", ".parquet")]
                script_files = [p for p in entries if p.suffix.lower() in (".py", ".sh", ".js", ".ts")]
                doc_files = [p for p in entries if p.suffix.lower() in (".md", ".txt", ".html")]

                if doc_files and doc_files[0].stat().st_size > 0:
                    return f"正在撰写长文正文与结构排版（最新产物：{doc_files[0].name}）"

                if data_files and data_files[0].stat().st_size > 0:
                    size_kb = max(1, data_files[0].stat().st_size // 1024)
                    return f"已采集关键研究素材（已生成 {data_files[0].name} 约 {size_kb}KB），正在进行数据清洗与交叉提炼"

                if script_files:
                    return f"正在编写并执行数据检索与调研脚本（最新脚本：{script_files[0].name}）"

                return f"正在工作区进行代码与资料处理（最新文件：{newest.name}）"

        if recent_lines:
            last_line = recent_lines[-1]
            clean_line = re.sub(r"\x1b\[[0-9;]*m", "", last_line).strip()
            if clean_line:
                return f"正在后台执行研究任务（最新动态：{clean_line[:80]}）"

        elapsed_min = max(1, elapsed_sec // 60)
        return f"沙箱正在执行中（已运行 {elapsed_min} 分钟），正在进行任务规划与网络检索"

    async def run_cli_job(
        self,
        req: WorkspaceJobRequest,
        progress_callback: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> WorkspaceJobResult:
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

            ensure_maibot_kit_assets(ws_dir, dist_dir)
            self._write_task_context_md(ws_dir, req)
            self._write_agents_md(ws_dir, slug, req.access_policy, req.task_type)

            ws_root = SANDBOX_WORKSPACE_PATH
            files_note = (
                "可用微信附件：" + ", ".join(f"`{ws_root}/inbox/{p.name}`" for p in req.staged_files)
                if req.staged_files else "无关联微信附件。"
            )
            full_prompt = (
                f"任务类型: {req.task_type} | 操作类型: {req.action} | 权限模式: {req.access_policy} | "
                f"项目目录: `{ws_root}/projects/{slug}` | 产物目录: `{ws_root}/dist/{slug}`\n"
                f"{files_note}\n\n"
                f"用户需求:\n{req.task_prompt}\n\n"
                f"请严格遵守 `{ws_root}/TASK_CONTEXT.md` 与 `{ws_root}/AGENTS.md` 规范，完成后务必写入 `{ws_root}/dist/{slug}/result.json`。"
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
                    stderr=asyncio.subprocess.STDOUT,
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

            build_log = project_dir / "build.log"
            recent_lines: list[str] = []

            async def _stream_reader() -> None:
                try:
                    with open(build_log, "a", encoding="utf-8", errors="ignore") as lf:
                        while True:
                            line = await proc.stdout.readline()
                            if not line:
                                break
                            text = line.decode("utf-8", errors="ignore")
                            lf.write(text)
                            lf.flush()
                            clean = text.strip()
                            if clean:
                                recent_lines.append(clean)
                                if len(recent_lines) > 50:
                                    recent_lines.pop(0)
                except Exception:
                    pass

            reader_task = asyncio.create_task(_stream_reader())

            async def _progress_monitor_loop() -> None:
                start_time = time.monotonic()
                last_report_time = start_time
                last_reported_status = ""
                initial_delay = min(self.progress_report_interval_seconds, 60)
                await asyncio.sleep(initial_delay)
                while proc.returncode is None:
                    now = time.monotonic()
                    elapsed_sec = int(now - start_time)
                    if (now - last_report_time) >= self.progress_report_interval_seconds:
                        status = self.inspect_project_progress(project_dir, dist_dir, recent_lines, elapsed_sec)
                        if status and status != last_reported_status:
                            last_reported_status = status
                            last_report_time = now
                            if progress_callback:
                                try:
                                    await progress_callback(status, elapsed_sec)
                                except Exception:
                                    pass
                    await asyncio.sleep(15)

            monitor_task = asyncio.create_task(_progress_monitor_loop()) if progress_callback else None

            try:
                await asyncio.wait_for(proc.wait(), timeout=self.timeout_seconds)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return WorkspaceJobResult(ok=False, error_message=f"构建超时（超过 {self.timeout_seconds} 秒）")
            finally:
                if monitor_task:
                    monitor_task.cancel()
                    try:
                        await monitor_task
                    except asyncio.CancelledError:
                        pass
                try:
                    await reader_task
                except Exception:
                    pass

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
                tail = "\n".join(recent_lines[-10:])[-300:]
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
