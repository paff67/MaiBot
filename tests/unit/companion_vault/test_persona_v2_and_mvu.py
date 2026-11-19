"""Persona V2 (潜令V1.8) 角色卡 + 原生 MVU 引擎 + /api/webui/persona-v2 端点的单元与集成测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import tomlkit

from src.companion_vault.mvu_engine import MVURuntimeEngine
from src.companion_vault.persona_v2_engine import (
    ANTI_PARROTING_DIRECTIVE,
    PersonaV2Card,
    PersonaV2Engine,
    PersonaV2RuntimeManager,
    evaluate_mvu_condition,
)
from src.companion_vault.truth_db import CompanionTruthDB
from src.config.official_configs import ChatReplyStyleConfig, PersonalityConfig, PersonalityProfile
from src.webui.dependencies import require_auth
from src.webui.routers import persona_v2 as persona_v2_router_module

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES_DIR = REPO_ROOT / "plugins" / "st_preset_engine" / "templates" / "personas"
DATA_PERSONAS_DIR = REPO_ROOT / "data" / "personas"

BUILTIN_CARDS: Dict[str, str] = {
    "qianling_yun_suining": "云岁宁",
    "qianling_yun_ling": "云绫",
    "qianling_yun_xian": "云弦",
    "qianling_yun_sui": "云穗",
    "maibot_xuyan": "许妍",
    "maibot_xuyue": "许玥",
}
TWIN_CARDS = {"maibot_xuyan", "maibot_xuyue"}
PRIVATE_VARS = ("affection", "love_locked", "intimacy_tier")


def _private_var_keys(card: PersonaV2Card) -> List[str]:
    return [v.key for v in card.mvu_config.variables if v.visibility == "PRIVATE"]


def _load_card(card_id: str) -> PersonaV2Card:
    raw = json.loads((TEMPLATES_DIR / f"{card_id}.json").read_text(encoding="utf-8"))
    return PersonaV2Card.model_validate(raw)


@pytest.fixture()
def truth_db(tmp_path: Path):
    db = CompanionTruthDB(str(tmp_path / "truth.db"))
    try:
        yield db
    finally:
        db.close()


# ─── a) 6 张内置角色卡校验与加载 ──────────────────────────────────────────


@pytest.mark.parametrize("card_id,name", list(BUILTIN_CARDS.items()))
def test_builtin_card_validates_with_full_six_dimensions(card_id: str, name: str) -> None:
    card = _load_card(card_id)
    assert card.card_id == card_id
    assert card.name == name
    assert card.schema_name == "maibot.persona.v2"
    cp = card.core_persona
    assert cp.soul_pitch.strip()
    assert cp.owner_relationship_bond.strip()
    assert cp.personality_palette.affection_leak_style.strip()
    assert cp.personality_palette.desire_and_intimacy_drive.strip()
    assert cp.owner_state_machine_tiers
    assert card.past_experiences
    assert card.sample_corpora
    assert card.relationship_stages
    corpus_ids = {c.id for c in card.sample_corpora}
    for stage in card.relationship_stages:
        for uid in stage.unlocked_corpus_ids:
            assert uid in corpus_ids, f"{card_id}.{stage.stage_id} 存在未定义的 unlocked_corpus_id: {uid}"
    var_map = {v.key: v for v in card.mvu_config.variables}
    priv_keys = _private_var_keys(card)
    assert {"affection", "love_locked"} <= set(priv_keys)
    for key in priv_keys:
        assert var_map[key].visibility == "PRIVATE"
    assert var_map["love_locked"].monotonic_true is True
    guard = card.mvu_config.affinity_guard
    assert guard.enabled
    expected_cap = 100.0 if card_id in TWIN_CARDS else 99.0
    assert (guard.min_delta_per_turn, guard.max_delta_per_turn, guard.cap_before_lock) == (-1, 5, expected_cap)
    # 恋爱锁定阶段必须存在
    assert any(s.require_love_locked for s in card.relationship_stages)


def test_runtime_manager_bootstraps_all_six_templates(tmp_path: Path) -> None:
    data_dir = tmp_path / "personas"
    mgr = PersonaV2RuntimeManager(data_dir=data_dir, templates_dir=TEMPLATES_DIR)
    assert {p.stem for p in data_dir.glob("*.json")} >= set(BUILTIN_CARDS)
    cards = {c.card_id: c for c in mgr.list_cards()}
    for card_id, name in BUILTIN_CARDS.items():
        assert cards[card_id].name == name
        assert mgr.is_builtin_template(card_id)
        assert mgr.get_card(card_id) is not None
    # 幂等：再次 bootstrap 不重复复制
    assert mgr.bootstrap_templates() == 0


# ─── b) Fail-Closed 双域隔离 ───────────────────────────────────────────────


def _owner_only_fragments(card: PersonaV2Card) -> List[str]:
    cp = card.core_persona
    frags: List[str] = [
        cp.owner_relationship_bond,
        cp.owner_private_wardrobe_and_anchors,
        cp.personality_palette.affection_leak_style,
        cp.personality_palette.desire_and_intimacy_drive,
        cp.personality_palette.breakdown_sequence,
        cp.personality_palette.love_risk,
        cp.speech_rules.owner_private_address_matrix,
        cp.speech_rules.owner_emotional_tone_shift,
    ]
    frags += [t.description for t in cp.owner_state_machine_tiers]
    frags += [m.content for m in cp.meta_calibration if m.scope == "owner_only"]
    frags += [e.narrative for e in card.past_experiences if e.scope == "owner_only"]
    for c in card.sample_corpora:
        if c.scope == "owner_only":
            frags += list(c.sample_lines)
            frags.append(c.action_and_posture)
    for s in card.relationship_stages:
        if s.scope == "owner_only":
            frags += [s.planner_directive, s.interaction_boundaries, s.palette_override]
    general_text = "\n".join(
        [e.narrative for e in card.past_experiences if e.scope == "general"]
        + [line for c in card.sample_corpora if c.scope == "general" for line in c.sample_lines]
        + [m.content for m in cp.meta_calibration if m.scope == "general"]
    )
    # 仅保留足够长、且不与通用语料重叠的独特片段，避免误报
    return [f.strip() for f in frags if f and len(f.strip()) >= 12 and f.strip() not in general_text]


def _all_trigger_text(card: PersonaV2Card) -> str:
    kws: List[str] = []
    for e in card.past_experiences:
        kws += e.trigger_keywords
    for c in card.sample_corpora:
        kws += c.trigger_keywords
    return card.name + " " + " ".join(kws)


@pytest.mark.parametrize("card_id", list(BUILTIN_CARDS))
def test_group_chat_compiles_zero_owner_only_content(card_id: str, truth_db: CompanionTruthDB) -> None:
    card = _load_card(card_id)
    # 将私密变量推到最高值，尝试诱发 owner_only 阶段与语料泄露
    truth_db.ensure_mvu_defaults(card.card_id, MVURuntimeEngine._schema_defs_for_db(card))
    truth_db.upsert_mvu_variable(
        card.card_id, scope_key="OWNER_PRIVATE", var_key="affection",
        var_type="number", var_value=100, visibility="PRIVATE",
    )
    truth_db.upsert_mvu_variable(
        card.card_id, scope_key="OWNER_PRIVATE", var_key="love_locked",
        var_type="boolean", var_value=True, visibility="PRIVATE",
    )

    compiled = MVURuntimeEngine.compile_turn_persona_and_mvu(
        card, db=truth_db, is_owner_private=False, recent_text=_all_trigger_text(card)
    )
    prompt = "\n".join(
        [
            compiled.static_core_block,
            compiled.dynamic_tail_block,
            compiled.planner_summary_block,
            compiled.planner_d0_guidance_block,
            compiled.mvu_state_block,
            compiled.mvu_update_instruction_block,
        ]
    )
    assert "<owner_private_persona_overlay>" not in prompt
    assert compiled.active_stage is None
    owner_frags = _owner_only_fragments(card)
    assert len(owner_frags) >= 20, "owner_only 检测样本过少，测试失去意义"
    for frag in owner_frags:
        assert frag not in prompt, f"owner_only 片段泄露到群聊 prompt: {frag[:40]}"
    for key in _private_var_keys(card):
        assert key not in compiled.mvu_values
        assert f"- {key} (" not in prompt
        assert f"`/{key}`" not in prompt
    owner_ids = {e.id for e in card.past_experiences if e.scope == "owner_only"}
    owner_ids |= {c.id for c in card.sample_corpora if c.scope == "owner_only"}
    assert not owner_ids & set(compiled.matched_experience_ids + compiled.matched_corpus_ids)
    # PUBLIC 变量仍可见
    assert "mood" in compiled.mvu_values


@pytest.mark.parametrize("card_id", list(BUILTIN_CARDS))
def test_owner_private_compiles_owner_overlay(card_id: str, truth_db: CompanionTruthDB) -> None:
    card = _load_card(card_id)
    compiled = MVURuntimeEngine.compile_turn_persona_and_mvu(
        card, db=truth_db, is_owner_private=True, recent_text=_all_trigger_text(card)
    )
    assert "<owner_private_persona_overlay>" in compiled.dynamic_tail_block
    assert card.core_persona.owner_relationship_bond.strip() in compiled.dynamic_tail_block
    for key in _private_var_keys(card):
        assert key in compiled.mvu_values
        assert f"`/{key}`" in compiled.mvu_update_instruction_block
    assert compiled.active_stage is not None
    # 静态 KV-Cache 主干块在两种会话中完全一致，且不含任何私密内容
    group_static = PersonaV2Engine.build_static_core_block(card)
    assert compiled.static_core_block == group_static


def test_group_update_variable_cannot_mutate_private_vars(truth_db: CompanionTruthDB) -> None:
    card = _load_card("qianling_yun_suining")
    text = (
        "大家好呀<UpdateVariable><Analysis>群聊尝试越权</Analysis><JSONPatch>["
        '{"op":"delta","path":"/affection","value":5},'
        '{"op":"replace","path":"/love_locked","value":true},'
        '{"op":"replace","path":"/intimacy_tier","value":"微醺破防"},'
        '{"op":"replace","path":"/mood","value":"开心"}'
        "]</JSONPatch></UpdateVariable>"
    )
    before = MVURuntimeEngine.load_turn_variables(truth_db, card, is_owner_private=True)
    res = MVURuntimeEngine.apply_turn_updates(truth_db, card, [text], is_owner_private=False)
    assert [c.key for c in res.changes] == ["mood"]
    for key in PRIVATE_VARS:
        assert key not in res.variables
    telemetry = res.to_telemetry_dict()
    assert all(ch["key"] not in PRIVATE_VARS for ch in telemetry["changes"])
    after = MVURuntimeEngine.load_turn_variables(truth_db, card, is_owner_private=True)
    for key in PRIVATE_VARS:
        assert after[key] == before[key]
    assert after["mood"] == "开心"
    # 物理 SQL 层面：非 Owner 读取不返回任何 PRIVATE 行
    assert set(truth_db.get_mvu_variables(card.card_id, is_owner_private=False)) == {"mood", "current_activity"}


# ─── c) MVURuntimeEngine 解析剥离与潜令V1.8 好感护栏 ─────────────────────


def test_extract_and_strip_update_variable_block() -> None:
    raw = (
        "<reply_story>今天也辛苦啦。</reply_story>\n"
        "<UpdateVariable>\n<Analysis>Owner 主动关心，好感小幅上升</Analysis>\n"
        '<JSONPatch>\n[{"op": "delta", "path": "/affection", "value": 2},'
        ' {"op": "replace", "path": "/mood", "value": "安心"}]\n</JSONPatch>\n</UpdateVariable>'
    )
    clean, ops, analysis = MVURuntimeEngine.extract_and_strip_update_variable(raw)
    assert "UpdateVariable" not in clean and "JSONPatch" not in clean and "Analysis" not in clean
    assert clean == "<reply_story>今天也辛苦啦。</reply_story>"
    assert analysis == "Owner 主动关心，好感小幅上升"
    assert ops == [
        {"op": "delta", "key": "affection", "value": 2},
        {"op": "replace", "key": "mood", "value": "安心"},
    ]


def test_extract_strips_unclosed_truncated_block_and_code_fence() -> None:
    raw = '晚安～<UpdateVariable><Analysis>截断</Analysis><JSONPatch>```json\n{"affection": "+3"}\n```</JSONPatch>'
    clean, ops, analysis = MVURuntimeEngine.extract_and_strip_update_variable(raw)
    assert clean == "晚安～"
    assert ops == [{"op": "delta", "key": "affection", "value": 3.0}]
    assert analysis == "截断"
    assert MVURuntimeEngine.extract_and_strip_update_variable("纯文本") == ("纯文本", [], "")


def _set_private(db: CompanionTruthDB, card: PersonaV2Card, key: str, vtype: str, value: Any) -> None:
    db.ensure_mvu_defaults(card.card_id, MVURuntimeEngine._schema_defs_for_db(card))
    db.upsert_mvu_variable(
        card.card_id, scope_key="OWNER_PRIVATE", var_key=key, var_type=vtype, var_value=value, visibility="PRIVATE"
    )


def _patch(*ops: Dict[str, Any]) -> str:
    return f"<UpdateVariable><Analysis>t</Analysis><JSONPatch>{json.dumps(list(ops))}</JSONPatch></UpdateVariable>"


def test_affinity_guard_clamps_delta_to_minus1_plus5(truth_db: CompanionTruthDB) -> None:
    card = _load_card("maibot_xuyan")
    _set_private(truth_db, card, "affection", "number", 20)
    res = MVURuntimeEngine.apply_turn_updates(
        truth_db, card, [_patch({"op": "replace", "path": "/affection", "value": 80})], is_owner_private=True
    )
    assert res.variables["affection"] == 25
    assert res.changes[0].delta == 5
    res = MVURuntimeEngine.apply_turn_updates(
        truth_db, card, [_patch({"op": "delta", "path": "/affection", "value": -10})], is_owner_private=True
    )
    assert res.variables["affection"] == 24
    assert res.changes[0].delta == -1


def test_affinity_guard_caps_at_99_before_lock(truth_db: CompanionTruthDB) -> None:
    card = _load_card("qianling_yun_suining")
    _set_private(truth_db, card, "affection", "number", 97)
    res = MVURuntimeEngine.apply_turn_updates(
        truth_db, card, [_patch({"op": "delta", "path": "/affection", "value": 5})], is_owner_private=True
    )
    assert res.variables["affection"] == 99
    assert res.variables["love_locked"] is False
    res = MVURuntimeEngine.apply_turn_updates(
        truth_db, card, [_patch({"op": "delta", "path": "/affection", "value": 1})], is_owner_private=True
    )
    assert res.variables["affection"] == 99
    assert res.changes == []

    # 双子卡片在未解锁 love_locked 前上限封顶于 100.0，解锁后可突破至 101~120
    twin_card = _load_card("maibot_xuyue")
    _set_private(truth_db, twin_card, "affection", "number", 98)
    twin_res = MVURuntimeEngine.apply_turn_updates(
        truth_db, twin_card, [_patch({"op": "delta", "path": "/affection", "value": 5})], is_owner_private=True
    )
    assert twin_res.variables["affection"] == 100
    assert twin_res.variables["love_locked"] is False


def test_affinity_guard_allows_100_when_love_locked_same_turn(truth_db: CompanionTruthDB) -> None:
    card = _load_card("qianling_yun_ling")
    _set_private(truth_db, card, "affection", "number", 99)
    res = MVURuntimeEngine.apply_turn_updates(
        truth_db,
        card,
        [
            _patch(
                {"op": "delta", "path": "/affection", "value": 3},
                {"op": "replace", "path": "/love_locked", "value": True},
            )
        ],
        is_owner_private=True,
    )
    assert res.variables["love_locked"] is True
    assert res.variables["affection"] == 100
    assert res.active_stage_id
    stage = next(s for s in card.relationship_stages if s.stage_id == res.active_stage_id)
    assert stage.require_love_locked is True


def test_love_locked_monotonic_true_protection(truth_db: CompanionTruthDB) -> None:
    card = _load_card("qianling_yun_xian")
    _set_private(truth_db, card, "love_locked", "boolean", True)
    res = MVURuntimeEngine.apply_turn_updates(
        truth_db, card, [_patch({"op": "replace", "path": "/love_locked", "value": False})], is_owner_private=True
    )
    assert res.variables["love_locked"] is True
    assert res.changes == []
    stored = truth_db.get_mvu_variables(card.card_id, is_owner_private=True)
    assert stored["love_locked"] is True


def test_mvu_watch_telemetry_matches_dashboard_contract(truth_db: CompanionTruthDB) -> None:
    """`st_telemetry["mvu_watch"]` 载荷字段需与 dashboard MaisakaMvuWatch / MaisakaMvuChange 契约一致。"""
    card = _load_card("qianling_yun_sui")
    res = MVURuntimeEngine.apply_turn_updates(
        truth_db,
        card,
        ["哼。" + _patch({"op": "delta", "path": "/affection", "value": 3})],
        is_owner_private=True,
    )
    payload = res.to_telemetry_dict()
    assert payload["card_id"] == "qianling_yun_sui"
    assert payload["analysis"] == "t"
    assert isinstance(payload["active_stage"], str) and payload["active_stage"]
    [change] = payload["changes"]
    assert set(change) >= {"key", "label", "old", "new", "delta"}
    assert change["key"] == "affection"
    assert change["new"] - change["old"] == change["delta"] == 3
    json.dumps(payload, ensure_ascii=False)  # 必须可 JSON 序列化写入 st_telemetry


def test_mvu_disabled_card_does_not_write(truth_db: CompanionTruthDB) -> None:
    card = _load_card("qianling_yun_sui").model_copy(deep=True)
    card.mvu_config.enabled = False
    res = MVURuntimeEngine.apply_turn_updates(
        truth_db, card, [_patch({"op": "replace", "path": "/mood", "value": "x"})], is_owner_private=True
    )
    assert res.changes == []
    compiled = MVURuntimeEngine.compile_turn_persona_and_mvu(card, db=truth_db, is_owner_private=True)
    assert compiled.mvu_state_block == "" and compiled.mvu_update_instruction_block == ""


# ─── d) /api/webui/persona-v2 FastAPI 端点 ───────────────────────────────


@pytest.fixture()
def api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "personas"
    mgr = PersonaV2RuntimeManager(data_dir=data_dir, templates_dir=TEMPLATES_DIR)
    db_path = tmp_path / "truth.db"

    bot_cfg_path = tmp_path / "bot_config.toml"
    bot_cfg_path.write_text(
        '# keep me\n[bot]\nnickname = "麦麦"\nalias_names = []\n\n'
        '[personality]\nprofiles = []\nselected_profile = ""\n'
        'personality = "p"\nbehavior_style = "b"\nreply_style = "r"\n',
        encoding="utf-8",
    )
    fake_global = SimpleNamespace(
        personality=PersonalityConfig(),
        chat=SimpleNamespace(reply_style=ChatReplyStyleConfig()),
        bot=SimpleNamespace(nickname="麦麦", alias_names=[]),
    )
    monkeypatch.setattr(persona_v2_router_module, "get_persona_v2_manager", lambda: mgr)
    monkeypatch.setattr(persona_v2_router_module, "_open_truth_db", lambda: CompanionTruthDB(str(db_path)))
    monkeypatch.setattr(persona_v2_router_module, "global_config", fake_global)
    monkeypatch.setattr(persona_v2_router_module, "config_manager", SimpleNamespace(bot_config_path=bot_cfg_path))

    app = FastAPI()
    app.include_router(persona_v2_router_module.router)
    app.dependency_overrides[require_auth] = lambda: "test-token"
    return SimpleNamespace(
        client=TestClient(app), mgr=mgr, data_dir=data_dir, cfg=fake_global, bot_cfg_path=bot_cfg_path
    )


def test_list_cards_returns_six_and_registers_selectable_profiles(api_env) -> None:
    resp = api_env.client.get("/api/webui/persona-v2/cards")
    assert resp.status_code == 200
    body = resp.json()
    items = {c["card_id"]: c for c in body["cards"]}
    assert set(items) >= set(BUILTIN_CARDS)
    for card_id, name in BUILTIN_CARDS.items():
        item = items[card_id]
        assert item["name"] == name
        assert item["is_builtin"] is True
        assert item["experience_count"] > 0 and item["corpus_count"] > 0 and item["stage_count"] > 0
        assert item["card"]["schema"] == "maibot.persona.v2"
        assert set(item) >= {
            "card_id", "name", "role_tag", "soul_pitch", "avatar_url", "aliases",
            "experience_count", "corpus_count", "stage_count", "bound_targets", "card",
        }
    assert set(body["bindings"]) == {"active", "sister_younger", "sister_elder"}
    assert (api_env.data_dir / "maibot_xuyan.json").is_file()

    # 6 张卡片已注册为可选 PersonalityProfile（内存 + 磁盘）
    mem_profiles = {p.persona_v2_card_id: p for p in api_env.cfg.personality.profiles}
    assert set(mem_profiles) >= set(BUILTIN_CARDS)
    for card_id, name in BUILTIN_CARDS.items():
        assert mem_profiles[card_id].name == name
    doc = tomlkit.parse(api_env.bot_cfg_path.read_text(encoding="utf-8"))
    disk_ids = {p["persona_v2_card_id"] for p in doc["personality"]["profiles"]}
    assert disk_ids >= set(BUILTIN_CARDS)
    assert "# keep me" in api_env.bot_cfg_path.read_text(encoding="utf-8")
    # 磁盘配置可被 PersonalityConfig 正常解析
    PersonalityConfig(profiles=[PersonalityProfile(**p) for p in doc["personality"]["profiles"].unwrap()])

    # 幂等：再次调用不会重复注册
    count = len(api_env.cfg.personality.profiles)
    assert api_env.client.get("/api/webui/persona-v2/cards").status_code == 200
    assert len(api_env.cfg.personality.profiles) == count


def test_builtin_registration_preserves_existing_named_profile_and_backfills_card_id(api_env) -> None:
    existing = PersonalityProfile(
        name="许妍", personality="用户自定义", behavior_style="b", reply_style="r",
        group_chat_prompt="g", private_chat_prompts="p",
    )
    api_env.cfg.personality.profiles.append(existing)
    registered = persona_v2_router_module.ensure_builtin_cards_registered_sync()
    assert "maibot_xuyan" in registered
    xuyan = [p for p in api_env.cfg.personality.profiles if p.name == "许妍"]
    assert len(xuyan) == 1
    assert xuyan[0].personality == "用户自定义"
    assert xuyan[0].persona_v2_card_id == "maibot_xuyan"
    assert persona_v2_router_module.ensure_builtin_cards_registered_sync() == []


def test_get_card_detail(api_env) -> None:
    resp = api_env.client.get("/api/webui/persona-v2/cards/qianling_yun_suining")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"card", "mvu_state", "active_stage", "bound_targets", "bindings"}
    assert body["card"]["name"] == "云岁宁"
    assert set(body["mvu_state"]) >= set(PRIVATE_VARS) | {"mood", "current_activity"}
    assert body["active_stage"]["stage_id"]
    assert api_env.client.get("/api/webui/persona-v2/cards/not_exist").status_code == 404


def test_save_card_persists_and_validates(api_env) -> None:
    card = api_env.client.get("/api/webui/persona-v2/cards/maibot_xuyue").json()["card"]
    card["role_tag"] = "测试修改后的定位"
    card["schema_name"] = "maibot.persona.v2"  # 前端同时发送 schema 与 schema_name
    resp = api_env.client.put("/api/webui/persona-v2/cards/maibot_xuyue", json=card)
    assert resp.status_code == 200
    assert resp.json()["card"]["role_tag"] == "测试修改后的定位"
    saved = json.loads((api_env.data_dir / "maibot_xuyue.json").read_text(encoding="utf-8"))
    assert saved["role_tag"] == "测试修改后的定位"
    # 模板源文件不被修改
    assert _load_card("maibot_xuyue").role_tag != "测试修改后的定位"

    bad = dict(card)
    bad["mvu_config"] = {"variables": [{"key": "x", "label": "x", "type": "invalid"}]}
    assert api_env.client.put("/api/webui/persona-v2/cards/maibot_xuyue", json=bad).status_code == 422


def test_bind_card_to_slots(api_env) -> None:
    for target in ("active", "sister_younger", "sister_elder"):
        card_id = {"active": "qianling_yun_suining", "sister_younger": "maibot_xuyan", "sister_elder": "maibot_xuyue"}[
            target
        ]
        resp = api_env.client.post(f"/api/webui/persona-v2/cards/{card_id}/bind", json={"target": target})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["bindings"][target] == card_id
        assert target in body["bound_targets"]
    p_cfg = api_env.cfg.personality
    assert p_cfg.persona_v2_card_id == "qianling_yun_suining"
    assert p_cfg.selected_profile == "云岁宁"
    assert p_cfg.younger_slot_profile == "许妍"
    assert p_cfg.elder_slot_profile == "许玥"
    assert api_env.cfg.bot.nickname == "云岁宁"
    doc = tomlkit.parse(api_env.bot_cfg_path.read_text(encoding="utf-8"))
    assert doc["personality"]["persona_v2_card_id"] == "qianling_yun_suining"
    assert doc["personality"]["younger_slot_profile"] == "许妍"
    assert doc["personality"]["elder_slot_profile"] == "许玥"
    assert api_env.client.post(
        "/api/webui/persona-v2/cards/qianling_yun_ling/bind", json={"target": "bogus"}
    ).status_code == 422

    # 修改当前激活卡的别名应同步更新 global_config.bot.alias_names
    active_card = api_env.client.get("/api/webui/persona-v2/cards/qianling_yun_suining").json()["card"]
    active_card["aliases"] = ["岁宁", "宁姨", "家主"]
    upd_active = api_env.client.put("/api/webui/persona-v2/cards/qianling_yun_suining", json=active_card)
    assert upd_active.status_code == 200
    assert api_env.cfg.bot.alias_names == ["岁宁", "宁姨", "家主"]


def test_update_and_reset_mvu_state(api_env) -> None:
    resp = api_env.client.put(
        "/api/webui/persona-v2/cards/qianling_yun_xian/mvu-state",
        json={"values": {"affection": 150, "love_locked": True, "mood": "雀跃"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["card_id"] == "qianling_yun_xian"
    assert body["mvu_state"]["affection"] == 100  # schema max 截断
    assert body["mvu_state"]["love_locked"] is True
    assert body["mvu_state"]["mood"] == "雀跃"
    assert body["active_stage"]["require_love_locked"] is True

    detail = api_env.client.get("/api/webui/persona-v2/cards/qianling_yun_xian").json()
    assert detail["mvu_state"]["affection"] == 100

    reset = api_env.client.post("/api/webui/persona-v2/cards/qianling_yun_xian/mvu-reset", json={})
    assert reset.status_code == 200
    card = _load_card("qianling_yun_xian")
    defaults = {v.key: v.default_value for v in card.mvu_config.variables}
    assert reset.json()["mvu_state"]["affection"] == defaults["affection"]
    assert reset.json()["mvu_state"]["love_locked"] is False
    assert api_env.client.post("/api/webui/persona-v2/cards/nope/mvu-reset", json={}).status_code == 404


def test_create_and_delete_custom_card(api_env) -> None:
    src = json.loads((TEMPLATES_DIR / "maibot_xuyan.json").read_text(encoding="utf-8"))
    src["card_id"] = "custom_test_card"
    src["name"] = "测试自定义卡"
    resp = api_env.client.post("/api/webui/persona-v2/cards", json=src)
    assert resp.status_code == 200
    assert any(p.persona_v2_card_id == "custom_test_card" for p in api_env.cfg.personality.profiles)
    assert api_env.client.delete("/api/webui/persona-v2/cards/custom_test_card").status_code == 200
    assert api_env.client.delete("/api/webui/persona-v2/cards/custom_test_card").status_code == 404


# ─── e) Step 2 双子 Persona V2 与三层共享 MVU 引擎 (shared:twin_xu) 专项校验 ────


@pytest.mark.parametrize("card_id", ["maibot_xuyue", "maibot_xuyan"])
def test_twin_cards_data_and_template_parity_and_corpus_integrity(card_id: str) -> None:
    data_path = DATA_PERSONAS_DIR / f"{card_id}.json"
    tpl_path = TEMPLATES_DIR / f"{card_id}.json"
    assert data_path.is_file() and tpl_path.is_file()
    raw_data = json.loads(data_path.read_text(encoding="utf-8"))
    raw_tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
    assert raw_data == raw_tpl
    card = PersonaV2Card.model_validate(raw_data)
    assert len(card.past_experiences) == 14
    assert len(card.sample_corpora) == 32
    assert len(card.relationship_stages) == 6
    assert card.mvu_config.shared_namespace == "twin_xu"
    corpus_ids = {c.id for c in card.sample_corpora}
    assert len(corpus_ids) == 32
    for stage in card.relationship_stages:
        assert stage.unlocked_corpus_ids
        for uid in stage.unlocked_corpus_ids:
            assert uid in corpus_ids, f"{card_id}.{stage.stage_id} 存在未定义的 unlocked_corpus_id: {uid}"


def test_shared_twin_xu_cross_card_sync_and_fail_closed(truth_db: CompanionTruthDB) -> None:
    xuyue = _load_card("maibot_xuyue")
    xuyan = _load_card("maibot_xuyan")

    # 在 maibot_xuyue 上更新跨卡共享变量 apartment_scene (PRIVATE) 与 love_locked (PRIVATE, shared_key=twin_harmony_locked)
    new_scene = "主卧浴缸已放满苦橙精油热水，许玥把吧台共用手机交接给刚走出琴房的许妍"
    res = MVURuntimeEngine.apply_turn_updates(
        truth_db,
        xuyue,
        [
            _patch(
                {"op": "replace", "path": "/apartment_scene", "value": new_scene},
                {"op": "replace", "path": "/love_locked", "value": True},
            )
        ],
        is_owner_private=True,
    )
    assert res.variables["apartment_scene"] == new_scene
    assert res.variables["love_locked"] is True

    # 立即加载 maibot_xuyan（Owner 私聊）：共享的 apartment_scene 与 love_locked 同步生效，而独立变量 affection 互不干扰
    xuyan_priv = MVURuntimeEngine.load_turn_variables(truth_db, xuyan, is_owner_private=True)
    assert xuyan_priv["apartment_scene"] == new_scene
    assert xuyan_priv["love_locked"] is True
    assert xuyan_priv["affection"] == 25  # 保持许妍独立默认值

    # Fail-Closed（非 Owner / 群聊）：apartment_scene 与 love_locked 均严格隔离，严禁向群聊泄露公寓私密切片
    xuyan_pub = MVURuntimeEngine.load_turn_variables(truth_db, xuyan, is_owner_private=False)
    assert "apartment_scene" not in xuyan_pub
    assert "love_locked" not in xuyan_pub
    assert "affection" not in xuyan_pub
    shared_pub_rows = truth_db.get_mvu_variables("shared:twin_xu", is_owner_private=False)
    assert shared_pub_rows == {}

    # 兼容旧库平滑收紧：若旧库存在 GLOBAL_PUBLIC 的 apartment_scene 行，ensure_mvu_defaults 自动迁移至 OWNER_PRIVATE 并清理旧行
    truth_db.upsert_mvu_variable(
        "shared:twin_xu",
        scope_key="GLOBAL_PUBLIC",
        var_key="apartment_scene",
        var_type="string",
        var_value="旧库遗留公开切片",
        visibility="PUBLIC",
    )
    MVURuntimeEngine.ensure_card_mvu_defaults(truth_db, xuyan)
    assert truth_db.get_mvu_variables("shared:twin_xu", is_owner_private=False) == {}
    assert truth_db.get_mvu_variables("shared:twin_xu", is_owner_private=True)["apartment_scene"] == new_scene

    # 重置后共享命名空间同步恢复默认值
    MVURuntimeEngine.reset_card_mvu_variables(truth_db, xuyue)
    after_reset = MVURuntimeEngine.load_turn_variables(truth_db, xuyan, is_owner_private=True)
    assert after_reset["love_locked"] is False


def test_public_mvu_update_instruction_hides_custom_instr_and_private_examples(
    truth_db: CompanionTruthDB,
) -> None:
    """非 Owner 会话严禁暴露 update_instruction（含金主/亲密细则）、/affection 或错误的 /current_mood 示例。"""
    for card_id in ("maibot_xuyan", "maibot_xuyue"):
        card = _load_card(card_id)
        pub_block = MVURuntimeEngine.build_update_instruction_block(card, is_owner_private=False)
        assert pub_block
        assert "金主" not in pub_block
        assert "打屁股" not in pub_block
        assert "affection" not in pub_block
        assert "apartment_scene" not in pub_block
        assert "/current_mood" not in pub_block
        assert '"/mood"' in pub_block

        priv_block = MVURuntimeEngine.build_update_instruction_block(card, is_owner_private=True)
        assert "角色专属变量演进细则" in priv_block
        assert '"/affection"' in priv_block
        assert "/current_mood" not in priv_block

        # 确认 6 张内置卡 planner_summary 均无 Owner 私密泄露
    forbidden_tokens = ("paff", "金主", "打屁股", "挠腰窝", "泌乳", "真空", "充血", "潜令", "恋爱锁定", "{{user}}", "Owner")
    for card_id in BUILTIN_CARDS:
        card = _load_card(card_id)
        for token in forbidden_tokens:
            assert token.lower() not in card.planner_summary.lower(), (
                f"{card_id}.planner_summary 泄露敏感词: {token}"
            )


def test_affinity_guard_clamps_cumulative_multi_patch_in_single_turn(
    truth_db: CompanionTruthDB,
) -> None:
    """同一轮中多条 affection 补丁叠加或重复来源输入，累计净变化严格受限于 [-1, +5]。"""
    card = _load_card("maibot_xuyan")
    _set_private(truth_db, card, "affection", "number", 20)

    # 1. 同轮两条 +4 补丁，累计不得超过 +5（应为 25 而非 28）
    res_pos = MVURuntimeEngine.apply_turn_updates(
        truth_db,
        card,
        [
            _patch(
                {"op": "delta", "path": "/affection", "value": 4},
                {"op": "delta", "path": "/affection", "value": 4},
            )
        ],
        is_owner_private=True,
    )
    assert res_pos.variables["affection"] == 25
    assert len(res_pos.changes) == 1
    assert res_pos.changes[0].old == 20
    assert res_pos.changes[0].new == 25
    assert res_pos.changes[0].delta == 5

    # 2. 同轮两条 -1 补丁，累计不得低于 -1（应为 24 而非 23）
    res_neg = MVURuntimeEngine.apply_turn_updates(
        truth_db,
        card,
        [
            _patch(
                {"op": "delta", "path": "/affection", "value": -1},
                {"op": "delta", "path": "/affection", "value": -1},
            )
        ],
        is_owner_private=True,
    )
    assert res_neg.variables["affection"] == 24
    assert len(res_neg.changes) == 1
    assert res_neg.changes[0].delta == -1

    # 3. 相同 raw_sources 传入两次时自动去重，仅结算一次 (+2 -> 26)
    dup_src = "好的呀" + _patch({"op": "delta", "path": "/affection", "value": 2})
    res_dup = MVURuntimeEngine.apply_turn_updates(
        truth_db,
        card,
        [dup_src, dup_src],
        is_owner_private=True,
    )
    assert res_dup.variables["affection"] == 26
    assert len(res_dup.changes) == 1
    assert res_dup.changes[0].delta == 2


def test_system_readonly_variables_injection_and_update_immunity(truth_db: CompanionTruthDB) -> None:
    xuyue = _load_card("maibot_xuyue")
    xuyan = _load_card("maibot_xuyan")

    # 1. 验证算力档位与持机槽位映射并注入 <status_current_variables>
    with truth_db.conn:
        truth_db.conn.execute(
            "INSERT OR REPLACE INTO global_state (row_id, fairy_compute_tier, active_persona_slot, active_sister) "
            "VALUES (1, 'SUPERCHARGED', 'sister_elder', 'sister_elder')"
        )
    compiled_yue = MVURuntimeEngine.compile_turn_persona_and_mvu(
        xuyue, db=truth_db, is_owner_private=True
    )
    assert compiled_yue.mvu_values["balance_tier"] == "high"
    assert compiled_yue.mvu_values["phone_holder"] == "xuyue"
    assert '- balance_tier (算力与生活费水位): "high" [type=string, system_readonly=true]' in compiled_yue.mvu_state_block
    assert '- phone_holder (当前吧台共用手机持有者): "xuyue" [type=string, system_readonly=true]' in compiled_yue.mvu_state_block
    assert "`/balance_tier`" not in compiled_yue.mvu_update_instruction_block
    assert "`/phone_holder`" not in compiled_yue.mvu_update_instruction_block

    compiled_yan_low = MVURuntimeEngine.compile_turn_persona_and_mvu(
        xuyan, db=truth_db, is_owner_private=False, persona_slot="sister_younger", compute_tier="THRIFTY"
    )
    assert compiled_yan_low.mvu_values["balance_tier"] == "low"
    assert compiled_yan_low.mvu_values["phone_holder"] == "xuyan"
    assert '- balance_tier (算力与生活费水位): "low"' in compiled_yan_low.mvu_state_block
    assert '- phone_holder (当前吧台共用手机持有者): "xuyan"' in compiled_yan_low.mvu_state_block

    # 2. 验证 <UpdateVariable> 无法篡改只读系统变量，也不会写入 mvu_variables 表
    res = MVURuntimeEngine.apply_turn_updates(
        truth_db,
        xuyue,
        [
            _patch(
                {"op": "replace", "path": "/balance_tier", "value": "hacked"},
                {"op": "replace", "path": "/phone_holder", "value": "nobody"},
                {"op": "delta", "path": "/affection", "value": 2},
            )
        ],
        is_owner_private=True,
    )
    assert [c.key for c in res.changes] == ["affection"]
    assert res.variables["balance_tier"] == "high"
    assert res.variables["phone_holder"] == "xuyue"
    stored_db = truth_db.get_mvu_variables(xuyue.card_id, is_owner_private=True)
    assert "balance_tier" not in stored_db and "phone_holder" not in stored_db


def test_twin_float_stage_resolution_and_compound_mvu_conditions(truth_db: CompanionTruthDB) -> None:
    xuyue = _load_card("maibot_xuyue")
    xuyan = _load_card("maibot_xuyan")

    for card in (xuyue, xuyan):
        # 19.5 -> stage_0_19（消除整数区间浮点空隙）
        s_19_5 = MVURuntimeEngine.resolve_active_stage(
            card, {"affection": 19.5, "love_locked": False}, is_owner_private=True
        )
        assert s_19_5 is not None and s_19_5.stage_id == "stage_0_19"

        # 100.0 + love_locked=False -> stage_80_100
        s_100_unlocked = MVURuntimeEngine.resolve_active_stage(
            card, {"affection": 100.0, "love_locked": False}, is_owner_private=True
        )
        assert s_100_unlocked is not None and s_100_unlocked.stage_id == "stage_80_100"

        # 100.5 + love_locked=True -> stage_101_120_harmony
        s_100_5_locked = MVURuntimeEngine.resolve_active_stage(
            card, {"affection": 100.5, "love_locked": True}, is_owner_private=True
        )
        assert s_100_5_locked is not None and s_100_5_locked.stage_id == "stage_101_120_harmony"

    # 复合 mvu_condition 判定：affection >= 60 and control_tension >= 50
    cond = "affection >= 60 and control_tension >= 50"
    assert evaluate_mvu_condition(cond, {"affection": 65.0, "control_tension": 30.0}) is False
    assert evaluate_mvu_condition(cond, {"affection": 55.0, "control_tension": 70.0}) is False
    assert evaluate_mvu_condition(cond, {"affection": 60.0, "control_tension": 50.0}) is True

    # 编译召回校验：control_tension 不足 50 时不召回 intimacy_remote_t1，达标后正常召回
    low_tension_turn = PersonaV2Engine.compile_for_turn(
        xuyue,
        is_owner_private=True,
        recent_text="关灯 耳机 听你的",
        mvu_values={"affection": 70.0, "control_tension": 20.0, "love_locked": False},
    )
    assert "intimacy_remote_t1" not in low_tension_turn.matched_corpus_ids

    high_tension_turn = PersonaV2Engine.compile_for_turn(
        xuyue,
        is_owner_private=True,
        recent_text="关灯 耳机 听你的",
        mvu_values={"affection": 70.0, "control_tension": 60.0, "love_locked": False},
    )
    assert "intimacy_remote_t1" in high_tension_turn.matched_corpus_ids


def test_persona_v2_full_four_layer_loading_by_scene(truth_db: CompanionTruthDB) -> None:
    """校验场景感知四层全量加载：短消息 '111' 下公私域物理隔离、全量经历/语料与阶段全景概览。"""
    for card_id in ("maibot_xuyan", "maibot_xuyue"):
        card = _load_card(card_id)
        general_exps = [e for e in card.past_experiences if e.scope == "general"]
        general_dyn_exps = [e for e in general_exps if not e.constant]
        general_corpora = [c for c in card.sample_corpora if c.scope == "general"]
        assert len(general_exps) == 5
        assert len(general_corpora) == 8
        assert len(card.past_experiences) == 14

        # 1. 非 Owner / 群聊场景（短消息 "111"）：全量加载 5 条通用经历 + 8 条通用语料，0 owner_only 泄露
        pub_turn = MVURuntimeEngine.compile_turn_persona_and_mvu(
            card, db=truth_db, is_owner_private=False, recent_text="111"
        )
        pub_prompt = "\n".join(
            [
                pub_turn.static_core_block,
                pub_turn.dynamic_tail_block,
                pub_turn.planner_summary_block,
                pub_turn.planner_d0_guidance_block,
                pub_turn.mvu_state_block,
                pub_turn.mvu_update_instruction_block,
            ]
        )
        pub_layers_text = f"{pub_turn.static_core_block}\n{pub_turn.dynamic_tail_block}"
        assert card.core_persona.soul_pitch.strip() in pub_turn.static_core_block
        assert card.core_persona.basic_info.strip() in pub_turn.static_core_block
        assert pub_turn.matched_experience_ids == [e.id for e in general_dyn_exps]
        for exp in general_exps:
            assert exp.title in pub_layers_text
            assert exp.narrative.strip() in pub_layers_text
        assert len(pub_turn.matched_corpus_ids) == 8
        assert set(pub_turn.matched_corpus_ids) == {c.id for c in general_corpora}
        for corpus in general_corpora:
            assert corpus.situation_title in pub_turn.dynamic_tail_block
        assert pub_turn.dynamic_tail_block.count(ANTI_PARROTING_DIRECTIVE) == 2

        assert "<owner_private_persona_overlay>" not in pub_prompt
        assert "<relationship_stages_overview>" not in pub_prompt
        assert "<active_relationship_stage" not in pub_prompt
        assert pub_turn.active_stage is None
        for frag in _owner_only_fragments(card):
            assert frag not in pub_prompt, f"{card_id} 群聊泄露 owner_only 片段: {frag[:40]}"
        for key in _private_var_keys(card):
            assert key not in pub_turn.mvu_values
            assert f"`/{key}`" not in pub_prompt

        # 2. Owner 私聊场景（短消息 "111"）：全量 4 层 + <relationship_stages_overview> + 全量 14 经历 + 条件合格语料
        priv_turn = MVURuntimeEngine.compile_turn_persona_and_mvu(
            card, db=truth_db, is_owner_private=True, recent_text="111"
        )
        priv_layers_text = f"{priv_turn.static_core_block}\n{priv_turn.dynamic_tail_block}"
        assert priv_turn.static_core_block == pub_turn.static_core_block
        assert "<owner_private_persona_overlay>" in priv_turn.dynamic_tail_block
        assert card.core_persona.owner_relationship_bond.strip() in priv_turn.dynamic_tail_block
        assert card.core_persona.owner_private_wardrobe_and_anchors.strip() in priv_turn.dynamic_tail_block

        overview_idx = priv_turn.dynamic_tail_block.index("<relationship_stages_overview>")
        active_stage_idx = priv_turn.dynamic_tail_block.index("<active_relationship_stage")
        assert overview_idx < active_stage_idx
        for stage in card.relationship_stages:
            assert stage.stage_id in priv_turn.dynamic_tail_block
            assert stage.stage_name in priv_turn.dynamic_tail_block
            assert f"{stage.min_affection}~{stage.max_affection}" in priv_turn.dynamic_tail_block
            assert stage.tsundere_to_honesty_ratio.strip() in priv_turn.dynamic_tail_block
            assert " ".join(stage.palette_override.split()) in priv_turn.dynamic_tail_block

        all_dyn_exps = [e for e in card.past_experiences if not e.constant]
        assert set(priv_turn.matched_experience_ids) == {e.id for e in all_dyn_exps}
        for exp in card.past_experiences:
            assert exp.title in priv_layers_text
            assert exp.narrative.strip() in priv_layers_text

        expected_default_corpora = [
            c.id
            for c in card.sample_corpora
            if not c.mvu_condition or evaluate_mvu_condition(c.mvu_condition, priv_turn.mvu_values)
        ]
        assert set(priv_turn.matched_corpus_ids) == set(expected_default_corpora)
        assert "[✨ 当前阶段核心语料]" in priv_turn.dynamic_tail_block
        assert priv_turn.dynamic_tail_block.count(ANTI_PARROTING_DIRECTIVE) == 2

        # 3. 高好感恋爱锁定态下，所有满足 mvu_condition 的语料应全部载入
        elevated_mvu = {
            "affection": 105.0,
            "love_locked": True,
            "intimacy_tension": 70.0,
            "control_tension": 70.0,
        }
        elevated_stage = MVURuntimeEngine.resolve_active_stage(
            card, elevated_mvu, is_owner_private=True
        )
        elevated_turn = PersonaV2Engine.compile_for_turn(
            card,
            is_owner_private=True,
            recent_text="111",
            mvu_values=elevated_mvu,
            active_stage=elevated_stage,
        )
        expected_elevated_corpora = [
            c.id
            for c in card.sample_corpora
            if not c.mvu_condition or evaluate_mvu_condition(c.mvu_condition, elevated_mvu)
        ]
        assert set(elevated_turn.matched_corpus_ids) == set(expected_elevated_corpora)
        assert len(elevated_turn.matched_corpus_ids) >= 31

    # 4. 关键词优先置顶标签、阶段核心其次、其余合格语料随后；<=0 不限制；显式正整数上限截断
    xuyan_full = _load_card("maibot_xuyan")
    kw_exp = next(e for e in xuyan_full.past_experiences if e.id == "xuyan_vignette_07")
    kw_corpus = next(c for c in xuyan_full.sample_corpora if c.id == "xuyan_echo_05")
    hit_text = f"{kw_exp.trigger_keywords[0]} {kw_corpus.trigger_keywords[0]}"
    kw_turn = PersonaV2Engine.compile_for_turn(
        xuyan_full,
        is_owner_private=True,
        recent_text=hit_text,
        mvu_values={"affection": 25.0, "love_locked": False},
        active_stage=xuyan_full.relationship_stages[0],  # stage_0_19 仅解锁 echo_01..08，flirt_01..03 走其余合格语料层
        max_dynamic_experiences=0,
        max_sample_corpora=-1,
    )
    assert len(kw_turn.matched_experience_ids) == 10
    assert kw_exp.id in kw_turn.matched_experience_ids[:2]
    assert f"- [🔥 当前话题强相关] 《{kw_exp.title}》" in kw_turn.dynamic_tail_block
    assert kw_turn.matched_corpus_ids[0] == kw_corpus.id
    assert f"- [🔥 当前话题优先化用] 情境《{kw_corpus.situation_title}》" in kw_turn.dynamic_tail_block
    assert "[✨ 当前阶段核心语料] 情境《" in kw_turn.dynamic_tail_block
    # 未在 stage_0_19 unlocked_set 中的 xuyan_flirt_01 仍会被作为第三层全量补入，且排在阶段核心语料之后
    assert kw_turn.matched_corpus_ids[-3:] == ["xuyan_flirt_01", "xuyan_flirt_02", "xuyan_flirt_03"]
    assert kw_turn.dynamic_tail_block.index("[🔥 当前话题优先化用]") < kw_turn.dynamic_tail_block.index(
        "[✨ 当前阶段核心语料]"
    )

    capped_turn = PersonaV2Engine.compile_for_turn(
        xuyan_full,
        is_owner_private=True,
        recent_text=hit_text,
        mvu_values={"affection": 25.0, "love_locked": False},
        max_dynamic_experiences=2,
        max_sample_corpora=3,
    )
    assert len(capped_turn.matched_experience_ids) == 2
    assert kw_exp.id in capped_turn.matched_experience_ids
    assert len(capped_turn.matched_corpus_ids) == 3
    assert capped_turn.matched_corpus_ids[0] == kw_corpus.id

    # 若卡片包含 general 阶段，非 Owner 会话仅在 <relationship_stages_overview> 暴露该 general 阶段
    mixed_card = xuyan_full.model_copy(deep=True)
    mixed_card.relationship_stages[0].scope = "general"
    mixed_pub = PersonaV2Engine.compile_for_turn(
        mixed_card,
        is_owner_private=False,
        recent_text="111",
        active_stage=mixed_card.relationship_stages[0],
    )
    assert "<relationship_stages_overview>" in mixed_pub.dynamic_tail_block
    assert mixed_card.relationship_stages[0].stage_id in mixed_pub.dynamic_tail_block
    for priv_stage in mixed_card.relationship_stages[1:]:
        assert priv_stage.stage_id not in mixed_pub.dynamic_tail_block
