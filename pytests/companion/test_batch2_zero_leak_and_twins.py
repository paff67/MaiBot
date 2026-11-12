"""Batch 2 净室重构专项验证：LEAK-01~06 四道物理防线、单一 Schema、双子预留与多模态内联直通。"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.embodied_workspace.user_truth_writer import UserScopedTruthWriter
from src.chat.message_receive.message import SessionMessage
from src.common.data_models.mai_message_data_model import GroupInfo, MessageInfo, UserInfo
from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
from src.companion_vault.asset_encoder import encode_inline_descriptor, encode_media_part
from src.companion_vault.compute_ledger import ComputeLedger, PERSONA_SLOT_ELDER, PERSONA_SLOT_YOUNGER
from src.companion_vault.consolidator import MemoryConsolidator
from src.companion_vault.output_guardrail import check_group_outbound_text
from src.companion_vault.runtime_bridge import build_planner_truth_catalog_async, get_twin_routing_state
from src.companion_vault.truth_db import CompanionTruthDB
from src.maisaka.builtin_tool import query_person_profile, reply as reply_tool_module
from src.maisaka.memory.person_profile import build_person_profile_injection_messages
from src.platform_io.wechat.message_ingestor import MessageLedger

ACCOUNT = "b" * 32
SOURCE = "https://127.0.0.1:8443"


def _make_session_message(
    *,
    platform: str = "wechat",
    user_id: str = "wxid_owner",
    nickname: str = "星河",
    group_id: str = "",
    text: str = "大家晚上好",
    msg_id: str = "5001",
    session_id: str = "sess_test",
) -> SessionMessage:
    msg = SessionMessage(
        message_id=msg_id,
        timestamp=datetime.datetime.now(),
        platform=platform,
    )
    msg.session_id = session_id
    msg.message_info = MessageInfo(
        user_info=UserInfo(user_id=user_id, user_nickname=nickname),
        group_info=GroupInfo(group_id=group_id, group_name="测试群") if group_id else None,
    )
    msg.raw_message = MessageSequence([TextComponent(text)])
    msg.processed_plain_text = text
    msg.initialized = True
    return msg


@pytest.mark.asyncio
async def test_defense_line_1_leak01_leak02_sql_projection_and_owner_overlay(tmp_path: Path):
    """验证 LEAK-01 & LEAK-02 物理封堵 + OwnerPersonaOverlay 仅在 OWNER 私聊挂载"""
    db_path = tmp_path / "truth.db"
    db = CompanionTruthDB(str(db_path))
    with db.conn:
        db.conn.execute(
            """
            INSERT INTO person_info (
                person_id, platform_ids, person_name, wechat_remark, trust_role,
                one_line_intro, public_impression, owner_private_notes, intimacy_score
            ) VALUES (?, ?, '星河', '绝密备注_老公', 'OWNER', '古典乐与AI研究者', '聊过拉赫玛尼诺夫第三钢协', '绝密金主关系与私密恋人暗号9527', 99)
            """,
            ("P_OWNER", json.dumps({"wechat": "wxid_owner"})),
        )
    db.close()

    # 1. LEAK-01 封堵：OWNER 在群聊发言时，自动画像注入严禁包含 owner_private_notes 与 wechat_remark
    group_msg = _make_session_message(
        user_id="wxid_owner",
        nickname="星河",
        group_id="room_alpha@chatroom",
        session_id="sess_group_alpha",
    )
    group_blocks = await build_person_profile_injection_messages(
        anchor_message=group_msg,
        db_path=str(db_path),
        is_group_chat=True,
    )
    joined_group = "\n".join(group_blocks)
    assert "聊过拉赫玛尼诺夫第三钢协" in joined_group
    assert "绝密金主关系" not in joined_group
    assert "9527" not in joined_group
    assert "绝密备注_老公" not in joined_group

    # 2. OWNER 私聊：完整读取 owner_private_notes 与 OwnerPersonaOverlay
    dm_msg = _make_session_message(
        user_id="wxid_owner",
        nickname="星河",
        group_id="",
        session_id="sess_owner_dm",
    )
    dm_blocks = await build_person_profile_injection_messages(
        anchor_message=dm_msg,
        db_path=str(db_path),
        is_group_chat=False,
    )
    assert "绝密金主关系与私密恋人暗号9527" in "\n".join(dm_blocks)

    owner_catalog = await build_planner_truth_catalog_async(
        session_id="sess_owner_dm",
        is_group_chat=False,
        user_id="wxid_owner",
        platform="wechat",
        db_path=str(db_path),
        owner_overlay_text="专属附加设定：私下语气更松弛亲昵",
    )
    assert "<Owner专属关系与私密记忆层>" in owner_catalog
    assert "绝密金主关系与私密恋人暗号9527" in owner_catalog
    assert "专属附加设定：私下语气更松弛亲昵" in owner_catalog

    group_catalog = await build_planner_truth_catalog_async(
        session_id="sess_group_alpha",
        is_group_chat=True,
        user_id="wxid_owner",
        platform="wechat",
        db_path=str(db_path),
        owner_overlay_text="专属附加设定：私下语气更松弛亲昵",
    )
    assert "<Owner专属关系与私密记忆层>" not in group_catalog
    assert "绝密金主关系" not in group_catalog
    assert "专属附加设定" not in group_catalog

    # 3. LEAK-02 封堵：群聊调用 query_person_profile 工具查询 OWNER，绝不返回 owner_private_notes
    group_tool_ctx = SimpleNamespace(
        runtime=SimpleNamespace(
            chat_stream=SimpleNamespace(is_group_session=True, platform="wechat", user_id="wxid_friend")
        ),
        build_success_result=lambda name, text, structured_content=None: SimpleNamespace(
            ok=True, text=text, structured=structured_content
        ),
        build_failure_result=lambda name, err, structured_content=None: SimpleNamespace(
            ok=False, text=err, structured=structured_content
        ),
    )
    inv = SimpleNamespace(tool_name="query_person_profile", arguments={"person_name": "星河", "db_path": str(db_path)})
    res_grp = await query_person_profile.handle_tool(group_tool_ctx, inv)
    assert res_grp.ok is True
    assert "聊过拉赫玛尼诺夫第三钢协" in res_grp.text
    assert "绝密金主关系" not in res_grp.text
    assert "绝密备注_老公" not in res_grp.text
    # 尝试用私密词反搜（防模糊查询侧信道）也必须搜不到！
    inv_probe = SimpleNamespace(
        tool_name="query_person_profile", arguments={"person_name": "暗号9527", "db_path": str(db_path)}
    )
    res_probe = await query_person_profile.handle_tool(group_tool_ctx, inv_probe)
    assert res_probe.ok is False


def test_defense_line_2_leak03_leak04_consolidator_hard_locks_and_quest_scope_isolation(tmp_path: Path):
    """验证 LEAK-03 & LEAK-04 物理封堵 + 群待办 scope_chat_id 跨群零泄漏"""
    truth_path = tmp_path / "truth.db"
    ledger_path = tmp_path / "ledger.db"
    ledger = MessageLedger(str(ledger_path))

    def _ingest(seq: int, chat: str, body: str) -> None:
        ledger.ingest(
            SOURCE,
            "ep1",
            {
                "seq": seq,
                "account": ACCOUNT,
                "msg_id": str(seq),
                "chat_id": chat,
                "revision": f"r{seq}",
                "observed_at_ms": seq * 1000,
                "row": {
                    "msgId": str(seq),
                    "talker": chat,
                    "type": "1",
                    "content": f"wxid_owner:\n{body}" if chat.endswith("@chatroom") else body,
                    "msgSvrId": str(9000 + seq),
                    "isSend": "0",
                    "createTime": "1793880000000",
                },
            },
        )

    _ingest(1, "wxid_owner", "周五晚上八点我们两个人私下看电影")
    _ingest(2, "group_a@chatroom", "周末群里约打羽毛球")

    dm_worker = MemoryConsolidator(
        str(truth_path), str(ledger_path), source=SOURCE, account=ACCOUNT, chat_id="wxid_owner", backfill=True
    )
    with dm_worker.db:
        dm_worker.db.execute(
            "INSERT INTO person_info (person_id, platform_ids, person_name, trust_role, owner_private_notes) "
            "VALUES ('P_OWNER', '{\"wechat\":\"wxid_owner\"}', '星河', 'OWNER', '初始绝密笔记')"
        )

    # 1. LEAK-04 封堵：私聊巩固模型故意将私密约定 quests_events 与 user_status 误标为 PUBLIC，提交层强制覆写回 PRIVATE！
    dm_worker.enqueue()
    dm_job = dm_worker.claim()
    malicious_dm_sql = """
    INSERT INTO user_status (key, category, current_value, visibility, updated_at)
    VALUES ('secret_plan', 'date', '周五私下约会', 'PUBLIC', '2026-11-11 20:00');
    INSERT INTO quests_events (quest_code, title, current_progress, status, visibility, updated_at)
    VALUES ('QE0001', '周五晚双人私密电影之夜', '已定好影院', 'ongoing', 'PUBLIC', '2026-11-11 20:00');
    INSERT INTO chronicle_events (code_index, summary, chronicle_text, visibility)
    VALUES ('AM0001', '私下看电影约定', '周五晚上八点我们两个人私下看电影', 'PUBLIC');
    """
    assert dm_worker.commit(dm_job, malicious_dm_sql) is True

    q_row = dm_worker.db.execute("SELECT visibility, scope_chat_id FROM quests_events WHERE quest_code='QE0001'").fetchone()
    u_row = dm_worker.db.execute("SELECT visibility, scope_chat_id FROM user_status WHERE key='secret_plan'").fetchone()
    c_row = dm_worker.db.execute("SELECT visibility, scope_chat_id FROM chronicle_events WHERE code_index='AM0001'").fetchone()
    assert tuple(q_row) == ("PRIVATE", "wxid_owner")
    assert tuple(u_row) == ("PRIVATE", "wxid_owner")
    assert tuple(c_row) == ("PRIVATE", "wxid_owner")

    # 2. LEAK-03 封堵：群聊巩固器尝试更新 owner_private_notes 被 SQLite Authorizer 内核级物理拒绝！
    grp_worker = MemoryConsolidator(
        str(truth_path), str(ledger_path), source=SOURCE, account=ACCOUNT, chat_id="group_a@chatroom", backfill=True
    )
    grp_worker.enqueue()
    grp_job = grp_worker.claim()
    with pytest.raises(ValueError, match="owner_private_notes"):
        grp_worker.commit(
            grp_job,
            "UPDATE person_info SET owner_private_notes='群聊篡改绝密笔记' WHERE person_id='P_OWNER';",
        )

    # 群聊合法更新 public_impression 与本群待办：成功写入且自动绑定 scope_chat_id='group_a@chatroom'
    valid_grp_sql = """
    UPDATE person_info SET public_impression='喜欢周末打羽毛球' WHERE person_id='P_OWNER';
    INSERT INTO quests_events (quest_code, title, current_progress, status, visibility)
    VALUES ('QE0002', '群A周末羽毛球局', '报名中', 'ongoing', 'PUBLIC');
    """
    assert grp_worker.commit(grp_job, valid_grp_sql) is True

    # 3. 验证跨群待办隔离（STM-03 修复）：群 A 的待办绝不注入群 B 的常驻提示词，且私聊待办绝不注入任何群聊！
    truth_check = CompanionTruthDB(str(truth_path))
    cat_group_a = truth_check.build_planner_catalog_injection(
        is_group_chat=True, sender_trust_role="GROUP_MEMBER", chat_id="group_a@chatroom"
    )
    cat_group_b = truth_check.build_planner_catalog_injection(
        is_group_chat=True, sender_trust_role="GROUP_MEMBER", chat_id="group_b@chatroom"
    )
    assert "群A周末羽毛球局" in cat_group_a
    assert "周五晚双人私密电影之夜" not in cat_group_a
    assert "周五私下约会" not in cat_group_a
    assert "群A周末羽毛球局" not in cat_group_b
    assert "周五晚双人私密电影之夜" not in cat_group_b

    # 验证 UserScopedTruthWriter 群网页提交同样绑定 scope_chat_id
    writer = UserScopedTruthWriter(truth_db_path=truth_path, wechat_source=SOURCE, wechat_account=ACCOUNT)
    writer.apply_user_scoped_patch(
        actor_wxid="wxid_member_a",
        origin_chat_id="group_a@chatroom",
        patch={"quest_title": "群A聚餐投票"},
    )
    cat_group_b_after = truth_check.build_planner_catalog_injection(
        is_group_chat=True, sender_trust_role="GROUP_MEMBER", chat_id="group_b@chatroom"
    )
    assert "群A聚餐投票" not in cat_group_b_after

    truth_check.close()
    grp_worker.close()
    dm_worker.close()
    ledger.db.close()


@pytest.mark.asyncio
async def test_defense_line_3_and_4_leak05_leak06_and_output_guardrail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """验证 LEAK-05 (微信旁路禁用 A_memorix)、LEAK-06 (私聊表达严禁流入群聊选择器) 与防线四群聊出口熔断"""
    from src.chat.replyer.maisaka_expression_selector import MaisakaExpressionSelector
    from src.common.utils.utils_config import ChatConfigUtils
    from src.services.memory_flow_service import MemoryAutomationService

    automation = MemoryAutomationService()
    automation._observe_sender_identity = AsyncMock()
    automation.fact_writeback.enqueue = AsyncMock()
    automation.chat_summary_writeback.enqueue = AsyncMock()
    automation.image_writeback.enqueue = AsyncMock()
    automation._started = True

    wechat_msg = _make_session_message(platform="wechat", text="私密对话")
    await automation.on_incoming_message(wechat_msg)
    await automation.on_message_sent(wechat_msg)
    automation._observe_sender_identity.assert_not_awaited()
    automation.fact_writeback.enqueue.assert_not_awaited()
    automation.chat_summary_writeback.enqueue.assert_not_awaited()
    automation.image_writeback.enqueue.assert_not_awaited()

    monkeypatch.setattr(ChatConfigUtils, "is_private_session_id", lambda sid: sid == "sid_owner_private")
    monkeypatch.setattr(ChatConfigUtils, "collect_known_private_session_ids", lambda: {"sid_owner_private"})

    selector = MaisakaExpressionSelector()
    priv_scope, priv_global = selector._resolve_expression_group_scope("sid_owner_private")
    assert priv_scope == {"sid_owner_private"}
    assert priv_global is False

    class _FakeSessionCtx:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def exec(self, _stmt):
            return SimpleNamespace(
                all=lambda: [
                    (1, "被夸奖时", "只对主人说的亲密撒娇句式", 5, "sid_owner_private"),
                    (2, "群友打招呼时", "大家好呀今天吃什么", 3, "sid_public_group"),
                ]
            )

    monkeypatch.setattr("src.chat.replyer.maisaka_expression_selector.get_db_session", lambda **kw: _FakeSessionCtx())
    monkeypatch.setattr(selector, "_resolve_expression_group_scope", lambda sid: ({sid, "sid_public_group"}, True))

    group_candidates = selector._load_all_expression_candidates("sid_public_group")
    styles = [c["style"] for c in group_candidates]
    assert "大家好呀今天吃什么" in styles
    assert "只对主人说的亲密撒娇句式" not in styles

    db_path = tmp_path / "guard_truth.db"
    db = CompanionTruthDB(str(db_path))
    with db.conn:
        db.conn.execute(
            "INSERT INTO global_state (row_id, fairy_remote_balance_usd, fairy_unsettled_cost_usd) VALUES (1, 8.37, 0.0)"
        )
    db.close()

    monkeypatch.setenv("MAIBOT_GROUP_SECRET_ANCHORS", "专属金主暗号,秘密昵称宝宝")
    ok_normal, _ = check_group_outbound_text("哈哈你们太能整活了，周末一起开黑！", is_group_chat=True, db_path=str(db_path))
    assert ok_normal is True

    ok_leak_word, hit1 = check_group_outbound_text("其实我有专属金主暗号哦", is_group_chat=True, db_path=str(db_path))
    assert ok_leak_word is False and hit1 == "专属金主暗号"

    ok_leak_bal, hit2 = check_group_outbound_text("当前剩余算力 $8.37 USD", is_group_chat=True, db_path=str(db_path))
    assert ok_leak_bal is False and hit2 == "$8.37 USD"

    ok_dm_allowed, _ = check_group_outbound_text("主人，当前剩余算力 $8.37 USD", is_group_chat=False, db_path=str(db_path))
    assert ok_dm_allowed is True


@pytest.mark.asyncio
async def test_compute_ledger_twins_away_arc_st_slots_and_inline_media_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """验证第三章双子预留状态机、ST 零侵入挂载点与 MAIBOT_FILES_API_ENABLED=0 内联直通"""
    from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator
    from src.maisaka.chat_loop_service import MaisakaChatLoopService

    db_path = tmp_path / "twins.db"
    events: list[str] = []

    async def on_away(bal: float) -> str:
        events.append(f"away:{bal:.2f}")
        return "本乡研究室闭关跑大规模实验（预计周末回）"

    async def on_return(bal: float, arc: str) -> None:
        events.append(f"return:{bal:.2f}:{arc}")

    ledger = ComputeLedger(
        truth_db_path=db_path,
        on_elder_away_enter=on_away,
        on_elder_return=on_return,
    )
    snap_tok = ledger.on_token_usage("gemini-4-argon", prompt_tokens=100_000, completion_tokens=20_000)
    assert snap_tok.unsettled_local_cost_usd > 0
    assert "+09:00" in snap_tok.updated_at_iso

    ledger._active_persona_slot = PERSONA_SLOT_ELDER
    snap_low = await ledger.apply_remote_balance_reading(2.20, is_available=True)
    assert snap_low.tier_code == "THRIFTY"
    assert snap_low.active_persona_slot == PERSONA_SLOT_YOUNGER
    assert "本乡研究室" in snap_low.elder_away_arc
    routing_low = get_twin_routing_state(str(db_path))
    assert routing_low["force_younger_planner"] is True

    snap_recovered = await ledger.apply_remote_balance_reading(12.00, is_available=True)
    assert snap_recovered.tier_code == "STANDARD"
    assert any(e.startswith("return:12.00:") for e in events)

    spec = reply_tool_module.get_tool_spec()
    assert spec.parameters_schema["properties"]["persona_slot"]["enum"] == ["sister_younger", "sister_elder"]

    loop_svc = MaisakaChatLoopService(is_group_chat=False)
    assert loop_svc.chat_execution_mode == "dual_director"

    gen = object.__new__(BaseMaisakaReplyGenerator)
    assert gen._uses_st_preset_engine() is False

    monkeypatch.setenv("MAIBOT_FILES_API_ENABLED", "0")
    fail_client = MagicMock()
    fail_client.files.upload.side_effect = AssertionError("禁用 Files API 时绝不能发起 /upload 请求")

    wav_file = tmp_path / "voice_1mb.wav"
    wav_bytes = b"RIFF" + (b"\x00" * (600 * 1024))
    wav_file.write_bytes(wav_bytes)

    part, uri, _ = encode_media_part(wav_file, mime_type="audio/wav", client=fail_client)
    assert uri is None
    assert part.inline_data is not None

    desc, desc_uri, _ = encode_inline_descriptor(
        file_code="FM0099",
        semantic_name="语音消息",
        raw_bytes=wav_bytes,
        mime_type="audio/wav",
        client=fail_client,
    )
    assert desc_uri is None
    assert "audio_base64" in desc
    fail_client.files.upload.assert_not_called()



