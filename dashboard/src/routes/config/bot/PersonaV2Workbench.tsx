import { type ComponentProps, useCallback, useEffect, useMemo, useState } from 'react'
import {
  BookOpen,
  Check,
  Crown,
  Flame,
  Globe,
  Heart,
  Layers,
  Lock,
  MessageSquareQuote,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldAlert,
  Sparkles,
  Sliders,
  Trash2,
  UserCheck,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { DraftNumberInput } from '@/components/ui/draft-number-input'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Slider } from '@/components/ui/slider'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { useToast } from '@/hooks/use-toast'
import {
  bindPersonaV2Card,
  computeActiveStageFromState,
  createDefaultPersonaV2Card,
  createDefaultRelationshipStages,
  createPersonaV2Card,
  deletePersonaV2Card,
  getPersonaV2Card,
  listPersonaV2Cards,
  resetPersonaV2MvuState,
  savePersonaV2Card,
  updatePersonaV2MvuState,
  type MetaCalibrationEntry,
  type MVUVariableSchema,
  type PastExperienceCategory,
  type PastExperienceEntry,
  type PersonaBindTarget,
  type PersonaV2ActiveStageInfo,
  type PersonaV2Card,
  type PersonaV2CardSummaryItem,
  type RelationshipStageOverlay,
  type SampleCorpusCategory,
  type SampleCorpusEntry,
  type ScopeType,
  type StateMachineTier,
  type VisibilityType,
} from '@/lib/persona-v2-api'
import { cn } from '@/lib/utils'

type ScopeFilterMode = 'all' | 'general' | 'owner_only'

const CORPUS_CATEGORIES: Array<{
  value: SampleCorpusCategory | 'all'
  label: string
  defaultScope: ScopeType
}> = [
  { value: 'all', label: '全部语料', defaultScope: 'general' },
  { value: 'daily_echo', label: '日常回音 (daily_echo)', defaultScope: 'general' },
  { value: 'flirt_scene', label: '调情场景 (flirt_scene)', defaultScope: 'owner_only' },
  {
    value: 'intimacy_gradient_dialogue',
    label: '5级对白阶梯 (intimacy_gradient_dialogue)',
    defaultScope: 'owner_only',
  },
  {
    value: 'intimacy_gradient_posture',
    label: '5级体态阶梯 (intimacy_gradient_posture)',
    defaultScope: 'owner_only',
  },
  {
    value: 'romance_lock_corpus',
    label: '恋爱锁定语料 (romance_lock_corpus)',
    defaultScope: 'owner_only',
  },
]

const INTIMACY_TIER_OPTIONS = [
  '日常态',
  '微醺破防',
  '1. 潜伏 / 突发异样',
  '2. 升温 / 呼吸失序',
  '3. 动摇 / 肢体迎合',
  '4. 失守 / 沉溺索取',
  '5. 事后 / 余温依恋',
  '恋爱锁定常驻态',
]

function ScopeSegmentedToggle({
  value,
  onChange,
  size = 'sm',
}: {
  value: ScopeType
  onChange: (next: ScopeType) => void
  size?: 'sm' | 'xs'
}) {
  const isOwner = value === 'owner_only'
  return (
    <div
      role="group"
      aria-label="作用域切换"
      className={cn(
        'inline-flex shrink-0 items-center rounded-md border p-0.5',
        isOwner
          ? 'border-amber-500/40 bg-amber-500/10'
          : 'border-emerald-500/40 bg-emerald-500/10'
      )}
    >
      <button
        type="button"
        onClick={() => onChange('general')}
        className={cn(
          'inline-flex items-center gap-1 rounded px-2 font-medium transition-colors',
          size === 'xs' ? 'h-6 text-[11px]' : 'h-7 text-xs',
          !isOwner
            ? 'bg-emerald-600 text-white shadow-sm'
            : 'text-muted-foreground hover:text-foreground'
        )}
      >
        <Globe className="h-3 w-3" />
        🌐 通用设定
      </button>
      <button
        type="button"
        onClick={() => onChange('owner_only')}
        className={cn(
          'inline-flex items-center gap-1 rounded px-2 font-medium transition-colors',
          size === 'xs' ? 'h-6 text-[11px]' : 'h-7 text-xs',
          isOwner
            ? 'bg-amber-600 text-white shadow-sm'
            : 'text-muted-foreground hover:text-foreground'
        )}
      >
        <Lock className="h-3 w-3" />
        🔒 Owner专属
      </button>
    </div>
  )
}

function VisibilitySegmentedToggle({
  value,
  onChange,
}: {
  value: VisibilityType
  onChange: (next: VisibilityType) => void
}) {
  const isPrivate = value === 'PRIVATE'
  return (
    <div
      role="group"
      aria-label="变量可见域切换"
      className={cn(
        'inline-flex shrink-0 items-center rounded-md border p-0.5',
        isPrivate
          ? 'border-amber-500/40 bg-amber-500/10'
          : 'border-emerald-500/40 bg-emerald-500/10'
      )}
    >
      <button
        type="button"
        onClick={() => onChange('PUBLIC')}
        className={cn(
          'inline-flex h-6 items-center gap-1 rounded px-2 text-[11px] font-medium transition-colors',
          !isPrivate
            ? 'bg-emerald-600 text-white shadow-sm'
            : 'text-muted-foreground hover:text-foreground'
        )}
      >
        🌐 PUBLIC 通用
      </button>
      <button
        type="button"
        onClick={() => onChange('PRIVATE')}
        className={cn(
          'inline-flex h-6 items-center gap-1 rounded px-2 text-[11px] font-medium transition-colors',
          isPrivate
            ? 'bg-amber-600 text-white shadow-sm'
            : 'text-muted-foreground hover:text-foreground'
        )}
      >
        🔒 PRIVATE Owner专属
      </button>
    </div>
  )
}

function parseCommaTokens(raw: string): string[] {
  return raw
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function parseLineItems(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
}

function makeEntryId(prefix: string): string {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`
}

function useDraftStringList(
  value: string[],
  joiner: string,
  parser: (raw: string) => string[],
  onCommit: (next: string[]) => void
) {
  const canonical = value.join(joiner)
  const [draftValue, setDraftValue] = useState(canonical)
  const [seenCanonical, setSeenCanonical] = useState(canonical)
  const [focused, setFocused] = useState(false)

  if (canonical !== seenCanonical) {
    setSeenCanonical(canonical)
    if (!focused) {
      setDraftValue(canonical)
    }
  }

  return {
    draftValue,
    onFocus: () => setFocused(true),
    onChange: (nextRaw: string) => {
      setDraftValue(nextRaw)
      onCommit(parser(nextRaw))
    },
    onBlur: () => {
      setFocused(false)
      const cleaned = parser(draftValue)
      setDraftValue(cleaned.join(joiner))
      onCommit(cleaned)
    },
  }
}

type DraftTokenInputProps = Omit<ComponentProps<typeof Input>, 'onChange' | 'value'> & {
  value: string[]
  onChange: (next: string[]) => void
}

function DraftTokenInput({
  value,
  onChange,
  onBlur,
  onFocus,
  ...props
}: DraftTokenInputProps) {
  const draft = useDraftStringList(value, ', ', parseCommaTokens, onChange)
  return (
    <Input
      {...props}
      value={draft.draftValue}
      onFocus={(e) => {
        draft.onFocus()
        onFocus?.(e)
      }}
      onChange={(e) => draft.onChange(e.target.value)}
      onBlur={(e) => {
        draft.onBlur()
        onBlur?.(e)
      }}
    />
  )
}

type DraftLinesTextareaProps = Omit<ComponentProps<typeof Textarea>, 'onChange' | 'value'> & {
  value: string[]
  onChange: (next: string[]) => void
}

function DraftLinesTextarea({
  value,
  onChange,
  onBlur,
  onFocus,
  ...props
}: DraftLinesTextareaProps) {
  const draft = useDraftStringList(value, '\n', parseLineItems, onChange)
  return (
    <Textarea
      {...props}
      value={draft.draftValue}
      onFocus={(e) => {
        draft.onFocus()
        onFocus?.(e)
      }}
      onChange={(e) => draft.onChange(e.target.value)}
      onBlur={(e) => {
        draft.onBlur()
        onBlur?.(e)
      }}
    />
  )
}

export interface PersonaV2WorkbenchProps {
  personalityValues?: Record<string, unknown>
  onReloadConfig?: () => Promise<boolean | void> | void
}

export function PersonaV2Workbench({
  personalityValues,
  onReloadConfig,
}: PersonaV2WorkbenchProps) {
  const { toast } = useToast()

  const [cards, setCards] = useState<PersonaV2CardSummaryItem[]>([])
  const [apiBindings, setApiBindings] = useState<{
    active?: string
    sister_younger?: string
    sister_elder?: string
  }>({})
  const [selectedCardId, setSelectedCardId] = useState<string>('')
  const [editingCard, setEditingCard] = useState<PersonaV2Card | null>(null)
  const [liveMvuState, setLiveMvuState] = useState<Record<string, unknown>>({})
  const [activeStageInfo, setActiveStageInfo] = useState<PersonaV2ActiveStageInfo | null>(null)
  const [hasCardUnsavedChanges, setHasCardUnsavedChanges] = useState(false)

  const [loadingList, setLoadingList] = useState(true)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [savingCard, setSavingCard] = useState(false)
  const [bindingSlot, setBindingSlot] = useState<string | null>(null)
  const [savingMvu, setSavingMvu] = useState(false)
  const [resettingMvu, setResettingMvu] = useState(false)

  // 双域过滤与子分类过滤状态
  const [scopeFilter, setScopeFilter] = useState<ScopeFilterMode>('all')
  const [activeEditorTab, setActiveEditorTab] = useState<string>('core_persona')
  const [corpusCategoryFilter, setCorpusCategoryFilter] = useState<SampleCorpusCategory | 'all'>(
    'all'
  )
  const [experienceCategoryFilter, setExperienceCategoryFilter] = useState<
    PastExperienceCategory | 'all'
  >('all')

  // 新建角色卡弹窗状态
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [newCardName, setNewCardName] = useState('')
  const [newCardId, setNewCardId] = useState('')
  const [newCardRoleTag, setNewCardRoleTag] = useState('')
  const [newCardSoulPitch, setNewCardSoulPitch] = useState('')
  const [creatingCard, setCreatingCard] = useState(false)

  // 从 personalityValues 推导槽位绑定状态（与后端返回结果互补）
  const configDerivedBindings = useMemo(() => {
    const p = personalityValues ?? {}
    const profiles = Array.isArray(p.profiles) ? (p.profiles as Array<Record<string, unknown>>) : []
    const selectedProfileName = typeof p.selected_profile === 'string' ? p.selected_profile : ''
    const youngerName = typeof p.younger_slot_profile === 'string' ? p.younger_slot_profile : ''
    const elderName = typeof p.elder_slot_profile === 'string' ? p.elder_slot_profile : ''
    const directCardId = typeof p.persona_v2_card_id === 'string' ? p.persona_v2_card_id : ''

    const findCardIdForProfile = (profName: string): string | undefined => {
      if (!profName) return undefined
      const matchedProf = profiles.find((item) => item.name === profName)
      if (matchedProf && typeof matchedProf.persona_v2_card_id === 'string' && matchedProf.persona_v2_card_id) {
        return matchedProf.persona_v2_card_id
      }
      const matchedCard = cards.find((c) => c.name === profName || c.card_id === profName)
      return matchedCard?.card_id
    }

    return {
      active:
        apiBindings.active ||
        directCardId ||
        findCardIdForProfile(selectedProfileName) ||
        undefined,
      sister_younger:
        apiBindings.sister_younger || findCardIdForProfile(youngerName) || undefined,
      sister_elder: apiBindings.sister_elder || findCardIdForProfile(elderName) || undefined,
    }
  }, [apiBindings, cards, personalityValues])

  const getCardBoundTargets = useCallback(
    (card: PersonaV2CardSummaryItem): PersonaBindTarget[] => {
      const set = new Set<PersonaBindTarget>(card.bound_targets)
      if (configDerivedBindings.active === card.card_id) set.add('active')
      if (configDerivedBindings.sister_younger === card.card_id) set.add('sister_younger')
      if (configDerivedBindings.sister_elder === card.card_id) set.add('sister_elder')
      return Array.from(set)
    },
    [configDerivedBindings]
  )

  const loadCardDetail = useCallback(
    async (cardId: string, fallbackCard?: PersonaV2Card) => {
      if (!cardId) return
      try {
        setLoadingDetail(true)
        const detail = await getPersonaV2Card(cardId)
        setEditingCard(detail.card)
        setLiveMvuState(detail.mvu_state)
        setActiveStageInfo(
          detail.active_stage ?? computeActiveStageFromState(detail.card, detail.mvu_state)
        )
        setHasCardUnsavedChanges(false)
      } catch (error) {
        if (fallbackCard) {
          setEditingCard(fallbackCard)
          const fallbackState: Record<string, unknown> = {}
          for (const v of fallbackCard.mvu_config.variables) {
            fallbackState[v.key] = v.default_value
          }
          setLiveMvuState(fallbackState)
          setActiveStageInfo(computeActiveStageFromState(fallbackCard, fallbackState))
          setHasCardUnsavedChanges(false)
        } else {
          toast({
            variant: 'destructive',
            title: '加载角色卡详情失败',
            description: error instanceof Error ? error.message : '无法读取角色卡详情',
          })
        }
      } finally {
        setLoadingDetail(false)
      }
    },
    [toast]
  )

  const refreshCardList = useCallback(
    async (preferredCardId?: string) => {
      try {
        setLoadingList(true)
        const res = await listPersonaV2Cards()
        setCards(res.cards)
        setApiBindings(res.bindings)

        const nextId =
          preferredCardId ||
          selectedCardId ||
          res.bindings.active ||
          (typeof personalityValues?.persona_v2_card_id === 'string'
            ? personalityValues.persona_v2_card_id
            : '') ||
          res.cards[0]?.card_id ||
          ''

        if (nextId && nextId !== selectedCardId) {
          setSelectedCardId(nextId)
          const summaryItem = res.cards.find((c) => c.card_id === nextId)
          await loadCardDetail(nextId, summaryItem?.card)
        } else if (nextId && !editingCard) {
          const summaryItem = res.cards.find((c) => c.card_id === nextId)
          await loadCardDetail(nextId, summaryItem?.card)
        }
      } catch {
        // 测试环境或后端未启动时静默降级，避免阻断其他设置项使用
      } finally {
        setLoadingList(false)
      }
    },
    [editingCard, loadCardDetail, personalityValues?.persona_v2_card_id, selectedCardId]
  )

  useEffect(() => {
    void refreshCardList()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleSelectCardForEdit = async (cardItem: PersonaV2CardSummaryItem) => {
    if (cardItem.card_id === selectedCardId && editingCard) return
    setSelectedCardId(cardItem.card_id)
    await loadCardDetail(cardItem.card_id, cardItem.card)
  }

  const updateEditingCard = useCallback((updater: (prev: PersonaV2Card) => PersonaV2Card) => {
    setEditingCard((prev) => {
      if (!prev) return prev
      const next = updater(prev)
      return next
    })
    setHasCardUnsavedChanges(true)
  }, [])

  const handleSaveCurrentCard = async () => {
    if (!editingCard) return
    try {
      setSavingCard(true)
      const saved = await savePersonaV2Card(editingCard.card_id, editingCard)
      setEditingCard(saved.card)
      setLiveMvuState(saved.mvu_state)
      setActiveStageInfo(
        saved.active_stage ?? computeActiveStageFromState(saved.card, saved.mvu_state)
      )
      setHasCardUnsavedChanges(false)
      await refreshCardList(saved.card.card_id)
      if (onReloadConfig) {
        await onReloadConfig()
      }
      toast({
        title: '保存成功',
        description: `人物卡片「${saved.card.name}」及双域隔离设定已保存同步`,
      })
    } catch (error) {
      toast({
        variant: 'destructive',
        title: '保存人物卡片失败',
        description: error instanceof Error ? error.message : '保存请求未成功',
      })
    } finally {
      setSavingCard(false)
    }
  }

  const handleBindCard = async (cardItem: PersonaV2CardSummaryItem, target: PersonaBindTarget) => {
    const key = `${cardItem.card_id}:${target}`
    try {
      setBindingSlot(key)
      await bindPersonaV2Card(cardItem.card_id, target)
      setApiBindings((prev) => ({
        ...prev,
        [target]: cardItem.card_id,
      }))
      await refreshCardList(selectedCardId || cardItem.card_id)
      if (onReloadConfig) {
        await onReloadConfig()
      }
      const targetLabel =
        target === 'active'
          ? '当前主卡（主人设）'
          : target === 'sister_younger'
            ? '🌸 妹妹槽位 (Flash)'
            : '🌙 姐姐槽位 (Argon)'
      toast({
        title: '槽位绑定成功',
        description: `已将「${cardItem.name}」绑定至 ${targetLabel}`,
      })
    } catch (error) {
      toast({
        variant: 'destructive',
        title: '绑定失败',
        description: error instanceof Error ? error.message : '无法绑定角色卡到目标槽位',
      })
    } finally {
      setBindingSlot(null)
    }
  }

  const handleCreateCard = async () => {
    const trimmedName = newCardName.trim()
    if (!trimmedName) {
      toast({ variant: 'destructive', title: '请输入人物卡片名称' })
      return
    }
    const slug =
      newCardId
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, '_')
        .replace(/^_+|_+$/g, '') ||
      `persona_${Date.now().toString(36)}`

    const draftCard = createDefaultPersonaV2Card({
      card_id: slug,
      name: trimmedName,
      role_tag: newCardRoleTag.trim() || '自定义角色 · V2六维人设',
    })
    if (newCardSoulPitch.trim()) {
      draftCard.core_persona.soul_pitch = newCardSoulPitch.trim()
    }

    try {
      setCreatingCard(true)
      const created = await createPersonaV2Card(draftCard)
      setCreateDialogOpen(false)
      setNewCardName('')
      setNewCardId('')
      setNewCardRoleTag('')
      setNewCardSoulPitch('')
      setSelectedCardId(created.card.card_id)
      setEditingCard(created.card)
      setLiveMvuState(created.mvu_state)
      setActiveStageInfo(created.active_stage)
      setHasCardUnsavedChanges(false)
      await refreshCardList(created.card.card_id)
      if (onReloadConfig) {
        await onReloadConfig()
      }
      toast({
        title: '人物卡片已创建',
        description: `已创建「${created.card.name}」(${created.card.card_id})`,
      })
    } catch (error) {
      toast({
        variant: 'destructive',
        title: '创建人物卡片失败',
        description: error instanceof Error ? error.message : '创建请求失败',
      })
    } finally {
      setCreatingCard(false)
    }
  }

  const handleDeleteCurrentCard = async () => {
    if (!editingCard) return
    const builtinPrefixes = ['qianling_', 'maibot_']
    if (builtinPrefixes.some((p) => editingCard.card_id.startsWith(p))) {
      toast({
        variant: 'destructive',
        title: '预设核心角色卡不可删除',
        description: '潜令 V1.8 与双子核心角色卡支持直接修改并保存，无需删除。',
      })
      return
    }
    try {
      await deletePersonaV2Card(editingCard.card_id)
      toast({
        title: '已删除角色卡',
        description: `已删除「${editingCard.name}」`,
      })
      setEditingCard(null)
      setSelectedCardId('')
      await refreshCardList()
    } catch (error) {
      toast({
        variant: 'destructive',
        title: '删除失败',
        description: error instanceof Error ? error.message : '无法删除该角色卡',
      })
    }
  }

  const handleSaveLiveMvuState = async () => {
    if (!editingCard) return
    try {
      setSavingMvu(true)
      const res = await updatePersonaV2MvuState(editingCard.card_id, liveMvuState)
      setLiveMvuState(res.mvu_state)
      setActiveStageInfo(
        res.active_stage ?? computeActiveStageFromState(editingCard, res.mvu_state)
      )
      toast({
        title: '实时变量已更新',
        description: `已将「${editingCard.name}」的 MVU 实时状态写入 CompanionTruthDB`,
      })
    } catch (error) {
      toast({
        variant: 'destructive',
        title: '保存实时变量失败',
        description: error instanceof Error ? error.message : '写入 CompanionTruthDB 失败',
      })
    } finally {
      setSavingMvu(false)
    }
  }

  const handleResetLiveMvuState = async () => {
    if (!editingCard) return
    try {
      setResettingMvu(true)
      const res = await resetPersonaV2MvuState(editingCard.card_id)
      const fallbackDefaults: Record<string, unknown> = {}
      for (const v of editingCard.mvu_config.variables) {
        fallbackDefaults[v.key] =
          res.mvu_state[v.key] !== undefined ? res.mvu_state[v.key] : v.default_value
      }
      setLiveMvuState(fallbackDefaults)
      setActiveStageInfo(
        res.active_stage ?? computeActiveStageFromState(editingCard, fallbackDefaults)
      )
      toast({
        title: '已重置为初始值',
        description: `「${editingCard.name}」的所有 MVU 实时变量已恢复默认初始状态`,
      })
    } catch (error) {
      toast({
        variant: 'destructive',
        title: '重置变量失败',
        description: error instanceof Error ? error.message : '重置请求失败',
      })
    } finally {
      setResettingMvu(false)
    }
  }

  // 当本地调整 liveMvuState（如拖动好感度滑块或切换恋爱锁）时，即时预览命中的阶段
  const previewActiveStage = useMemo(() => {
    if (!editingCard) return activeStageInfo
    return computeActiveStageFromState(editingCard, liveMvuState) ?? activeStageInfo
  }, [activeStageInfo, editingCard, liveMvuState])

  // 过滤后的数据集
  const filteredMetaCalibrations = useMemo(() => {
    if (!editingCard) return []
    return editingCard.core_persona.meta_calibration
      .map((entry, index) => ({ entry, index }))
      .filter(({ entry }) => scopeFilter === 'all' || entry.scope === scopeFilter)
  }, [editingCard, scopeFilter])

  const filteredCorpora = useMemo(() => {
    if (!editingCard) return []
    return editingCard.sample_corpora
      .map((entry, index) => ({ entry, index }))
      .filter(({ entry }) => {
        if (scopeFilter !== 'all' && entry.scope !== scopeFilter) return false
        if (corpusCategoryFilter !== 'all' && entry.category !== corpusCategoryFilter) return false
        return true
      })
  }, [corpusCategoryFilter, editingCard, scopeFilter])

  const filteredExperiences = useMemo(() => {
    if (!editingCard) return []
    return editingCard.past_experiences
      .map((entry, index) => ({ entry, index }))
      .filter(({ entry }) => {
        if (scopeFilter !== 'all' && entry.scope !== scopeFilter) return false
        if (experienceCategoryFilter !== 'all' && entry.category !== experienceCategoryFilter) {
          return false
        }
        return true
      })
  }, [editingCard, experienceCategoryFilter, scopeFilter])

  const filteredStages = useMemo(() => {
    if (!editingCard) return []
    return editingCard.relationship_stages
      .map((stage, index) => ({ stage, index }))
      .filter(({ stage }) => scopeFilter === 'all' || stage.scope === scopeFilter)
  }, [editingCard, scopeFilter])

  const filteredMvuVariables = useMemo(() => {
    if (!editingCard) return []
    return editingCard.mvu_config.variables
      .map((variable, index) => ({ variable, index }))
      .filter(({ variable }) => {
        if (scopeFilter === 'general') return variable.visibility === 'PUBLIC'
        if (scopeFilter === 'owner_only') return variable.visibility === 'PRIVATE'
        return true
      })
  }, [editingCard, scopeFilter])

  const showGeneral = scopeFilter !== 'owner_only'
  const showOwnerOnly = scopeFilter !== 'general'

  const liveLoveLocked = Boolean(
    liveMvuState[editingCard?.mvu_config.affinity_guard.lock_var || 'love_locked'] ??
      liveMvuState.love_locked ??
      liveMvuState.twin_harmony_locked
  )

  return (
    <div
      data-testid="persona-v2-workbench"
      className="space-y-4 rounded-xl border border-primary/20 bg-gradient-to-b from-primary/[0.03] to-transparent p-3.5 sm:p-4"
    >
      {/* ─── 顶部标题栏与操作区 ─── */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 pb-3">
        <div className="space-y-0.5">
          <div className="flex flex-wrap items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary" />
            <h3 className="text-base font-semibold tracking-tight">
              Persona V2 角色卡与 MVU 动态演进工作台
            </h3>
            <Badge variant="secondary" className="text-[11px]">
              潜令 V1.8 六维架构 + 双域物理隔离
            </Badge>
          </div>
          <p className="text-xs text-muted-foreground">
            支持多角色卡一键选装、主卡/姐妹双槽位绑定、通用与 Owner 私密设定物理隔离编辑，以及 CompanionTruthDB 实时变量调试。
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void refreshCardList(selectedCardId)}
            disabled={loadingList}
          >
            <RefreshCw className={cn('mr-1.5 h-3.5 w-3.5', loadingList && 'animate-spin')} />
            刷新卡片架
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setCreateDialogOpen(true)}
          >
            <Plus className="mr-1.5 h-3.5 w-3.5" />
            新建人物卡片
          </Button>
          {editingCard && (
            <Button
              type="button"
              size="sm"
              onClick={() => void handleSaveCurrentCard()}
              disabled={savingCard}
            >
              <Save className="mr-1.5 h-3.5 w-3.5" />
              {savingCard ? '保存中...' : '保存当前人物卡片修改'}
              {hasCardUnsavedChanges && (
                <span className="ml-1.5 inline-block h-2 w-2 rounded-full bg-amber-400" />
              )}
            </Button>
          )}
        </div>
      </div>

      {/* ─── 1. 可选人物卡片架 (Selectable Character Card Gallery) ─── */}
      <div className="space-y-2.5">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            可选人物卡片架 ({cards.length})
          </span>
          <span className="text-[11px] text-muted-foreground">
            点击「编辑此卡片」可在下方工作台深度调校；点击槽位按钮可一键切换当前生效人设或双子值班槽位
          </span>
        </div>

        {cards.length === 0 && !loadingList ? (
          <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
            暂未读取到角色卡数据，请确认后端服务已启动或点击右上角「+ 新建人物卡片」。
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {cards.map((cardItem) => {
              const isSelected = cardItem.card_id === selectedCardId
              const boundTargets = getCardBoundTargets(cardItem)
              const isActiveMain = boundTargets.includes('active')
              const isYoungerSlot = boundTargets.includes('sister_younger')
              const isElderSlot = boundTargets.includes('sister_elder')

              return (
                <div
                  key={cardItem.card_id}
                  className={cn(
                    'group relative flex flex-col justify-between rounded-lg border p-3 transition-all',
                    isSelected
                      ? 'border-primary bg-primary/[0.06] shadow-sm ring-1 ring-primary/30'
                      : 'border-border/70 bg-background/80 hover:border-primary/40 hover:bg-background'
                  )}
                >
                  <div className="space-y-2">
                    {/* 标题与绑定徽章 */}
                    <div className="flex flex-wrap items-start justify-between gap-1.5">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="truncate text-sm font-bold text-foreground">
                            {cardItem.name}
                          </span>
                          {cardItem.role_tag && (
                            <Badge variant="outline" className="max-w-full truncate text-[10px]">
                              {cardItem.role_tag}
                            </Badge>
                          )}
                        </div>
                        <div className="font-mono text-[10px] text-muted-foreground">
                          ID: {cardItem.card_id}
                        </div>
                      </div>

                      <div className="flex flex-wrap items-center gap-1">
                        {isActiveMain && (
                          <Badge className="bg-emerald-600 text-[10px] text-white hover:bg-emerald-600">
                            <Crown className="mr-0.5 h-2.5 w-2.5" />
                            当前主卡
                          </Badge>
                        )}
                        {isYoungerSlot && (
                          <Badge
                            variant="secondary"
                            className="border-pink-500/30 bg-pink-500/15 text-[10px] text-pink-700 dark:text-pink-300"
                          >
                            🌸 妹妹槽位 (Flash)
                          </Badge>
                        )}
                        {isElderSlot && (
                          <Badge
                            variant="secondary"
                            className="border-indigo-500/30 bg-indigo-500/15 text-[10px] text-indigo-700 dark:text-indigo-300"
                          >
                            🌙 姐姐槽位 (Argon)
                          </Badge>
                        )}
                      </div>
                    </div>

                    {/* 一句话灵魂基调预览 */}
                    <p className="line-clamp-2 min-h-[2.25rem] text-xs leading-relaxed text-muted-foreground">
                      {cardItem.soul_pitch || '暂无一句话灵魂基调说明'}
                    </p>

                    {/* 统计计数徽章 */}
                    <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
                      <span className="inline-flex items-center gap-1 rounded bg-muted/60 px-1.5 py-0.5">
                        <BookOpen className="h-3 w-3" />
                        经历 {cardItem.experience_count}
                      </span>
                      <span className="inline-flex items-center gap-1 rounded bg-muted/60 px-1.5 py-0.5">
                        <MessageSquareQuote className="h-3 w-3" />
                        语料 {cardItem.corpus_count}
                      </span>
                      <span className="inline-flex items-center gap-1 rounded bg-muted/60 px-1.5 py-0.5">
                        <Layers className="h-3 w-3" />
                        阶段 {cardItem.stage_count}
                      </span>
                    </div>
                  </div>

                  {/* 一键动作按钮栏 */}
                  <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-border/50 pt-2.5">
                    <Button
                      type="button"
                      size="sm"
                      variant={isSelected ? 'default' : 'secondary'}
                      className="h-7 px-2.5 text-xs"
                      onClick={() => void handleSelectCardForEdit(cardItem)}
                    >
                      {isSelected ? (
                        <>
                          <Check className="mr-1 h-3 w-3" />
                          正在编辑
                        </>
                      ) : (
                        '编辑此卡片'
                      )}
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant={isActiveMain ? 'default' : 'outline'}
                      className="h-7 px-2 text-xs"
                      disabled={bindingSlot === `${cardItem.card_id}:active`}
                      onClick={() => void handleBindCard(cardItem, 'active')}
                    >
                      <UserCheck className="mr-1 h-3 w-3" />
                      设为主人设
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant={isYoungerSlot ? 'default' : 'outline'}
                      className="h-7 px-2 text-xs"
                      disabled={bindingSlot === `${cardItem.card_id}:sister_younger`}
                      onClick={() => void handleBindCard(cardItem, 'sister_younger')}
                    >
                      绑定至妹妹槽位
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant={isElderSlot ? 'default' : 'outline'}
                      className="h-7 px-2 text-xs"
                      disabled={bindingSlot === `${cardItem.card_id}:sister_elder`}
                      onClick={() => void handleBindCard(cardItem, 'sister_elder')}
                    >
                      绑定至姐姐槽位
                    </Button>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* ─── 2. 雙域视图过滤控制栏 (First-Class Dual-Scope Control) ─── */}
      {editingCard && (
        <div className="space-y-3 pt-2">
          <div className="rounded-lg border border-border/80 bg-card p-3 shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-semibold text-muted-foreground">当前编辑角色：</span>
                <Badge variant="default" className="text-xs font-semibold">
                  {editingCard.name}
                </Badge>
                <span className="font-mono text-xs text-muted-foreground">
                  ({editingCard.card_id})
                </span>
                {loadingDetail && (
                  <span className="text-xs text-muted-foreground">同步数据中...</span>
                )}
              </div>

              {/* Scope View Filter Bar */}
              <div
                role="group"
                aria-label="双域设定视图过滤器"
                className="flex flex-wrap items-center gap-1 rounded-lg border bg-muted/40 p-1"
              >
                <button
                  type="button"
                  onClick={() => setScopeFilter('all')}
                  className={cn(
                    'rounded-md px-2.5 py-1 text-xs font-semibold transition-all',
                    scopeFilter === 'all'
                      ? 'bg-background text-foreground shadow-sm'
                      : 'text-muted-foreground hover:text-foreground'
                  )}
                >
                  全部维度 (All)
                </button>
                <button
                  type="button"
                  onClick={() => setScopeFilter('general')}
                  className={cn(
                    'inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-semibold transition-all',
                    scopeFilter === 'general'
                      ? 'bg-emerald-600 text-white shadow-sm'
                      : 'text-muted-foreground hover:text-foreground'
                  )}
                >
                  <Globe className="h-3 w-3" />
                  🌐 仅看通用设定 (群聊/公开生效)
                </button>
                <button
                  type="button"
                  onClick={() => setScopeFilter('owner_only')}
                  className={cn(
                    'inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-semibold transition-all',
                    scopeFilter === 'owner_only'
                      ? 'bg-amber-600 text-white shadow-sm'
                      : 'text-muted-foreground hover:text-foreground'
                  )}
                >
                  <Lock className="h-3 w-3" />
                  🔒 仅看 Owner 专属设定 (仅 Owner 私聊物理隔离生效)
                </button>
              </div>
            </div>

            {/* 物理隔离防线说明横幅 */}
            <div className="mt-2.5 flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-900 dark:text-amber-200">
              <ShieldAlert className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
              <span>
                🌐 通用设定在群聊与所有私聊中均生效；🔒 Owner专属设定受四道物理防线保护，仅在 Owner 私聊中加载与触发，群聊中绝对零泄露。
              </span>
            </div>

            {/* 角色卡顶层元信息快捷编辑行 */}
            <div className="mt-3 grid gap-3 border-t border-border/50 pt-3 sm:grid-cols-3">
              <div className="space-y-1">
                <Label htmlFor="v2-card-name" className="text-xs">
                  角色名称 (name)
                </Label>
                <Input
                  id="v2-card-name"
                  value={editingCard.name}
                  onChange={(e) =>
                    updateEditingCard((prev) => ({ ...prev, name: e.target.value }))
                  }
                  className="h-8 text-xs"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="v2-card-role-tag" className="text-xs">
                  身份定位标签 (role_tag)
                </Label>
                <Input
                  id="v2-card-role-tag"
                  value={editingCard.role_tag}
                  onChange={(e) =>
                    updateEditingCard((prev) => ({ ...prev, role_tag: e.target.value }))
                  }
                  placeholder="如：42岁 · 云家家主 / 养母"
                  className="h-8 text-xs"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="v2-card-aliases" className="text-xs">
                  触发别名 (aliases，逗号分隔，用于 240 字邻近度召回)
                </Label>
                <DraftTokenInput
                  id="v2-card-aliases"
                  value={editingCard.aliases}
                  onChange={(nextAliases) =>
                    updateEditingCard((prev) => ({
                      ...prev,
                      aliases: nextAliases,
                    }))
                  }
                  placeholder="如：岁宁, 宁姨, 母亲"
                  className="h-8 text-xs"
                />
              </div>
            </div>
          </div>

          {/* ─── 3. 角色卡四维深度编辑页签 (4 Comprehensive Editable Tabs) ─── */}
          <Tabs
            value={activeEditorTab}
            onValueChange={setActiveEditorTab}
            className="space-y-3"
          >
            <TabsList className="grid h-auto w-full grid-cols-2 gap-1 p-1 lg:grid-cols-4">
              <TabsTrigger value="core_persona" className="gap-1.5 py-2 text-xs font-semibold">
                <Sparkles className="h-3.5 w-3.5" />
                1. 核心画像与性格调色盘
              </TabsTrigger>
              <TabsTrigger value="speech_corpora" className="gap-1.5 py-2 text-xs font-semibold">
                <MessageSquareQuote className="h-3.5 w-3.5" />
                2. 语言规范与示例语料库 ({editingCard.sample_corpora.length})
              </TabsTrigger>
              <TabsTrigger value="past_experiences" className="gap-1.5 py-2 text-xs font-semibold">
                <BookOpen className="h-3.5 w-3.5" />
                3. 过往经历与生活切片 ({editingCard.past_experiences.length})
              </TabsTrigger>
              <TabsTrigger value="mvu_stages" className="gap-1.5 py-2 text-xs font-semibold">
                <Sliders className="h-3.5 w-3.5" />
                4. MVU 动态变量与分阶段演进
              </TabsTrigger>
            </TabsList>

            {/* ==================== TAB 1: 核心画像与性格调色盘 ==================== */}
            <TabsContent value="core_persona" className="space-y-4">
              {/* 🌐 通用基础画像区 */}
              {showGeneral && (
                <Card className="border-emerald-500/30 bg-emerald-500/[0.02]">
                  <CardHeader className="border-b border-emerald-500/20 pb-2.5">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <CardTitle className="flex items-center gap-2 text-sm font-semibold text-emerald-800 dark:text-emerald-300">
                        <Globe className="h-4 w-4" />
                        🌐 通用基础画像区 (core_persona · 群聊与私聊常驻生效)
                      </CardTitle>
                      <Badge
                        variant="outline"
                        className="border-emerald-500/40 text-[10px] text-emerald-700 dark:text-emerald-300"
                      >
                        KV-Cache 静态前缀注入
                      </Badge>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-3 pt-3">
                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="space-y-1.5">
                        <Label htmlFor="v2-soul-pitch" className="text-xs font-medium">
                          一句话灵魂基调 (soul_pitch)
                        </Label>
                        <Textarea
                          id="v2-soul-pitch"
                          rows={2}
                          value={editingCard.core_persona.soul_pitch}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                soul_pitch: e.target.value,
                              },
                            }))
                          }
                          placeholder="概括角色的核心身份反差与灵魂张力..."
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label htmlFor="v2-planner-summary" className="text-xs font-medium">
                          Planner 极简摘要 (planner_summary)
                        </Label>
                        <Textarea
                          id="v2-planner-summary"
                          rows={2}
                          value={editingCard.planner_summary}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              planner_summary: e.target.value,
                            }))
                          }
                          placeholder="供 Planner 决策行动时快速读取的轻量级行为准则..."
                        />
                      </div>
                    </div>

                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="space-y-1.5">
                        <Label htmlFor="v2-basic-info" className="text-xs font-medium">
                          基本信息 (basic_info)
                        </Label>
                        <Textarea
                          id="v2-basic-info"
                          rows={4}
                          value={editingCard.core_persona.basic_info}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                basic_info: e.target.value,
                              },
                            }))
                          }
                          placeholder="年龄、生日、身高三围、职业职务、家庭排行与社会身份..."
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label htmlFor="v2-public-visual" className="text-xs font-medium">
                          公开外貌与日常/工作穿搭锚点 (public_visual_and_wardrobe)
                        </Label>
                        <Textarea
                          id="v2-public-visual"
                          rows={4}
                          value={editingCard.core_persona.public_visual_and_wardrobe}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                public_visual_and_wardrobe: e.target.value,
                              },
                            }))
                          }
                          placeholder="外形辨识锚点、体态习惯、工作装与居家/外出日常穿搭..."
                        />
                      </div>
                    </div>

                    <div className="space-y-1.5">
                      <Label htmlFor="v2-public-interaction" className="text-xs font-medium">
                        通用社交与群聊表现守则 (public_interaction_baseline)
                      </Label>
                      <Textarea
                        id="v2-public-interaction"
                        rows={3}
                        value={editingCard.core_persona.public_interaction_baseline}
                        onChange={(e) =>
                          updateEditingCard((prev) => ({
                            ...prev,
                            core_persona: {
                              ...prev.core_persona,
                              public_interaction_baseline: e.target.value,
                            },
                          }))
                        }
                        placeholder="在群聊、家庭餐桌或外人面前的社交风度与互动边界..."
                      />
                    </div>

                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="space-y-1.5">
                        <Label htmlFor="v2-daily-baseline" className="text-xs font-medium">
                          性格调色盘·日常底色 (daily_baseline)
                        </Label>
                        <Textarea
                          id="v2-daily-baseline"
                          rows={3}
                          value={editingCard.core_persona.personality_palette.daily_baseline}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                personality_palette: {
                                  ...prev.core_persona.personality_palette,
                                  daily_baseline: e.target.value,
                                },
                              },
                            }))
                          }
                          placeholder="平时的情绪温度、处事风格与外显性格特质..."
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label htmlFor="v2-coexistence-rule" className="text-xs font-medium">
                          性格调色盘·同屏原则 (coexistence_rule)
                        </Label>
                        <Textarea
                          id="v2-coexistence-rule"
                          rows={3}
                          value={editingCard.core_persona.personality_palette.coexistence_rule}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                personality_palette: {
                                  ...prev.core_persona.personality_palette,
                                  coexistence_rule: e.target.value,
                                },
                              },
                            }))
                          }
                          placeholder="多人同场或群聊时如何保持角色独立性、不抢戏不脸谱化..."
                        />
                      </div>
                    </div>
                  </CardContent>
                </Card>
              )}

              {/* 🔒 Owner 专属深层画像区 */}
              {showOwnerOnly && (
                <Card className="border-amber-500/40 bg-amber-500/[0.03]">
                  <CardHeader className="border-b border-amber-500/20 pb-2.5">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <CardTitle className="flex items-center gap-2 text-sm font-semibold text-amber-800 dark:text-amber-300">
                        <Lock className="h-4 w-4" />
                        🔒 Owner 专属深层画像区 (仅在 is_owner_private == True 时挂载，群聊物理阻断)
                      </CardTitle>
                      <Badge
                        variant="outline"
                        className="border-amber-500/50 bg-amber-500/10 text-[10px] text-amber-800 dark:text-amber-200"
                      >
                        Owner 私聊物理隔离
                      </Badge>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-4 pt-3">
                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="space-y-1.5">
                        <Label htmlFor="v2-owner-bond" className="text-xs font-medium">
                          与 Owner 专属关系前史与羁绊 (owner_relationship_bond)
                        </Label>
                        <Textarea
                          id="v2-owner-bond"
                          rows={4}
                          value={editingCard.core_persona.owner_relationship_bond}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                owner_relationship_bond: e.target.value,
                              },
                            }))
                          }
                          placeholder="与 Owner 的共同记忆、关系张力、主观好感认知原则..."
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label htmlFor="v2-owner-wardrobe" className="text-xs font-medium">
                          私密独处穿搭与身体反应锚点 (owner_private_wardrobe_and_anchors)
                        </Label>
                        <Textarea
                          id="v2-owner-wardrobe"
                          rows={4}
                          value={editingCard.core_persona.owner_private_wardrobe_and_anchors}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                owner_private_wardrobe_and_anchors: e.target.value,
                              },
                            }))
                          }
                          placeholder="深夜独处/私密衣着用法、饰品细节与身体敏感反应锚点..."
                        />
                      </div>
                    </div>

                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="space-y-1.5">
                        <Label htmlFor="v2-affection-leak" className="text-xs font-medium">
                          调色盘·温柔泄漏/调戏方式 (affection_leak_style)
                        </Label>
                        <Textarea
                          id="v2-affection-leak"
                          rows={3}
                          value={editingCard.core_persona.personality_palette.affection_leak_style}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                personality_palette: {
                                  ...prev.core_persona.personality_palette,
                                  affection_leak_style: e.target.value,
                                },
                              },
                            }))
                          }
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label htmlFor="v2-desire-drive" className="text-xs font-medium">
                          调色盘·欲望底色与主动性 (desire_and_intimacy_drive)
                        </Label>
                        <Textarea
                          id="v2-desire-drive"
                          rows={3}
                          value={
                            editingCard.core_persona.personality_palette.desire_and_intimacy_drive
                          }
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                personality_palette: {
                                  ...prev.core_persona.personality_palette,
                                  desire_and_intimacy_drive: e.target.value,
                                },
                              },
                            }))
                          }
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label htmlFor="v2-breakdown-seq" className="text-xs font-medium">
                          调色盘·失控递进特征 (breakdown_sequence)
                        </Label>
                        <Textarea
                          id="v2-breakdown-seq"
                          rows={3}
                          value={editingCard.core_persona.personality_palette.breakdown_sequence}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                personality_palette: {
                                  ...prev.core_persona.personality_palette,
                                  breakdown_sequence: e.target.value,
                                },
                              },
                            }))
                          }
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label htmlFor="v2-love-risk" className="text-xs font-medium">
                          调色盘·爱情风险点 (love_risk)
                        </Label>
                        <Textarea
                          id="v2-love-risk"
                          rows={3}
                          value={editingCard.core_persona.personality_palette.love_risk}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                personality_palette: {
                                  ...prev.core_persona.personality_palette,
                                  love_risk: e.target.value,
                                },
                              },
                            }))
                          }
                        />
                      </div>
                    </div>

                    {/* 面对 Owner 的三档表演状态机 */}
                    <div className="space-y-2.5 rounded-lg border border-amber-500/30 bg-background/80 p-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div>
                          <h4 className="text-xs font-semibold text-amber-900 dark:text-amber-200">
                            面对 Owner 的三档表演状态机 (owner_state_machine_tiers)
                          </h4>
                          <p className="text-[11px] text-muted-foreground">
                            定义角色在面对 Owner 时从日常矜持、破防动摇到彻底失守的三档状态递进表现。
                          </p>
                        </div>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          className="h-7 text-xs"
                          onClick={() =>
                            updateEditingCard((prev) => {
                              const nextTierNum =
                                prev.core_persona.owner_state_machine_tiers.length + 1
                              const nextTier: StateMachineTier = {
                                tier: nextTierNum,
                                name: `第 ${nextTierNum} 档状态`,
                                description: '',
                              }
                              return {
                                ...prev,
                                core_persona: {
                                  ...prev.core_persona,
                                  owner_state_machine_tiers: [
                                    ...prev.core_persona.owner_state_machine_tiers,
                                    nextTier,
                                  ],
                                },
                              }
                            })
                          }
                        >
                          <Plus className="mr-1 h-3 w-3" />
                          添加状态档位
                        </Button>
                      </div>

                      <div className="grid gap-2.5 md:grid-cols-3">
                        {editingCard.core_persona.owner_state_machine_tiers.map((tierItem, idx) => (
                          <div
                            key={idx}
                            className="space-y-2 rounded-md border border-border/70 bg-muted/20 p-2.5"
                          >
                            <div className="flex items-center justify-between gap-1.5">
                              <Badge variant="secondary" className="text-[10px]">
                                Tier {tierItem.tier}
                              </Badge>
                              <Input
                                value={tierItem.name}
                                onChange={(e) =>
                                  updateEditingCard((prev) => {
                                    const tiers = [
                                      ...prev.core_persona.owner_state_machine_tiers,
                                    ]
                                    tiers[idx] = { ...tiers[idx], name: e.target.value }
                                    return {
                                      ...prev,
                                      core_persona: {
                                        ...prev.core_persona,
                                        owner_state_machine_tiers: tiers,
                                      },
                                    }
                                  })
                                }
                                placeholder="档位名称"
                                className="h-7 flex-1 text-xs font-medium"
                              />
                              {editingCard.core_persona.owner_state_machine_tiers.length > 1 && (
                                <Button
                                  type="button"
                                  size="icon"
                                  variant="ghost"
                                  className="h-6 w-6 text-muted-foreground hover:text-destructive"
                                  onClick={() =>
                                    updateEditingCard((prev) => ({
                                      ...prev,
                                      core_persona: {
                                        ...prev.core_persona,
                                        owner_state_machine_tiers:
                                          prev.core_persona.owner_state_machine_tiers.filter(
                                            (_, i) => i !== idx
                                          ),
                                      },
                                    }))
                                  }
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                </Button>
                              )}
                            </div>
                            <Textarea
                              rows={4}
                              value={tierItem.description}
                              onChange={(e) =>
                                updateEditingCard((prev) => {
                                  const tiers = [...prev.core_persona.owner_state_machine_tiers]
                                  tiers[idx] = { ...tiers[idx], description: e.target.value }
                                  return {
                                    ...prev,
                                    core_persona: {
                                      ...prev.core_persona,
                                      owner_state_machine_tiers: tiers,
                                    },
                                  }
                                })
                              }
                              placeholder="该档位下的神态、动作、心理防线与语气细节..."
                              className="text-xs"
                            />
                          </div>
                        ))}
                      </div>
                    </div>
                  </CardContent>
                </Card>
              )}

              {/* 二次解释（防脸谱化校准 meta_calibration） */}
              <Card>
                <CardHeader className="border-b border-border/50 pb-2.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <CardTitle className="text-sm font-semibold">
                        二次解释 · 防脸谱化深度校准 (meta_calibration · 显示 {filteredMetaCalibrations.length} / 共 {editingCard.core_persona.meta_calibration.length} 条)
                      </CardTitle>
                      <p className="text-xs text-muted-foreground">
                        纠正大模型对角色标签的刻板印象（每条可独立指定「🌐 通用设定」或「🔒 Owner专属」作用域）。
                      </p>
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        updateEditingCard((prev) => {
                          const newEntry: MetaCalibrationEntry = {
                            id: makeEntryId('meta'),
                            title: '新增防脸谱化校准项',
                            content: '',
                            scope: scopeFilter === 'owner_only' ? 'owner_only' : 'general',
                          }
                          return {
                            ...prev,
                            core_persona: {
                              ...prev.core_persona,
                              meta_calibration: [
                                ...prev.core_persona.meta_calibration,
                                newEntry,
                              ],
                            },
                          }
                        })
                      }
                    >
                      <Plus className="mr-1 h-3.5 w-3.5" />
                      新增二次解释条目
                    </Button>
                  </div>
                </CardHeader>
                <CardContent className="space-y-3 pt-3">
                  {filteredMetaCalibrations.length === 0 ? (
                    <p className="py-4 text-center text-xs text-muted-foreground">
                      当前过滤视图下暂无二次解释条目，点击右上角「新增二次解释条目」添加。
                    </p>
                  ) : (
                    filteredMetaCalibrations.map(({ entry, index }) => (
                      <div
                        key={entry.id || index}
                        className={cn(
                          'space-y-2 rounded-lg border p-3',
                          entry.scope === 'owner_only'
                            ? 'border-amber-500/35 bg-amber-500/[0.03]'
                            : 'border-emerald-500/30 bg-emerald-500/[0.02]'
                        )}
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <Input
                            value={entry.title}
                            onChange={(e) =>
                              updateEditingCard((prev) => {
                                const list = [...prev.core_persona.meta_calibration]
                                list[index] = { ...list[index], title: e.target.value }
                                return {
                                  ...prev,
                                  core_persona: {
                                    ...prev.core_persona,
                                    meta_calibration: list,
                                  },
                                }
                              })
                            }
                            placeholder="校准标题（如：所谓冷淡不是没感情，而是习惯先兜底）"
                            className="h-8 min-w-[14rem] flex-1 text-xs font-semibold"
                          />
                          <div className="flex items-center gap-2">
                            <ScopeSegmentedToggle
                              size="xs"
                              value={entry.scope}
                              onChange={(nextScope) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.core_persona.meta_calibration]
                                  list[index] = { ...list[index], scope: nextScope }
                                  return {
                                    ...prev,
                                    core_persona: {
                                      ...prev.core_persona,
                                      meta_calibration: list,
                                    },
                                  }
                                })
                              }
                            />
                            <Button
                              type="button"
                              size="icon"
                              variant="ghost"
                              className="h-7 w-7 text-muted-foreground hover:text-destructive"
                              onClick={() =>
                                updateEditingCard((prev) => ({
                                  ...prev,
                                  core_persona: {
                                    ...prev.core_persona,
                                    meta_calibration:
                                      prev.core_persona.meta_calibration.filter(
                                        (_, i) => i !== index
                                      ),
                                  },
                                }))
                              }
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          </div>
                        </div>
                        <Textarea
                          rows={3}
                          value={entry.content}
                          onChange={(e) =>
                            updateEditingCard((prev) => {
                              const list = [...prev.core_persona.meta_calibration]
                              list[index] = { ...list[index], content: e.target.value }
                              return {
                                ...prev,
                                core_persona: {
                                  ...prev.core_persona,
                                  meta_calibration: list,
                                },
                              }
                            })
                          }
                          placeholder="输入对角色的深层理解与反刻板印象指导..."
                          className="text-xs"
                        />
                      </div>
                    ))
                  )}
                </CardContent>
              </Card>
            </TabsContent>

            {/* ==================== TAB 2: 语言规范与示例语料库 ==================== */}
            <TabsContent value="speech_corpora" className="space-y-4">
              {/* 语言与情绪规范 */}
              <div className="grid gap-4 lg:grid-cols-2">
                {showGeneral && (
                  <Card className="border-emerald-500/30 bg-emerald-500/[0.02]">
                    <CardHeader className="border-b border-emerald-500/20 pb-2.5">
                      <CardTitle className="flex items-center gap-2 text-sm font-semibold text-emerald-800 dark:text-emerald-300">
                        <Globe className="h-4 w-4" />
                        🌐 通用语言规则 (speech_rules · 公开与日常生效)
                      </CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-3 pt-3">
                      <div className="space-y-1.5">
                        <Label className="text-xs font-medium">
                          公开称呼规范 (public_address_matrix)
                        </Label>
                        <Textarea
                          rows={2}
                          value={editingCard.core_persona.speech_rules.public_address_matrix}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                speech_rules: {
                                  ...prev.core_persona.speech_rules,
                                  public_address_matrix: e.target.value,
                                },
                              },
                            }))
                          }
                          placeholder="对家人、群友、同事或外人在公开场合的称呼方式..."
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label className="text-xs font-medium">
                          专属语气词规则 (modal_particles)
                        </Label>
                        <Textarea
                          rows={2}
                          value={editingCard.core_persona.speech_rules.modal_particles}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                speech_rules: {
                                  ...prev.core_persona.speech_rules,
                                  modal_particles: e.target.value,
                                },
                              },
                            }))
                          }
                          placeholder="常用尾音、停顿语气词与克制原则..."
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label className="text-xs font-medium">
                          长短句与口语节奏 (sentence_rhythm)
                        </Label>
                        <Textarea
                          rows={2}
                          value={editingCard.core_persona.speech_rules.sentence_rhythm}
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                speech_rules: {
                                  ...prev.core_persona.speech_rules,
                                  sentence_rhythm: e.target.value,
                                },
                              },
                            }))
                          }
                          placeholder="平时说话的句式长短、微信断句节奏与中式口语习惯..."
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label className="text-xs font-medium">
                          禁用词与禁演腔调列表 (negative_constraints，每行一条)
                        </Label>
                        <DraftLinesTextarea
                          rows={3}
                          value={editingCard.core_persona.speech_rules.negative_constraints}
                          onChange={(nextLines) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                speech_rules: {
                                  ...prev.core_persona.speech_rules,
                                  negative_constraints: nextLines,
                                },
                              },
                            }))
                          }
                          placeholder="严禁使用的词汇、译制片腔调或过度客服化表达..."
                        />
                      </div>
                    </CardContent>
                  </Card>
                )}

                {showOwnerOnly && (
                  <Card className="border-amber-500/40 bg-amber-500/[0.03]">
                    <CardHeader className="border-b border-amber-500/20 pb-2.5">
                      <CardTitle className="flex items-center gap-2 text-sm font-semibold text-amber-800 dark:text-amber-300">
                        <Lock className="h-4 w-4" />
                        🔒 Owner专属语言规则 (仅 Owner 私聊生效)
                      </CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-3 pt-3">
                      <div className="space-y-1.5">
                        <Label className="text-xs font-medium">
                          Owner私密称呼矩阵 (owner_private_address_matrix)
                        </Label>
                        <Textarea
                          rows={4}
                          value={
                            editingCard.core_persona.speech_rules.owner_private_address_matrix
                          }
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                speech_rules: {
                                  ...prev.core_persona.speech_rules,
                                  owner_private_address_matrix: e.target.value,
                                },
                              },
                            }))
                          }
                          placeholder="独处时、调情时、情绪失守时或恋爱锁定后对 Owner 的专属私密称呼..."
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label className="text-xs font-medium">
                          情绪失守时的语调变化 (owner_emotional_tone_shift)
                        </Label>
                        <Textarea
                          rows={5}
                          value={
                            editingCard.core_persona.speech_rules.owner_emotional_tone_shift
                          }
                          onChange={(e) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              core_persona: {
                                ...prev.core_persona,
                                speech_rules: {
                                  ...prev.core_persona.speech_rules,
                                  owner_emotional_tone_shift: e.target.value,
                                },
                              },
                            }))
                          }
                          placeholder="面对 Owner 逼近、吃醋、害羞或深情失控时的句子碎裂感、呼吸停顿与声线变化..."
                        />
                      </div>
                    </CardContent>
                  </Card>
                )}
              </div>

              {/* 分类分级示例语料库 (sample_corpora) */}
              <Card>
                <CardHeader className="border-b border-border/50 pb-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <CardTitle className="text-sm font-semibold">
                        分类分级示例语料库 (sample_corpora · 当前筛选 {filteredCorpora.length} / 共 {editingCard.sample_corpora.length} 组)
                      </CardTitle>
                      <p className="text-xs text-muted-foreground">
                        支持 240 字邻近度/关键词触发与 MVU 条件解锁；注入时自动包裹防复读总则（同轮最多化用 1~3 句）。
                      </p>
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        updateEditingCard((prev) => {
                          const cat: SampleCorpusCategory =
                            corpusCategoryFilter === 'all' ? 'daily_echo' : corpusCategoryFilter
                          const defaultScope: ScopeType =
                            scopeFilter === 'owner_only'
                              ? 'owner_only'
                              : scopeFilter === 'general'
                                ? 'general'
                                : cat === 'daily_echo'
                                  ? 'general'
                                  : 'owner_only'
                          const newCorpus: SampleCorpusEntry = {
                            id: makeEntryId('corpus'),
                            category: cat,
                            situation_title: '新建情境语料',
                            tier_label: '',
                            action_and_posture: '',
                            sample_lines: ['示例台词一'],
                            trigger_keywords: [],
                            mvu_condition: cat === 'daily_echo' ? '' : 'affection >= 40',
                            scope: defaultScope,
                          }
                          return {
                            ...prev,
                            sample_corpora: [newCorpus, ...prev.sample_corpora],
                          }
                        })
                      }
                    >
                      <Plus className="mr-1 h-3.5 w-3.5" />
                      新增示例语料组
                    </Button>
                  </div>

                  {/* Category filter pills */}
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {CORPUS_CATEGORIES.map((cat) => (
                      <button
                        key={cat.value}
                        type="button"
                        onClick={() => setCorpusCategoryFilter(cat.value)}
                        className={cn(
                          'rounded-full border px-2.5 py-1 text-xs font-medium transition-colors',
                          corpusCategoryFilter === cat.value
                            ? 'border-primary bg-primary text-primary-foreground'
                            : 'border-border/70 bg-muted/30 text-muted-foreground hover:text-foreground'
                        )}
                      >
                        {cat.label}
                      </button>
                    ))}
                  </div>
                </CardHeader>

                <CardContent className="space-y-3 pt-3">
                  {filteredCorpora.length === 0 ? (
                    <p className="py-6 text-center text-xs text-muted-foreground">
                      当前分类与作用域筛选下暂无语料条目。
                    </p>
                  ) : (
                    filteredCorpora.map(({ entry, index }) => (
                      <div
                        key={entry.id || index}
                        className={cn(
                          'space-y-2.5 rounded-lg border p-3',
                          entry.scope === 'owner_only'
                            ? 'border-amber-500/35 bg-amber-500/[0.03]'
                            : 'border-emerald-500/30 bg-emerald-500/[0.02]'
                        )}
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
                            <Select
                              value={entry.category}
                              onValueChange={(nextCat: SampleCorpusCategory) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.sample_corpora]
                                  list[index] = { ...list[index], category: nextCat }
                                  return { ...prev, sample_corpora: list }
                                })
                              }
                            >
                              <SelectTrigger className="h-7 w-48 text-xs">
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                {CORPUS_CATEGORIES.filter((c) => c.value !== 'all').map((c) => (
                                  <SelectItem key={c.value} value={c.value}>
                                    {c.label}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>

                            <Input
                              value={entry.situation_title}
                              onChange={(e) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.sample_corpora]
                                  list[index] = {
                                    ...list[index],
                                    situation_title: e.target.value,
                                  }
                                  return { ...prev, sample_corpora: list }
                                })
                              }
                              placeholder="情境标题 (situation_title)"
                              className="h-7 min-w-[10rem] flex-1 text-xs font-semibold"
                            />

                            <Input
                              value={entry.tier_label}
                              onChange={(e) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.sample_corpora]
                                  list[index] = { ...list[index], tier_label: e.target.value }
                                  return { ...prev, sample_corpora: list }
                                })
                              }
                              placeholder="分级档位标签 (tier_label，如 1. 突发异样)"
                              className="h-7 w-44 text-xs"
                            />
                            <Badge variant="outline" className="font-mono text-[10px] text-muted-foreground">
                              {entry.id}
                            </Badge>
                          </div>

                          <div className="flex items-center gap-2">
                            <ScopeSegmentedToggle
                              size="xs"
                              value={entry.scope}
                              onChange={(nextScope) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.sample_corpora]
                                  list[index] = { ...list[index], scope: nextScope }
                                  return { ...prev, sample_corpora: list }
                                })
                              }
                            />
                            <Button
                              type="button"
                              size="icon"
                              variant="ghost"
                              className="h-7 w-7 text-muted-foreground hover:text-destructive"
                              onClick={() =>
                                updateEditingCard((prev) => ({
                                  ...prev,
                                  sample_corpora: prev.sample_corpora.filter(
                                    (_, i) => i !== index
                                  ),
                                }))
                              }
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          </div>
                        </div>

                        <div className="grid gap-2.5 md:grid-cols-2">
                          <div className="space-y-1">
                            <Label className="text-[11px] text-muted-foreground">
                              动作与体态描写 (action_and_posture)
                            </Label>
                            <Textarea
                              rows={3}
                              value={entry.action_and_posture}
                              onChange={(e) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.sample_corpora]
                                  list[index] = {
                                    ...list[index],
                                    action_and_posture: e.target.value,
                                  }
                                  return { ...prev, sample_corpora: list }
                                })
                              }
                              placeholder="该情境下的微表情、眼神、肢体小动作描写..."
                              className="text-xs"
                            />
                          </div>
                          <div className="space-y-1">
                            <Label className="text-[11px] text-muted-foreground">
                              可用短句列表 (sample_lines，每行一句)
                            </Label>
                            <DraftLinesTextarea
                              rows={3}
                              value={entry.sample_lines}
                              onChange={(nextLines) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.sample_corpora]
                                  list[index] = {
                                    ...list[index],
                                    sample_lines: nextLines,
                                  }
                                  return { ...prev, sample_corpora: list }
                                })
                              }
                              placeholder="每行输入一句代表性台词..."
                              className="text-xs"
                            />
                          </div>
                        </div>

                        <div className="grid gap-2.5 sm:grid-cols-2">
                          <div className="space-y-1">
                            <Label className="text-[11px] text-muted-foreground">
                              触发关键词 (trigger_keywords，逗号分隔)
                            </Label>
                            <DraftTokenInput
                              value={entry.trigger_keywords}
                              onChange={(nextKeywords) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.sample_corpora]
                                  list[index] = {
                                    ...list[index],
                                    trigger_keywords: nextKeywords,
                                  }
                                  return { ...prev, sample_corpora: list }
                                })
                              }
                              placeholder="如：加班, 熬夜, 喝茶, 累"
                              className="h-7 text-xs"
                            />
                          </div>
                          <div className="space-y-1">
                            <Label className="text-[11px] text-muted-foreground">
                              MVU 触发条件 (mvu_condition，如 affection &gt;= 60 或 love_locked == true)
                            </Label>
                            <Input
                              value={entry.mvu_condition}
                              onChange={(e) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.sample_corpora]
                                  list[index] = {
                                    ...list[index],
                                    mvu_condition: e.target.value,
                                  }
                                  return { ...prev, sample_corpora: list }
                                })
                              }
                              placeholder="留空则无门槛；或填 affection >= 40"
                              className="h-7 font-mono text-xs"
                            />
                          </div>
                        </div>
                      </div>
                    ))
                  )}
                </CardContent>
              </Card>
            </TabsContent>

            {/* ==================== TAB 3: 过往经历与生活切片 ==================== */}
            <TabsContent value="past_experiences" className="space-y-4">
              <Card>
                <CardHeader className="border-b border-border/50 pb-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <CardTitle className="text-sm font-semibold">
                        过往经历与分场景生活切片 (past_experiences · 当前筛选 {filteredExperiences.length} / 共 {editingCard.past_experiences.length} 条)
                      </CardTitle>
                      <p className="text-xs text-muted-foreground">
                        时间轴履历可设为常驻注入 (constant=true)；分场景生活切片支持按触发关键词与 240 字角色名邻近度动态召回。
                      </p>
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        updateEditingCard((prev) => {
                          const cat: PastExperienceCategory =
                            experienceCategoryFilter === 'all'
                              ? 'life_vignette'
                              : experienceCategoryFilter
                          const newExp: PastExperienceEntry = {
                            id: makeEntryId('exp'),
                            title: '新增经历/生活切片',
                            period_or_scene: '',
                            category: cat,
                            narrative: '',
                            usage_note_or_monologue:
                              '参考该场景的口吻与互动方式，按当前剧情取用，不强制复刻事件或代写玩家回应。',
                            constant: cat === 'timeline_milestone',
                            trigger_keywords: [],
                            scope: scopeFilter === 'owner_only' ? 'owner_only' : 'general',
                          }
                          return {
                            ...prev,
                            past_experiences: [newExp, ...prev.past_experiences],
                          }
                        })
                      }
                    >
                      <Plus className="mr-1 h-3.5 w-3.5" />
                      新增经历/切片条目
                    </Button>
                  </div>

                  {/* Filter pills */}
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {(
                      [
                        ['all', '全部条目'],
                        ['timeline_milestone', '时间轴履历 (timeline_milestone)'],
                        ['life_vignette', '分场景生活切片 (life_vignette)'],
                      ] as const
                    ).map(([val, label]) => (
                      <button
                        key={val}
                        type="button"
                        onClick={() => setExperienceCategoryFilter(val)}
                        className={cn(
                          'rounded-full border px-3 py-1 text-xs font-medium transition-colors',
                          experienceCategoryFilter === val
                            ? 'border-primary bg-primary text-primary-foreground'
                            : 'border-border/70 bg-muted/30 text-muted-foreground hover:text-foreground'
                        )}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </CardHeader>

                <CardContent className="space-y-3 pt-3">
                  {filteredExperiences.length === 0 ? (
                    <p className="py-6 text-center text-xs text-muted-foreground">
                      当前筛选下暂无过往经历或生活切片条目。
                    </p>
                  ) : (
                    filteredExperiences.map(({ entry, index }) => (
                      <div
                        key={entry.id || index}
                        className={cn(
                          'space-y-2.5 rounded-lg border p-3',
                          entry.scope === 'owner_only'
                            ? 'border-amber-500/35 bg-amber-500/[0.03]'
                            : 'border-emerald-500/30 bg-emerald-500/[0.02]'
                        )}
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
                            <Select
                              value={entry.category}
                              onValueChange={(nextCat: PastExperienceCategory) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.past_experiences]
                                  list[index] = { ...list[index], category: nextCat }
                                  return { ...prev, past_experiences: list }
                                })
                              }
                            >
                              <SelectTrigger className="h-7 w-44 text-xs">
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="timeline_milestone">
                                  时间轴履历 (timeline_milestone)
                                </SelectItem>
                                <SelectItem value="life_vignette">
                                  分场景生活切片 (life_vignette)
                                </SelectItem>
                              </SelectContent>
                            </Select>

                            <Input
                              value={entry.title}
                              onChange={(e) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.past_experiences]
                                  list[index] = { ...list[index], title: e.target.value }
                                  return { ...prev, past_experiences: list }
                                })
                              }
                              placeholder="标题 (title)"
                              className="h-7 min-w-[10rem] flex-1 text-xs font-semibold"
                            />

                            <Input
                              value={entry.period_or_scene}
                              onChange={(e) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.past_experiences]
                                  list[index] = {
                                    ...list[index],
                                    period_or_scene: e.target.value,
                                  }
                                  return { ...prev, past_experiences: list }
                                })
                              }
                              placeholder="时期/场景 (如 15-22岁 或 深夜书房)"
                              className="h-7 w-44 text-xs"
                            />
                          </div>

                          <div className="flex flex-wrap items-center gap-2">
                            <div className="flex items-center gap-1.5 rounded border bg-background/70 px-2 py-1">
                              <Switch
                                id={`exp-constant-${index}`}
                                checked={entry.constant}
                                onCheckedChange={(checked) =>
                                  updateEditingCard((prev) => {
                                    const list = [...prev.past_experiences]
                                    list[index] = { ...list[index], constant: checked }
                                    return { ...prev, past_experiences: list }
                                  })
                                }
                              />
                              <Label
                                htmlFor={`exp-constant-${index}`}
                                className="cursor-pointer text-[11px] font-medium"
                              >
                                常驻注入 (constant)
                              </Label>
                            </div>

                            <ScopeSegmentedToggle
                              size="xs"
                              value={entry.scope}
                              onChange={(nextScope) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.past_experiences]
                                  list[index] = { ...list[index], scope: nextScope }
                                  return { ...prev, past_experiences: list }
                                })
                              }
                            />

                            <Button
                              type="button"
                              size="icon"
                              variant="ghost"
                              className="h-7 w-7 text-muted-foreground hover:text-destructive"
                              onClick={() =>
                                updateEditingCard((prev) => ({
                                  ...prev,
                                  past_experiences: prev.past_experiences.filter(
                                    (_, i) => i !== index
                                  ),
                                }))
                              }
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          </div>
                        </div>

                        <div className="grid gap-2.5 md:grid-cols-2">
                          <div className="space-y-1">
                            <Label className="text-[11px] text-muted-foreground">
                              经历叙事正文 (narrative)
                            </Label>
                            <Textarea
                              rows={3}
                              value={entry.narrative}
                              onChange={(e) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.past_experiences]
                                  list[index] = { ...list[index], narrative: e.target.value }
                                  return { ...prev, past_experiences: list }
                                })
                              }
                              placeholder="详细描述该时期的人生经历或具体生活切片场景..."
                              className="text-xs"
                            />
                          </div>
                          <div className="space-y-1">
                            <Label className="text-[11px] text-muted-foreground">
                              第一人称心声 / 取用说明 (usage_note_or_monologue)
                            </Label>
                            <Textarea
                              rows={3}
                              value={entry.usage_note_or_monologue}
                              onChange={(e) =>
                                updateEditingCard((prev) => {
                                  const list = [...prev.past_experiences]
                                  list[index] = {
                                    ...list[index],
                                    usage_note_or_monologue: e.target.value,
                                  }
                                  return { ...prev, past_experiences: list }
                                })
                              }
                              placeholder="角色内心独白或提示模型的取用边界..."
                              className="text-xs"
                            />
                          </div>
                        </div>

                        <div className="space-y-1">
                          <Label className="text-[11px] text-muted-foreground">
                            触发关键词 (trigger_keywords，逗号分隔；当 constant=false 时凭关键词或 240 字邻近度按需注入)
                          </Label>
                          <DraftTokenInput
                            value={entry.trigger_keywords}
                            onChange={(nextKeywords) =>
                              updateEditingCard((prev) => {
                                const list = [...prev.past_experiences]
                                list[index] = {
                                  ...list[index],
                                  trigger_keywords: nextKeywords,
                                }
                                return { ...prev, past_experiences: list }
                              })
                            }
                            placeholder="如：旧照片, 药箱, 书签, 留灯, 雨夜"
                            className="h-7 text-xs"
                          />
                        </div>
                      </div>
                    ))
                  )}
                </CardContent>
              </Card>
            </TabsContent>

            {/* ==================== TAB 4: MVU 动态变量与分阶段演进 ==================== */}
            <TabsContent value="mvu_stages" className="space-y-4">
              {/* 4.1 实时变量控制台 (Live Runtime State in CompanionTruthDB) */}
              <Card className="border-primary/35 bg-primary/[0.03]">
                <CardHeader className="border-b border-primary/20 pb-2.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="space-y-0.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <CardTitle className="flex items-center gap-2 text-sm font-semibold">
                          <Flame className="h-4 w-4 text-primary" />
                          实时变量控制台 (CompanionTruthDB Live Runtime State)
                        </CardTitle>
                        {previewActiveStage ? (
                          <Badge className="bg-primary text-xs text-primary-foreground">
                            <Heart className="mr-1 h-3 w-3" />
                            当前激活阶段：{previewActiveStage.stage_name}
                          </Badge>
                        ) : (
                          <Badge variant="outline" className="text-xs">
                            未命中特定阶段
                          </Badge>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">
                        实时读取与手动干预 SQLite 权威真理库中的角色动态状态变量；修改后立即影响下一轮对话注入的 &lt;status_current_variables&gt; 与阶段导引。
                      </p>
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={resettingMvu || savingMvu}
                        onClick={() => void handleResetLiveMvuState()}
                      >
                        <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
                        {resettingMvu ? '重置中...' : '重置为初始值'}
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        disabled={savingMvu || resettingMvu}
                        onClick={() => void handleSaveLiveMvuState()}
                      >
                        <Save className="mr-1.5 h-3.5 w-3.5" />
                        {savingMvu ? '写入中...' : '保存实时变量值'}
                      </Button>
                    </div>
                  </div>
                </CardHeader>

                <CardContent className="space-y-4 pt-3">
                  {filteredMvuVariables.length === 0 ? (
                    <p className="py-4 text-center text-xs text-muted-foreground">
                      当前过滤视图下暂无实时 MVU 变量，可在下方 Schema 列表中新增或调整可见域。
                    </p>
                  ) : (
                    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                      {filteredMvuVariables.map(({ variable }) => {
                        const isPrivate = variable.visibility === 'PRIVATE'
                        const targetVar = editingCard.mvu_config.affinity_guard.target_var || 'affection'
                        const lockVar = editingCard.mvu_config.affinity_guard.lock_var || 'love_locked'
                        const rawVal =
                          liveMvuState[variable.key] !== undefined
                            ? liveMvuState[variable.key]
                            : variable.default_value
                        const borderClass = isPrivate
                          ? 'border-amber-500/30 bg-background/90'
                          : 'border-emerald-500/30 bg-background/90'

                        if (variable.type === 'number') {
                          const minBound = variable.min ?? 0
                          const maxBound = variable.max ?? 100
                          const numVal = Number.isFinite(Number(rawVal)) ? Number(rawVal) : minBound
                          const clampedNum = Math.max(minBound, Math.min(maxBound, numVal))
                          const step = variable.key === targetVar ? 0.5 : 1

                          return (
                            <div
                              key={`live-mvu-${variable.key}`}
                              className={cn('space-y-2 rounded-lg border p-3', borderClass)}
                            >
                              <div className="flex flex-wrap items-center justify-between gap-1.5">
                                <Label className="text-xs font-semibold">
                                  {isPrivate ? '🔒' : '🌐'} {variable.label} ({variable.key})
                                </Label>
                                <div className="flex items-center gap-1">
                                  {variable.shared && (
                                    <Badge variant="outline" className="text-[10px]">
                                      跨卡共享
                                    </Badge>
                                  )}
                                  <Badge variant="secondary" className="font-mono text-xs">
                                    {numVal} / {maxBound}
                                  </Badge>
                                </div>
                              </div>
                              <div className="flex items-center gap-3">
                                <Slider
                                  min={minBound}
                                  max={maxBound}
                                  step={step}
                                  value={[clampedNum]}
                                  onValueChange={([val]) =>
                                    setLiveMvuState((prev) => ({ ...prev, [variable.key]: val }))
                                  }
                                  className="flex-1"
                                />
                                <DraftNumberInput
                                  aria-label={variable.label}
                                  min={minBound}
                                  max={maxBound}
                                  step={step}
                                  value={numVal}
                                  onValueChange={(nextNum) =>
                                    setLiveMvuState((prev) => ({
                                      ...prev,
                                      [variable.key]: Math.max(minBound, Math.min(maxBound, nextNum)),
                                    }))
                                  }
                                  className="h-8 w-20 font-mono text-xs"
                                />
                              </div>
                              <p className="text-[11px] text-muted-foreground">
                                {variable.key === targetVar
                                  ? liveLoveLocked
                                    ? `已触发关系锁定，${variable.key} 上限可达 ${maxBound}。`
                                    : `未锁定关系前受 Affinity Guard 保护，上限 ${editingCard.mvu_config.affinity_guard.cap_before_lock}。`
                                  : variable.description || `数值区间 ${minBound} ~ ${maxBound}`}
                              </p>
                            </div>
                          )
                        }

                        if (variable.type === 'boolean') {
                          const boolVal = Boolean(rawVal)
                          return (
                            <div
                              key={`live-mvu-${variable.key}`}
                              className={cn(
                                'flex flex-col justify-between space-y-2 rounded-lg border p-3',
                                borderClass
                              )}
                            >
                              <div className="flex items-start justify-between gap-2">
                                <div className="space-y-0.5">
                                  <div className="flex flex-wrap items-center gap-1.5">
                                    <Label
                                      htmlFor={`live-mvu-${variable.key}`}
                                      className="text-xs font-semibold"
                                    >
                                      {isPrivate ? '🔒' : '🌐'} {variable.label} ({variable.key})
                                    </Label>
                                    {variable.shared && (
                                      <Badge variant="outline" className="text-[10px]">
                                        跨卡共享
                                      </Badge>
                                    )}
                                  </div>
                                  <p className="text-[11px] text-muted-foreground">
                                    {variable.description ||
                                      (variable.key === lockVar
                                        ? '开启后解锁最高阶段与恋爱锁定专属语料'
                                        : '布尔开关状态变量')}
                                  </p>
                                </div>
                                <Switch
                                  id={`live-mvu-${variable.key}`}
                                  checked={boolVal}
                                  onCheckedChange={(checked) =>
                                    setLiveMvuState((prev) => ({
                                      ...prev,
                                      [variable.key]: checked,
                                      ...(variable.key === lockVar &&
                                      checked &&
                                      Number(prev[targetVar] ?? 0) >= 99 &&
                                      Number(prev[targetVar] ?? 0) < 100
                                        ? { [targetVar]: 100 }
                                        : {}),
                                    }))
                                  }
                                />
                              </div>
                            </div>
                          )
                        }

                        const strVal = String(rawVal ?? '')
                        const enumOptions =
                          variable.enum_values.length > 0
                            ? variable.enum_values
                            : variable.key === 'intimacy_tier'
                              ? INTIMACY_TIER_OPTIONS
                              : []

                        return (
                          <div
                            key={`live-mvu-${variable.key}`}
                            className={cn('space-y-2 rounded-lg border p-3', borderClass)}
                          >
                            <div className="flex flex-wrap items-center justify-between gap-1.5">
                              <Label
                                htmlFor={`live-mvu-${variable.key}`}
                                className="text-xs font-semibold"
                              >
                                {isPrivate ? '🔒' : '🌐'} {variable.label} ({variable.key})
                              </Label>
                              {variable.shared && (
                                <Badge variant="outline" className="text-[10px]">
                                  跨卡共享
                                </Badge>
                              )}
                            </div>
                            {enumOptions.length > 0 ? (
                              <div className="flex items-center gap-1.5">
                                <Select
                                  value={enumOptions.includes(strVal) ? strVal : '__custom__'}
                                  onValueChange={(v) => {
                                    if (v !== '__custom__') {
                                      setLiveMvuState((prev) => ({ ...prev, [variable.key]: v }))
                                    }
                                  }}
                                >
                                  <SelectTrigger className="h-8 flex-1 text-xs">
                                    <SelectValue placeholder="选择枚举值" />
                                  </SelectTrigger>
                                  <SelectContent>
                                    {enumOptions.map((opt) => (
                                      <SelectItem key={opt} value={opt}>
                                        {opt}
                                      </SelectItem>
                                    ))}
                                    <SelectItem value="__custom__">自定义文本</SelectItem>
                                  </SelectContent>
                                </Select>
                                <Input
                                  id={`live-mvu-${variable.key}`}
                                  value={strVal}
                                  onChange={(e) =>
                                    setLiveMvuState((prev) => ({
                                      ...prev,
                                      [variable.key]: e.target.value,
                                    }))
                                  }
                                  placeholder="状态值"
                                  className="h-8 w-36 text-xs"
                                />
                              </div>
                            ) : strVal.length > 36 ||
                              variable.key.includes('scene') ||
                              variable.description.length > 60 ? (
                              <Textarea
                                id={`live-mvu-${variable.key}`}
                                rows={2}
                                value={strVal}
                                onChange={(e) =>
                                  setLiveMvuState((prev) => ({
                                    ...prev,
                                    [variable.key]: e.target.value,
                                  }))
                                }
                                placeholder={variable.description || `输入 ${variable.key} 值`}
                                className="text-xs"
                              />
                            ) : (
                              <Input
                                id={`live-mvu-${variable.key}`}
                                value={strVal}
                                onChange={(e) =>
                                  setLiveMvuState((prev) => ({
                                    ...prev,
                                    [variable.key]: e.target.value,
                                  }))
                                }
                                placeholder={variable.description || `输入 ${variable.key} 值`}
                                className="h-8 text-xs"
                              />
                            )}
                            {variable.description && (
                              <p className="line-clamp-2 text-[11px] text-muted-foreground">
                                {variable.description}
                              </p>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </CardContent>
              </Card>

              {/* 4.2 MVU 变量结构与防篡改守卫配置 (mvu_config) */}
              <Card>
                <CardHeader className="border-b border-border/50 pb-2.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <CardTitle className="text-sm font-semibold">
                        MVU 变量结构与潜令 V1.8 防篡改守卫配置 (mvu_config)
                      </CardTitle>
                      <p className="text-xs text-muted-foreground">
                        配置好感度单轮变化上下界（默认 [-1, +5]）、未确认关系前好感上限（99），以及每个变量的 PUBLIC/PRIVATE 物理隔离属性。
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <Label htmlFor="mvu-config-enabled" className="text-xs">
                        启用 MVU 引擎 (mvu_config.enabled)
                      </Label>
                      <Switch
                        id="mvu-config-enabled"
                        checked={editingCard.mvu_config.enabled}
                        onCheckedChange={(checked) =>
                          updateEditingCard((prev) => ({
                            ...prev,
                            mvu_config: { ...prev.mvu_config, enabled: checked },
                          }))
                        }
                      />
                    </div>
                  </div>
                </CardHeader>

                <CardContent className="space-y-4 pt-3">
                  {/* Affinity Guard 卡片 */}
                  <div className="rounded-lg border border-amber-500/30 bg-amber-500/[0.03] p-3">
                    <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
                      <span className="text-xs font-semibold text-amber-900 dark:text-amber-200">
                        🛡️ 潜令 V1.8 好感防刷守卫 (MVUAffinityGuardConfig)
                      </span>
                      <div className="flex items-center gap-2">
                        <Label htmlFor="affinity-guard-enabled" className="text-xs">
                          开启单轮钳制与 99 锁上限
                        </Label>
                        <Switch
                          id="affinity-guard-enabled"
                          checked={editingCard.mvu_config.affinity_guard.enabled}
                          onCheckedChange={(checked) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              mvu_config: {
                                ...prev.mvu_config,
                                affinity_guard: {
                                  ...prev.mvu_config.affinity_guard,
                                  enabled: checked,
                                },
                              },
                            }))
                          }
                        />
                      </div>
                    </div>

                    <div className="grid gap-3 sm:grid-cols-3">
                      <div className="space-y-1">
                        <Label className="text-xs">
                          单轮最小好感变化 (min_delta_per_turn)
                        </Label>
                        <DraftNumberInput
                          step={0.5}
                          value={editingCard.mvu_config.affinity_guard.min_delta_per_turn}
                          onValueChange={(nextNum) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              mvu_config: {
                                ...prev.mvu_config,
                                affinity_guard: {
                                  ...prev.mvu_config.affinity_guard,
                                  min_delta_per_turn: nextNum,
                                },
                              },
                            }))
                          }
                          className="h-8 font-mono text-xs"
                        />
                      </div>
                      <div className="space-y-1">
                        <Label className="text-xs">
                          单轮最大好感变化 (max_delta_per_turn)
                        </Label>
                        <DraftNumberInput
                          step={0.5}
                          value={editingCard.mvu_config.affinity_guard.max_delta_per_turn}
                          onValueChange={(nextNum) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              mvu_config: {
                                ...prev.mvu_config,
                                affinity_guard: {
                                  ...prev.mvu_config.affinity_guard,
                                  max_delta_per_turn: nextNum,
                                },
                              },
                            }))
                          }
                          className="h-8 font-mono text-xs"
                        />
                      </div>
                      <div className="space-y-1">
                        <Label className="text-xs">
                          确认关系前好感上限 (cap_before_lock)
                        </Label>
                        <DraftNumberInput
                          value={editingCard.mvu_config.affinity_guard.cap_before_lock}
                          onValueChange={(nextNum) =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              mvu_config: {
                                ...prev.mvu_config,
                                affinity_guard: {
                                  ...prev.mvu_config.affinity_guard,
                                  cap_before_lock: nextNum,
                                },
                              },
                            }))
                          }
                          className="h-8 font-mono text-xs"
                        />
                      </div>
                    </div>
                  </div>

                  {/* 跨卡共享命名空间与更新规则提示词 */}
                  <div className="grid gap-3 rounded-lg border border-border/60 bg-muted/20 p-3 md:grid-cols-3">
                    <div className="space-y-1">
                      <Label htmlFor="mvu-shared-namespace" className="text-xs">
                        跨卡共享命名空间 (shared_namespace)
                      </Label>
                      <Input
                        id="mvu-shared-namespace"
                        value={editingCard.mvu_config.shared_namespace ?? ''}
                        onChange={(e) =>
                          updateEditingCard((prev) => ({
                            ...prev,
                            mvu_config: {
                              ...prev.mvu_config,
                              shared_namespace: e.target.value,
                            },
                          }))
                        }
                        placeholder="如：twin_xu（留空表示不启用跨卡共享）"
                        className="h-8 font-mono text-xs"
                      />
                    </div>
                    <div className="space-y-1 md:col-span-2">
                      <Label htmlFor="mvu-update-instruction" className="text-xs">
                        MVU 变量演化规则说明 (update_instruction)
                      </Label>
                      <Textarea
                        id="mvu-update-instruction"
                        rows={3}
                        value={editingCard.mvu_config.update_instruction}
                        onChange={(e) =>
                          updateEditingCard((prev) => ({
                            ...prev,
                            mvu_config: {
                              ...prev.mvu_config,
                              update_instruction: e.target.value,
                            },
                          }))
                        }
                        placeholder="描述每轮对话后模型应如何通过 <UpdateVariable> 更新各状态变量..."
                        className="text-xs"
                      />
                    </div>
                  </div>

                  {/* 变量定义列表 */}
                  <div className="space-y-2.5">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold">
                        MVU 变量 Schema 列表 (显示 {filteredMvuVariables.length} / 共 {editingCard.mvu_config.variables.length} 项)
                      </span>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs"
                        onClick={() =>
                          updateEditingCard((prev) => {
                            const newVar: MVUVariableSchema = {
                              key: `custom_var_${prev.mvu_config.variables.length + 1}`,
                              label: '自定义状态变量',
                              type: 'string',
                              default_value: '',
                              min: null,
                              max: null,
                              enum_values: [],
                              monotonic_true: false,
                              visibility:
                                scopeFilter === 'owner_only' ? 'PRIVATE' : 'PUBLIC',
                              description: '',
                            }
                            return {
                              ...prev,
                              mvu_config: {
                                ...prev.mvu_config,
                                variables: [...prev.mvu_config.variables, newVar],
                              },
                            }
                          })
                        }
                      >
                        <Plus className="mr-1 h-3 w-3" />
                        新增变量定义
                      </Button>
                    </div>

                    <div className="space-y-2">
                      {filteredMvuVariables.map(({ variable, index }) => (
                        <div
                          key={`mvu-var-${index}`}
                          className={cn(
                            'space-y-2 rounded-lg border p-2.5',
                            variable.visibility === 'PRIVATE'
                              ? 'border-amber-500/35 bg-amber-500/[0.03]'
                              : 'border-emerald-500/30 bg-emerald-500/[0.02]'
                          )}
                        >
                          <div className="grid gap-2 sm:grid-cols-12 sm:items-center">
                            <div className="sm:col-span-2">
                              <Label className="text-[10px] text-muted-foreground">变量键 (key)</Label>
                              <Input
                                aria-label={`变量键 ${index + 1}`}
                                value={variable.key}
                                onChange={(e) =>
                                  updateEditingCard((prev) => {
                                    const vars = [...prev.mvu_config.variables]
                                    vars[index] = { ...vars[index], key: e.target.value }
                                    return {
                                      ...prev,
                                      mvu_config: { ...prev.mvu_config, variables: vars },
                                    }
                                  })
                                }
                                className="h-7 font-mono text-xs"
                              />
                            </div>
                            <div className="sm:col-span-2">
                              <Label className="text-[10px] text-muted-foreground">中文名 (label)</Label>
                              <Input
                                aria-label={`变量中文名 ${index + 1}`}
                                value={variable.label}
                                onChange={(e) =>
                                  updateEditingCard((prev) => {
                                    const vars = [...prev.mvu_config.variables]
                                    vars[index] = { ...vars[index], label: e.target.value }
                                    return {
                                      ...prev,
                                      mvu_config: { ...prev.mvu_config, variables: vars },
                                    }
                                  })
                                }
                                className="h-7 text-xs"
                              />
                            </div>
                            <div className="sm:col-span-2">
                              <Label className="text-[10px] text-muted-foreground">类型 (type)</Label>
                              <Select
                                value={variable.type}
                                onValueChange={(nextType: 'number' | 'string' | 'boolean') =>
                                  updateEditingCard((prev) => {
                                    const vars = [...prev.mvu_config.variables]
                                    const currentDefault = vars[index].default_value
                                    const normalizedDefault =
                                      nextType === 'number'
                                        ? Number.isFinite(Number(currentDefault))
                                          ? Number(currentDefault)
                                          : 0
                                        : nextType === 'boolean'
                                          ? currentDefault === true || currentDefault === 'true'
                                          : typeof currentDefault === 'string'
                                            ? currentDefault
                                            : String(currentDefault ?? '')
                                    vars[index] = {
                                      ...vars[index],
                                      type: nextType,
                                      default_value: normalizedDefault,
                                    }
                                    return {
                                      ...prev,
                                      mvu_config: { ...prev.mvu_config, variables: vars },
                                    }
                                  })
                                }
                              >
                                <SelectTrigger className="h-7 text-xs">
                                  <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                  <SelectItem value="number">number (数值)</SelectItem>
                                  <SelectItem value="string">string (文本)</SelectItem>
                                  <SelectItem value="boolean">boolean (布尔)</SelectItem>
                                </SelectContent>
                              </Select>
                            </div>
                            <div className="sm:col-span-2">
                              <Label className="text-[10px] text-muted-foreground">
                                初始默认值 (default)
                              </Label>
                              {variable.type === 'number' ? (
                                <DraftNumberInput
                                  aria-label={`初始默认值 ${variable.key || index + 1}`}
                                  step={0.5}
                                  value={variable.default_value}
                                  onValueChange={(nextNum) =>
                                    updateEditingCard((prev) => {
                                      const vars = [...prev.mvu_config.variables]
                                      vars[index] = { ...vars[index], default_value: nextNum }
                                      return {
                                        ...prev,
                                        mvu_config: { ...prev.mvu_config, variables: vars },
                                      }
                                    })
                                  }
                                  className="h-7 font-mono text-xs"
                                />
                              ) : variable.type === 'boolean' ? (
                                <div className="flex h-7 items-center gap-2">
                                  <Switch
                                    aria-label={`初始默认值 ${variable.key || index + 1}`}
                                    checked={Boolean(variable.default_value)}
                                    onCheckedChange={(checked) =>
                                      updateEditingCard((prev) => {
                                        const vars = [...prev.mvu_config.variables]
                                        vars[index] = { ...vars[index], default_value: checked }
                                        return {
                                          ...prev,
                                          mvu_config: { ...prev.mvu_config, variables: vars },
                                        }
                                      })
                                    }
                                  />
                                  <span className="font-mono text-xs text-muted-foreground">
                                    {variable.default_value === true || variable.default_value === 'true'
                                      ? 'true'
                                      : 'false'}
                                  </span>
                                </div>
                              ) : (
                                <Input
                                  aria-label={`初始默认值 ${variable.key || index + 1}`}
                                  value={String(variable.default_value ?? '')}
                                  onChange={(e) =>
                                    updateEditingCard((prev) => {
                                      const vars = [...prev.mvu_config.variables]
                                      vars[index] = { ...vars[index], default_value: e.target.value }
                                      return {
                                        ...prev,
                                        mvu_config: { ...prev.mvu_config, variables: vars },
                                      }
                                    })
                                  }
                                  className="h-7 font-mono text-xs"
                                />
                              )}
                            </div>
                            <div className="flex items-end gap-1 sm:col-span-2">
                              <div className="flex-1">
                                <Label className="text-[10px] text-muted-foreground">Min</Label>
                                <Input
                                  type="number"
                                  value={variable.min ?? ''}
                                  onChange={(e) =>
                                    updateEditingCard((prev) => {
                                      const vars = [...prev.mvu_config.variables]
                                      vars[index] = {
                                        ...vars[index],
                                        min:
                                          e.target.value === '' ? null : Number(e.target.value),
                                      }
                                      return {
                                        ...prev,
                                        mvu_config: { ...prev.mvu_config, variables: vars },
                                      }
                                    })
                                  }
                                  placeholder="-"
                                  className="h-7 font-mono text-xs"
                                />
                              </div>
                              <div className="flex-1">
                                <Label className="text-[10px] text-muted-foreground">Max</Label>
                                <Input
                                  type="number"
                                  value={variable.max ?? ''}
                                  onChange={(e) =>
                                    updateEditingCard((prev) => {
                                      const vars = [...prev.mvu_config.variables]
                                      vars[index] = {
                                        ...vars[index],
                                        max:
                                          e.target.value === '' ? null : Number(e.target.value),
                                      }
                                      return {
                                        ...prev,
                                        mvu_config: { ...prev.mvu_config, variables: vars },
                                      }
                                    })
                                  }
                                  placeholder="-"
                                  className="h-7 font-mono text-xs"
                                />
                              </div>
                            </div>
                            <div className="flex items-center justify-end gap-1.5 pt-3 sm:col-span-2 sm:pt-0">
                              <VisibilitySegmentedToggle
                                value={variable.visibility}
                                onChange={(nextVis) =>
                                  updateEditingCard((prev) => {
                                    const vars = [...prev.mvu_config.variables]
                                    vars[index] = { ...vars[index], visibility: nextVis }
                                    return {
                                      ...prev,
                                      mvu_config: { ...prev.mvu_config, variables: vars },
                                    }
                                  })
                                }
                              />
                              <Button
                                type="button"
                                size="icon"
                                variant="ghost"
                                className="h-7 w-7 text-muted-foreground hover:text-destructive"
                                onClick={() =>
                                  updateEditingCard((prev) => ({
                                    ...prev,
                                    mvu_config: {
                                      ...prev.mvu_config,
                                      variables: prev.mvu_config.variables.filter(
                                        (_, i) => i !== index
                                      ),
                                    },
                                  }))
                                }
                              >
                                <Trash2 className="h-3.5 w-3.5" />
                              </Button>
                            </div>
                          </div>

                          <div className="grid gap-2 border-t border-border/40 pt-2 sm:grid-cols-12 sm:items-center">
                            <div className="sm:col-span-4">
                              <Label className="text-[10px] text-muted-foreground">
                                变量说明 (description)
                              </Label>
                              <Input
                                value={variable.description}
                                onChange={(e) =>
                                  updateEditingCard((prev) => {
                                    const vars = [...prev.mvu_config.variables]
                                    vars[index] = { ...vars[index], description: e.target.value }
                                    return {
                                      ...prev,
                                      mvu_config: { ...prev.mvu_config, variables: vars },
                                    }
                                  })
                                }
                                placeholder="说明该变量的语义与演化方式"
                                className="h-7 text-xs"
                              />
                            </div>
                            <div className="sm:col-span-3">
                              <Label className="text-[10px] text-muted-foreground">
                                枚举候选项 (enum_values，逗号分隔)
                              </Label>
                              <DraftTokenInput
                                value={variable.enum_values}
                                onChange={(nextEnums) =>
                                  updateEditingCard((prev) => {
                                    const vars = [...prev.mvu_config.variables]
                                    vars[index] = { ...vars[index], enum_values: nextEnums }
                                    return {
                                      ...prev,
                                      mvu_config: { ...prev.mvu_config, variables: vars },
                                    }
                                  })
                                }
                                placeholder="如：日常态, 微醺破防"
                                className="h-7 text-xs"
                              />
                            </div>
                            <div className="flex flex-wrap items-center gap-3 pt-2 sm:col-span-5 sm:pt-3">
                              <div className="flex items-center gap-1">
                                <Switch
                                  id={`mvu-var-mono-${index}`}
                                  checked={variable.monotonic_true}
                                  onCheckedChange={(checked) =>
                                    updateEditingCard((prev) => {
                                      const vars = [...prev.mvu_config.variables]
                                      vars[index] = { ...vars[index], monotonic_true: checked }
                                      return {
                                        ...prev,
                                        mvu_config: { ...prev.mvu_config, variables: vars },
                                      }
                                    })
                                  }
                                />
                                <Label
                                  htmlFor={`mvu-var-mono-${index}`}
                                  className="cursor-pointer text-[11px] font-normal"
                                >
                                  单向不可逆
                                </Label>
                              </div>
                              <div className="flex items-center gap-1">
                                <Switch
                                  id={`mvu-var-shared-${index}`}
                                  checked={Boolean(variable.shared)}
                                  onCheckedChange={(checked) =>
                                    updateEditingCard((prev) => {
                                      const vars = [...prev.mvu_config.variables]
                                      vars[index] = { ...vars[index], shared: checked }
                                      return {
                                        ...prev,
                                        mvu_config: { ...prev.mvu_config, variables: vars },
                                      }
                                    })
                                  }
                                />
                                <Label
                                  htmlFor={`mvu-var-shared-${index}`}
                                  className="cursor-pointer text-[11px] font-normal"
                                >
                                  跨卡共享
                                </Label>
                              </div>
                              {variable.shared && (
                                <Input
                                  value={variable.shared_key ?? ''}
                                  onChange={(e) =>
                                    updateEditingCard((prev) => {
                                      const vars = [...prev.mvu_config.variables]
                                      vars[index] = { ...vars[index], shared_key: e.target.value }
                                      return {
                                        ...prev,
                                        mvu_config: { ...prev.mvu_config, variables: vars },
                                      }
                                    })
                                  }
                                  placeholder="shared_key"
                                  className="h-7 w-28 font-mono text-xs"
                                />
                              )}
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </CardContent>
              </Card>

              {/* 4.3 分阶段动态覆盖配置 (relationship_stages) */}
              <Card>
                <CardHeader className="border-b border-border/50 pb-2.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <CardTitle className="text-sm font-semibold">
                        分阶段动态覆盖配置 (relationship_stages · 显示 {filteredStages.length} / 共 {editingCard.relationship_stages.length} 个阶段)
                      </CardTitle>
                      <p className="text-xs text-muted-foreground">
                        按好感度区间（0-19, 20-39, 40-59, 60-79, 80-99, 100 恋爱锁定）动态覆盖嘴硬比例、Planner 导引、调色盘补丁与互动边界。
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      {editingCard.relationship_stages.length === 0 && (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            updateEditingCard((prev) => ({
                              ...prev,
                              relationship_stages: createDefaultRelationshipStages(),
                            }))
                          }
                        >
                          填入标准 6 阶段模板
                        </Button>
                      )}
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          updateEditingCard((prev) => {
                            const newStage: RelationshipStageOverlay = {
                              stage_id: `stage_custom_${prev.relationship_stages.length + 1}`,
                              stage_name: '新增关系阶段',
                              min_affection: 0,
                              max_affection: 100,
                              require_love_locked: false,
                              scope: scopeFilter === 'general' ? 'general' : 'owner_only',
                              tsundere_to_honesty_ratio: '',
                              planner_directive: '',
                              palette_override: '',
                              interaction_boundaries: '',
                              unlocked_corpus_ids: [],
                            }
                            return {
                              ...prev,
                              relationship_stages: [...prev.relationship_stages, newStage],
                            }
                          })
                        }
                      >
                        <Plus className="mr-1 h-3.5 w-3.5" />
                        新增阶段卡片
                      </Button>
                    </div>
                  </div>
                </CardHeader>

                <CardContent className="space-y-3 pt-3">
                  {filteredStages.length === 0 ? (
                    <p className="py-6 text-center text-xs text-muted-foreground">
                      当前过滤视图下暂无关系阶段配置。
                    </p>
                  ) : (
                    <div className="grid gap-3 xl:grid-cols-2">
                      {filteredStages.map(({ stage, index }) => {
                        const isCurrentlyActive =
                          previewActiveStage?.stage_id === stage.stage_id ||
                          previewActiveStage?.stage_name === stage.stage_name
                        return (
                          <div
                            key={stage.stage_id || index}
                            className={cn(
                              'space-y-2.5 rounded-lg border p-3 transition-all',
                              isCurrentlyActive
                                ? 'border-primary bg-primary/[0.05] ring-1 ring-primary/30'
                                : stage.scope === 'owner_only'
                                  ? 'border-amber-500/35 bg-amber-500/[0.02]'
                                  : 'border-emerald-500/30 bg-emerald-500/[0.02]'
                            )}
                          >
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <div className="flex min-w-0 flex-1 items-center gap-2">
                                <Badge
                                  variant={isCurrentlyActive ? 'default' : 'secondary'}
                                  className="shrink-0 font-mono text-[11px]"
                                >
                                  {stage.min_affection}~{stage.max_affection}
                                  {stage.require_love_locked ? ' 🔒恋爱锁' : ''}
                                </Badge>
                                <Input
                                  value={stage.stage_name}
                                  onChange={(e) =>
                                    updateEditingCard((prev) => {
                                      const list = [...prev.relationship_stages]
                                      list[index] = {
                                        ...list[index],
                                        stage_name: e.target.value,
                                      }
                                      return { ...prev, relationship_stages: list }
                                    })
                                  }
                                  placeholder="阶段名称 (stage_name)"
                                  className="h-7 min-w-[8rem] flex-1 text-xs font-semibold"
                                />
                              </div>

                              <div className="flex items-center gap-1.5">
                                {isCurrentlyActive && (
                                  <Badge className="bg-emerald-600 text-[10px] text-white">
                                    当前激活
                                  </Badge>
                                )}
                                <ScopeSegmentedToggle
                                  size="xs"
                                  value={stage.scope}
                                  onChange={(nextScope) =>
                                    updateEditingCard((prev) => {
                                      const list = [...prev.relationship_stages]
                                      list[index] = { ...list[index], scope: nextScope }
                                      return { ...prev, relationship_stages: list }
                                    })
                                  }
                                />
                                <Button
                                  type="button"
                                  size="icon"
                                  variant="ghost"
                                  className="h-7 w-7 text-muted-foreground hover:text-destructive"
                                  onClick={() =>
                                    updateEditingCard((prev) => ({
                                      ...prev,
                                      relationship_stages: prev.relationship_stages.filter(
                                        (_, i) => i !== index
                                      ),
                                    }))
                                  }
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                </Button>
                              </div>
                            </div>

                            <div className="flex flex-wrap items-center gap-3 rounded bg-muted/40 px-2.5 py-1.5 text-xs">
                              <div className="flex items-center gap-1.5">
                                <span className="text-[11px] text-muted-foreground">
                                  好感区间:
                                </span>
                                <DraftNumberInput
                                  integer
                                  min={0}
                                  value={stage.min_affection}
                                  onValueChange={(nextMin) =>
                                    updateEditingCard((prev) => {
                                      const list = [...prev.relationship_stages]
                                      list[index] = {
                                        ...list[index],
                                        min_affection: nextMin,
                                      }
                                      return { ...prev, relationship_stages: list }
                                    })
                                  }
                                  className="h-6 w-14 font-mono text-xs"
                                />
                                <span>~</span>
                                <DraftNumberInput
                                  integer
                                  min={0}
                                  value={stage.max_affection}
                                  onValueChange={(nextMax) =>
                                    updateEditingCard((prev) => {
                                      const list = [...prev.relationship_stages]
                                      list[index] = {
                                        ...list[index],
                                        max_affection: nextMax,
                                      }
                                      return { ...prev, relationship_stages: list }
                                    })
                                  }
                                  className="h-6 w-14 font-mono text-xs"
                                />
                              </div>

                              <div className="ml-auto flex items-center gap-1.5">
                                <Switch
                                  id={`stage-lovelock-${index}`}
                                  checked={stage.require_love_locked}
                                  onCheckedChange={(checked) =>
                                    updateEditingCard((prev) => {
                                      const list = [...prev.relationship_stages]
                                      list[index] = {
                                        ...list[index],
                                        require_love_locked: checked,
                                      }
                                      return { ...prev, relationship_stages: list }
                                    })
                                  }
                                />
                                <Label
                                  htmlFor={`stage-lovelock-${index}`}
                                  className="cursor-pointer text-[11px] font-medium"
                                >
                                  需确认恋爱锁定 (require_love_locked)
                                </Label>
                              </div>
                            </div>

                            <div className="space-y-1">
                              <Label className="text-[11px] text-muted-foreground">
                                嘴硬与真心话比例 (tsundere_to_honesty_ratio)
                              </Label>
                              <Input
                                value={stage.tsundere_to_honesty_ratio}
                                onChange={(e) =>
                                  updateEditingCard((prev) => {
                                    const list = [...prev.relationship_stages]
                                    list[index] = {
                                      ...list[index],
                                      tsundere_to_honesty_ratio: e.target.value,
                                    }
                                    return { ...prev, relationship_stages: list }
                                  })
                                }
                                placeholder="如：7:3（嘴上维持长姐威严，行动上处处留门偏心）"
                                className="h-7 text-xs"
                              />
                            </div>

                            <div className="grid gap-2 sm:grid-cols-2">
                              <div className="space-y-1">
                                <Label className="text-[11px] text-muted-foreground">
                                  Planner 阶段导引 (planner_directive)
                                </Label>
                                <Textarea
                                  rows={2}
                                  value={stage.planner_directive}
                                  onChange={(e) =>
                                    updateEditingCard((prev) => {
                                      const list = [...prev.relationship_stages]
                                      list[index] = {
                                        ...list[index],
                                        planner_directive: e.target.value,
                                      }
                                      return { ...prev, relationship_stages: list }
                                    })
                                  }
                                  className="text-xs"
                                />
                              </div>
                              <div className="space-y-1">
                                <Label className="text-[11px] text-muted-foreground">
                                  性格调色盘覆盖补丁 (palette_override)
                                </Label>
                                <Textarea
                                  rows={2}
                                  value={stage.palette_override}
                                  onChange={(e) =>
                                    updateEditingCard((prev) => {
                                      const list = [...prev.relationship_stages]
                                      list[index] = {
                                        ...list[index],
                                        palette_override: e.target.value,
                                      }
                                      return { ...prev, relationship_stages: list }
                                    })
                                  }
                                  className="text-xs"
                                />
                              </div>
                            </div>

                            <div className="space-y-1">
                              <Label className="text-[11px] text-muted-foreground">
                                本阶段互动边界 (interaction_boundaries)
                              </Label>
                              <Textarea
                                rows={2}
                                value={stage.interaction_boundaries}
                                onChange={(e) =>
                                  updateEditingCard((prev) => {
                                    const list = [...prev.relationship_stages]
                                    list[index] = {
                                      ...list[index],
                                      interaction_boundaries: e.target.value,
                                    }
                                    return { ...prev, relationship_stages: list }
                                  })
                                }
                                className="text-xs"
                              />
                            </div>

                            <div className="space-y-1">
                              <Label className="text-[11px] text-muted-foreground">
                                本阶段核心解锁语料 ID (unlocked_corpus_ids，逗号分隔)
                              </Label>
                              <DraftTokenInput
                                value={stage.unlocked_corpus_ids}
                                onChange={(nextIds) =>
                                  updateEditingCard((prev) => {
                                    const list = [...prev.relationship_stages]
                                    list[index] = {
                                      ...list[index],
                                      unlocked_corpus_ids: nextIds,
                                    }
                                    return { ...prev, relationship_stages: list }
                                  })
                                }
                                placeholder="如：xuyan_echo_01, xuyan_flirt_01"
                                className="h-7 font-mono text-xs"
                              />
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  )}
                </CardContent>
              </Card>
            </TabsContent>
          </Tabs>

          {/* ─── 4. 底部保存与操作状态栏 ─── */}
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border/70 bg-muted/30 px-3.5 py-2.5">
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>
                当前卡片：<strong className="text-foreground">{editingCard.name}</strong>
              </span>
              {hasCardUnsavedChanges ? (
                <Badge variant="outline" className="border-amber-500/50 text-amber-600">
                  有未保存的卡片修改
                </Badge>
              ) : (
                <Badge variant="outline" className="text-[10px]">
                  已同步至最新
                </Badge>
              )}
            </div>
            <div className="flex items-center gap-2">
              {!['qianling_', 'maibot_'].some((p) => editingCard.card_id.startsWith(p)) && (
                <Button
                  type="button"
                  size="sm"
                  variant="destructive"
                  onClick={() => void handleDeleteCurrentCard()}
                >
                  <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                  删除此自定义卡片
                </Button>
              )}
              <Button
                type="button"
                size="sm"
                onClick={() => void handleSaveCurrentCard()}
                disabled={savingCard}
              >
                <Save className="mr-1.5 h-3.5 w-3.5" />
                {savingCard ? '正在保存...' : '保存当前人物卡片修改'}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* ─── 新建人物卡片弹窗 ─── */}
      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent className="[--dialog-width:36rem]">
          <DialogHeader>
            <DialogTitle>新建 Persona V2 人物卡片</DialogTitle>
            <DialogDescription>
              创建一张自带「🌐 通用设定 / 🔒 Owner专属设定」双域隔离结构与 6 阶段 MVU 动态演进模板的新角色卡。
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-3">
              <div className="space-y-1.5">
                <Label htmlFor="new-card-name">角色名称 *</Label>
                <Input
                  id="new-card-name"
                  value={newCardName}
                  onChange={(e) => setNewCardName(e.target.value)}
                  placeholder="如：云岁宁 / 许妍 / 新角色名"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="new-card-id">卡片唯一 ID (可选，留空自动生成)</Label>
                <Input
                  id="new-card-id"
                  value={newCardId}
                  onChange={(e) => setNewCardId(e.target.value)}
                  placeholder="如：custom_character_01"
                  className="font-mono"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="new-card-role">角色定位标签 (role_tag)</Label>
                <Input
                  id="new-card-role"
                  value={newCardRoleTag}
                  onChange={(e) => setNewCardRoleTag(e.target.value)}
                  placeholder="如：25岁 · 行为医学中心主任 / 大姐"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="new-card-pitch">一句话灵魂基调 (soul_pitch)</Label>
                <Textarea
                  id="new-card-pitch"
                  rows={3}
                  value={newCardSoulPitch}
                  onChange={(e) => setNewCardSoulPitch(e.target.value)}
                  placeholder="简述角色的公开气场与私下面对 Owner 时的反差张力..."
                />
              </div>
            </div>
          </DialogBody>
          <div className="flex items-center justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => setCreateDialogOpen(false)}
            >
              取消
            </Button>
            <Button
              type="button"
              disabled={creatingCard}
              onClick={() => void handleCreateCard()}
            >
              {creatingCard ? '创建中...' : '确认创建并进入编辑'}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
