from __future__ import annotations

import asyncio
import re
import secrets
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
from .fast_renderer import render_markdown_to_wechat_html
from .gateway_server import WorkspaceGatewayServer
from .subdomain_router import SubdomainRoute, SubdomainRouter
from .user_truth_writer import UserScopedTruthWriter


class PluginSectionConfig(PluginConfigBase):
    config_version: str = Field(default="1.0.0", description="插件配置版本")


class EmbodiedWorkspaceConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig, description="插件元配置")
    base_domain: str = Field(default="paff-67.top", description="交付基础域名（单级通配符）")
    short_link_base_url: str = Field(default="", description="短链接基础域名（例如 https://xy.pages.dev）")
    cookie_secret: str = Field(default="maibot-embodied-workspace-hmac-secret-2026", description="HMAC Cookie 签名秘钥")
    gateway_port: int = Field(default=18790, description="本地网关监听端口")
    default_ttl_hours: int = Field(default=24, description="默认链接有效期（小时）")
    cli_backend: str = Field(default="agy", description="执行引擎：agy 或 codex")
    cli_timeout_seconds: int = Field(default=180, description="代码执行超时时间（秒）")
    workspaces_root: str = Field(default="/opt/maibot-brain/data/workspaces", description="会话工作区根目录")
    truth_db_path: str = Field(default="/opt/maibot-brain/data/companion_truth.db", description="真值库路径")
    ledger_db_path: str = Field(default="/opt/maibot-brain/data/message_ledger.db", description="消息账本路径")
    router_db_path: str = Field(default="/opt/maibot-brain/data/workspaces/subdomain_routes.db", description="路由数据库路径")
    gdrive_archive_root: str = Field(default="/mnt/gdrive/companion_vault/workspace_archives", description="Google Drive 归档目录")
    wechat_source: str = Field(default="https://100.90.156.92:8443", description="微信驱动数据源地址")
    wechat_account: str = Field(default="4d86ac1b06dd252fa3c7327eb5614865", description="微信账号目录哈希")


class EmbodiedWorkspacePlugin(MaiBotPlugin):
    config_model = EmbodiedWorkspaceConfig

    def __init__(self) -> None:
        super().__init__()
        self.set_plugin_config({})

    @property
    def ctx(self) -> Any:
        if self._ctx is None:
            raise RuntimeError("插件上下文尚未初始化，确保在 Runner 环境中运行")
        return self._ctx

    @ctx.setter
    def ctx(self, value: Any) -> None:
        self._set_context(value)

    async def on_load(self) -> None:
        ws_root = Path(self.config.workspaces_root)
        truth_path = Path(self.config.truth_db_path)

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
        self.cli_bridge = CLIBridge(
            workspaces_root=ws_root,
            truth_db_path=truth_path,
            ledger_db_path=Path(self.config.ledger_db_path),
            wechat_source=self.config.wechat_source,
            wechat_account=self.config.wechat_account,
            cli_backend=self.config.cli_backend,
            timeout_seconds=self.config.cli_timeout_seconds,
        )
        self.supervisor = BackendSupervisor(workspaces_root=ws_root)
        self.supervisor.start_janitor()

        self.gateway = WorkspaceGatewayServer(
            router=self.router,
            supervisor=self.supervisor,
            truth_writer=self.truth_writer,
            cookie_secret=self.config.cookie_secret,
            on_web_notify=self._handle_web_notify_callback,
            listen_port=self.config.gateway_port,
        )
        await self.gateway.start()
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    async def on_unload(self) -> None:
        if hasattr(self, "gateway"):
            await self.gateway.stop()
        if hasattr(self, "supervisor"):
            await self.supervisor.stop_all()

    @staticmethod
    def _resolve_chat_id(kwargs: dict[str, Any]) -> str:
        group_id = str(kwargs.get("group_id") or "").strip()
        user_id = str(kwargs.get("user_id") or "").strip()
        if group_id:
            return group_id if group_id.endswith("@chatroom") else f"{group_id}@chatroom"
        return user_id

    @staticmethod
    def _extract_command_arg(kwargs: dict[str, Any]) -> str:
        groups = kwargs.get("matched_groups")
        if isinstance(groups, (list, tuple)) and groups:
            return str(groups[0] or "").strip()
        if isinstance(groups, dict) and groups:
            for val in groups.values():
                if val:
                    return str(val).strip()
        raw_text = str(kwargs.get("text") or kwargs.get("command_text") or "").strip()
        parts = raw_text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @Tool(
        name="publish_document_page",
        description=(
            "【快轨·长文排版页】当需要向用户交付长篇文章、深度报告、攻略、故事或包含复杂 Markdown/数学公式/流程图的内容时调用。"
            "保留你的人设文风并直接生成移动端网页，1~2 秒内同步返回极简短链。"
            "若任务需要写代码、制作交互式图表/小游戏、处理微信附件(FMxxxx)或修改已有网页，严禁调用本工具，请改用 run_agent_workspace。"
        ),
        parameters=[
            ToolParameterInfo(
                name="title",
                param_type=ToolParamType.STRING,
                description="页面标题（将显示在微信链接卡片与网页顶部）",
                required=True,
            ),
            ToolParameterInfo(
                name="writing_instruction",
                param_type=ToolParamType.STRING,
                description="长文写作要求、核心论点与大纲（非空，1~500000 字符）",
                required=True,
            ),
            ToolParameterInfo(
                name="reference_text",
                param_type=ToolParamType.STRING,
                description="可选补充参考材料或上下文摘录",
                required=False,
                default="",
            ),
        ],
    )
    async def publish_document_page(
        self,
        title: str,
        writing_instruction: str,
        reference_text: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        chat_id = self._resolve_chat_id(kwargs)
        creator_wxid = str(kwargs.get("user_id") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "")

        fm_matches = re.findall(r"\bFM\d{4,}\b", f"{writing_instruction} {reference_text}".upper())
        if fm_matches:
            return await self.run_agent_workspace(
                task_prompt=f"标题：{title}\n需求：{writing_instruction}\n参考：{reference_text}",
                project_slug="doc-with-attachment",
                action="create",
                access_policy="creator_only",
                file_codes=",".join(sorted(set(fm_matches))),
                **kwargs,
            )

        gen_res = await self.ctx.call_capability(
            "maisaka.reply.generate",
            stream_id=stream_id,
            reference=writing_instruction,
            format_instruction=f"请撰写一篇标题为《{title}》的完整长文 Markdown。\n补充参考材料：{reference_text}",
        )
        markdown_body = str((gen_res or {}).get("text") or "").strip()
        if not markdown_body:
            err_detail = (gen_res or {}).get("error") or "长文生成结果为空"
            return {"ok": False, "error": str(err_detail)}

        token = self.router.generate_short_token(6)
        slug = f"note-{token}"
        ws_dir = self.cli_bridge.get_chat_workspace(chat_id)
        dist_dir = ws_dir / "dist" / slug
        project_dir = ws_dir / "projects" / slug
        dist_dir.mkdir(parents=True, exist_ok=True)
        project_dir.mkdir(parents=True, exist_ok=True)

        html_doc = render_markdown_to_wechat_html(title=title, markdown_text=markdown_body)
        (dist_dir / "index.html").write_text(html_doc, encoding="utf-8")

        url = self.router.upsert_route(
            token=token,
            chat_id=chat_id,
            stream_id=stream_id,
            creator_wxid=creator_wxid,
            access_policy="creator_only",
            project_slug=slug,
            title=title,
            summary=writing_instruction[:80],
            mode="static",
            dist_dir=dist_dir,
            project_dir=project_dir,
            ttl_hours=self.config.default_ttl_hours,
        )
        return {
            "ok": True,
            "mode": "fast_track_sync",
            "url": url,
            "title": title,
            "planner_hint": f"长文页面已生成：{url} ，请直接调用 reply 工具用简短自然的气泡将链接发给用户。",
        }

    @Tool(
        name="run_agent_workspace",
        description=(
            "【慢轨·全功能代码与全栈网页工作区】当需要执行 Python/Node 代码、制作交互式 HTML/图表/小游戏、构建带后端的多人投票/收集表、"
            "分析当前会话的微信附件(FMxxxx)、或对之前生成的项目进行多轮修改(action='update')时调用。"
            "本工具立即返回 task_id（非阻塞），后台完成后会自动唤醒你把极简短链发给用户。"
        ),
        parameters=[
            ToolParameterInfo(
                name="task_prompt",
                param_type=ToolParamType.STRING,
                description="给后台 agy 编程 Agent 的详细需求描述",
                required=True,
            ),
            ToolParameterInfo(
                name="project_slug",
                param_type=ToolParamType.STRING,
                description="项目英文短标识（如 'camp-vote'；修改已有项目时传入相同标识）",
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
        file_codes: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        chat_id = self._resolve_chat_id(kwargs)
        creator_wxid = str(kwargs.get("user_id") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "")

        if self.cli_bridge.is_project_locked(chat_id, project_slug):
            return {
                "ok": False,
                "error": f"项目 {project_slug} 当前正在后台构建中，请等待构建完成后再提交新修改。",
            }

        codes = [c.strip().upper() for c in file_codes.split(",") if c.strip()]
        try:
            staged = self.cli_bridge.stage_authorized_files(
                destination_chat_id=chat_id,
                file_codes=codes,
            )
        except Exception as exc:
            return {"ok": False, "error": f"附件权限校验或准备失败：{exc}"}

        job_id = f"job_{int(time.time())}_{secrets.token_hex(3)}"
        req = WorkspaceJobRequest(
            job_id=job_id,
            stream_id=stream_id,
            chat_id=chat_id,
            creator_wxid=creator_wxid,
            access_policy="member_collect" if access_policy == "member_collect" else "creator_only",
            project_slug=project_slug,
            action=action,
            task_prompt=task_prompt,
            staged_files=staged,
            ttl_hours=self.config.default_ttl_hours,
        )
        task = asyncio.create_task(self._run_slow_job_and_notify(req))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

        return {
            "ok": True,
            "mode": "slow_track_async",
            "task_id": job_id,
            "project_slug": project_slug,
            "access_policy": req.access_policy,
            "planner_hint": "后台 Agent 已开始构建。请先调用 reply 简短告诉用户你正在写代码/处理数据，稍后完成后会自动发链接。",
        }

    async def _append_context_note(self, stream_id: str, note: str) -> None:
        await self.ctx.call_capability(
            "maisaka.context.append",
            stream_id=stream_id,
            segments=[{"type": "text", "content": note}],
            visible_text=note,
        )

    async def _run_slow_job_and_notify(self, req: WorkspaceJobRequest) -> None:
        slug = self.cli_bridge.safe_id(req.project_slug)
        existing_token = self.router.find_active_token_by_slug(req.chat_id, slug) if req.action == "update" else None
        if existing_token:
            await self.supervisor.stop_backend(existing_token)

        res = await self.cli_bridge.run_cli_job(req)
        if res.ok and res.dist_dir and res.project_dir:
            token = existing_token or self.router.generate_short_token(6)
            url = self.router.upsert_route(
                token=token,
                chat_id=req.chat_id,
                stream_id=req.stream_id,
                creator_wxid=req.creator_wxid,
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
                f"[工作区任务完成] task_id={req.job_id} | 模式={res.mode} | 权限={req.access_policy}\n"
                f"标题：{res.title}\n摘要：{res.summary_for_replyer}\n链接：{url}{archive_note}"
            )
            intent = f"后台任务 {req.job_id} 已完成（{res.summary_for_replyer}），请用自然语气把链接 {url} 发给用户。"
        else:
            note = f"[工作区任务失败] task_id={req.job_id} | 原因：{res.error_message}"
            intent = f"后台任务 {req.job_id} 失败（{res.error_message}），请简短向用户说明原因。"

        await self._append_context_note(req.stream_id, note)
        await self.ctx.call_capability("maisaka.proactive.trigger", stream_id=req.stream_id, intent=intent)

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

    @Command(
        name="claim",
        description="绑定跨设备/跨浏览器网页身份口令",
        pattern=r"^(?:/claim|/认领|/绑定|认领|绑定)\s+([0-9]{4,8})\s*$",
        aliases=["/claim", "/认领", "/绑定", "认领", "绑定"],
    )
    async def claim_web_identity_cmd(self, **kwargs: Any) -> str:
        code = self._extract_command_arg(kwargs)
        sender_wxid = str(kwargs.get("user_id") or "").strip()
        sender_name = str(kwargs.get("user_nickname") or sender_wxid)
        if not code:
            return "请提供 4 位认领口令，例如：认领 8392"
        ok = self.router.fulfill_claim_by_wechat(code, sender_wxid, sender_name)
        if ok:
            return "✅ 微信身份绑定成功！你正在浏览的网页将在 1 秒内自动登录。"
        return "❌ 认领口令无效或已过期，请在网页弹窗中重新点击获取。"

    @Command(
        name="pin",
        description="设置你在动态工作区网页的 4 位跨设备登录 PIN 码",
        pattern=r"^(?:/pin|/设置pin|设置pin)\s+(\S+)\s*$",
        aliases=["/pin", "/设置pin", "设置pin"],
    )
    async def set_web_pin_cmd(self, **kwargs: Any) -> str:
        clean_pin = self._extract_command_arg(kwargs)
        sender_wxid = str(kwargs.get("user_id") or "").strip()
        sender_name = str(kwargs.get("user_nickname") or sender_wxid)
        if len(clean_pin) < 4:
            return "❌ PIN 码长度至少需要 4 位，例如：/pin 6688"
        self.router.set_user_pin(sender_wxid, clean_pin, sender_name)
        return "✅ 你的跨设备网页 PIN 码已生效！今后在 Safari、电脑或新设备打开群网页，直接选择你的名字输入该 PIN 码即可秒登。"

    @Command(
        name="revoke",
        description="立即撤销并销毁指定的限时子域名链接",
        pattern=r"^(?:/revoke|/撤销|撤销)\s+(\S+)\s*$",
        aliases=["/revoke", "/撤销", "撤销"],
    )
    async def revoke_link_cmd(self, **kwargs: Any) -> str:
        raw_arg = self._extract_command_arg(kwargs)
        chat_id = self._resolve_chat_id(kwargs)
        clean_token = raw_arg.strip().rstrip("/").split("/")[-1].split(".")[0].replace("https://", "")
        if not clean_token:
            return "请指定要销毁的短链标识，例如：/revoke k9x4m2"
        ok = self.router.revoke_token(clean_token, chat_id=chat_id)
        if ok:
            await self.supervisor.stop_backend(clean_token)
            return f"已销毁工作区链接：{clean_token}"
        return f"未找到当前会话下可撤销的链接标识：{clean_token}"

    @HookHandler("maisaka.reply.before_post_process")
    async def protect_subdomain_url_hook(self, event: dict[str, Any]) -> dict[str, Any]:
        text = str(event.get("response") or "")
        short_host = self.config.short_link_base_url.replace("https://", "").replace("http://", "").strip("/")
        if self.config.base_domain in text or (short_host and short_host in text):
            event["enable_chinese_typo"] = False
            event["enable_splitter"] = False
        return event


def create_plugin() -> EmbodiedWorkspacePlugin:
    return EmbodiedWorkspacePlugin()
