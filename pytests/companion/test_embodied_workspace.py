from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import pytest
from maibot_sdk.components import collect_components

from plugins.embodied_workspace.backend_supervisor import BackendSupervisor
from plugins.embodied_workspace.cli_bridge import (
    CLIBridge,
    FAIRY_AGENTS_TEMPLATE_PATH,
    SANDBOX_HOME_PATH,
    SANDBOX_WORKSPACE_PATH,
    WorkspaceJobRequest,
    build_bwrap_prefix,
    build_sandbox_env,
)
from plugins.embodied_workspace.gateway_server import WorkspaceGatewayServer
from plugins.embodied_workspace.plugin import (
    EmbodiedWorkspaceConfig,
    EmbodiedWorkspacePlugin,
    create_plugin,
)
from plugins.embodied_workspace.subdomain_router import SubdomainRouter
from src.companion_vault.access_policy import replace_group_members
from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.message_media_worker import MessageMediaQueue
from src.companion_vault.truth_db import CompanionTruthDB
from src.platform_io.wechat.message_ingestor import MessageLedger
from src.plugin_runtime.runner.manifest_validator import PluginManifest

ACCOUNT = "4d86ac1b06dd252fa3c7327eb5614865"
SOURCE = "https://127.0.0.1:8443"
CHAT_PRIVATE = "wxid_tester"
CHAT_GROUP = "12345@chatroom"


def _make_ledger_event(seq: int, chat: str, kind: str = "1", body: str = "test", mid: str | None = None) -> dict:
    msg_id = mid or str(seq)
    return dict(
        seq=seq,
        account=ACCOUNT,
        msg_id=msg_id,
        chat_id=chat,
        revision=f"r{seq}",
        observed_at_ms=seq,
        row=dict(
            msgId=msg_id,
            talker=chat,
            type=kind,
            content=(f"{CHAT_PRIVATE}:\n{body}" if chat.endswith("@chatroom") else body),
            msgSvrId=str(1000 + seq),
            isSend="0",
            createTime="1793880000000",
        ),
    )


def test_manifest_cleanroom_contract_and_no_dead_fast_renderer():
    manifest_path = Path("plugins/embodied_workspace/_manifest.json")
    assert manifest_path.exists()
    assert not Path("plugins/embodied_workspace/fast_renderer.py").exists()
    assert FAIRY_AGENTS_TEMPLATE_PATH.is_file()

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = PluginManifest.model_validate(data)
    assert manifest.id == "maibot-team.embodied-workspace"
    assert manifest.manifest_version == 2
    assert "maisaka.reply.generate" not in manifest.capabilities
    assert set(manifest.capabilities) == {"maisaka.context.append", "maisaka.proactive.trigger"}

    plugin = create_plugin()
    assert isinstance(plugin, EmbodiedWorkspacePlugin)
    assert plugin.config_model is EmbodiedWorkspaceConfig
    assert plugin.config.cookie_secret == ""
    assert not hasattr(plugin, "publish_document_page")

    tools = [c["name"] for c in collect_components(plugin) if c["type"] == "TOOL"]
    assert set(tools) == {"run_agent_workspace", "manage_workspace_projects"}

    cli_src = Path("plugins/embodied_workspace/cli_bridge.py").read_text(encoding="utf-8")
    assert "sk-" not in cli_src


def test_cleanroom_sandbox_env_whitelist_and_bwrap_clearenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAIBOT_EDGE_TOKEN", "top-secret-edge-token")
    monkeypatch.setenv("RANDOM_HOST_SECRET", "do-not-leak")
    monkeypatch.delenv("FAIRY_SANDBOX_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("FAIRY_SANDBOX_BASE_URL", raising=False)
    monkeypatch.delenv("GOOGLE_GEMINI_BASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="FAIRY_SANDBOX_API_KEY"):
        build_sandbox_env(require_api_key=True)

    monkeypatch.setenv("FAIRY_SANDBOX_API_KEY", "sandbox-scoped-key")
    monkeypatch.setenv("FAIRY_SANDBOX_BASE_URL", "https://gateway.example.test")

    env = build_sandbox_env(require_api_key=True)
    assert env["HOME"] == SANDBOX_HOME_PATH
    assert env["TZ"] == "Asia/Tokyo"
    assert env["LANG"] == "C.UTF-8"
    assert env["GEMINI_API_KEY"] == "sandbox-scoped-key"
    assert env["GOOGLE_GEMINI_BASE_URL"] == "https://gateway.example.test"
    assert "MAIBOT_EDGE_TOKEN" not in env
    assert "RANDOM_HOST_SECRET" not in env

    be_env = build_sandbox_env(port=19055, require_api_key=False)
    assert be_env["PORT"] == "19055"
    assert "GEMINI_API_KEY" not in be_env
    assert "MAIBOT_EDGE_TOKEN" not in be_env

    custom_auth = tmp_path / "custom_gemini"
    (custom_auth / "config").mkdir(parents=True)
    (custom_auth / "antigravity-cli").mkdir(parents=True)
    (custom_auth / "antigravity-cli" / "settings.json").write_text("{}", encoding="utf-8")

    chat_ws = tmp_path / "chat_ws"
    chat_ws.mkdir(parents=True)
    bwrap_cmd = build_bwrap_prefix(
        chat_ws,
        workdir=SANDBOX_WORKSPACE_PATH,
        cli_auth_dir=str(custom_auth),
        sandbox_env=env,
    )
    assert "--clearenv" in bwrap_cmd
    tz_idx = bwrap_cmd.index("TZ")
    assert bwrap_cmd[tz_idx - 1] == "--setenv"
    assert bwrap_cmd[tz_idx + 1] == "Asia/Tokyo"
    assert str(custom_auth / "config") in bwrap_cmd
    assert "/root/.gemini/config" not in bwrap_cmd

    if shutil.which("bwrap"):
        exec_cmd = [*bwrap_cmd, "sh", "-c", f"touch {SANDBOX_WORKSPACE_PATH}/hello.txt && echo $TZ:${{MAIBOT_EDGE_TOKEN:-empty}}"]
        proc = subprocess.run(exec_cmd, capture_output=True, text=True)
        assert proc.returncode == 0
        assert (chat_ws / "hello.txt").exists()
        assert proc.stdout.strip() == "Asia/Tokyo:empty"


@pytest.mark.asyncio
async def test_rbac_per_job_hardlink_staging_and_auto_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    truth_db_path = tmp_path / "truth.db"
    ledger_db_path = tmp_path / "ledger.db"
    ws_root = tmp_path / "workspaces"

    truth_db = CompanionTruthDB(str(truth_db_path))
    ledger = MessageLedger(str(ledger_db_path))

    replace_group_members(truth_db.conn, source=SOURCE, account=ACCOUNT, group_id=CHAT_GROUP, user_ids=[CHAT_PRIVATE])
    ledger.ingest(SOURCE, "epoch", _make_ledger_event(1, CHAT_PRIVATE, kind="49", mid="101"))

    assets = AssetPipeline(truth_db, spool=str(tmp_path / "assets"))
    MessageMediaQueue(assets, ledger.db, SOURCE, None)

    sample_file = tmp_path / "secret.txt"
    sample_file.write_text("classified data", encoding="utf-8")
    asset = assets.ingest(
        sample_file.read_bytes(),
        {"mime_type": "text/plain", "original_name": "secret.txt"},
        source_id="src_101",
        chat_id=f"{ACCOUNT}/{CHAT_PRIVATE}",
    )
    file_code = asset["file_code"]

    with truth_db.conn:
        truth_db.conn.execute("UPDATE asset_sources SET association='verified' WHERE file_code=?", (file_code,))
        truth_db.conn.execute(
            "INSERT INTO message_media_jobs (job_id, source, account, msg_id, revision, chat_id, ref_index, reference, state, updated_at) "
            "VALUES ('j101', ?, ?, '101', 'r1', ?, 0, '{}', 'ready', 0)",
            (SOURCE, ACCOUNT, CHAT_PRIVATE),
        )
        truth_db.conn.execute(
            "INSERT INTO message_asset_links (job_id, file_code, representation, account, msg_id, revision, chat_id, active, evidence) "
            "VALUES ('j101', ?, 'original', ?, '101', 'r1', ?, 1, '{}')",
            (file_code, ACCOUNT, CHAT_PRIVATE),
        )
        src_row = truth_db.conn.execute("SELECT local_path FROM asset_sources WHERE file_code=?", (file_code,)).fetchone()
        orig_local_path = Path(str(src_row[0]))

    bridge = CLIBridge(
        workspaces_root=ws_root,
        truth_db_path=truth_db_path,
        ledger_db_path=ledger_db_path,
        wechat_source=SOURCE,
        wechat_account=ACCOUNT,
    )

    staged = bridge.stage_authorized_files(destination_chat_id=CHAT_PRIVATE, file_codes=[file_code], job_id="job_alpha")
    assert len(staged) == 1
    assert staged[0].parent.name == "job_alpha"
    assert staged[0].read_text(encoding="utf-8") == "classified data"
    if orig_local_path.drive == staged[0].drive:
        assert os.stat(orig_local_path).st_ino == os.stat(staged[0]).st_ino

    with pytest.raises(PermissionError):
        bridge.stage_authorized_files(destination_chat_id=CHAT_GROUP, file_codes=[file_code], job_id="job_beta")

    monkeypatch.delenv("FAIRY_SANDBOX_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    res = await bridge.run_cli_job(
        WorkspaceJobRequest(
            job_id="job_alpha",
            stream_id="stream_1",
            chat_id=CHAT_PRIVATE,
            project_slug="demo",
            staged_files=staged,
        )
    )
    assert res.ok is False
    assert "FAIRY_SANDBOX_API_KEY" in res.error_message
    assert not staged[0].parent.exists()

    truth_db.close()
    ledger.db.close()


@pytest.mark.asyncio
async def test_backend_auto_wake_scale_to_zero_and_shared_session(tmp_path: Path):
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir(parents=True, exist_ok=True)
    router_db = tmp_path / "routes.db"

    router = SubdomainRouter(db_path=router_db, base_domain="s.testdomain.com")
    supervisor = BackendSupervisor(workspaces_root=ws_root, port_min=19800, port_max=19850)
    supervisor.start_janitor()

    notify_mock = AsyncMock()
    gateway = WorkspaceGatewayServer(
        router=router,
        supervisor=supervisor,
        workspaces_root=ws_root,
        on_web_notify=notify_mock,
        listen_port=18797,
    )
    await gateway.start()

    secret_file = ws_root / ".cookie_secret"
    assert secret_file.is_file()
    assert len(secret_file.read_text(encoding="utf-8").strip()) == 64
    shared_session = gateway._client_session
    assert shared_session is not None and not shared_session.closed

    token = "testtoken123"
    proj_dir = ws_root / "test_chat" / "projects" / "web_app"
    dist_dir = ws_root / "test_chat" / "dist" / "web_app"
    proj_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "index.html").write_text("<h1>Backend Live</h1>", encoding="utf-8")
    (proj_dir / "data.bin").write_bytes(b"binary-stream-payload-12345")

    router.upsert_route(
        token=token,
        chat_id="test_chat",
        stream_id="stream_01",
        issuer_name="小妍测试署名",
        project_slug="web_app",
        title="Web App",
        mode="backend",
        dist_dir=dist_dir,
        project_dir=proj_dir,
        start_cmd=["python3", "-m", "http.server", "{PORT}"],
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://127.0.0.1:18797/",
                headers={"Host": f"{token}.s.testdomain.com"},
            ) as resp:
                assert resp.status == 200
                text = await resp.text()
                assert "Backend Live" in text
                assert "/_maibot/sdk.js" in text

            async with session.get(
                "http://127.0.0.1:18797/data.bin",
                headers={"Host": f"{token}.s.testdomain.com"},
            ) as resp_bin:
                assert resp_bin.status == 200
                assert await resp_bin.read() == b"binary-stream-payload-12345"
                assert gateway._client_session is shared_session

        await supervisor.stop_backend(token)
        assert token not in supervisor._active

        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://127.0.0.1:18797/",
                headers={"Host": f"{token}.s.testdomain.com"},
            ) as resp:
                assert resp.status == 200
                assert "Backend Live" in await resp.text()

            router.revoke_token(token, chat_id="test_chat")
            async with session.get(
                "http://127.0.0.1:18797/",
                headers={"Host": f"{token}.s.testdomain.com"},
            ) as resp_exp:
                assert resp_exp.status == 410
                exp_html = await resp_exp.text()
                assert "小妍测试署名" in exp_html
                assert "许妍" not in exp_html
    finally:
        await gateway.stop()
        assert shared_session.closed
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_protect_subdomain_url_hook_native_runner_protocol():
    plugin = create_plugin()
    plugin.config.base_domain = "s.yourdomain.com"
    plugin.config.short_link_base_url = "https://xy.pages.dev"

    res1 = await plugin.protect_subdomain_url_hook(
        response="攻略已生成，点击查看：https://abc123.s.yourdomain.com",
        session_id="sess_1",
        reply_message_id="msg_1",
        reply_tool_args={"reply_reference": "交付"},
        skip_post_process=False,
        enable_splitter=True,
        enable_chinese_typo=True,
    )
    assert res1["action"] == "continue"
    mod = res1["modified_kwargs"]
    assert mod["skip_post_process"] is False
    assert mod["enable_splitter"] is False
    assert mod["enable_chinese_typo"] is False
    assert mod["response"] == "攻略已生成，点击查看：https://abc123.s.yourdomain.com"

    res2 = await plugin.protect_subdomain_url_hook(
        response="今天天气真好，去公园散散步吧！",
        session_id="sess_1",
        reply_message_id="msg_2",
        skip_post_process=False,
        enable_splitter=True,
        enable_chinese_typo=True,
    )
    assert res2 == {"action": "continue"}


