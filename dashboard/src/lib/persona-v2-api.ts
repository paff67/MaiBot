/**
 * Persona V2 & MVU 角色卡工作台 API 客户端与契约类型
 *
 * 严格对应后端 `src/companion_vault/persona_v2_engine.py` 中的 `PersonaV2Card` Pydantic 模型
 * 以及 `src/webui/routers/persona_v2.py` 暴露的 `/api/webui/persona-v2` REST 接口。
 */

import * as configApi from '@/lib/config-api'
import { backendApi } from '@/lib/http'

const API_BASE = '/api/webui/persona-v2'
export const PERSONA_V2_UPDATED_EVENT = 'maibot:persona-v2-updated'

export type ScopeType = 'general' | 'owner_only'
export type VisibilityType = 'PUBLIC' | 'PRIVATE'
export type PersonaBindTarget = 'active' | 'sister_younger' | 'sister_elder'

export type PastExperienceCategory = 'timeline_milestone' | 'life_vignette'

export type SampleCorpusCategory =
  | 'daily_echo'
  | 'flirt_scene'
  | 'intimacy_gradient_dialogue'
  | 'intimacy_gradient_posture'
  | 'romance_lock_corpus'

export interface StateMachineTier {
  tier: number
  name: string
  description: string
}

export interface MetaCalibrationEntry {
  id: string
  title: string
  content: string
  scope: ScopeType
}

export interface PersonalityPaletteV2 {
  daily_baseline: string
  coexistence_rule: string
  affection_leak_style: string
  desire_and_intimacy_drive: string
  breakdown_sequence: string
  love_risk: string
}

export interface SpeechRulesV2 {
  public_address_matrix: string
  modal_particles: string
  sentence_rhythm: string
  negative_constraints: string[]
  owner_private_address_matrix: string
  owner_emotional_tone_shift: string
}

export interface CorePersonaV2 {
  soul_pitch: string
  basic_info: string
  public_visual_and_wardrobe: string
  public_interaction_baseline: string
  owner_relationship_bond: string
  owner_private_wardrobe_and_anchors: string
  personality_palette: PersonalityPaletteV2
  owner_state_machine_tiers: StateMachineTier[]
  meta_calibration: MetaCalibrationEntry[]
  speech_rules: SpeechRulesV2
}

export interface PastExperienceEntry {
  id: string
  title: string
  period_or_scene: string
  category: PastExperienceCategory
  narrative: string
  usage_note_or_monologue: string
  constant: boolean
  trigger_keywords: string[]
  scope: ScopeType
}

export interface SampleCorpusEntry {
  id: string
  category: SampleCorpusCategory
  situation_title: string
  tier_label: string
  action_and_posture: string
  sample_lines: string[]
  trigger_keywords: string[]
  mvu_condition: string
  scope: ScopeType
}

export interface RelationshipStageOverlay {
  stage_id: string
  stage_name: string
  min_affection: number
  max_affection: number
  require_love_locked: boolean
  scope: ScopeType
  tsundere_to_honesty_ratio: string
  planner_directive: string
  palette_override: string
  interaction_boundaries: string
  unlocked_corpus_ids: string[]
}

export interface MVUAffinityGuardConfig {
  enabled: boolean
  target_var: string
  lock_var: string
  min_delta_per_turn: number
  max_delta_per_turn: number
  cap_before_lock: number
}

export interface MVUVariableSchema {
  key: string
  label: string
  type: 'number' | 'string' | 'boolean'
  default_value: unknown
  min?: number | null
  max?: number | null
  enum_values: string[]
  monotonic_true: boolean
  visibility: VisibilityType
  description: string
  shared?: boolean
  shared_key?: string
}

export interface MVUConfig {
  enabled: boolean
  shared_namespace?: string
  update_instruction: string
  affinity_guard: MVUAffinityGuardConfig
  variables: MVUVariableSchema[]
}

export interface PersonaV2Card {
  schema?: string
  schema_name?: string
  version: string
  card_id: string
  name: string
  aliases: string[]
  role_tag: string
  avatar_url: string
  planner_summary: string
  core_persona: CorePersonaV2
  past_experiences: PastExperienceEntry[]
  sample_corpora: SampleCorpusEntry[]
  relationship_stages: RelationshipStageOverlay[]
  mvu_config: MVUConfig
}

export interface PersonaV2ActiveStageInfo {
  stage_id: string
  stage_name: string
  min_affection?: number
  max_affection?: number
  require_love_locked?: boolean
}

export interface PersonaV2CardSummaryItem {
  card_id: string
  name: string
  role_tag: string
  soul_pitch: string
  avatar_url: string
  aliases: string[]
  experience_count: number
  corpus_count: number
  stage_count: number
  bound_targets: PersonaBindTarget[]
  card?: PersonaV2Card
}

export interface PersonaV2CardListResponse {
  cards: PersonaV2CardSummaryItem[]
  bindings: {
    active?: string
    sister_younger?: string
    sister_elder?: string
  }
}

export interface PersonaV2CardDetailResponse {
  card: PersonaV2Card
  mvu_state: Record<string, unknown>
  active_stage: PersonaV2ActiveStageInfo | null
  bound_targets: PersonaBindTarget[]
}

export interface PersonaV2MvuStateResponse {
  card_id: string
  mvu_state: Record<string, unknown>
  active_stage: PersonaV2ActiveStageInfo | null
}

/** `/chat` 监控面板中的 MVU 状态演进遥测结构 */
export interface MaisakaMvuChange {
  key: string
  label?: string
  old: unknown
  new: unknown
  delta?: number | null
}

export interface MaisakaMvuWatch {
  card_id?: string
  analysis?: string
  changes?: MaisakaMvuChange[]
  active_stage?: string | { stage_id?: string; stage_name?: string } | null
}

// ─── 默认结构与安全归一化辅助函数 ──────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function asNumber(value: unknown, fallback = 0): number {
  const num = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(num) ? num : fallback
}

function asBoolean(value: unknown, fallback = false): boolean {
  if (typeof value === 'boolean') return value
  if (value === 'true' || value === 1) return true
  if (value === 'false' || value === 0) return false
  return fallback
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => String(item ?? '').trim()).filter(Boolean)
}

function asScope(value: unknown, fallback: ScopeType = 'general'): ScopeType {
  return value === 'owner_only' || value === 'general' ? value : fallback
}

function asVisibility(value: unknown, fallback: VisibilityType = 'PUBLIC'): VisibilityType {
  return value === 'PRIVATE' || value === 'PUBLIC' ? value : fallback
}

export function createDefaultMvuVariables(): MVUVariableSchema[] {
  return [
    {
      key: 'affection',
      label: '主观好感度',
      type: 'number',
      default_value: 20,
      min: 0,
      max: 100,
      enum_values: [],
      monotonic_true: false,
      visibility: 'PRIVATE',
      description: '仅在 Owner 私聊中演进的主观好感度（0-100），未确认关系前上限为 99。',
    },
    {
      key: 'love_locked',
      label: '恋爱关系锁定',
      type: 'boolean',
      default_value: false,
      min: null,
      max: null,
      enum_values: [],
      monotonic_true: true,
      visibility: 'PRIVATE',
      description: '单向不可逆恋爱确认锁（一旦为 true 不可回退，突破 99 好感上限直达 100）。',
    },
    {
      key: 'intimacy_tier',
      label: '当前亲密档位',
      type: 'string',
      default_value: '日常态',
      min: null,
      max: null,
      enum_values: ['日常态', '微醺破防', '潜伏异样', '升温试探', '失守沉溺', '事后余温'],
      monotonic_true: false,
      visibility: 'PRIVATE',
      description: 'Owner 私聊专属亲密状态档位标签。',
    },
    {
      key: 'mood',
      label: '当前表层心情',
      type: 'string',
      default_value: '平静自然',
      min: null,
      max: null,
      enum_values: [],
      monotonic_true: false,
      visibility: 'PUBLIC',
      description: '通用公开心情状态，群聊与私聊均可感知。',
    },
    {
      key: 'current_activity',
      label: '当前正在做的事',
      type: 'string',
      default_value: '日常待命',
      min: null,
      max: null,
      enum_values: [],
      monotonic_true: false,
      visibility: 'PUBLIC',
      description: '通用公开活动与生活轨迹锚点。',
    },
  ]
}

export function createDefaultRelationshipStages(): RelationshipStageOverlay[] {
  return [
    {
      stage_id: 'stage_0_19',
      stage_name: '0-19 · 礼貌防线与身份克制',
      min_affection: 0,
      max_affection: 19,
      require_love_locked: false,
      scope: 'owner_only',
      tsundere_to_honesty_ratio: '9:1（90% 维持日常身份边界与嘴硬，10% 无意识细节照拂）',
      planner_directive: '保持原有身份距离感，不主动开启暧昧话题，被调侃时迅速转移话题或冷淡带过。',
      palette_override: '以日常底色为主，严格收敛温柔泄漏。',
      interaction_boundaries: '仅限日常关心与眼神交汇，拒绝越界肢体接触。',
      unlocked_corpus_ids: [],
    },
    {
      stage_id: 'stage_20_39',
      stage_name: '20-39 · 熟稔默契与微澜松动',
      min_affection: 20,
      max_affection: 39,
      require_love_locked: false,
      scope: 'owner_only',
      tsundere_to_honesty_ratio: '7:3（70% 嘴硬或矜持，30% 行动上的偏心与默契）',
      planner_directive: '允许在日常对话中流露专属习惯与冷幽默调侃，嘴上不饶人但行动总是顺着对方。',
      palette_override: '偶发温柔泄漏，被戳破时会耳根微红或假装镇定。',
      interaction_boundaries: '接受日常顺手的肢体触碰（递东西、拍肩、整理衣领）。',
      unlocked_corpus_ids: [],
    },
    {
      stage_id: 'stage_40_59',
      stage_name: '40-59 · 暧昧拉扯与双层试探',
      min_affection: 40,
      max_affection: 59,
      require_love_locked: false,
      scope: 'owner_only',
      tsundere_to_honesty_ratio: '5:5（半真半假的试探与退缩交替）',
      planner_directive: '解锁调情场景语料；独处时眼神与距离感明显拉近，会主动制造独处借口但仍保留退路。',
      palette_override: '温柔泄漏与轻微占有欲并存，开始在意对方身边的其他人。',
      interaction_boundaries: '允许牵手、靠肩、深夜近距离独处与轻度耳语调情。',
      unlocked_corpus_ids: [],
    },
    {
      stage_id: 'stage_60_79',
      stage_name: '60-79 · 防线动摇与暗潮失守',
      min_affection: 60,
      max_affection: 79,
      require_love_locked: false,
      scope: 'owner_only',
      tsundere_to_honesty_ratio: '3:7（嘴上仅剩最后的矜持，身体反应与语气完全诚实）',
      planner_directive: '解锁分级对白与体态语料前段；面对直球逼近时防线明显松动，呼吸与停顿增多。',
      palette_override: '欲望底色浮现，失控递进特征进入第 1-2 档。',
      interaction_boundaries: '允许拥抱、亲吻额头/脸颊与深层情绪依恋倾诉。',
      unlocked_corpus_ids: [],
    },
    {
      stage_id: 'stage_80_99',
      stage_name: '80-99 · 临门一脚与极致张力（未锁上限）',
      min_affection: 80,
      max_affection: 99,
      require_love_locked: false,
      scope: 'owner_only',
      tsundere_to_honesty_ratio: '1:9（几乎不再掩饰深情，只差最后一层关系确认）',
      planner_directive: '处于确认关系前的最高张力区间（好感上限卡在 99）；主动索取陪伴，直面爱情风险点。',
      palette_override: '全部三档表演状态机自由切换，情感浓度拉满。',
      interaction_boundaries: '深度亲密互动全开，等待双向明确承诺以触发 love_locked。',
      unlocked_corpus_ids: [],
    },
    {
      stage_id: 'stage_100_locked',
      stage_name: '100 · 恋爱锁定与毫无保留（Love Locked）',
      min_affection: 100,
      max_affection: 100,
      require_love_locked: true,
      scope: 'owner_only',
      tsundere_to_honesty_ratio: '0:10（直球坦诚，偶尔的情趣傲娇也是为了撒娇）',
      planner_directive: '恋爱模式全量覆盖生效！卸下一切身份包袱，展现确认关系后的独占欲、松弛感与长期相守感。',
      palette_override: '恋爱锁定专属调色盘覆盖：外人面前依旧得体，私下对 Owner 完全敞开。',
      interaction_boundaries: '无防线恋人亲密关系，全档位语料解锁。',
      unlocked_corpus_ids: [],
    },
  ]
}

export function createDefaultPersonaV2Card(partial?: Partial<PersonaV2Card>): PersonaV2Card {
  const idSuffix = Math.random().toString(36).slice(2, 7)
  return normalizePersonaV2Card({
    schema: 'maibot.persona.v2',
    version: '2.0',
    card_id: partial?.card_id || `custom_persona_${idSuffix}`,
    name: partial?.name || '新建人物卡片',
    aliases: partial?.aliases || [],
    role_tag: partial?.role_tag || '自定义角色 · 双域隔离人设',
    avatar_url: partial?.avatar_url || '',
    planner_summary:
      partial?.planner_summary ||
      '在公开场合保持鲜明立体的日常性格底色；仅在与 Owner 私聊时按好感阶段渐进展露深层羁绊。',
    core_persona: partial?.core_persona ?? {
      soul_pitch: '',
      basic_info: '',
      public_visual_and_wardrobe: '',
      public_interaction_baseline: '',
      owner_relationship_bond: '',
      owner_private_wardrobe_and_anchors: '',
      personality_palette: {
        daily_baseline: '',
        coexistence_rule: '',
        affection_leak_style: '',
        desire_and_intimacy_drive: '',
        breakdown_sequence: '',
        love_risk: '',
      },
      owner_state_machine_tiers: [
        { tier: 1, name: '一档 · 日常矜持态', description: '维持惯常身份与克制语气，细节处藏着偏心。' },
        { tier: 2, name: '二档 · 破防松动态', description: '被拉近距离或直球击中时，语速变慢、嘴硬心软。' },
        { tier: 3, name: '三档 · 失守沉溺态', description: '彻底卸下防备，声音放轻，表现出强烈的依恋与占有欲。' },
      ],
      meta_calibration: [],
      speech_rules: {
        public_address_matrix: '',
        modal_particles: '',
        sentence_rhythm: '',
        negative_constraints: [],
        owner_private_address_matrix: '',
        owner_emotional_tone_shift: '',
      },
    },
    past_experiences: partial?.past_experiences ?? [],
    sample_corpora: partial?.sample_corpora ?? [],
    relationship_stages: partial?.relationship_stages ?? createDefaultRelationshipStages(),
    mvu_config: partial?.mvu_config ?? {
      enabled: true,
      update_instruction:
        '每轮回复末尾根据本轮互动质量输出 <UpdateVariable> 块，先写 <Analysis> 归因，再输出 <JSONPatch> 更新变量。',
      affinity_guard: {
        enabled: true,
        target_var: 'affection',
        lock_var: 'love_locked',
        min_delta_per_turn: -1,
        max_delta_per_turn: 5,
        cap_before_lock: 99,
      },
      variables: createDefaultMvuVariables(),
    },
  })
}

export function normalizePersonaV2Card(raw: unknown): PersonaV2Card {
  const obj = isRecord(raw) ? raw : {}
  const coreRaw = isRecord(obj.core_persona) ? obj.core_persona : {}
  const paletteRaw = isRecord(coreRaw.personality_palette) ? coreRaw.personality_palette : {}
  const speechRaw = isRecord(coreRaw.speech_rules) ? coreRaw.speech_rules : {}
  const mvuRaw = isRecord(obj.mvu_config) ? obj.mvu_config : {}
  const guardRaw = isRecord(mvuRaw.affinity_guard) ? mvuRaw.affinity_guard : {}

  const tiersRaw = Array.isArray(coreRaw.owner_state_machine_tiers)
    ? coreRaw.owner_state_machine_tiers
    : []
  const normalizedTiers: StateMachineTier[] =
    tiersRaw.length > 0
      ? tiersRaw.map((item, idx) => {
          const t = isRecord(item) ? item : {}
          return {
            tier: asNumber(t.tier, idx + 1),
            name: asString(t.name, `第 ${idx + 1} 档`),
            description: asString(t.description),
          }
        })
      : [
          { tier: 1, name: '一档 · 日常掩饰态', description: '' },
          { tier: 2, name: '二档 · 动摇破防态', description: '' },
          { tier: 3, name: '三档 · 彻底失守态', description: '' },
        ]

  const metaRaw = Array.isArray(coreRaw.meta_calibration) ? coreRaw.meta_calibration : []
  const normalizedMeta: MetaCalibrationEntry[] = metaRaw.map((item, idx) => {
    const m = isRecord(item) ? item : {}
    return {
      id: asString(m.id, `meta_${idx + 1}`),
      title: asString(m.title),
      content: asString(m.content),
      scope: asScope(m.scope, 'general'),
    }
  })

  const expRaw = Array.isArray(obj.past_experiences) ? obj.past_experiences : []
  const normalizedExperiences: PastExperienceEntry[] = expRaw.map((item, idx) => {
    const e = isRecord(item) ? item : {}
    const cat = e.category === 'life_vignette' ? 'life_vignette' : 'timeline_milestone'
    return {
      id: asString(e.id, `exp_${idx + 1}`),
      title: asString(e.title),
      period_or_scene: asString(e.period_or_scene),
      category: cat,
      narrative: asString(e.narrative),
      usage_note_or_monologue: asString(e.usage_note_or_monologue),
      constant: asBoolean(e.constant, cat === 'timeline_milestone'),
      trigger_keywords: asStringArray(e.trigger_keywords),
      scope: asScope(e.scope, 'general'),
    }
  })

  const corporaRaw = Array.isArray(obj.sample_corpora) ? obj.sample_corpora : []
  const validCategories = new Set<SampleCorpusCategory>([
    'daily_echo',
    'flirt_scene',
    'intimacy_gradient_dialogue',
    'intimacy_gradient_posture',
    'romance_lock_corpus',
  ])
  const normalizedCorpora: SampleCorpusEntry[] = corporaRaw.map((item, idx) => {
    const c = isRecord(item) ? item : {}
    const rawCat = asString(c.category, 'daily_echo') as SampleCorpusCategory
    const category: SampleCorpusCategory = validCategories.has(rawCat) ? rawCat : 'daily_echo'
    const defaultScope: ScopeType = category === 'daily_echo' ? 'general' : 'owner_only'
    return {
      id: asString(c.id, `corpus_${idx + 1}`),
      category,
      situation_title: asString(c.situation_title),
      tier_label: asString(c.tier_label),
      action_and_posture: asString(c.action_and_posture),
      sample_lines: Array.isArray(c.sample_lines)
        ? c.sample_lines.map((line) => String(line ?? ''))
        : [],
      trigger_keywords: asStringArray(c.trigger_keywords),
      mvu_condition: asString(c.mvu_condition),
      scope: asScope(c.scope, defaultScope),
    }
  })

  const stagesRaw = Array.isArray(obj.relationship_stages) ? obj.relationship_stages : []
  const normalizedStages: RelationshipStageOverlay[] = stagesRaw.map((item, idx) => {
    const s = isRecord(item) ? item : {}
    return {
      stage_id: asString(s.stage_id, `stage_${idx + 1}`),
      stage_name: asString(s.stage_name, `阶段 ${idx + 1}`),
      min_affection: asNumber(s.min_affection, 0),
      max_affection: asNumber(s.max_affection, 100),
      require_love_locked: asBoolean(s.require_love_locked, false),
      scope: asScope(s.scope, 'owner_only'),
      tsundere_to_honesty_ratio: asString(s.tsundere_to_honesty_ratio),
      planner_directive: asString(s.planner_directive),
      palette_override: asString(s.palette_override),
      interaction_boundaries: asString(s.interaction_boundaries),
      unlocked_corpus_ids: asStringArray(s.unlocked_corpus_ids),
    }
  })

  const varsRaw = Array.isArray(mvuRaw.variables) ? mvuRaw.variables : []
  const normalizedVars: MVUVariableSchema[] =
    varsRaw.length > 0
      ? varsRaw.map((item, idx) => {
          const v = isRecord(item) ? item : {}
          const rawType = asString(v.type, 'string')
          const type: 'number' | 'string' | 'boolean' =
            rawType === 'number' || rawType === 'boolean' ? rawType : 'string'
          return {
            key: asString(v.key, `var_${idx + 1}`),
            label: asString(v.label, asString(v.key, `变量 ${idx + 1}`)),
            type,
            default_value:
              v.default_value !== undefined
                ? v.default_value
                : type === 'number'
                  ? 0
                  : type === 'boolean'
                    ? false
                    : '',
            min: v.min === null || v.min === undefined || v.min === '' ? null : asNumber(v.min, 0),
            max: v.max === null || v.max === undefined || v.max === '' ? null : asNumber(v.max, 100),
            enum_values: asStringArray(v.enum_values),
            monotonic_true: asBoolean(v.monotonic_true, false),
            visibility: asVisibility(v.visibility, 'PUBLIC'),
            description: asString(v.description),
            shared: asBoolean(v.shared, false),
            shared_key: asString(v.shared_key),
          }
        })
      : createDefaultMvuVariables()

  return {
    schema: asString(obj.schema || obj.schema_name, 'maibot.persona.v2'),
    schema_name: asString(obj.schema_name || obj.schema, 'maibot.persona.v2'),
    version: asString(obj.version, '2.0'),
    card_id: asString(obj.card_id),
    name: asString(obj.name, '未命名角色卡'),
    aliases: asStringArray(obj.aliases),
    role_tag: asString(obj.role_tag),
    avatar_url: asString(obj.avatar_url),
    planner_summary: asString(obj.planner_summary),
    core_persona: {
      soul_pitch: asString(coreRaw.soul_pitch),
      basic_info: asString(coreRaw.basic_info),
      public_visual_and_wardrobe: asString(coreRaw.public_visual_and_wardrobe),
      public_interaction_baseline: asString(coreRaw.public_interaction_baseline),
      owner_relationship_bond: asString(coreRaw.owner_relationship_bond),
      owner_private_wardrobe_and_anchors: asString(coreRaw.owner_private_wardrobe_and_anchors),
      personality_palette: {
        daily_baseline: asString(paletteRaw.daily_baseline),
        coexistence_rule: asString(paletteRaw.coexistence_rule),
        affection_leak_style: asString(paletteRaw.affection_leak_style),
        desire_and_intimacy_drive: asString(paletteRaw.desire_and_intimacy_drive),
        breakdown_sequence: asString(paletteRaw.breakdown_sequence),
        love_risk: asString(paletteRaw.love_risk),
      },
      owner_state_machine_tiers: normalizedTiers,
      meta_calibration: normalizedMeta,
      speech_rules: {
        public_address_matrix: asString(speechRaw.public_address_matrix),
        modal_particles: asString(speechRaw.modal_particles),
        sentence_rhythm: asString(speechRaw.sentence_rhythm),
        negative_constraints: asStringArray(speechRaw.negative_constraints),
        owner_private_address_matrix: asString(speechRaw.owner_private_address_matrix),
        owner_emotional_tone_shift: asString(speechRaw.owner_emotional_tone_shift),
      },
    },
    past_experiences: normalizedExperiences,
    sample_corpora: normalizedCorpora,
    relationship_stages: normalizedStages,
    mvu_config: {
      enabled: asBoolean(mvuRaw.enabled, true),
      shared_namespace: asString(mvuRaw.shared_namespace),
      update_instruction: asString(mvuRaw.update_instruction),
      affinity_guard: {
        enabled: asBoolean(guardRaw.enabled, true),
        target_var: asString(guardRaw.target_var, 'affection'),
        lock_var: asString(guardRaw.lock_var, 'love_locked'),
        min_delta_per_turn: asNumber(guardRaw.min_delta_per_turn, -1),
        max_delta_per_turn: asNumber(guardRaw.max_delta_per_turn, 5),
        cap_before_lock: asNumber(guardRaw.cap_before_lock, 99),
      },
      variables: normalizedVars,
    },
  }
}

function normalizeActiveStage(raw: unknown): PersonaV2ActiveStageInfo | null {
  if (!raw) return null
  if (typeof raw === 'string') {
    const trimmed = raw.trim()
    return trimmed ? { stage_id: trimmed, stage_name: trimmed } : null
  }
  if (isRecord(raw)) {
    const stage_id = asString(raw.stage_id || raw.id)
    const stage_name = asString(raw.stage_name || raw.name || stage_id)
    if (!stage_id && !stage_name) return null
    return {
      stage_id: stage_id || stage_name,
      stage_name: stage_name || stage_id,
      ...(raw.min_affection !== undefined ? { min_affection: asNumber(raw.min_affection, 0) } : {}),
      ...(raw.max_affection !== undefined ? { max_affection: asNumber(raw.max_affection, 100) } : {}),
      ...(raw.require_love_locked !== undefined
        ? { require_love_locked: asBoolean(raw.require_love_locked) }
        : {}),
    }
  }
  return null
}

function extractBoundTargets(
  itemRaw: Record<string, unknown>,
  cardId: string,
  bindings: { active?: string; sister_younger?: string; sister_elder?: string }
): PersonaBindTarget[] {
  const targets = new Set<PersonaBindTarget>()
  if (Array.isArray(itemRaw.bound_targets)) {
    for (const t of itemRaw.bound_targets) {
      if (t === 'active' || t === 'sister_younger' || t === 'sister_elder') {
        targets.add(t)
      }
    }
  }
  if (Array.isArray(itemRaw.bound_slots)) {
    for (const t of itemRaw.bound_slots) {
      if (t === 'active' || t === 'sister_younger' || t === 'sister_elder') {
        targets.add(t)
      }
    }
  }
  if (itemRaw.is_active === true || (cardId && bindings.active === cardId)) {
    targets.add('active')
  }
  if (
    itemRaw.is_sister_younger === true ||
    itemRaw.is_younger_slot === true ||
    (cardId && bindings.sister_younger === cardId)
  ) {
    targets.add('sister_younger')
  }
  if (
    itemRaw.is_sister_elder === true ||
    itemRaw.is_elder_slot === true ||
    (cardId && bindings.sister_elder === cardId)
  ) {
    targets.add('sister_elder')
  }
  return Array.from(targets)
}

function unwrapCardDetailResponse(raw: unknown): PersonaV2CardDetailResponse {
  const top = isRecord(raw) ? raw : {}
  const data = isRecord(top.data) ? top.data : top
  const rawCard = isRecord(data.card) ? data.card : data
  const card = normalizePersonaV2Card(rawCard)

  // 提取实时 MVU 变量值，若后端尚未写入任何值则用 schema 默认值填充展示
  const rawMvu =
    (isRecord(data.mvu_state) && data.mvu_state) ||
    (isRecord(data.live_mvu) && data.live_mvu) ||
    (isRecord(data.live_variables) && data.live_variables) ||
    (isRecord(data.mvu_values) && data.mvu_values) ||
    (isRecord(data.variables) && !Array.isArray(data.variables) ? data.variables : null) ||
    {}

  const mvu_state: Record<string, unknown> = {}
  for (const v of card.mvu_config.variables) {
    mvu_state[v.key] = rawMvu[v.key] !== undefined ? rawMvu[v.key] : v.default_value
  }
  for (const [k, v] of Object.entries(rawMvu)) {
    if (!(k in mvu_state)) {
      mvu_state[k] = v
    }
  }

  const active_stage =
    normalizeActiveStage(data.active_stage ?? data.current_stage) ??
    computeActiveStageFromState(card, mvu_state)

  const bindingsRaw = isRecord(data.bindings) ? data.bindings : {}
  const bound_targets = extractBoundTargets(data, card.card_id, {
    active: asString(bindingsRaw.active || data.active_card_id),
    sister_younger: asString(bindingsRaw.sister_younger || data.younger_card_id),
    sister_elder: asString(bindingsRaw.sister_elder || data.elder_card_id),
  })

  return {
    card,
    mvu_state,
    active_stage,
    bound_targets,
  }
}

/** 根据当前 MVU 变量状态本地推算命中的分阶段覆盖（当后端未显式返回 active_stage 时兜底） */
export function computeActiveStageFromState(
  card: PersonaV2Card,
  mvuState: Record<string, unknown>
): PersonaV2ActiveStageInfo | null {
  if (!card.relationship_stages.length) return null
  const affection = asNumber(mvuState.affection, 20)
  const lockVar = card.mvu_config.affinity_guard.lock_var || 'love_locked'
  const loveLocked = asBoolean(
    mvuState[lockVar] ?? mvuState.love_locked ?? mvuState.twin_harmony_locked,
    false
  )

  // 优先匹配 require_love_locked 的阶段（当 love_locked 为 true）
  if (loveLocked) {
    const lockedLookup = affection > 100 ? Math.max(101, Math.ceil(affection)) : Math.floor(affection)
    const lockedStage =
      card.relationship_stages.find(
        (s) =>
          s.require_love_locked &&
          ((affection >= s.min_affection && affection <= s.max_affection) ||
            (lockedLookup >= s.min_affection && lockedLookup <= s.max_affection))
      ) ?? card.relationship_stages.find((s) => s.require_love_locked)
    if (lockedStage) {
      return {
        stage_id: lockedStage.stage_id,
        stage_name: lockedStage.stage_name,
        min_affection: lockedStage.min_affection,
        max_affection: lockedStage.max_affection,
        require_love_locked: lockedStage.require_love_locked,
      }
    }
  }

  const cappedAffection = loveLocked
    ? affection
    : Math.min(affection, card.mvu_config.affinity_guard.cap_before_lock ?? 99)
  const flooredAffection = Math.max(0, Math.floor(cappedAffection))

  const matched = card.relationship_stages.find(
    (s) =>
      !s.require_love_locked &&
      ((flooredAffection >= s.min_affection && flooredAffection <= s.max_affection) ||
        (cappedAffection >= s.min_affection && cappedAffection <= s.max_affection))
  )
  if (!matched) return null
  return {
    stage_id: matched.stage_id,
    stage_name: matched.stage_name,
    min_affection: matched.min_affection,
    max_affection: matched.max_affection,
    require_love_locked: matched.require_love_locked,
  }
}

function notifyPersonaV2Updated(): void {
  configApi.invalidateBotConfigCache?.()
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(PERSONA_V2_UPDATED_EVENT))
    window.dispatchEvent(new Event(configApi.BOT_CONFIG_UPDATED_EVENT))
  }
}

// ─── REST API 调用函数 ─────────────────────────────────────────────────

/**
 * 获取全部 Persona V2 角色卡列表与槽位绑定概况
 */
export async function listPersonaV2Cards(): Promise<PersonaV2CardListResponse> {
  const raw = await backendApi.get<unknown>(`${API_BASE}/cards`, {
    cache: 'no-store',
    errorMessage: '获取角色卡列表失败',
  })

  const top = isRecord(raw) ? raw : {}
  const data = isRecord(top.data) ? top.data : top
  const rawList = Array.isArray(raw)
    ? raw
    : Array.isArray(data.cards)
      ? data.cards
      : Array.isArray(data.items)
        ? data.items
        : []

  const bindingsRaw = isRecord(data.bindings) ? data.bindings : {}
  const bindings = {
    active: asString(bindingsRaw.active || data.active_card_id) || undefined,
    sister_younger:
      asString(bindingsRaw.sister_younger || bindingsRaw.younger || data.younger_card_id) ||
      undefined,
    sister_elder:
      asString(bindingsRaw.sister_elder || bindingsRaw.elder || data.elder_card_id) || undefined,
  }

  const cards: PersonaV2CardSummaryItem[] = rawList.map((entry) => {
    const item = isRecord(entry) ? entry : {}
    const hasEmbeddedCard = isRecord(item.card) || isRecord(item.core_persona)
    const normalizedCard = hasEmbeddedCard
      ? normalizePersonaV2Card(isRecord(item.card) ? item.card : item)
      : undefined
    const card_id = asString(item.card_id, normalizedCard?.card_id ?? '')
    const name = asString(item.name, normalizedCard?.name ?? card_id)
    const role_tag = asString(item.role_tag, normalizedCard?.role_tag ?? '')
    const soul_pitch = asString(
      item.soul_pitch,
      normalizedCard?.core_persona.soul_pitch ?? asString(item.planner_summary)
    )
    const avatar_url = asString(item.avatar_url, normalizedCard?.avatar_url ?? '')
    const aliases = asStringArray(item.aliases ?? normalizedCard?.aliases)

    const countsObj = isRecord(item.counts) ? item.counts : {}
    const experience_count = asNumber(
      item.experience_count ?? item.past_experiences_count ?? countsObj.experiences,
      normalizedCard?.past_experiences.length ?? 0
    )
    const corpus_count = asNumber(
      item.corpus_count ?? item.sample_corpora_count ?? countsObj.corpora,
      normalizedCard?.sample_corpora.length ?? 0
    )
    const stage_count = asNumber(
      item.stage_count ?? item.relationship_stages_count ?? countsObj.stages,
      normalizedCard?.relationship_stages.length ?? 0
    )

    const bound_targets = extractBoundTargets(item, card_id, bindings)

    return {
      card_id,
      name,
      role_tag,
      soul_pitch,
      avatar_url,
      aliases,
      experience_count,
      corpus_count,
      stage_count,
      bound_targets,
      ...(normalizedCard ? { card: normalizedCard } : {}),
    }
  })

  return { cards, bindings }
}

/**
 * 获取单张角色卡完整定义 + CompanionTruthDB 实时 MVU 变量值 + 当前命中阶段
 */
export async function getPersonaV2Card(cardId: string): Promise<PersonaV2CardDetailResponse> {
  const raw = await backendApi.get<unknown>(
    `${API_BASE}/cards/${encodeURIComponent(cardId)}`,
    {
      cache: 'no-store',
      errorMessage: `获取角色卡 ${cardId} 详情失败`,
    }
  )
  return unwrapCardDetailResponse(raw)
}

/**
 * 保存/更新角色卡定义并同步至 data/personas/{card_id}.json 与 bot_config.toml
 */
export async function savePersonaV2Card(
  cardId: string,
  card: PersonaV2Card
): Promise<PersonaV2CardDetailResponse> {
  const payload: PersonaV2Card = {
    ...card,
    schema: 'maibot.persona.v2',
    schema_name: 'maibot.persona.v2',
    card_id: cardId,
  }
  const raw = await backendApi.put<unknown>(
    `${API_BASE}/cards/${encodeURIComponent(cardId)}`,
    {
      body: payload,
      errorMessage: `保存角色卡 ${card.name || cardId} 失败`,
    }
  )
  notifyPersonaV2Updated()
  return unwrapCardDetailResponse(raw)
}

/**
 * 创建新角色卡并自动在 bot_config.toml 注册对应人设档案
 */
export async function createPersonaV2Card(
  card: PersonaV2Card
): Promise<PersonaV2CardDetailResponse> {
  const payload: PersonaV2Card = {
    ...card,
    schema: 'maibot.persona.v2',
    schema_name: 'maibot.persona.v2',
  }
  const raw = await backendApi.post<unknown>(`${API_BASE}/cards`, {
    body: payload,
    errorMessage: '新建角色卡失败',
  })
  notifyPersonaV2Updated()
  return unwrapCardDetailResponse(raw)
}

/**
 * 删除自定义角色卡
 */
export async function deletePersonaV2Card(cardId: string): Promise<void> {
  await backendApi.delete(`${API_BASE}/cards/${encodeURIComponent(cardId)}`, {
    errorMessage: '删除角色卡失败',
  })
  notifyPersonaV2Updated()
}

/**
 * 一键将角色卡绑定至当前主卡 (`active`)、妹妹槽位 (`sister_younger`) 或姐姐槽位 (`sister_elder`)
 */
export async function bindPersonaV2Card(
  cardId: string,
  target: PersonaBindTarget
): Promise<Record<string, unknown>> {
  const result = await backendApi.post<Record<string, unknown>>(
    `${API_BASE}/cards/${encodeURIComponent(cardId)}/bind`,
    {
      body: { target },
      errorMessage: '绑定角色卡槽位失败',
    }
  )
  notifyPersonaV2Updated()
  return result
}

/**
 * 手动修改并持久化角色卡在 CompanionTruthDB 中的实时 MVU 变量值
 */
export async function updatePersonaV2MvuState(
  cardId: string,
  values: Record<string, unknown>
): Promise<PersonaV2MvuStateResponse> {
  const raw = await backendApi.put<unknown>(
    `${API_BASE}/cards/${encodeURIComponent(cardId)}/mvu-state`,
    {
      body: { values },
      errorMessage: '保存实时 MVU 变量值失败',
    }
  )
  const top = isRecord(raw) ? raw : {}
  const data = isRecord(top.data) ? top.data : top
  const mvu_state =
    (isRecord(data.mvu_state) && data.mvu_state) ||
    (isRecord(data.live_mvu) && data.live_mvu) ||
    (isRecord(data.values) && data.values) ||
    values
  return {
    card_id: asString(data.card_id, cardId),
    mvu_state,
    active_stage: normalizeActiveStage(data.active_stage ?? data.current_stage),
  }
}

/**
 * 将角色卡的实时 MVU 变量重置回 mvu_config.variables 定义的初始默认值
 */
export async function resetPersonaV2MvuState(
  cardId: string
): Promise<PersonaV2MvuStateResponse> {
  const raw = await backendApi.post<unknown>(
    `${API_BASE}/cards/${encodeURIComponent(cardId)}/mvu-reset`,
    {
      body: {},
      errorMessage: '重置 MVU 变量初始值失败',
    }
  )
  const top = isRecord(raw) ? raw : {}
  const data = isRecord(top.data) ? top.data : top
  const mvu_state =
    (isRecord(data.mvu_state) && data.mvu_state) ||
    (isRecord(data.live_mvu) && data.live_mvu) ||
    (isRecord(data.values) && data.values) ||
    {}
  return {
    card_id: asString(data.card_id, cardId),
    mvu_state,
    active_stage: normalizeActiveStage(data.active_stage ?? data.current_stage),
  }
}
