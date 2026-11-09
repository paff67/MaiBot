from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.embodied_workspace.cli_bridge import (
    CLIBridge,
    WorkspaceJobRequest,
    SANDBOX_WORKSPACE_PATH,
)
from plugins.embodied_workspace.compute_economy import (
    ComputeEconomyManager,
    ComputeEconomySnapshot,
)
from plugins.embodied_workspace.editorial_kit import ensure_maibot_kit_assets
from plugins.embodied_workspace.plugin import create_plugin
from src.companion_vault.truth_db import CompanionTruthDB


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
async def test_group_async_delivery_target_id_and_source_message_id_binding(tmp_path: Path):
    ws_root = tmp_path / "workspaces"
    truth_db_path = tmp_path / "truth.db"
    ledger_db_path = tmp_path / "ledger.db"
    router_db_path = tmp_path / "routes.db"

    # 初始化真值库与消息账本，模拟入站群消息
    truth_db = CompanionTruthDB(str(truth_db_path))
    truth_db.close()

    with sqlite3.connect(str(ledger_db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                msg_id TEXT PRIMARY KEY,
                chat_id TEXT,
                sender_id TEXT,
                sender_name TEXT,
                body TEXT,
                direction TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO messages (msg_id, chat_id, sender_id, sender_name, body, direction, created_at)
            VALUES ('10099', 'group_xyz@chatroom', 'wxid_sender_01', 'Alice', '请帮我写个攻略', 'inbound', '2026-11-09T12:00:00')
            """
        )

    plugin = create_plugin()
    plugin.config.workspaces_root = str(ws_root)
    plugin.config.truth_db_path = str(truth_db_path)
    plugin.config.ledger_db_path = str(ledger_db_path)
    plugin.config.router_db_path = str(router_db_path)
    plugin.config.gateway_port = 18799

    mock_ctx = MagicMock()
    mock_ctx.call_capability = AsyncMock()
    plugin._set_context(mock_ctx)

    await plugin.on_load()

    try:
        # 反查 source_message_id
        src_id = plugin.cli_bridge.resolve_latest_source_message_id("group_xyz@chatroom", "wxid_sender_01")
        assert src_id == "10099"

        # 模拟执行完成投递通知
        ws_dir = plugin.cli_bridge.get_chat_workspace("group_xyz@chatroom")
        dist_dir = ws_dir / "dist" / "app-demo"
        project_dir = ws_dir / "projects" / "app-demo"
        dist_dir.mkdir(parents=True, exist_ok=True)
        project_dir.mkdir(parents=True, exist_ok=True)
        (dist_dir / "index.html").write_text("<h1>Demo</h1>", encoding="utf-8")

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

        # 验证 proactive trigger 被调用且携带 target_id 与 source_message_id
        proactive_calls = [
            c for c in mock_ctx.call_capability.call_args_list
            if c[0][0] == "maisaka.proactive.trigger"
        ]
        assert len(proactive_calls) == 1
        _, kwargs = proactive_calls[0]
        assert kwargs["target_id"] == "group_xyz@chatroom"
        assert kwargs["source_message_id"] == "10099"
        assert "job_test_dedup_01" in kwargs["intent"]

        # 验证防重锁：同一 job_id 再次调用 _run_slow_job_and_notify 不会再次触发
        await plugin._run_slow_job_and_notify(req)
        proactive_calls_after = [
            c for c in mock_ctx.call_capability.call_args_list
            if c[0][0] == "maisaka.proactive.trigger"
        ]
        assert len(proactive_calls_after) == 1
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

    truth_db.close()
