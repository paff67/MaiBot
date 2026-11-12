from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from maibot_sdk.components import collect_components

from plugins.embodied_workspace import create_plugin
from plugins.embodied_workspace.archive_manager import WorkspaceArchiveManager
from plugins.embodied_workspace.backend_supervisor import BackendSupervisor
from plugins.embodied_workspace.editorial_kit import EDITORIAL_KIT_JS
from plugins.embodied_workspace.gateway_server import (
    DEFAULT_COOKIE_MAX_AGE_SEC,
    MAIBOT_SDK_JS,
    IdentityVerifier,
    WorkspaceGatewayServer,
)
from plugins.embodied_workspace.subdomain_router import SubdomainRouter
from plugins.embodied_workspace.user_truth_writer import UserScopedTruthWriter
from src.companion_vault.access_policy import replace_group_members
from src.companion_vault.truth_db import CompanionTruthDB
from src.plugin_runtime.host.component_registry import ComponentRegistry

SOURCE = "https://127.0.0.1:8443"
ACCOUNT = "4d86ac1b06dd252fa3c7327eb5614865"


def test_single_level_domain_and_cookie_expiry_guard(tmp_path: Path):
    router = SubdomainRouter(
        db_path=tmp_path / "routes.db",
        base_domain="paff-67.top",
        short_link_base_url="https://xy.pages.dev",
    )

    for _ in range(50):
        token = router.generate_short_token(6)
        assert len(token) == 6
        assert token not in {"www", "api", "mail", "s", "admin"}

    assert router.format_direct_url("k9x4m2") == "https://k9x4m2.paff-67.top"
    assert router.format_url("k9x4m2") == "https://xy.pages.dev/k9x4m2"

    verifier = IdentityVerifier("unit-test-random-secret-key", "paff-67.top")
    now = 1_800_000_000
    valid_cookie = verifier.sign_wxid("wxid_owner", issued_at=now - 3600)
    assert verifier.verify_cookie(valid_cookie, now=now) == "wxid_owner"

    expired_cookie = verifier.sign_wxid("wxid_owner", issued_at=now - DEFAULT_COOKIE_MAX_AGE_SEC - 10)
    assert verifier.verify_cookie(expired_cookie, now=now) is None

    future_cookie = verifier.sign_wxid("wxid_owner", issued_at=now + 3600)
    assert verifier.verify_cookie(future_cookie, now=now) is None


def test_user_scoped_truth_writer_and_group_public_member_filtering(tmp_path: Path):
    truth_db_path = tmp_path / "companion_truth.db"
    truth_db = CompanionTruthDB(str(truth_db_path))

    group_a = "group_a@chatroom"
    group_b = "group_b@chatroom"
    replace_group_members(
        truth_db.conn,
        source=SOURCE,
        account=ACCOUNT,
        group_id=group_a,
        user_ids=["wxid_owner", "wxid_alice"],
    )
    replace_group_members(
        truth_db.conn,
        source=SOURCE,
        account=ACCOUNT,
        group_id=group_b,
        user_ids=["wxid_outsider"],
    )
    truth_db.close()

    writer = UserScopedTruthWriter(
        truth_db_path=truth_db_path,
        wechat_source=SOURCE,
        wechat_account=ACCOUNT,
    )

    with sqlite3.connect(str(truth_db_path)) as conn:
        conn.execute(
            """
            INSERT INTO person_info (
                person_id, platform_ids, person_name, wechat_remark,
                trust_role, one_line_intro, impression, intimacy_score, shared_episodes
            ) VALUES ('P0001', ?, '星河', '私密备注_金主老公', 'OWNER', '项目所有者', '绝密档案', 100, '[]')
            """,
            (json.dumps({"wechat": "wxid_owner"}),),
        )
        conn.execute(
            """
            INSERT INTO person_info (
                person_id, platform_ids, person_name, wechat_remark,
                trust_role, one_line_intro, impression, intimacy_score, shared_episodes
            ) VALUES ('P0002', ?, 'Alice_<img onerror=alert(1)>', '私密备注_学妹', 'GROUP_MEMBER', '技术伙伴', '', 10, '[]')
            """,
            (json.dumps({"wechat": "wxid_alice"}),),
        )
        conn.execute(
            """
            INSERT INTO person_info (
                person_id, platform_ids, person_name, wechat_remark,
                trust_role, one_line_intro, impression, intimacy_score, shared_episodes
            ) VALUES ('P0003', ?, 'Outsider', '私密备注_群外人', 'GROUP_MEMBER', '其他群友', '', 5, '[]')
            """,
            (json.dumps({"wechat": "wxid_outsider"}),),
        )

    members_a = writer.list_group_public_members(group_a)
    wxids_a = {m["wxid"] for m in members_a}
    assert wxids_a == {"wxid_owner", "wxid_alice"}
    assert "wxid_outsider" not in wxids_a
    serialized_a = json.dumps(members_a, ensure_ascii=False)
    assert "私密备注" not in serialized_a
    assert "星河" in serialized_a
    assert writer.list_group_public_members("wxid_owner") == []

    assert "${label}" not in MAIBOT_SDK_JS
    assert "${m.nickname}" not in MAIBOT_SDK_JS
    assert "-->|$1|" not in EDITORIAL_KIT_JS
    assert "${h.textContent}" not in EDITORIAL_KIT_JS

    res_alice = writer.apply_user_scoped_patch(
        actor_wxid="wxid_alice",
        origin_chat_id=group_a,
        patch={
            "current_status": "准备自驾游",
            "preference_item": {"餐饮偏好": "不吃辣"},
            "quest_title": "自驾游订酒店",
            "deadline_or_condition": "本周五前",
        },
    )
    assert len(res_alice) >= 2

    res_owner = writer.apply_user_scoped_patch(
        actor_wxid="wxid_owner",
        origin_chat_id="wxid_owner",
        patch={"current_status": "在机房检修服务器"},
    )
    assert any("主人全局状态" in s for s in res_owner)

    with sqlite3.connect(str(truth_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        writer.apply_user_scoped_patch(
            actor_wxid="wxid_outsider",
            origin_chat_id=group_a,
            patch={"quest_code": "QE0001", "quest_title": "被恶意篡改的待办"},
        )
        q_row = conn.execute("SELECT title FROM quests_events WHERE quest_code = 'QE0001'").fetchone()
        assert str(q_row["title"]) == "自驾游订酒店"


@pytest.mark.asyncio
async def test_private_share_readonly_no_owner_cookie_leak_and_pin_lockout(tmp_path: Path):
    truth_db_path = tmp_path / "truth.db"
    CompanionTruthDB(str(truth_db_path)).close()

    router = SubdomainRouter(db_path=tmp_path / "routes.db", base_domain="paff-67.top")
    router.set_user_pin("wxid_owner", "2468", "OwnerPublic")
    supervisor = BackendSupervisor(workspaces_root=tmp_path / "ws")
    truth_writer = UserScopedTruthWriter(truth_db_path=truth_db_path)
    notify_mock = AsyncMock()

    gateway = WorkspaceGatewayServer(
        router=router,
        supervisor=supervisor,
        truth_writer=truth_writer,
        cookie_secret="test-secret-key-cleanroom-2026",
        on_web_notify=notify_mock,
        listen_port=18798,
    )
    await gateway.start()

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<html><body>Shared Report</body></html>", encoding="utf-8")
    outside_secret = tmp_path / "dist_evil"
    outside_secret.mkdir(parents=True, exist_ok=True)
    (outside_secret / "secret.txt").write_text("do-not-read", encoding="utf-8")

    token_private = "pvttok123"
    router.upsert_route(
        token=token_private,
        chat_id="wxid_owner",
        stream_id="stream_owner",
        creator_wxid="wxid_owner",
        issuer_name="姐姐架构师",
        access_policy="creator_only",
        project_slug="private_notes",
        title="Owner Report",
        mode="static",
        dist_dir=dist_dir,
        project_dir=tmp_path / "proj",
    )

    try:
        async with aiohttp.ClientSession() as friend_session:
            async with friend_session.get(
                "http://127.0.0.1:18798/",
                headers={"Host": f"{token_private}.paff-67.top"},
            ) as resp:
                assert resp.status == 200
                assert "Shared Report" in await resp.text()
                assert "maibot_wxid" not in resp.cookies

            async with friend_session.get(
                "http://127.0.0.1:18798/_maibot/auth/whoami",
                headers={"Host": f"{token_private}.paff-67.top"},
            ) as resp_who:
                assert resp_who.status == 200
                assert "maibot_wxid" not in resp_who.cookies
                who_data = await resp_who.json()
                assert who_data["wxid"] is None
                assert who_data["isCreator"] is False
                assert who_data["members"] == []
                assert who_data["issuerName"] == "姐姐架构师"

            async with friend_session.post(
                "http://127.0.0.1:18798/_maibot/notify",
                headers={"Host": f"{token_private}.paff-67.top"},
                json={"event": "未授权提交", "detail": "test"},
            ) as resp_unauth:
                assert resp_unauth.status == 401
                assert (await resp_unauth.json())["require_auth"] is True

            async with friend_session.get(
                "http://127.0.0.1:18798/../dist_evil/secret.txt",
                headers={"Host": f"{token_private}.paff-67.top"},
            ) as resp_trav:
                assert resp_trav.status in (400, 403, 404)
                assert "do-not-read" not in await resp_trav.text()

            for i in range(5):
                async with friend_session.post(
                    "http://127.0.0.1:18798/_maibot/auth/pin_login",
                    headers={"Host": f"{token_private}.paff-67.top"},
                    json={"pin": "0000"},
                ) as resp_fail:
                    assert resp_fail.status == (429 if i == 4 else 403)

            async with friend_session.post(
                "http://127.0.0.1:18798/_maibot/auth/pin_login",
                headers={"Host": f"{token_private}.paff-67.top"},
                json={"pin": "2468"},
            ) as resp_locked:
                assert resp_locked.status == 429

        router._pin_failures.clear()
        async with aiohttp.ClientSession() as owner_session:
            async with owner_session.post(
                "http://127.0.0.1:18798/_maibot/auth/pin_login",
                headers={"Host": f"{token_private}.paff-67.top"},
                json={"pin": "2468"},
            ) as resp_ok:
                assert resp_ok.status == 200
                cookie_val = resp_ok.cookies["maibot_wxid"].value

            async with aiohttp.ClientSession(cookies={"maibot_wxid": cookie_val}) as authed_owner:
                async with authed_owner.post(
                    "http://127.0.0.1:18798/_maibot/notify",
                    headers={"Host": f"{token_private}.paff-67.top"},
                    json={"event": "主人确认", "detail": "已阅"},
                ) as resp_notify:
                    assert resp_notify.status == 200
                    notify_mock.assert_awaited_once()
    finally:
        await gateway.stop()
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_native_named_group_commands_and_claim_scope_isolation(tmp_path: Path):
    truth_db_path = tmp_path / "truth.db"
    truth_db = CompanionTruthDB(str(truth_db_path))
    replace_group_members(
        truth_db.conn,
        source=SOURCE,
        account=ACCOUNT,
        group_id="group_target@chatroom",
        user_ids=["wxid_david"],
    )
    truth_db.close()

    plugin = create_plugin()
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "1.5.0"},
            "gateway_port": 0,
            "workspaces_root": str(tmp_path / "workspaces"),
            "router_db_path": str(tmp_path / "routes.db"),
            "truth_db_path": str(truth_db_path),
            "ledger_db_path": str(tmp_path / "ledger.db"),
            "wechat_source": SOURCE,
            "wechat_account": ACCOUNT,
        }
    )
    plugin._set_context(MagicMock())
    await plugin.on_load()

    try:
        registry = ComponentRegistry()
        for comp in collect_components(plugin):
            if comp["type"] == "COMMAND":
                registry.register_component(
                    name=comp["name"],
                    component_type="command",
                    plugin_id="embodied_workspace",
                    metadata=comp["metadata"],
                )

        claim_code = plugin.router.create_claim(
            chat_id="group_target@chatroom",
            browser_nonce="nonce123",
            pin_to_set="6688",
        )
        match_claim = registry.find_command_by_text(f"认领 {claim_code}")
        assert match_claim is not None
        assert match_claim[0].name == "claim"
        assert match_claim[1] == {"code": claim_code}

        match_pin = registry.find_command_by_text("/pin 9999")
        assert match_pin is not None and match_pin[1] == {"pin": "9999"}

        match_rb = registry.find_command_by_text("/rollback camp-vote 2")
        assert match_rb is not None and match_rb[1] == {"slug": "camp-vote", "version": "2"}

        match_revoke = registry.find_command_by_text("撤销 k9x4m2")
        assert match_revoke is not None and match_revoke[1] == {"token": "k9x4m2"}

        cross_group_reply = await plugin.claim_web_identity_cmd(
            text=f"认领 {claim_code}",
            matched_groups=match_claim[1],
            group_id="other_group@chatroom",
            user_id="wxid_david",
        )
        assert "绑定成功" not in cross_group_reply

        outsider_dm_reply = await plugin.claim_web_identity_cmd(
            text=f"认领 {claim_code}",
            matched_groups=match_claim[1],
            user_id="wxid_stranger",
        )
        assert "绑定成功" not in outsider_dm_reply

        ok_reply = await plugin.claim_web_identity_cmd(
            text=f"认领 {claim_code}",
            matched_groups=match_claim[1],
            group_id="group_target@chatroom",
            user_id="wxid_david",
            message={"message_info": {"user_info": {"user_id": "wxid_david", "user_nickname": "David"}}},
        )
        assert "绑定成功" in ok_reply
        assert plugin.router.poll_claim(claim_code, "nonce123") == "wxid_david"
        assert plugin.router.verify_user_pin("wxid_david", "6688") is True
        assert plugin.router.get_pin_user_nickname("wxid_david") == "David"

        pin_reply = await plugin.set_web_pin_cmd(
            text="/pin 9999",
            matched_groups=match_pin[1],
            user_id="wxid_david",
            message={"message_info": {"user_info": {"user_id": "wxid_david", "user_nickname": "David"}}},
        )
        assert "PIN 码已生效" in pin_reply
        assert plugin.router.verify_user_pin("wxid_david", "9999") is True

        ticket = plugin.router.create_handoff_ticket("wxid_david", ttl_sec=90.0)
        assert plugin.router.consume_handoff_ticket(ticket) == "wxid_david"
        assert plugin.router.consume_handoff_ticket(ticket) is None
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_archive_manager_preserves_subdirectory_tree_and_blocks_traversal(tmp_path: Path):
    gdrive_root = tmp_path / "gdrive_companion_vault" / "workspace_archives"
    archive_mgr = WorkspaceArchiveManager(gdrive_archive_root=gdrive_root)

    dist_dir = tmp_path / "dist"
    (dist_dir / "files" / "charts").mkdir(parents=True, exist_ok=True)
    (dist_dir / "files" / "tables").mkdir(parents=True, exist_ok=True)
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    (dist_dir / "files" / "charts" / "summary.png").write_bytes(b"CHART_PNG_DATA")
    (dist_dir / "files" / "tables" / "summary.png").write_bytes(b"TABLE_PNG_DATA")

    archived = await archive_mgr.archive_job_artifacts(
        chat_safe_id="wxid_sample",
        project_slug="subdir_export",
        title="分层归档测试",
        task_prompt="生成多目录图表",
        short_url="https://xy.pages.dev/k9x4m2",
        dist_dir=dist_dir,
        declared_files=["../outside.txt"],
    )

    assert set(archived) == {"charts/summary.png", "tables/summary.png"}

    target_dir = next(gdrive_root.glob("wxid_sample/*_subdir_export"))
    assert (target_dir / "charts" / "summary.png").read_bytes() == b"CHART_PNG_DATA"
    assert (target_dir / "tables" / "summary.png").read_bytes() == b"TABLE_PNG_DATA"
    assert not (target_dir / "outside.txt").exists()


