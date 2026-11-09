from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from plugins.embodied_workspace import create_plugin
from plugins.embodied_workspace.archive_manager import WorkspaceArchiveManager
from plugins.embodied_workspace.backend_supervisor import BackendSupervisor
from plugins.embodied_workspace.gateway_server import IdentityVerifier, WorkspaceGatewayServer
from plugins.embodied_workspace.subdomain_router import SubdomainRouter
from plugins.embodied_workspace.user_truth_writer import UserScopedTruthWriter
from src.companion_vault.truth_db import CompanionTruthDB
from src.plugin_runtime.host.component_registry import CommandEntry, ComponentRegistry


# ---------------------------------------------------------------------------
# 断言 1: 单级泛域名与保留字规避测试
# ---------------------------------------------------------------------------
def test_single_level_domain_and_reserved_filtering(tmp_path: Path):
    router = SubdomainRouter(
        db_path=tmp_path / "routes.db",
        base_domain="paff-67.top",
        short_link_base_url="https://xy.pages.dev",
    )

    # 1. 验证短码生成不包含保留字
    for _ in range(50):
        token = router.generate_short_token(6)
        assert len(token) == 6
        assert token.islower() or token.isdigit()
        assert token not in {"www", "api", "mail", "s", "admin"}

    # 2. 验证格式化 URL 与直链
    assert router.format_direct_url("k9x4m2") == "https://k9x4m2.paff-67.top"
    assert router.format_url("k9x4m2") == "https://xy.pages.dev/k9x4m2"

    # 3. 验证无短链前缀时回退到 direct url
    router_no_short = SubdomainRouter(
        db_path=tmp_path / "routes2.db",
        base_domain="paff-67.top",
    )
    assert router_no_short.format_url("k9x4m2") == "https://k9x4m2.paff-67.top"


# ---------------------------------------------------------------------------
# 断言 2: 真实 8 表 Schema 用户级隔离测试
# ---------------------------------------------------------------------------
def test_user_scoped_truth_writer_isolation(tmp_path: Path):
    truth_db_path = tmp_path / "companion_truth.db"
    truth_db = CompanionTruthDB(str(truth_db_path))
    truth_db.close()

    writer = UserScopedTruthWriter(
        truth_db_path=truth_db_path,
        wechat_source="https://100.90.156.92:8443",
        wechat_account="4d86ac1b06dd252fa3c7327eb5614865",
    )

    # 预置一个 OWNER 用户和一个普通成员
    with sqlite3.connect(str(truth_db_path)) as conn:
        conn.execute(
            """
            INSERT INTO person_info (
                person_id, platform_ids, person_name, wechat_remark,
                trust_role, one_line_intro, impression, intimacy_score, shared_episodes
            ) VALUES ('P0001', ?, '主人', '小明', 'OWNER', '项目所有者', '勤奋工程师', 100, '[]')
            """,
            (json.dumps({"wechat": "wxid_owner"}),),
        )
        conn.execute(
            """
            INSERT INTO person_info (
                person_id, platform_ids, person_name, wechat_remark,
                trust_role, one_line_intro, impression, intimacy_score, shared_episodes
            ) VALUES ('P0002', ?, '爱丽丝', 'Alice', 'GROUP_MEMBER', '技术伙伴', '', 10, '[]')
            """,
            (json.dumps({"wechat": "wxid_alice"}),),
        )

    # 1. wxid_alice 提交偏好与待办
    res_alice = writer.apply_user_scoped_patch(
        actor_wxid="wxid_alice",
        origin_chat_id="test_group@chatroom",
        patch={
            "current_status": "准备自驾游",
            "preference_item": {"餐饮偏好": "不吃辣"},
            "quest_title": "自驾游订酒店",
            "deadline_or_condition": "本周五前",
        },
    )
    assert len(res_alice) >= 2

    # 2. wxid_owner 提交状态更新
    res_owner = writer.apply_user_scoped_patch(
        actor_wxid="wxid_owner",
        origin_chat_id="wxid_owner",
        patch={
            "current_status": "在机房检修服务器",
            "availability": "busy",
        },
    )
    assert any("主人全局状态" in s for s in res_owner)

    # 3. 校验数据库隔离状态
    with sqlite3.connect(str(truth_db_path)) as conn:
        conn.row_factory = sqlite3.Row

        # Alice 的 impression 被更新
        p_alice = conn.execute("SELECT impression FROM person_info WHERE person_id = 'P0002'").fetchone()
        assert "近期动态：准备自驾游" in str(p_alice["impression"])
        assert "餐饮偏好=不吃辣" in str(p_alice["impression"])

        # 校验 OWNER 独占的 user_status 表被更新且仅包含 OWNER
        u_status = conn.execute("SELECT * FROM user_status WHERE key = 'current_status'").fetchone()
        assert u_status is not None
        assert "在机房检修服务器" in str(u_status["current_value"])

        # 待办事项已生成并嵌入 [owner:wxid_alice]
        q_row = conn.execute("SELECT * FROM quests_events WHERE quest_code = 'QE0001'").fetchone()
        assert q_row is not None
        assert "[owner:wxid_alice]" in str(q_row["current_progress"])
        assert "自驾游订酒店" in str(q_row["title"])

        # 4. 越权校验：wxid_bob 尝试修改 Alice 的待办 QE0001，应被拦截拒绝修改
        writer.apply_user_scoped_patch(
            actor_wxid="wxid_bob",
            origin_chat_id="test_group@chatroom",
            patch={
                "quest_code": "QE0001",
                "quest_title": "被恶意篡改的待办",
            },
        )
        q_row_after = conn.execute("SELECT title FROM quests_events WHERE quest_code = 'QE0001'").fetchone()
        assert str(q_row_after["title"]) == "自驾游订酒店"


# ---------------------------------------------------------------------------
# 断言 3: 私聊抗微信 Pre-fetch 免登与三级 RBAC 测试
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_private_chat_prefetch_immunity_and_rbac(tmp_path: Path):
    truth_db_path = tmp_path / "truth.db"
    CompanionTruthDB(str(truth_db_path)).close()

    router = SubdomainRouter(db_path=tmp_path / "routes.db", base_domain="paff-67.top")
    supervisor = BackendSupervisor(workspaces_root=tmp_path / "ws")
    truth_writer = UserScopedTruthWriter(truth_db_path=truth_db_path)
    notify_mock = AsyncMock()

    gateway = WorkspaceGatewayServer(
        router=router,
        supervisor=supervisor,
        truth_writer=truth_writer,
        cookie_secret="test-secret-key-12345",
        on_web_notify=notify_mock,
        listen_port=18798,
    )
    await gateway.start()

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<html><body>Test Page</body></html>", encoding="utf-8")

    token_private = "pvttok123"
    router.upsert_route(
        token=token_private,
        chat_id="wxid_charlie",
        stream_id="stream_charlie",
        creator_wxid="wxid_charlie",
        access_policy="creator_only",
        project_slug="private_notes",
        title="Charlie Notes",
        mode="static",
        dist_dir=dist_dir,
        project_dir=tmp_path / "proj",
    )

    try:
        # 1. 模拟微信后台预取（连续 3 次 GET /，不保存 Cookie）
        async with aiohttp.ClientSession() as session:
            for _ in range(3):
                async with session.get(
                    "http://127.0.0.1:18798/",
                    headers={"Host": f"{token_private}.paff-67.top", "Accept": "text/html"},
                ) as resp:
                    assert resp.status == 200

        # 2. 真实用户 WebView 执行 sdk.js 请求 whoami：依然免登成功并下发 Cookie
        verifier = IdentityVerifier("test-secret-key-12345", "paff-67.top")
        async with aiohttp.ClientSession() as user_session:
            async with user_session.get(
                "http://127.0.0.1:18798/_maibot/auth/whoami",
                headers={"Host": f"{token_private}.paff-67.top"},
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["wxid"] == "wxid_charlie"
                assert data["isCreator"] is True
                assert "maibot_wxid" in resp.cookies
                assert verifier.verify_cookie(resp.cookies["maibot_wxid"].value) == "wxid_charlie"

        # 3. RBAC 权限测试：creator_only 阻止非创建者提交 notify
        stranger_cookie = verifier.sign_wxid("wxid_stranger")

        async with aiohttp.ClientSession(cookies={"maibot_wxid": stranger_cookie}) as stranger_session:
            async with stranger_session.post(
                "http://127.0.0.1:18798/_maibot/notify",
                headers={"Host": f"{token_private}.paff-67.top"},
                json={"event": "尝试修改", "detail": "恶意请求"},
            ) as resp:
                assert resp.status == 403
                res = await resp.json()
                assert "创建者独占模式" in res["error"]
    finally:
        await gateway.stop()
        await supervisor.stop_all()


# ---------------------------------------------------------------------------
# 断言 4: @Command(**kwargs) 契约与跨设备认证测试
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_command_kwargs_and_cross_device_auth(tmp_path: Path):
    plugin = create_plugin()
    plugin.config.gateway_port = 0
    plugin.config.workspaces_root = str(tmp_path / "workspaces")
    plugin.config.router_db_path = str(tmp_path / "routes.db")
    plugin.config.truth_db_path = str(tmp_path / "truth.db")
    plugin.config.ledger_db_path = str(tmp_path / "ledger.db")

    mock_ctx = MagicMock()
    plugin._set_context(mock_ctx)
    await plugin.on_load()

    try:
        # 1. 验证 @Command 使用 ComponentRegistry 进行正则匹配
        from maibot_sdk.components import collect_components

        registry = ComponentRegistry()
        for comp in collect_components(plugin):
            if comp["type"] == "COMMAND":
                registry.register_component(
                    name=comp["name"],
                    component_type="command",
                    plugin_id="embodied_workspace",
                    metadata=comp["metadata"],
                )

        # 验证文本命中
        match_claim = registry.find_command_by_text("认领 8392")
        assert match_claim is not None
        assert match_claim[0].name == "claim"

        match_pin = registry.find_command_by_text("/pin 6688")
        assert match_pin is not None
        assert match_pin[0].name == "pin"

        match_revoke = registry.find_command_by_text("撤销 k9x4m2")
        assert match_revoke is not None
        assert match_revoke[0].name == "revoke"
        assert match_pin[0].name == "pin"

        match_revoke = registry.find_command_by_text("撤销 k9x4m2")
        assert match_revoke is not None
        assert match_revoke[0].name == "revoke"

        # 2. 模拟微信端调用 claim 命令（验证 **kwargs 解构安全）
        claim_code = plugin.router.create_claim(chat_id="test_group@chatroom", browser_nonce="nonce123", pin_to_set="6688")
        reply_msg = await plugin.claim_web_identity_cmd(
            text=f"认领 {claim_code}",
            matched_groups=(claim_code,),
            user_id="wxid_david",
            user_nickname="David",
        )
        assert "绑定成功" in reply_msg

        # 浏览器轮询提取绑定成功的 wxid
        verified_wxid = plugin.router.poll_claim(claim_code, "nonce123")
        assert verified_wxid == "wxid_david"

        # 3. 验证顺便设置的 4 位 PIN 码生效
        assert plugin.router.verify_user_pin("wxid_david", "6688") is True

        # 4. 验证 /pin 指令调用
        pin_reply = await plugin.set_web_pin_cmd(
            text="/pin 9999",
            matched_groups=("9999",),
            user_id="wxid_david",
            user_nickname="David",
        )
        assert "PIN 码已生效" in pin_reply
        assert plugin.router.verify_user_pin("wxid_david", "9999") is True

        # 5. 验证跨浏览器 90 秒单次票据 _ho
        ticket = plugin.router.create_handoff_ticket("wxid_david", ttl_sec=90.0)
        assert plugin.router.consume_handoff_ticket(ticket) == "wxid_david"
        # 重放票据失效
        assert plugin.router.consume_handoff_ticket(ticket) is None
    finally:
        await plugin.on_unload()


# ---------------------------------------------------------------------------
# 断言 5: Google Drive FUSE 目录直写归档测试
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_archive_manager_fuse_direct_write(tmp_path: Path):
    gdrive_root = tmp_path / "gdrive_companion_vault" / "workspace_archives"
    archive_mgr = WorkspaceArchiveManager(gdrive_archive_root=gdrive_root)

    dist_dir = tmp_path / "dist"
    files_dir = dist_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    test_file = files_dir / "analysis_report.xlsx"
    test_file.write_bytes(b"Mock Excel Binary Data 2026")

    archived = await archive_mgr.archive_job_artifacts(
        chat_safe_id="wxid_sample",
        project_slug="excel_export",
        title="数据分析导出",
        task_prompt="生成 Excel 报表",
        short_url="https://xy.pages.dev/k9x4m2",
        dist_dir=dist_dir,
    )

    assert "analysis_report.xlsx" in archived

    # 验证独立目录树结构与 manifest.json
    target_folders = list(gdrive_root.glob("wxid_sample/*_excel_export"))
    assert len(target_folders) == 1
    target_dir = target_folders[0]

    dest_file = target_dir / "analysis_report.xlsx"
    manifest_file = target_dir / "manifest.json"
    assert dest_file.is_file()
    assert manifest_file.is_file()

    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest_data["title"] == "数据分析导出"
    assert manifest_data["short_url"] == "https://xy.pages.dev/k9x4m2"
    assert len(manifest_data["files"]) == 1
    assert manifest_data["files"][0]["file_name"] == "analysis_report.xlsx"
    assert "sha256" in manifest_data["files"][0]
