"""Persona V2 角色卡工作台与原生 MVU 状态机管理 REST API 路由。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional
import asyncio
import os
import tempfile

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
import tomlkit

from src.common.logger import get_logger
from src.companion_vault import runtime_bridge
from src.companion_vault.mvu_engine import MVURuntimeEngine
from src.companion_vault.persona_v2_engine import (
    PersonaV2Card,
    RelationshipStageOverlay,
    get_persona_v2_manager,
)
from src.companion_vault.truth_db import CompanionTruthDB
from src.config.config import config_manager, global_config
from src.config.official_configs import PersonalityProfile
from src.config.personality_service import profile_data
from src.webui.dependencies import require_auth

logger = get_logger("webui.persona_v2")

router = APIRouter(
    prefix="/api/webui/persona-v2",
    tags=["PersonaV2"],
    dependencies=[Depends(require_auth)],
)


class BindCardRequest(BaseModel):
    target: Literal["active", "sister_younger", "sister_elder"] = "active"


class UpdateMVUStateRequest(BaseModel):
    values: Dict[str, Any] = Field(default_factory=dict)


CardBody = Annotated[Dict[str, Any], Body()]
BindBody = Annotated[BindCardRequest, Body()]
MVUStateBody = Annotated[UpdateMVUStateRequest, Body()]


def _open_truth_db() -> CompanionTruthDB:
    truth_db_path = (
        os.environ.get("MAIBOT_TRUTH_DB_PATH", "").strip()
        or runtime_bridge.DEFAULT_TRUTH_DB_PATH
    )
    os.makedirs(os.path.dirname(truth_db_path) or ".", exist_ok=True)
    return runtime_bridge._open_truth_db(truth_db_path)


def _serialize_stage_info(stage: Optional[RelationshipStageOverlay]) -> Optional[Dict[str, Any]]:
    if stage is None:
        return None
    return {
        "stage_id": stage.stage_id,
        "stage_name": stage.stage_name,
        "min_affection": stage.min_affection,
        "max_affection": stage.max_affection,
        "require_love_locked": stage.require_love_locked,
    }


def _load_card_mvu_state(card: PersonaV2Card) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    db: Optional[CompanionTruthDB] = None
    try:
        db = _open_truth_db()
        mvu_state = MVURuntimeEngine.load_turn_variables(db, card, is_owner_private=True)
        active_stage = MVURuntimeEngine.resolve_active_stage(
            card, mvu_state, is_owner_private=True
        )
        return mvu_state, _serialize_stage_info(active_stage)
    except Exception as exc:
        logger.warning(f"读取角色卡 {card.card_id} 的实时 MVU 状态失败: {exc}")
        fallback_state = {v.key: v.default_value for v in card.mvu_config.variables}
        active_stage = MVURuntimeEngine.resolve_active_stage(
            card, fallback_state, is_owner_private=True
        )
        return fallback_state, _serialize_stage_info(active_stage)
    finally:
        if db is not None:
            db.close()


def _resolve_current_bindings(all_cards: Optional[List[PersonaV2Card]] = None) -> Dict[str, str]:
    """解析当前主配置、妹妹槽位与姐姐槽位各自绑定的 card_id。"""
    p_cfg = global_config.personality
    cards = all_cards if all_cards is not None else get_persona_v2_manager().list_cards()
    name_to_card_id = {c.name.strip(): c.card_id for c in cards if c.name.strip()}
    id_set = {c.card_id for c in cards}

    def _card_id_for_profile_name(prof_name: str) -> str:
        clean_name = str(prof_name or "").strip()
        if not clean_name:
            return ""
        for prof in p_cfg.profiles:
            if prof.name == clean_name:
                cid = str(getattr(prof, "persona_v2_card_id", "") or "").strip()
                if cid:
                    return cid
                if prof.name in name_to_card_id:
                    return name_to_card_id[prof.name]
                if prof.nickname and prof.nickname.strip() in name_to_card_id:
                    return name_to_card_id[prof.nickname.strip()]
        if clean_name in id_set:
            return clean_name
        return name_to_card_id.get(clean_name, "")

    active_cid = str(getattr(p_cfg, "persona_v2_card_id", "") or "").strip()
    if not active_cid and p_cfg.selected_profile:
        active_cid = _card_id_for_profile_name(p_cfg.selected_profile)

    younger_cid = _card_id_for_profile_name(p_cfg.younger_slot_profile)
    if not younger_cid and p_cfg.profiles:
        first_prof = p_cfg.profiles[0]
        younger_cid = (
            str(getattr(first_prof, "persona_v2_card_id", "") or "").strip()
            or name_to_card_id.get(first_prof.name.strip(), "")
        )

    elder_cid = _card_id_for_profile_name(p_cfg.elder_slot_profile)
    if not elder_cid and len(p_cfg.profiles) >= 2:
        second_prof = p_cfg.profiles[1]
        elder_cid = (
            str(getattr(second_prof, "persona_v2_card_id", "") or "").strip()
            or name_to_card_id.get(second_prof.name.strip(), "")
        )

    return {
        "active": active_cid,
        "sister_younger": younger_cid,
        "sister_elder": elder_cid,
    }


def _bound_targets_for_card(card_id: str, bindings: Dict[str, str]) -> List[str]:
    targets: List[str] = []
    for key in ("active", "sister_younger", "sister_elder"):
        if bindings.get(key) == card_id:
            targets.append(key)
    return targets


def _card_to_profile_dict(card: PersonaV2Card, existing: Optional[PersonalityProfile] = None) -> Dict[str, Any]:
    legacy = card.to_legacy_profile_fields()
    cp = card.core_persona
    owner_parts = [
        p.strip()
        for p in (
            cp.owner_relationship_bond,
            cp.owner_private_wardrobe_and_anchors,
            cp.personality_palette.affection_leak_style,
            cp.personality_palette.desire_and_intimacy_drive,
        )
        if p and p.strip()
    ]
    owner_prompt = "\n".join(owner_parts)
    group_prompt = (
        cp.public_interaction_baseline.strip()
        or (existing.group_chat_prompt if existing else global_config.chat.reply_style.group_chat_prompt)
    )
    private_prompt = (
        existing.private_chat_prompts
        if existing and existing.private_chat_prompts.strip()
        else global_config.chat.reply_style.private_chat_prompts
    )
    return {
        "name": existing.name if existing else card.name.strip() or card.card_id,
        "personality": legacy["personality"] or "是人类。",
        "behavior_style": legacy["plan_style"] or legacy["personality"] or "自然交流。",
        "reply_style": legacy["reply_style"] or "自然口语化回复。",
        "group_chat_prompt": group_prompt,
        "private_chat_prompts": private_prompt,
        "multiple_reply_style": list(existing.multiple_reply_style) if existing else [],
        "multiple_probability": float(existing.multiple_probability) if existing else 0.0,
        "nickname": legacy["nickname"] or card.name.strip(),
        "alias_names": list(legacy["alias_names"]),
        "owner_private_prompt": owner_prompt,
        "st_preset_file": existing.st_preset_file if existing else "",
        "persona_v2_card_id": card.card_id,
        "mvu_enabled": bool(card.mvu_config.enabled),
    }


def _sync_card_to_bot_config_sync(
    card: PersonaV2Card,
    *,
    ensure_profile_registered: bool = False,
    bind_target: Optional[Literal["active", "sister_younger", "sister_elder"]] = None,
) -> bool:
    """在工作线程中将 PersonaV2Card 同步至内存 global_config 与磁盘 bot_config.toml。"""
    p_cfg = global_config.personality
    matched_idx: Optional[int] = None
    for idx, prof in enumerate(p_cfg.profiles):
        if (
            str(getattr(prof, "persona_v2_card_id", "") or "").strip() == card.card_id
            or prof.name.strip() == card.name.strip()
        ):
            matched_idx = idx
            break

    changed = False
    target_profile: Optional[PersonalityProfile] = None

    if matched_idx is not None:
        existing_prof = p_cfg.profiles[matched_idx]
        updated_dict = _card_to_profile_dict(card, existing=existing_prof)
        target_profile = PersonalityProfile(**updated_dict)
        p_cfg.profiles[matched_idx] = target_profile
        changed = True
    elif ensure_profile_registered or bind_target is not None:
        new_dict = _card_to_profile_dict(card, existing=None)
        # 防止重名冲突
        existing_names = {p.name for p in p_cfg.profiles}
        base_name = new_dict["name"]
        if base_name in existing_names:
            new_dict["name"] = f"{base_name} ({card.card_id})"
        target_profile = PersonalityProfile(**new_dict)
        p_cfg.profiles.append(target_profile)
        changed = True

    sync_bot_identity = False
    if bind_target is not None and target_profile is not None:
        if bind_target == "active":
            p_cfg.selected_profile = target_profile.name
            p_cfg.persona_v2_card_id = card.card_id
            p_cfg.mvu_enabled = bool(card.mvu_config.enabled)
            p_cfg.personality = target_profile.personality
            p_cfg.behavior_style = target_profile.behavior_style
            p_cfg.reply_style = target_profile.reply_style
            p_cfg.owner_private_prompt = target_profile.owner_private_prompt
            sync_bot_identity = True
            changed = True
        elif bind_target == "sister_younger":
            p_cfg.younger_slot_profile = target_profile.name
            changed = True
        elif bind_target == "sister_elder":
            p_cfg.elder_slot_profile = target_profile.name
            changed = True
    elif target_profile is not None and (
        p_cfg.selected_profile == target_profile.name
        or str(getattr(p_cfg, "persona_v2_card_id", "") or "").strip() == card.card_id
    ):
        p_cfg.persona_v2_card_id = card.card_id
        p_cfg.mvu_enabled = bool(card.mvu_config.enabled)
        p_cfg.personality = target_profile.personality
        p_cfg.behavior_style = target_profile.behavior_style
        p_cfg.reply_style = target_profile.reply_style
        p_cfg.owner_private_prompt = target_profile.owner_private_prompt
        sync_bot_identity = True
        changed = True

    if sync_bot_identity and target_profile is not None:
        if target_profile.nickname.strip():
            global_config.bot.nickname = target_profile.nickname.strip()
        global_config.bot.alias_names = list(target_profile.alias_names)

    if not changed:
        return False

    bot_cfg_path: Optional[Path] = getattr(config_manager, "bot_config_path", None)
    if bot_cfg_path is not None and bot_cfg_path.is_file():
        try:
            document = tomlkit.parse(bot_cfg_path.read_text(encoding="utf-8"))
            if "personality" not in document:
                document["personality"] = tomlkit.table()
            section = document["personality"]
            section["profiles"] = [profile_data(p) for p in p_cfg.profiles]
            section["selected_profile"] = p_cfg.selected_profile
            section["younger_slot_profile"] = p_cfg.younger_slot_profile
            section["elder_slot_profile"] = p_cfg.elder_slot_profile
            section["persona_v2_card_id"] = p_cfg.persona_v2_card_id
            section["mvu_enabled"] = bool(p_cfg.mvu_enabled)
            section["personality"] = p_cfg.personality
            section["behavior_style"] = p_cfg.behavior_style
            section["reply_style"] = p_cfg.reply_style
            section["owner_private_prompt"] = p_cfg.owner_private_prompt

            if sync_bot_identity and target_profile is not None and "bot" in document:
                bot_sec = document["bot"]
                if isinstance(bot_sec, dict):
                    if target_profile.nickname.strip():
                        bot_sec["nickname"] = target_profile.nickname.strip()
                    bot_sec["alias_names"] = [str(a) for a in target_profile.alias_names]

            temporary_path: Optional[Path] = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=bot_cfg_path.parent, delete=False
                ) as tmp:
                    temporary_path = Path(tmp.name)
                    tmp.write(tomlkit.dumps(document))
                os.replace(temporary_path, bot_cfg_path)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(f"同步 Persona V2 配置至 bot_config.toml 失败（内存态已生效）: {exc}")

    return True


def _persist_personality_profiles_sync() -> None:
    """仅将 personality.profiles 列表原子写回 bot_config.toml（保留其余配置与注释）。"""
    bot_cfg_path: Optional[Path] = getattr(config_manager, "bot_config_path", None)
    if bot_cfg_path is None or not bot_cfg_path.is_file():
        return
    document = tomlkit.parse(bot_cfg_path.read_text(encoding="utf-8"))
    if "personality" not in document:
        document["personality"] = tomlkit.table()
    document["personality"]["profiles"] = [profile_data(p) for p in global_config.personality.profiles]
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=bot_cfg_path.parent, delete=False
        ) as tmp:
            temporary_path = Path(tmp.name)
            tmp.write(tomlkit.dumps(document))
        os.replace(temporary_path, bot_cfg_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def ensure_builtin_cards_registered_sync(cards: Optional[List[PersonaV2Card]] = None) -> List[str]:
    """
    幂等地将内置模板 PersonaV2Card 注册为可选 PersonalityProfile：
    - 已存在同 card_id 或同名 Profile 的卡片保持原样（不覆盖用户已调校的内容）；
    - 仅追加缺失的卡片，并一次性原子持久化到 bot_config.toml。
    返回本次新注册的 card_id 列表。
    """
    mgr = get_persona_v2_manager()
    all_cards = cards if cards is not None else mgr.list_cards()
    p_cfg = global_config.personality
    registered: List[str] = []
    for card in all_cards:
        if not mgr.is_builtin_template(card.card_id):
            continue
        matched_prof = next(
            (
                prof
                for prof in p_cfg.profiles
                if str(getattr(prof, "persona_v2_card_id", "") or "").strip() == card.card_id
                or prof.name.strip() == card.name.strip()
            ),
            None,
        )
        if matched_prof is not None:
            if not str(getattr(matched_prof, "persona_v2_card_id", "") or "").strip():
                matched_prof.persona_v2_card_id = card.card_id
                matched_prof.mvu_enabled = bool(card.mvu_config.enabled)
                registered.append(card.card_id)
            continue
        new_dict = _card_to_profile_dict(card, existing=None)
        existing_names = {p.name for p in p_cfg.profiles}
        if new_dict["name"] in existing_names:
            new_dict["name"] = f"{new_dict['name']} ({card.card_id})"
        p_cfg.profiles.append(PersonalityProfile(**new_dict))
        registered.append(card.card_id)

    if registered:
        try:
            _persist_personality_profiles_sync()
        except Exception as exc:
            logger.warning(f"持久化内置 Persona V2 角色卡 Profile 失败（内存态已生效）: {exc}")
        logger.info(f"已将内置 Persona V2 角色卡注册为可选人设: {registered}")
    return registered


def _build_card_detail_response(card: PersonaV2Card) -> Dict[str, Any]:
    mvu_state, active_stage = _load_card_mvu_state(card)
    bindings = _resolve_current_bindings()
    bound_targets = _bound_targets_for_card(card.card_id, bindings)
    return {
        "card": card.model_dump(by_alias=True, mode="json"),
        "mvu_state": mvu_state,
        "active_stage": active_stage,
        "bound_targets": bound_targets,
        "bindings": bindings,
    }


@router.get("/cards")
async def list_persona_v2_cards() -> Dict[str, Any]:
    """获取全部 Persona V2 角色卡摘要及完整数据（自动从模板目录幂等初始化）。"""
    mgr = get_persona_v2_manager()
    cards = await asyncio.to_thread(mgr.list_cards)
    try:
        await asyncio.to_thread(ensure_builtin_cards_registered_sync, cards)
    except Exception as exc:
        logger.warning(f"注册内置 Persona V2 角色卡为可选人设失败: {exc}")
    bindings = _resolve_current_bindings(cards)
    items: List[Dict[str, Any]] = []
    for c in cards:
        bound_targets = _bound_targets_for_card(c.card_id, bindings)
        items.append(
            {
                "card_id": c.card_id,
                "name": c.name,
                "role_tag": c.role_tag,
                "soul_pitch": c.core_persona.soul_pitch or c.planner_summary,
                "avatar_url": c.avatar_url,
                "aliases": list(c.aliases),
                "experience_count": len(c.past_experiences),
                "corpus_count": len(c.sample_corpora),
                "stage_count": len(c.relationship_stages),
                "bound_targets": bound_targets,
                "is_builtin": mgr.is_builtin_template(c.card_id),
                "card": c.model_dump(by_alias=True, mode="json"),
            }
        )
    return {
        "cards": items,
        "bindings": bindings,
    }


@router.get("/cards/{card_id}")
async def get_persona_v2_card(card_id: str) -> Dict[str, Any]:
    """获取指定 PersonaV2Card 完整定义、实时 MVU 变量状态与当前激活阶段。"""
    mgr = get_persona_v2_manager()
    card = await asyncio.to_thread(mgr.get_card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"未找到角色卡: {card_id}")
    return await asyncio.to_thread(_build_card_detail_response, card)


@router.put("/cards/{card_id}")
async def update_persona_v2_card(card_id: str, payload: CardBody) -> Dict[str, Any]:
    """保存并更新指定 PersonaV2Card，同步默认变量至 TruthDB 并更新关联槽位摘要。"""
    raw_data = dict(payload.get("card") if isinstance(payload.get("card"), dict) else payload)
    raw_data["card_id"] = card_id
    try:
        card = PersonaV2Card.model_validate(raw_data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    mgr = get_persona_v2_manager()

    def _save_and_sync() -> Dict[str, Any]:
        saved = mgr.save_card(card)
        db = _open_truth_db()
        try:
            MVURuntimeEngine.ensure_card_mvu_defaults(db, saved)
        finally:
            db.close()
        _sync_card_to_bot_config_sync(saved, ensure_profile_registered=False)
        return _build_card_detail_response(saved)

    return await asyncio.to_thread(_save_and_sync)


@router.post("/cards")
async def create_persona_v2_card(payload: CardBody) -> Dict[str, Any]:
    """新建一张 PersonaV2Card 并在 bot_config.toml 中注册对应 PersonalityProfile。"""
    raw_data = dict(payload.get("card") if isinstance(payload.get("card"), dict) else payload)
    try:
        card = PersonaV2Card.model_validate(raw_data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    mgr = get_persona_v2_manager()

    def _create_and_sync() -> Dict[str, Any]:
        saved = mgr.save_card(card)
        db = _open_truth_db()
        try:
            MVURuntimeEngine.ensure_card_mvu_defaults(db, saved)
        finally:
            db.close()
        _sync_card_to_bot_config_sync(saved, ensure_profile_registered=True)
        return _build_card_detail_response(saved)

    return await asyncio.to_thread(_create_and_sync)


@router.delete("/cards/{card_id}")
async def delete_persona_v2_card(card_id: str) -> Dict[str, Any]:
    """删除自定义 PersonaV2Card。"""
    mgr = get_persona_v2_manager()
    deleted = await asyncio.to_thread(mgr.delete_card, card_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"未找到或无法删除角色卡: {card_id}")
    return {"status": "ok", "deleted": card_id}


@router.post("/cards/{card_id}/bind")
async def bind_persona_v2_card(card_id: str, body: BindBody) -> Dict[str, Any]:
    """将指定 PersonaV2Card 绑定至 active / sister_younger / sister_elder 并持久化到 bot_config.toml。"""
    mgr = get_persona_v2_manager()
    card = await asyncio.to_thread(mgr.get_card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"未找到角色卡: {card_id}")

    def _bind_sync() -> Dict[str, Any]:
        _sync_card_to_bot_config_sync(
            card,
            ensure_profile_registered=True,
            bind_target=body.target,
        )
        bindings = _resolve_current_bindings()
        return {
            "status": "ok",
            "card_id": card.card_id,
            "target": body.target,
            "bindings": bindings,
            "bound_targets": _bound_targets_for_card(card.card_id, bindings),
        }

    return await asyncio.to_thread(_bind_sync)


@router.put("/cards/{card_id}/mvu-state")
async def update_persona_v2_mvu_state(card_id: str, body: MVUStateBody) -> Dict[str, Any]:
    """从 WebUI 工作台手动修改指定角色卡的实时 MVU 变量值。"""
    mgr = get_persona_v2_manager()
    card = await asyncio.to_thread(mgr.get_card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"未找到角色卡: {card_id}")

    def _update_mvu_sync() -> Dict[str, Any]:
        db = _open_truth_db()
        try:
            MVURuntimeEngine.ensure_card_mvu_defaults(db, card)
            current_vars = MVURuntimeEngine.load_turn_variables(db, card, is_owner_private=True)
            schema_map = {v.key: v for v in card.mvu_config.variables}
            shared_alias_map = {
                str(v.shared_key).strip(): v
                for v in card.mvu_config.variables
                if getattr(v, "shared_key", "")
            }
            if "love_locked" in schema_map and "twin_harmony_locked" not in schema_map:
                shared_alias_map["twin_harmony_locked"] = schema_map["love_locked"]
            elif "twin_harmony_locked" in schema_map and "love_locked" not in schema_map:
                shared_alias_map["love_locked"] = schema_map["twin_harmony_locked"]

            now_iso = datetime.now(timezone.utc).isoformat()
            for vkey, raw_val in (body.values or {}).items():
                clean_k = str(vkey or "").strip()
                if not clean_k or clean_k in MVURuntimeEngine.SYSTEM_READONLY_KEYS:
                    continue
                vschema = schema_map.get(clean_k) or shared_alias_map.get(clean_k)
                vtype = vschema.type if vschema else "string"
                vis = vschema.visibility if vschema else "PRIVATE"
                scope_key = "OWNER_PRIVATE" if vis == "PRIVATE" else "GLOBAL_PUBLIC"

                coerced_val = CompanionTruthDB._deserialize_mvu_value(
                    vtype,
                    CompanionTruthDB._serialize_mvu_value(vtype, raw_val),
                )
                if vschema and vschema.type == "number":
                    try:
                        num_v = float(coerced_val)
                        if vschema.min is not None:
                            num_v = max(float(vschema.min), num_v)
                        if vschema.max is not None:
                            num_v = min(float(vschema.max), num_v)
                        coerced_val = int(num_v) if num_v.is_integer() else round(num_v, 4)
                    except Exception:
                        pass
                elif vschema and vschema.type == "boolean":
                    if (
                        vschema.monotonic_true
                        and bool(current_vars.get(vschema.key, False))
                        and not bool(coerced_val)
                    ):
                        coerced_val = True
                elif vschema and vschema.type == "string":
                    cand_s = str(coerced_val or "").strip()
                    if not cand_s and str(vschema.default_value or "").strip():
                        cand_s = str(vschema.default_value).strip()
                    if vschema.enum_values and cand_s not in vschema.enum_values:
                        matched = MVURuntimeEngine.match_enum_value(cand_s, vschema.enum_values)
                        if matched is not None:
                            cand_s = matched
                        else:
                            cand_s = str(current_vars.get(vschema.key, vschema.default_value))
                    coerced_val = cand_s

                if vschema is not None:
                    storage_cid, storage_vkey = MVURuntimeEngine.resolve_storage_target(card, vschema)
                    local_key = vschema.key
                else:
                    storage_cid, storage_vkey = card.card_id, clean_k
                    local_key = clean_k

                db.upsert_mvu_variable(
                    storage_cid,
                    scope_key=scope_key,
                    var_key=storage_vkey,
                    var_type=vtype,
                    var_value=coerced_val,
                    visibility=vis,
                    updated_at=now_iso,
                )
                if storage_cid != card.card_id:
                    db.upsert_mvu_variable(
                        card.card_id,
                        scope_key=scope_key,
                        var_key=local_key,
                        var_type=vtype,
                        var_value=coerced_val,
                        visibility=vis,
                        updated_at=now_iso,
                    )
                current_vars[local_key] = coerced_val

            mvu_state = MVURuntimeEngine.load_turn_variables(db, card, is_owner_private=True)
            active_stage = MVURuntimeEngine.resolve_active_stage(
                card, mvu_state, is_owner_private=True
            )
            return {
                "card_id": card.card_id,
                "mvu_state": mvu_state,
                "active_stage": _serialize_stage_info(active_stage),
            }
        finally:
            db.close()

    return await asyncio.to_thread(_update_mvu_sync)


@router.post("/cards/{card_id}/mvu-reset")
async def reset_persona_v2_mvu_state(card_id: str) -> Dict[str, Any]:
    """将指定角色卡的实时 MVU 变量重置回 Schema 初始默认值。"""
    mgr = get_persona_v2_manager()
    card = await asyncio.to_thread(mgr.get_card, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"未找到角色卡: {card_id}")

    def _reset_mvu_sync() -> Dict[str, Any]:
        db = _open_truth_db()
        try:
            mvu_state = MVURuntimeEngine.reset_card_mvu_variables(db, card)
            active_stage = MVURuntimeEngine.resolve_active_stage(
                card, mvu_state, is_owner_private=True
            )
            return {
                "card_id": card.card_id,
                "mvu_state": mvu_state,
                "active_stage": _serialize_stage_info(active_stage),
            }
        finally:
            db.close()

    return await asyncio.to_thread(_reset_mvu_sync)
