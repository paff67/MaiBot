import { useEffect, useMemo, useState } from 'react'
import { Download, Plus, Wrench } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { useToast } from '@/hooks/use-toast'
import {
  clearPersonalityImport, parsePersonalityImport, PERSONALITY_IMPORT_EVENT, PERSONALITY_IMPORT_EXAMPLE,
  readPersonalityImport, type PersonalityProfile as Profile,
} from '@/lib/personality-import'

const SLOT_UNSET = '__unset__'

type ExtendedProfile = Profile & {
  persona_v2_card_id?: string
  mvu_enabled?: boolean
}

function snapshot(values: Record<string, unknown>, name: string): ExtendedProfile {
  return {
    name,
    personality: values.personality as string,
    behavior_style: values.behavior_style as string,
    reply_style: values.reply_style as string,
    group_chat_prompt: values.group_chat_prompt as string,
    private_chat_prompts: values.private_chat_prompts as string,
    multiple_reply_style: values.multiple_reply_style as string[],
    multiple_probability: values.multiple_probability as number,
    ...(typeof values.nickname === 'string' ? { nickname: values.nickname } : {}),
    ...(Array.isArray(values.alias_names) ? { alias_names: values.alias_names as string[] } : {}),
    ...(typeof values.owner_private_prompt === 'string'
      ? { owner_private_prompt: values.owner_private_prompt }
      : {}),
    ...(typeof values.st_preset_file === 'string' ? { st_preset_file: values.st_preset_file } : {}),
    ...(typeof values.persona_v2_card_id === 'string'
      ? { persona_v2_card_id: values.persona_v2_card_id }
      : {}),
    ...(typeof values.mvu_enabled === 'boolean' ? { mvu_enabled: values.mvu_enabled } : {}),
  }
}

export function PersonalityProfiles({ values, onChange }: {
  values: Record<string, unknown>
  onChange: (values: Record<string, unknown>) => void
}) {
  const [panel, setPanel] = useState<'create' | 'manage' | 'import' | null>(() => readPersonalityImport() !== null ? 'import' : null)
  const [importText, setImportText] = useState(() => readPersonalityImport() ?? '')
  const [draft, setDraft] = useState<ExtendedProfile | null>(null)
  const [originalName, setOriginalName] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const { toast } = useToast()
  const profiles = (values.profiles ?? []) as ExtendedProfile[]
  const selected = profiles.find((profile) => profile.name === values.selected_profile)
  const twinModeEnabled = Boolean(values.twin_mode_enabled)
  const mvuEnabled = values.mvu_enabled === undefined ? true : Boolean(values.mvu_enabled)
  const activeV2CardId = typeof values.persona_v2_card_id === 'string' ? values.persona_v2_card_id : (selected?.persona_v2_card_id ?? '')
  const youngerSlotProfile = typeof values.younger_slot_profile === 'string' ? values.younger_slot_profile : ''
  const elderSlotProfile = typeof values.elder_slot_profile === 'string' ? values.elder_slot_profile : ''

  const imported = useMemo(() => {
    if (!importText.trim()) return { profile: null, error: '' }
    try { return { profile: parsePersonalityImport(importText), error: '' } }
    catch (error) { return { profile: null, error: (error as Error).message } }
  }, [importText])

  useEffect(() => {
    const receive = () => {
      setImportText(readPersonalityImport() ?? '')
      setDraft(null)
      setOriginalName(null)
      setConfirmDelete(false)
      setPanel('import')
    }
    window.addEventListener(PERSONALITY_IMPORT_EVENT, receive)
    return () => window.removeEventListener(PERSONALITY_IMPORT_EVENT, receive)
  }, [])

  const closePanel = () => {
    clearPersonalityImport()
    setPanel(null)
  }

  const editProfile = (profile: ExtendedProfile) => {
    // 当前人设可能在主表单中有新编辑，管理卡片应展示这份最新内容。
    setDraft(profile.name === selected?.name ? snapshot(values, profile.name) : { ...profile })
    setOriginalName(profile.name)
    setConfirmDelete(false)
  }

  const createProfile = () => {
    setDraft(snapshot(values, ''))
    setOriginalName(null)
    setConfirmDelete(false)
    setPanel('create')
  }

  const switchTo = (nextName: string) => {
    const target = profiles.find((profile) => profile.name === nextName)
    if (!target || nextName === selected?.name) return
    // 切换前把当前编辑写回原人设；未入库的内容自动留一份，避免第一次切换丢失。
    let nextProfiles: ExtendedProfile[]
    if (selected) {
      nextProfiles = profiles.map((profile) => profile.name === selected.name ? snapshot(values, selected.name) : profile)
    } else {
      let backupName = '切换前人设'
      let suffix = 2
      while (profiles.some((profile) => profile.name === backupName)) backupName = '切换前人设 ' + suffix++
      nextProfiles = [...profiles, snapshot(values, backupName)]
    }
    const { name: targetName, ...settings } = target
    const nextValues: Record<string, unknown> = {
      ...values,
      ...settings,
      owner_private_prompt: target.owner_private_prompt ?? '',
      st_preset_file: target.st_preset_file ?? '',
      persona_v2_card_id: target.persona_v2_card_id ?? '',
      mvu_enabled: target.mvu_enabled ?? true,
      profiles: nextProfiles,
      selected_profile: targetName,
    }
    if (target.nickname !== undefined) nextValues.nickname = target.nickname
    if (target.alias_names !== undefined) nextValues.alias_names = target.alias_names
    onChange(nextValues)
  }

  const saveProfile = () => {
    if (!draft) return
    const name = draft.name.trim()
    if (!name || profiles.some((profile) => profile.name === name && profile.name !== originalName)) {
      toast({ title: '请输入未使用的人设名称', variant: 'destructive' })
      return
    }
    if (!Number.isFinite(draft.multiple_probability) || draft.multiple_probability < 0 || draft.multiple_probability > 1) {
      toast({ title: '备用表达风格概率应在 0 到 1 之间', variant: 'destructive' })
      return
    }
    const cleanedAliases = Array.isArray(draft.alias_names)
      ? draft.alias_names.map((alias) => alias.trim()).filter(Boolean)
      : undefined
    const saved: ExtendedProfile = {
      ...draft,
      name,
      multiple_reply_style: draft.multiple_reply_style.filter((style) => style.trim()),
      ...(cleanedAliases !== undefined ? { alias_names: cleanedAliases } : {}),
    }
    const nextProfiles = originalName === null
      ? [...profiles, saved]
      : profiles.map((profile) => profile.name === originalName ? saved : profile)
    const { name: savedName, ...settings } = saved
    const renameSlot = (slotValue: unknown) => (
      originalName !== null && slotValue === originalName ? savedName : slotValue
    )
    const slotUpdates = {
      younger_slot_profile: renameSlot(values.younger_slot_profile),
      elder_slot_profile: renameSlot(values.elder_slot_profile),
    }
    // 修改正在使用的人设时同步主表单；编辑其他备选人设不改变当前生效内容。
    onChange(originalName !== null && originalName === selected?.name
      ? {
          ...values,
          ...settings,
          ...slotUpdates,
          owner_private_prompt: saved.owner_private_prompt ?? '',
          st_preset_file: saved.st_preset_file ?? '',
          persona_v2_card_id: saved.persona_v2_card_id ?? '',
          mvu_enabled: saved.mvu_enabled ?? true,
          profiles: nextProfiles,
          selected_profile: savedName,
        }
      : { ...values, ...slotUpdates, profiles: nextProfiles })
    closePanel()
  }

  const deleteProfile = () => {
    onChange({
      ...values,
      profiles: profiles.filter((profile) => profile.name !== originalName),
      selected_profile: originalName === selected?.name ? '' : values.selected_profile,
      younger_slot_profile: originalName === values.younger_slot_profile ? '' : values.younger_slot_profile,
      elder_slot_profile: originalName === values.elder_slot_profile ? '' : values.elder_slot_profile,
    })
    setDraft(null)
    setOriginalName(null)
    setConfirmDelete(false)
  }

  return (
    <>
      <div className="flex min-w-0 items-center gap-2">
        <Label htmlFor="personality-profile" className="shrink-0">当前人设</Label>
        <Select value={selected?.name ?? ''} onValueChange={switchTo}>
          <SelectTrigger id="personality-profile" className="min-w-0 flex-1">
            <SelectValue placeholder="当前人设（未命名）" />
          </SelectTrigger>
          <SelectContent>
            {profiles.length === 0 && <div className="px-2 py-3 text-sm text-muted-foreground">暂无备选人设，点击“新建”添加</div>}
            {profiles.map((profile) => <SelectItem key={profile.name} value={profile.name}>{profile.name}</SelectItem>)}
          </SelectContent>
        </Select>
        <Button type="button" size="sm" variant="outline" className="shrink-0" onClick={() => {
          setDraft(null); setOriginalName(null); setConfirmDelete(false); setImportText(''); setPanel('import')
        }}><Download className="mr-1 h-4 w-4" />导入</Button>
        <Button type="button" size="sm" className="shrink-0" onClick={createProfile}><Plus className="mr-1 h-4 w-4" />新建</Button>
        <Button type="button" size="icon" variant="outline" className="shrink-0" aria-label="管理人设" title="管理人设" onClick={() => {
          const first = selected ?? profiles[0]
          if (first) editProfile(first)
          else { setDraft(null); setOriginalName(null); setConfirmDelete(false) }
          setPanel('manage')
        }}><Wrench className="h-4 w-4" /></Button>
      </div>

      <div className="space-y-3 rounded-lg border border-border/70 bg-muted/20 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">双子人格控制台</span>
            <Badge variant={twinModeEnabled ? 'default' : 'secondary'} className="text-[11px]">
              {twinModeEnabled ? '已开启双子值班' : '单人格模式'}
            </Badge>
            {activeV2CardId && (
              <Badge variant="outline" className="font-mono text-[10px]">
                V2卡片: {activeV2CardId}
              </Badge>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2">
              <Label htmlFor="mvu-runtime-enabled" className="text-xs text-muted-foreground">
                启用 MVU 动态变量演进 (mvu_enabled)
              </Label>
              <Switch
                id="mvu-runtime-enabled"
                checked={mvuEnabled}
                onCheckedChange={(checked) => onChange({ ...values, mvu_enabled: checked })}
              />
            </div>
            <div className="flex items-center gap-2">
              <Label htmlFor="twin-mode-enabled" className="text-xs text-muted-foreground">
                启用双子人格切换 (twin_mode_enabled)
              </Label>
              <Switch
                id="twin-mode-enabled"
                checked={twinModeEnabled}
                onCheckedChange={(checked) => onChange({ ...values, twin_mode_enabled: checked })}
              />
            </div>
          </div>
        </div>

        <div className="grid gap-2.5 sm:grid-cols-2">
          <div className="space-y-1.5 rounded-md border border-border/60 bg-background/80 p-2.5">
            <div className="flex items-center justify-between gap-2">
              <Label htmlFor="twin-younger-slot" className="text-xs font-medium">
                🌸 妹妹主值班槽位
              </Label>
              {youngerSlotProfile && selected?.name === youngerSlotProfile && (
                <Badge variant="outline" className="text-[10px]">编辑中</Badge>
              )}
            </div>
            <div className="flex items-center gap-1.5">
              <Select
                value={youngerSlotProfile || SLOT_UNSET}
                onValueChange={(nextValue) => onChange({
                  ...values,
                  younger_slot_profile: nextValue === SLOT_UNSET ? '' : nextValue,
                })}
              >
                <SelectTrigger id="twin-younger-slot" className="h-8 min-w-0 flex-1 text-xs">
                  <SelectValue placeholder="跟随当前全局人设" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SLOT_UNSET}>跟随当前全局人设</SelectItem>
                  {profiles.map((profile) => (
                    <SelectItem key={'younger-' + profile.name} value={profile.name}>
                      {profile.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                type="button"
                size="sm"
                variant={youngerSlotProfile && selected?.name === youngerSlotProfile ? 'default' : 'outline'}
                className="h-8 shrink-0 px-2.5 text-xs"
                disabled={!youngerSlotProfile || !profiles.some((p) => p.name === youngerSlotProfile) || selected?.name === youngerSlotProfile}
                onClick={() => switchTo(youngerSlotProfile)}
              >
                切到妹妹编辑
              </Button>
            </div>
          </div>

          <div className="space-y-1.5 rounded-md border border-border/60 bg-background/80 p-2.5">
            <div className="flex items-center justify-between gap-2">
              <Label htmlFor="twin-elder-slot" className="text-xs font-medium">
                🌙 姐姐协同槽位
              </Label>
              {elderSlotProfile && selected?.name === elderSlotProfile && (
                <Badge variant="outline" className="text-[10px]">编辑中</Badge>
              )}
            </div>
            <div className="flex items-center gap-1.5">
              <Select
                value={elderSlotProfile || SLOT_UNSET}
                onValueChange={(nextValue) => onChange({
                  ...values,
                  elder_slot_profile: nextValue === SLOT_UNSET ? '' : nextValue,
                })}
              >
                <SelectTrigger id="twin-elder-slot" className="h-8 min-w-0 flex-1 text-xs">
                  <SelectValue placeholder="跟随当前全局人设" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SLOT_UNSET}>跟随当前全局人设</SelectItem>
                  {profiles.map((profile) => (
                    <SelectItem key={'elder-' + profile.name} value={profile.name}>
                      {profile.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                type="button"
                size="sm"
                variant={elderSlotProfile && selected?.name === elderSlotProfile ? 'default' : 'outline'}
                className="h-8 shrink-0 px-2.5 text-xs"
                disabled={!elderSlotProfile || !profiles.some((p) => p.name === elderSlotProfile) || selected?.name === elderSlotProfile}
                onClick={() => switchTo(elderSlotProfile)}
              >
                切到姐姐编辑
              </Button>
            </div>
          </div>
        </div>
      </div>

      <Dialog open={panel !== null} onOpenChange={(open) => { if (!open) closePanel() }}>
        <DialogContent className="[--dialog-width:44rem]">
          <DialogHeader>
            <DialogTitle>{panel === 'import' ? '导入人设' : panel === 'create' ? '新建人设' : '管理人设'}</DialogTitle>
            <DialogDescription>{panel === 'import' ? '粘贴 maibot.personality v1 JSON，解析后可继续编辑并确认保存。' : panel === 'create' ? '编辑完成后保存到人设库，可在下拉框中切换。' : '选择人设查看或修改，保存当前使用的人设会同步生效。'}</DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-4">
              {panel === 'import' && <div className="space-y-3">
                <div className="flex items-center justify-between gap-2">
                  <Label htmlFor="personality-import-text">人设 JSON</Label>
                  <Button type="button" size="sm" variant="outline" onClick={() => setImportText(PERSONALITY_IMPORT_EXAMPLE)}>填入格式示例</Button>
                </div>
                <Textarea id="personality-import-text" rows={12} value={importText} onChange={(event) => setImportText(event.target.value)} placeholder={PERSONALITY_IMPORT_EXAMPLE} />
                {imported.error && <p role="alert" className="text-sm text-destructive">{imported.error}</p>}
                {imported.profile && <div className="space-y-2 rounded-md border p-3">
                  <p className="font-medium">已解析：{imported.profile.name}</p>
                  <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">{imported.profile.personality}</p>
                  {profiles.some((profile) => profile.name === imported.profile?.name) && <p className="text-sm text-destructive">已有同名人设，请在下一步修改名称。</p>}
                </div>}
              </div>}
              {panel === 'manage' && <div className="flex flex-wrap gap-2">
                {profiles.map((profile) => <Button type="button" key={profile.name} size="sm" variant={originalName === profile.name ? 'default' : 'outline'} onClick={() => editProfile(profile)}>
                  {profile.name}{selected?.name === profile.name ? ' · 使用中' : ''}
                </Button>)}
                <Button type="button" size="sm" variant="outline" onClick={createProfile}><Plus className="mr-1 h-4 w-4" />新建</Button>
              </div>}
              {!draft && panel !== 'import' && <p className="py-6 text-center text-sm text-muted-foreground">暂无选中的人设，选择已有的人设或新建一套。</p>}
              {draft && <>
                <div className="space-y-2">
                  <Label htmlFor="profile-name">人设名称</Label>
                  <Input id="profile-name" placeholder="为这套人设起个名字" value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
                </div>
                {([
                  ['personality', '人格设定'], ['behavior_style', '行为风格'], ['reply_style', '表达风格'],
                  ['group_chat_prompt', '群聊提示词'], ['private_chat_prompts', '私聊提示词'],
                ] as const).map(([field, label]) => <div key={field} className="space-y-2">
                  <Label htmlFor={'profile-' + field}>{label}</Label>
                  <Textarea id={'profile-' + field} rows={3} value={draft[field]} onChange={(event) => setDraft({ ...draft, [field]: event.target.value })} />
                </div>)}
                <div className="space-y-2">
                  <Label htmlFor="profile-alternate">备用表达风格（每行一条）</Label>
                  <Textarea id="profile-alternate" rows={3} value={draft.multiple_reply_style.join('\n')} onChange={(event) => setDraft({ ...draft, multiple_reply_style: event.target.value.split('\n') })} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="profile-probability">备用表达风格触发概率</Label>
                  <Input id="profile-probability" type="number" min={0} max={1} step={0.1} value={Number.isNaN(draft.multiple_probability) ? '' : draft.multiple_probability} onChange={(event) => setDraft({ ...draft, multiple_probability: event.target.valueAsNumber })} />
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="profile-nickname">昵称 (可选，切换时同步 bot 昵称)</Label>
                    <Input id="profile-nickname" placeholder="如：许妍 / 姐姐" value={draft.nickname ?? ''} onChange={(event) => setDraft({ ...draft, nickname: event.target.value })} />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="profile-st-preset">专属酒馆预设文件名 (可选)</Label>
                    <Input id="profile-st-preset" placeholder="留空跟随全局 st_preset_engine" value={draft.st_preset_file ?? ''} onChange={(event) => setDraft({ ...draft, st_preset_file: event.target.value })} />
                  </div>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="profile-aliases">别名（每行一条，可选）</Label>
                  <Textarea id="profile-aliases" rows={2} value={(draft.alias_names ?? []).join('\n')} onChange={(event) => setDraft({ ...draft, alias_names: event.target.value.split('\n') })} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="profile-owner-private">Owner 专属私密提示词（仅限最高信任度私聊挂载）</Label>
                  <Textarea id="profile-owner-private" rows={3} placeholder="仅当与高信任度 Owner 一对一私聊时追加挂载，群聊绝不注入。" value={draft.owner_private_prompt ?? ''} onChange={(event) => setDraft({ ...draft, owner_private_prompt: event.target.value })} />
                </div>
              </>}
            </div>
          </DialogBody>
          {confirmDelete ? <div className="flex flex-wrap items-center justify-end gap-2">
            <p className="mr-auto text-sm">删除“{originalName}”？当前生效的设定会保留。</p>
            <Button type="button" variant="outline" onClick={() => setConfirmDelete(false)}>取消</Button>
            <Button type="button" variant="destructive" onClick={deleteProfile}>确认删除</Button>
          </div> : <div className="flex items-center justify-end gap-2">
            {originalName !== null && <Button type="button" variant="destructive" className="mr-auto" onClick={() => setConfirmDelete(true)}>删除人设</Button>}
            <Button type="button" variant="outline" onClick={closePanel}>取消</Button>
            {panel === 'import' && <Button type="button" disabled={!imported.profile} onClick={() => {
              if (!imported.profile) return
              setDraft(imported.profile); setOriginalName(null); setConfirmDelete(false); setPanel('create')
            }}>继续编辑</Button>}
            {draft && <Button type="button" onClick={saveProfile}>{panel === 'create' ? '创建人设' : '保存修改'}</Button>}
          </div>}
        </DialogContent>
      </Dialog>
    </>
  )
}
