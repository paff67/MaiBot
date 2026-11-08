from __future__ import annotations

import asyncio
import re
import secrets
import time
from pathlib import Path
from typing import Any

from maibot_sdk import (
    Command,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .backend_supervisor import BackendSupervisor
from .cli_bridge import CLIBridge, WorkspaceJobRequest
from .fast_renderer import render_markdown_to_wechat_html
from .gateway_server import WorkspaceGatewayServer
from .subdomain_router import SubdomainRoute, SubdomainRouter


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件设置"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用工作区插件")
    config_version: str = Field(default="1.0.0", description="配置版本号")


class EmbodiedWorkspaceConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    base_domain: str = Field(default="s.yourdomain.com", description="泛域名服务后缀")
    gateway_port: int = Field(default=18790, description="本地网关监听端口")
    default_ttl_hours: int = Field(default=24, description="默认链接有效期（小时）")
    cli_backend: str = Field(default="codex", description="执行引擎：codex 或 agy")
    cli_timeout_seconds: int = Field(default=180, description="代码执行超时时间（秒）")
    workspaces_root: str = Field(default="/opt/maibot-brain/data/workspaces", description="会话工作区根目录")
    truth_db_path: str = Field(default="/opt/maibot-brain/data/companion_truth.db", description="真值库路径")
    ledger_db_path: str = Field(default="/opt/maibot-brain/data/message_ledger.db", description="消息账本路径")
    router_db_path: str = Field(default="/opt/maibot-brain/data/workspaces/subdomain_routes.db", description="路由数据库路径")
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
        self.router = SubdomainRouter(
            db_path=Path(self.config.router_db_path),
            base_domain=self.config.base_domain,
        )
        self.cli_bridge = CLIBridge(
            workspaces_root=ws_root,
            truth_db_path=Path(self.config.truth_db_path),
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
    def _new_token() -> str:
        return secrets.token_urlsafe(9).lower().replace("-", "x").replace("_", "z")[:12]

    @staticmethod
    def _resolve_chat_id(kwargs: dict[str, Any]) -> str:
        group_id = str(kwargs.get("group_id") or "").strip()
        user_id = str(kwargs.get("user_id") or "").strip()
        if group_id:
            return group_id if group_id.endswith("@chatroom") else f"{group_id}@chatroom"
        return user_id

    @Tool(
        name="publish_document_page",
        description=(
            "【快轨·长文排版页】当需要向用户交付长篇文章、深度报告、攻略、故事或包含复杂 Markdown/数学公式/流程图的内容时调用。"
            "保留你的人设文风并直接生成移动端网页，1~2 秒内同步返回限时链接。"
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
        stream_id = str(kwargs.get("stream_id") or "")

        # 确定性拦截升级：若指令中引用了 FMxxxx 附件，自动升级至慢轨工作区
        fm_matches = re.findall(r"\bFM\d{4,}\b", f"{writing_instruction} {reference_text}".upper())
        if fm_matches:
            return await self.run_agent_workspace(
                task_prompt=f"标题：{title}\n需求：{writing_instruction}\n参考：{reference_text}",
                project_slug="doc-with-attachment",
                action="create",
                file_codes=",".join(sorted(set(fm_matches))),
                **kwargs,
            )

        # 严格对齐宿主 src/plugin_runtime/capabilities/core.py 的 maisaka.reply.generate 契约
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

        token = self._new_token()
        slug = f"note-{token[:6]}"
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
            project_slug=slug,
            title=title,
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
            "【慢轨·全功能代码与全栈网页工作区】当需要执行 Python/Node 代码、制作交互式 HTML/图表/小游戏、构建带后端的多人投票/工具、"
            "分析当前会话的微信附件(FMxxxx)、或对之前生成的项目进行多轮修改(action='update')时调用。"
            "本工具立即返回 task_id（非阻塞），后台完成后会自动唤醒你把限时链接发给用户。"
        ),
        parameters=[
            ToolParameterInfo(
                name="task_prompt",
                param_type=ToolParamType.STRING,
                description="给后台编程 Agent 的详细需求描述",
                required=True,
            ),
            ToolParameterInfo(
                name="project_slug",
                param_type=ToolParamType.STRING,
                description="项目英文短标识（如 'trip-vote'；修改已有项目时传入相同标识）",
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
        file_codes: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        chat_id = self._resolve_chat_id(kwargs)
        stream_id = str(kwargs.get("stream_id") or "")
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
            "planner_hint": "后台 Agent 已开始构建。请先调用 reply 简短告诉用户你正在写代码/处理数据，稍后完成后会自动发链接。",
        }

    async def _append_context_note(self, stream_id: str, note: str) -> None:
        """严格对齐宿主 maisaka.context.append 的 segments + visible_text 契约"""
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
            token = existing_token or self._new_token()
            url = self.router.upsert_route(
                token=token,
                chat_id=req.chat_id,
                stream_id=req.stream_id,
                project_slug=slug,
                title=res.title,
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

            note = (
                f"[工作区任务完成] task_id={req.job_id} | 模式={res.mode}\n"
                f"标题：{res.title}\n摘要：{res.summary_for_replyer}\n链接：{url}"
            )
            intent = f"后台任务 {req.job_id} 已完成（{res.summary_for_replyer}），请用自然语气把链接 {url} 发给用户。"
        else:
            note = f"[工作区任务失败] task_id={req.job_id} | 原因：{res.error_message}"
            intent = f"后台任务 {req.job_id} 失败（{res.error_message}），请简短向用户说明原因。"

        await self._append_context_note(req.stream_id, note)
        await self.ctx.call_capability("maisaka.proactive.trigger", stream_id=req.stream_id, intent=intent)

    async def _handle_web_notify_callback(
        self, route: SubdomainRoute, event_name: str, detail: str, trigger_proactive: bool
    ) -> None:
        note = f"[网页端实时回调] 页面《{route.title}》({route.project_slug}) 触发事件：{event_name}\n详情：{detail}"
        await self._append_context_note(route.stream_id, note)
        if trigger_proactive:
            await self.ctx.call_capability(
                "maisaka.proactive.trigger",
                stream_id=route.stream_id,
                intent=f"用户在你生成的网页《{route.title}》上执行了操作（{event_name}: {detail}），请自然地回应或跟进。",
            )

    @Command(
        "revoke",
        description="立即撤销并销毁指定的限时子域名链接",
        pattern=r"^/revoke(?:\s+(?P<token>\S+))?$",
    )
    async def revoke_link_cmd(self, token: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        chat_id = self._resolve_chat_id(kwargs)
        matched = kwargs.get("matched_groups") or {}
        raw_token = token or matched.get("token") or ""
        clean_token = str(raw_token).strip().split(".")[0].replace("https://", "").replace("http://", "")
        if not clean_token:
            return False, "用法：/revoke <token_or_url>", True

        ok = self.router.revoke_token(clean_token, chat_id=chat_id)
        if ok:
            await self.supervisor.stop_backend(clean_token)
            return True, f"已销毁子域名链接：{clean_token}", True
        return False, f"未找到当前会话下可撤销的链接标识：{clean_token}", True

    @HookHandler("maisaka.reply.before_post_process")
    async def protect_subdomain_url_hook(self, event: dict[str, Any]) -> dict[str, Any]:
        # 严格对齐 src/maisaka/builtin_tool/reply.py L100 的 response 字段名
        text = str(event.get("response") or "")
        if self.config.base_domain in text:
            event["enable_chinese_typo"] = False
            event["enable_splitter"] = False
        return event


def create_plugin() -> EmbodiedWorkspacePlugin:
    """plugin_loader.py 强制要求的插件实例化工厂函数"""
    return EmbodiedWorkspacePlugin()
