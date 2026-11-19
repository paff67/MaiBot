"""Offline regression tests for Batch 1 coupling, privacy isolation, and MVU fixes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
import json

import pytest

from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator
from src.companion_vault.consolidator import MemoryConsolidator
from src.companion_vault.mvu_engine import MVURuntimeEngine
from src.companion_vault.persona_v2_engine import PersonaV2RuntimeManager
from src.companion_vault.truth_db import CompanionTruthDB
from src.config.config import global_config
from src.config.official_configs import PersonalityProfile
from src.llm_models.payload_content.context_item import (
    ContextItemBuilder,
    ContextItemMeta,
    ContextToolCall,
    FunctionCallItem,
    RoleType,
)
from src.platform_io.wechat.message_ingestor import MessageLedger
from src.services.llm_service import LLMResponseResult

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "plugins" / "st_preset_engine" / "templates" / "personas"


def _make_session_msg(
    *,
    platform: str = "wechat",
    user_id: str,
    person_id: str = "",
    group_id: str = "",
    text: str = "hi",
    msg_id: str = "m1",
) -> Any:
    group_info = SimpleNamespace(group_id=group_id, group_name="测试群") if group_id else None
    user_info = SimpleNamespace(
        user_id=user_id,
        person_id=person_id,
        user_nickname="测试用户",
        user_cardname="",
    )
    msg_info = SimpleNamespace(user_info=user_info, group_info=group_info)
    return SimpleNamespace(
        message_id=msg_id,
        platform=platform,
        message_info=msg_info,
        processed_plain_text=text,
        time=1700000000.0,
    )


def _setup_owner_truth_db(tmp_path: Path, *, owner_wxid: str = "wxid_owner") -> Path:
    db_path = tmp_path / "truth.db"
    db = CompanionTruthDB(str(db_path))
    try:
        with db.conn:
            db.conn.execute(
                """
                INSERT INTO person_info (person_id, platform_ids, person_name, trust_role)
                VALUES ('P_OWNER', ?, '主人', 'OWNER')
                """,
                (json.dumps({"wechat": owner_wxid}),),
            )
    finally:
        db.close()
    return db_path


def test_group_consolidator_blocks_mvu_variables_mutation(tmp_path: Path) -> None:
    """群聊巩固器尝试修改 mvu_variables 时被 SQLite Authorizer 物理拦截且数据不变。"""
    source = "http://edge.local"
    account = "a" * 32
    chat_id = "123456@chatroom"
    truth_path = tmp_path / "truth.db"
    ledger_path = tmp_path / "ledger.db"

    db = CompanionTruthDB(str(truth_path))
    db.upsert_mvu_variable(
        "maibot_xuyan",
        scope_key="OWNER_PRIVATE",
        var_key="affection",
        var_type="number",
        var_value=30,
        visibility="PRIVATE",
    )
    db.close()

    ledger = MessageLedger(str(ledger_path))
    ledger.ingest(
        source,
        "epoch-1",
        {
            "seq": 1,
            "account": account,
            "msg_id": "1001",
            "chat_id": chat_id,
            "revision": "rev-1",
            "observed_at_ms": 1700000000000,
            "row": {
                "msgId": "1001",
                "talker": chat_id,
                "isSend": "0",
                "type": "1",
                "content": "wxid_other:\n群聊测试消息",
                "createTime": "1700000000000",
            },
        },
    )
    ledger.db.close()

    worker = MemoryConsolidator(
        str(truth_path),
        str(ledger_path),
        source=source,
        account=account,
        chat_id=chat_id,
        backfill=True,
    )
    try:
        worker.enqueue()
        job = worker.claim()
        assert job is not None
        with pytest.raises(ValueError, match="mvu_variables"):
            worker.commit(
                job,
                "UPDATE mvu_variables SET var_value='99' WHERE card_id='maibot_xuyan' AND var_key='affection';",
            )
        verify_db = CompanionTruthDB(str(truth_path))
        try:
            assert verify_db.get_mvu_variables("maibot_xuyan", is_owner_private=True)["affection"] == 30
        finally:
            verify_db.close()
    finally:
        worker.close()


def test_reply_to_bot_own_message_in_owner_dm_preserves_owner_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner 一对一私聊中引用/回复 Bot 自身消息时，回退会话对端 user_id 保持 is_owner_private=True。"""
    db_path = _setup_owner_truth_db(tmp_path, owner_wxid="wxid_owner")
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(db_path))
    monkeypatch.setattr(
        "src.chat.replyer.maisaka_generator_base.is_bot_self",
        lambda platform, uid: platform == "wechat" and uid == "wxid_bot_self",
    )
    monkeypatch.setattr(
        "src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info",
        lambda sid: (False, "wxid_owner") if sid == "dm_owner" else (True, "123@chatroom"),
    )

    gen = BaseMaisakaReplyGenerator.__new__(BaseMaisakaReplyGenerator)
    gen.chat_stream = SimpleNamespace(
        session_id="dm_owner",
        platform="wechat",
        user_id="wxid_owner",
        is_group_session=False,
    )

    bot_msg = _make_session_msg(user_id="wxid_bot_self", text="我刚说的话")
    is_owner_priv, role = gen._resolve_is_owner_private(session_id="dm_owner", reply_message=bot_msg)
    assert is_owner_priv is True
    assert role == "OWNER"

    # 群聊中引用 Bot 自身消息依然严格 Fail-Closed 返回 False
    gen.chat_stream = SimpleNamespace(
        session_id="grp_1",
        platform="wechat",
        user_id="wxid_owner",
        is_group_session=True,
    )
    grp_bot_msg = _make_session_msg(user_id="wxid_bot_self", group_id="123@chatroom")
    is_owner_priv_grp, role_grp = gen._resolve_is_owner_private(session_id="grp_1", reply_message=grp_bot_msg)
    assert is_owner_priv_grp is False
    assert role_grp == "GROUP_MEMBER"


def test_retro_prompt_persists_last_is_owner_private_and_reset_clears_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retro 路径必须刷新 _last_is_owner_private，且 _reset_st_request_state 必须清理跨请求残留。"""
    gen = BaseMaisakaReplyGenerator.__new__(BaseMaisakaReplyGenerator)
    gen.chat_stream = SimpleNamespace(session_id="grp_1", platform="wechat", user_id="u1", is_group_session=True)
    gen._last_is_owner_private = True
    gen._last_persona_v2_turn = SimpleNamespace(card_id="maibot_xuyan")

    gen._reset_st_request_state()
    assert gen._last_is_owner_private is False
    assert gen._last_persona_v2_turn is None

    # 模拟从 Owner 私聊残留 True 进入 retro 群聊构建，应刷新为 False
    gen._last_is_owner_private = True
    monkeypatch.setattr(gen, "_select_retro_prompt_name", lambda **kwargs: "default_retro")
    monkeypatch.setattr(gen, "_resolve_slot_profile", lambda args=None: SimpleNamespace(owner_private_prompt="SECRET"))
    monkeypatch.setattr(gen, "_build_personality_prompt", lambda: "persona")
    monkeypatch.setattr(gen, "_resolve_session_id", lambda sid=None: "grp_1")
    monkeypatch.setattr(gen, "_resolve_is_owner_private", lambda **kwargs: (False, "GROUP_MEMBER"))
    monkeypatch.setattr(
        gen,
        "_build_retro_template_context",
        lambda **kwargs: {"planner_reasoning": ""},
    )
    monkeypatch.setattr(gen, "_build_recalled_messages", lambda args=None: [])
    gen._load_prompt = lambda name, **ctx: "rendered"

    items = gen._build_retro_request_messages(
        chat_history=[],
        reply_message=None,
        reply_reason="",
        stream_id="grp_1",
    )
    assert items
    assert gen._last_is_owner_private is False


class _StubLLMClient:
    def __init__(self, result: LLMResponseResult) -> None:
        self.task_name = "replyer"
        self._result = result

    async def generate_response_with_context(self, *, context_factory, options=None) -> LLMResponseResult:
        await context_factory(SimpleNamespace(api_provider=SimpleNamespace(client_type="gemini")))
        return self._result


class _StubRuntimeManager:
    async def invoke_hook(self, _name: str, **kwargs: Any) -> Any:
        return SimpleNamespace(kwargs=dict(kwargs), aborted=False)


def _build_minimal_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    llm_result: LLMResponseResult,
    slot_mvu_enabled: bool = True,
    global_mvu_enabled: bool = True,
) -> tuple[BaseMaisakaReplyGenerator, Path, PersonaV2RuntimeManager]:
    db_path = _setup_owner_truth_db(tmp_path, owner_wxid="wxid_owner")
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(db_path))

    v2_dir = tmp_path / "personas"
    mgr = PersonaV2RuntimeManager(data_dir=v2_dir, templates_dir=TEMPLATES_DIR)
    monkeypatch.setattr("src.companion_vault.persona_v2_engine._PERSONA_V2_MANAGER", mgr)

    younger_prof = PersonalityProfile(
        name="许妍",
        nickname="许妍",
        personality="p",
        behavior_style="b",
        reply_style="r",
        group_chat_prompt="g",
        private_chat_prompts="p",
        persona_v2_card_id="maibot_xuyan",
        mvu_enabled=slot_mvu_enabled,
    )
    elder_prof = PersonalityProfile(
        name="许玥",
        nickname="许玥",
        personality="p",
        behavior_style="b",
        reply_style="r",
        group_chat_prompt="g",
        private_chat_prompts="p",
        persona_v2_card_id="maibot_xuyue",
        mvu_enabled=slot_mvu_enabled,
    )
    monkeypatch.setattr(global_config.personality, "twin_mode_enabled", True)
    monkeypatch.setattr(global_config.personality, "mvu_enabled", global_mvu_enabled)
    monkeypatch.setattr(global_config.personality, "younger_slot_profile", "许妍")
    monkeypatch.setattr(global_config.personality, "elder_slot_profile", "许玥")
    monkeypatch.setattr(global_config.personality, "profiles", [younger_prof, elder_prof])
    monkeypatch.setattr(global_config.personality, "selected_profile", "许妍")
    monkeypatch.setattr(
        "src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info",
        lambda sid: (False, "wxid_owner"),
    )

    gen = BaseMaisakaReplyGenerator.__new__(BaseMaisakaReplyGenerator)
    gen.chat_stream = SimpleNamespace(
        session_id="dm_owner",
        platform="wechat",
        user_id="wxid_owner",
        is_group_session=False,
    )
    gen.request_type = "maisaka_replyer"
    gen.express_model = _StubLLMClient(llm_result)
    gen.elder_express_model = _StubLLMClient(llm_result)
    gen._llm_client_cls = lambda **kwargs: _StubLLMClient(llm_result)
    gen._last_st_compiled_request = None
    gen._last_st_output_mode = "im_clean"
    gen._last_st_reply_tool_args = None
    gen._last_request_used_st = False
    gen._last_is_owner_private = False
    gen._last_persona_v2_turn = None
    gen._last_st_request_messages = None

    monkeypatch.setattr(gen, "_get_runtime_manager", lambda: _StubRuntimeManager())
    monkeypatch.setattr(gen, "_uses_st_preset_engine", lambda args=None: False)
    monkeypatch.setattr(gen, "_uses_retro_prompt", lambda: False)
    monkeypatch.setattr(gen, "_persist_reply_preview", lambda *args, **kwargs: None)
    monkeypatch.setattr(gen, "_build_request_messages", lambda **kwargs: _mark_owner_and_return(gen))
    return gen, db_path, mgr


def _mark_owner_and_return(gen: BaseMaisakaReplyGenerator) -> List[Any]:
    gen._last_is_owner_private = True
    return []


def _make_text_llm_result(text: str) -> LLMResponseResult:
    item = ContextItemBuilder().set_role(RoleType.Assistant).add_text_content(text).build()
    return LLMResponseResult(output_items=(item,), model_name="fake-model")


def _make_tool_llm_result(func_name: str, args: dict[str, Any]) -> LLMResponseResult:
    call_item = FunctionCallItem(
        meta=ContextItemMeta.create(logical_turn_id="turn_1"),
        tool_call=ContextToolCall(
            call_id="call_1",
            func_name=func_name,
            args_json=json.dumps(args, ensure_ascii=False).encode("utf-8"),
        ),
    )
    return LLMResponseResult(output_items=(call_item,), model_name="fake-model")


@pytest.mark.asyncio
async def test_plain_text_update_variable_applies_once_without_double_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """含 <UpdateVariable> 的纯文本/standard_chat 回复仅结算一次 delta，不得双倍累加。"""
    raw_resp = (
        "今天实验很顺利呀！<UpdateVariable><Analysis>愉快互动</Analysis>"
        '<JSONPatch>[{"op": "delta", "path": "/affection", "value": 3}]</JSONPatch></UpdateVariable>'
    )
    llm_res = _make_text_llm_result(raw_resp)
    gen, db_path, mgr = _build_minimal_generator(tmp_path, monkeypatch, llm_result=llm_res)

    xuyan = mgr.get_card("maibot_xuyan")
    assert xuyan is not None
    db = CompanionTruthDB(str(db_path))
    try:
        db.ensure_mvu_defaults(xuyan.card_id, MVURuntimeEngine._schema_defs_for_db(xuyan))
        db.upsert_mvu_variable(
            xuyan.card_id,
            scope_key="OWNER_PRIVATE",
            var_key="affection",
            var_type="number",
            var_value=25,
            visibility="PRIVATE",
        )
    finally:
        db.close()

    ok, reply_result = await gen.generate_reply_with_context(
        reply_message=_make_session_msg(user_id="wxid_owner", text="今天怎么样"),
        chat_history=[],
        stream_id="dm_owner",
        reply_tool_args={"persona_slot": "sister_younger"},
    )
    assert ok is True
    assert reply_result.completion.response_text == "今天实验很顺利呀！"
    assert "<UpdateVariable>" not in reply_result.completion.response_text

    db = CompanionTruthDB(str(db_path))
    try:
        after = MVURuntimeEngine.load_turn_variables(db, xuyan, is_owner_private=True)
        # 25 + 3 == 28（绝不是双倍 31）
        assert after["affection"] == 28
    finally:
        db.close()

    mvu_watch = reply_result.metrics.extra["st_telemetry"]["mvu_watch"]
    assert len(mvu_watch["changes"]) == 1
    assert mvu_watch["changes"][0]["delta"] == 3


@pytest.mark.asyncio
async def test_mvu_disabled_profile_skips_mvu_writeback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """slot_profile.mvu_enabled=False 时，即使回复包含 <UpdateVariable> 也不得写入 TruthDB。"""
    raw_resp = (
        "收到啦<UpdateVariable><Analysis>测试禁用</Analysis>"
        '<JSONPatch>[{"op": "delta", "path": "/affection", "value": 4}]</JSONPatch></UpdateVariable>'
    )
    llm_res = _make_text_llm_result(raw_resp)
    gen, db_path, mgr = _build_minimal_generator(
        tmp_path,
        monkeypatch,
        llm_result=llm_res,
        slot_mvu_enabled=False,
    )
    xuyan = mgr.get_card("maibot_xuyan")
    assert xuyan is not None
    db = CompanionTruthDB(str(db_path))
    try:
        db.ensure_mvu_defaults(xuyan.card_id, MVURuntimeEngine._schema_defs_for_db(xuyan))
        db.upsert_mvu_variable(
            xuyan.card_id,
            scope_key="OWNER_PRIVATE",
            var_key="affection",
            var_type="number",
            var_value=25,
            visibility="PRIVATE",
        )
    finally:
        db.close()

    ok, reply_result = await gen.generate_reply_with_context(
        reply_message=_make_session_msg(user_id="wxid_owner", text="hi"),
        chat_history=[],
        stream_id="dm_owner",
        reply_tool_args={"persona_slot": "sister_younger"},
    )
    assert ok is True
    assert reply_result.completion.response_text == "收到啦"
    db = CompanionTruthDB(str(db_path))
    try:
        after = MVURuntimeEngine.load_turn_variables(db, xuyan, is_owner_private=True)
        assert after["affection"] == 25
    finally:
        db.close()


@pytest.mark.asyncio
async def test_precomputed_slot_switch_applies_mvu_to_compiled_card_not_switched_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """one_pass_st 首轮按许妍编译，但工具调用切到 sister_elder 时，MVU 补丁应落盘至原编译卡而非污染许玥卡。"""
    precomputed = _make_tool_llm_result(
        "soliumbra_reply",
        {
            "persona_slot": "sister_elder",
            "game_content": (
                "姐姐来接手一下。<UpdateVariable><Analysis>基于首轮编译状态更新</Analysis>"
                '<JSONPatch>[{"op": "delta", "path": "/affection", "value": 2}]</JSONPatch></UpdateVariable>'
            ),
        },
    )
    gen, db_path, mgr = _build_minimal_generator(tmp_path, monkeypatch, llm_result=precomputed)
    gen._last_st_request_messages = []
    gen._last_request_used_st = True
    gen._last_persona_v2_turn = SimpleNamespace(card_id="maibot_xuyan", is_owner_private=True)
    monkeypatch.setattr(gen, "_uses_st_preset_engine", lambda args=None: True)

    xuyan = mgr.get_card("maibot_xuyan")
    xuyue = mgr.get_card("maibot_xuyue")
    assert xuyan is not None and xuyue is not None
    db = CompanionTruthDB(str(db_path))
    try:
        for card, start_aff in ((xuyan, 25), (xuyue, 20)):
            db.ensure_mvu_defaults(card.card_id, MVURuntimeEngine._schema_defs_for_db(card))
            db.upsert_mvu_variable(
                card.card_id,
                scope_key="OWNER_PRIVATE",
                var_key="affection",
                var_type="number",
                var_value=start_aff,
                visibility="PRIVATE",
            )
    finally:
        db.close()

    ok, reply_result = await gen.generate_reply_with_context(
        reply_message=_make_session_msg(user_id="wxid_owner", text="姐姐在吗"),
        chat_history=[],
        stream_id="dm_owner",
        reply_tool_args={
            "persona_slot": "sister_elder",
            "_precomputed_st_response": precomputed,
        },
    )
    assert ok is True
    assert reply_result.completion.response_text == "姐姐来接手一下。"
    db = CompanionTruthDB(str(db_path))
    try:
        xuyan_after = MVURuntimeEngine.load_turn_variables(db, xuyan, is_owner_private=True)
        xuyue_after = MVURuntimeEngine.load_turn_variables(db, xuyue, is_owner_private=True)
        assert xuyan_after["affection"] == 27
        assert xuyue_after["affection"] == 20
    finally:
        db.close()
