"""Batch 3 净室重构专项验证：微信撤回级联内存剔除、去硬编码与废弃模块清理校验。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from src.chat.debounce.controller import MessageDebouncer
from src.chat.heart_flow.heartflow_manager import heartflow_manager
from src.chat.message_receive.message import SessionMessage
from src.common.data_models.mai_message_data_model import GroupInfo, MessageInfo, UserInfo
from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
from src.companion_vault.truth_db import CompanionTruthDB
from src.maisaka.context.messages import InternalContextMessage
from src.maisaka.runtime import MaisakaHeartFlowChatting
from src.platform_io.wechat.driver import WeChatRootDriver
from src.platform_io.wechat.edge_client import WeChatEdgeClient
from src.platform_io.wechat.message_ingestor import MessageLedger
from src.platform_io.wechat.projection import native_message_id

ACCOUNT = "c" * 32
SOURCE = "http://127.0.0.1:8765"
CHAT = "room_revoke@chatroom"


def _make_runtime_stub(session_id: str, ext_msg_id: str) -> MaisakaHeartFlowChatting:
    rt = object.__new__(MaisakaHeartFlowChatting)
    rt.session_id = session_id
    rt.log_prefix = f"[{session_id}]"
    rt._message_ingest_lock = asyncio.Lock()
    rt._fixed_message_debouncer = MessageDebouncer()
    rt._fixed_message_debouncer.observe(
        author="wxid_sender",
        is_bare_image=False,
        is_pure_emoji=False,
        at_bot=False,
        received_at=100.0,
        wall_time=100.0,
        now=100.0,
    )
    rt._last_processed_index = 0
    rt._focus_group = None
    rt._message_turn_scheduled = True
    rt._deferred_message_turn_task = None

    msg = SessionMessage(message_id=ext_msg_id, timestamp=datetime.now(), platform="wechat")
    msg.session_id = session_id
    msg.message_info = MessageInfo(
        user_info=UserInfo(user_id="wxid_sender", user_nickname="测试群友"),
        group_info=GroupInfo(group_id=CHAT, group_name="撤回测试群"),
    )
    msg.raw_message = MessageSequence([TextComponent("说错话了马上撤回")])
    msg.processed_plain_text = "说错话了马上撤回"
    msg.initialized = True

    rt.message_cache = [msg]
    rt._chat_history = [
        InternalContextMessage(
            raw_message=MessageSequence([TextComponent("说错话了马上撤回")]),
            visible_text="说错话了马上撤回",
            timestamp=datetime.now(),
            message_id=ext_msg_id,
            source_kind="user",
        )
    ]
    return rt


@pytest.mark.asyncio
async def test_wechat_revoke_cascades_to_debounce_queue_and_chat_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """验证微信 7 秒防抖窗口内撤回消息时，LedgerSource -> WeChatRootDriver -> Runtime 即时清空缓存与历史"""
    ledger_path = tmp_path / "ledger.db"
    ledger = MessageLedger(str(ledger_path))

    ledger.ingest(
        SOURCE,
        "ep1",
        {
            "seq": 1,
            "account": ACCOUNT,
            "msg_id": "8801",
            "chat_id": CHAT,
            "revision": "rev_1",
            "observed_at_ms": 1000,
            "row": {
                "msgId": "8801",
                "talker": CHAT,
                "type": "1",
                "content": "wxid_sender:\n说错话了马上撤回",
                "msgSvrId": "9990001",
                "isSend": "0",
                "createTime": "1793880000000",
            },
        },
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={}))) as client:
        edge = WeChatEdgeClient(SOURCE, ACCOUNT, client=client, use_hook_send=True)
        driver = WeChatRootDriver(edge, allowed_chats=[CHAT], ledger_path=str(ledger_path), backfill=True)
        inbound_envelopes = []

        async def _on_inbound(env):
            inbound_envelopes.append(env)
            return True

        driver.set_inbound_handler(_on_inbound)
        assert await driver.ledger_source.consume_once(driver) == 1
        ext_id = native_message_id(SOURCE, ACCOUNT, "8801")
        assert inbound_envelopes[0].external_message_id == ext_id

        rt = _make_runtime_stub("sess_revoke_room", ext_id)
        monkeypatch.setitem(heartflow_manager.heartflow_chat_list, "sess_revoke_room", rt)

        ledger.ingest(
            SOURCE,
            "ep1",
            {
                "seq": 2,
                "account": ACCOUNT,
                "msg_id": "8801",
                "chat_id": CHAT,
                "revision": "rev_2_revoked",
                "observed_at_ms": 3000,
                "row": {
                    "msgId": "8801",
                    "talker": CHAT,
                    "type": "268445456",
                    "content": '"测试群友" 撤回了一条消息',
                    "msgSvrId": "9990001",
                    "isSend": "0",
                    "createTime": "1793880003000",
                },
            },
        )
        assert await driver.ledger_source.consume_once(driver) == 0
        assert rt.message_cache == []
        assert rt._chat_history == []
        assert rt._fixed_message_debouncer.deadline is None
        assert rt._message_turn_scheduled is False

    ledger.db.close()


def test_cleanroom_zero_hardcoded_identifiers_and_dead_dirs_removed(tmp_path: Path):
    """验证源码已剥离真实用户 wxid/昵称/143000 魔数，且废弃原型目录已归档移除"""
    repo_root = Path(__file__).resolve().parents[2]

    assert not (repo_root / "src" / "mobile_agent").exists()
    assert not (repo_root / "src" / "integration_tools").exists()
    assert not (repo_root / "plugins" / "embodied_workspace" / "fast_renderer.py").exists()

    truth_src = (repo_root / "src" / "companion_vault" / "truth_db.py").read_text(encoding="utf-8")
    proj_src = (repo_root / "src" / "platform_io" / "wechat" / "projection.py").read_text(encoding="utf-8")
    js_src = (repo_root / "src" / "phone_edge" / "message_capture.js").read_text(encoding="utf-8")
    dev_tools_src = (repo_root / "src" / "maisaka" / "builtin_tool" / "device_sandbox_tools.py").read_text(
        encoding="utf-8"
    )

    for forbidden in ("wxid_w5d0efdxczt322", "ì _eun", "许妍", "妍妍"):
        assert forbidden not in truth_src
        assert forbidden not in proj_src

    assert "143000" not in js_src
    assert "handle_sandbox_task_tool" not in dev_tools_src
    assert "100.87.39.37" not in dev_tools_src

    db = CompanionTruthDB(str(tmp_path / "anchor.db"))
    anchor = db._render_self_identity_anchor(self_wxid="wxid_custom_instance")
    assert "wxid_custom_instance" in anchor
    assert "@某群友" in anchor
    db.close()

