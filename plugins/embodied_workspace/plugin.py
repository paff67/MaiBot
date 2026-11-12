from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from maibot_sdk import (
    Command,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ToolParameterInfo, ToolParamType
from pydantic import Field

from .archive_manager import WorkspaceArchiveManager
from .backend_supervisor import BackendSupervisor
from .cli_bridge import CLIBridge, WorkspaceJobRequest
from .compute_economy import ComputeEconomyManager
from .gateway_server import WorkspaceGatewayServer, resolve_or_create_cookie_secret
from .realtime_hub import GroupRealtimeHub
from .resource_quota import QuotaLimits, ResourceQuotaWrapper
from .subdomain_router import SubdomainRoute, SubdomainRouter
from .time_machine import WorkspaceTimeMachine
from .user_truth_writer import UserScopedTruthWriter


class PluginSectionConfig(PluginConfigBase):
    config_version: str = Field(default="1.5.0", description="插件配置版本")


class EmbodiedWorkspaceConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig, description="插件元配置")
    base_domain: str = Field(default="workspace.local", description="交付基础域名（单级通配符）")
    short_link_base_url: str = Field(default="", description="短链接基础域名（例如 https://xy.pages.dev）")
    cookie_secret: str = Field(default="", description="HMAC Cookie 签名秘钥（留空则在首次启动时自动生成随机密钥）")
    default_issuer_name: str = Field(default="AI 助手", description="工作区页面默认署名角色显示名")
    gateway_port: int = Field(default=18790, description="本地网关监听端口")
    default_ttl_hours: int = Field(default=24, description="默认链接有效期（小时）")
    cli_backend: str = Field(default="agy", description="执行引擎：agy 或 codex")
    fairy_cli_model: str = Field(default="gemini-4-argon-high", description="慢轨编程 CLI 使用的模型名称")
    cli_auth_dir: str = Field(default="~/.gemini", description="CLI 鉴权配置与插件根目录")
    cli_timeout_seconds: int = Field(default=10800, description="代码执行超时时间（秒，默认 3 小时 = 10800 秒，最小 30 秒）")
    progress_report_interval_seconds: int = Field(default=180, description="慢轨长任务期间快轨巡检与回报的最小间隔秒数（默认 180 秒）")
    workspaces_root: str = Field(default="data/workspaces", description="会话工作区根目录")
    truth_db_path: str = Field(default="data/companion_truth.db", description="真值库路径")
    ledger_db_path: str = Field(default="data/message_ledger.db", description="消息账本路径")
    router_db_path: str = Field(default="data/workspaces/subdomain_routes.db", description="路由数据库路径")
    gdrive_archive_root: str = Field(default="data/workspace_archives", description="衍生文件归档目录")
    wechat_source: str = Field(default="", description="微信驱动数据源地址")
    wechat_account: str = Field(default="", description="微信账号目录哈希")
    enable_cgroup_quota: bool = Field(default=True, description="是否启用 cgroups v2 进程配额限制")
    cgroup_cpu_quota_percent: int = Field(default=150, description="CPU 配额百分比（150% = 1.5 核）")
    cgroup_memory_max_mb: int = Field(default=1024, description="物理内存硬上限 (MB)")
    cgroup_tasks_max: int = Field(default=256, description="最大进程/线程数")
    cgroup_oom_score_adjust: int = Field(default=500, description="OOMScoreAdjust 优先级")


class EmbodiedWorkspacePlugin(MaiBotPlugin):
    config_model = EmbodiedWorkspaceConfig
    config_reload_subscriptions = ("bot",)

    def __init__(self) -> None:
        super().__init__()
        self.set_plugin_config({})
        self.quota_wrapper: ResourceQuotaWrapper | None = None
        self.time_machine: WorkspaceTimeMachine | None = None
        self.realtime_hub: GroupRealtimeHub | None = None
        self.compute_economy: ComputeEconomyManager | None = None
        self._bot_nickname: str = ""
        self._notified_job_ids: set[str] = set()
        self._active_jobs: dict[str, dict[str, Any]] = {}
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    def _effective_timeout_seconds(self) -> int:
        raw = self.config.cli_timeout_seconds
        val = 10800 if raw is None else int(raw)
        return max(30, val)

    def _resolve_issuer_name(self, kwargs: dict[str, Any] | None = None) -> str:
        if kwargs:
            explicit = str(kwargs.get("issuer_name") or "").strip()
            if explicit:
                return explicit
        if self._bot_nickname:
            return self._bot_nickname
        return (self.config.default_issuer_name or "").strip() or "AI 助手"

    async def on_load(self) -> None:
        ws_root = Path(self.config.workspaces_root)
        truth_path = Path(self.config.truth_db_path)

        self._notified_job_ids = set()
        self._active_jobs = {}
        self._bg_tasks = set()

        self.compute_economy = ComputeEconomyManager(
            truth_db_path=truth_path,
        )
        self.compute_economy.start_background_sync()

        self.router = SubdomainRouter(
            db_path=Path(self.config.router_db_path),
            base_domain=self.config.base_domain,
            short_link_base_url=self.config.short_link_base_url,
        )
        self.truth_writer = UserScopedTruthWriter(
            truth_db_path=truth_path,
            wechat_source=self.config.wechat_source,
            wechat_account=self.config.wechat_account,
        )
        self.archive_mgr = WorkspaceArchiveManager(
            gdrive_archive_root=Path(self.config.gdrive_archive_root),
        )

        quota_limits = QuotaLimits(
            enabled=self.config.enable_cgroup_quota,
            cpu_quota_percent=self.config.cgroup_cpu_quota_percent,
            memory_max_mb=self.config.cgroup_memory_max_mb,
            tasks_max=self.config.cgroup_tasks_max,
            oom_score_adjust=self.config.cgroup_oom_score_adjust,
        )
        self.quota_wrapper = ResourceQuotaWrapper(quota_limits)
        self.time_machine = WorkspaceTimeMachine(ws_root)
        self.realtime_hub = GroupRealtimeHub(
            Path(self.config.ledger_db_path),
            wechat_source=self.config.wechat_source,
            wechat_account=self.config.wechat_account,
        )
        self.realtime_hub.start()

        self.cli_bridge = CLIBridge(
            workspaces_root=ws_root,
            truth_db_path=truth_path,
            ledger_db_path=Path(self.config.ledger_db_path),
            wechat_source=self.config.wechat_source,
            wechat_account=self.config.wechat_account,
            cli_backend=self.config.cli_backend,
            fairy_cli_model=self.config.fairy_cli_model,
            cli_auth_dir=self.config.cli_auth_dir,
            timeout_seconds=self._effective_timeout_seconds(),
            progress_report_interval_seconds=self.config.progress_report_interval_seconds,
            quota_wrapper=self.quota_wrapper,
            time_machine=self.time_machine,
        )
        self.supervisor = BackendSupervisor(
            workspaces_root=ws_root,
            quota_wrapper=self.quota_wrapper,
            cli_auth_dir=self.config.cli_auth_dir,
        )
        self.supervisor.start_janitor()

        self.gateway = WorkspaceGatewayServer(
            router=self.router,
            supervisor=self.supervisor,
            truth_writer=self.truth_writer,
            cookie_secret=self.config.cookie_secret,
            workspaces_root=ws_root,
            default_issuer_name=self._resolve_issuer_name(),
            on_web_notify=self._handle_web_notify_callback,
            listen_port=self.config.gateway_port,
            realtime_hub=self.realtime_hub,
        )
        await self.gateway.start()

    async def on_unload(self) -> None:
        pending_tasks = [t for t in list(self._bg_tasks) if not t.done()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        self._active_jobs.clear()
        self._sync_active_jobs_to_truth_db()

        if hasattr(self, "compute_economy") and self.compute_economy:
            await self.compute_economy.stop_background_sync()
        if hasattr(self, "realtime_hub") and self.realtime_hub:
            await self.realtime_hub.stop()
        if hasattr(self, "gateway"):
            await self.gateway.stop()
        if hasattr(self, "supervisor"):
            await self.supervisor.stop_all()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新回调：同步插件与机器人署名配置至各底层组件。"""
        _ = version
        if scope == "bot" and isinstance(config_data, dict):
            nick = str(config_data.get("nickname") or "").strip()
            if nick:
                self._bot_nickname = nick
                if hasattr(self, "gateway") and self.gateway:
                    self.gateway.default_issuer_name = nick
            return

        if scope == "self" and isinstance(config_data, dict):
            self.set_plugin_config(config_data)
            ws_root = Path(self.config.workspaces_root)
            if hasattr(self, "compute_economy") and self.compute_economy:
                self.compute_economy.truth_db_path = Path(self.config.truth_db_path)
            if hasattr(self, "truth_writer") and self.truth_writer:
                self.truth_writer.truth_db_path = Path(self.config.truth_db_path)
                self.truth_writer.wechat_source = self.config.wechat_source
                self.truth_writer.wechat_account = self.config.wechat_account
            if hasattr(self, "archive_mgr") and self.archive_mgr:
                self.archive_mgr.gdrive_archive_root = Path(self.config.gdrive_archive_root)
            if hasattr(self, "router") and self.router:
                self.router.base_domain = self.config.base_domain.strip().lstrip(".")
                self.router.short_link_base_url = self.config.short_link_base_url.strip().rstrip("/")
            if hasattr(self, "quota_wrapper") and self.quota_wrapper:
                self.quota_wrapper.limits.enabled = self.config.enable_cgroup_quota
                self.quota_wrapper.limits.cpu_quota_percent = self.config.cgroup_cpu_quota_percent
                self.quota_wrapper.limits.memory_max_mb = self.config.cgroup_memory_max_mb
                self.quota_wrapper.limits.tasks_max = self.config.cgroup_tasks_max
                self.quota_wrapper.limits.oom_score_adjust = self.config.cgroup_oom_score_adjust
                self.quota_wrapper._systemd_mode = None
            if hasattr(self, "realtime_hub") and self.realtime_hub:
                self.realtime_hub.ledger_db_path = Path(self.config.ledger_db_path)
                self.realtime_hub.wechat_source = self.config.wechat_source.strip()
                self.realtime_hub.wechat_account = self.config.wechat_account.strip()
            if hasattr(self, "cli_bridge") and self.cli_bridge:
                self.cli_bridge.workspaces_root = ws_root
                self.cli_bridge.truth_db_path = Path(self.config.truth_db_path)
                self.cli_bridge.ledger_db_path = Path(self.config.ledger_db_path)
                self.cli_bridge.wechat_source = self.config.wechat_source
                self.cli_bridge.wechat_account = self.config.wechat_account
                self.cli_bridge.cli_backend = self.config.cli_backend
                self.cli_bridge.fairy_cli_model = self.config.fairy_cli_model.strip() or "gemini-4-argon-high"
                self.cli_bridge.cli_auth_dir = self.config.cli_auth_dir.strip() or "~/.gemini"
                self.cli_bridge.timeout_seconds = self._effective_timeout_seconds()
                self.cli_bridge.progress_report_interval_seconds = int(self.config.progress_report_interval_seconds or 180)
            if hasattr(self, "supervisor") and self.supervisor:
                self.supervisor.workspaces_root = ws_root
                self.supervisor.cli_auth_dir = self.config.cli_auth_dir.strip() or "~/.gemini"
            if hasattr(self, "gateway") and self.gateway:
                resolved_secret = resolve_or_create_cookie_secret(self.config.cookie_secret, ws_root)
                self.gateway.identity.secret_key = resolved_secret.encode("utf-8")
                parts = self.router.base_domain.split(".")
                self.gateway.identity.cookie_domain = (
                    "." + ".".join(parts[-2:]) if len(parts) >= 2 else self.router.base_domain
                )
                self.gateway.default_issuer_name = self._resolve_issuer_name()

    @staticmethod
    def _resolve_chat_id(kwargs: dict[str, Any]) -> str:
        group_id = str(kwargs.get("group_id") or "").strip()
        user_id = str(kwargs.get("user_id") or "").strip()
        if group_id:
            return group_id if group_id.endswith("@chatroom") else f"{group_id}@chatroom"
        return user_id

    def _resolve_sender_nickname(self, kwargs: dict[str, Any], sender_wxid: str) -> str:
        msg = kwargs.get("message")
        if isinstance(msg, dict):
            msg_info = msg.get("message_info")
            if isinstance(msg_info, dict):
                user_info = msg_info.get("user_info")
                if isinstance(user_info, dict):
                    nick = str(user_info.get("user_nickname") or user_info.get("user_cardname") or "").strip()
                    if nick:
                        return nick
        if hasattr(self, "truth_writer") and self.truth_writer and sender_wxid:
            pub = self.truth_writer.get_public_nickname(sender_wxid)
            if pub:
                return pub
        return sender_wxid

    @staticmethod
    def _extract_named_group(kwargs: dict[str, Any], group_name: str, fallback_index: int = 1) -> str:
        groups = kwargs.get("matched_groups")
        if isinstance(groups, dict):
            val = groups.get(group_name)
            if val is not None and str(val).strip():
                return str(val).strip()
        raw_text = str(kwargs.get("text") or "").strip()
        if not raw_text:
            return ""
        parts = raw_text.split()
        return parts[fallback_index].strip() if len(parts) > fallback_index else ""

    @Tool(
        name="run_agent_workspace",
        description=(
            "【全能慢轨·代码与全栈网页/长文工作区】当需要撰写/排版长文文档(task_type='document')、"
            "执行 Python/Node 代码、制作交互式 HTML/图表/小游戏(task_type='webapp')、"
            "构建带后端的多人投票/收集表(task_type='backend')、"
            "分析当前会话的微信附件(FMxxxx)、或对之前生成的项目进行多轮修改(action='update')时调用。"
            "本工具立即返回 task_id（非阻塞），后台完成后会自动唤醒你把结果链接发给用户。"
        ),
        parameters=[
            ToolParameterInfo(
                name="task_prompt",
                param_type=ToolParamType.STRING,
                description="给后台 Fairy 编程 Agent 的详细需求描述",
                required=True,
            ),
            ToolParameterInfo(
                name="project_slug",
                param_type=ToolParamType.STRING,
                description="项目英文短标识（如 'doc-tokyo-guide' 或 'camp-vote'；修改已有项目时传入相同标识）",
                required=True,
            ),
            ToolParameterInfo(
                name="action",
                param_type=ToolParamType.STRING,
                description="操作类型：'create' 新建 或 'update' 增量修改",
                required=False,
                default="create",
            ),
            ToolParameterInfo(
                name="access_policy",
                param_type=ToolParamType.STRING,
                description="权限策略：'creator_only'（默认，仅创建者本人可修改）或 'member_collect'（群成员协作收集/投票，允许每位成员实名更新各自记录）",
                required=False,
                default="creator_only",
            ),
            ToolParameterInfo(
                name="task_type",
                param_type=ToolParamType.STRING,
                description="交付类型：'document'（长文撰写与杂志级排版） | 'webapp'（交互单页与图表应用） | 'backend'（受控临时后端应用）",
                required=False,
                default="webapp",
            ),
            ToolParameterInfo(
                name="context_notes",
                param_type=ToolParamType.STRING,
                description="补充说明或对话背景备注（供写入 TASK_CONTEXT.md）",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="file_codes",
                param_type=ToolParamType.STRING,
                description="需读取的微信附件编号，英文逗号分隔如 'FM0012,FM0015'",
                required=False,
                default="",
            ),
        ],
    )
    async def run_agent_workspace(
        self,
        task_prompt: str,
        project_slug: str,
        action: str = "create",
        access_policy: str = "creator_only",
        task_type: str = "webapp",
        context_notes: str = "",
        file_codes: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        chat_id = self._resolve_chat_id(kwargs)
        creator_wxid = str(kwargs.get("user_id") or "").strip()
        stream_id = str(kwargs.get("stream_id") or kwargs.get("session_id") or kwargs.get("chat_id") or chat_id).strip()
        target_id = str(kwargs.get("target_id") or kwargs.get("group_id") or kwargs.get("user_id") or chat_id).strip()
        source_message_id = str(kwargs.get("source_message_id") or kwargs.get("msg_id") or "").strip()
        if not source_message_id and hasattr(self, "cli_bridge") and self.cli_bridge:
            source_message_id = self.cli_bridge.resolve_latest_source_message_id(chat_id, creator_wxid)

        if self.cli_bridge.is_project_locked(chat_id, project_slug):
            return {
                "ok": False,
                "error": f"项目 {project_slug} 当前正在后台构建或回滚中，请等待完成后再提交新修改。",
            }

        job_id = f"job_{int(time.time())}_{secrets.token_hex(3)}"
        codes = [c.strip().upper() for c in file_codes.split(",") if c.strip()]
        try:
            staged = self.cli_bridge.stage_authorized_files(
                destination_chat_id=chat_id,
                file_codes=codes,
                job_id=job_id,
            )
        except Exception as exc:
            self.cli_bridge.cleanup_job_inbox(chat_id, job_id)
            return {"ok": False, "error": f"附件权限校验或准备失败：{exc}"}

        effective_task_type = task_type if task_type in ("document", "webapp", "backend") else "webapp"
        issuer_name = self._resolve_issuer_name(kwargs)
        req = WorkspaceJobRequest(
            job_id=job_id,
            stream_id=stream_id,
            chat_id=chat_id,
            target_id=target_id,
            source_message_id=source_message_id,
            creator_wxid=creator_wxid,
            issuer_name=issuer_name,
            access_policy="member_collect" if access_policy == "member_collect" else "creator_only",
            project_slug=project_slug,
            action=action,
            task_type=effective_task_type,
            task_prompt=task_prompt,
            context_notes=context_notes,
            staged_files=staged,
            ttl_hours=self.config.default_ttl_hours,
        )

        if hasattr(self, "compute_economy") and self.compute_economy:
            self.compute_economy.record_local_cost(0.02)

        task = asyncio.create_task(self._run_slow_job_and_notify(req))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

        return {
            "ok": True,
            "mode": "slow_track_async",
            "executor": "Fairy",
            "task_id": job_id,
            "project_slug": project_slug,
            "task_type": req.task_type,
            "planner_hint": (
                f"任务已交由 Fairy 在后台工作区异步执行（task_id={job_id}，当前尚未产出结果）。"
                "请立即调用 reply 工具，基于你自身的人设、当前对话语境与相关记忆，对用户进行一个简单的不固定回复；"
                "待 Fairy 后台执行完成后，系统将自动唤醒你向用户交付结果。"
            ),
        }

    async def _append_context_note(
        self,
        stream_id: str,
        note: str,
        *,
        source_kind: str = "plugin:embodied_workspace",
    ) -> None:
        await self.ctx.call_capability(
            "maisaka.context.append",
            stream_id=stream_id,
            segments=[{"type": "text", "content": note}],
            visible_text=note,
            source_kind=source_kind,
        )

    def get_active_jobs_summary(self) -> str:
        if not self._active_jobs:
            return ""
        parts: list[str] = []
        for item in self._active_jobs.values():
            slug = str(item.get("project_slug") or "")
            status = str(item.get("latest_status") or "")
            parts.append(f"项目【{slug}】{status}")
        return "；".join(parts)

    def _sync_active_jobs_to_truth_db(self) -> None:
        """仅在真值库已正规定义 fairy_active_job_summary 列时同步聚合多槽状态，严禁插件运行期执行 ALTER TABLE。"""
        try:
            truth_path = Path(self.config.truth_db_path)
            if not truth_path.exists():
                return
            summary = self.get_active_jobs_summary()
            with sqlite3.connect(str(truth_path), timeout=5.0) as conn:
                cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(global_state)").fetchall()}
                if "fairy_active_job_summary" not in cols:
                    return
                conn.execute(
                    "UPDATE global_state SET fairy_active_job_summary = ? WHERE row_id = 1",
                    (summary,),
                )
                conn.commit()
        except Exception:
            pass

    async def _run_slow_job_and_notify(self, req: WorkspaceJobRequest) -> None:
        if req.job_id in self._notified_job_ids:
            return

        slug = self.cli_bridge.safe_id(req.project_slug)
        existing_token = self.router.find_active_token_by_slug(req.chat_id, slug) if req.action == "update" else None
        if existing_token:
            await self.supervisor.stop_backend(existing_token)

        self._active_jobs[req.job_id] = {
            "project_slug": slug,
            "task_type": req.task_type,
            "task_prompt": req.task_prompt,
            "chat_id": req.chat_id,
            "target_id": req.target_id,
            "stream_id": req.stream_id,
            "started_at": time.time(),
            "latest_status": f"正在执行：{req.task_prompt[:40]}",
        }
        self._sync_active_jobs_to_truth_db()

        async def on_progress(status: str, elapsed_sec: int) -> None:
            elapsed_min = max(1, elapsed_sec // 60)
            elapsed_desc = f"{elapsed_min} 分钟"
            if req.job_id in self._active_jobs:
                self._active_jobs[req.job_id]["latest_status"] = f"已运行 {elapsed_desc}：{status}"
            self._sync_active_jobs_to_truth_db()

            progress_note = (
                f"[Fairy工作区进度巡检] task_id={req.job_id} | 项目={slug} | 已耗时={elapsed_desc}\n"
                f"当前客观进展：{status}"
            )
            await self._append_context_note(req.stream_id, progress_note)
            intent = (
                f"Fairy 慢轨工作区正在为用户执行后台长程任务（项目: {slug}，已运行 {elapsed_desc}）。\n"
                f"当前客观进展：{status}。\n"
                f"请结合你的人设与当前对话情境，以自然、生活化、符合陪伴默契的方式主动向用户汇报一下目前的执行进度，"
                f"告知后台正在有序推进中，让他安心等待，语气轻松自然，不要机械生硬。"
            )
            await self.ctx.call_capability(
                "maisaka.proactive.trigger",
                stream_id=req.stream_id,
                intent=intent,
            )

        try:
            res = await self.cli_bridge.run_cli_job(req, progress_callback=on_progress)
        except asyncio.CancelledError:
            raise
        finally:
            self._active_jobs.pop(req.job_id, None)
            self._sync_active_jobs_to_truth_db()

        quote_anchor = f"[引用: {req.source_message_id}]\n" if req.source_message_id else ""
        if res.ok and res.dist_dir and res.project_dir:
            token = existing_token or self.router.generate_short_token(6)
            url = self.router.upsert_route(
                token=token,
                chat_id=req.chat_id,
                stream_id=req.stream_id,
                creator_wxid=req.creator_wxid,
                issuer_name=req.issuer_name or self._resolve_issuer_name(),
                access_policy=req.access_policy,
                project_slug=slug,
                title=res.title,
                summary=res.summary_for_replyer,
                mode=res.mode,
                dist_dir=res.dist_dir,
                project_dir=res.project_dir,
                start_cmd=res.start_cmd,
                ttl_hours=req.ttl_hours,
            )
            if res.mode == "backend" and res.start_cmd:
                route = self.router.get_route(token)
                if route:
                    try:
                        await self.supervisor.ensure_running(route, res.start_cmd)
                    except Exception:
                        pass

            archived_files: list[str] = []
            try:
                archived_files = await self.archive_mgr.archive_job_artifacts(
                    chat_safe_id=self.cli_bridge.safe_id(req.chat_id),
                    project_slug=slug,
                    title=res.title,
                    task_prompt=req.task_prompt,
                    short_url=url,
                    dist_dir=res.dist_dir,
                    declared_files=res.archive_files,
                )
            except Exception:
                pass

            archive_note = f"\n已归档至 Google Drive 独立目录文件：{', '.join(archived_files)}" if archived_files else ""
            note = (
                f"{quote_anchor}[Fairy工作区任务完成] task_id={req.job_id} | 类型={req.task_type} | 模式={res.mode}\n"
                f"目标会话(target_id)：{req.target_id}\n"
                f"标题：{res.title}\n摘要：{res.summary_for_replyer}\n链接：{url}{archive_note}"
            )
            intent = (
                f"Fairy 已完成后台任务 {req.job_id}（摘要：{res.summary_for_replyer}）。"
                f"请基于你的人设与当前会话语境，将结果链接 {url} 自然地交付给用户。"
            )
        else:
            note = f"{quote_anchor}[Fairy工作区任务失败] task_id={req.job_id} | 原因：{res.error_message}"
            intent = f"Fairy 后台任务 {req.job_id} 执行失败（{res.error_message}），请结合你的人设向用户简短说明情况。"

        self._notified_job_ids.add(req.job_id)
        await self._append_context_note(req.stream_id, note)
        await self.ctx.call_capability(
            "maisaka.proactive.trigger",
            stream_id=req.stream_id,
            intent=intent,
        )

    async def _handle_web_notify_callback(
        self,
        route: SubdomainRoute,
        actor_wxid: str,
        event_name: str,
        detail: str,
        applied_changes: list[str],
        trigger_proactive: bool,
    ) -> None:
        mem_note = f" | 已同步更新用户({actor_wxid})专属记忆：{'; '.join(applied_changes)}" if applied_changes else ""
        note = (
            f"[网页端实名回调] 用户={actor_wxid} 在页面《{route.title}》({route.project_slug}) "
            f"触发事件：{event_name}\n详情：{detail}{mem_note}"
        )
        await self._append_context_note(route.stream_id, note)
        if trigger_proactive:
            await self.ctx.call_capability(
                "maisaka.proactive.trigger",
                stream_id=route.stream_id,
                intent=f"用户 {actor_wxid} 在网页《{route.title}》上完成了操作（{event_name}: {detail}{mem_note}），请自然地回应。",
            )

    def _can_actor_mutate_route(self, route: SubdomainRoute | None, actor_wxid: str) -> bool:
        if route is None or route.access_policy != "creator_only" or not route.creator_wxid:
            return True
        clean_actor = actor_wxid.strip()
        if not clean_actor:
            return False
        if clean_actor == route.creator_wxid:
            return True
        return bool(hasattr(self, "truth_writer") and self.truth_writer and self.truth_writer.is_owner_wxid(clean_actor))

    async def _perform_project_rollback(
        self,
        chat_id: str,
        project_slug: str,
        target_spec: str = "1",
        fallback_stream_id: str = "",
        actor_wxid: str = "",
    ) -> dict[str, Any]:
        slug = self.cli_bridge.safe_id(project_slug)
        ws_dir = self.cli_bridge.get_chat_workspace(chat_id)

        if self.cli_bridge.is_project_locked(chat_id, slug):
            return {
                "ok": False,
                "error": f"项目 `{slug}` 当前正在后台构建中，请等待构建完成后再执行回滚",
            }

        async with self.cli_bridge.project_lock(chat_id, slug):
            token = self.router.find_active_token_by_slug(chat_id, slug)
            old_route = self.router.get_route(token) if token else None

            if not self._can_actor_mutate_route(old_route, actor_wxid):
                return {
                    "ok": False,
                    "error": "该项目为创建者独占模式，仅创建者本人或 OWNER 可执行回滚",
                }

            if token:
                await self.supervisor.stop_backend(token)

            rb_res = await self.time_machine.rollback(ws_dir, slug, target_spec=target_spec)
            if not rb_res.ok:
                return {"ok": False, "error": rb_res.error}

            meta = rb_res.restored_meta or {}
            mode = str(meta.get("mode") or (old_route.mode if old_route else "static")).strip().lower()
            start_cmd = (
                [str(x) for x in meta.get("start_cmd", [])]
                if isinstance(meta.get("start_cmd"), list)
                else (json.loads(old_route.start_cmd_json) if old_route and old_route.start_cmd_json else [])
            )
            title = str(meta.get("title") or (old_route.title if old_route else slug))
            summary = str(meta.get("summary_for_replyer") or f"已回滚至 {rb_res.target_commit}")

            creator_wxid = (old_route.creator_wxid if old_route and old_route.creator_wxid else actor_wxid)
            issuer_name = (old_route.issuer_name if old_route and old_route.issuer_name else self._resolve_issuer_name())
            access_policy = (old_route.access_policy if old_route else "creator_only")
            stream_id = (old_route.stream_id if old_route and old_route.stream_id else fallback_stream_id)

            dist_dir = ws_dir / "dist" / slug
            project_dir = ws_dir / "projects" / slug
            eff_token = token or self.router.generate_short_token(6)

            url = self.router.upsert_route(
                token=eff_token,
                chat_id=chat_id,
                stream_id=stream_id,
                creator_wxid=creator_wxid,
                issuer_name=issuer_name,
                access_policy=access_policy,
                project_slug=slug,
                title=title,
                summary=summary,
                mode=mode,
                dist_dir=dist_dir,
                project_dir=project_dir,
                start_cmd=start_cmd,
                ttl_hours=self.config.default_ttl_hours,
            )
            if mode == "backend" and start_cmd:
                route = self.router.get_route(eff_token)
                if route:
                    try:
                        await self.supervisor.ensure_running(route, start_cmd)
                    except Exception:
                        pass

            return {
                "ok": True,
                "project_slug": slug,
                "target_commit": rb_res.target_commit,
                "restored_subject": rb_res.restored_subject,
                "access_policy": access_policy,
                "url": url,
                "mode": mode,
            }

    @Tool(
        name="manage_workspace_projects",
        description=(
            "【工作区目录与时光机回滚工具】用于查询当前会话已创建的所有网页项目列表(action='list')、"
            "查看某个项目的 Git 快照历史版本(action='history')，或将改坏的项目一键回滚至之前的稳定快照(action='rollback')。"
        ),
        parameters=[
            ToolParameterInfo(
                name="action",
                param_type=ToolParamType.STRING,
                description="操作类型：'list'（列出当前会话项目）、'history'（查看项目快照历史）、'rollback'（执行回滚）",
                required=True,
            ),
            ToolParameterInfo(
                name="project_slug",
                param_type=ToolParamType.STRING,
                description="目标项目英文标识（action 为 'history' 或 'rollback' 时必填）",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="target_version",
                param_type=ToolParamType.STRING,
                description="回滚目标：传数字 '1' 表示回退 1 个版本（上一稳定版），也可传具体 short_hash",
                required=False,
                default="1",
            ),
        ],
    )
    async def manage_workspace_projects(
        self,
        action: str,
        project_slug: str = "",
        target_version: str = "1",
        **kwargs: Any,
    ) -> dict[str, Any]:
        chat_id = self._resolve_chat_id(kwargs)
        act = action.strip().lower()

        if act == "list":
            routes = self.router.list_chat_routes(chat_id, limit=15)
            items = [
                {
                    "project_slug": r.project_slug,
                    "title": r.title,
                    "mode": r.mode,
                    "access_policy": r.access_policy,
                    "url": self.router.format_url(r.token),
                }
                for r in routes
            ]
            return {"ok": True, "projects": items}

        slug = self.cli_bridge.safe_id(project_slug)
        ws_dir = self.cli_bridge.get_chat_workspace(chat_id)

        if act == "history":
            snaps = await self.time_machine.list_snapshots(ws_dir, slug, limit=10)
            return {
                "ok": True,
                "project_slug": slug,
                "snapshots": [
                    {
                        "short_hash": s.short_hash,
                        "timestamp": s.timestamp_iso,
                        "subject": s.subject,
                    }
                    for s in snaps
                ],
            }

        if act == "rollback":
            if not project_slug.strip():
                return {"ok": False, "error": "执行回滚必须提供 project_slug"}
            sender_wxid = str(kwargs.get("user_id") or "").strip()
            stream_id = str(kwargs.get("stream_id") or "")
            return await self._perform_project_rollback(
                chat_id,
                slug,
                target_spec=target_version,
                fallback_stream_id=stream_id,
                actor_wxid=sender_wxid,
            )

        return {"ok": False, "error": f"未知的 action: {action}"}

    @Command(
        name="rollback",
        description="一键将指定工作区项目回滚至上一稳定快照版本",
        pattern=r"^(?:/rollback|/回滚|回滚)\s+(?P<slug>[a-zA-Z0-9_.-]+)(?:\s+(?P<version>\S+))?\s*$",
        aliases=["/rollback", "/回滚", "回滚"],
    )
    async def rollback_workspace_cmd(self, **kwargs: Any) -> str:
        chat_id = self._resolve_chat_id(kwargs)
        slug = self._extract_named_group(kwargs, "slug", fallback_index=1)
        target_spec = self._extract_named_group(kwargs, "version", fallback_index=2) or "1"

        if not slug:
            return "请指定要回滚的项目标识，例如：/rollback camp-vote"

        sender_wxid = str(kwargs.get("user_id") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "")
        res = await self._perform_project_rollback(
            chat_id,
            slug,
            target_spec=target_spec,
            fallback_stream_id=stream_id,
            actor_wxid=sender_wxid,
        )
        if res.get("ok"):
            return (
                f"⏪ 已将项目 `{res['project_slug']}` 回滚至快照 `{res['target_commit']}` "
                f"（{res['restored_subject']}）！\n访问链接：{res['url']}"
            )
        return f"❌ 回滚失败：{res.get('error')}"

    @Command(
        name="claim",
        description="绑定跨设备/跨浏览器网页身份口令",
        pattern=r"^(?:/claim|/认领|/绑定|认领|绑定)\s+(?P<code>[0-9]{4,8})\s*$",
        aliases=["/claim", "/认领", "/绑定", "认领", "绑定"],
    )
    async def claim_web_identity_cmd(self, **kwargs: Any) -> str:
        code = self._extract_named_group(kwargs, "code", fallback_index=1)
        sender_wxid = str(kwargs.get("user_id") or "").strip()
        sender_name = self._resolve_sender_nickname(kwargs, sender_wxid)
        chat_id = self._resolve_chat_id(kwargs)
        if not code:
            return "请提供 4 位认领口令，例如：认领 8392"

        claim_chat_id = self.router.get_claim_chat_id(code)
        allowed_members: set[str] | None = None
        if claim_chat_id.endswith("@chatroom"):
            group_set = self.truth_writer.get_group_member_wxids(claim_chat_id)
            if group_set:
                allowed_members = group_set

        ok = self.router.fulfill_claim_by_wechat(
            code,
            sender_wxid,
            sender_name,
            source_chat_id=chat_id,
            allowed_group_members=allowed_members,
        )
        if ok:
            return "✅ 微信身份绑定成功！你正在浏览的网页将在 1 秒内自动登录。"
        return "❌ 认领口令无效、已过期或不属于当前群聊成员，请在网页弹窗中重新获取。"

    @Command(
        name="pin",
        description="设置你在动态工作区网页的 4 位跨设备登录 PIN 码",
        pattern=r"^(?:/pin|/设置pin|设置pin)\s+(?P<pin>\S+)\s*$",
        aliases=["/pin", "/设置pin", "设置pin"],
    )
    async def set_web_pin_cmd(self, **kwargs: Any) -> str:
        clean_pin = self._extract_named_group(kwargs, "pin", fallback_index=1)
        sender_wxid = str(kwargs.get("user_id") or "").strip()
        sender_name = self._resolve_sender_nickname(kwargs, sender_wxid)
        if len(clean_pin) < 4:
            return "❌ PIN 码长度至少需要 4 位，例如：/pin 6688"
        self.router.set_user_pin(sender_wxid, clean_pin, sender_name)
        return "✅ 你的跨设备网页 PIN 码已生效！今后在 Safari、电脑或新设备打开群网页，直接选择你的名字输入该 PIN 码即可秒登。"

    @Command(
        name="revoke",
        description="立即撤销并销毁指定的限时子域名链接",
        pattern=r"^(?:/revoke|/撤销|撤销)\s+(?P<token>\S+)\s*$",
        aliases=["/revoke", "/撤销", "撤销"],
    )
    async def revoke_link_cmd(self, **kwargs: Any) -> str:
        raw_arg = self._extract_named_group(kwargs, "token", fallback_index=1)
        chat_id = self._resolve_chat_id(kwargs)
        sender_wxid = str(kwargs.get("user_id") or "").strip()
        clean_token = raw_arg.strip().rstrip("/").split("/")[-1].split(".")[0].replace("https://", "")
        if not clean_token:
            return "请指定要销毁的短链标识，例如：/revoke k9x4m2"

        route = self.router.get_route(clean_token)
        if not route or route.chat_id != chat_id or route.revoked:
            return f"未找到当前会话下可撤销的链接标识：{clean_token}"

        if not self._can_actor_mutate_route(route, sender_wxid):
            return "❌ 该链接为创建者独占模式，仅创建者本人或 OWNER 可撤销销毁。"

        ok = self.router.revoke_token(clean_token, chat_id=chat_id)
        if ok:
            await self.supervisor.stop_backend(clean_token)
            return f"已销毁工作区链接：{clean_token}"
        return f"未找到当前会话下可撤销的链接标识：{clean_token}"

    @HookHandler("maisaka.reply.before_post_process")
    async def protect_subdomain_url_hook(
        self,
        *,
        response: str = "",
        skip_post_process: bool = False,
        enable_splitter: bool = True,
        enable_chinese_typo: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = enable_splitter, enable_chinese_typo
        short_host = self.config.short_link_base_url.replace("https://", "").replace("http://", "").strip("/")
        matched = bool(
            (self.config.base_domain and self.config.base_domain in response)
            or (short_host and short_host in response)
        )
        if not matched:
            return {"action": "continue"}
        return {
            "action": "continue",
            "modified_kwargs": {
                **kwargs,
                "response": response,
                "skip_post_process": bool(skip_post_process),
                "enable_splitter": False,
                "enable_chinese_typo": False,
            },
        }


def create_plugin() -> EmbodiedWorkspacePlugin:
    return EmbodiedWorkspacePlugin()
