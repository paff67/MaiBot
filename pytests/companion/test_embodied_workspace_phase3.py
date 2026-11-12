from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from plugins.embodied_workspace import create_plugin
from plugins.embodied_workspace.gateway_server import WorkspaceGatewayServer
from plugins.embodied_workspace.realtime_hub import GroupRealtimeHub
from plugins.embodied_workspace.resource_quota import QuotaLimits, ResourceQuotaWrapper
from plugins.embodied_workspace.subdomain_router import SubdomainRouter
from plugins.embodied_workspace.time_machine import WorkspaceTimeMachine
from plugins.embodied_workspace.user_truth_writer import UserScopedTruthWriter
from src.companion_vault.truth_db import CompanionTruthDB


# ---------------------------------------------------------------------------
# 断言 1: cgroups v2 资源硬配额包装与权限降级测试 (隐患 5 修复)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resource_quota_named_scope_and_atomic_stop_unit(monkeypatch: pytest.MonkeyPatch):
    limits = QuotaLimits(
        enabled=True,
        cpu_quota_percent=150,
        memory_max_mb=1024,
        tasks_max=256,
        oom_score_adjust=500,
    )
    wrapper = ResourceQuotaWrapper(limits)

    wrapper._systemd_mode = "system"
    base_cmd = ["bwrap", "--clearenv", "--ro-bind", "/", "/", "--", "echo", "hello"]
    cmd_system = wrapper.wrap_command(base_cmd, unit_name="maibot-fairy-cli-job_42")
    assert cmd_system[0] == "systemd-run"
    assert "--scope" in cmd_system
    assert "--unit=maibot-fairy-cli-job_42" in cmd_system
    assert "--collect" in cmd_system
    assert "--property=CPUQuota=150%" in cmd_system
    assert f"--property=MemoryHigh={int(1024 * 0.85)}M" in cmd_system
    assert "--property=MemoryMax=1024M" in cmd_system
    assert "--property=MemorySwapMax=0" in cmd_system
    assert "--property=TasksMax=256" in cmd_system
    assert not any("OOMScoreAdjust" in arg for arg in cmd_system)
    assert cmd_system[-len(base_cmd):] == base_cmd

    wrapper._systemd_mode = "user"
    cmd_user = wrapper.wrap_command(base_cmd, unit_name="maibot-fairy-be-tok1")
    assert cmd_user[0] == "systemd-run"
    assert cmd_user[1] == "--user"
    assert "--unit=maibot-fairy-be-tok1" in cmd_user

    # 验证 stop_unit 优先调用 systemctl kill --kill-who=all <unit>.scope
    recorded_cmds: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):
        recorded_cmds.append(args)
        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        return mock_proc

    monkeypatch.setattr("shutil.which", lambda name: "/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    await wrapper.stop_unit("maibot-fairy-be-tok1", proc=None)
    assert len(recorded_cmds) == 1
    assert recorded_cmds[0] == (
        "systemctl",
        "--user",
        "kill",
        "--kill-who=all",
        "--signal=SIGKILL",
        "maibot-fairy-be-tok1.scope",
    )

    wrapper.limits.enabled = False
    assert wrapper.wrap_command(base_cmd, unit_name="ignored") == base_cmd


@pytest.mark.asyncio
async def test_embodied_workspace_on_config_update_and_unclamped_timeout(tmp_path: Path):
    """验证插件原生 _set_context、on_config_update 全字段热更以及解除 max(10800) 硬钳制"""
    plugin = create_plugin()
    mock_ctx = AsyncMock()
    mock_ctx.call_capability = AsyncMock(return_value={"ok": True})
    plugin._set_context(mock_ctx)
    plugin.set_plugin_config({
        "plugin": {"config_version": "1.5.0"},
        "workspaces_root": str(tmp_path / "workspaces"),
        "router_db_path": str(tmp_path / "routes.db"),
        "truth_db_path": str(tmp_path / "truth.db"),
        "ledger_db_path": str(tmp_path / "ledger.db"),
        "base_domain": "old.example.com",
        "gateway_port": 0,
        "cli_timeout_seconds": 600,
        "fairy_cli_model": "gemini-4-argon-high",
        "cli_auth_dir": str(tmp_path / "auth1"),
        "wechat_source": "http://127.0.0.1:8765",
    })
    await plugin.on_load()
    assert plugin.cli_bridge.timeout_seconds == 600
    assert plugin.truth_writer.wechat_source == "http://127.0.0.1:8765"

    new_cfg = {
        "plugin": {"config_version": "1.5.0"},
        "workspaces_root": str(tmp_path / "workspaces"),
        "router_db_path": str(tmp_path / "routes.db"),
        "truth_db_path": str(tmp_path / "truth.db"),
        "ledger_db_path": str(tmp_path / "ledger.db"),
        "base_domain": "new.example.com",
        "cli_timeout_seconds": 120,
        "fairy_cli_model": "gemini-4-flash-preview",
        "cli_auth_dir": str(tmp_path / "auth2"),
        "wechat_source": "http://127.0.0.1:9999",
        "wechat_account": "4d86ac1b06dd252fa3c7327eb5614865",
    }
    await plugin.on_config_update(scope="self", config_data=new_cfg, version="1.5.1")
    await plugin.on_config_update(scope="bot", config_data={"nickname": "双子姐姐"}, version="1.5.1")
    assert plugin.cli_bridge.timeout_seconds == 120
    assert plugin.cli_bridge.fairy_cli_model == "gemini-4-flash-preview"
    assert plugin.router.base_domain == "new.example.com"
    assert plugin.gateway.default_issuer_name == "双子姐姐"
    assert plugin.truth_writer.wechat_source == "http://127.0.0.1:9999"
    assert plugin.truth_writer.wechat_account == "4d86ac1b06dd252fa3c7327eb5614865"
    await plugin.on_unload()


# ---------------------------------------------------------------------------
# 断言 2: 单目录演进与回滚原子性测试 (隐患 1 + 隐患 3 修复)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_time_machine_single_dir_evolution_and_rollback(tmp_path: Path):
    ws_root = tmp_path / "workspaces"
    chat_id = "group_camping@chatroom"
    chat_ws_dir = ws_root / "group_camping_chatroom"
    slug = "camp_vote"

    dist_dir = chat_ws_dir / "dist" / slug
    project_dir = chat_ws_dir / "projects" / slug
    dist_dir.mkdir(parents=True, exist_ok=True)

    tm = WorkspaceTimeMachine(ws_root)

    # 阶段 1: v1 纯静态版本（仅生成 dist/<slug>/index.html，不存在 projects/<slug>）
    (dist_dir / "index.html").write_text("<html>v1 static</html>", encoding="utf-8")
    (dist_dir / "result.json").write_text(
        json.dumps({"title": "露营投票 v1", "mode": "static", "start_cmd": []}),
        encoding="utf-8",
    )
    snap1 = await tm.capture_snapshot(chat_ws_dir, slug, job_id="job_001", action="create", summary="初始静态页面")
    assert snap1 is not None

    # 阶段 2: v2 全栈演进（新增 projects/<slug>/app.py，更新 dist/<slug>）
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "app.py").write_text("# backend v2", encoding="utf-8")
    (dist_dir / "index.html").write_text("<html>v2 fullstack</html>", encoding="utf-8")
    (dist_dir / "result.json").write_text(
        json.dumps({"title": "露营投票 v2", "mode": "backend", "start_cmd": ["python3", "app.py"]}),
        encoding="utf-8",
    )
    snap2 = await tm.capture_snapshot(chat_ws_dir, slug, job_id="job_002", action="update", summary="增加后端服务")
    assert snap2 is not None

    history = await tm.list_snapshots(chat_ws_dir, slug)
    assert len(history) == 2
    diff_stat = await tm.get_latest_diff_stat(chat_ws_dir, slug)
    assert "file" in diff_stat or "insert" in diff_stat or diff_stat != ""

    # 装配插件验证 _perform_project_rollback 保持原路由 stream_id、issuer_name、access_policy 并遵守互斥锁与权限
    truth_db_path = tmp_path / "truth.db"
    truth_db = CompanionTruthDB(str(truth_db_path))
    with truth_db.conn:
        truth_db.conn.execute(
            """
            INSERT INTO person_info (
                person_id, platform_ids, person_name, wechat_remark,
                trust_role, one_line_intro, impression, intimacy_score, shared_episodes
            ) VALUES ('P_OWNER', ?, '号主', '', 'OWNER', '', '', 100, '[]')
            """,
            (json.dumps({"wechat": "wxid_super_owner"}),),
        )
    truth_db.close()

    plugin = create_plugin()
    mock_ctx = AsyncMock()
    mock_ctx.call_capability = AsyncMock(return_value={"ok": True})
    plugin._set_context(mock_ctx)
    plugin.set_plugin_config({
        "plugin": {"config_version": "1.5.0"},
        "workspaces_root": str(ws_root),
        "router_db_path": str(tmp_path / "routes.db"),
        "truth_db_path": str(truth_db_path),
        "ledger_db_path": str(tmp_path / "ledger.db"),
        "base_domain": "paff-67.top",
        "gateway_port": 0,
    })
    await plugin.on_load()

    token = "cv9999"
    plugin.router.upsert_route(
        token=token,
        chat_id=chat_id,
        stream_id="stream_xyz789",
        creator_wxid="wxid_alice",
        issuer_name="许妍",
        access_policy="creator_only",
        project_slug=slug,
        title="露营投票 v2",
        summary="v2后端全栈",
        mode="backend",
        dist_dir=dist_dir,
        project_dir=project_dir,
        start_cmd=["python3", "app.py"],
    )

    # 1. 权限拦截：creator_only 项目严禁非创建者普通群成员回滚或撤销
    denied_rb = await plugin.rollback_workspace_cmd(
        group_id=chat_id,
        user_id="wxid_random_member",
        text=f"/rollback {slug} 1",
        matched_groups={"slug": slug, "version": "1"},
    )
    assert "创建者独占模式" in denied_rb

    denied_rev = await plugin.revoke_link_cmd(
        group_id=chat_id,
        user_id="wxid_random_member",
        text=f"/revoke {token}",
        matched_groups={"token": token},
    )
    assert "创建者独占模式" in denied_rev

    # 2. 互斥锁拦截：构建锁占用期间禁止并发回滚破坏工作目录
    async with plugin.cli_bridge.project_lock(chat_id, slug):
        locked_rb = await plugin._perform_project_rollback(chat_id, slug, target_spec="1", actor_wxid="wxid_alice")
        assert locked_rb["ok"] is False
        assert "正在后台构建中" in locked_rb["error"]

    # 3. 创建者本人执行回滚至 v1（纯静态版本）
    rb_res = await plugin._perform_project_rollback(chat_id, slug, target_spec="1", actor_wxid="wxid_alice")
    assert rb_res["ok"] is True
    assert rb_res["mode"] == "static"
    assert (dist_dir / "index.html").read_text(encoding="utf-8") == "<html>v1 static</html>"
    assert not (project_dir / "app.py").exists()

    restored_route = plugin.router.get_route(token)
    assert restored_route is not None
    assert restored_route.creator_wxid == "wxid_alice"
    assert restored_route.issuer_name == "许妍"
    assert restored_route.stream_id == "stream_xyz789"
    assert restored_route.mode == "static"

    # 4. OWNER 越级权限验证：OWNER 可回滚任意 creator_only 项目
    cmd_reply = await plugin.rollback_workspace_cmd(
        group_id=chat_id,
        user_id="wxid_super_owner",
        text=f"/rollback {slug} 1",
        matched_groups={"slug": slug, "version": "1"},
    )
    assert "已将项目" in cmd_reply

    tool_res = await plugin.manage_workspace_projects(
        action="rollback",
        project_slug=slug,
        target_version="1",
        group_id=chat_id,
        user_id="wxid_alice",
    )
    assert tool_res["ok"] is True

    await plugin.on_unload()


# ---------------------------------------------------------------------------
# 断言 3: 生产 message_ledger.db 真实字段与 WebSocket 实时总线 (隐患 2 修复)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_realtime_hub_production_ledger_schema_and_broadcast(tmp_path: Path):
    ledger_path = tmp_path / "message_ledger.db"

    # 使用生产 message_ingestor.py 真实表结构建表 (含 body 与 updated_at_ms)
    conn = sqlite3.connect(str(ledger_path))
    conn.execute(
        """
        CREATE TABLE messages (
            source TEXT,
            account TEXT,
            msg_id TEXT,
            chat_id TEXT NOT NULL,
            server_id TEXT,
            sender_id TEXT,
            direction TEXT,
            kind TEXT,
            body TEXT,
            raw_content TEXT,
            revision TEXT,
            row_json TEXT NOT NULL,
            updated_at_ms INTEGER,
            PRIMARY KEY(source, account, msg_id)
        )
        """
    )
    conn.commit()
    conn.close()

    hub = GroupRealtimeHub(ledger_db_path=ledger_path, poll_interval_sec=0.1)
    hub.start()

    router = SubdomainRouter(db_path=tmp_path / "routes.db", base_domain="paff-67.top")
    supervisor = MagicMock()
    truth_writer = UserScopedTruthWriter(truth_db_path=tmp_path / "truth.db")

    gateway = WorkspaceGatewayServer(
        router=router,
        supervisor=supervisor,
        truth_writer=truth_writer,
        realtime_hub=hub,
        listen_port=18795,
    )
    await gateway.start()

    token = "rt8888"
    chat_id = "group_rt_test@chatroom"
    dist_dir = tmp_path / "dist_rt"
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<html>RT test</html>", encoding="utf-8")

    router.upsert_route(
        token=token,
        chat_id=chat_id,
        stream_id="stream_rt",
        creator_wxid="wxid_rt_owner",
        access_policy="member_collect",
        project_slug="rt_vote",
        title="实时投票看板",
        mode="static",
        dist_dir=dist_dir,
        project_dir=tmp_path / "proj_rt",
    )

    try:
        ws_url = "http://127.0.0.1:18795/_maibot/ws"
        headers = {"Host": f"{token}.paff-67.top"}

        async with aiohttp.ClientSession() as session:
            # 客户端 A 与 B 建立 WebSocket 连接
            async with session.ws_connect(ws_url, headers=headers) as ws_a, \
                       session.ws_connect(ws_url, headers=headers) as ws_b:

                msg_a_init = await asyncio.wait_for(ws_a.receive_json(), timeout=2.0)
                msg_b_init = await asyncio.wait_for(ws_b.receive_json(), timeout=2.0)
                assert msg_a_init["type"] == "connected"
                assert msg_b_init["type"] == "connected"

                # 1. 验证客户端 A 广播协同事件 -> 客户端 B 实时接收（echo=False 不回送自身）
                await ws_a.send_json({
                    "action": "broadcast",
                    "event": "vote_cast",
                    "data": {"item": "tent", "count": 3},
                    "echo": False,
                })
                peer_msg = await asyncio.wait_for(ws_b.receive_json(), timeout=2.0)
                assert peer_msg["type"] == "peer_broadcast"
                assert peer_msg["event"] == "vote_cast"
                assert peer_msg["data"]["item"] == "tent"

                # 2. 验证心跳 ping -> pong
                await ws_a.send_json({"action": "ping"})
                pong = await asyncio.wait_for(ws_a.receive_json(), timeout=2.0)
                assert pong["type"] == "pong"

                # 2.5 验证超过 64KB 的超大广播包被网关总线拒绝拦截
                await ws_a.send_str(json.dumps({"action": "broadcast", "event": "huge", "data": "x" * 70000}))
                err_frame = await asyncio.wait_for(ws_a.receive_json(), timeout=2.0)
                assert err_frame["type"] == "error"

                # 3. 向生产表插入撤回行 (kind=268445456) 与正常文本行 (kind=1)，验证撤回行被物理过滤
                conn_w = sqlite3.connect(str(ledger_path))
                conn_w.execute(
                    """
                    INSERT INTO messages (
                        source, account, msg_id, chat_id, server_id, sender_id,
                        direction, kind, body, raw_content, revision, row_json, updated_at_ms
                    ) VALUES (
                        'wechat', 'acc_main', 'msg_revoked', ?, 'srv_0', 'wxid_evil',
                        'incoming', '268445456', '"wxid_evil" 撤回了一条消息', 'revoked',
                        '1', '{}', 1762679999000
                    )
                    """,
                    (chat_id,),
                )
                conn_w.execute(
                    """
                    INSERT INTO messages (
                        source, account, msg_id, chat_id, server_id, sender_id,
                        direction, kind, body, raw_content, revision, row_json, updated_at_ms
                    ) VALUES (
                        'wechat', 'acc_main', 'msg_99901', ?, 'srv_1', 'wxid_bob',
                        'incoming', '1', '大家周末露营带什么？', '大家周末露营带什么？',
                        '1', '{}', 1762680000000
                    )
                    """,
                    (chat_id,),
                )
                conn_w.commit()
                conn_w.close()

                # 两端均应在短时间内收到 wechat_message 推送
                push_a = await asyncio.wait_for(ws_a.receive_json(), timeout=2.0)
                assert push_a["type"] == "wechat_message"
                assert push_a["sender"] == "wxid_bob"
                assert push_a["content"] == "大家周末露营带什么？"
                assert push_a["timestamp"] == 1762680000000

                push_b = await asyncio.wait_for(ws_b.receive_json(), timeout=2.0)
                assert push_b["type"] == "wechat_message"
                assert push_b["content"] == "大家周末露营带什么？"

                # 4. 验证直接通过 push_chat_message_event 广播
                await hub.push_chat_message_event(chat_id, "wxid_sys", "系统广播测试", 1762680005000)
                push_event = await asyncio.wait_for(ws_a.receive_json(), timeout=2.0)
                assert push_event["sender"] == "wxid_sys"
                assert push_event["content"] == "系统广播测试"

    finally:
        await gateway.stop()
        await hub.stop()


# ---------------------------------------------------------------------------
# 断言 4: 会话项目目录查询与跨会话隔离断言
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_manage_workspace_projects_catalog_isolation(tmp_path: Path):
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir(parents=True, exist_ok=True)

    plugin = create_plugin()
    mock_ctx = AsyncMock()
    mock_ctx.call_capability = AsyncMock(return_value={"ok": True})
    plugin._set_context(mock_ctx)
    plugin.set_plugin_config({
        "plugin": {"config_version": "1.0.0"},
        "workspaces_root": str(ws_root),
        "router_db_path": str(tmp_path / "routes.db"),
        "truth_db_path": str(tmp_path / "truth.db"),
        "ledger_db_path": str(tmp_path / "ledger.db"),
        "base_domain": "paff-67.top",
        "gateway_port": 0,
    })
    await plugin.on_load()

    group_alpha = "group_alpha@chatroom"
    group_beta = "group_beta@chatroom"

    # 在群 Alpha 下创建两个项目
    plugin.router.upsert_route(
        token="tok_a1",
        chat_id=group_alpha,
        stream_id="stream_a",
        creator_wxid="wxid_user1",
        access_policy="creator_only",
        project_slug="alpha_project_1",
        title="Alpha 项目 1",
        summary="A1 摘要",
        mode="static",
        dist_dir=ws_root / "dist" / "alpha_project_1",
        project_dir=ws_root / "projects" / "alpha_project_1",
    )
    plugin.router.upsert_route(
        token="tok_a2",
        chat_id=group_alpha,
        stream_id="stream_a",
        creator_wxid="wxid_user1",
        access_policy="member_collect",
        project_slug="alpha_project_2",
        title="Alpha 项目 2",
        summary="A2 摘要",
        mode="backend",
        dist_dir=ws_root / "dist" / "alpha_project_2",
        project_dir=ws_root / "projects" / "alpha_project_2",
    )

    # 在群 Beta 下创建一个项目
    plugin.router.upsert_route(
        token="tok_b1",
        chat_id=group_beta,
        stream_id="stream_b",
        creator_wxid="wxid_user2",
        access_policy="creator_only",
        project_slug="beta_project_1",
        title="Beta 独占项目",
        summary="B1 摘要",
        mode="static",
        dist_dir=ws_root / "dist" / "beta_project_1",
        project_dir=ws_root / "projects" / "beta_project_1",
    )

    # 1. 验证查询群 Alpha 项目列表：严格隔离，绝不返回群 Beta 的项目
    res_alpha = await plugin.manage_workspace_projects(action="list", group_id=group_alpha)
    assert res_alpha["ok"] is True
    projects_alpha = res_alpha["projects"]
    assert len(projects_alpha) == 2
    slugs_alpha = {p["project_slug"] for p in projects_alpha}
    assert slugs_alpha == {"alpha_project_1", "alpha_project_2"}
    assert "beta_project_1" not in slugs_alpha

    # 2. 验证查询群 Beta 项目列表：仅返回 Beta 项目
    res_beta = await plugin.manage_workspace_projects(action="list", group_id=group_beta)
    assert res_beta["ok"] is True
    projects_beta = res_beta["projects"]
    assert len(projects_beta) == 1
    assert projects_beta[0]["project_slug"] == "beta_project_1"

    # 3. 验证查看历史版本 action="history"
    tm = plugin.time_machine
    chat_ws_alpha = plugin.cli_bridge.get_chat_workspace(group_alpha)
    (chat_ws_alpha / "dist" / "alpha_project_1").mkdir(parents=True, exist_ok=True)
    (chat_ws_alpha / "dist" / "alpha_project_1" / "index.html").write_text("init", encoding="utf-8")
    await tm.capture_snapshot(chat_ws_alpha, "alpha_project_1", job_id="init_job", action="create", summary="初始化")

    res_history = await plugin.manage_workspace_projects(
        action="history",
        project_slug="alpha_project_1",
        group_id=group_alpha,
    )
    assert res_history["ok"] is True
    assert len(res_history["snapshots"]) == 1
    assert res_history["snapshots"][0]["subject"].endswith("初始化")

    await plugin.on_unload()
