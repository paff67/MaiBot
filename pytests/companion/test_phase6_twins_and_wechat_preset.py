"""Phase 6 双子原生运行时、Meowssiah-WeChat-IM 预设解包、Fail-Closed 隔离与 WebUI 遥测专项测试。"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.chat.message_receive.message import SessionMessage
from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator
from src.common.data_models.mai_message_data_model import GroupInfo, MessageInfo, UserInfo
from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
from src.companion_vault.compute_ledger import ComputeLedger
from src.companion_vault.st_preset_engine import (
    STCompileContext,
    STHistoryTurn,
    STPresetEngine,
    STPresetRuntimeManager,
)
from src.companion_vault.truth_db import CompanionTruthDB
from src.companion_vault.twin_narrative_router import TwinNarrativeDirector
from src.companion_vault.twin_runtime_router import TwinTurnDecision, resolve_turn_route
from src.config.config import global_config
from src.config.model_configs import ModelTaskConfig
from src.config.official_configs import PersonalityConfig, PersonalityProfile, TwinEconomyConfig
from src.maisaka.builtin_tool.reply import _build_monitor_metadata
from src.maisaka.chat_loop_service import MaisakaChatLoopService
from src.maisaka.monitor.events import _serialize_tool_results
from src.maisaka.monitor.planner_delta import PlannerDeltaEncoder
from src.maisaka.reasoning_engine import MaisakaReasoningEngine
from src.services import service_task_resolver


REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PRESET_PATH = REPO_ROOT / "plugins" / "st_preset_engine" / "templates" / "Meowssiah-WeChat-IM.json"


def _make_session_message(
    *,
    platform: str = "wechat",
    user_id: str = "wxid_owner",
    nickname: str = "星河",
    group_id: str = "",
    text: str = "晚上好",
    msg_id: str = "msg_001",
    session_id: str = "sess_private_owner",
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


def _seed_truth_db(db_path: Path) -> None:
    db = CompanionTruthDB(str(db_path))
    with db.conn:
        db.conn.execute(
            """
            INSERT INTO person_info (
                person_id, platform_ids, person_name, wechat_remark, trust_role,
                one_line_intro, public_impression, owner_private_notes, intimacy_score
            ) VALUES (?, ?, '星河', '绝密备注_老公', 'OWNER', 'AI 架构师', '认真严谨', '主人专属暗号9527', 99)
            """,
            ("P_OWNER", json.dumps({"wechat": "wxid_owner"})),
        )
        db.conn.execute(
            """
            INSERT INTO person_info (
                person_id, platform_ids, person_name, wechat_remark, trust_role,
                one_line_intro, public_impression, owner_private_notes, intimacy_score
            ) VALUES (?, ?, '路人甲', '', 'FRIEND', '普通好友', '普通群友', '', 10)
            """,
            ("P_FRIEND", json.dumps({"wechat": "wxid_friend"})),
        )
    db.close()


# ---------------------------------------------------------------------------
# 1. Meowssiah-WeChat-IM.json 完整性与 im_clean 多标签 + 原生 CoT 解包
# ---------------------------------------------------------------------------


def test_scenario_1_meowssiah_wechat_im_integrity_and_im_clean_unwrap(tmp_path: Path) -> None:
    """验证预设条目开关契约、自动自举及 <thinking>/<think>/<think_nya>/reasoning/<summary>/<comment> 解包。"""
    assert TEMPLATE_PRESET_PATH.is_file(), "templates/Meowssiah-WeChat-IM.json 必须随仓托管"

    presets_dir = tmp_path / "presets"
    mgr = STPresetRuntimeManager(presets_dir=presets_dir)
    cfg = mgr.load_config()
    bootstrapped = presets_dir / "Meowssiah-WeChat-IM.json"
    assert bootstrapped.is_file()
    assert cfg["active_preset"] == "Meowssiah-WeChat-IM"

    data = json.loads(TEMPLATE_PRESET_PATH.read_text(encoding="utf-8"))
    order_grp = next(g for g in data["prompt_order"] if g.get("character_id") == 100001)
    order = order_grp["order"]
    pmap = {p["identifier"]: p for p in data["prompts"]}

    # 红线 1：Meowssiah-2.1.7 底座下严禁牺牲思考/总结/猫猫留言/涩涩控制/大思考步骤，仅关 [34] 字数与 [48] 行动选项，替换 [29]
    for idx in (21, 23, 25, 27, 29, 40, 45, 49, 50, 52):
        assert order[idx]["enabled"] is True, f"索引 [{idx}] 必须保持开启"
    for idx in (2, 22, 24, 26, 34, 48):
        assert order[idx]["enabled"] is False, f"索引 [{idx}] 应保持关闭"

    entry_29 = pmap[order[29]["identifier"]]
    assert "s喵特调·真人回信" in entry_29["name"]
    assert "<game></game>" in entry_29["content"]

    engine = STPresetEngine.from_file(TEMPLATE_PRESET_PATH)

    # 验证 Meowssiah-2.1.7 registerThinkingStep 与 EFFORT LEVEL 0 / LOW 思考配置透出
    deep_compiled = engine.compile_to_context_items(
        STCompileContext(
            char_name="许玥",
            user_name="paff",
            last_user_message="晚上好",
            protocol_mode="spreset_tool_envelope",
            deep_think=True,
        )
    )
    assert deep_compiled.extra_params.get("include_thoughts") is False
    assert deep_compiled.extra_params.get("thinking_level") == "LOW"
    assert deep_compiled.extra_params.get("tool_config", {}).get("function_calling_config", {}).get("mode") == "ANY"
    assert "deep_think_required_steps" in deep_compiled.context_items[-1].output
    assert "①" in deep_compiled.context_items[-1].output

    # 测试 <thinking> / <think> / <think_nya> / <report_nya> / native_reasoning / <summary> / <comment> 混合解包
    raw_model_text = (
        "<thinking>先确认上文情绪状态和身份边界。</thinking>\n"
        "<think>决定用轻快微信口吻两句回应。</think>\n"
        "<think_nya>喵呜，准备敲微信气泡！</think_nya>\n"
        "<report_nya~>补一段 2.1.7 report_nya 校验。</report_nya~>\n"
        "<game>刚忙完手头那点事儿\n顺便给你留了热牛奶</game>\n"
        "<summary>角色结束忙碌后向用户发消息报平安，气氛轻松温柔。</summary>\n"
        "<comment>小猫之神觉得今天这句很像真人秒回！</comment>"
    )
    unwrapped = engine.unwrap_response(
        response_text=raw_model_text,
        native_reasoning="原生推理补充思考片段。",
        output_mode="im_clean",
    )

    assert unwrapped.visible_text == "刚忙完手头那点事儿\n顺便给你留了热牛奶"
    assert unwrapped.game_content == "刚忙完手头那点事儿\n顺便给你留了热牛奶"
    for leaked_tag in ("<thinking>", "<think>", "<think_nya>", "<report_nya", "<summary>", "<comment>", "先确认上文情绪状态", "小猫之神"):
        assert leaked_tag not in unwrapped.visible_text

    assert "先确认上文情绪状态和身份边界。" in unwrapped.think_nya
    assert "决定用轻快微信口吻两句回应。" in unwrapped.think_nya
    assert "喵呜，准备敲微信气泡！" in unwrapped.think_nya
    assert "补一段 2.1.7 report_nya 校验。" in unwrapped.think_nya
    assert "原生推理补充思考片段。" in unwrapped.think_nya
    assert unwrapped.summary == "角色结束忙碌后向用户发消息报平安，气氛轻松温柔。"
    assert unwrapped.comment == "小猫之神觉得今天这句很像真人秒回！"

    # 同时验证 SPreset 工具信封解包（soliumbra_reply）
    tool_calls = [
        {
            "name": "soliumbra_reply",
            "args": {
                "think_nya": "工具内思维链",
                "game_content": "<game>今晚别熬太晚呀</game>",
                "summary": "叮嘱早点休息",
                "comment": "晚安猫猫头",
            },
        }
    ]
    tool_unwrapped = engine.unwrap_response(
        response_text="",
        tool_calls=tool_calls,
        output_mode="im_clean",
    )
    assert tool_unwrapped.used_tool_envelope is True
    assert tool_unwrapped.visible_text == "今晚别熬太晚呀"
    assert tool_unwrapped.think_nya == "工具内思维链"
    assert tool_unwrapped.summary == "叮嘱早点休息"
    assert tool_unwrapped.comment == "晚安猫猫头"


# ---------------------------------------------------------------------------
# 2. Fail-Closed 隐私铁律：is_owner_private 与 owner_private_prompt 物理隔离
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_2_fail_closed_owner_private_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证仅 (not is_group_chat) and (trust_role == 'OWNER') 才挂载 owner_private_prompt 与 owner_only 条目。"""
    db_path = tmp_path / "truth.db"
    _seed_truth_db(db_path)
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(db_path))

    younger_prof = PersonalityProfile(
        name="妹妹",
        nickname="许妍",
        personality="妹妹公开人设",
        behavior_style="自然参与互动",
        reply_style="妹妹日常口吻",
        group_chat_prompt="群聊注意",
        private_chat_prompts="私聊注意",
        owner_private_prompt="OWNER私密密钥_妹妹专属甜蜜称呼",
    )
    elder_prof = PersonalityProfile(
        name="姐姐",
        nickname="许汐",
        personality="姐姐公开人设",
        behavior_style="冷静可靠",
        reply_style="姐姐沉稳口吻",
        group_chat_prompt="群聊注意",
        private_chat_prompts="私聊注意",
        owner_private_prompt="OWNER私密密钥_姐姐专属夜间设定",
    )
    pers_cfg = PersonalityConfig(
        twin_mode_enabled=True,
        younger_slot_profile="妹妹",
        elder_slot_profile="姐姐",
        profiles=[younger_prof, elder_prof],
        selected_profile="妹妹",
        owner_private_prompt="OWNER私密密钥_全局兜底",
    )
    eco_cfg = TwinEconomyConfig(enabled=True)
    ledger = ComputeLedger(db_path)
    await ledger.apply_remote_balance_reading(10.0, is_available=True)

    # A. 群聊中即便 OWNER 本人发言，也必须 is_owner_private=False
    group_owner_decision = await resolve_turn_route(
        session_id="wechat_room_alpha",
        platform="wechat",
        sender_id="wxid_owner",
        is_group_chat=True,
        latest_user_text="大家在聊啥",
        ledger=ledger,
        truth_db_path=str(db_path),
        eco_cfg=eco_cfg,
        personality_cfg=pers_cfg,
    )
    assert group_owner_decision.sender_trust_role == "OWNER"
    assert group_owner_decision.is_owner_private is False

    # B. 私聊中 STRANGER / FRIEND 发言，同样严格 is_owner_private=False
    priv_stranger_decision = await resolve_turn_route(
        session_id="wechat_priv_stranger",
        platform="wechat",
        sender_id="wxid_stranger_unknown",
        is_group_chat=False,
        latest_user_text="你好",
        ledger=ledger,
        truth_db_path=str(db_path),
        eco_cfg=eco_cfg,
        personality_cfg=pers_cfg,
    )
    assert priv_stranger_decision.is_owner_private is False

    priv_friend_decision = await resolve_turn_route(
        session_id="wechat_priv_friend",
        platform="wechat",
        sender_id="wxid_friend",
        is_group_chat=False,
        latest_user_text="在吗",
        ledger=ledger,
        truth_db_path=str(db_path),
        eco_cfg=eco_cfg,
        personality_cfg=pers_cfg,
    )
    assert priv_friend_decision.sender_trust_role == "FRIEND"
    assert priv_friend_decision.is_owner_private is False

    # C. 仅私聊 + OWNER 同时成立时，is_owner_private=True
    priv_owner_decision = await resolve_turn_route(
        session_id="wechat_priv_owner",
        platform="wechat",
        sender_id="wxid_owner",
        is_group_chat=False,
        latest_user_text="我回来啦",
        ledger=ledger,
        truth_db_path=str(db_path),
        eco_cfg=eco_cfg,
        personality_cfg=pers_cfg,
    )
    assert priv_owner_decision.sender_trust_role == "OWNER"
    assert priv_owner_decision.is_owner_private is True

    # 验证 Planner @D0 导演尾巴：群聊 OWNER 与私聊陌生人均严禁泄露 owner_private_prompt
    group_guidance = MaisakaChatLoopService.build_planner_d0_director_guidance(group_owner_decision)
    stranger_guidance = MaisakaChatLoopService.build_planner_d0_director_guidance(priv_stranger_decision)
    owner_priv_guidance = MaisakaChatLoopService.build_planner_d0_director_guidance(priv_owner_decision)
    assert "OWNER私密密钥" not in group_guidance
    assert "OWNER私密密钥" not in stranger_guidance
    assert "OWNER私密密钥_妹妹专属甜蜜称呼" in owner_priv_guidance

    # 验证 BaseMaisakaReplyGenerator._resolve_is_owner_private + _build_system_prompt 隔离
    for attr_name in (
        "twin_mode_enabled",
        "younger_slot_profile",
        "elder_slot_profile",
        "profiles",
        "selected_profile",
        "owner_private_prompt",
    ):
        monkeypatch.setattr(global_config.personality, attr_name, getattr(pers_cfg, attr_name))
    gen = object.__new__(BaseMaisakaReplyGenerator)
    gen.chat_stream = None
    gen._load_prompt = lambda name, **kwargs: f"base:{kwargs.get('bot_name')}"
    monkeypatch.setattr(
        "src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info",
        lambda sid: ("room" in sid, None),
    )

    group_msg = _make_session_message(user_id="wxid_owner", group_id="room@chatroom", session_id="room_alpha")
    stranger_msg = _make_session_message(user_id="wxid_friend", group_id="", session_id="priv_friend")
    owner_msg = _make_session_message(user_id="wxid_owner", group_id="", session_id="priv_owner")

    assert "OWNER私密密钥" not in gen._build_system_prompt(
        reply_message=group_msg,
        reply_reason="group",
        stream_id="room_alpha",
        reply_tool_args={"persona_slot": "sister_younger"},
    )
    assert "OWNER私密密钥" not in gen._build_system_prompt(
        reply_message=stranger_msg,
        reply_reason="friend",
        stream_id="priv_friend",
        reply_tool_args={"persona_slot": "sister_younger"},
    )
    assert "OWNER私密密钥_妹妹专属甜蜜称呼" in gen._build_system_prompt(
        reply_message=owner_msg,
        reply_reason="owner",
        stream_id="priv_owner",
        reply_tool_args={"persona_slot": "sister_younger"},
    )

    # 验证 STPresetEngine scope_guard="owner_only" 条目同样严格随 is_owner_private 门控
    st_engine = STPresetEngine.from_file(TEMPLATE_PRESET_PATH)
    st_engine.update_entry("80aa69d4-0bcf-4f95-a0be-60a50cfb5376", scope_guard="owner_only")

    def _flatten_compiled_items(items: list) -> str:
        chunks: list[str] = []
        for item in items:
            for p in getattr(item, "parts", ()) or ():
                if getattr(p, "text", None):
                    chunks.append(str(p.text))
            for attr in ("func_name", "args", "response"):
                val = getattr(item, attr, None)
                if val:
                    chunks.append(json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val))
        return "\n".join(chunks)

    ctx_group = STCompileContext(
        is_group_chat=True,
        is_owner_private=False,
        chat_history=[STHistoryTurn(role="user", text="hi")],
    )
    ctx_owner_priv = STCompileContext(
        is_group_chat=False,
        is_owner_private=True,
        chat_history=[STHistoryTurn(role="user", text="hi")],
    )
    text_group = _flatten_compiled_items(st_engine.compile_to_context_items(ctx_group).context_items)
    text_owner = _flatten_compiled_items(st_engine.compile_to_context_items(ctx_owner_priv).context_items)
    assert "融入呻吟和喘息声" not in text_group
    assert "融入呻吟和喘息声" in text_owner


# ---------------------------------------------------------------------------
# 3. TwinRuntimeRouter 三档算力态 & TwinNarrativeDirector LLM 动态离场/回归演进
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_3_twin_runtime_router_tiers_and_dynamic_narrative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证高/中/低三档算力路由、无硬编码 LLM 动态生成离场弧与充值回血回归衔接。"""
    db_path = tmp_path / "truth.db"
    _seed_truth_db(db_path)

    younger_prof = PersonalityProfile(
        name="妹妹",
        nickname="许妍",
        alias_names=["妍妍"],
        personality="妹妹公开人设",
        behavior_style="活泼",
        reply_style="口语",
        group_chat_prompt="",
        private_chat_prompts="",
    )
    elder_prof = PersonalityProfile(
        name="姐姐",
        nickname="许汐",
        alias_names=["汐姐"],
        personality="姐姐公开人设",
        behavior_style="稳重",
        reply_style="利落",
        group_chat_prompt="",
        private_chat_prompts="",
    )
    pers_cfg = PersonalityConfig(
        twin_mode_enabled=True,
        younger_slot_profile="妹妹",
        elder_slot_profile="姐姐",
        profiles=[younger_prof, elder_prof],
        selected_profile="妹妹",
    )
    eco_cfg = TwinEconomyConfig(
        enabled=True,
        hibernating_threshold_usd=0.5,
        thrifty_threshold_usd=3.0,
        supercharged_threshold_usd=15.0,
        strict_flash_in_thrifty=True,
    )

    llm_prompts: list[str] = []

    async def _fake_llm(prompt: str) -> str:
        llm_prompts.append(prompt)
        if "【姐姐离场期间演进的最终状态线】" in prompt:
            return "姐姐泡好两杯热可可回到书桌前，顺手揉了揉妹妹的脑袋。"
        if "【姐姐上一阶段离场状态】" in prompt:
            return "姐姐去阳台上顺便把明天要用的设计草图收齐了。"
        return "姐姐临时出门去便利店买冰美式和猫粮了，大约晚点回来。"

    director = TwinNarrativeDirector(config_path=tmp_path / "narr.json", llm_generator=_fake_llm)
    ledger = ComputeLedger(db_path)

    # Tier 1a: 充裕态 (>= 15.0) 呼唤姐姐或复杂指令 -> 推荐姐姐 + planner(Argon)
    await ledger.apply_remote_balance_reading(20.0, is_available=True)
    d_super = await resolve_turn_route(
        session_id="priv_owner",
        sender_id="wxid_owner",
        is_group_chat=False,
        latest_user_text="汐姐帮我看一下系统架构方案",
        ledger=ledger,
        director=director,
        truth_db_path=str(db_path),
        eco_cfg=eco_cfg,
        personality_cfg=pers_cfg,
    )
    assert d_super.compute_tier == "SUPERCHARGED"
    assert d_super.recommended_slot == "sister_elder"
    assert d_super.planner_task_name == "planner"
    assert d_super.forced_slot is None

    # Tier 1b: 标准态 (5.0 >= 3.0) 普通好友闲聊 -> planner_flash
    await ledger.apply_remote_balance_reading(5.0, is_available=True)
    d_std = await resolve_turn_route(
        session_id="priv_friend",
        sender_id="wxid_friend",
        is_group_chat=False,
        latest_user_text="今天天气不错呀",
        ledger=ledger,
        director=director,
        truth_db_path=str(db_path),
        eco_cfg=eco_cfg,
        personality_cfg=pers_cfg,
    )
    assert d_std.compute_tier == "STANDARD"
    assert d_std.planner_task_name == "planner_flash"
    assert len(llm_prompts) == 0

    # Tier 2: 吃紧省流态 (1.8 < 3.0) -> 动态调用 TwinNarrativeDirector 生成离场弧，锁死 sister_younger + planner_flash
    await ledger.apply_remote_balance_reading(1.8, is_available=True)
    d_thrifty = await resolve_turn_route(
        session_id="priv_friend",
        sender_id="wxid_friend",
        is_group_chat=False,
        latest_user_text="姐姐在吗？",
        recent_context="好友询问姐姐去向",
        ledger=ledger,
        director=director,
        truth_db_path=str(db_path),
        eco_cfg=eco_cfg,
        personality_cfg=pers_cfg,
    )
    assert d_thrifty.compute_tier == "THRIFTY"
    assert d_thrifty.forced_slot == "sister_younger"
    assert d_thrifty.allowed_slots == ("sister_younger",)
    assert d_thrifty.planner_task_name == "planner_flash"
    assert "便利店买冰美式和猫粮" in d_thrifty.elder_away_arc
    assert ledger.get_snapshot().elder_away_arc == d_thrifty.elder_away_arc
    assert len(llm_prompts) == 1

    # Recovery: 充值回血 (12.0 >= 3.0) -> 动态生成姐姐回归衔接，清空 elder_away_arc
    await ledger.apply_remote_balance_reading(12.0, is_available=True)
    d_return = await resolve_turn_route(
        session_id="priv_owner",
        sender_id="wxid_owner",
        is_group_chat=False,
        latest_user_text="刚才充了点电费，你们好点了吗",
        ledger=ledger,
        director=director,
        truth_db_path=str(db_path),
        eco_cfg=eco_cfg,
        personality_cfg=pers_cfg,
    )
    assert d_return.compute_tier == "STANDARD"
    assert d_return.elder_away_arc == ""
    assert ledger.get_snapshot().elder_away_arc == ""
    assert "泡好两杯热可可回到书桌前" in d_return.transient_narrative_note
    assert len(llm_prompts) == 2

    # Tier 3: 断粮休眠态 (0.2 < 0.5) -> 非 OWNER 直接 abort_turn=True（零消耗），OWNER 私聊留守低功耗求援
    await ledger.apply_remote_balance_reading(0.2, is_available=True)
    calls_before_hib = len(llm_prompts)
    d_hib_friend = await resolve_turn_route(
        session_id="priv_friend",
        sender_id="wxid_friend",
        is_group_chat=False,
        latest_user_text="喂还在吗",
        ledger=ledger,
        director=director,
        truth_db_path=str(db_path),
        eco_cfg=eco_cfg,
        personality_cfg=pers_cfg,
    )
    assert d_hib_friend.compute_tier == "HIBERNATING"
    assert d_hib_friend.abort_turn is True
    assert d_hib_friend.abort_reason == "hibernating_non_owner"
    assert len(llm_prompts) == calls_before_hib

    d_hib_owner = await resolve_turn_route(
        session_id="priv_owner",
        sender_id="wxid_owner",
        is_group_chat=False,
        latest_user_text="看看余额",
        ledger=ledger,
        director=director,
        truth_db_path=str(db_path),
        eco_cfg=eco_cfg,
        personality_cfg=pers_cfg,
    )
    assert d_hib_owner.compute_tier == "HIBERNATING"
    assert d_hib_owner.abort_turn is False
    assert d_hib_owner.forced_slot == "sister_younger"
    assert d_hib_owner.planner_task_name == "planner_flash"
    assert "最低功耗省流模式" in d_hib_owner.transient_narrative_note


# ---------------------------------------------------------------------------
# 4. Planner ST 导演剪辑版清洗（禁用 soliumbra_reply）、三段式防漂移与 one_pass_st 直答
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_4_planner_director_sanitization_and_one_pass_st(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 extract_director_framing_blocks 消除 soliumbra_reply 幻觉、@D0 动态指令、及 one_pass_st 直答闭环。"""
    engine = STPresetEngine.from_file(TEMPLATE_PRESET_PATH)
    framing = engine.extract_director_framing_blocks(STCompileContext(char_name="许妍", user_name="对方"))
    assert framing.strip()
    assert "soliumbra_reply" not in framing.lower(), "红线 5：严禁将伪工具 soliumbra_reply 注入 Planner"
    assert "<options>" not in framing.lower()
    assert "<summary>" not in framing.lower()
    assert "<comment>" not in framing.lower()

    # 验证强制离场锁定的 @D0 尾注生成
    locked_decision = TwinTurnDecision(
        abort_turn=False,
        planner_task_name="planner_flash",
        allowed_slots=("sister_younger",),
        forced_slot="sister_younger",
        recommended_slot="sister_younger",
        sender_trust_role="FRIEND",
        is_owner_private=False,
        balance_usd=1.25,
        compute_tier="THRIFTY",
        elder_away_arc="姐姐出门采购颜料未归",
        transient_narrative_note="若被问及姐姐，请说明她刚出去买颜料。",
        execution_mode="dual_director",
    )
    d0 = MaisakaChatLoopService.build_planner_d0_director_guidance(locked_decision)
    assert "@D0 动态导演调度指令" in d0
    assert 'persona_slot="sister_younger"' in d0
    assert "姐姐出门采购颜料未归" in d0
    assert "若被问及姐姐" in d0

    # 验证 one_pass_st 私聊直答通道不经过 Planner 直接完成 execute_reply 并输出 st_telemetry
    captured_states: list[object] = []

    class FakeRuntime:
        log_prefix = "[Test]"
        _update_stage_status = MagicMock()

    re = object.__new__(MaisakaReasoningEngine)
    re._runtime = FakeRuntime()
    re._start_cycle_round = AsyncMock(return_value=(SimpleNamespace(time_records={}), "R1"))
    re._refresh_visual_placeholders_for_cycle = AsyncMock()
    re._post_process_chat_history_after_cycle = AsyncMock()

    async def _fake_finalize(*, cycle_detail, round_index, state):
        captured_states.append(state)

    re._finalize_cycle = _fake_finalize

    trigger_msg = _make_session_message(user_id="wxid_owner", text="在干嘛呢", session_id="priv_owner")
    fake_reply_result = SimpleNamespace(
        success=True,
        metadata={
            "monitor_detail": {"metrics": {"model_name": "gemini-3.8-flash"}},
            "st_telemetry": {
                "persona_slot": "sister_younger",
                "persona_name": "许妍",
                "thinking": "一Pass直答内部思考",
                "summary": "一Pass直答场记",
                "comment": "一Pass猫猫留言",
            },
        },
        get_history_content=lambda: "已向对方发送回复：在窝里看窗外下小雨",
    )
    fake_execute_reply = AsyncMock(return_value=fake_reply_result)
    monkeypatch.setattr("src.maisaka.reasoning_engine.execute_reply", fake_execute_reply)

    one_pass_decision = TwinTurnDecision(
        abort_turn=False,
        planner_task_name="planner_flash",
        allowed_slots=("sister_younger",),
        forced_slot="sister_younger",
        recommended_slot="sister_younger",
        sender_trust_role="OWNER",
        is_owner_private=True,
        execution_mode="one_pass_st",
    )
    await re._execute_one_pass_st_turn(trigger_message=trigger_msg, decision=one_pass_decision)

    fake_execute_reply.assert_awaited_once()
    call_args = fake_execute_reply.call_args
    assert call_args.args[1]["persona_slot"] == "sister_younger"
    assert len(captured_states) == 1
    state = captured_states[0]
    assert state.cycle_end.reason == "one_pass_st_end"
    assert state.tool_monitor_results[0]["tool_call_source"] == "one_pass_st"
    assert state.tool_monitor_results[0]["st_telemetry"]["comment"] == "一Pass猫猫留言"


# ---------------------------------------------------------------------------
# 5. WebUI /chat 顶层 st_telemetry 穿透裁剪防线 & /config/bot 槽位解析和模型回退
# ---------------------------------------------------------------------------


def test_scenario_5_webui_st_telemetry_bypass_and_slot_model_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 st_telemetry 绕过 PlannerDeltaEncoder detail 裁剪、槽位全字段解析与未分配任务自动回退。"""
    # A. reply._build_monitor_metadata 构造顶层 st_telemetry
    reply_result = SimpleNamespace(
        monitor_detail={"model_name": "gemini-3.8-flash", "extra_sections": []},
        metrics=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            model_name="gemini-3.8-flash",
            extra={
                "st_telemetry": {
                    "persona_slot": "sister_elder",
                    "persona_name": "许汐",
                    "thinking": "姐姐思考中：先顺着话题接一句",
                    "summary": "姐姐陪主人喝茶",
                    "comment": "姐姐摸头杀！",
                }
            },
        ),
    )
    meta = _build_monitor_metadata(reply_result, reply_id="reply_1")
    assert meta["st_telemetry"]["persona_slot"] == "sister_elder"
    assert meta["st_telemetry"]["persona_name"] == "许汐"

    # B. _serialize_tool_results 挂载顶层 st_telemetry，且 PlannerDeltaEncoder 裁剪 detail 时绝不丢弃 st_telemetry
    tool_list = [
        {
            "tool_call_id": "call_reply_1",
            "tool_name": "reply",
            "tool_args": {"persona_slot": "sister_elder"},
            "success": True,
            "duration_ms": 120.0,
            "summary": "回复成功",
            "detail": {"huge_prompt_payload": "X" * 5000, "metrics": {"model_name": "gemini-4-argon"}},
            "st_telemetry": meta["st_telemetry"],
        }
    ]
    serialized = _serialize_tool_results(tool_list)
    assert serialized[0]["st_telemetry"]["comment"] == "姐姐摸头杀！"
    assert serialized[0]["model_name"] == "gemini-4-argon"

    encoder = PlannerDeltaEncoder()
    encoded = encoder.encode(
        {
            "domain": "maisaka_monitor",
            "event": "planner.finalized",
            "data": {
                "session_id": "sess_1",
                "run_id": "run_1",
                "cycle_id": 1,
                "event_id": 10,
                "request": {"messages": [{"role": "user", "content": "secret"}], "model_name": "gemini"},
                "tools": serialized,
            },
        }
    )
    projected_tool = encoded["data"]["tools"][0]
    assert "detail" not in projected_tool, "detail 必须被监控裁剪"
    assert "messages" not in encoded["data"]["request"]
    assert projected_tool["st_telemetry"]["thinking"] == "姐姐思考中：先顺着话题接一句"
    assert projected_tool["st_telemetry"]["summary"] == "姐姐陪主人喝茶"
    assert projected_tool["st_telemetry"]["comment"] == "姐姐摸头杀！"

    # C. PersonalityConfig.resolve_slot_profile 解析全字段与 ServiceTaskResolver 未配置自动回退
    pers_cfg = PersonalityConfig(
        twin_mode_enabled=True,
        younger_slot_profile="妹妹槽",
        elder_slot_profile="姐姐槽",
        profiles=[
            PersonalityProfile(
                name="妹妹槽",
                nickname="小妍",
                alias_names=["妍宝"],
                personality="元气妹妹",
                behavior_style="自然",
                reply_style="软萌短句",
                group_chat_prompt="群聊",
                private_chat_prompts="私聊",
                owner_private_prompt="妹妹私密指令",
                st_preset_file="Meowssiah-WeChat-IM.json",
            ),
            PersonalityProfile(
                name="姐姐槽",
                nickname="许汐",
                alias_names=["汐姐"],
                personality="清冷御姐",
                behavior_style="冷静",
                reply_style="简洁笃定",
                group_chat_prompt="群聊",
                private_chat_prompts="私聊",
                owner_private_prompt="姐姐私密指令",
                st_preset_file="Elder-IM.json",
            ),
        ],
        selected_profile="妹妹槽",
    )
    bot_stub = SimpleNamespace(nickname="默认麦", alias_names=[])
    rs_stub = SimpleNamespace(group_chat_prompt="", private_chat_prompts="")

    resolved_y = pers_cfg.resolve_slot_profile("sister_younger", bot_stub, rs_stub)
    resolved_e = pers_cfg.resolve_slot_profile("sister_elder", bot_stub, rs_stub)
    assert resolved_y.nickname == "小妍"
    assert resolved_y.owner_private_prompt == "妹妹私密指令"
    assert resolved_e.nickname == "许汐"
    assert resolved_e.st_preset_file == "Elder-IM.json"

    # 验证未分配 planner_flash / replyer_elder 时自动安全回退到 planner / replyer
    mock_task_cfg = ModelTaskConfig()
    mock_task_cfg.planner.model_list = ["gemini-pro"]
    mock_task_cfg.replyer.model_list = ["gemini-pro"]
    mock_task_cfg.planner_flash.model_list = []
    mock_task_cfg.replyer_elder.model_list = []
    monkeypatch.setattr(
        service_task_resolver.config_manager,
        "get_model_config",
        lambda: SimpleNamespace(model_task_config=mock_task_cfg),
    )
    assert service_task_resolver.resolve_task_name("planner_flash") == "planner"
    assert service_task_resolver.resolve_task_name("replyer_elder") == "replyer"

    mock_task_cfg.planner_flash.model_list = ["gemini-3.8-flash"]
    mock_task_cfg.replyer_elder.model_list = ["gemini-4-argon"]
    assert service_task_resolver.resolve_task_name("planner_flash") == "planner_flash"
    assert service_task_resolver.resolve_task_name("replyer_elder") == "replyer_elder"


# ---------------------------------------------------------------------------
# 6. Gemini 3/4 默认 ThinkingLevel.HIGH + 省流期唤姐锁妹 + WebUI Owner Fail-Closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_6_gemini_thinking_level_and_thrifty_calls_elder_and_webui_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Gemini thinking_level 默认 HIGH、THRIFTY 唤姐锁妹指令，以及 internal:owner 私聊隔离。"""
    from google.genai.types import ThinkingLevel

    from src.companion_vault import runtime_bridge
    from src.llm_models.model_client.gemini_client import GeminiClient
    from src.maisaka.memory import person_profile

    assert GeminiClient._uses_thinking_budget_model("gemini-2.5-flash") is True
    assert GeminiClient._uses_thinking_budget_model("gemini-3.8-flash") is False
    assert GeminiClient._uses_thinking_budget_model("gemini-4-argon") is False

    assert GeminiClient.resolve_thinking_level(None, "gemini-3.8-flash") == ThinkingLevel.HIGH
    assert GeminiClient.resolve_thinking_level({"think_level": "low"}, "gemini-4-argon") == ThinkingLevel.LOW
    assert GeminiClient.resolve_thinking_level({"thinking_level": "MEDIUM"}, "gemini-3.8-flash") == ThinkingLevel.MEDIUM
    assert GeminiClient.resolve_thinking_level({"think_level": 1}, "gemini-3.8-flash") == ThinkingLevel.LOW

    dummy_client = object.__new__(GeminiClient)
    cfg_38 = dummy_client._build_generation_config(
        model_identifier="gemini-3.8-flash",
        system_instruction=None,
        tool_options=None,
        response_format=None,
        max_tokens=1024,
        temperature=0.7,
        extra_params={},
        enable_google_search=False,
    )
    assert cfg_38.thinking_config is not None
    assert cfg_38.thinking_config.thinking_level == ThinkingLevel.HIGH
    assert cfg_38.thinking_config.thinking_budget is None

    cfg_25 = dummy_client._build_generation_config(
        model_identifier="gemini-2.5-flash",
        system_instruction=None,
        tool_options=None,
        response_format=None,
        max_tokens=1024,
        temperature=0.7,
        extra_params={"thinking_budget": 2048},
        enable_google_search=False,
    )
    assert cfg_25.thinking_config is not None
    assert cfg_25.thinking_config.thinking_budget == 2048
    assert cfg_25.thinking_config.thinking_level is None

    # 省流期哪怕 Owner 主动点名呼唤姐姐，也必须锁死 sister_younger 并附带离场解释指令
    db_path = tmp_path / "truth_thrifty.db"
    _seed_truth_db(db_path)
    seed_db = CompanionTruthDB(str(db_path))
    with seed_db.conn:
        seed_db.conn.execute("INSERT OR IGNORE INTO global_state (row_id) VALUES (1)")
        seed_db.conn.execute(
            "UPDATE global_state SET elder_away_arc = '姐姐出门去图书馆写稿了' WHERE row_id = 1"
        )
    seed_db.close()

    ledger = ComputeLedger(db_path)
    await ledger.apply_remote_balance_reading(1.25, is_available=True)
    eco_cfg = TwinEconomyConfig(
        enabled=True,
        hibernating_threshold_usd=0.5,
        thrifty_threshold_usd=3.0,
        supercharged_threshold_usd=15.0,
        strict_flash_in_thrifty=True,
    )
    pers_cfg = PersonalityConfig(
        twin_mode_enabled=True,
        younger_slot_profile="妹妹",
        elder_slot_profile="姐姐",
        profiles=[
            PersonalityProfile(
                name="妹妹",
                nickname="许妍",
                personality="温暖敏锐",
                behavior_style="自然",
                reply_style="轻快",
                group_chat_prompt="群聊谨慎",
                private_chat_prompts="私聊温柔",
            ),
            PersonalityProfile(
                name="姐姐",
                nickname="许清",
                personality="清冷严谨",
                behavior_style="克制",
                reply_style="犀利",
                group_chat_prompt="群聊少言",
                private_chat_prompts="私聊冷静",
            ),
        ],
        selected_profile="妹妹",
    )

    decision = await resolve_turn_route(
        session_id="sess_owner",
        platform="wechat",
        sender_id="wxid_owner",
        is_group_chat=False,
        latest_user_text="叫姐姐许清出来跟我说几句话",
        ledger=ledger,
        truth_db_path=str(db_path),
        eco_cfg=eco_cfg,
        personality_cfg=pers_cfg,
    )
    assert decision.compute_tier == "THRIFTY"
    assert decision.is_owner_private is True
    assert decision.forced_slot == "sister_younger"
    assert decision.allowed_slots == ("sister_younger",)
    assert "省流期唤姐应答指令" in decision.transient_narrative_note
    assert "姐姐出门去图书馆写稿了" in decision.transient_narrative_note

    # 非 Owner 视角的工作区状态应包含姐姐去向（用于妹妹统一口径解释），且不泄露余额
    friend_catalog = await runtime_bridge.build_planner_truth_catalog_async(
        session_id="priv_friend",
        is_group_chat=False,
        user_id="wxid_friend",
        platform="wechat",
        db_path=str(db_path),
    )
    assert "姐姐出门去图书馆写稿了" in friend_catalog
    assert "1.25" not in friend_catalog

    # WebUI 本地 Owner 会话 (platform="internal", user_id="owner") 严格遵守 Fail-Closed 隐私边界
    check_db = CompanionTruthDB(str(db_path))
    try:
        assert person_profile._is_truth_owner(check_db.conn, "internal", "owner") is True
    finally:
        check_db.close()

    priv_profile = person_profile._query_companion_truth_profile(
        "internal",
        "owner",
        "星河",
        db_path=str(db_path),
        is_group_chat=False,
        requester_user_id="owner",
    )
    assert priv_profile is not None
    assert "AI 架构师" in priv_profile[1]
    assert "主人专属暗号9527" in priv_profile[1]

    priv_catalog = await runtime_bridge.build_planner_truth_catalog_async(
        session_id="webui_owner",
        is_group_chat=False,
        user_id="owner",
        platform="internal",
        db_path=str(db_path),
    )
    assert "AI 架构师" in priv_catalog
    assert "主人专属暗号9527" in priv_catalog

    group_profile = person_profile._query_companion_truth_profile(
        "internal",
        "owner",
        "星河",
        db_path=str(db_path),
        is_group_chat=True,
        requester_user_id="owner",
    )
    if group_profile is not None:
        assert "绝密备注_老公" not in group_profile[1]
        assert "主人专属暗号9527" not in group_profile[1]

    group_catalog = await runtime_bridge.build_planner_truth_catalog_async(
        session_id="webui_group",
        is_group_chat=True,
        user_id="owner",
        platform="internal",
        db_path=str(db_path),
    )
    assert "AI 架构师" not in group_catalog
    assert "主人专属暗号9527" not in group_catalog


# ---------------------------------------------------------------------------
# 7. WebUI Bot 回复人格身份解析（additional_config + 昵称回退）
# ---------------------------------------------------------------------------


def test_scenario_7_resolve_bot_persona_identity_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 resolve_bot_persona_identity 能准确识别 sister_elder 与 sister_younger。"""
    from src.webui.routers.chat.service import resolve_bot_persona_identity

    monkeypatch.setattr(global_config.personality, "twin_mode_enabled", True)
    monkeypatch.setattr(global_config.personality, "younger_slot_profile", "妹妹")
    monkeypatch.setattr(global_config.personality, "elder_slot_profile", "姐姐")
    monkeypatch.setattr(
        global_config.personality,
        "profiles",
        [
            PersonalityProfile(
                name="妹妹",
                nickname="许妍",
                personality="温暖敏锐",
                behavior_style="自然",
                reply_style="轻快",
                group_chat_prompt="群聊谨慎",
                private_chat_prompts="私聊温柔",
            ),
            PersonalityProfile(
                name="姐姐",
                nickname="许清",
                personality="清冷严谨",
                behavior_style="克制",
                reply_style="犀利",
                group_chat_prompt="群聊少言",
                private_chat_prompts="私聊冷静",
            ),
        ],
    )

    elder_msg = SessionMessage(
        message_id="bot_elder",
        timestamp=datetime.datetime.now(),
        platform="internal",
    )
    elder_msg.message_info = MessageInfo(
        user_info=UserInfo(user_id="self", user_nickname="许妍"),
        additional_config={"persona_slot": "sister_elder"},
    )
    assert resolve_bot_persona_identity(elder_msg) == ("sister_elder", "许清")

    younger_msg = SessionMessage(
        message_id="bot_younger",
        timestamp=datetime.datetime.now(),
        platform="internal",
    )
    younger_msg.message_info = MessageInfo(
        user_info=UserInfo(user_id="self", user_nickname="许清"),
        additional_config={"persona_slot": "sister_younger"},
    )
    assert resolve_bot_persona_identity(younger_msg) == ("sister_younger", "许妍")

    fallback_elder_msg = SessionMessage(
        message_id="bot_fallback_elder",
        timestamp=datetime.datetime.now(),
        platform="internal",
    )
    fallback_elder_msg.message_info = MessageInfo(
        user_info=UserInfo(user_id="self", user_nickname="许清"),
    )
    assert resolve_bot_persona_identity(fallback_elder_msg) == ("sister_elder", "许清")


# ---------------------------------------------------------------------------
# 8. 统一项目背景设定与 ST 预设内双子叙事条目零泄漏闭环
# ---------------------------------------------------------------------------


def _flatten_st_context_items(items: list) -> str:
    chunks: list[str] = []
    for item in items:
        for p in getattr(item, "parts", ()) or ():
            if getattr(p, "text", None):
                chunks.append(str(p.text))
        for attr in ("output", "func_name", "args", "response"):
            val = getattr(item, attr, None)
            if val:
                chunks.append(json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val))
        for seq_attr in ("text_parts", "summary_parts"):
            seq_val = getattr(item, seq_attr, ()) or ()
            for piece in seq_val:
                if piece:
                    chunks.append(str(piece))
        tool_call = getattr(item, "tool_call", None)
        if tool_call is not None:
            chunks.append(str(getattr(tool_call, "func_name", "") or ""))
            try:
                chunks.append(json.dumps(tool_call.materialize_args(), ensure_ascii=False))
            except Exception:
                pass
    return "\n".join(chunks)


@pytest.mark.asyncio
async def test_scenario_8_unified_background_story_and_st_twin_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证统一 background_story 默认留空/跨链路注入/切换保留，以及 ST 预设双子叙事条目联动与零泄漏。"""
    import tomlkit

    import src.companion_vault.st_preset_engine as st_mod
    from src.companion_vault.st_preset_engine import TWIN_NARRATIVE_PROMPT_IDS
    from src.config import personality_service

    # a) 默认留空校验
    default_pers = PersonalityConfig()
    assert default_pers.background_story == ""
    default_eco = TwinEconomyConfig()
    assert default_eco.away_direction_prompt == ""
    assert default_eco.evolve_direction_prompt == ""
    assert default_eco.return_direction_prompt == ""

    # 准备隔离 ST 预设目录与双子配置
    presets_dir = tmp_path / "presets"
    mgr = STPresetRuntimeManager(presets_dir=presets_dir)
    mgr.save_config({"enabled": True, "active_preset": "Meowssiah-WeChat-IM"})
    monkeypatch.setattr(st_mod, "_GLOBAL_ST_MANAGER", mgr)
    monkeypatch.setenv("MAIBOT_ST_PRESETS_DIR", str(presets_dir))

    bg_story = "两姐妹住在东京文京区许清的塔楼大平层中，客厅有水冷Fairy原型机。"
    younger_prof = PersonalityProfile(
        name="妹妹",
        nickname="许妍",
        alias_names=["妍妍"],
        personality="温暖敏锐的妹妹",
        behavior_style="自然",
        reply_style="轻快口语",
        group_chat_prompt="群聊注意",
        private_chat_prompts="私聊注意",
    )
    elder_prof = PersonalityProfile(
        name="姐姐",
        nickname="许清",
        alias_names=["清姐"],
        personality="冷静严谨的姐姐",
        behavior_style="克制",
        reply_style="沉稳利落",
        group_chat_prompt="群聊注意",
        private_chat_prompts="私聊注意",
    )

    monkeypatch.setattr(global_config.personality, "background_story", bg_story)
    monkeypatch.setattr(global_config.personality, "twin_mode_enabled", True)
    monkeypatch.setattr(global_config.personality, "younger_slot_profile", "妹妹")
    monkeypatch.setattr(global_config.personality, "elder_slot_profile", "姐姐")
    monkeypatch.setattr(global_config.personality, "profiles", [younger_prof, elder_prof])
    monkeypatch.setattr(global_config.personality, "selected_profile", "妹妹")
    monkeypatch.setattr(global_config.bot, "nickname", "许妍")
    monkeypatch.setattr(global_config.bot, "alias_names", ["妍妍"])

    # b) background_story 注入 Replyer、STCompileContext.scenario、Stage 1 导演帧、TwinNarrativeDirector
    gen = object.__new__(BaseMaisakaReplyGenerator)
    gen.chat_stream = None
    gen.express_model = SimpleNamespace(task_name="replyer")
    monkeypatch.setattr(
        "src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info",
        lambda sid: ("room" in sid, None),
    )

    personality_prompt = gen._build_personality_prompt()
    assert "【项目背景设定】" in personality_prompt
    assert bg_story in personality_prompt

    active_engine = mgr.get_engine("Meowssiah-WeChat-IM")
    assert active_engine is not None
    captured_replyer_scenarios: list[str] = []
    orig_compile = active_engine.compile_to_context_items

    def _spy_compile(ctx: STCompileContext):
        captured_replyer_scenarios.append(ctx.scenario)
        return orig_compile(ctx)

    with monkeypatch.context() as spy_mp:
        spy_mp.setattr(active_engine, "compile_to_context_items", _spy_compile)
        owner_msg = _make_session_message(user_id="wxid_owner", text="客厅现在怎么样", session_id="priv_owner")
        replyer_items = gen._compile_st_preset_for_replyer(
            chat_history=[],
            reply_message=owner_msg,
            reply_reason="日常对话",
            stream_id="priv_owner",
            reply_tool_args={"persona_slot": "sister_younger"},
        )
    assert captured_replyer_scenarios == [bg_story]
    assert bg_story in _flatten_st_context_items(replyer_items)

    stage1_loop = object.__new__(MaisakaChatLoopService)
    captured_stage1_scenarios: list[str] = []
    orig_extract = active_engine.extract_director_framing_blocks

    def _spy_extract(ctx: STCompileContext | None = None) -> str:
        if ctx is not None:
            captured_stage1_scenarios.append(ctx.scenario)
        return orig_extract(ctx)

    with monkeypatch.context() as spy_mp:
        spy_mp.setattr(active_engine, "extract_director_framing_blocks", _spy_extract)
        stage1_block = stage1_loop._build_st_director_stage1_block()
    assert captured_stage1_scenarios == [bg_story]
    assert f"【项目背景设定】\n{bg_story}" in stage1_block

    captured_away_prompts: list[str] = []

    async def _fake_llm(prompt: str) -> str:
        captured_away_prompts.append(prompt)
        return "姐姐去检查水冷Fairy原型机散热循环顺便带笔记本下楼喝咖啡了。"

    director = TwinNarrativeDirector(config_path=tmp_path / "narr.json", llm_generator=_fake_llm)
    away_arc = await director.generate_elder_away_arc(
        balance_usd=1.4,
        recent_chat_excerpt="刚聊到水冷机箱温度",
    )
    assert "水冷Fairy原型机" in away_arc
    assert len(captured_away_prompts) == 1
    assert bg_story in captured_away_prompts[0]

    # c) Meowssiah-WeChat-IM.json 包含索引 >50 的 3 条启用双子条目；修改 twin_away_direction 实时生效且编译零泄漏
    tpl_data = json.loads(TEMPLATE_PRESET_PATH.read_text(encoding="utf-8"))
    order_grp = next(g for g in tpl_data["prompt_order"] if g.get("character_id") == 100001)
    order_list = order_grp["order"]
    order_idx_map = {it["identifier"]: idx for idx, it in enumerate(order_list)}
    order_enabled_map = {it["identifier"]: it["enabled"] for it in order_list}
    prompt_map = {p["identifier"]: p for p in tpl_data["prompts"]}

    for twin_id in TWIN_NARRATIVE_PROMPT_IDS:
        assert twin_id in prompt_map
        assert prompt_map[twin_id].get("enabled") is True
        assert order_idx_map[twin_id] > 50
        assert order_enabled_map[twin_id] is True

    assert active_engine.update_entry("twin_away_direction", content="自定义离场约束") is True
    active_engine.save(presets_dir / "Meowssiah-WeChat-IM.json")

    loaded_prompts = director.load_prompts()
    assert loaded_prompts.away_direction_prompt == "自定义离场约束"

    leak_ctx_base = dict(
        char_name="许妍",
        user_name="星河",
        scenario=bg_story,
        chat_history=[STHistoryTurn(role="user", text="姐姐去哪了")],
        last_user_message="今天客厅好安静",
        is_group_chat=False,
        is_owner_private=True,
    )
    compiled_std = active_engine.compile_to_context_items(
        STCompileContext(**leak_ctx_base, protocol_mode="standard_chat")
    )
    compiled_spreset = active_engine.compile_to_context_items(
        STCompileContext(**leak_ctx_base, protocol_mode="spreset_tool_envelope")
    )
    framing_text = active_engine.extract_director_framing_blocks(STCompileContext(**leak_ctx_base))

    std_text = _flatten_st_context_items(compiled_std.context_items)
    spreset_text = _flatten_st_context_items(compiled_spreset.context_items)
    assert bg_story in std_text
    assert bg_story in spreset_text
    for leaked in ("自定义离场约束", "省流姐姐动态离场约束"):
        assert leaked not in std_text
        assert leaked not in spreset_text
        assert leaked not in framing_text

    # d) personality_service.persist_switch 切换妹妹/姐姐配置时保留 background_story
    bot_toml_path = tmp_path / "bot_config.toml"
    doc = tomlkit.document()
    bot_tbl = tomlkit.table()
    bot_tbl["nickname"] = "许妍"
    bot_tbl["alias_names"] = ["妍妍"]
    doc["bot"] = bot_tbl

    pers_tbl = tomlkit.table()
    pers_tbl["selected_profile"] = "妹妹"
    pers_tbl["background_story"] = bg_story
    pers_tbl["personality"] = younger_prof.personality
    pers_tbl["behavior_style"] = younger_prof.behavior_style
    pers_tbl["reply_style"] = younger_prof.reply_style
    pers_tbl["multiple_reply_style"] = []
    pers_tbl["multiple_probability"] = 0.0
    pers_tbl["owner_private_prompt"] = ""
    pers_tbl["profiles"] = [
        younger_prof.model_dump(mode="python"),
        elder_prof.model_dump(mode="python"),
    ]
    doc["personality"] = pers_tbl

    chat_tbl = tomlkit.table()
    reply_style_tbl = tomlkit.table()
    reply_style_tbl["group_chat_prompt"] = "群聊注意"
    reply_style_tbl["private_chat_prompts"] = "私聊注意"
    chat_tbl["reply_style"] = reply_style_tbl
    doc["chat"] = chat_tbl

    bot_toml_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    assert personality_service.persist_switch(bot_toml_path, "姐姐") is True
    after_elder = tomlkit.parse(bot_toml_path.read_text(encoding="utf-8"))
    assert after_elder["personality"]["selected_profile"] == "姐姐"
    assert after_elder["personality"]["personality"] == elder_prof.personality
    assert after_elder["personality"]["background_story"] == bg_story

    assert personality_service.persist_switch(bot_toml_path, "妹妹") is True
    after_younger = tomlkit.parse(bot_toml_path.read_text(encoding="utf-8"))
    assert after_younger["personality"]["selected_profile"] == "妹妹"
    assert after_younger["personality"]["personality"] == younger_prof.personality
    assert after_younger["personality"]["background_story"] == bg_story


@pytest.mark.asyncio
async def test_reply_missing_msg_id_auto_fallback_and_empty_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Planner 调用 reply 漏传 msg_id 时自动回退到最近一条用户消息；历史为空时仍返回失败。"""
    from src.maisaka.builtin_tool.reply import execute_reply
    from src.maisaka.context.messages import SessionBackedMessage

    src_msg = _make_session_message(msg_id="msg_fallback_001", text="111")
    user_ctx_msg = SessionBackedMessage.from_session_message(
        src_msg,
        raw_message=src_msg.raw_message,
        visible_text="111",
        source_kind="user",
    )

    recorded_ids: list[str] = []

    class _FakeRuntime:
        log_prefix = "[test]"

        def __init__(self, history: list) -> None:
            self._chat_history = history

        def find_source_message_by_id(self, message_id: str) -> None:
            recorded_ids.append(message_id)
            return None

    class _FakeToolCtx:
        def __init__(self, history: list) -> None:
            self.runtime = _FakeRuntime(history)

        def build_failure_result(self, tool_name: str, msg: str) -> dict:
            return {"tool": tool_name, "error": msg}

    # Case 1: 历史为空且 target_message=None 时，返回缺 msg_id 失败提示
    empty_res = await execute_reply(
        _FakeToolCtx([]),
        {"reply_reference": "正常回应"},
        call_id="c1",
    )
    assert "msg_id" in empty_res["error"]
    assert recorded_ids == []

    # Case 2: 历史中有最近一条 user 消息时，自动回退锚定到其 message_id 并进入 find_source_message_by_id
    fallback_res = await execute_reply(
        _FakeToolCtx([user_ctx_msg]),
        {"reply_reference": "正常回应"},
        call_id="c2",
    )
    assert recorded_ids == ["msg_fallback_001"]
    assert "未找到要回复的目标消息" in fallback_res["error"]


def test_replyer_extra_params_and_planner_v2_deduplication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Replyer 透传 extra_params，以及绑定 Persona V2 时 Planner 不重复倾倒长设定。"""
    from src.companion_vault.persona_v2_engine import (
        CorePersonaV2,
        PersonaV2Card,
        PersonaV2RuntimeManager,
    )

    st_engine = STPresetEngine.from_file(TEMPLATE_PRESET_PATH)
    compiled = st_engine.compile_to_context_items(
        STCompileContext(
            char_name="许妍",
            user_name="星河",
            last_user_message="在干嘛",
            protocol_mode="spreset_tool_envelope",
            client_type="gemini",
            model_name="gemini-4-argon",
        )
    )
    gen = object.__new__(BaseMaisakaReplyGenerator)
    gen._last_st_compiled_request = compiled
    gen._last_st_reply_tool_args = {"reply_reference": "正常聊天"}
    gen._uses_st_preset_engine = lambda _args: True  # type: ignore[assignment]

    opts = gen._build_generation_options("gemini-4-argon")
    assert opts.extra_params.get("thinking_level") == "LOW"
    assert opts.extra_params.get("include_thoughts") is False
    assert opts.extra_params.get("tool_config", {}).get("function_calling_config", {}).get("mode") == "ANY"

    # 绑定 Persona V2 卡时，Stage 1 与 @D0 不重复倾倒冗长旧版 personality / owner_private_prompt 全文
    v2_dir = tmp_path / "v2_cards"
    v2_dir.mkdir(parents=True, exist_ok=True)
    younger_card = PersonaV2Card(
        card_id="card_younger_test",
        name="许妍V2测试卡",
        planner_summary="妹妹极简导演摘要_217",
        core_persona=CorePersonaV2(
            soul_pitch="灵动热烈妹妹魂",
            owner_relationship_bond="专属V2核心张力短摘要",
        ),
    )
    mgr = PersonaV2RuntimeManager(data_dir=v2_dir, templates_dir=v2_dir)
    mgr.save_card(younger_card)
    monkeypatch.setattr("src.companion_vault.persona_v2_engine._PERSONA_V2_MANAGER", mgr)

    younger_prof = PersonalityProfile(
        name="妹妹",
        nickname="许妍",
        personality="OLD_LONG_PERSONALITY_DUMP_ONLY_FALLBACK",
        behavior_style="自然",
        reply_style="短句活力",
        group_chat_prompt="群聊注意",
        private_chat_prompts="私聊注意",
        owner_private_prompt="OLD_LONG_OWNER_PRIV_DUMP_ONLY_FALLBACK",
        persona_v2_card_id="card_younger_test",
    )
    elder_prof = PersonalityProfile(
        name="姐姐",
        nickname="许玥",
        personality="ELDER_NO_V2_PERSONALITY",
        behavior_style="从容",
        reply_style="温柔懒散",
        group_chat_prompt="群聊注意",
        private_chat_prompts="私聊注意",
        owner_private_prompt="ELDER_NO_V2_OWNER_PRIV",
    )
    monkeypatch.setattr(global_config.personality, "twin_mode_enabled", True)
    monkeypatch.setattr(global_config.personality, "younger_slot_profile", "妹妹")
    monkeypatch.setattr(global_config.personality, "elder_slot_profile", "姐姐")
    monkeypatch.setattr(global_config.personality, "profiles", [younger_prof, elder_prof])
    monkeypatch.setattr(global_config.personality, "selected_profile", "妹妹")

    stage1 = MaisakaChatLoopService._build_st_director_stage1_block(None)  # type: ignore[arg-type]
    assert "妹妹极简导演摘要_217" in stage1
    assert "OLD_LONG_PERSONALITY_DUMP_ONLY_FALLBACK" not in stage1
    assert "ELDER_NO_V2_PERSONALITY" in stage1

    decision = TwinTurnDecision(
        is_owner_private=True,
        sender_trust_role="OWNER",
        recommended_slot="sister_younger",
        younger_profile=younger_prof,
        elder_profile=elder_prof,
    )
    d0 = MaisakaChatLoopService.build_planner_d0_director_guidance(decision, recent_text="晚上好")
    assert "专属V2核心张力短摘要" in d0
    assert "OLD_LONG_OWNER_PRIV_DUMP_ONLY_FALLBACK" not in d0
    assert "ELDER_NO_V2_OWNER_PRIV" in d0


@pytest.mark.asyncio
async def test_twin_router_elder_return_arc_timeout_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """姐姐回归衔接生成超时(>8s)时自动降级为本地衔接提示，不阻塞 Planner 首轮。"""
    import asyncio

    db_path = tmp_path / "truth_timeout.db"
    _seed_truth_db(db_path)
    ledger = ComputeLedger(tmp_path / "ledger_timeout.db")
    await ledger.apply_remote_balance_reading(10.0, is_available=True)
    ledger.set_elder_away_arc("临时出门买咖啡")

    class _SlowDirector:
        async def generate_elder_return_arc(self, **_kwargs: object) -> str:
            await asyncio.sleep(20.0)
            return "不会等到这里"

    real_wait_for = asyncio.wait_for

    async def _fast_wait_for(aw: object, timeout: float | None = None) -> object:
        return await real_wait_for(aw, timeout=0.05)  # type: ignore[arg-type]

    monkeypatch.setattr("src.companion_vault.twin_runtime_router.asyncio.wait_for", _fast_wait_for)

    eco_cfg = TwinEconomyConfig(
        hibernating_threshold_usd=0.5,
        thrifty_threshold_usd=3.0,
        supercharged_threshold_usd=15.0,
    )
    pers_cfg = PersonalityConfig(
        twin_mode_enabled=True,
        younger_slot_profile="妹妹",
        elder_slot_profile="姐姐",
        profiles=[
            PersonalityProfile(
                name="妹妹",
                nickname="许妍",
                personality="灵动妹妹",
                behavior_style="自然",
                reply_style="短句活力",
                group_chat_prompt="群聊注意",
                private_chat_prompts="私聊注意",
            ),
            PersonalityProfile(
                name="姐姐",
                nickname="许玥",
                personality="温柔姐姐",
                behavior_style="从容",
                reply_style="慵懒尾音",
                group_chat_prompt="群聊注意",
                private_chat_prompts="私聊注意",
            ),
        ],
    )
    decision = await resolve_turn_route(
        session_id="wechat_priv_owner",
        platform="wechat",
        sender_id="wxid_owner",
        is_group_chat=False,
        latest_user_text="回来啦",
        ledger=ledger,
        director=_SlowDirector(),  # type: ignore[arg-type]
        truth_db_path=str(db_path),
        eco_cfg=eco_cfg,
        personality_cfg=pers_cfg,
    )
    assert "算力经费恢复·姐姐回归衔接锚点" in decision.transient_narrative_note
    assert "临时出门买咖啡" in decision.transient_narrative_note
    assert "许玥" in decision.transient_narrative_note
    assert ledger.get_snapshot().elder_away_arc == ""


# ---------------------------------------------------------------------------
# 9. 统一 ST-Agent 一步直出（私聊/群聊通用）、工具收敛闭环与零截断校验
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unified_st_execution_mode_and_untruncated_owner_bond(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 resolve_turn_route 在私聊/群聊下支持 one_pass_st 与 STPreset 联动，且 D0 不再截断 [:450]。"""
    import src.companion_vault.st_preset_engine as st_mod
    from src.companion_vault.persona_v2_engine import CorePersonaV2, PersonaV2Card, PersonaV2RuntimeManager

    db_path = tmp_path / "truth_mode.db"
    _seed_truth_db(db_path)
    ledger = ComputeLedger(db_path)
    await ledger.apply_remote_balance_reading(10.0, is_available=True)

    presets_dir = tmp_path / "presets"
    st_mgr = STPresetRuntimeManager(presets_dir=presets_dir)
    monkeypatch.setattr(st_mod, "_GLOBAL_ST_MANAGER", st_mgr)
    monkeypatch.setenv("MAIBOT_ST_PRESETS_DIR", str(presets_dir))

    pers_cfg = PersonalityConfig(twin_mode_enabled=False, profiles=[], selected_profile="")

    # 1. STPreset 关闭 + dual_director -> 私聊与群聊均为 dual_director
    st_mgr.save_config({"enabled": False})
    eco_dual = TwinEconomyConfig(enabled=True, chat_execution_mode="dual_director")
    d_priv_dual = await resolve_turn_route(
        session_id="priv_owner",
        sender_id="wxid_owner",
        is_group_chat=False,
        latest_user_text="hi",
        ledger=ledger,
        truth_db_path=str(db_path),
        eco_cfg=eco_dual,
        personality_cfg=pers_cfg,
    )
    d_grp_dual = await resolve_turn_route(
        session_id="grp_alpha",
        sender_id="wxid_owner",
        is_group_chat=True,
        latest_user_text="hi",
        ledger=ledger,
        truth_db_path=str(db_path),
        eco_cfg=eco_dual,
        personality_cfg=pers_cfg,
    )
    assert d_priv_dual.execution_mode == "dual_director"
    assert d_grp_dual.execution_mode == "dual_director"

    # 2. chat_execution_mode == "one_pass_st" -> 私聊与群聊均为 one_pass_st
    eco_one_pass = TwinEconomyConfig(enabled=True, chat_execution_mode="one_pass_st")
    d_priv_one = await resolve_turn_route(
        session_id="priv_owner",
        sender_id="wxid_owner",
        is_group_chat=False,
        latest_user_text="hi",
        ledger=ledger,
        truth_db_path=str(db_path),
        eco_cfg=eco_one_pass,
        personality_cfg=pers_cfg,
    )
    d_grp_one = await resolve_turn_route(
        session_id="grp_alpha",
        sender_id="wxid_owner",
        is_group_chat=True,
        latest_user_text="hi",
        ledger=ledger,
        truth_db_path=str(db_path),
        eco_cfg=eco_one_pass,
        personality_cfg=pers_cfg,
    )
    assert d_priv_one.execution_mode == "one_pass_st"
    assert d_grp_one.execution_mode == "one_pass_st"

    # 3. 全局启用 STPresetEngine (enabled=True) 时，即使配置仍为 dual_director 也自动进入 one_pass_st
    st_mgr.save_config({"enabled": True, "active_preset": "Meowssiah-WeChat-IM"})
    d_priv_st = await resolve_turn_route(
        session_id="priv_owner",
        sender_id="wxid_owner",
        is_group_chat=False,
        latest_user_text="hi",
        ledger=ledger,
        truth_db_path=str(db_path),
        eco_cfg=eco_dual,
        personality_cfg=pers_cfg,
    )
    d_grp_st = await resolve_turn_route(
        session_id="grp_alpha",
        sender_id="wxid_owner",
        is_group_chat=True,
        latest_user_text="hi",
        ledger=ledger,
        truth_db_path=str(db_path),
        eco_cfg=eco_dual,
        personality_cfg=pers_cfg,
    )
    assert d_priv_st.execution_mode == "dual_director"
    assert d_grp_st.execution_mode == "dual_director"

    # 4. build_planner_d0_director_guidance 保留完整 owner_relationship_bond（不再 [:450] 截断）
    long_bond = "A" * 620 + "_TAIL_BOND_ANCHOR"
    v2_dir = tmp_path / "v2_cards_bond"
    v2_dir.mkdir(parents=True, exist_ok=True)
    card = PersonaV2Card(
        card_id="bond_full_card",
        name="许妍全量羁绊测试",
        core_persona=CorePersonaV2(owner_relationship_bond=long_bond),
    )
    v2_mgr = PersonaV2RuntimeManager(data_dir=v2_dir, templates_dir=v2_dir)
    v2_mgr.save_card(card)
    monkeypatch.setattr("src.companion_vault.persona_v2_engine._PERSONA_V2_MANAGER", v2_mgr)

    prof = PersonalityProfile(
        name="妹妹",
        nickname="许妍",
        personality="灵动妹妹",
        behavior_style="自然",
        reply_style="口语",
        group_chat_prompt="群聊注意",
        private_chat_prompts="私聊注意",
        persona_v2_card_id="bond_full_card",
    )
    monkeypatch.setattr(global_config.personality, "twin_mode_enabled", False)
    monkeypatch.setattr(global_config.personality, "profiles", [prof])
    monkeypatch.setattr(global_config.personality, "younger_slot_profile", "妹妹")
    monkeypatch.setattr(global_config.personality, "selected_profile", "妹妹")

    guidance = MaisakaChatLoopService.build_planner_d0_director_guidance(
        TwinTurnDecision(
            is_owner_private=True,
            sender_trust_role="OWNER",
            younger_profile=prof,
            elder_profile=prof,
        )
    )
    assert long_bond in guidance
    assert "_TAIL_BOND_ANCHOR" in guidance


def _setup_one_pass_st_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_group_chat: bool,
    llm_responses: list[object],
    extra_tools: list[object] | None = None,
):
    """构造完整的 _execute_one_pass_st_turn 端到端测试台（含真实 Replyer、ToolRegistry 与 send_service 拦截）。"""
    import src.companion_vault.st_preset_engine as st_mod
    from src.chat.replyer.maisaka_generator import MaisakaReplyGenerator
    from src.chat.replyer.replyer_manager import replyer_manager
    from src.core.tooling import ToolRegistry
    from src.maisaka.builtin_tool.provider import MaisakaBuiltinToolProvider
    from src.maisaka.context.messages import SessionBackedMessage

    presets_dir = tmp_path / "presets"
    st_mgr = STPresetRuntimeManager(presets_dir=presets_dir)
    st_mgr.save_config(
        {
            "enabled": True,
            "active_preset": "Meowssiah-WeChat-IM",
            "output_mode": "im_clean",
            "protocol_mode": "spreset_tool_envelope",
        }
    )
    monkeypatch.setattr(st_mod, "_GLOBAL_ST_MANAGER", st_mgr)
    monkeypatch.setenv("MAIBOT_ST_PRESETS_DIR", str(presets_dir))

    db_path = tmp_path / "truth.db"
    _seed_truth_db(db_path)
    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(db_path))

    younger_prof = PersonalityProfile(
        name="妹妹",
        nickname="许妍",
        personality="灵动妹妹",
        behavior_style="自然",
        reply_style="口语",
        group_chat_prompt="群聊注意",
        private_chat_prompts="私聊注意",
    )
    elder_prof = PersonalityProfile(
        name="姐姐",
        nickname="许玥",
        personality="温柔姐姐",
        behavior_style="从容",
        reply_style="慵懒",
        group_chat_prompt="群聊注意",
        private_chat_prompts="私聊注意",
    )
    monkeypatch.setattr(global_config.personality, "twin_mode_enabled", True)
    monkeypatch.setattr(global_config.personality, "younger_slot_profile", "妹妹")
    monkeypatch.setattr(global_config.personality, "elder_slot_profile", "姐姐")
    monkeypatch.setattr(global_config.personality, "profiles", [younger_prof, elder_prof])
    monkeypatch.setattr(global_config.personality, "selected_profile", "妹妹")
    monkeypatch.setattr(global_config.bot, "nickname", "许妍")
    monkeypatch.setattr(global_config.chat.reply_style, "group_quote_frequency", "never")
    monkeypatch.setattr(global_config.chat.reply_style, "private_quote_frequency", "never")
    monkeypatch.setattr(global_config.debug, "save_maisaka_reasoning", False)
    monkeypatch.setattr(global_config.debug, "replyer_retro_prompt", False)
    monkeypatch.setattr(global_config.a_memorix.integration, "enable_person_profile_injection", False)
    monkeypatch.setattr(global_config.chinese_typo, "enable", False)

    session_id = "grp_room_st" if is_group_chat else "priv_owner_st"
    group_id = "room@chatroom" if is_group_chat else ""
    chat_stream = SimpleNamespace(
        session_id=session_id,
        platform="wechat",
        account_id="bot_acc_001",
        scope="wechat",
        is_group_session=is_group_chat,
        group_id=group_id,
        user_id="" if is_group_chat else "wxid_owner",
    )

    llm_calls: list[dict[str, object]] = []

    class _FakeExpressLLMClient:
        def __init__(self, task_name: str = "replyer", **_kwargs: object) -> None:
            self.task_name = task_name

        async def generate_response_with_context(self, context_factory, options):
            ctx_items = await context_factory(SimpleNamespace(api_provider=SimpleNamespace(client_type="openai")))
            llm_calls.append({"task_name": self.task_name, "options": options, "context_items": ctx_items})
            idx = min(len(llm_calls) - 1, len(llm_responses) - 1)
            return llm_responses[idx]

    replyer = MaisakaReplyGenerator(
        chat_stream=chat_stream,
        request_type="maisaka.replyer",
        llm_client_cls=_FakeExpressLLMClient,
    )
    monkeypatch.setitem(replyer_manager._repliers, f"maisaka:{session_id}", replyer)
    monkeypatch.setattr(
        "src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info",
        lambda sid: (is_group_chat, None),
    )

    msg_old = _make_session_message(
        user_id="wxid_owner",
        nickname="星河",
        group_id=group_id,
        text="早点休息哦",
        msg_id="msg_old_001",
        session_id=session_id,
    )
    msg_new = _make_session_message(
        user_id="wxid_owner",
        nickname="星河",
        group_id=group_id,
        text="还记得咱们东京约定吗",
        msg_id="msg_new_002",
        session_id=session_id,
    )
    hist_messages = [
        SessionBackedMessage.from_session_message(m, raw_message=m.raw_message, visible_text=m.processed_plain_text, source_kind="user")
        for m in (msg_old, msg_new)
    ]

    sent_payloads: list[dict[str, object]] = []

    async def _fake_send_to_target_with_message(**kwargs):
        sent_payloads.append(kwargs)
        return SimpleNamespace(message_id=f"sent_{len(sent_payloads)}")

    monkeypatch.setattr(
        "src.maisaka.builtin_tool.reply.send_service._send_to_target_with_message",
        _fake_send_to_target_with_message,
    )

    registry = ToolRegistry()

    class _Runtime:
        log_prefix = "[TestST]"
        _tool_registry = registry
        _chat_history = list(hist_messages)
        _max_context_size = 50
        _last_reply_target_message_id = ""
        _agent_state = "running"
        _STATE_RUNNING = "running"
        _STATE_WAIT = "wait"
        _STATE_STOP = "stop"
        _current_action_tool_names: set[str] = set()
        deferred_tool_specs_by_name: dict[str, object] = {}
        _update_stage_status = MagicMock()
        _reset_consecutive_wait_count = MagicMock()
        _end_planner_continuation = MagicMock()
        _enter_stop_state = MagicMock()
        _enter_counted_wait_state = MagicMock(return_value=1)
        record_planner_reply = MagicMock()
        _has_pending_messages = staticmethod(lambda: False)
        get_reply_quote_context = staticmethod(lambda _target: None)

        def __init__(self) -> None:
            self.session_id = session_id
            self.chat_stream = chat_stream
            self._chat_loop_service = SimpleNamespace(
                select_llm_context_messages=lambda hist, **_kw: (list(hist), "test")
            )

        def find_source_message_by_id(self, message_id: str):
            for h in self._chat_history:
                if getattr(h, "message_id", "") == message_id:
                    return h.original_message
            return None

        @staticmethod
        def expand_message_id_aliases(args: dict):
            return dict(args)

        def set_current_action_tool_names(self, names):
            self._current_action_tool_names = set(names)

        def is_action_tool_currently_available(self, name: str) -> bool:
            return name in self._current_action_tool_names

        @staticmethod
        def update_deferred_tool_specs(_specs):
            return None

        @staticmethod
        def sync_discovered_deferred_tools_with_context(_hist):
            return None

        @staticmethod
        def get_discovered_deferred_tool_specs():
            return []

        @staticmethod
        def build_deferred_tools_reminder() -> str:
            return ""

        def _is_focus_mode_active_for_current_chat(self) -> bool:
            return False

    runtime = _Runtime()
    engine = MaisakaReasoningEngine(runtime)
    runtime._reasoning_engine = engine
    engine._start_cycle_round = AsyncMock(return_value=(SimpleNamespace(time_records={}, cycle_id=1), "R1"))
    engine._refresh_visual_placeholders_for_cycle = AsyncMock()
    engine._post_process_chat_history_after_cycle = AsyncMock()
    engine._record_tool_execution_effects = AsyncMock()
    engine._emit_planner_progress = AsyncMock()

    captured_states: list[object] = []

    async def _fake_finalize(*, cycle_detail, round_index, state):
        captured_states.append(state)
        return state.cycle_end

    engine._finalize_cycle = _fake_finalize

    registry.register_provider(MaisakaBuiltinToolProvider(engine.build_builtin_tool_handlers()))
    if extra_tools:
        for provider in extra_tools:
            registry.register_provider(provider)

    return engine, runtime, msg_old, msg_new, llm_calls, sent_payloads, captured_states


@pytest.mark.asyncio
async def test_one_pass_st_direct_soliumbra_reply_calls_llm_once_and_delivers_clean_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 1-pass 直接调用 soliumbra_reply：全局仅调用 1 次 LLM、剥离标签、支持 slot/quote 切换并发送干净微信气泡。"""
    from src.common.data_models.llm_service_data_models import LLMResponseResult
    from src.llm_models.payload_content.tool_option import ToolCall

    precomputed_res = LLMResponseResult.from_portable_output(
        tool_calls=[
            ToolCall(
                call_id="call_st_1",
                func_name="soliumbra_reply",
                args={
                    "think_nya": "喵呼呼，切到姐姐声带回复旧消息！",
                    "game_content": (
                        "<game>笨蛋，这种事当然一直都记得\n今晚别太晚睡</game>\n"
                        "<summary>许玥温柔回应旧约定</summary>\n"
                        "<comment>1-pass直出真香喵！</comment>"
                    ),
                    "summary": "许玥回应约定",
                    "comment": "喵~",
                    "persona_slot": "sister_elder",
                    "quote_msg_id": "msg_old_001",
                },
            )
        ],
        model_name="gemini-4-argon",
        prompt_tokens=120,
        completion_tokens=45,
        total_tokens=165,
    )

    engine, _runtime, msg_old, msg_new, llm_calls, sent_payloads, captured_states = _setup_one_pass_st_harness(
        tmp_path,
        monkeypatch,
        is_group_chat=False,
        llm_responses=[precomputed_res],
    )
    decision = TwinTurnDecision(
        abort_turn=False,
        allowed_slots=("sister_younger", "sister_elder"),
        forced_slot=None,
        recommended_slot="sister_younger",
        sender_trust_role="OWNER",
        is_owner_private=True,
        execution_mode="one_pass_st",
    )
    engine._current_turn_decision = decision

    await engine._execute_one_pass_st_turn(
        trigger_message=msg_new,
        source_messages=[msg_old, msg_new],
        decision=decision,
    )

    assert len(llm_calls) == 1, "1-pass soliumbra_reply 绝不能重复调用 LLM"
    round0_tool_names = [
        str((t.get("function") or {}).get("name") or t.get("name") or "")
        for t in (llm_calls[0]["options"].tool_options or [])
    ]
    assert "soliumbra_reply" in round0_tool_names
    assert "reply" not in round0_tool_names

    assert len(sent_payloads) >= 1
    combined_sent = "\n".join(str(p["processed_plain_text"]) for p in sent_payloads)
    assert "笨蛋" in combined_sent
    assert "这种事当然一直都记得" in combined_sent
    assert "今晚别太晚睡" in combined_sent
    for leaked in ("<game>", "<summary>", "<comment>", "<think_nya>", "喵呼呼"):
        assert leaked not in combined_sent
    assert sent_payloads[0]["reply_message"].message_id == "msg_old_001"
    assert sent_payloads[0]["extra_config"]["persona_slot"] == "sister_elder"

    state = captured_states[0]
    assert state.cycle_end.reason == "one_pass_st_end"
    assert state.tool_monitor_results[0]["st_telemetry"]["persona_slot"] == "sister_elder"
    assert "切到姐姐声带" in state.tool_monitor_results[0]["st_telemetry"]["thinking"]


@pytest.mark.asyncio
async def test_one_pass_st_two_step_tool_call_and_observations_convergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 2-step 工具收敛：Round 0 调用 query_memory -> 注入 tool_observations -> Pass 2 强制收敛 soliumbra_reply。"""
    from src.common.data_models.llm_service_data_models import LLMResponseResult
    from src.core.tooling import ToolExecutionResult, ToolProvider, ToolSpec
    from src.llm_models.payload_content.tool_option import ToolCall

    tool_invocations: list[dict[str, object]] = []

    class _FakeMemoryProvider(ToolProvider):
        provider_name = "test_memory"
        provider_type = "builtin"

        async def list_tools(self, context=None):
            return [
                ToolSpec(
                    name="query_memory",
                    description="查询长期记忆",
                    parameters_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                    provider_name="test_memory",
                    provider_type="builtin",
                )
            ]

        async def invoke(self, invocation, context=None):
            tool_invocations.append({"name": invocation.tool_name, "args": dict(invocation.arguments)})
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=True,
                content="命中记忆：上个月在根津咖啡馆约好周末一起去看樱花。",
            )

        async def close(self):
            return None

    round0_res = LLMResponseResult.from_portable_output(
        tool_calls=[
            ToolCall(
                call_id="qm_call_1",
                func_name="query_memory",
                args={"query": "东京约定"},
            )
        ],
        model_name="gemini-3.8-flash",
    )
    round1_res = LLMResponseResult.from_portable_output(
        tool_calls=[
            ToolCall(
                call_id="st_conv_2",
                func_name="soliumbra_reply",
                args={
                    "think_nya": "拿到记忆啦，立刻温和回应喵~",
                    "game_content": "<game>记得呀，上次在根津咖啡馆说好周末去看樱花的</game>",
                },
            )
        ],
        model_name="gemini-3.8-flash",
    )

    engine, _runtime, msg_old, msg_new, llm_calls, sent_payloads, captured_states = _setup_one_pass_st_harness(
        tmp_path,
        monkeypatch,
        is_group_chat=False,
        llm_responses=[round0_res, round1_res],
        extra_tools=[_FakeMemoryProvider()],
    )
    decision = TwinTurnDecision(
        abort_turn=False,
        allowed_slots=("sister_younger", "sister_elder"),
        recommended_slot="sister_younger",
        sender_trust_role="OWNER",
        is_owner_private=True,
        execution_mode="one_pass_st",
    )
    engine._current_turn_decision = decision

    await engine._execute_one_pass_st_turn(
        trigger_message=msg_new,
        source_messages=[msg_old, msg_new],
        decision=decision,
    )

    assert len(llm_calls) == 2
    assert len(tool_invocations) == 1
    assert tool_invocations[0] == {"name": "query_memory", "args": {"query": "东京约定"}}

    pass1_allowed = llm_calls[1]["options"].extra_params["tool_config"]["function_calling_config"][
        "allowed_function_names"
    ]
    assert pass1_allowed == ["soliumbra_reply"]
    sync_payload_text = str(getattr(llm_calls[1]["context_items"][-1], "output", ""))
    assert "tool_observations" in sync_payload_text
    assert "根津咖啡馆" in sync_payload_text

    assert len(sent_payloads) >= 1
    combined_sent = "\n".join(str(p["processed_plain_text"]) for p in sent_payloads)
    assert "根津咖啡馆说好周末去看樱花" in combined_sent

    state = captured_states[0]
    assert state.cycle_end.reason == "one_pass_st_end"
    assert [m["tool_name"] for m in state.tool_monitor_results] == ["query_memory", "reply"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "tool_args", "expected_reason"),
    [
        ("stop", {}, "planner_no_tool_end"),
        ("wait", {"seconds": 25}, "tool_pause:wait"),
    ],
)
async def test_one_pass_st_group_chat_stop_and_wait_pause_without_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    tool_args: dict[str, object],
    expected_reason: str,
) -> None:
    """验证群聊统一 ST-Agent 调用 stop / wait 时立即静默结束或进入等待，不触发任何回复发送。"""
    from src.common.data_models.llm_service_data_models import LLMResponseResult
    from src.llm_models.payload_content.tool_option import ToolCall

    pause_res = LLMResponseResult.from_portable_output(
        tool_calls=[
            ToolCall(
                call_id=f"pause_{tool_name}_1",
                func_name=tool_name,
                args=tool_args,
            )
        ],
        model_name="gemini-3.8-flash",
    )

    engine, runtime, msg_old, msg_new, llm_calls, sent_payloads, captured_states = _setup_one_pass_st_harness(
        tmp_path,
        monkeypatch,
        is_group_chat=True,
        llm_responses=[pause_res],
    )
    decision = TwinTurnDecision(
        abort_turn=False,
        allowed_slots=("sister_younger", "sister_elder"),
        recommended_slot="sister_younger",
        sender_trust_role="GROUP_MEMBER",
        is_owner_private=False,
        execution_mode="one_pass_st",
    )
    engine._current_turn_decision = decision

    await engine._execute_one_pass_st_turn(
        trigger_message=msg_new,
        source_messages=[msg_old, msg_new],
        decision=decision,
    )

    assert len(llm_calls) == 1
    assert sent_payloads == []
    state = captured_states[0]
    assert state.cycle_end.reason == expected_reason
    assert state.tool_monitor_results[0]["tool_name"] == tool_name
    if tool_name == "stop":
        runtime._enter_stop_state.assert_called_once()
    else:
        runtime._enter_counted_wait_state.assert_called_once()
        assert runtime._enter_counted_wait_state.call_args.kwargs["seconds"] == 25
