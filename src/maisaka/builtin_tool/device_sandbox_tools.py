"""私聊 OWNER 的手机 SOP 指令登记协议。"""

from typing import Any, Dict, Optional
import asyncio
import json

from src.companion_vault.consolidator import update_body_state
from src.companion_vault.runtime_bridge import DEFAULT_TRUTH_DB_PATH, _open_truth_db, _resolve_trust_role_sync
from src.core.tooling import ToolExecutionContext, ToolExecutionResult, ToolInvocation, ToolSpec
from src.maisaka.subagent_tasks import SUBAGENT_MAX_RUNNING_TASKS, SubAgentOutcome, SubAgentTask

from .context import BuiltinToolRuntimeContext


def get_phone_action_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="phone_action",
        description="仅主人私聊可用。登记手机 SOP、确认恢复或停止请求，供 Heartflow / ARTEMIS 消费；登记不代表手机已执行。",
        parameters_schema={
            "type": "object",
            "properties": {
                "action_type": {"type": "string", "enum": ["run_sop", "resume_confirm", "stop"]},
                "sop_id": {"type": "string"},
                "task_goal": {"type": "string"},
                "use_file_code": {"type": "string", "default": ""},
            },
            "required": ["action_type", "sop_id", "task_goal"],
        },
        provider_name="maisaka_builtin",
        provider_type="builtin",
    )


async def _is_owner(tool_ctx: BuiltinToolRuntimeContext) -> bool:
    stream = tool_ctx.runtime.chat_stream
    if stream.user_id is None:
        raise ValueError("私聊会话缺少 user_id，无法解析硬件权限")
    role = await asyncio.to_thread(
        _resolve_trust_role_sync, DEFAULT_TRUTH_DB_PATH, "", stream.user_id, stream.platform,
    )
    return role == "OWNER"


def _register_phone_request(arguments: Dict[str, Any], task_id: str) -> None:
    """验证 SOP/文件后写入待消费请求；不把排队误报为设备执行成功。"""
    action_type = arguments["action_type"]
    states = {"run_sop": "RUN_SOP_PENDING", "resume_confirm": "RESUME_CONFIRM_PENDING", "stop": "STOP_PENDING"}
    state = states[action_type]
    db = _open_truth_db(DEFAULT_TRUTH_DB_PATH)
    try:
        current = db.conn.execute("SELECT current_app, latest_screen_file_code FROM global_state WHERE row_id=1").fetchone()
        current_app = current["current_app"] if current is not None else ""
        screen_code = current["latest_screen_file_code"] if current is not None else ""
        if action_type == "run_sop":
            sop = db.conn.execute("SELECT sop_id FROM app_sop_skills WHERE sop_id=?", (arguments["sop_id"],)).fetchone()
            if sop is None:
                raise ValueError(f"未知 SOP：{arguments['sop_id']}")
        file_code = arguments.get("use_file_code", "")
        if file_code and db.conn.execute("SELECT 1 FROM media_vault WHERE file_code=?", (file_code,)).fetchone() is None:
            raise ValueError(f"未知文件：{file_code}")
    finally:
        db.close()
    update_body_state(
        DEFAULT_TRUTH_DB_PATH, body_state=state, current_app=current_app,
        live_observation=json.dumps({"task_id": task_id, "phone_action": arguments}, ensure_ascii=False),
        latest_screen_file_code=screen_code,
    )


async def handle_phone_action_tool(
    tool_ctx: BuiltinToolRuntimeContext, invocation: ToolInvocation,
    context: Optional[ToolExecutionContext] = None,
) -> ToolExecutionResult:
    if tool_ctx.runtime.chat_stream.is_group_session:
        return tool_ctx.build_failure_result(invocation.tool_name, "群聊禁止执行手机操作。")
    if not await _is_owner(tool_ctx):
        return tool_ctx.build_failure_result(invocation.tool_name, "只有 OWNER 可以执行手机操作。")
    manager = tool_ctx.runtime._subagent_tasks
    if manager.running_count >= SUBAGENT_MAX_RUNNING_TASKS:
        return tool_ctx.build_failure_result(invocation.tool_name, "后台任务已达到并发上限。")
    arguments = invocation.arguments
    if arguments["action_type"] not in {"run_sop", "resume_confirm", "stop"}:
        return tool_ctx.build_failure_result(invocation.tool_name, "未知手机 action_type。")
    task_id = manager.allocate_task_id()
    await asyncio.to_thread(_register_phone_request, arguments, task_id)

    async def record_request(task: SubAgentTask) -> SubAgentOutcome:
        return SubAgentOutcome(
            conclusion="手机指令已登记，等待 Heartflow / ARTEMIS 消费；尚未确认设备执行。",
            tool_usage={"phone_action": 1},
        )

    manager.start(
        task_id=task_id, description=json.dumps(arguments, ensure_ascii=False),
        granted_tools=["phone_action"], runner=record_request,
        on_finished=tool_ctx.runtime.deliver_subagent_result,
    )
    return tool_ctx.build_success_result(
        invocation.tool_name,
        "手机指令已登记，等待设备执行器消费。",
        structured_content={"task_id": task_id, "status": "pending", **arguments},
    )
