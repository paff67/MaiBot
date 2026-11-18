"""原生 MVU (Magical Variable Update) 状态机引擎：负责状态注入、<UpdateVariable> 解析剥离、潜令V1.8好感防崩坏护栏与 TruthDB 持久化。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import json
import math
import re

from src.common.logger import get_logger
from .persona_v2_engine import (
    MVUVariableSchema,
    PersonaV2Card,
    PersonaV2CompiledTurn,
    PersonaV2Engine,
    RelationshipStageOverlay,
)
from .truth_db import CompanionTruthDB

logger = get_logger("mvu_engine")

_UPDATE_VARIABLE_BLOCK_RE = re.compile(
    r"<UpdateVariable\b[^>]*>([\s\S]*?)</UpdateVariable>",
    flags=re.IGNORECASE,
)
_UNCLOSED_UPDATE_VARIABLE_RE = re.compile(
    r"<UpdateVariable\b[^>]*>[\s\S]*\Z",
    flags=re.IGNORECASE,
)
_ANALYSIS_TAG_RE = re.compile(
    r"<Analysis\b[^>]*>([\s\S]*?)</Analysis>",
    flags=re.IGNORECASE,
)
_JSON_PATCH_TAG_RE = re.compile(
    r"<JSONPatch\b[^>]*>([\s\S]*?)</JSONPatch>",
    flags=re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", flags=re.IGNORECASE)


@dataclass
class MVUVariableChange:
    key: str
    label: str
    old: Any
    new: Any
    delta: Optional[float] = None
    visibility: str = "PUBLIC"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "old": self.old,
            "new": self.new,
            "delta": self.delta,
            "visibility": self.visibility,
        }


@dataclass
class MVUTurnUpdateResult:
    card_id: str
    card_name: str = ""
    analysis: str = ""
    changes: List[MVUVariableChange] = field(default_factory=list)
    active_stage: str = ""
    active_stage_id: str = ""
    variables: Dict[str, Any] = field(default_factory=dict)
    had_update_tag: bool = False

    def to_telemetry_dict(self) -> Dict[str, Any]:
        return {
            "card_id": self.card_id,
            "card_name": self.card_name,
            "analysis": self.analysis,
            "changes": [c.to_dict() for c in self.changes],
            "active_stage": self.active_stage,
            "active_stage_id": self.active_stage_id,
            "variables": dict(self.variables),
        }


class MVURuntimeEngine:
    """MVU 变量运行时：提供公私域物理隔离读取、跨卡共享命名空间(shared:twin_xu)、潜令V1.8好感护栏校验与 <UpdateVariable> 解析。"""

    SYSTEM_READONLY_KEYS: Tuple[str, ...] = ("balance_tier", "phone_holder")

    @staticmethod
    def _schema_defs_for_db(card: PersonaV2Card) -> List[Dict[str, Any]]:
        return [
            {
                "key": v.key,
                "label": v.label,
                "type": v.type,
                "default_value": v.default_value,
                "visibility": v.visibility,
            }
            for v in card.mvu_config.variables
            if v.key not in MVURuntimeEngine.SYSTEM_READONLY_KEYS
        ]

    @staticmethod
    def resolve_storage_target(
        card: PersonaV2Card,
        vschema: MVUVariableSchema,
    ) -> Tuple[str, str]:
        """
        解析变量的底层存储路由目标 `(storage_card_id, storage_var_key)`：
        - 当 `vschema.shared` 为 True 时，路由至 `f"shared:{card.mvu_config.shared_namespace or 'twin_xu'}"`
          与 `vschema.shared_key or vschema.key`；
        - 否则路由至 `(card.card_id, vschema.key)`。
        """
        if getattr(vschema, "shared", False):
            ns = str(getattr(card.mvu_config, "shared_namespace", "") or "").strip() or "twin_xu"
            storage_card_id = ns if ns.startswith("shared:") else f"shared:{ns}"
            storage_var_key = str(getattr(vschema, "shared_key", "") or "").strip() or vschema.key
            return storage_card_id, storage_var_key
        return card.card_id, vschema.key

    @classmethod
    def resolve_system_readonly_variables(
        cls,
        db: Optional[CompanionTruthDB],
        card: PersonaV2Card,
        *,
        persona_slot: str = "",
        compute_tier: str = "",
    ) -> Dict[str, str]:
        """
        解析 Layer 1 系统只读状态变量 (`balance_tier`, `phone_holder`)：
        - `balance_tier`：映射自 `CompanionTruthDB.global_state.fairy_compute_tier`
          (`SUPERCHARGED` -> `"high"`, `STANDARD` -> `"medium"`, `THRIFTY`/`HIBERNATING` -> `"low"`)；
        - `phone_holder`：映射自当前激活角色卡与槽位 (`maibot_xuyue`/`sister_elder` -> `"xuyue"`,
          `maibot_xuyan`/`sister_younger` -> `"xuyan"`)。
        """
        eff_tier = str(compute_tier or "").strip().upper()
        eff_slot = str(persona_slot or "").strip()
        if db is not None and (not eff_tier or not eff_slot):
            try:
                row = db.conn.execute(
                    "SELECT fairy_compute_tier, active_persona_slot, active_sister FROM global_state WHERE row_id = 1"
                ).fetchone()
                if row is not None:
                    if not eff_tier:
                        eff_tier = str(row["fairy_compute_tier"] or "STANDARD").strip().upper()
                    if not eff_slot:
                        eff_slot = (
                            str(row["active_persona_slot"] or "").strip()
                            or str(row["active_sister"] or "").strip()
                        )
            except Exception:
                pass

        if eff_tier == "SUPERCHARGED":
            balance_tier = "high"
        elif eff_tier in {"THRIFTY", "HIBERNATING"}:
            balance_tier = "low"
        else:
            balance_tier = "medium"

        cid = str(card.card_id or "").strip()
        if cid == "maibot_xuyue" or eff_slot == "sister_elder":
            phone_holder = "xuyue"
        elif cid == "maibot_xuyan" or eff_slot == "sister_younger":
            phone_holder = "xuyan"
        else:
            phone_holder = cid or "unknown"

        return {
            "balance_tier": balance_tier,
            "phone_holder": phone_holder,
        }

    @classmethod
    def ensure_card_mvu_defaults(
        cls,
        db: Optional[CompanionTruthDB],
        card: PersonaV2Card,
    ) -> None:
        """幂等初始化指定卡片的私有存储桶与 `shared:*` 共享存储桶默认值。"""
        if db is None:
            return
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for v in card.mvu_config.variables:
            if v.key in cls.SYSTEM_READONLY_KEYS:
                continue
            storage_cid, storage_vkey = cls.resolve_storage_target(card, v)
            buckets.setdefault(storage_cid, []).append(
                {
                    "key": storage_vkey,
                    "label": v.label,
                    "type": v.type,
                    "default_value": v.default_value,
                    "visibility": v.visibility,
                }
            )
        # 同时确保 card.card_id 本身的全部变量定义也已初始化（兼容直接查询 card.card_id 的场景）
        db.ensure_mvu_defaults(card.card_id, cls._schema_defs_for_db(card))
        for storage_cid, defs in buckets.items():
            if storage_cid != card.card_id:
                db.ensure_mvu_defaults(storage_cid, defs)

    @classmethod
    def reset_card_mvu_variables(
        cls,
        db: Optional[CompanionTruthDB],
        card: PersonaV2Card,
    ) -> Dict[str, Any]:
        """重置指定角色卡的变量及关联的共享命名空间变量回初始默认值。"""
        if db is None:
            return cls.load_turn_variables(None, card, is_owner_private=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        db.reset_mvu_variables(
            card.card_id,
            variable_defs=cls._schema_defs_for_db(card),
        )
        for v in card.mvu_config.variables:
            if not getattr(v, "shared", False) or v.key in cls.SYSTEM_READONLY_KEYS:
                continue
            storage_cid, storage_vkey = cls.resolve_storage_target(card, v)
            scope_key = "OWNER_PRIVATE" if v.visibility == "PRIVATE" else "GLOBAL_PUBLIC"
            db.upsert_mvu_variable(
                storage_cid,
                scope_key=scope_key,
                var_key=storage_vkey,
                var_type=v.type,
                var_value=v.default_value,
                visibility=v.visibility,
                updated_at=now_iso,
            )
            # 清理同命名空间下可能缓存的单卡镜像行，防止单调锁旧值残留
            with db.conn:
                db.conn.execute(
                    "UPDATE mvu_variables SET var_value = ?, updated_at = ? "
                    "WHERE scope_key = ? AND var_key IN (?, ?)",
                    (
                        CompanionTruthDB._serialize_mvu_value(v.type, v.default_value),
                        now_iso,
                        scope_key,
                        v.key,
                        storage_vkey,
                    ),
                )
        return cls.load_turn_variables(db, card, is_owner_private=True)

    @staticmethod
    def match_enum_value(candidate_str: str, enum_values: Sequence[str]) -> Optional[str]:
        """精确或按分段词根(`·`分隔)匹配枚举项，防止单字符误命中。"""
        clean = str(candidate_str or "").strip()
        if not clean:
            return None
        if clean in enum_values:
            return clean
        if len(clean) < 2:
            return None
        # 优先匹配 `·` 分隔的语义词根（如 "吧台截胡" -> "吧台截胡·独占当晚"）
        for ev in enum_values:
            segments = [seg.strip() for seg in re.split(r"[·/|、\s]+", ev) if len(seg.strip()) >= 2]
            if any(clean == seg or seg in clean or clean in seg for seg in segments):
                return ev
        for ev in enum_values:
            if ev in clean or clean in ev:
                return ev
        return None

    @classmethod
    def load_turn_variables(
        cls,
        db: Optional[CompanionTruthDB],
        card: PersonaV2Card,
        *,
        is_owner_private: bool,
        persona_slot: str = "",
        compute_tier: str = "",
        include_system_readonly: bool = True,
    ) -> Dict[str, Any]:
        """
        加载当前轮次可见的 MVU 变量字典（含 Layer 1 只读系统变量 + Layer 2 `shared:twin_xu` 共享变量 + Layer 3 角色独立变量）：
        - Fail-Closed：当 `is_owner_private == False` 时，仅返回 `visibility == 'PUBLIC'` 的变量，
          且底层 SQL 仅查询 `visibility = 'PUBLIC' AND scope_key = 'GLOBAL_PUBLIC'`（如共享的 `apartment_scene` 可见，
          而共享的 `love_locked` 物理隔离不可见）。
        """
        allowed_schemas = [
            v
            for v in card.mvu_config.variables
            if (is_owner_private or v.visibility == "PUBLIC")
            and v.key not in cls.SYSTEM_READONLY_KEYS
        ]
        result: Dict[str, Any] = {}
        if include_system_readonly:
            result.update(
                cls.resolve_system_readonly_variables(
                    db,
                    card,
                    persona_slot=persona_slot,
                    compute_tier=compute_tier,
                )
            )
        for v in allowed_schemas:
            result[v.key] = CompanionTruthDB._deserialize_mvu_value(
                v.type,
                CompanionTruthDB._serialize_mvu_value(v.type, v.default_value),
            )
        if db is None:
            return result

        try:
            cls.ensure_card_mvu_defaults(db, card)
            bucket_ids = {card.card_id}
            for v in allowed_schemas:
                storage_cid, _ = cls.resolve_storage_target(card, v)
                bucket_ids.add(storage_cid)
            stored_by_bucket: Dict[str, Dict[str, Any]] = {
                cid: db.get_mvu_variables(cid, is_owner_private=is_owner_private)
                for cid in bucket_ids
            }
            card_stored = stored_by_bucket.get(card.card_id, {})
            for v in allowed_schemas:
                storage_cid, storage_vkey = cls.resolve_storage_target(card, v)
                bucket_stored = stored_by_bucket.get(storage_cid, {})
                if storage_vkey in bucket_stored:
                    val = bucket_stored[storage_vkey]
                    # 兼容单调锁：若直接向 card.card_id 写入了 True，同步提升共享单调锁
                    if (
                        getattr(v, "shared", False)
                        and v.monotonic_true
                        and v.type == "boolean"
                        and bool(card_stored.get(v.key, False))
                    ):
                        val = True
                    result[v.key] = val
                elif v.key in card_stored:
                    result[v.key] = card_stored[v.key]
        except Exception as exc:
            logger.warning(f"读取 MVU 变量失败 ({card.card_id}): {exc}")
        return result

    @staticmethod
    def resolve_active_stage(
        card: PersonaV2Card,
        mvu_values: Mapping[str, Any],
        *,
        is_owner_private: bool,
    ) -> Optional[RelationshipStageOverlay]:
        """
        根据当前好感度 (`affection`, 支持 `0.0 ~ 120.0` 浮点数) 与恋爱锁定状态 (`love_locked` / `twin_harmony_locked`)
        无死角匹配关系阶段覆写层：
        - Fail-Closed：非 Owner 私聊下绝不激活 `scope == 'owner_only'` 的阶段；
        - 当 `love_locked == True` 且 `affection > 100.0`（如 `100.5`）时，精准命中 `stage_101_120_harmony` (`101..120`)；
        - 当 `affection <= 100.0` 的小数（如 `19.5`, `39.5`）时，使用 `int(math.floor(affection_val))` 消除整数区间空隙。
        """
        if not card.relationship_stages:
            return None

        guard = card.mvu_config.affinity_guard
        aff_key = guard.target_var or "affection"
        lock_key = guard.lock_var or "love_locked"

        try:
            affection_val = float(mvu_values.get(aff_key, 0.0))
        except Exception:
            affection_val = 0.0
        love_locked_val = bool(
            mvu_values.get(
                lock_key,
                mvu_values.get("love_locked", mvu_values.get("twin_harmony_locked", False)),
            )
        )

        eligible_stages = [
            s
            for s in card.relationship_stages
            if is_owner_private or s.scope == "general"
        ]
        if not eligible_stages:
            return None

        floored_aff = max(0, int(math.floor(affection_val)))

        # 1. 若已达成恋爱锁定，优先匹配 require_love_locked=True 的阶段
        if love_locked_val:
            if affection_val > 100.0:
                locked_aff_int = max(101, int(math.ceil(affection_val)))
                for s in eligible_stages:
                    if s.require_love_locked and (
                        s.min_affection <= affection_val <= s.max_affection
                        or s.min_affection <= locked_aff_int <= s.max_affection
                    ):
                        return s
            for s in eligible_stages:
                if s.require_love_locked and (
                    s.min_affection <= affection_val <= s.max_affection
                    or s.min_affection <= floored_aff <= s.max_affection
                ):
                    return s

        # 2. 常规匹配 require_love_locked=False（或已满足锁定要求）的阶段，使用 floored_aff 消除 19.5/39.5 等浮点空隙
        for s in eligible_stages:
            if s.require_love_locked and not love_locked_val:
                continue
            if (
                s.min_affection <= floored_aff <= s.max_affection
                or s.min_affection <= affection_val <= s.max_affection
            ):
                return s

        return None

    @classmethod
    def build_status_current_variables_block(
        cls,
        card: PersonaV2Card,
        mvu_values: Mapping[str, Any],
        *,
        is_owner_private: bool,
        active_stage: Optional[RelationshipStageOverlay] = None,
    ) -> str:
        """构建注入 Prompt 尾部的 `<status_current_variables>` 实时状态块（顶部固定注入只读系统变量）。"""
        if not card.mvu_config.enabled:
            return ""
        visible_schemas = [
            v
            for v in card.mvu_config.variables
            if (is_owner_private or v.visibility == "PUBLIC")
            and v.key not in cls.SYSTEM_READONLY_KEYS
        ]
        if not visible_schemas:
            return ""

        balance_tier_val = str(mvu_values.get("balance_tier") or "medium")
        default_holder = (
            "xuyue"
            if card.card_id == "maibot_xuyue"
            else ("xuyan" if card.card_id == "maibot_xuyan" else card.card_id)
        )
        phone_holder_val = str(mvu_values.get("phone_holder") or default_holder)

        lines: List[str] = [
            "<status_current_variables>",
            f"- balance_tier (算力与生活费水位): {json.dumps(balance_tier_val, ensure_ascii=False)} "
            "[type=string, system_readonly=true] // 由网关算力账本实时映射(high/medium/low)，只读禁改",
            f"- phone_holder (当前吧台共用手机持有者): {json.dumps(phone_holder_val, ensure_ascii=False)} "
            "[type=string, system_readonly=true] // 当前执掌微信输入权的槽位(xuyue/xuyan)，只读禁改",
        ]
        for v in visible_schemas:
            cur_val = mvu_values.get(v.key, v.default_value)
            val_repr = json.dumps(cur_val, ensure_ascii=False)
            constraint_parts: List[str] = [f"type={v.type}"]
            if v.min is not None or v.max is not None:
                lo = "" if v.min is None else f"{float(v.min):g}"
                hi = "" if v.max is None else f"{float(v.max):g}"
                constraint_parts.append(f"range=[{lo}..{hi}]")
            if v.enum_values:
                constraint_parts.append(f"options={v.enum_values}")
            if v.monotonic_true:
                constraint_parts.append("monotonic_lock=true")
            if getattr(v, "shared", False):
                constraint_parts.append("shared=true")
            desc_suffix = f" // {v.description.strip()}" if v.description else ""
            lines.append(
                f"- {v.key} ({v.label}): {val_repr} [{', '.join(constraint_parts)}]{desc_suffix}"
            )

        if active_stage is not None and (is_owner_private or active_stage.scope == "general"):
            lines.append(
                f"- active_relationship_stage: \"{active_stage.stage_name}\" ({active_stage.stage_id})"
            )

        lines.append("</status_current_variables>")
        return "\n".join(lines)

    @classmethod
    def build_update_instruction_block(
        cls,
        card: PersonaV2Card,
        *,
        is_owner_private: bool,
    ) -> str:
        """构建 `<UpdateVariable>` 输出协议说明块（严格仅暴露当前会话可见可写的变量，排除系统只读变量）。"""
        if not card.mvu_config.enabled:
            return ""
        visible_schemas = [
            v
            for v in card.mvu_config.variables
            if (is_owner_private or v.visibility == "PUBLIC")
            and v.key not in cls.SYSTEM_READONLY_KEYS
        ]
        if not visible_schemas:
            return ""

        allowed_keys = ", ".join(f"`/{v.key}`" for v in visible_schemas)
        custom_instr = card.mvu_config.update_instruction.strip()
        guard = card.mvu_config.affinity_guard

        lines: List[str] = [
            "<mvu_variable_update_protocol>",
            "【MVU 动态变量更新协议】",
            f"- 当前会话允许更新的变量路径：{allowed_keys}（注：`balance_tier` 与 `phone_holder` 为系统只读变量，严禁在 `<JSONPatch>` 中修改）",
        ]
        if is_owner_private and guard.enabled:
            min_d = f"{float(guard.min_delta_per_turn):+g}"
            max_d = f"{float(guard.max_delta_per_turn):+g}"
            cap_v = f"{float(guard.cap_before_lock):g}"
            lines.append(
                f"- 好感度防崩坏护栏（潜令V1.8准则）：`/{guard.target_var}` 单轮变化幅度严禁超出 "
                f"[{min_d}, {max_d}]；"
                f"在 `/{guard.lock_var}` 为 `false` 期间，`/{guard.target_var}` 上限封顶为 {cap_v}；"
                f"一旦 `/{guard.lock_var}` 置为 `true` 则永久锁定不可逆转。"
            )
        elif not is_owner_private:
            lines.append(
                "- 当前处于公共/非Owner会话：仅允许更新 PUBLIC 公开状态变量，严禁输出或修改任何 PRIVATE 私密变量。"
            )

        if custom_instr:
            lines.append(f"- 角色专属变量演进细则：{custom_instr}")

        lines.extend(
            [
                "- 当本轮互动引起上述状态变量发生改变时，请在回复的最末尾（或 `<reply_story>` 之后）附加 `<UpdateVariable>` 块，格式如下：",
                "<UpdateVariable>",
                "<Analysis>简述本轮变量变化的原因与幅度校验</Analysis>",
                "<JSONPatch>",
                "[",
                '  {"op": "replace", "path": "/current_mood", "value": "平稳"},',
                '  {"op": "delta", "path": "/affection", "value": 1}',
                "]",
                "</JSONPatch>",
                "</UpdateVariable>",
                "- 注意：`<UpdateVariable>` 块会被系统自动剥离并写入状态库，切勿在给用户的正文中提及变量数值或标签名。",
                "</mvu_variable_update_protocol>",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _parse_patch_payload(raw_inner: str) -> List[Dict[str, Any]]:
        """从 `<UpdateVariable>` 内部文本解析出标准化的操作列表 `[{'op': ..., 'key': ..., 'value': ...}]`。"""
        patch_match = _JSON_PATCH_TAG_RE.search(raw_inner)
        candidate_text = patch_match.group(1).strip() if patch_match else raw_inner.strip()
        # 移除 <Analysis>...</Analysis>
        candidate_text = _ANALYSIS_TAG_RE.sub("", candidate_text).strip()
        fence_match = _CODE_FENCE_RE.search(candidate_text)
        if fence_match:
            candidate_text = fence_match.group(1).strip()

        parsed: Any = None
        for snippet in (candidate_text,):
            if not snippet:
                continue
            try:
                parsed = json.loads(snippet)
                break
            except Exception:
                # 尝试截取首个 [...] 或 {...}
                arr_m = re.search(r"(\[[\s\S]*\])", snippet)
                obj_m = re.search(r"(\{[\s\S]*\})", snippet)
                for m in (arr_m, obj_m):
                    if m:
                        try:
                            parsed = json.loads(m.group(1))
                            break
                        except Exception:
                            continue

        ops: List[Dict[str, Any]] = []
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                op = str(item.get("op") or "replace").strip().lower()
                raw_path = str(item.get("path") or item.get("key") or "").strip()
                key = raw_path.lstrip("/").split("/")[0].strip()
                if not key:
                    continue
                ops.append({"op": op, "key": key, "value": item.get("value")})
        elif isinstance(parsed, dict):
            # 既支持单条 JSONPatch 对象 {"op": "replace", "path": "/..."}，也支持简易字典 {"affection": 35}
            if "op" in parsed and ("path" in parsed or "key" in parsed):
                op = str(parsed.get("op") or "replace").strip().lower()
                raw_path = str(parsed.get("path") or parsed.get("key") or "").strip()
                key = raw_path.lstrip("/").split("/")[0].strip()
                if key:
                    ops.append({"op": op, "key": key, "value": parsed.get("value")})
            else:
                for k, v in parsed.items():
                    clean_k = str(k).lstrip("/").strip()
                    if not clean_k:
                        continue
                    if isinstance(v, str) and re.match(r"^[+-]\d+(?:\.\d+)?$", v.strip()):
                        ops.append({"op": "delta", "key": clean_k, "value": float(v.strip())})
                    else:
                        ops.append({"op": "replace", "key": clean_k, "value": v})
        return ops

    @classmethod
    def extract_and_strip_update_variable(
        cls,
        text: str,
    ) -> Tuple[str, List[Dict[str, Any]], str]:
        """
        从模型输出文本中提取所有 `<UpdateVariable>...</UpdateVariable>` 块并将其彻底从可见文本中剥离。
        返回 `(clean_text, patch_ops, combined_analysis)`。
        """
        raw = str(text or "")
        if not raw or "<updatevariable" not in raw.lower():
            return raw, [], ""

        analyses: List[str] = []
        all_ops: List[Dict[str, Any]] = []

        for match in _UPDATE_VARIABLE_BLOCK_RE.finditer(raw):
            inner = match.group(1) or ""
            for a_match in _ANALYSIS_TAG_RE.finditer(inner):
                a_text = (a_match.group(1) or "").strip()
                if a_text:
                    analyses.append(a_text)
            all_ops.extend(cls._parse_patch_payload(inner))

        cleaned = _UPDATE_VARIABLE_BLOCK_RE.sub("", raw)
        # 防御截断导致未闭合的 <UpdateVariable> 泄露到前台
        if "<updatevariable" in cleaned.lower():
            unclosed_m = _UNCLOSED_UPDATE_VARIABLE_RE.search(cleaned)
            if unclosed_m:
                tail_inner = unclosed_m.group(0)
                for a_match in _ANALYSIS_TAG_RE.finditer(tail_inner):
                    a_text = (a_match.group(1) or "").strip()
                    if a_text:
                        analyses.append(a_text)
                all_ops.extend(cls._parse_patch_payload(tail_inner))
                cleaned = _UNCLOSED_UPDATE_VARIABLE_RE.sub("", cleaned)

        return cleaned.strip(), all_ops, " | ".join(analyses)

    @classmethod
    def apply_turn_updates(
        cls,
        db: Optional[CompanionTruthDB],
        card: PersonaV2Card,
        raw_sources: Sequence[str],
        *,
        is_owner_private: bool,
        pre_extracted_ops: Optional[Sequence[Dict[str, Any]]] = None,
        pre_extracted_analysis: str = "",
    ) -> MVUTurnUpdateResult:
        """
        综合多个输出文本源（如 raw_response / outer_tail / clean_story）中的 `<UpdateVariable>` 补丁，
        经过 Fail-Closed 权限过滤、潜令V1.8好感度防崩坏护栏与单调锁校验后写入 TruthDB，
        并生成供 `st_telemetry["mvu_watch"]` 展示的结构化遥测数据。
        """
        current_vars = cls.load_turn_variables(db, card, is_owner_private=is_owner_private)
        schema_map: Dict[str, MVUVariableSchema] = {
            v.key: v for v in card.mvu_config.variables
        }
        shared_alias_map: Dict[str, MVUVariableSchema] = {}
        for v in card.mvu_config.variables:
            skey = str(getattr(v, "shared_key", "") or "").strip()
            if skey and skey not in schema_map:
                shared_alias_map[skey] = v
        if "love_locked" in schema_map and "twin_harmony_locked" not in schema_map:
            shared_alias_map["twin_harmony_locked"] = schema_map["love_locked"]
        elif "twin_harmony_locked" in schema_map and "love_locked" not in schema_map:
            shared_alias_map["love_locked"] = schema_map["twin_harmony_locked"]

        combined_ops: List[Dict[str, Any]] = list(pre_extracted_ops or [])
        analyses: List[str] = [pre_extracted_analysis] if pre_extracted_analysis else []

        for src in raw_sources:
            if not src:
                continue
            _, ops, analysis = cls.extract_and_strip_update_variable(src)
            if ops:
                combined_ops.extend(ops)
            if analysis and analysis not in analyses:
                analyses.append(analysis)

        had_tag = bool(combined_ops or analyses)
        if not card.mvu_config.enabled:
            active_stage = cls.resolve_active_stage(card, current_vars, is_owner_private=is_owner_private)
            return MVUTurnUpdateResult(
                card_id=card.card_id,
                card_name=card.name,
                analysis=" | ".join(analyses),
                changes=[],
                active_stage=active_stage.stage_name if active_stage else "",
                active_stage_id=active_stage.stage_id if active_stage else "",
                variables=current_vars,
                had_update_tag=had_tag,
            )

        guard = card.mvu_config.affinity_guard
        lock_var_key = guard.lock_var or "love_locked"
        target_aff_key = guard.target_var or "affection"

        # 先处理非好感度变量（以便本轮若同时解锁 love_locked=true，好感度上限判断能感知最新状态）
        ordered_ops = sorted(
            combined_ops,
            key=lambda item: 1 if item.get("key") == target_aff_key else 0,
        )

        changes: List[MVUVariableChange] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for patch in ordered_ops:
            raw_vkey = str(patch.get("key") or "").strip()
            op = str(patch.get("op") or "replace").strip().lower()
            raw_val = patch.get("value")

            if raw_vkey in cls.SYSTEM_READONLY_KEYS:
                logger.warning(
                    f"[MVU Readonly] 拦截对系统只读变量 `{raw_vkey}` 的修改尝试 ({card.card_id})"
                )
                continue

            vschema = schema_map.get(raw_vkey) or shared_alias_map.get(raw_vkey)
            if vschema is None:
                continue
            vkey = vschema.key

            # Fail-Closed：非 Owner 私聊严禁修改 PRIVATE 变量
            if not is_owner_private and vschema.visibility == "PRIVATE":
                logger.warning(
                    f"[MVU Fail-Closed] 拦截非 Owner 会话对 PRIVATE 变量 `{vkey}` 的修改尝试 ({card.card_id})"
                )
                continue

            old_val = current_vars.get(
                vkey,
                CompanionTruthDB._deserialize_mvu_value(
                    vschema.type,
                    CompanionTruthDB._serialize_mvu_value(vschema.type, vschema.default_value),
                ),
            )

            if op == "remove":
                new_val = CompanionTruthDB._deserialize_mvu_value(
                    vschema.type,
                    CompanionTruthDB._serialize_mvu_value(vschema.type, vschema.default_value),
                )
            elif vschema.type == "number":
                try:
                    old_num = float(old_val)
                except Exception:
                    old_num = 0.0
                try:
                    if isinstance(raw_val, str) and re.match(r"^[+-]\d+(?:\.\d+)?$", raw_val.strip()):
                        patch_num = float(raw_val.strip())
                        candidate_num = old_num + patch_num if op == "replace" else old_num + patch_num
                    else:
                        patch_num = float(raw_val)
                        if op in {"delta", "inc", "add"}:
                            candidate_num = old_num + patch_num
                        else:
                            candidate_num = patch_num
                except Exception:
                    continue

                # 潜令V1.8 好感度护栏
                if guard.enabled and vkey == target_aff_key:
                    raw_delta = candidate_num - old_num
                    clamped_delta = max(
                        float(guard.min_delta_per_turn),
                        min(float(guard.max_delta_per_turn), raw_delta),
                    )
                    candidate_num = old_num + clamped_delta
                    is_locked = bool(
                        current_vars.get(
                            lock_var_key,
                            current_vars.get("love_locked", current_vars.get("twin_harmony_locked", False)),
                        )
                    )
                    if not is_locked and candidate_num > float(guard.cap_before_lock):
                        candidate_num = float(guard.cap_before_lock)

                if vschema.min is not None:
                    candidate_num = max(float(vschema.min), candidate_num)
                if vschema.max is not None:
                    candidate_num = min(float(vschema.max), candidate_num)

                new_val = int(candidate_num) if candidate_num.is_integer() else round(candidate_num, 4)

            elif vschema.type == "boolean":
                if isinstance(raw_val, str):
                    candidate_bool = raw_val.strip().lower() in {"true", "1", "yes", "on"}
                else:
                    candidate_bool = bool(raw_val)
                # 单调锁：一旦为 True 不可逆转回 False
                if vschema.monotonic_true and bool(old_val) and not candidate_bool:
                    candidate_bool = True
                new_val = candidate_bool

            else:
                candidate_str = str(raw_val if raw_val is not None else "").strip()
                if not candidate_str and op != "remove":
                    default_str = str(vschema.default_value if vschema.default_value is not None else "").strip()
                    if default_str:
                        candidate_str = default_str
                    else:
                        continue
                if vschema.enum_values and candidate_str not in vschema.enum_values:
                    matched_enum = cls.match_enum_value(candidate_str, vschema.enum_values)
                    if matched_enum is None:
                        continue
                    candidate_str = matched_enum
                new_val = candidate_str

            if new_val == old_val:
                continue

            current_vars[vkey] = new_val
            delta_val: Optional[float] = None
            if vschema.type == "number":
                try:
                    delta_val = round(float(new_val) - float(old_val), 4)
                except Exception:
                    delta_val = None

            scope_key = "OWNER_PRIVATE" if vschema.visibility == "PRIVATE" else "GLOBAL_PUBLIC"
            storage_cid, storage_vkey = cls.resolve_storage_target(card, vschema)
            if db is not None:
                try:
                    db.upsert_mvu_variable(
                        storage_cid,
                        scope_key=scope_key,
                        var_key=storage_vkey,
                        var_type=vschema.type,
                        var_value=new_val,
                        visibility=vschema.visibility,
                        updated_at=now_iso,
                    )
                    if storage_cid != card.card_id:
                        db.upsert_mvu_variable(
                            card.card_id,
                            scope_key=scope_key,
                            var_key=vkey,
                            var_type=vschema.type,
                            var_value=new_val,
                            visibility=vschema.visibility,
                            updated_at=now_iso,
                        )
                except Exception as exc:
                    logger.warning(f"持久化 MVU 变量失败 ({storage_cid}.{storage_vkey}): {exc}")

            changes.append(
                MVUVariableChange(
                    key=vkey,
                    label=vschema.label,
                    old=old_val,
                    new=new_val,
                    delta=delta_val,
                    visibility=vschema.visibility,
                )
            )

        active_stage = cls.resolve_active_stage(
            card, current_vars, is_owner_private=is_owner_private
        )
        return MVUTurnUpdateResult(
            card_id=card.card_id,
            card_name=card.name,
            analysis=" | ".join(analyses),
            changes=changes,
            active_stage=active_stage.stage_name if active_stage else "",
            active_stage_id=active_stage.stage_id if active_stage else "",
            variables=current_vars,
            had_update_tag=had_tag,
        )

    @classmethod
    def compile_turn_persona_and_mvu(
        cls,
        card: PersonaV2Card,
        *,
        db: Optional[CompanionTruthDB],
        is_owner_private: bool,
        recent_text: str = "",
        mvu_enabled_override: Optional[bool] = None,
        persona_slot: str = "",
        compute_tier: str = "",
        max_dynamic_experiences: Optional[int] = None,
        max_sample_corpora: Optional[int] = None,
    ) -> PersonaV2CompiledTurn:
        """一步完成当前轮次的 MVU 变量加载、关系阶段判定与 Persona V2 静态/动态块编译。"""
        eff_mvu_enabled = (
            card.mvu_config.enabled
            if mvu_enabled_override is None
            else (bool(mvu_enabled_override) and bool(card.mvu_config.enabled))
        )
        if eff_mvu_enabled:
            mvu_values = cls.load_turn_variables(
                db,
                card,
                is_owner_private=is_owner_private,
                persona_slot=persona_slot,
                compute_tier=compute_tier,
            )
            active_stage = cls.resolve_active_stage(
                card, mvu_values, is_owner_private=is_owner_private
            )
            mvu_state_block = cls.build_status_current_variables_block(
                card,
                mvu_values,
                is_owner_private=is_owner_private,
                active_stage=active_stage,
            )
            mvu_update_instruction_block = cls.build_update_instruction_block(
                card, is_owner_private=is_owner_private
            )
        else:
            mvu_values = {}
            active_stage = None
            mvu_state_block = ""
            mvu_update_instruction_block = ""

        return PersonaV2Engine.compile_for_turn(
            card,
            is_owner_private=is_owner_private,
            recent_text=recent_text,
            mvu_values=mvu_values,
            active_stage=active_stage,
            mvu_state_block=mvu_state_block,
            mvu_update_instruction_block=mvu_update_instruction_block,
            max_dynamic_experiences=max_dynamic_experiences,
            max_sample_corpora=max_sample_corpora,
        )
