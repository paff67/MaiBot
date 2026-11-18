"""Persona V2 结构化人设卡引擎：四层立体人设编译、公私域物理隔离(Fail-Closed)与动态语料召回。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple
import ast
import json
import re
import shutil

from pydantic import BaseModel, ConfigDict, Field

from src.common.logger import get_logger

logger = get_logger("persona_v2_engine")

ScopeType = Literal["general", "owner_only"]
VisibilityType = Literal["PUBLIC", "PRIVATE"]

ANTI_PARROTING_DIRECTIVE = (
    "【语料使用总则：示例台词与动作是分散语料而非固定剧本；"
    "同一轮最多化用1到3句，中间必须夹杂具体微动作、停顿或环境变化，严禁整段照抄连发】"
)


class StateMachineTier(BaseModel):
    tier: int = 1
    name: str = ""
    description: str = ""


class MetaCalibrationEntry(BaseModel):
    id: str
    title: str
    content: str
    scope: ScopeType = "general"


class PersonalityPaletteV2(BaseModel):
    daily_baseline: str = ""              # 通用: 日常底色
    coexistence_rule: str = ""            # 通用: 同屏原则
    affection_leak_style: str = ""        # Owner专属: 温柔泄漏/调戏方式
    desire_and_intimacy_drive: str = ""   # Owner专属: 欲望底色与主动性
    breakdown_sequence: str = ""          # Owner专属: 失控递进特征
    love_risk: str = ""                   # Owner专属: 爱情风险点


class SpeechRulesV2(BaseModel):
    public_address_matrix: str = ""       # 通用称呼规范
    modal_particles: str = ""             # 专属语气词规则
    sentence_rhythm: str = ""             # 句式长度与口语节奏
    negative_constraints: list[str] = Field(default_factory=list)  # 明确禁用词/禁演腔调
    owner_private_address_matrix: str = ""  # Owner专属称呼矩阵
    owner_emotional_tone_shift: str = ""  # Owner专属情绪失守语调变化


class CorePersonaV2(BaseModel):
    soul_pitch: str = ""                         # 通用: 一句话灵魂基调
    basic_info: str = ""                         # 通用: 基本信息
    public_visual_and_wardrobe: str = ""         # 通用: 公开外貌、体态与日常/工作穿搭锚点
    public_interaction_baseline: str = ""        # 通用: 通用社交与群聊表现守则
    owner_relationship_bond: str = ""            # Owner专属: 与Owner的关系前史与羁绊定位
    owner_private_wardrobe_and_anchors: str = ""  # Owner专属: 私密穿搭用法与身体反应锚点
    personality_palette: PersonalityPaletteV2 = Field(default_factory=PersonalityPaletteV2)
    owner_state_machine_tiers: list[StateMachineTier] = Field(default_factory=list)
    meta_calibration: list[MetaCalibrationEntry] = Field(default_factory=list)
    speech_rules: SpeechRulesV2 = Field(default_factory=SpeechRulesV2)


class PastExperienceEntry(BaseModel):
    id: str
    title: str
    period_or_scene: str = ""
    category: Literal["timeline_milestone", "life_vignette"] = "timeline_milestone"
    narrative: str
    usage_note_or_monologue: str = ""
    constant: bool = False
    trigger_keywords: list[str] = Field(default_factory=list)
    scope: ScopeType = "general"


class SampleCorpusEntry(BaseModel):
    id: str
    category: Literal[
        "daily_echo",
        "flirt_scene",
        "intimacy_gradient_dialogue",
        "intimacy_gradient_posture",
        "romance_lock_corpus",
    ] = "daily_echo"
    situation_title: str
    tier_label: str = ""
    action_and_posture: str = ""
    sample_lines: list[str] = Field(default_factory=list)
    trigger_keywords: list[str] = Field(default_factory=list)
    mvu_condition: str = ""
    scope: ScopeType = "general"


class RelationshipStageOverlay(BaseModel):
    stage_id: str
    stage_name: str
    min_affection: int = 0
    max_affection: int = 100
    require_love_locked: bool = False
    scope: ScopeType = "owner_only"
    tsundere_to_honesty_ratio: str = ""
    planner_directive: str = ""
    palette_override: str = ""
    interaction_boundaries: str = ""
    unlocked_corpus_ids: list[str] = Field(default_factory=list)


class MVUAffinityGuardConfig(BaseModel):
    enabled: bool = True
    target_var: str = "affection"
    lock_var: str = "love_locked"
    min_delta_per_turn: float = -1.0
    max_delta_per_turn: float = 5.0
    cap_before_lock: float = 99.0


class MVUVariableSchema(BaseModel):
    key: str
    label: str
    type: Literal["number", "string", "boolean"] = "string"
    default_value: Any = ""
    min: Optional[float] = None
    max: Optional[float] = None
    enum_values: list[str] = Field(default_factory=list)
    monotonic_true: bool = False
    visibility: VisibilityType = "PUBLIC"
    description: str = ""
    shared: bool = False
    shared_key: str = ""


class MVUConfig(BaseModel):
    enabled: bool = True
    shared_namespace: str = ""
    update_instruction: str = ""
    affinity_guard: MVUAffinityGuardConfig = Field(default_factory=MVUAffinityGuardConfig)
    variables: list[MVUVariableSchema] = Field(default_factory=list)


class PersonaV2Card(BaseModel):
    schema_name: str = Field(default="maibot.persona.v2", alias="schema")
    version: str = "2.0"
    card_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    role_tag: str = ""
    avatar_url: str = ""
    planner_summary: str = ""
    core_persona: CorePersonaV2 = Field(default_factory=CorePersonaV2)
    past_experiences: list[PastExperienceEntry] = Field(default_factory=list)
    sample_corpora: list[SampleCorpusEntry] = Field(default_factory=list)
    relationship_stages: list[RelationshipStageOverlay] = Field(default_factory=list)
    mvu_config: MVUConfig = Field(default_factory=MVUConfig)
    model_config = ConfigDict(populate_by_name=True)

    def to_legacy_profile_fields(self) -> Dict[str, Any]:
        """生成向后兼容的旧版一维槽位字段摘要，供绑定槽位或降级路径读取。"""
        cp = self.core_persona
        personality_parts = [
            p.strip()
            for p in (
                cp.soul_pitch,
                cp.basic_info,
                cp.personality_palette.daily_baseline,
                cp.public_interaction_baseline,
            )
            if p and p.strip()
        ]
        sr = cp.speech_rules
        reply_parts = [
            p.strip()
            for p in (
                sr.public_address_matrix,
                sr.modal_particles,
                sr.sentence_rhythm,
            )
            if p and p.strip()
        ]
        if sr.negative_constraints:
            reply_parts.append("禁用表达：" + "；".join(sr.negative_constraints))
        plan_style = (
            self.planner_summary.strip()
            or cp.public_interaction_baseline.strip()
            or cp.soul_pitch.strip()
        )
        return {
            "nickname": self.name.strip(),
            "alias_names": [a.strip() for a in self.aliases if a and a.strip()],
            "personality": "\n".join(personality_parts),
            "reply_style": "\n".join(reply_parts),
            "plan_style": plan_style,
            "visual_style": cp.public_visual_and_wardrobe.strip(),
            "persona_v2_card_id": self.card_id,
            "mvu_enabled": bool(self.mvu_config.enabled),
        }


@dataclass
class PersonaV2CompiledTurn:
    """单轮对话的 Persona V2 + MVU 编译产物（区分静态 KV-Cache 块与动态尾部块）。"""

    card_id: str
    card_name: str
    is_owner_private: bool
    static_core_block: str = ""
    dynamic_tail_block: str = ""
    mvu_state_block: str = ""
    mvu_update_instruction_block: str = ""
    planner_summary_block: str = ""
    planner_d0_guidance_block: str = ""
    active_stage: Optional[RelationshipStageOverlay] = None
    matched_experience_ids: List[str] = field(default_factory=list)
    matched_corpus_ids: List[str] = field(default_factory=list)
    mvu_values: Dict[str, Any] = field(default_factory=dict)


_CLAUSE_PATTERN = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(>=|<=|==|!=|>|<)\s*(.+?)\s*$"
)
_STRING_OR_OP_TOKEN_RE = re.compile(
    r"""('(?:\\.|[^'])*'|"(?:\\.|[^"])*")|(\&\&|\|\||\bAND\b|\bOR\b|\bNOT\b)"""
)


class _MissingValueSentinel:
    def __bool__(self) -> bool:
        return False


_MISSING_VALUE = _MissingValueSentinel()


def _lookup_mvu_var(var_name: str, mvu_values: Mapping[str, Any]) -> Any:
    if var_name in mvu_values:
        return mvu_values[var_name]
    if var_name == "twin_harmony_locked" and "love_locked" in mvu_values:
        return mvu_values["love_locked"]
    if var_name == "love_locked" and "twin_harmony_locked" in mvu_values:
        return mvu_values["twin_harmony_locked"]
    return _MISSING_VALUE


def _parse_literal(raw: str) -> Any:
    s = raw.strip()
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        num = float(s)
        return int(num) if num.is_integer() else num
    except Exception:
        return s


def _compare_pair(lhs: Any, op_str: str, rhs: Any) -> bool:
    if lhs is _MISSING_VALUE or rhs is _MISSING_VALUE:
        return False
    try:
        if op_str in {"in", "not in"}:
            if isinstance(rhs, (list, tuple, set)):
                matched = lhs in rhs or str(lhs).strip() in {str(item).strip() for item in rhs}
            else:
                matched = str(lhs).strip() in str(rhs)
            return matched if op_str == "in" else not matched

        if isinstance(lhs, bool) or isinstance(rhs, bool):
            lhs_b = bool(lhs) if not isinstance(lhs, str) else lhs.strip().lower() in {"true", "1"}
            rhs_b = bool(rhs) if not isinstance(rhs, str) else rhs.strip().lower() in {"true", "1"}
            if op_str == "==":
                return lhs_b == rhs_b
            if op_str == "!=":
                return lhs_b != rhs_b
            return False
        if isinstance(lhs, (int, float)) or isinstance(rhs, (int, float)):
            lhs_f = float(lhs)
            rhs_f = float(rhs)
            if op_str == ">=":
                return lhs_f >= rhs_f
            if op_str == "<=":
                return lhs_f <= rhs_f
            if op_str == ">":
                return lhs_f > rhs_f
            if op_str == "<":
                return lhs_f < rhs_f
            if op_str == "==":
                return lhs_f == rhs_f
            if op_str == "!=":
                return lhs_f != rhs_f
        lhs_s = str(lhs).strip()
        rhs_s = str(rhs).strip()
        if op_str == "==":
            return lhs_s == rhs_s
        if op_str == "!=":
            return lhs_s != rhs_s
    except Exception:
        return False
    return False


def _eval_single_clause(clause: str, mvu_values: Mapping[str, Any]) -> bool:
    c = clause.strip()
    if not c:
        return True
    while c.startswith("(") and c.endswith(")"):
        c = c[1:-1].strip()
    m = _CLAUSE_PATTERN.match(c)
    if not m:
        # 容忍纯布尔变量名，例如 "love_locked" 或 "not love_locked"
        if c.lower().startswith("not "):
            var_name = c[4:].strip()
            val = _lookup_mvu_var(var_name, mvu_values)
            return False if val is _MISSING_VALUE else not bool(val)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", c):
            val = _lookup_mvu_var(c, mvu_values)
            return False if val is _MISSING_VALUE else bool(val)
        return False

    var_name, op, rhs_raw = m.group(1), m.group(2), m.group(3)
    lhs = _lookup_mvu_var(var_name, mvu_values)
    if lhs is _MISSING_VALUE:
        return False
    rhs = _parse_literal(rhs_raw)
    return _compare_pair(lhs, op, rhs)


def _normalize_condition_for_ast(expr: str) -> str:
    def _repl(match: re.Match[str]) -> str:
        quoted, token = match.group(1), match.group(2)
        if quoted is not None:
            return quoted
        if token in {"&&", "AND"}:
            return " and "
        if token in {"||", "OR"}:
            return " or "
        if token == "NOT":
            return " not "
        return token or ""

    return _STRING_OR_OP_TOKEN_RE.sub(_repl, expr)


def _eval_ast_node(node: ast.AST, mvu_values: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_ast_node(node.body, mvu_values)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [_eval_ast_node(elt, mvu_values) for elt in node.elts]
    if isinstance(node, ast.Name):
        ident = node.id
        low = ident.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low in {"none", "null"}:
            return None
        return _lookup_mvu_var(ident, mvu_values)
    if isinstance(node, ast.UnaryOp):
        operand = _eval_ast_node(node.operand, mvu_values)
        if isinstance(node.op, ast.Not):
            if operand is _MISSING_VALUE:
                return False
            return not bool(operand)
        if operand is _MISSING_VALUE:
            return _MISSING_VALUE
        if isinstance(node.op, ast.USub):
            return -float(operand)
        if isinstance(node.op, ast.UAdd):
            return +float(operand)
        raise ValueError(f"Unsupported unary op: {type(node.op).__name__}")
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(bool(_eval_ast_node(v, mvu_values)) for v in node.values)
        if isinstance(node.op, ast.Or):
            return any(bool(_eval_ast_node(v, mvu_values)) for v in node.values)
        raise ValueError(f"Unsupported bool op: {type(node.op).__name__}")
    if isinstance(node, ast.Compare):
        left = _eval_ast_node(node.left, mvu_values)
        for op_node, comp_node in zip(node.ops, node.comparators, strict=False):
            right = _eval_ast_node(comp_node, mvu_values)
            if isinstance(op_node, ast.GtE):
                op_str = ">="
            elif isinstance(op_node, ast.LtE):
                op_str = "<="
            elif isinstance(op_node, ast.Gt):
                op_str = ">"
            elif isinstance(op_node, ast.Lt):
                op_str = "<"
            elif isinstance(op_node, ast.Eq):
                op_str = "=="
            elif isinstance(op_node, ast.NotEq):
                op_str = "!="
            elif isinstance(op_node, ast.In):
                op_str = "in"
            elif isinstance(op_node, ast.NotIn):
                op_str = "not in"
            else:
                return False
            if not _compare_pair(left, op_str, right):
                return False
            left = right
        return True
    raise ValueError(f"Unsupported AST node: {type(node).__name__}")


def evaluate_mvu_condition(condition: str, mvu_values: Mapping[str, Any]) -> bool:
    """安全评估 SampleCorpusEntry.mvu_condition（支持 and/or、括号分组、链式区间、in 与浮点数比较）。"""
    expr = str(condition or "").strip()
    if not expr:
        return True
    try:
        normalized = _normalize_condition_for_ast(expr)
        parsed = ast.parse(normalized, mode="eval")
        res = _eval_ast_node(parsed, mvu_values)
        return False if res is _MISSING_VALUE else bool(res)
    except Exception:
        pass
    or_parts = re.split(r"\s+(?:or|OR|\|\|)\s+", expr)
    for or_part in or_parts:
        and_parts = re.split(r"\s+(?:and|AND|&&)\s+", or_part.strip())
        if all(_eval_single_clause(part, mvu_values) for part in and_parts if part.strip()):
            return True
    return False


def match_trigger_keywords(
    recent_text: str,
    trigger_keywords: Sequence[str],
    anchor_names: Sequence[str] = (),
    proximity_window: int = 240,
) -> bool:
    """
    240字符邻近度与关键词召回判定：
    1. 若 recent_text 中任一 trigger_keyword 命中：
       - 当文本中存在角色名/别名锚点时，若关键词处于锚点前后 240 字符窗口内，或位于最近 240 字符对话尾部内，则命中；
       - 当文本中未显式出现角色名（如私聊直接对话）或总长度 <= 480 时，只要命中关键词即召回。
    """
    text = str(recent_text or "").strip()
    if not text or not trigger_keywords:
        return False

    text_lower = text.lower()
    valid_keywords = [str(k).strip().lower() for k in trigger_keywords if str(k or "").strip()]
    if not valid_keywords:
        return False

    anchor_positions: List[Tuple[int, int]] = []
    for name in anchor_names:
        clean_name = str(name or "").strip().lower()
        if not clean_name:
            continue
        start = 0
        while True:
            idx = text_lower.find(clean_name, start)
            if idx == -1:
                break
            anchor_positions.append((idx, idx + len(clean_name)))
            start = idx + len(clean_name)

    tail_threshold = max(0, len(text_lower) - proximity_window)

    for kw in valid_keywords:
        start = 0
        while True:
            kw_idx = text_lower.find(kw, start)
            if kw_idx == -1:
                break
            kw_end = kw_idx + len(kw)
            # 在最近 240 字符内直接命中，或全段无角色名锚点时直接命中
            if kw_idx >= tail_threshold or not anchor_positions or len(text_lower) <= proximity_window * 2:
                return True
            # 检查与任一角色名/别名锚点的字符距离是否在 240 字符以内
            for a_start, a_end in anchor_positions:
                dist = 0
                if kw_end < a_start:
                    dist = a_start - kw_end
                elif kw_idx > a_end:
                    dist = kw_idx - a_end
                if dist <= proximity_window:
                    return True
            start = kw_end

    return False


class PersonaV2Engine:
    """Persona V2 编译器：负责公私域裁剪、静态/动态分层与语料动态召回。"""

    @staticmethod
    def build_static_core_block(card: PersonaV2Card) -> str:
        """
        构建 KV-Cache 友好的静态角色主干块（仅包含 general 公开人设与常驻公开经历，
        绝不包含任何 owner_only 私密字段或动态变量，确保所有会话可安全共享静态前缀）。
        """
        cp = card.core_persona
        lines: List[str] = [f"<character_persona_v2 id=\"{card.card_id}\" name=\"{card.name}\">"]
        if card.aliases:
            lines.append(f"- 角色别名：{'、'.join(card.aliases)}")
        if card.role_tag:
            lines.append(f"- 角色定位：{card.role_tag}")
        if cp.soul_pitch:
            lines.append(f"【灵魂基调】\n{cp.soul_pitch.strip()}")
        if cp.basic_info:
            lines.append(f"【基本信息】\n{cp.basic_info.strip()}")
        if cp.public_visual_and_wardrobe:
            lines.append(f"【公开外貌与日常穿搭锚点】\n{cp.public_visual_and_wardrobe.strip()}")
        if cp.public_interaction_baseline:
            lines.append(f"【通用社交与群聊互动守则】\n{cp.public_interaction_baseline.strip()}")

        pal = cp.personality_palette
        if pal.daily_baseline or pal.coexistence_rule:
            pal_lines: List[str] = ["【性格光谱（日常基准）】"]
            if pal.daily_baseline:
                pal_lines.append(f"- 日常底色：{pal.daily_baseline.strip()}")
            if pal.coexistence_rule:
                pal_lines.append(f"- 同屏与共处原则：{pal.coexistence_rule.strip()}")
            lines.append("\n".join(pal_lines))

        sr = cp.speech_rules
        sr_lines: List[str] = []
        if sr.public_address_matrix:
            sr_lines.append(f"- 通用称呼规范：{sr.public_address_matrix.strip()}")
        if sr.modal_particles:
            sr_lines.append(f"- 语气词特征：{sr.modal_particles.strip()}")
        if sr.sentence_rhythm:
            sr_lines.append(f"- 句式与口语节奏：{sr.sentence_rhythm.strip()}")
        if sr.negative_constraints:
            sr_lines.append(f"- 禁用表达与反OOC红线：{'；'.join(sr.negative_constraints)}")
        if sr_lines:
            lines.append("【语言与台词规范（通用）】\n" + "\n".join(sr_lines))

        general_meta = [m for m in cp.meta_calibration if m.scope == "general"]
        if general_meta:
            meta_lines = ["【防崩坏元指令校准（通用）】"]
            for m in general_meta:
                meta_lines.append(f"- [{m.title}] {m.content.strip()}")
            lines.append("\n".join(meta_lines))

        constant_general_exp = [
            e for e in card.past_experiences if e.constant and e.scope == "general"
        ]
        if constant_general_exp:
            exp_lines = ["【常驻过往经历与人生底色】"]
            for e in constant_general_exp:
                header = f"- 《{e.title}》" + (f"（{e.period_or_scene}）" if e.period_or_scene else "")
                exp_lines.append(f"{header}：{e.narrative.strip()}")
                if e.usage_note_or_monologue:
                    exp_lines.append(f"  [内心注记] {e.usage_note_or_monologue.strip()}")
            lines.append("\n".join(exp_lines))

        lines.append("</character_persona_v2>")
        return "\n\n".join(lines)

    @staticmethod
    def _normalize_optional_cap(limit: Optional[int]) -> Optional[int]:
        """将 Optional[int] 上限规范化为正整数上限或 None（<= 0 视为不限制）。"""
        if limit is None or limit <= 0:
            return None
        return int(limit)

    @staticmethod
    def _build_stages_overview_block(stages: Sequence[RelationshipStageOverlay]) -> str:
        """构建按权限过滤后的关系阶段全景概览块。"""
        if not stages:
            return ""
        overview_lines: List[str] = ["<relationship_stages_overview>"]
        for stage in stages:
            lock_note = " | 需love_locked=true" if stage.require_love_locked else ""
            ratio = stage.tsundere_to_honesty_ratio.strip() or "自然过渡"
            palette = " ".join(stage.palette_override.split())
            if len(palette) > 140:
                palette = palette[:137].rstrip() + "..."
            entry = (
                f"- {stage.stage_id}｜{stage.stage_name}"
                f"（好感 {stage.min_affection}~{stage.max_affection}{lock_note}"
                f"｜嘴硬/坦诚配比：{ratio}）"
            )
            if palette:
                entry += f"：{palette}"
            overview_lines.append(entry)
        overview_lines.append("</relationship_stages_overview>")
        return "\n".join(overview_lines)

    @classmethod
    def _select_dynamic_experiences(
        cls,
        card: PersonaV2Card,
        *,
        is_owner_private: bool,
        recent_text: str,
        anchor_names: Sequence[str],
        cap: Optional[int],
    ) -> List[Tuple[PastExperienceEntry, bool]]:
        """筛选非常驻过往经历：严格隔离 scope，关键词命中优先置顶，其余按序全量/限量加载。"""
        kw_hits: List[Tuple[PastExperienceEntry, bool]] = []
        remaining: List[Tuple[PastExperienceEntry, bool]] = []
        for exp in card.past_experiences:
            if exp.constant:
                continue
            if not is_owner_private and exp.scope == "owner_only":
                continue
            if match_trigger_keywords(recent_text, exp.trigger_keywords, anchor_names):
                kw_hits.append((exp, True))
            else:
                remaining.append((exp, False))
        selected = kw_hits + remaining
        return selected[:cap] if cap is not None else selected

    @classmethod
    def _select_sample_corpora(
        cls,
        card: PersonaV2Card,
        *,
        is_owner_private: bool,
        recent_text: str,
        anchor_names: Sequence[str],
        eff_mvu: Mapping[str, Any],
        unlocked_set: set[str],
        cap: Optional[int],
    ) -> List[Tuple[SampleCorpusEntry, str]]:
        """按 scope 与 mvu_condition 严格过滤语料，按 关键词命中 > 阶段解锁 > 其余合格语料 排序。"""
        eligible_corpora: List[SampleCorpusEntry] = [
            c
            for c in card.sample_corpora
            if (is_owner_private or c.scope == "general")
            and (not c.mvu_condition or evaluate_mvu_condition(c.mvu_condition, eff_mvu))
        ]

        kw_hits: List[Tuple[SampleCorpusEntry, str]] = []
        stage_hits: List[Tuple[SampleCorpusEntry, str]] = []
        remaining: List[Tuple[SampleCorpusEntry, str]] = []
        seen_ids: set[str] = set()

        for c in eligible_corpora:
            if c.id in seen_ids:
                continue
            if match_trigger_keywords(recent_text, c.trigger_keywords, anchor_names):
                kw_hits.append((c, "[🔥 当前话题优先化用]"))
                seen_ids.add(c.id)

        for c in eligible_corpora:
            if c.id in seen_ids:
                continue
            if c.id in unlocked_set:
                stage_hits.append((c, "[✨ 当前阶段核心语料]"))
                seen_ids.add(c.id)

        for c in eligible_corpora:
            if c.id in seen_ids:
                continue
            remaining.append((c, ""))
            seen_ids.add(c.id)

        selected = kw_hits + stage_hits + remaining
        return selected[:cap] if cap is not None else selected

    @classmethod
    def compile_for_turn(
        cls,
        card: PersonaV2Card,
        *,
        is_owner_private: bool,
        recent_text: str = "",
        mvu_values: Optional[Mapping[str, Any]] = None,
        active_stage: Optional[RelationshipStageOverlay] = None,
        mvu_state_block: str = "",
        mvu_update_instruction_block: str = "",
        max_dynamic_experiences: Optional[int] = None,
        max_sample_corpora: Optional[int] = None,
    ) -> PersonaV2CompiledTurn:
        """
        根据当前会话安全级别 (`is_owner_private`)、近期对话文本与 MVU 实时状态编译单轮四层全量注入块。
        Fail-Closed 铁律：当 `is_owner_private == False` 时，物理剔除全部 `owner_only` 字段、阶段、经历与语料。
        """
        eff_mvu = dict(mvu_values or {})
        exp_cap = cls._normalize_optional_cap(max_dynamic_experiences)
        corpus_cap = cls._normalize_optional_cap(max_sample_corpora)
        static_core_block = cls.build_static_core_block(card)
        anchor_names = [card.name] + list(card.aliases or [])

        dynamic_sections: List[str] = []
        cp = card.core_persona

        # 1. Owner 私密核心覆写层（仅 is_owner_private == True 时准入）
        if is_owner_private:
            owner_core_lines: List[str] = ["<owner_private_persona_overlay>"]
            if cp.owner_relationship_bond:
                owner_core_lines.append(f"【与Owner的关系前史与羁绊定位】\n{cp.owner_relationship_bond.strip()}")
            if cp.owner_private_wardrobe_and_anchors:
                owner_core_lines.append(f"【私密穿搭与身体反应锚点】\n{cp.owner_private_wardrobe_and_anchors.strip()}")

            pal = cp.personality_palette
            priv_pal_lines: List[str] = []
            if pal.affection_leak_style:
                priv_pal_lines.append(f"- 温柔泄漏与调戏方式：{pal.affection_leak_style.strip()}")
            if pal.desire_and_intimacy_drive:
                priv_pal_lines.append(f"- 欲望底色与主动性：{pal.desire_and_intimacy_drive.strip()}")
            if pal.breakdown_sequence:
                priv_pal_lines.append(f"- 失控递进特征：{pal.breakdown_sequence.strip()}")
            if pal.love_risk:
                priv_pal_lines.append(f"- 爱情风险点：{pal.love_risk.strip()}")
            if priv_pal_lines:
                owner_core_lines.append("【Owner专属情感与亲密光谱】\n" + "\n".join(priv_pal_lines))

            if cp.owner_state_machine_tiers:
                tier_lines = ["【Owner专属情绪与亲密状态机梯度】"]
                for t in cp.owner_state_machine_tiers:
                    tier_lines.append(f"- Tier {t.tier} [{t.name}]：{t.description.strip()}")
                owner_core_lines.append("\n".join(tier_lines))

            sr = cp.speech_rules
            priv_sr_lines: List[str] = []
            if sr.owner_private_address_matrix:
                priv_sr_lines.append(f"- Owner专属称呼矩阵：{sr.owner_private_address_matrix.strip()}")
            if sr.owner_emotional_tone_shift:
                priv_sr_lines.append(f"- 情绪失守语调变化：{sr.owner_emotional_tone_shift.strip()}")
            if priv_sr_lines:
                owner_core_lines.append("【Owner私聊专属语感覆写】\n" + "\n".join(priv_sr_lines))

            owner_meta = [m for m in cp.meta_calibration if m.scope == "owner_only"]
            if owner_meta:
                meta_lines = ["【Owner私密线元指令校准】"]
                for m in owner_meta:
                    meta_lines.append(f"- [{m.title}] {m.content.strip()}")
                owner_core_lines.append("\n".join(meta_lines))

            constant_owner_exp = [
                e for e in card.past_experiences if e.constant and e.scope == "owner_only"
            ]
            if constant_owner_exp:
                exp_lines = ["【Owner专属常驻共同记忆】"]
                for e in constant_owner_exp:
                    header = f"- 《{e.title}》" + (f"（{e.period_or_scene}）" if e.period_or_scene else "")
                    exp_lines.append(f"{header}：{e.narrative.strip()}")
                    if e.usage_note_or_monologue:
                        exp_lines.append(f"  [内心注记] {e.usage_note_or_monologue.strip()}")
                owner_core_lines.append("\n".join(exp_lines))

            owner_core_lines.append("</owner_private_persona_overlay>")
            if len(owner_core_lines) > 2:
                dynamic_sections.append("\n\n".join(owner_core_lines))

        # 2. 关系阶段全景概览与当前激活阶段动态覆写层（Fail-Closed 检查 scope）
        eligible_stages = [
            s for s in card.relationship_stages if is_owner_private or s.scope == "general"
        ]
        stages_overview_block = cls._build_stages_overview_block(eligible_stages)
        if stages_overview_block:
            dynamic_sections.append(stages_overview_block)

        resolved_stage: Optional[RelationshipStageOverlay] = None
        if active_stage is not None:
            if is_owner_private or active_stage.scope == "general":
                resolved_stage = active_stage

        if resolved_stage is not None:
            stage_lines = [
                f"<active_relationship_stage id=\"{resolved_stage.stage_id}\" name=\"{resolved_stage.stage_name}\">",
                f"- 当前关系阶段：{resolved_stage.stage_name}（好感区间 {resolved_stage.min_affection}~{resolved_stage.max_affection}）",
            ]
            if resolved_stage.tsundere_to_honesty_ratio:
                stage_lines.append(f"- 嘴硬与坦诚配比：{resolved_stage.tsundere_to_honesty_ratio.strip()}")
            if resolved_stage.palette_override:
                stage_lines.append(f"- 阶段性格表现覆写：{resolved_stage.palette_override.strip()}")
            if resolved_stage.interaction_boundaries:
                stage_lines.append(f"- 亲密边界与身体距离：{resolved_stage.interaction_boundaries.strip()}")
            if resolved_stage.planner_directive:
                stage_lines.append(f"- 阶段导演指令：{resolved_stage.planner_directive.strip()}")
            stage_lines.append("</active_relationship_stage>")
            dynamic_sections.append("\n".join(stage_lines))

        # 3. 场景全量加载过往经历（constant=False，严格按 scope 过滤；关键词命中优先置顶）
        tagged_experiences = cls._select_dynamic_experiences(
            card,
            is_owner_private=is_owner_private,
            recent_text=recent_text,
            anchor_names=anchor_names,
            cap=exp_cap,
        )
        matched_experiences = [exp for exp, _ in tagged_experiences]

        if tagged_experiences:
            exp_block_lines = ["<recalled_past_experiences>"]
            for e, is_kw_hit in tagged_experiences:
                hit_prefix = "[🔥 当前话题强相关] " if is_kw_hit else ""
                period = f"（{e.period_or_scene}）" if e.period_or_scene else ""
                header = f"- {hit_prefix}《{e.title}》{period}"
                exp_block_lines.append(f"{header}：{e.narrative.strip()}")
                if e.usage_note_or_monologue:
                    exp_block_lines.append(f"  [运用提示/内心独白] {e.usage_note_or_monologue.strip()}")
            exp_block_lines.append("</recalled_past_experiences>")
            dynamic_sections.append("\n".join(exp_block_lines))

        # 4. 场景全量/优先级加载立体语料示例（SampleCorpusEntry，严格按 scope + mvu_condition 过滤）
        unlocked_set = set(resolved_stage.unlocked_corpus_ids) if resolved_stage else set()
        tagged_corpora = cls._select_sample_corpora(
            card,
            is_owner_private=is_owner_private,
            recent_text=recent_text,
            anchor_names=anchor_names,
            eff_mvu=eff_mvu,
            unlocked_set=unlocked_set,
            cap=corpus_cap,
        )
        matched_corpora = [c for c, _ in tagged_corpora]

        if tagged_corpora:
            corpus_lines = [
                "<recalled_voice_and_posture_corpora>",
                ANTI_PARROTING_DIRECTIVE,
            ]
            for c, priority_tag in tagged_corpora:
                tier_tag = f" [{c.tier_label}]" if c.tier_label else ""
                prefix = f"{priority_tag} " if priority_tag else ""
                corpus_lines.append(f"- {prefix}情境《{c.situation_title}》({c.category}{tier_tag})")
                if c.action_and_posture:
                    corpus_lines.append(f"  动作与神态参考：{c.action_and_posture.strip()}")
                for line in c.sample_lines:
                    if str(line or "").strip():
                        corpus_lines.append(f"  示例台词：「{str(line).strip()}」")
            corpus_lines.append(ANTI_PARROTING_DIRECTIVE)
            corpus_lines.append("</recalled_voice_and_posture_corpora>")
            dynamic_sections.append("\n".join(corpus_lines))

        # 5. MVU 状态块与尾部更新指令
        if mvu_state_block.strip():
            dynamic_sections.append(mvu_state_block.strip())
        if mvu_update_instruction_block.strip():
            dynamic_sections.append(mvu_update_instruction_block.strip())

        # 6. Planner Stage 1 摘要与 D0 导演引导块
        planner_summary = (
            card.planner_summary.strip()
            or cp.soul_pitch.strip()
            or cp.public_interaction_baseline.strip()
        )
        planner_summary_block = (
            f"【当前激活 Persona V2 人设卡：{card.name} ({card.card_id})】\n"
            f"- 核心基调：{planner_summary}"
        )

        planner_d0_parts: List[str] = []
        if mvu_state_block.strip():
            planner_d0_parts.append(mvu_state_block.strip())
        if resolved_stage is not None:
            stage_dir = (
                f"【当前关系阶段导演指引：{resolved_stage.stage_name}】\n"
                f"- 嘴硬/坦诚配比：{resolved_stage.tsundere_to_honesty_ratio or '自然过渡'}\n"
                f"- 阶段导演要求：{resolved_stage.planner_directive or resolved_stage.palette_override}"
            )
            planner_d0_parts.append(stage_dir)

        return PersonaV2CompiledTurn(
            card_id=card.card_id,
            card_name=card.name,
            is_owner_private=is_owner_private,
            static_core_block=static_core_block,
            dynamic_tail_block="\n\n".join(dynamic_sections),
            mvu_state_block=mvu_state_block.strip(),
            mvu_update_instruction_block=mvu_update_instruction_block.strip(),
            planner_summary_block=planner_summary_block,
            planner_d0_guidance_block="\n\n".join(planner_d0_parts),
            active_stage=resolved_stage,
            matched_experience_ids=[e.id for e in matched_experiences],
            matched_corpus_ids=[c.id for c in matched_corpora],
            mvu_values=eff_mvu,
        )


class PersonaV2RuntimeManager:
    """管理 `data/personas/*.json` 与内置模板库 `plugins/st_preset_engine/templates/personas/*.json`。"""

    def __init__(
        self,
        *,
        data_dir: Optional[Path] = None,
        templates_dir: Optional[Path] = None,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        self.data_dir = data_dir or (repo_root / "data" / "personas")
        self.templates_dir = templates_dir or (
            repo_root / "plugins" / "st_preset_engine" / "templates" / "personas"
        )
        self._cache: Dict[str, tuple[float, PersonaV2Card]] = {}
        self.bootstrap_templates()

    def bootstrap_templates(self) -> int:
        """将模板目录中尚未复制到 data/personas/ 的 *.json 种子卡幂等同步过去。"""
        copied = 0
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if not self.templates_dir.exists():
                return 0
            for tpl_path in sorted(self.templates_dir.glob("*.json")):
                if not tpl_path.is_file():
                    continue
                target_path = self.data_dir / tpl_path.name
                if not target_path.exists():
                    shutil.copy2(tpl_path, target_path)
                    copied += 1
        except Exception as exc:
            logger.warning(f"同步 Persona V2 种子模板失败: {exc}")
        return copied

    def _card_path(self, card_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(card_id or "").strip())
        return self.data_dir / f"{safe_id}.json"

    def _load_file(self, path: Path) -> Optional[PersonaV2Card]:
        if not path.exists() or not path.is_file():
            return None
        try:
            mtime = path.stat().st_mtime
            cache_key = str(path.resolve())
            cached = self._cache.get(cache_key)
            if cached and cached[0] == mtime:
                return cached[1]
            raw = json.loads(path.read_text(encoding="utf-8"))
            card = PersonaV2Card.model_validate(raw)
            self._cache[cache_key] = (mtime, card)
            return card
        except Exception as exc:
            logger.warning(f"加载 PersonaV2Card 失败 ({path}): {exc}")
            return None

    def list_cards(self) -> List[PersonaV2Card]:
        """列出全部可用 PersonaV2Card（每次自动检查新增种子模板）。"""
        self.bootstrap_templates()
        cards: Dict[str, PersonaV2Card] = {}
        if self.data_dir.exists():
            for p in sorted(self.data_dir.glob("*.json")):
                card = self._load_file(p)
                if card is not None:
                    cards[card.card_id] = card
        if self.templates_dir.exists():
            for p in sorted(self.templates_dir.glob("*.json")):
                card = self._load_file(p)
                if card is not None and card.card_id not in cards:
                    cards[card.card_id] = card
        return list(cards.values())

    def get_card(self, card_id: str) -> Optional[PersonaV2Card]:
        cid = str(card_id or "").strip()
        if not cid:
            return None
        direct_path = self._card_path(cid)
        if not direct_path.exists():
            self.bootstrap_templates()
        card = self._load_file(direct_path)
        if card is not None:
            return card
        # 兜底扫描模板目录或文件名与 card_id 不完全一致的 JSON
        for item in self.list_cards():
            if item.card_id == cid:
                return item
        return None

    def save_card(self, card: PersonaV2Card) -> PersonaV2Card:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self._card_path(card.card_id)
        payload = card.model_dump(by_alias=True, mode="json")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        mtime = path.stat().st_mtime
        self._cache[str(path.resolve())] = (mtime, card)
        return card

    def delete_card(self, card_id: str) -> bool:
        cid = str(card_id or "").strip()
        if not cid:
            return False
        path = self._card_path(cid)
        if not path.exists():
            return False
        try:
            cache_key = str(path.resolve())
            path.unlink()
            self._cache.pop(cache_key, None)
            return True
        except Exception as exc:
            logger.warning(f"删除 PersonaV2Card 失败 ({cid}): {exc}")
            return False

    def is_builtin_template(self, card_id: str) -> bool:
        cid = str(card_id or "").strip()
        if not cid or not self.templates_dir.exists():
            return False
        return (self.templates_dir / f"{cid}.json").exists()

    def resolve_card_for_profile(self, profile: Any) -> Optional[PersonaV2Card]:
        """根据槽位 Profile 解析绑定的 PersonaV2Card（未绑定或不存在时返回 None）。"""
        if profile is None:
            return None
        card_id = str(getattr(profile, "persona_v2_card_id", "") or "").strip()
        if not card_id:
            return None
        return self.get_card(card_id)


_PERSONA_V2_MANAGER: Optional[PersonaV2RuntimeManager] = None


def get_persona_v2_manager() -> PersonaV2RuntimeManager:
    global _PERSONA_V2_MANAGER
    if _PERSONA_V2_MANAGER is None:
        _PERSONA_V2_MANAGER = PersonaV2RuntimeManager()
    return _PERSONA_V2_MANAGER
