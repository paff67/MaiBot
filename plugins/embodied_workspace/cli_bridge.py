from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from src.companion_vault.access_policy import authorized_file_rows
from .editorial_kit import ensure_maibot_kit_assets
from .resource_quota import ResourceQuotaWrapper
from .time_machine import WorkspaceTimeMachine

SANDBOX_WORKSPACE_PATH = "/tmp/workspace"
SANDBOX_HOME_PATH = "/tmp/home"
DEFAULT_SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
FAIRY_AGENTS_TEMPLATE_PATH = Path(__file__).parent / "templates" / "FAIRY_AGENTS.md"


@dataclass
class WorkspaceJobRequest:
    job_id: str
    stream_id: str
    chat_id: str
    target_id: str = ""
    source_message_id: str = ""
    creator_wxid: str = ""
    issuer_name: str = ""
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


def build_sandbox_env(
    *,
    port: int | None = None,
    require_api_key: bool = True,
) -> dict[str, str]:
    """
    严格白名单环境变量构造器：绝不复制宿主 os.environ 全量字典，绝不硬编码任何明文密钥。
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH") or DEFAULT_SANDBOX_PATH,
        "HOME": SANDBOX_HOME_PATH,
        "XDG_CACHE_HOME": f"{SANDBOX_HOME_PATH}/.cache",
        "XDG_CONFIG_HOME": f"{SANDBOX_HOME_PATH}/.config",
        "LANG": "C.UTF-8",
        "TZ": "Asia/Tokyo",
    }
    if port is not None:
        env["PORT"] = str(port)
    if os.name == "nt":
        for win_key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
            val = os.environ.get(win_key)
            if val:
                env[win_key] = val

    api_key = (
        os.environ.get("FAIRY_SANDBOX_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or ""
    ).strip()
    base_url = (
        os.environ.get("FAIRY_SANDBOX_BASE_URL")
        or os.environ.get("GOOGLE_GEMINI_BASE_URL")
        or ""
    ).strip()

    if require_api_key:
        if not api_key:
            raise RuntimeError(
                "沙盒缺少网关凭据：请在宿主受控环境中配置 FAIRY_SANDBOX_API_KEY（或 GEMINI_API_KEY）"
            )
        env["GEMINI_API_KEY"] = api_key
        env["OPENAI_API_KEY"] = api_key
        if base_url:
            env["GOOGLE_GEMINI_BASE_URL"] = base_url
            env["OPENAI_BASE_URL"] = base_url

    return env


def build_bwrap_prefix(
    chat_ws_dir: Path,
    workdir: str = SANDBOX_WORKSPACE_PATH,
    *,
    cli_auth_dir: str = "~/.gemini",
    sandbox_env: dict[str, str] | None = None,
) -> list[str]:
    effective_env = dict(sandbox_env) if sandbox_env is not None else {
        "PATH": os.environ.get("PATH") or DEFAULT_SANDBOX_PATH,
        "HOME": SANDBOX_HOME_PATH,
        "XDG_CACHE_HOME": f"{SANDBOX_HOME_PATH}/.cache",
        "XDG_CONFIG_HOME": f"{SANDBOX_HOME_PATH}/.config",
        "LANG": "C.UTF-8",
        "TZ": "Asia/Tokyo",
    }

    cmd = [
        "bwrap",
        "--clearenv",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--dir", SANDBOX_WORKSPACE_PATH,
        "--bind", str(chat_ws_dir.resolve()), SANDBOX_WORKSPACE_PATH,
        "--tmpfs", SANDBOX_HOME_PATH,
    ]
    for env_key, env_val in effective_env.items():
        cmd.extend(["--setenv", str(env_key), str(env_val)])

    # 透传配置指定的 CLI 认证与设置只读挂载（避免硬编码 /root/.gemini）
    auth_root = Path(cli_auth_dir).expanduser()
    gemini_config_path = auth_root / "config"
    gemini_settings_path = auth_root / "antigravity-cli" / "settings.json"
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


class CLIBridge:
    def __init__(
        self,
        workspaces_root: Path,
        truth_db_path: Path,
        ledger_db_path: Path,
        wechat_source: str,
        wechat_account: str,
        cli_backend: str = "agy",
        fairy_cli_model: str = "gemini-4-argon-high",
        cli_auth_dir: str = "~/.gemini",
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
        self.fairy_cli_model = fairy_cli_model.strip() or "gemini-4-argon-high"
        self.cli_auth_dir = cli_auth_dir.strip() or "~/.gemini"
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

    def project_lock(self, chat_id: str, project_slug: str) -> asyncio.Lock:
        key = (self.safe_id(chat_id), self.safe_id(project_slug))
        return self._project_locks.setdefault(key, asyncio.Lock())

    def is_project_locked(self, chat_id: str, project_slug: str) -> bool:
        key = (self.safe_id(chat_id), self.safe_id(project_slug))
        lock = self._project_locks.get(key)
        return bool(lock and lock.locked())

    def get_job_inbox_dir(self, chat_id: str, job_id: str) -> Path:
        inbox_root = (self.get_chat_workspace(chat_id) / "inbox").resolve()
        job_dir = (inbox_root / self.safe_id(job_id)).resolve()
        if not job_dir.is_relative_to(inbox_root):
            raise ValueError(f"非法任务暂存目录标识: {job_id!r}")
        return job_dir

    def cleanup_job_inbox(self, chat_id: str, job_id: str) -> None:
        if not job_id:
            return
        try:
            job_dir = self.get_job_inbox_dir(chat_id, job_id)
            if job_dir.exists():
                shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass

    def stage_authorized_files(
        self,
        destination_chat_id: str,
        file_codes: list[str],
        *,
        job_id: str = "manual",
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

        inbox_dir = self.get_job_inbox_dir(destination_chat_id, job_id)
        inbox_dir.mkdir(parents=True, exist_ok=True)
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
                try:
                    os.link(src_path, dest_path)
                except OSError:
                    shutil.copy2(src_path, dest_path)
            staged.append(dest_path)
        return staged

    def _build_ledger_scope(self, chat_id: str) -> tuple[str, list[object]]:
        clauses = ["chat_id = ?"]
        params: list[object] = [chat_id]
        if self.wechat_source:
            clauses.append("source = ?")
            params.append(self.wechat_source)
        if self.wechat_account:
            clauses.append("account = ?")
            params.append(self.wechat_account)
        return " AND ".join(clauses), params

    def resolve_latest_source_message_id(self, chat_id: str, sender_wxid: str = "") -> str:
        """从 message_ledger.db 只读反查触发本次慢轨任务的最新入站 msg_id（严格使用 MessageLedger 真实列）"""
        if not self.ledger_db_path.exists():
            return ""
        try:
            conn = sqlite3.connect(f"file:{self.ledger_db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                where_sql, params = self._build_ledger_scope(chat_id)
                not_revoked = "(CAST(COALESCE(kind, '0') AS INTEGER) & 65535) != 10000"
                if sender_wxid:
                    row = conn.execute(
                        f"SELECT msg_id FROM messages WHERE {where_sql} AND sender_id = ? AND {not_revoked} ORDER BY rowid DESC LIMIT 1",
                        (*params, sender_wxid),
                    ).fetchone()
                    if row and row["msg_id"]:
                        return str(row["msg_id"])
                row = conn.execute(
                    f"SELECT msg_id FROM messages WHERE {where_sql} AND {not_revoked} ORDER BY rowid DESC LIMIT 1",
                    tuple(params),
                ).fetchone()
                return str(row["msg_id"]) if (row and row["msg_id"]) else ""
            finally:
                conn.close()
        except Exception:
            return ""

    def _fetch_recent_chat_excerpt(self, chat_id: str, limit: int = 15) -> str:
        """只读提取当前会话最近 15 条未撤回消息摘要写入 TASK_CONTEXT.md，严守跨会话隔离"""
        if not self.ledger_db_path.exists():
            return "（无近期对话记录）"
        try:
            conn = sqlite3.connect(f"file:{self.ledger_db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                where_sql, params = self._build_ledger_scope(chat_id)
                rows = conn.execute(
                    f"""
                    SELECT sender_id, direction, body
                    FROM messages
                    WHERE {where_sql}
                      AND COALESCE(body, '') != ''
                      AND (CAST(COALESCE(kind, '0') AS INTEGER) & 65535) != 10000
                    ORDER BY rowid DESC LIMIT ?
                    """,
                    (*params, int(limit)),
                ).fetchall()
                lines: list[str] = []
                for r in reversed(rows):
                    role = "Bot" if str(r["direction"]) in ("outgoing", "outbound") else str(r["sender_id"] or "User")
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

    def _write_agents_md(
        self,
        ws_dir: Path,
        slug: str,
        access_policy: str,
        task_type: str = "webapp",
        job_id: str = "manual",
    ) -> None:
        ws_root = SANDBOX_WORKSPACE_PATH
        inbox_dir = f"{ws_root}/inbox/{self.safe_id(job_id)}"
        template_text = FAIRY_AGENTS_TEMPLATE_PATH.read_text(encoding="utf-8")
        content = (
            template_text.replace("{ws_root}", ws_root)
            .replace("{inbox_dir}", inbox_dir)
            .replace("{slug}", slug)
            .replace("{access_policy}", access_policy)
            .replace("{task_type}", task_type)
        )
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

    @staticmethod
    def validate_html_artifact_warnings(index_file: Path) -> list[str]:
        """在宿主侧收尾校验 HTML 产物，提供轻量规范检查而不客户端篡改 DOM。"""
        if not index_file.is_file():
            return []
        warnings: list[str] = []
        try:
            html_text = index_file.read_text(encoding="utf-8", errors="ignore")
            for block in re.findall(
                r'<pre[^>]*class=["\'][^"\']*mermaid[^"\']*["\'][^>]*>(.*?)</pre>',
                html_text,
                re.DOTALL | re.IGNORECASE,
            ):
                if re.search(r'--\s*"[^"\n]+"?\s*-->', block):
                    warnings.append("Mermaid 块使用了非标准 `-- \"label\" -->` 箭头标注，建议使用 `-->|label|`")
                    break
        except Exception:
            pass
        return warnings

    async def _stop_cli_unit(self, unit_name: str, proc: asyncio.subprocess.Process) -> None:
        if self.quota_wrapper is not None:
            await self.quota_wrapper.stop_unit(unit_name, proc)
            return
        if proc.returncode is None:
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

    async def run_cli_job(
        self,
        req: WorkspaceJobRequest,
        progress_callback: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> WorkspaceJobResult:
        ws_dir = self.get_chat_workspace(req.chat_id)
        slug = self.safe_id(req.project_slug)
        safe_job_id = self.safe_id(req.job_id)
        lock = self.project_lock(req.chat_id, slug)

        try:
            async with lock:
                try:
                    sandbox_env = build_sandbox_env(require_api_key=True)
                except RuntimeError as exc:
                    return WorkspaceJobResult(ok=False, error_message=str(exc))

                project_dir = ws_dir / "projects" / slug
                dist_dir = ws_dir / "dist" / slug
                project_dir.mkdir(parents=True, exist_ok=True)
                dist_dir.mkdir(parents=True, exist_ok=True)
                (dist_dir / "files").mkdir(parents=True, exist_ok=True)

                ensure_maibot_kit_assets(ws_dir, dist_dir)
                self._write_task_context_md(ws_dir, req)
                self._write_agents_md(ws_dir, slug, req.access_policy, req.task_type, job_id=safe_job_id)

                ws_root = SANDBOX_WORKSPACE_PATH
                files_note = (
                    "可用微信附件："
                    + ", ".join(f"`{ws_root}/inbox/{safe_job_id}/{p.name}`" for p in req.staged_files)
                    if req.staged_files
                    else "无关联微信附件。"
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

                raw_cmd = build_bwrap_prefix(
                    ws_dir,
                    workdir=ws_root,
                    cli_auth_dir=self.cli_auth_dir,
                    sandbox_env=sandbox_env,
                )
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
                        "--model", self.fairy_cli_model,
                        "-p", full_prompt,
                    ])

                unit_name = f"maibot-fairy-cli-{safe_job_id}"
                cmd = (
                    self.quota_wrapper.wrap_command(raw_cmd, unit_name=unit_name)
                    if self.quota_wrapper
                    else raw_cmd
                )

                popen_kwargs: dict[str, object] = {
                    "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.STDOUT,
                    "env": sandbox_env,
                }
                if os.name != "nt":
                    popen_kwargs["start_new_session"] = True

                try:
                    proc = await asyncio.create_subprocess_exec(*cmd, **popen_kwargs)
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
                    await self._stop_cli_unit(unit_name, proc)
                    return WorkspaceJobResult(ok=False, error_message=f"构建超时（超过 {self.timeout_seconds} 秒）")
                except asyncio.CancelledError:
                    await self._stop_cli_unit(unit_name, proc)
                    raise
                finally:
                    if monitor_task:
                        try:
                            monitor_task.cancel()
                            await monitor_task
                        except (asyncio.CancelledError, RuntimeError, Exception):
                            pass
                    try:
                        if not reader_task.done():
                            reader_task.cancel()
                        await reader_task
                    except (asyncio.CancelledError, RuntimeError, Exception):
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

                artifact_warnings = self.validate_html_artifact_warnings(index_file)
                if artifact_warnings:
                    with open(build_log, "a", encoding="utf-8", errors="ignore") as lf:
                        for w in artifact_warnings:
                            lf.write(f"\n[maibot-artifact-check] {w}\n")

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
        finally:
            self.cleanup_job_inbox(req.chat_id, safe_job_id)
