from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from src.companion_vault.access_policy import replace_group_members
from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.message_media_worker import MessageMediaQueue
from src.companion_vault.truth_db import CompanionTruthDB
from src.platform_io.wechat.message_ingestor import MessageLedger
from src.plugin_runtime.runner.manifest_validator import PluginManifest

from plugins.embodied_workspace.backend_supervisor import BackendSupervisor
from plugins.embodied_workspace.cli_bridge import (
    CLIBridge,
    SANDBOX_HOME_PATH,
    SANDBOX_WORKSPACE_PATH,
    build_bwrap_prefix,
)
from plugins.embodied_workspace.gateway_server import WorkspaceGatewayServer
from plugins.embodied_workspace.plugin import (
    EmbodiedWorkspaceConfig,
    EmbodiedWorkspacePlugin,
    create_plugin,
)
from plugins.embodied_workspace.subdomain_router import SubdomainRouter

ACCOUNT = "4d86ac1b06dd252fa3c7327eb5614865"
SOURCE = "https://100.90.156.92:8443"
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


# ---------------------------------------------------------------------------
# 断言 1: Manifest 与插件加载器校验断言
# ---------------------------------------------------------------------------
def test_manifest_and_plugin_factory():
    manifest_path = Path("plugins/embodied_workspace/_manifest.json")
    assert manifest_path.exists(), "缺少 _manifest.json 文件"

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = PluginManifest.model_validate(data)
    assert manifest.id == "maibot-team.embodied-workspace"
    assert manifest.manifest_version == 2
    assert "maisaka.reply.generate" in manifest.capabilities
    assert "maisaka.context.append" in manifest.capabilities
    assert "maisaka.proactive.trigger" in manifest.capabilities

    plugin = create_plugin()
    assert isinstance(plugin, EmbodiedWorkspacePlugin)
    assert plugin.config_model is EmbodiedWorkspaceConfig
    assert plugin.config.base_domain == "s.yourdomain.com"


# ---------------------------------------------------------------------------
# 断言 2: bwrap /tmp/workspace 与 /tmp/home 可写及隔离断言
# ---------------------------------------------------------------------------
def test_bwrap_workspace_and_home_isolation(tmp_path: Path):
    if not shutil.which("bwrap"):
        pytest.skip("系统未安装 bwrap")

    chat_ws = tmp_path / "chat_ws"
    chat_ws.mkdir(parents=True, exist_ok=True)

    cmd = build_bwrap_prefix(chat_ws, workdir=SANDBOX_WORKSPACE_PATH)
    test_script = (
        f"touch {SANDBOX_WORKSPACE_PATH}/hello.txt && "
        f"touch {SANDBOX_HOME_PATH}/test_cache.txt && "
        "ls -la /opt/maibot-brain"
    )
    cmd.extend(["sh", "-c", test_script])

    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bwrap 执行失败: {proc.stderr}"
    assert (chat_ws / "hello.txt").exists(), "工作区宿主目录未观测到写入的文件"


# ---------------------------------------------------------------------------
# 断言 3: 双库 RBAC 附件校验与 asset_sources 连表断言
# ---------------------------------------------------------------------------
def test_rbac_stage_authorized_files(tmp_path: Path):
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

    bridge = CLIBridge(
        workspaces_root=ws_root,
        truth_db_path=truth_db_path,
        ledger_db_path=ledger_db_path,
        wechat_source=SOURCE,
        wechat_account=ACCOUNT,
    )

    # 私聊自身有权读取
    staged = bridge.stage_authorized_files(destination_chat_id=CHAT_PRIVATE, file_codes=[file_code])
    assert len(staged) == 1
    assert staged[0].exists()
    assert staged[0].read_text(encoding="utf-8") == "classified data"

    # 群聊无权读取该私聊附件，断言拦截
    with pytest.raises(PermissionError):
        bridge.stage_authorized_files(destination_chat_id=CHAT_GROUP, file_codes=[file_code])

    truth_db.close()
    ledger.db.close()


# ---------------------------------------------------------------------------
# 断言 4: 快轨 IPC 契约与自动升级断言
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fast_track_ipc_and_auto_upgrade(tmp_path: Path):
    plugin = create_plugin()
    plugin.config.workspaces_root = str(tmp_path / "workspaces")
    plugin.config.router_db_path = str(tmp_path / "routes.db")
    plugin.config.truth_db_path = str(tmp_path / "truth.db")
    plugin.config.ledger_db_path = str(tmp_path / "ledger.db")
    plugin.config.gateway_port = 18796

    mock_ctx = MagicMock()
    mock_ctx.call_capability = AsyncMock()
    plugin._set_context(mock_ctx)

    await plugin.on_load()

    try:
        # 1. 正常快轨长文生成
        mock_ctx.call_capability.return_value = {
            "success": True,
            "text": "# 攻略标题\n这是攻略正文内容。",
        }

        res = await plugin.publish_document_page(
            title="东京旅游攻略",
            writing_instruction="写一份详细行程安排",
            reference_text="涉谷、新宿、秋叶原",
            group_id=CHAT_GROUP,
            stream_id="stream_001",
        )

        assert res["ok"] is True
        assert res["mode"] == "fast_track_sync"
        assert "url" in res
        mock_ctx.call_capability.assert_awaited_once_with(
            "maisaka.reply.generate",
            stream_id="stream_001",
            reference="写一份详细行程安排",
            format_instruction="请撰写一篇标题为《东京旅游攻略》的完整长文 Markdown。\n补充参考材料：涉谷、新宿、秋叶原",
        )

        # 2. 包含 FMxxxx 时自动升级至慢轨
        plugin.run_agent_workspace = AsyncMock(return_value={"ok": True, "mode": "slow_track_async", "task_id": "job_mock"})
        upgrade_res = await plugin.publish_document_page(
            title="数据分析报告",
            writing_instruction="请结合附件 FM0042 的图表进行深入分析",
            group_id=CHAT_GROUP,
            stream_id="stream_002",
        )
        assert upgrade_res["mode"] == "slow_track_async"
        plugin.run_agent_workspace.assert_awaited_once()
    finally:
        await plugin.on_unload()


# ---------------------------------------------------------------------------
# 断言 5: 临时后端 Auto-Wake 与 Scale-to-Zero 断言
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_backend_auto_wake_and_scale_to_zero(tmp_path: Path):
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
        on_web_notify=notify_mock,
        listen_port=18797,
    )
    await gateway.start()

    token = "testtoken123"
    proj_dir = ws_root / "test_chat" / "projects" / "web_app"
    dist_dir = ws_root / "test_chat" / "dist" / "web_app"
    proj_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "index.html").write_text("<h1>Backend Live</h1>", encoding="utf-8")

    # 启动轻量 python3 -m http.server {PORT} 作为后端
    router.upsert_route(
        token=token,
        chat_id="test_chat",
        stream_id="stream_01",
        project_slug="web_app",
        title="Web App",
        mode="backend",
        dist_dir=dist_dir,
        project_dir=proj_dir,
        start_cmd=["python3", "-m", "http.server", "{PORT}"],
    )

    try:
        # 1. 首次请求：Auto-Wake 冷启动并返回 200
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://127.0.0.1:18797/",
                headers={"Host": f"{token}.s.testdomain.com"},
            ) as resp:
                assert resp.status == 200
                text = await resp.text()
                assert "Backend Live" in text

        # 2. 模拟休眠：Scale-to-Zero
        await supervisor.stop_backend(token)
        assert token not in supervisor._active

        # 3. 再次请求：自动唤醒
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://127.0.0.1:18797/",
                headers={"Host": f"{token}.s.testdomain.com"},
            ) as resp:
                assert resp.status == 200
                text = await resp.text()
                assert "Backend Live" in text
    finally:
        await gateway.stop()
        await supervisor.stop_all()


# ---------------------------------------------------------------------------
# 断言 6: URL 保护 Hook（response 键名）断言
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_protect_subdomain_url_hook():
    plugin = create_plugin()
    plugin.config.base_domain = "s.yourdomain.com"

    # 包含子域名：关闭分句与错别字
    event_with_domain = {
        "response": "攻略已为你生成好啦，点击查看：https://abc123xyz789.s.yourdomain.com",
        "enable_chinese_typo": True,
        "enable_splitter": True,
    }
    res1 = await plugin.protect_subdomain_url_hook(event_with_domain)
    assert res1["enable_chinese_typo"] is False
    assert res1["enable_splitter"] is False

    # 普通回复：保持原有配置不变
    event_normal = {
        "response": "今天天气真好，去公园散散步吧！",
        "enable_chinese_typo": True,
        "enable_splitter": True,
    }
    res2 = await plugin.protect_subdomain_url_hook(event_normal)
    assert res2["enable_chinese_typo"] is True
    assert res2["enable_splitter"] is True
