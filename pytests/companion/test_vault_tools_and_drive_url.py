"""drive_url 透传、search_vault_files / inspect_vault_file 跨聊权限隔离与 one_pass_st Pass 2 多模态传递测试。"""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.chat.message_receive.message import SessionMessage
from src.common.data_models.mai_message_data_model import GroupInfo, MessageInfo, UserInfo
from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
from src.companion_vault.access_policy import authorized_file_rows, replace_group_members
from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.message_media_worker import MessageMediaQueue
from src.companion_vault.quote_recall import recall_message_attachments, recall_quotes
from src.companion_vault.runtime_bridge import build_planner_truth_catalog_async
from src.companion_vault.truth_db import CompanionTruthDB
from src.core.tooling import ToolExecutionContext, ToolInvocation, ToolRegistry
from src.maisaka.builtin_tool import BUILTIN_TOOL_ENTRIES
from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
from src.maisaka.builtin_tool.provider import MaisakaBuiltinToolProvider
from src.maisaka.context.messages import SessionBackedMessage
from src.maisaka.reasoning_engine import CycleRuntimeState, MaisakaReasoningEngine
from src.platform_io.wechat.message_ingestor import MessageLedger
from src.platform_io.wechat.projection import native_message_id

SRC = "http://100.87.39.37:8765"
ACC = "0123456789abcdef0123456789abcdef"
DM_CHAT = "wxid_friend_dm"
GRP_CHAT = "123456@chatroom"


def _seed_vault_fixture(tmp_path: Path):
    truth_path = tmp_path / "truth.db"
    ledger_path = tmp_path / "ledger.db"
    spool = tmp_path / "assets"

    db = CompanionTruthDB(str(truth_path))
    assets = AssetPipeline(db, str(spool))
    ledger = MessageLedger(str(ledger_path))
    MessageMediaQueue(assets, ledger.db, SRC, None)

    seq_counter = 0

    def put_msg(msg_id: str, chat_id: str, kind: str = "49", content: str = "<msg><appmsg><type>6</type><title>doc</title></appmsg></msg>"):
        nonlocal seq_counter
        seq_counter += 1
        ledger.ingest(
            SRC,
            "ep1",
            {
                "seq": seq_counter,
                "account": ACC,
                "msg_id": msg_id,
                "chat_id": chat_id,
                "revision": f"r{msg_id}",
                "observed_at_ms": seq_counter,
                "row": {
                    "msgId": msg_id,
                    "msgSvrId": "900" + msg_id,
                    "talker": chat_id,
                    "type": kind,
                    "content": f"wxid_other:\n{content}" if chat_id.endswith("@chatroom") and not content.startswith("wxid_") else content,
                    "isSend": "0",
                },
            },
        )

    # 群成员集合
    replace_group_members(db.conn, source=SRC, account=ACC, group_id=GRP_CHAT, user_ids=["wxid_bot", "wxid_other"])

    # DM 私密 PDF（verified + drive_url）
    put_msg("101", DM_CHAT)
    dm_pdf = assets.ingest(
        b"%PDF-1.7-dm-secret",
        {"mime_type": "application/pdf", "relative_path": "dm_secret.pdf"},
        source_id="src-dm-pdf",
        chat_id=f"{ACC}/{DM_CHAT}",
    )
    with db.conn:
        db.conn.execute(
            "UPDATE asset_sources SET drive_status='verified', drive_id='drv_dm_1', drive_url=? WHERE file_code=?",
            ("https://drive.google.com/file/d/drv_dm_1/view", dm_pdf["file_code"]),
        )
        db.conn.execute(
            "INSERT INTO message_media_jobs VALUES(?,?,?,?,?,?,0,'{}','ready',0,0,0,NULL,0)",
            ("job-dm-1", SRC, ACC, "101", "r101", DM_CHAT),
        )
        db.conn.execute(
            "INSERT INTO message_asset_links VALUES(?,?,?,?,?,?,?,1,'{}')",
            ("job-dm-1", dm_pdf["file_code"], "original", ACC, "101", "r101", DM_CHAT),
        )

    # 群聊合法 PNG 图片（verified + drive_url）
    put_msg("201", GRP_CHAT, kind="3", content="<msg><img /></msg>")
    grp_img = assets.ingest(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
        {"mime_type": "image/png", "relative_path": "grp_chart.png"},
        source_id="src-grp-img",
        chat_id=f"{ACC}/{GRP_CHAT}",
    )
    with db.conn:
        db.conn.execute(
            "UPDATE asset_sources SET drive_status='verified', drive_id='drv_grp_1', drive_url=? WHERE file_code=?",
            ("https://drive.google.com/file/d/drv_grp_1/view", grp_img["file_code"]),
        )
        db.conn.execute(
            "INSERT INTO message_media_jobs VALUES(?,?,?,?,?,?,0,'{}','ready',0,0,0,NULL,0)",
            ("job-grp-1", SRC, ACC, "201", "r201", GRP_CHAT),
        )
        db.conn.execute(
            "INSERT INTO message_asset_links VALUES(?,?,?,?,?,?,?,1,'{}')",
            ("job-grp-1", grp_img["file_code"], "original", ACC, "201", "r201", GRP_CHAT),
        )

    # 群聊待同步文件（pending，不能伪造 drive_url）与一条引用 msg 201 的消息
    put_msg("202", GRP_CHAT)
    grp_pending = assets.ingest(
        "待同步文本内容".encode(),
        {"mime_type": "text/plain", "relative_path": "pending.txt"},
        source_id="src-grp-pending",
        chat_id=f"{ACC}/{GRP_CHAT}",
    )
    with db.conn:
        db.conn.execute(
            "INSERT INTO message_media_jobs VALUES(?,?,?,?,?,?,0,'{}','ready',0,0,0,NULL,0)",
            ("job-grp-2", SRC, ACC, "202", "r202", GRP_CHAT),
        )
        db.conn.execute(
            "INSERT INTO message_asset_links VALUES(?,?,?,?,?,?,?,1,'{}')",
            ("job-grp-2", grp_pending["file_code"], "original", ACC, "202", "r202", GRP_CHAT),
        )

    # 引用 msg 201 的群聊消息 msg 203
    quote_xml = "<msg><appmsg><type>57</type><title>看看</title><refermsg><svrid>900201</svrid></refermsg></appmsg></msg>"
    put_msg("203", GRP_CHAT, kind="49", content=quote_xml)

    # 群聊已撤回消息 msg 204
    put_msg("204", GRP_CHAT, kind="10000", content="已撤回")
    grp_revoked = assets.ingest(
        b"%PDF-1.7-revoked",
        {"mime_type": "application/pdf", "relative_path": "revoked.pdf"},
        source_id="src-grp-revoked",
        chat_id=f"{ACC}/{GRP_CHAT}",
    )
    with db.conn:
        db.conn.execute(
            "UPDATE asset_sources SET drive_status='verified', drive_id='drv_rev', drive_url=? WHERE file_code=?",
            ("https://drive.google.com/file/d/drv_rev/view", grp_revoked["file_code"]),
        )
        db.conn.execute(
            "INSERT INTO message_media_jobs VALUES(?,?,?,?,?,?,0,'{}','ready',0,0,0,NULL,0)",
            ("job-grp-3", SRC, ACC, "204", "r204", GRP_CHAT),
        )
        db.conn.execute(
            "INSERT INTO message_asset_links VALUES(?,?,?,?,?,?,?,1,'{}')",
            ("job-grp-3", grp_revoked["file_code"], "original", ACC, "204", "r204", GRP_CHAT),
        )

    return {
        "truth_path": truth_path,
        "ledger_path": ledger_path,
        "spool": spool,
        "db": db,
        "assets": assets,
        "ledger": ledger,
        "dm_pdf": dm_pdf,
        "grp_img": grp_img,
        "grp_pending": grp_pending,
        "grp_revoked": grp_revoked,
    }


def _make_runtime_for_chat(chat_id: str, anchor_msg_id: str, *, is_group: bool):
    session_msg = SessionMessage(
        message_id=native_message_id(SRC, ACC, anchor_msg_id),
        timestamp=datetime.datetime.now(),
        platform="wechat",
    )
    session_msg.account_id = ACC
    session_msg.message_info = MessageInfo(
        user_info=UserInfo(user_id="wxid_other" if is_group else chat_id, user_nickname="测试"),
        group_info=GroupInfo(group_id=chat_id, group_name="测试群") if is_group else None,
    )
    session_msg.raw_message = MessageSequence([TextComponent("hello")])
    session_msg.processed_plain_text = "hello"
    hist_entry = SessionBackedMessage(
        raw_message=session_msg.raw_message,
        visible_text="hello",
        timestamp=session_msg.timestamp,
        message_id=session_msg.message_id,
        original_message=session_msg,
    )
    return SimpleNamespace(
        log_prefix="[test]",
        chat_stream=SimpleNamespace(
            platform="wechat",
            is_group_session=is_group,
            user_id="wxid_other" if is_group else chat_id,
            group_id=chat_id if is_group else "",
            account_id=ACC,
        ),
        _chat_history=[hist_entry],
    )


def test_drive_url_exposed_in_catalog_and_recalls_behind_access_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fx = _seed_vault_fixture(tmp_path)
    monkeypatch.setenv("MAIBOT_ASSET_SPOOL", str(fx["spool"]))
    monkeypatch.setenv("MAIBOT_FILES_API_ENABLED", "0")

    try:
        # 1. access_policy + asset_pipeline catalog + planner catalog
        grp_rows = authorized_file_rows(fx["db"].conn, fx["ledger"].db, source=SRC, account=ACC, destination_chat_id=GRP_CHAT)
        by_code = {r["file_code"]: r for r in grp_rows}
        assert fx["dm_pdf"]["file_code"] not in by_code
        assert fx["grp_revoked"]["file_code"] not in by_code
        assert by_code[fx["grp_img"]["file_code"]]["drive_url"] == "https://drive.google.com/file/d/drv_grp_1/view"
        assert by_code[fx["grp_pending"]["file_code"]]["drive_url"] == ""
        assert by_code[fx["grp_pending"]["file_code"]]["drive_status"] == "pending"

        cat_xml = fx["assets"].message_asset_catalog(f"{ACC}/{GRP_CHAT}")
        assert "https://drive.google.com/file/d/drv_grp_1/view" in cat_xml
        assert "drv_dm_1" not in cat_xml

        # 2. quote_recall.recall_quotes & recall_message_attachments
        qblock, _qfiles = recall_quotes(
            fx["ledger"].db,
            fx["assets"],
            source=SRC,
            account=ACC,
            chat_id=GRP_CHAT,
            quote_message_ids=["203"],
        )
        qpayload = json.loads(qblock)
        assert qpayload["items"][0]["file_codes"] == [fx["grp_img"]["file_code"]]
        assert qpayload["items"][0]["file_links"][0]["drive_url"] == "https://drive.google.com/file/d/drv_grp_1/view"

        ablock, _afiles = recall_message_attachments(
            fx["ledger"].db, fx["assets"], source=SRC, account=ACC, chat_id=GRP_CHAT, msg_id="202"
        )
        apayload = json.loads(ablock)
        assert apayload["file_codes"] == [fx["grp_pending"]["file_code"]]
        assert apayload["file_links"][0]["drive_status"] == "pending"
        assert "drive_url" not in apayload["file_links"][0]

        # 3. Planner catalog async injection carries drive_url
        planner_cat = asyncio.run(
            build_planner_truth_catalog_async(
                session_id="grp_test",
                is_group_chat=True,
                user_id="wxid_other",
                platform="wechat",
                db_path=str(fx["truth_path"]),
                anchor_message_id=native_message_id(SRC, ACC, "201"),
                account=ACC,
                chat_id=GRP_CHAT,
                ledger_path=str(fx["ledger_path"]),
            )
        )
        assert "https://drive.google.com/file/d/drv_grp_1/view" in planner_cat
        assert "drv_dm_1" not in planner_cat
    finally:
        fx["ledger"].db.close()
        fx["db"].close()


@pytest.mark.asyncio
async def test_vault_builtin_tools_isolation_multimodal_and_one_pass_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _seed_vault_fixture(tmp_path)
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(fx["truth_path"]))
    monkeypatch.setenv("MAIBOT_LEDGER_DB_PATH", str(fx["ledger_path"]))
    monkeypatch.setenv("MAIBOT_ASSET_SPOOL", str(fx["spool"]))
    monkeypatch.setenv("MAIBOT_FILES_API_ENABLED", "0")

    try:
        from src.maisaka.builtin_tool import build_builtin_tool_handlers

        grp_runtime = _make_runtime_for_chat(GRP_CHAT, "201", is_group=True)
        grp_ctx = BuiltinToolRuntimeContext(SimpleNamespace(), grp_runtime)
        handlers = build_builtin_tool_handlers(grp_ctx)

        # 1. 群聊检索不得包含 DM 或已撤回文件
        search_res = await handlers["search_vault_files"](
            ToolInvocation(call_id="c1", tool_name="search_vault_files", arguments={"query": "", "limit": 10}),
            ToolExecutionContext(),
        )
        assert search_res.success is True
        found_codes = {f["file_code"]: f for f in search_res.structured_content["files"]}
        assert fx["grp_img"]["file_code"] in found_codes
        assert found_codes[fx["grp_img"]["file_code"]]["drive_url"] == "https://drive.google.com/file/d/drv_grp_1/view"
        assert fx["dm_pdf"]["file_code"] not in found_codes
        assert fx["grp_revoked"]["file_code"] not in found_codes

        # 2. 群聊试图调取 DM 文件必须 fail-closed 拒绝，绝不返回元信息或字节
        deny_dm = await handlers["inspect_vault_file"](
            ToolInvocation(call_id="c2", tool_name="inspect_vault_file", arguments={"file_code": fx["dm_pdf"]["file_code"]}),
            ToolExecutionContext(),
        )
        assert deny_dm.success is False
        assert "drv_dm_1" not in (deny_dm.error_message or "")

        # 3. 群聊调取本群已授权 PNG -> 返回 drive_url + metadata._multimodal_parts
        ok_grp = await handlers["inspect_vault_file"](
            ToolInvocation(call_id="c3", tool_name="inspect_vault_file", arguments={"file_code": fx["grp_img"]["file_code"]}),
            ToolExecutionContext(),
        )
        assert ok_grp.success is True
        assert ok_grp.structured_content["drive_url"] == "https://drive.google.com/file/d/drv_grp_1/view"
        mm_parts = ok_grp.metadata.get("_multimodal_parts") or []
        assert len(mm_parts) == 1
        assert "image_base64" in mm_parts[0]
        assert mm_parts[0]["file_code"] == fx["grp_img"]["file_code"]

        # 4. 私聊可正常调取自己的 PDF -> 返回 binary_base64 + drive_url
        dm_runtime = _make_runtime_for_chat(DM_CHAT, "101", is_group=False)
        dm_handlers = build_builtin_tool_handlers(BuiltinToolRuntimeContext(SimpleNamespace(), dm_runtime))
        ok_dm = await dm_handlers["inspect_vault_file"](
            ToolInvocation(call_id="c4", tool_name="inspect_vault_file", arguments={"file_code": fx["dm_pdf"]["file_code"]}),
            ToolExecutionContext(),
        )
        assert ok_dm.success is True
        assert ok_dm.structured_content["drive_url"] == "https://drive.google.com/file/d/drv_dm_1/view"
        dm_parts = ok_dm.metadata.get("_multimodal_parts") or []
        assert len(dm_parts) == 1 and "binary_base64" in dm_parts[0]

        # 5. _handle_tool_calls 把 _multimodal_parts 注入 CycleRuntimeState.tool_multimodal_parts
        provider = MaisakaBuiltinToolProvider(handlers)
        registry = ToolRegistry()
        registry.register_provider(provider)
        fake_runtime = SimpleNamespace(
            log_prefix="[test]",
            chat_stream=grp_runtime.chat_stream,
            _tool_registry=registry,
            is_action_tool_currently_available=lambda _n: True,
            _reset_consecutive_wait_count=lambda _s: None,
            _update_stage_status=lambda *_a, **_kw: None,
        )
        engine = object.__new__(MaisakaReasoningEngine)
        engine._runtime = fake_runtime
        engine._log_tool_call_source = lambda *_a, **_kw: None
        engine._build_tool_invocation = lambda tc, _t: ToolInvocation(
            call_id=tc.call_id, tool_name=tc.func_name, arguments=dict(tc.args or {})
        )
        engine._build_tool_execution_context = lambda _t: ToolExecutionContext()
        engine._build_tool_availability_context = lambda: None
        engine._record_tool_execution_effects = AsyncMock()
        engine._append_tool_execution_result = lambda *_a, **_kw: None
        engine._append_tool_display_results = AsyncMock(
            side_effect=lambda **kw: kw["tool_result_summaries"].append("ok")
            or kw["tool_monitor_results"].append({})
        )
        engine._emit_planner_progress = AsyncMock()
        engine._append_tool_post_history_messages = lambda *_a, **_kw: None
        engine._turn_passive_replyer = False

        # 确保两个工具已注册在目录中
        entry_names = {e.name for e in BUILTIN_TOOL_ENTRIES}
        assert {"search_vault_files", "inspect_vault_file"} <= entry_names

        cycle_state = CycleRuntimeState()
        cycle_detail = SimpleNamespace(time_records={})
        tc = SimpleNamespace(call_id="call_inspect_1", func_name="inspect_vault_file", args={"file_code": fx["grp_img"]["file_code"]})
        await engine._handle_tool_calls([tc], "", cycle_detail, cycle_state)
        assert len(cycle_state.tool_multimodal_parts) == 1
        assert cycle_state.tool_multimodal_parts[0]["file_code"] == fx["grp_img"]["file_code"]

        # 6. 验证 _execute_one_pass_st_turn 将 tool_multimodal_parts 传入 Pass 2 _tool_observation_file_parts
        import src.maisaka.reasoning_engine as re_mod
        import src.chat.replyer.replyer_manager as rm_mod

        fake_replyer = SimpleNamespace(
            express_model=SimpleNamespace(task_name="replyer"),
            run_st_single_turn=AsyncMock(
                return_value=(
                    SimpleNamespace(
                        tool_calls=[
                            SimpleNamespace(
                                call_id="call_inspect_pass1",
                                func_name="inspect_vault_file",
                                args={"file_code": fx["grp_img"]["file_code"]},
                            )
                        ],
                        response="",
                        reasoning="",
                    ),
                    None,
                )
            ),
        )
        captured_pass2_args: list[dict] = []

        async def fake_execute_reply(_tool_ctx, args, **_kw):
            captured_pass2_args.append(dict(args))
            res = SimpleNamespace(
                success=True,
                get_history_content=lambda: "已发送回复",
                metadata={},
            )
            return res

        monkeypatch.setattr(rm_mod.replyer_manager, "get_replyer", lambda **_kw: fake_replyer)
        monkeypatch.setattr(re_mod, "execute_reply", fake_execute_reply)

        engine._start_cycle_round = AsyncMock(return_value=(cycle_detail, "round 1"))
        engine._finalize_cycle = AsyncMock()
        engine._refresh_visual_placeholders_for_cycle = AsyncMock()
        engine._post_process_chat_history_after_cycle = AsyncMock()
        engine._refresh_chat_history_visual_placeholders = AsyncMock(return_value=0)
        engine._build_one_pass_external_tools = AsyncMock(
            return_value=[{"name": "inspect_vault_file", "description": "inspect", "parameters": []}]
        )
        engine._build_one_pass_recalled_memory_context = AsyncMock(return_value=("", []))
        engine._runtime.session_id = "grp_test"
        engine._runtime._chat_history = grp_runtime._chat_history
        engine._active_logical_turn_id = None

        await engine._execute_one_pass_st_turn(
            trigger_message=grp_runtime._chat_history[0].original_message,
            decision=SimpleNamespace(forced_slot=None, recommended_slot="sister_younger", transient_narrative_note=""),
            source_messages=[grp_runtime._chat_history[0].original_message],
        )
        assert len(captured_pass2_args) == 1
        pass2_files = captured_pass2_args[0].get("_tool_observation_file_parts") or []
        assert len(pass2_files) == 1
        assert pass2_files[0]["file_code"] == fx["grp_img"]["file_code"]
        assert "image_base64" in pass2_files[0]
    finally:
        fx["ledger"].db.close()
        fx["db"].close()
