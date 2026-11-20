"""双子动态路由与算力水位状态机仲裁器（TwinRuntimeRouter）。

核心职责：
1. 严格通过 `runtime_bridge._resolve_trust_role_sync` 校验身份信任等级（Fail-Closed）；
2. 读取 `ComputeLedger` 实时有效余额，按 `TwinEconomyConfig` 动态阈值区分 HIBERNATING / THRIFTY / STANDARD / SUPERCHARGED 四档水位；
3. 省流态（< thrifty_threshold_usd）调用 `TwinNarrativeDirector` 由 LLM 动态生成并演进 `elder_away_arc`，并按需物理锁定 Flash 妹妹；
4. 充值回血（>= thrifty_threshold_usd）自动触发 LLM 生成回归衔接导语并清空离场锁；
5. 动态分配每轮 `planner` (Argon) vs `planner_flash` (Flash) 以及 `one_pass_st` vs `dual_director` 执行模式。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from typing import Any, Literal, Optional, Sequence, Tuple

from src.common.logger import get_logger
from src.companion_vault.access_policy import TurnScope
from src.companion_vault.compute_ledger import ComputeLedger, get_shared_compute_ledger
from src.companion_vault.runtime_bridge import DEFAULT_TRUTH_DB_PATH, _resolve_trust_role_sync, get_twin_routing_state
from src.companion_vault.twin_narrative_router import TwinNarrativeDirector, get_twin_narrative_director

logger = get_logger("twin_runtime_router")

_ACTIVE_EVOLVE_TASKS: set[asyncio.Task[Any]] = set()


@dataclass(frozen=True)
class TwinTurnDecision:
    """单轮对话的不可变双子路由决策。"""

    abort_turn: bool = False
    abort_reason: str = ""
    planner_task_name: str = "planner"
    allowed_slots: Tuple[str, ...] = ("sister_younger", "sister_elder")
    forced_slot: Optional[str] = None
    recommended_slot: str = "sister_younger"
    sender_trust_role: str = "GROUP_MEMBER"
    is_owner_private: bool = False
    balance_usd: float = 5.0
    compute_tier: str = "STANDARD"
    elder_away_arc: str = ""
    transient_narrative_note: str = ""
    execution_mode: Literal["dual_director", "one_pass_st"] = "dual_director"
    younger_profile: Optional[Any] = None
    elder_profile: Optional[Any] = None
    scope: Optional[TurnScope] = None

def _text_mentions_any_name(text: str, names: Sequence[str]) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    for raw in names:
        clean = str(raw or "").strip().lower()
        if len(clean) >= 1 and clean in lowered:
            return True
    return False


def _resolve_batch_slot_mentions(
    batch_texts: Sequence[str],
    elder_tokens: Sequence[str],
    younger_tokens: Sequence[str],
) -> Tuple[bool, bool, Optional[str]]:
    """扫描整批消息文本，返回 (calls_elder, calls_younger, last_explicit_slot)。

    当同一批次中先后呼唤两位姐妹时，按批次顺序以最后一次显式点名为准；
    若同一条消息内同时提及两位姐妹（如“许妍让你姐姐看一下”），优先 sister_elder。
    """
    calls_elder = False
    calls_younger = False
    last_explicit_slot: Optional[str] = None
    for raw in batch_texts:
        text = str(raw or "").strip()
        if not text:
            continue
        hit_elder = _text_mentions_any_name(text, elder_tokens)
        hit_younger = _text_mentions_any_name(text, younger_tokens)
        if hit_elder:
            calls_elder = True
        if hit_younger:
            calls_younger = True
        if hit_elder:
            last_explicit_slot = "sister_elder"
        elif hit_younger:
            last_explicit_slot = "sister_younger"
    return calls_elder, calls_younger, last_explicit_slot


async def _evolve_away_arc_background(
    ledger: ComputeLedger,
    director: TwinNarrativeDirector,
    previous_arc: str,
    recent_context: str,
    direction_prompt: str,
) -> None:
    try:
        updated = await director.evolve_elder_away_arc(
            previous_arc=previous_arc,
            recent_chat_excerpt=recent_context,
            direction_prompt=direction_prompt,
        )
        clean_updated = str(updated or "").strip()
        if clean_updated and clean_updated != previous_arc:
            ledger.set_elder_away_arc(clean_updated)
    except Exception as exc:
        logger.debug(f"后台演进姐姐离场状态失败（已安全忽略）: {exc}")


async def resolve_turn_route(
    *,
    session_id: str = "",
    platform: str = "wechat",
    sender_id: str = "",
    person_id: str = "",
    is_group_chat: bool = False,
    latest_user_text: str = "",
    batch_user_texts: Optional[Sequence[str]] = None,
    recent_context: str = "",
    batch_sender_ids: Optional[Sequence[str]] = None,
    mentions_bot: bool = False,
    ledger: Optional[ComputeLedger] = None,
    director: Optional[TwinNarrativeDirector] = None,
    truth_db_path: str = "",
    eco_cfg: Optional[Any] = None,
    personality_cfg: Optional[Any] = None,
    bot_cfg: Optional[Any] = None,
    reply_style_cfg: Optional[Any] = None,
) -> TwinTurnDecision:
    """根据身份鉴权、实时算力余额与对话语境，仲裁本轮双子路由与模型分配。"""
    from src.config.config import global_config

    resolved_eco = eco_cfg if eco_cfg is not None else getattr(global_config, "twin_economy", None)
    resolved_pers = personality_cfg if personality_cfg is not None else getattr(global_config, "personality", None)
    resolved_bot = bot_cfg if bot_cfg is not None else getattr(global_config, "bot", None)
    if reply_style_cfg is not None:
        resolved_reply_style = reply_style_cfg
    else:
        chat_cfg = getattr(global_config, "chat", None)
        resolved_reply_style = getattr(chat_cfg, "reply_style", None) if chat_cfg is not None else None

    db_path = (
        str(truth_db_path or "").strip()
        or str(os.environ.get("MAIBOT_TRUTH_DB_PATH", "") or "").strip()
        or DEFAULT_TRUTH_DB_PATH
    )

    # Step A: Fail-Closed 身份鉴权（严禁仅凭 not is_group_chat 判断）
    sender_trust_role = "GROUP_MEMBER"
    try:
        sender_trust_role = _resolve_trust_role_sync(
            db_path,
            str(person_id or "").strip(),
            str(sender_id or "").strip(),
            str(platform or "").strip(),
        )
    except Exception as exc:
        logger.debug(f"解析发送者信任角色失败，按默认成员降级: {exc}")

    normalized_role = str(sender_trust_role or "GROUP_MEMBER").strip().upper()
    is_owner_private = (not bool(is_group_chat)) and (normalized_role == "OWNER")

    owner_in_batch = normalized_role == "OWNER"
    if not owner_in_batch and batch_sender_ids:
        for sid in batch_sender_ids:
            clean_sid = str(sid or "").strip()
            if not clean_sid:
                continue
            try:
                role = _resolve_trust_role_sync(db_path, "", clean_sid, str(platform or "").strip())
                if str(role or "").upper() == "OWNER":
                    owner_in_batch = True
                    break
            except Exception:
                continue

    younger_profile = (
        resolved_pers.resolve_slot_profile("sister_younger", resolved_bot, resolved_reply_style)
        if resolved_pers is not None and hasattr(resolved_pers, "resolve_slot_profile")
        else None
    )
    elder_profile = (
        resolved_pers.resolve_slot_profile("sister_elder", resolved_bot, resolved_reply_style)
        if resolved_pers is not None and hasattr(resolved_pers, "resolve_slot_profile")
        else None
    )

    twin_mode_enabled = bool(getattr(resolved_pers, "twin_mode_enabled", True)) if resolved_pers else True
    eco_enabled = bool(getattr(resolved_eco, "enabled", True)) if resolved_eco else False
    raw_exec_mode = str(getattr(resolved_eco, "chat_execution_mode", "dual_director") or "dual_director").strip()
    from src.companion_vault.st_preset_engine import get_st_preset_manager, is_st_preset_enabled

    st_config = await asyncio.to_thread(lambda: get_st_preset_manager().load_config())
    st_preset_enabled = is_st_preset_enabled(st_config)
    execution_mode: Literal["dual_director", "one_pass_st"] = (
        raw_exec_mode if raw_exec_mode in {"dual_director", "one_pass_st"} else "dual_director"
    )
    if execution_mode == "one_pass_st" and not st_preset_enabled:
        return TwinTurnDecision(
            abort_turn=True,
            abort_reason="invalid_one_pass_st_without_preset",
            sender_trust_role=normalized_role,
            is_owner_private=is_owner_private,
            execution_mode=execution_mode,
            younger_profile=younger_profile,
            elder_profile=elder_profile,
        )

    if not eco_enabled:
        return TwinTurnDecision(
            abort_turn=False,
            planner_task_name="planner",
            allowed_slots=("sister_younger", "sister_elder") if twin_mode_enabled else ("sister_younger",),
            forced_slot=None if twin_mode_enabled else "sister_younger",
            recommended_slot="sister_younger",
            sender_trust_role=normalized_role,
            is_owner_private=is_owner_private,
            balance_usd=5.0,
            compute_tier="STANDARD",
            elder_away_arc="",
            transient_narrative_note="",
            execution_mode=execution_mode,
            younger_profile=younger_profile,
            elder_profile=elder_profile,
        )

    effective_ledger = ledger if ledger is not None else get_shared_compute_ledger(db_path)
    effective_director = director if director is not None else get_twin_narrative_director()

    try:
        await effective_ledger.sync_remote_balance_if_due()
    except Exception:
        pass
    snapshot = effective_ledger.get_snapshot()
    if isinstance(effective_ledger, ComputeLedger):
        # Another session may have confirmed delivery since this ledger instance
        # was loaded. Use the serialized database holder for the next arbitration.
        persisted = await asyncio.to_thread(get_twin_routing_state, str(effective_ledger.truth_db_path))
        snapshot = replace(snapshot, active_persona_slot=persisted["active_persona_slot"])
    balance_usd = float(snapshot.balance_usd)
    is_available = bool(snapshot.is_available)

    hib_threshold = float(getattr(resolved_eco, "hibernating_threshold_usd", 0.5))
    thrifty_threshold = float(getattr(resolved_eco, "thrifty_threshold_usd", 3.0))
    super_threshold = float(getattr(resolved_eco, "supercharged_threshold_usd", 15.0))
    evolve_hours = float(getattr(resolved_eco, "away_evolve_interval_hours", 6.0))
    # 方向约束由 TwinNarrativeDirector.load_prompts() 从 ST 预设条目解析，防止 stale 配置覆盖预设
    away_prompt = ""
    evolve_prompt = ""
    return_prompt = ""

    # Step B: 断粮休眠态 (< hibernating_threshold_usd)
    if not is_available or balance_usd < hib_threshold:
        if not is_owner_private:
            logger.info(
                f"[TwinRouter] 会话 {session_id} 命中 HIBERNATING 静默休眠 "
                f"(balance=${balance_usd:.2f} < ${hib_threshold:.2f}, role={normalized_role})"
            )
            return TwinTurnDecision(
                abort_turn=True,
                abort_reason="hibernating_non_owner",
                planner_task_name="planner_flash",
                allowed_slots=("sister_younger",),
                forced_slot="sister_younger",
                recommended_slot="sister_younger",
                sender_trust_role=normalized_role,
                is_owner_private=False,
                balance_usd=balance_usd,
                compute_tier="HIBERNATING",
                elder_away_arc=snapshot.elder_away_arc,
                transient_narrative_note="",
                execution_mode=execution_mode,
                younger_profile=younger_profile,
                elder_profile=elder_profile,
            )

        sos_note = (
            "【低功耗求援情境】当前算力经费已接近耗尽（处于最低功耗省流模式），姐姐已休眠待机，"
            "仅妹妹坚持留守回应 Owner 私聊。若合适，请以日常口吻轻声撒娇提及经费/电费见底的小状况，"
            "但不要念出死板的系统错误代码。"
        )
        return TwinTurnDecision(
            abort_turn=False,
            planner_task_name="planner_flash",
            allowed_slots=("sister_younger",),
            forced_slot="sister_younger",
            recommended_slot="sister_younger",
            sender_trust_role=normalized_role,
            is_owner_private=True,
            balance_usd=balance_usd,
            compute_tier="HIBERNATING",
            elder_away_arc=snapshot.elder_away_arc,
            transient_narrative_note=sos_note,
            execution_mode=execution_mode,
            younger_profile=younger_profile,
            elder_profile=elder_profile,
        )

    # 收集整批消息文本，避免末尾表情/短消息（如 [合十]）淹没同批前序消息中的点名/唤姐意图
    effective_batch_texts: list[str] = [
        str(t).strip() for t in (batch_user_texts or ()) if str(t or "").strip()
    ]
    clean_latest_text = str(latest_user_text or "").strip()
    if clean_latest_text and (not effective_batch_texts or effective_batch_texts[-1] != clean_latest_text):
        effective_batch_texts.append(clean_latest_text)
    routing_text = "\n".join(effective_batch_texts)

    # Step C: 吃紧省流态 (hibernating <= balance_usd < thrifty_threshold_usd)
    if balance_usd < thrifty_threshold:
        away_arc = str(snapshot.elder_away_arc or "").strip()
        elder_name = str(getattr(elder_profile, "nickname", "") or "").strip()
        younger_name = str(getattr(younger_profile, "nickname", "") or "").strip()
        elder_names: list[str] = []
        if elder_name:
            elder_names.append(elder_name)
        if elder_profile is not None:
            elder_names.extend(str(a) for a in (getattr(elder_profile, "alias_names", []) or []) if str(a).strip())
        if not elder_names:
            elder_names = ["许玥", "玥姐", "阿玥"]
        calls_elder = bool(twin_mode_enabled and _text_mentions_any_name(routing_text, [*elder_names, "姐姐"]))
        excerpt = (recent_context.strip() or routing_text or clean_latest_text) if is_group_chat else ""
        note = ""

        if not away_arc:
            try:
                generated_arc = await effective_director.generate_elder_away_arc(
                    balance_usd=balance_usd,
                    recent_chat_excerpt=excerpt,
                    direction_prompt=away_prompt,
                    elder_name=elder_name,
                    younger_name=younger_name,
                )
                away_arc = str(generated_arc or "").strip()
                if away_arc:
                    effective_ledger.set_elder_away_arc(away_arc)
                    note = (
                        f"【姐姐刚因省流暂离】姐姐刚刚因近况离开吧台前（状态：{away_arc}）。"
                        "本轮由妹妹独立回应，若对话提及姐姐请结合该近况自然带出。"
                    )
            except Exception as exc:
                logger.warning(f"[TwinRouter] 调用 TwinNarrativeDirector 生成离场状态失败: {exc}")
        else:
            elapsed = effective_ledger.get_elder_away_elapsed_seconds()
            if elapsed >= evolve_hours * 3600.0:
                try:
                    task = asyncio.create_task(
                        _evolve_away_arc_background(
                            effective_ledger,
                            effective_director,
                            away_arc,
                            excerpt,
                            evolve_prompt,
                        )
                    )
                    _ACTIVE_EVOLVE_TASKS.add(task)
                    task.add_done_callback(_ACTIVE_EVOLVE_TASKS.discard)
                except RuntimeError:
                    pass
            note = f"【姐姐持续离场中】姐姐当前不在场（最新场外进展：{away_arc}），由妹妹照看会话。"

        if calls_elder:
            elder_display = elder_name or "姐姐"
            younger_display = younger_name or "妹妹"
            away_summary = away_arc or f"{elder_display}暂时外出了"
            calls_elder_note = (
                f"【省流期唤姐应答指令】对方正在呼唤姐姐（{elder_display}），但当前处于省流模式，"
                f"强制由妹妹（{younger_display}）独立值班回复，严禁切出或扮演姐姐本人！"
                f"请以妹妹（{younger_display}）自己的口吻自然回应对方，"
                f"并根据【姐姐暂离剧情：{away_summary}】解释姐姐现在为什么不在、去忙什么了，帮姐姐带话或接手对方的话题。"
            )
            note = f"{note}\n{calls_elder_note}".strip() if note else calls_elder_note

        return TwinTurnDecision(
            abort_turn=False,
            planner_task_name="planner_flash",
            allowed_slots=("sister_younger",),
            forced_slot="sister_younger",
            recommended_slot="sister_younger",
            sender_trust_role=normalized_role,
            is_owner_private=is_owner_private,
            balance_usd=balance_usd,
            compute_tier="THRIFTY",
            elder_away_arc=away_arc,
            transient_narrative_note=note,
            execution_mode=execution_mode,
            younger_profile=younger_profile,
            elder_profile=elder_profile,
        )

    # Step D: 标准态 / 充裕超频态 (balance_usd >= thrifty_threshold_usd)
    compute_tier = "SUPERCHARGED" if balance_usd >= super_threshold else "STANDARD"
    prev_away_arc = str(snapshot.elder_away_arc or "").strip()
    return_note = ""
    if prev_away_arc:
        elder_display = (
            str(getattr(elder_profile, "nickname", "") or "").strip() if elder_profile is not None else ""
        ) or "许玥"
        fallback_note = (
            f"【算力经费恢复·姐姐回归衔接锚点】{elder_display}刚忙完手头事务（{prev_away_arc}）"
            "回到吧台前，拿回共用手机自然接过话题继续照看。"
        )
        try:
            bridge = await asyncio.wait_for(
                effective_director.generate_elder_return_arc(
                    evolved_arc=prev_away_arc,
                    balance_usd=balance_usd,
                    recent_chat_excerpt=recent_context.strip() if is_group_chat else "",
                    direction_prompt=return_prompt,
                ),
                timeout=8.0,
            )
            if bridge.strip():
                return_note = f"【算力经费恢复·姐姐回归衔接锚点】{bridge.strip()}"
            else:
                return_note = fallback_note
        except asyncio.TimeoutError:
            logger.warning("[TwinRouter] 生成姐姐回归衔接锚点超时（>8s），已降级为本地衔接提示")
            return_note = fallback_note
        except Exception as exc:
            logger.warning(f"[TwinRouter] 生成姐姐回归衔接锚点失败: {exc}")
            return_note = fallback_note
        finally:
            effective_ledger.set_elder_away_arc("")

    elder_names: list[str] = []
    younger_names: list[str] = []
    if elder_profile is not None:
        if getattr(elder_profile, "nickname", ""):
            elder_names.append(str(elder_profile.nickname))
        elder_names.extend(str(a) for a in (getattr(elder_profile, "alias_names", []) or []))
    if not elder_names:
        elder_names = ["许玥", "玥姐", "阿玥"]
    if younger_profile is not None:
        if getattr(younger_profile, "nickname", ""):
            younger_names.append(str(younger_profile.nickname))
        younger_names.extend(str(a) for a in (getattr(younger_profile, "alias_names", []) or []))

    bot_names = [*elder_names, *younger_names]
    if resolved_bot is not None:
        if getattr(resolved_bot, "nickname", ""):
            bot_names.append(str(resolved_bot.nickname))
        bot_names.extend(str(a) for a in (getattr(resolved_bot, "alias_names", []) or []))

    if twin_mode_enabled:
        calls_elder, _calls_younger, last_explicit_slot = _resolve_batch_slot_mentions(
            effective_batch_texts,
            [*elder_names, "姐姐"],
            [*younger_names, "妹妹"],
        )
    else:
        calls_elder, last_explicit_slot = False, None
    mentions_any = bool(mentions_bot or _text_mentions_any_name(routing_text, bot_names))

    use_argon_planner = (
        is_owner_private
        or (bool(is_group_chat) and owner_in_batch and mentions_any)
        or (compute_tier == "SUPERCHARGED" and calls_elder)
    )
    planner_task_name = "planner" if use_argon_planner else "planner_flash"

    if not twin_mode_enabled:
        recommended_slot = "sister_younger"
        allowed_slots: Tuple[str, ...] = ("sister_younger",)
        forced_slot: Optional[str] = "sister_younger"
    else:
        allowed_slots = ("sister_younger", "sister_elder")
        forced_slot = None
        if last_explicit_slot is not None:
            recommended_slot = last_explicit_slot
        elif (return_note and is_owner_private) or (compute_tier == "SUPERCHARGED" and is_owner_private):
            recommended_slot = "sister_elder"
        else:
            previous_slot = str(snapshot.active_persona_slot or "").strip()
            recommended_slot = (
                previous_slot
                if previous_slot in {"sister_younger", "sister_elder"}
                else "sister_younger"
            )

    return TwinTurnDecision(
        abort_turn=False,
        planner_task_name=planner_task_name,
        allowed_slots=allowed_slots,
        forced_slot=forced_slot,
        recommended_slot=recommended_slot,
        sender_trust_role=normalized_role,
        is_owner_private=is_owner_private,
        balance_usd=balance_usd,
        compute_tier=compute_tier,
        elder_away_arc="",
        transient_narrative_note=return_note,
        execution_mode=execution_mode,
        younger_profile=younger_profile,
        elder_profile=elder_profile,
    )
