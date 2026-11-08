from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from src.companion_vault.access_policy import authorized_file_rows

SANDBOX_WORKSPACE_PATH = "/tmp/workspace"
SANDBOX_HOME_PATH = "/tmp/home"


@dataclass
class WorkspaceJobRequest:
    job_id: str
    stream_id: str
    chat_id: str
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
    dist_dir: Path | None = None
    project_dir: Path | None = None
    error_message: str = ""


def build_bwrap_prefix(chat_ws_dir: Path, workdir: str = SANDBOX_WORKSPACE_PATH) -> list[str]:
    """
    构造纯命名空间自闭环的 bwrap 隔离前缀（解决只读 / 无法创建挂载点与 $HOME 只读崩溃）：
    1. 先只读挂载 /，并将 /tmp 挂载为可写 tmpfs；
    2. 在可写的 /tmp 下通过 --dir 创建 /tmp/workspace 并绑定当前会话工作区；
    3. 分配独立的 --tmpfs /tmp/home 并注入 HOME、XDG_* 环境变量供 CLI 写入缓存与会话状态；
    4. 对实际存在的敏感目录与文件（/opt/maibot-brain, /etc/maibot-mtls, /etc/maibot-media.env）执行 tmpfs 遮蔽。
    """
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
    for p in ("/opt/maibot-brain", "/etc/maibot-mtls"):
        if Path(p).exists():
            cmd.extend(["--tmpfs", p])
    if Path("/etc/maibot-media.env").exists():
        cmd.extend(["--ro-bind", "/dev/null", "/etc/maibot-media.env"])

    cmd.extend([
        "--share-net",
        "--chdir", workdir,
        "--die-with-parent",
        "--",
    ])
    return cmd


def build_sandbox_env() -> dict[str, str]:
    """构建传递给 bwrap 子进程的环境变量，显式保留模型 API Key 并重定向 HOME"""
    env = dict(os.environ)
    env["HOME"] = SANDBOX_HOME_PATH
    env["XDG_CACHE_HOME"] = f"{SANDBOX_HOME_PATH}/.cache"
    env["XDG_CONFIG_HOME"] = f"{SANDBOX_HOME_PATH}/.config"
    return env


class CLIBridge:
    def __init__(
        self,
        workspaces_root: Path,
        truth_db_path: Path,
        ledger_db_path: Path,
        wechat_source: str,
        wechat_account: str,
        cli_backend: str = "codex",
        timeout_seconds: int = 180,
    ) -> None:
        self.workspaces_root = workspaces_root
        self.truth_db_path = truth_db_path
        self.ledger_db_path = ledger_db_path
        self.wechat_source = wechat_source
        self.wechat_account = wechat_account
        self.cli_backend = cli_backend
        self.timeout_seconds = timeout_seconds
        self.workspaces_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def safe_id(raw: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "_", raw.strip())
        return cleaned[:64] or "default"

    def get_chat_workspace(self, chat_id: str) -> Path:
        ws = self.workspaces_root / self.safe_id(chat_id)
        for sub in ("inbox", "projects", "dist"):
            (ws / sub).mkdir(parents=True, exist_ok=True)
        return ws

    def stage_authorized_files(
        self,
        destination_chat_id: str,
        file_codes: list[str],
        source: str | None = None,
        account: str | None = None,
    ) -> list[Path]:
        """
        严格对齐 authorized_file_rows(db, ledger, *, source, account, destination_chat_id, codes) 签名，
        并从 asset_sources 表查询真实 local_path。
        """
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
                    "SELECT local_path, sha256 FROM asset_sources WHERE file_code = ? ORDER BY rowid DESC LIMIT 1",
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

    def _write_agents_md(self, ws_dir: Path, slug: str) -> None:
        ws_root = SANDBOX_WORKSPACE_PATH
        content = f"""# MaiBot Session Workspace Agent Contract

你运行在当前微信会话的专属隔离工作区 `{ws_root}` 中。

## 1. 目录约定
- `{ws_root}/inbox/`：当前会话已授权的微信附件原件（只读）。
- `{ws_root}/projects/{slug}/`：源码、Python 数据处理脚本、SQLite 数据库与后端工程目录（持久化保留，支持多轮增量迭代）。
- `{ws_root}/dist/{slug}/`：静态网页构建产物输出目录。

## 2. 动静双模交付契约（二选一）
### 模式 A：纯静态模式 (`"mode": "static"`，默认优先)
适用于单人数据可视化报告、交互式图表(ECharts/Chart.js)、纯前端 Canvas 小游戏、文档整理。
- 必须生成 `{ws_root}/dist/{slug}/index.html`（使用相对路径或公共 CDN）。
- 衍生可供下载的文件放入 `{ws_root}/dist/{slug}/files/` 并在页面提供下载入口。

### 模式 B：受控临时后端模式 (`"mode": "backend"`)
适用于群友多人实时互动（投票、排行榜、共享账单）、需在网页内上传文件调用服务端 Python 处理、或需避免 CORS 的实时 API 代理。
- **严禁在本次 CLI 执行中用 `nohup` 或 `&` 自行后台常驻进程**（CLI 退出时沙盒会清理子进程）。
- 请将后端代码（如 FastAPI `app.py` 或 Node `server.js`）写在 `{ws_root}/projects/{slug}/` 下，并在 `{ws_root}/dist/{slug}/result.json` 中声明 `start_cmd`（使用占位符 `{{PORT}}` 作为监听端口，运行工作目录固定为 `{ws_root}/projects/{slug}`）。

## 3. 微信内置浏览器 (XWeb) 适配与反向唤醒接口
- 所有前端页面必须遵循 Mobile-First（适配 375px~430px 宽度，无横向滚动条），包含 `<meta name="viewport" content="width=device-width, initial-scale=1.0">` 与 `<title>`。
- 如需在用户完成网页端关键操作后通知微信里的许妍主动冒泡，前端或后端可发送同源请求：
  `POST /_maibot/notify` JSON Body: `{{"event": "用户完成了XX操作", "detail": "具体数据摘要", "trigger_reply": true}}`

## 4. 必须生成的回执文件 `{ws_root}/dist/{slug}/result.json`
任务结束退出前，必须写入以下 JSON：
```json
{{
  "title": "简短中文页面标题",
  "summary_for_replyer": "两句话总结核心结论或已实现的功能（供许妍在微信回复用户时参考）",
  "mode": "static",
  "start_cmd": []
}}
```
（若为后端模式，则设 `"mode": "backend"`，且 `"start_cmd": ["python3", "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "{{PORT}}"]`）
"""
        (ws_dir / "AGENTS.md").write_text(content, encoding="utf-8")

    async def run_cli_job(self, req: WorkspaceJobRequest) -> WorkspaceJobResult:
        ws_dir = self.get_chat_workspace(req.chat_id)
        slug = self.safe_id(req.project_slug)
        project_dir = ws_dir / "projects" / slug
        dist_dir = ws_dir / "dist" / slug
        project_dir.mkdir(parents=True, exist_ok=True)
        dist_dir.mkdir(parents=True, exist_ok=True)

        self._write_agents_md(ws_dir, slug)

        ws_root = SANDBOX_WORKSPACE_PATH
        files_note = (
            "可用微信附件：" + ", ".join(f"`{ws_root}/inbox/{p.name}`" for p in req.staged_files)
            if req.staged_files else "无关联微信附件。"
        )
        full_prompt = (
            f"操作类型: {req.action} | 目标项目目录: `{ws_root}/projects/{slug}` | 产物目录: `{ws_root}/dist/{slug}`\n"
            f"{files_note}\n\n"
            f"用户需求:\n{req.task_prompt}\n\n"
            f"请严格遵守 `{ws_root}/AGENTS.md` 规范，完成后务必写入 `{ws_root}/dist/{slug}/result.json`。"
        )

        cmd = build_bwrap_prefix(ws_dir, workdir=ws_root)
        if self.cli_backend == "codex":
            cmd.extend([
                "codex", "exec", "--full-auto",
                "--sandbox", "workspace-write",
                "-C", ws_root,
                full_prompt,
            ])
        else:
            cmd.extend([
                "agy", "--headless",
                "--workspace", ws_root,
                "--prompt", full_prompt,
            ])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=build_sandbox_env(),
        )
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

        if mode == "static" and not index_file.exists():
            tail = stderr.decode("utf-8", errors="ignore")[-300:]
            return WorkspaceJobResult(ok=False, error_message=f"静态模式未产出 index.html (code={proc.returncode}): {tail}")
        if mode == "backend" and not start_cmd:
            return WorkspaceJobResult(ok=False, error_message="后端模式未在 result.json 中声明有效的 start_cmd")

        return WorkspaceJobResult(
            ok=True,
            title=str(meta.get("title") or req.project_slug),
            summary_for_replyer=str(meta.get("summary_for_replyer") or "已完成应用构建并挂载上线。"),
            mode=mode,
            start_cmd=[str(x) for x in start_cmd],
            dist_dir=dist_dir,
            project_dir=project_dir,
        )
