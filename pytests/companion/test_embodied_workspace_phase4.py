from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.embodied_workspace.cli_bridge import (
    CLIBridge,
    WorkspaceJobRequest,
)
from plugins.embodied_workspace.compute_economy import (
    ComputeEconomyManager,
)
from plugins.embodied_workspace.editorial_kit import ensure_maibot_kit_assets
from plugins.embodied_workspace.plugin import create_plugin
from src.companion_vault.truth_db import CompanionTruthDB
from src.platform_io.wechat.message_ingestor import MessageLedger


@pytest.mark.asyncio
async def test_global_state_row_id_and_zero_double_counting(tmp_path: Path):
    truth_db_path = tmp_path / "truth.db"
    truth_db = CompanionTruthDB(str(truth_db_path))

    manager = ComputeEconomyManager(
        truth_db_path=truth_db_path,
        api_key="test_mock_key",
    )

    # 1. 模拟初始远端余额为 10.00
    snap0 = await manager.apply_remote_balance_reading(10.00, is_available=True)
    assert snap0.balance_usd == 10.00
    assert snap0.remote_balance_usd == 10.00
    assert snap0.unsettled_local_cost_usd == 0.0
    assert snap0.fairy_total_spent_usd == 0.0
    assert snap0.tier_code == "STANDARD"

    # 2. 本地 2 次调用预扣各 0.25
    snap1 = manager.record_local_cost(0.25)
    assert snap1.unsettled_local_cost_usd == 0.25
    assert snap1.balance_usd == 9.75
    assert snap1.fairy_total_spent_usd == 0.25

    snap2 = manager.record_local_cost(0.25)
    assert snap2.unsettled_local_cost_usd == 0.50
    assert snap2.balance_usd == 9.50
    assert snap2.fairy_total_spent_usd == 0.50

    # 3. 远端同步返回 9.40 (真实消耗了 0.60)，预扣缓存归零，累计消耗精确为 0.60 (零双重记账)
    snap3 = await manager.apply_remote_balance_reading(9.40, is_available=True)
    assert snap3.unsettled_local_cost_usd == 0.0
    assert snap3.balance_usd == 9.40
    assert snap3.remote_balance_usd == 9.40
    assert snap3.fairy_total_spent_usd == 0.60

    # 4. 验证 SQLite 数据库中 global_state 表严格按 row_id = 1 持久化
    with sqlite3.connect(str(truth_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM global_state WHERE row_id = 1").fetchone()
        assert row is not None
        assert float(row["fairy_remote_balance_usd"]) == 9.40
        assert float(row["fairy_unsettled_cost_usd"]) == 0.0
        assert float(row["fairy_total_spent_usd"]) == 0.60
        assert row["fairy_compute_tier"] == "STANDARD"

    truth_db.close()


@pytest.mark.asyncio
async def test_unified_slow_track_and_unscripted_fast_track_hint(tmp_path: Path):
    ws_root = tmp_path / "workspaces"
    truth_db_path = tmp_path / "truth.db"
    ledger_db_path = tmp_path / "ledger.db"
    router_db_path = tmp_path / "routes.db"

    truth_db = CompanionTruthDB(str(truth_db_path))
    truth_db.close()

    plugin = create_plugin()
    plugin.config.workspaces_root = str(ws_root)
    plugin.config.truth_db_path = str(truth_db_path)
    plugin.config.ledger_db_path = str(ledger_db_path)
    plugin.config.router_db_path = str(router_db_path)
    plugin.config.gateway_port = 18798

    mock_ctx = MagicMock()
    mock_ctx.call_capability = AsyncMock()
    plugin._set_context(mock_ctx)

    await plugin.on_load()

    try:
        # 1. 验证调用慢轨统一入口 run_agent_workspace(task_type="document")
        res = await plugin.run_agent_workspace(
            task_prompt="撰写一份东京旅行美食指南",
            project_slug="tokyo-food",
            task_type="document",
            context_notes="用户偏好拉面与甜品",
            group_id="test_room@chatroom",
            user_id="wxid_traveler",
        )

        assert res["ok"] is True
        assert res["mode"] == "slow_track_async"
        assert res["executor"] == "Fairy"
        assert res["task_type"] == "document"
        assert "task_id" in res

        # 验证返回的 planner_hint 纯净无预设硬编码剧情台词
        hint = res["planner_hint"]
        assert "reply" in hint
        assert "Fairy" in hint
        # 严禁出现硬编码剧本
        for forbidden in ("泡咖啡", "练琴", "撒娇", "学长"):
            assert forbidden not in hint

        # 2. 验证三层外围配置生成
        ws_dir = plugin.cli_bridge.get_chat_workspace("test_room@chatroom")
        dist_dir = ws_dir / "dist" / "tokyo-food"
        dist_dir.mkdir(parents=True, exist_ok=True)

        req = WorkspaceJobRequest(
            job_id=res["task_id"],
            stream_id="stream_001",
            chat_id="test_room@chatroom",
            target_id="test_room@chatroom",
            project_slug="tokyo-food",
            task_type="document",
            task_prompt="撰写一份东京旅行美食指南",
            context_notes="用户偏好拉面与甜品",
        )

        ensure_maibot_kit_assets(ws_dir, dist_dir)
        plugin.cli_bridge._write_task_context_md(ws_dir, req)
        plugin.cli_bridge._write_agents_md(ws_dir, "tokyo-food", "creator_only", "document")

        assert (ws_dir / "AGENTS.md").is_file()
        assert (ws_dir / "TASK_CONTEXT.md").is_file()
        assert (ws_dir / ".maibot_kit" / "editorial_theme.css").is_file()
        assert (ws_dir / ".maibot_kit" / "editorial_kit.js").is_file()
        assert (dist_dir / "_kit" / "editorial_theme.css").is_file()
        assert (dist_dir / "_kit" / "editorial_kit.js").is_file()

        task_ctx_text = (ws_dir / "TASK_CONTEXT.md").read_text(encoding="utf-8")
        assert "用户偏好拉面与甜品" in task_ctx_text
        assert "tokyo-food" in task_ctx_text
        assert "document" in task_ctx_text
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_group_async_delivery_native_capabilities_and_multislot_unload(tmp_path: Path):
    ws_root = tmp_path / "workspaces"
    truth_db_path = tmp_path / "truth.db"
    ledger_db_path = tmp_path / "ledger.db"
    router_db_path = tmp_path / "routes.db"

    source = "https://127.0.0.1:8443"
    account = "4d86ac1b06dd252fa3c7327eb5614865"

    truth_db = CompanionTruthDB(str(truth_db_path))
    truth_db.close()

    ledger = MessageLedger(str(ledger_db_path))
    ledger.ingest(
        source,
        "epoch1",
        {
            "seq": 1,
            "account": account,
            "msg_id": "10099",
            "chat_id": "group_xyz@chatroom",
            "revision": "r1",
            "observed_at_ms": 1762680000000,
            "row": {
                "msgId": "10099",
                "talker": "group_xyz@chatroom",
                "type": "1",
                "content": "wxid_sender_01:\n请帮我写个攻略",
                "msgSvrId": "9001",
                "isSend": "0",
                "createTime": "1762680000000",
            },
        },
    )
    ledger.db.close()

    plugin = create_plugin()
    plugin.set_plugin_config({
        "plugin": {"config_version": "1.5.0"},
        "workspaces_root": str(ws_root),
        "truth_db_path": str(truth_db_path),
        "ledger_db_path": str(ledger_db_path),
        "router_db_path": str(router_db_path),
        "wechat_source": source,
        "wechat_account": account,
        "gateway_port": 0,
    })

    mock_ctx = MagicMock()
    mock_ctx.call_capability = AsyncMock()
    plugin._set_context(mock_ctx)
    await plugin.on_load()

    try:
        src_id = plugin.cli_bridge.resolve_latest_source_message_id("group_xyz@chatroom", "wxid_sender_01")
        assert src_id == "10099"

        ws_dir = plugin.cli_bridge.get_chat_workspace("group_xyz@chatroom")
        dist_dir = ws_dir / "dist" / "app-demo"
        project_dir = ws_dir / "projects" / "app-demo"
        dist_dir.mkdir(parents=True, exist_ok=True)
        project_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.html").write_text("<h1>Demo</h1>", encoding="utf-8")

        # 预置另一个群仍在运行的长任务槽位，验证本任务结束不会覆盖清空它
        plugin._active_jobs["job_other_group"] = {
            "project_slug": "other-proj",
            "latest_status": "正在执行数据分析",
        }

        req = WorkspaceJobRequest(
            job_id="job_test_dedup_01",
            stream_id="stream_grp",
            chat_id="group_xyz@chatroom",
            target_id="group_xyz@chatroom",
            source_message_id="10099",
            creator_wxid="wxid_sender_01",
            project_slug="app-demo",
            task_type="webapp",
        )

        plugin.cli_bridge.run_cli_job = AsyncMock(return_value=MagicMock(
            ok=True,
            title="群聊演示项目",
            summary_for_replyer="这是为群聊构建的应用",
            mode="static",
            start_cmd=[],
            archive_files=[],
            dist_dir=dist_dir,
            project_dir=project_dir,
        ))

        await plugin._run_slow_job_and_notify(req)

        # 验证并发任务槽位互不清空
        assert "job_other_group" in plugin._active_jobs
        assert "other-proj" in plugin.get_active_jobs_summary()

        # 验证调用宿主 Capability 仅传真实支持的参数，绝不含 target_id / source_message_id 伪参数
        ctx_calls = [c for c in mock_ctx.call_capability.call_args_list if c[0][0] == "maisaka.context.append"]
        assert len(ctx_calls) == 1
        _, ctx_kwargs = ctx_calls[0]
        assert set(ctx_kwargs.keys()) == {"stream_id", "segments", "visible_text", "source_kind"}
        assert "[引用: 10099]" in ctx_kwargs["visible_text"]

        proactive_calls = [c for c in mock_ctx.call_capability.call_args_list if c[0][0] == "maisaka.proactive.trigger"]
        assert len(proactive_calls) == 1
        _, pro_kwargs = proactive_calls[0]
        assert set(pro_kwargs.keys()) == {"stream_id", "intent"}
        assert "job_test_dedup_01" in pro_kwargs["intent"]

        # 防重锁验证
        await plugin._run_slow_job_and_notify(req)
        assert len([c for c in mock_ctx.call_capability.call_args_list if c[0][0] == "maisaka.proactive.trigger"]) == 1

        # 验证 on_unload 显式取消并回收在途后台任务
        cancelled_flag = False

        async def slow_sleeper() -> None:
            nonlocal cancelled_flag
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled_flag = True
                raise

        bg = asyncio.create_task(slow_sleeper())
        plugin._bg_tasks.add(bg)
        await asyncio.sleep(0.02)
        await plugin.on_unload()
        assert cancelled_flag is True
        assert bg.done()
    finally:
        await plugin.on_unload()


def test_truth_db_twins_background_and_public_private_isolation(tmp_path: Path):
    truth_db_path = tmp_path / "truth.db"
    truth_db = CompanionTruthDB(str(truth_db_path))

    # 更新 global_state 中的算力状态
    with truth_db.conn:
        truth_db.conn.execute("INSERT OR IGNORE INTO global_state (row_id) VALUES (1)")
        cols = {str(r["name"]) for r in truth_db.conn.execute("PRAGMA table_info(global_state)").fetchall()}
        if "fairy_remote_balance_usd" not in cols:
            truth_db.conn.execute("ALTER TABLE global_state ADD COLUMN fairy_remote_balance_usd REAL DEFAULT 5.0")
            truth_db.conn.execute("ALTER TABLE global_state ADD COLUMN fairy_unsettled_cost_usd REAL DEFAULT 0.0")
            truth_db.conn.execute("ALTER TABLE global_state ADD COLUMN fairy_total_spent_usd REAL DEFAULT 0.0")
            truth_db.conn.execute("ALTER TABLE global_state ADD COLUMN fairy_compute_tier TEXT DEFAULT 'STANDARD'")
            truth_db.conn.execute("ALTER TABLE global_state ADD COLUMN active_sister TEXT DEFAULT 'flash_primary'")
        truth_db.conn.execute(
            """
            UPDATE global_state SET
                fairy_remote_balance_usd = 8.50,
                fairy_unsettled_cost_usd = 0.50,
                fairy_total_spent_usd = 12.30,
                fairy_compute_tier = 'STANDARD',
                active_sister = 'flash_primary'
            WHERE row_id = 1
            """
        )

    # 1. 验证 Owner 私聊视图
    owner_injection = truth_db.build_planner_catalog_injection(
        is_group_chat=False,
        sender_trust_role="OWNER",
    )
    assert "<算力工况与工作区状态 (Owner私聊视图)>" in owner_injection
    assert "算力工况档位：日常标准态 (STANDARD)" in owner_injection
    assert "当前模型槽位：flash_primary" in owner_injection
    assert "实时算力可用余额：$8.00 USD" in owner_injection
    assert "累计消耗总额：$12.30 USD" in owner_injection

    # 2. 验证群聊公共视图（脱敏，严格隐藏美元具体余额）
    group_injection = truth_db.build_planner_catalog_injection(
        is_group_chat=True,
        sender_trust_role="GROUP_MEMBER",
        chat_id="test_room@chatroom",
    )
    assert "<工作区运行状态>" in group_injection
    assert "<算力工况与工作区状态 (Owner私聊视图)>" not in group_injection
    assert "算力工况档位：日常标准态 (STANDARD)" in group_injection
    assert "当前模型槽位：flash_primary" in group_injection
    # 严格确保脱敏：群聊中绝不泄露美元金额
    assert "实时算力可用余额" not in group_injection
    assert "累计消耗总额" not in group_injection
    assert "$8.00" not in group_injection
    assert "$12.30" not in group_injection

    # 3. 严格断言返回文本中绝不包含任何硬编码角色设定词汇
    for forbidden in ("DeepMind", "文京区", "本乡三丁目", "东京大学", "修士", "双子", "合租", "资助人"):
        assert forbidden not in owner_injection
        assert forbidden not in group_injection

    # 4. 验证运行中慢轨长程任务的动态状态投影（列已由 CompanionTruthDB 单一 Schema 原生提供，无需动态 ALTER TABLE）
    with truth_db.conn:
        truth_db.conn.execute(
            """
            UPDATE global_state SET fairy_active_job_summary = '项目【doc-jensen】已运行 3 分钟：已生成调研脚本与数据集'
            WHERE row_id = 1
            """
        )

    owner_injection_active = truth_db.build_planner_catalog_injection(
        is_group_chat=False,
        sender_trust_role="OWNER",
    )
    assert "- 后台慢轨长程任务：项目【doc-jensen】已运行 3 分钟：已生成调研脚本与数据集" in owner_injection_active

    group_injection_active = truth_db.build_planner_catalog_injection(
        is_group_chat=True,
        sender_trust_role="GROUP_MEMBER",
        chat_id="test_room@chatroom",
    )
    assert "- 后台慢轨任务：正在执行长程调研/构建中" in group_injection_active

    truth_db.close()


@pytest.mark.asyncio
async def test_3_hours_timeout_and_progress_monitoring_and_hook_compatibility(tmp_path: Path):
    ws_root = tmp_path / "workspaces"
    truth_db_path = tmp_path / "truth.db"
    truth_db = CompanionTruthDB(str(truth_db_path))
    truth_db.close()

    plugin = create_plugin()
    plugin.config.workspaces_root = str(ws_root)
    plugin.config.truth_db_path = str(truth_db_path)
    plugin.config.base_domain = "paff-67.top"

    # 1. 验证默认超时为 3 小时 (10800 秒)
    assert plugin.config.cli_timeout_seconds == 10800
    bridge = CLIBridge(
        workspaces_root=ws_root,
        truth_db_path=truth_db_path,
        ledger_db_path=tmp_path / "ledger.db",
        wechat_source="http://100.87.39.37:8765",
        wechat_account="test_acc",
    )
    assert bridge.timeout_seconds == 10800

    # 2. 验证 inspect_project_progress 进度提炼逻辑
    proj_dir = ws_root / "test_chat" / "projects" / "test_proj"
    dist_dir = ws_root / "test_chat" / "dist" / "test_proj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    # (a) 初始状态只有脚本
    (proj_dir / "fetch_news.py").write_text("print('hello')", encoding="utf-8")
    status1 = bridge.inspect_project_progress(proj_dir, dist_dir, [], 30)
    assert "正在编写并执行数据检索与调研脚本" in status1

    # (b) 数据集抓取完成
    (proj_dir / "raw_search.json").write_text("{" * 2048, encoding="utf-8")
    status2 = bridge.inspect_project_progress(proj_dir, dist_dir, [], 60)
    assert "已采集关键研究素材" in status2
    assert "raw_search.json" in status2

    # (c) 初版页面产出
    (dist_dir / "index.html").write_text("<h1>Report</h1>", encoding="utf-8")
    status3 = bridge.inspect_project_progress(proj_dir, dist_dir, [], 90)
    assert "已生成初版页面 (index.html)" in status3

    # 3. 验证 protect_subdomain_url_hook 原生 Runner kwargs 协议（已彻底移除旧版单一 dict 原地修改死分支）
    runner_kwargs_res = await plugin.protect_subdomain_url_hook(
        response="任务已完成，访问：https://abc123xyz789.paff-67.top",
        session_id="sess_01",
        reply_message_id="msg_01",
        skip_post_process=False,
        enable_chinese_typo=True,
        enable_splitter=True,
    )
    assert runner_kwargs_res["action"] == "continue"
    assert runner_kwargs_res["modified_kwargs"]["skip_post_process"] is False
    assert runner_kwargs_res["modified_kwargs"]["enable_chinese_typo"] is False
    assert runner_kwargs_res["modified_kwargs"]["enable_splitter"] is False

    # 4. 验证 cli_timeout_seconds 允许设置小于 10800 秒（仅保留 >= 30s 防呆）
    plugin.config.cli_timeout_seconds = 90
    assert plugin._effective_timeout_seconds() == 90
    plugin.config.cli_timeout_seconds = 5
    assert plugin._effective_timeout_seconds() == 30
