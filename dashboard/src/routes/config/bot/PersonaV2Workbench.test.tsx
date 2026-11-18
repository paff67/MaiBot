import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as configApi from '@/lib/config-api'
import { backendApi } from '@/lib/http'
import { createDefaultPersonaV2Card, type PersonaV2Card } from '@/lib/persona-v2-api'

import { PersonaV2Workbench } from './PersonaV2Workbench'

const toastMock = vi.fn()

vi.mock('@/hooks/use-toast', () => ({
  useToast: () => ({ toast: toastMock }),
}))

function buildTwinTestCard(): PersonaV2Card {
  const base = createDefaultPersonaV2Card({
    card_id: 'maibot_xuyan',
    name: '许妍',
    aliases: ['妍妍', '小妍', '妹妹'],
    role_tag: '21岁 · 东大具身智能修士 / 妹妹',
  })
  base.core_persona.soul_pitch = '及腰浅樱粉长发、白兰花香气的21岁东大极客少女。'
  base.core_persona.speech_rules.negative_constraints = ['不要用亲爱的客服腔', '禁用译制片敬语']
  base.sample_corpora = [
    {
      id: 'xuyan_echo_01',
      category: 'daily_echo',
      situation_title: '清晨琴房试音',
      tier_label: '日常态',
      action_and_posture: '抱琴微笑抬头。',
      sample_lines: ['早上好呀金主。', '刚调完A弦音准。'],
      trigger_keywords: ['练琴', '早上'],
      mvu_condition: '',
      scope: 'general',
    },
  ]
  base.past_experiences = [
    {
      id: 'xuyan_exp_01',
      title: '根津老街买抹茶',
      period_or_scene: '东京日常',
      category: 'life_vignette',
      narrative: '走6分钟去根津买宇治抹茶蒙布朗。',
      usage_note_or_monologue: '提到甜品时自然带出。',
      constant: false,
      trigger_keywords: ['抹茶', '根津'],
      scope: 'general',
    },
  ]
  base.mvu_config.shared_namespace = 'twin_xu'
  base.mvu_config.update_instruction = '每轮后自然更新 affection 与 intimacy_tension。'
  base.mvu_config.affinity_guard.cap_before_lock = 100
  base.mvu_config.variables = [
    {
      key: 'affection',
      label: '亲密倾心度',
      type: 'number',
      default_value: 25,
      min: 0,
      max: 120,
      enum_values: [],
      monotonic_true: false,
      visibility: 'PRIVATE',
      description: '主观倾心度 0~120',
      shared: false,
      shared_key: '',
    },
    {
      key: 'love_locked',
      label: '双生破局锁',
      type: 'boolean',
      default_value: false,
      min: null,
      max: null,
      enum_values: [],
      monotonic_true: true,
      visibility: 'PRIVATE',
      description: '跨卡共享锁',
      shared: true,
      shared_key: 'twin_harmony_locked',
    },
    {
      key: 'intimacy_tension',
      label: '当次私聊唤起温度',
      type: 'number',
      default_value: 15,
      min: 0,
      max: 100,
      enum_values: [],
      monotonic_true: false,
      visibility: 'PRIVATE',
      description: '短期亲密张力',
      shared: false,
      shared_key: '',
    },
    {
      key: 'sister_rivalry_mode',
      label: '双子双向博弈姿态',
      type: 'string',
      default_value: '橘势亲昵·有来有回',
      min: null,
      max: null,
      enum_values: ['橘势亲昵·有来有回', '沙发反扑·按姐家法'],
      monotonic_true: false,
      visibility: 'PRIVATE',
      description: '双子互动姿态',
      shared: false,
      shared_key: '',
    },
    {
      key: 'apartment_scene',
      label: '池之端15F实时切片',
      type: 'string',
      default_value: '东京傍晚微凉，客厅水冷机柜低频运转。',
      min: null,
      max: null,
      enum_values: [],
      monotonic_true: false,
      visibility: 'PUBLIC',
      description: '公寓跨卡共享环境',
      shared: true,
      shared_key: 'apartment_scene',
    },
  ]
  return base
}

describe('PersonaV2Workbench', () => {
  let cardStore: PersonaV2Card
  let mvuStore: Record<string, unknown>

  beforeEach(() => {
    cardStore = buildTwinTestCard()
    mvuStore = {
      affection: 26,
      love_locked: false,
      intimacy_tension: 15,
      sister_rivalry_mode: '橘势亲昵·有来有回',
      apartment_scene: '东京傍晚微凉，客厅水冷机柜低频运转。',
    }

    vi.spyOn(backendApi, 'get').mockImplementation(async (path: string) => {
      if (path === '/api/webui/persona-v2/cards') {
        return {
          cards: [
            {
              card_id: cardStore.card_id,
              name: cardStore.name,
              role_tag: cardStore.role_tag,
              soul_pitch: cardStore.core_persona.soul_pitch,
              avatar_url: '',
              aliases: cardStore.aliases,
              experience_count: cardStore.past_experiences.length,
              corpus_count: cardStore.sample_corpora.length,
              stage_count: cardStore.relationship_stages.length,
              bound_targets: ['active', 'sister_younger'],
              card: cardStore,
            },
          ],
          bindings: {
            active: 'maibot_xuyan',
            sister_younger: 'maibot_xuyan',
            sister_elder: 'maibot_xuyue',
          },
        }
      }
      if (path === '/api/webui/persona-v2/cards/maibot_xuyan') {
        return {
          card: cardStore,
          mvu_state: mvuStore,
          active_stage: null,
          bound_targets: ['active', 'sister_younger'],
        }
      }
      throw new Error(`Unexpected GET ${path}`)
    })

    vi.spyOn(backendApi, 'put').mockImplementation(async (path: string, options) => {
      if (path === '/api/webui/persona-v2/cards/maibot_xuyan') {
        cardStore = options?.body as PersonaV2Card
        return {
          card: cardStore,
          mvu_state: mvuStore,
          active_stage: null,
          bound_targets: ['active', 'sister_younger'],
        }
      }
      if (path === '/api/webui/persona-v2/cards/maibot_xuyan/mvu-state') {
        const values = (options?.body as { values: Record<string, unknown> })?.values ?? {}
        mvuStore = { ...mvuStore, ...values }
        return {
          card_id: 'maibot_xuyan',
          mvu_state: mvuStore,
          active_stage: null,
        }
      }
      throw new Error(`Unexpected PUT ${path}`)
    })
  })

  afterEach(() => {
    cleanup()
  })

  it('supports adding comma/newline list items without swallowing separators, and preserves edits across tabs', async () => {
    const user = userEvent.setup()
    const invalidateSpy = vi.spyOn(configApi, 'invalidateBotConfigCache')
    const reloadConfig = vi.fn().mockResolvedValue(true)

    render(
      <PersonaV2Workbench
        personalityValues={{ selected_profile: '许妍', persona_v2_card_id: 'maibot_xuyan' }}
        onReloadConfig={reloadConfig}
      />
    )

    const aliasesInput = (await screen.findByLabelText(/触发别名/)) as HTMLInputElement
    expect(aliasesInput.value).toBe('妍妍, 小妍, 妹妹')

    // Trailing comma + space must NOT be stripped while focused
    aliasesInput.focus()
    fireEvent.change(aliasesInput, { target: { value: '妍妍, 小妍, 妹妹, ' } })
    expect(aliasesInput.value).toBe('妍妍, 小妍, 妹妹, ')

    fireEvent.change(aliasesInput, { target: { value: '妍妍, 小妍, 妹妹, 阿妍' } })
    fireEvent.blur(aliasesInput)
    expect(aliasesInput.value).toBe('妍妍, 小妍, 妹妹, 阿妍')

    // Switch to Tab 2 and verify Enter / newline addition in negative_constraints & corpora
    await user.click(screen.getByRole('tab', { name: /2\. 语言规范与示例语料库/ }))
    const negBox = screen.getByPlaceholderText(/严禁使用的词汇/) as HTMLTextAreaElement
    negBox.focus()
    fireEvent.change(negBox, { target: { value: '不要用亲爱的客服腔\n禁用译制片敬语\n' } })
    expect(negBox.value).toBe('不要用亲爱的客服腔\n禁用译制片敬语\n')
    fireEvent.change(negBox, { target: { value: '不要用亲爱的客服腔\n禁用译制片敬语\n禁止套路安慰' } })
    fireEvent.blur(negBox)

    const corpusKwInput = screen.getByPlaceholderText(/如：加班, 熬夜/) as HTMLInputElement
    corpusKwInput.focus()
    fireEvent.change(corpusKwInput, { target: { value: '练琴, 早上, ' } })
    expect(corpusKwInput.value).toBe('练琴, 早上, ')
    fireEvent.change(corpusKwInput, { target: { value: '练琴, 早上, 午茶' } })
    fireEvent.blur(corpusKwInput)

    // Add a new corpus entry
    await user.click(screen.getByRole('button', { name: /新增示例语料组/ }))
    expect(screen.getByRole('tab', { name: /2\. 语言规范与示例语料库 \(2\)/ })).toBeInTheDocument()

    // Switch to Tab 3 and test adding experience & keywords
    await user.click(screen.getByRole('tab', { name: /3\. 过往经历与生活切片/ }))
    const expKwInput = screen.getByPlaceholderText(/如：旧照片, 药箱/) as HTMLInputElement
    expKwInput.focus()
    fireEvent.change(expKwInput, { target: { value: '抹茶, 根津, 晴空塔' } })
    fireEvent.blur(expKwInput)

    await user.click(screen.getAllByRole('button', { name: /保存当前人物卡片修改/ })[0])

    await waitFor(() => {
      expect(backendApi.put).toHaveBeenCalledWith(
        '/api/webui/persona-v2/cards/maibot_xuyan',
        expect.objectContaining({
          body: expect.objectContaining({
            aliases: ['妍妍', '小妍', '妹妹', '阿妍'],
          }),
        })
      )
    })
    expect(cardStore.core_persona.speech_rules.negative_constraints).toEqual([
      '不要用亲爱的客服腔',
      '禁用译制片敬语',
      '禁止套路安慰',
    ])
    expect(cardStore.sample_corpora.some((c) => c.trigger_keywords.includes('午茶'))).toBe(true)
    expect(cardStore.past_experiences[0].trigger_keywords).toEqual(['抹茶', '根津', '晴空塔'])
    expect(invalidateSpy).toHaveBeenCalled()
    expect(reloadConfig).toHaveBeenCalled()
  })

  it('renders dynamic MVU variables (0~120) and keeps input focus while editing MVU variable key & numbers', async () => {
    const user = userEvent.setup()

    render(
      <PersonaV2Workbench
        personalityValues={{ selected_profile: '许妍', persona_v2_card_id: 'maibot_xuyan' }}
      />
    )

    await user.click(await screen.findByRole('tab', { name: /4\. MVU 动态变量与分阶段演进/ }))

    // Custom variable from card schema renders in Live Console and supports values up to 120
    const affectionInput = screen.getByLabelText('亲密倾心度') as HTMLInputElement
    const tensionInput = screen.getByLabelText('当次私聊唤起温度') as HTMLInputElement
    expect(affectionInput.value).toBe('26')
    expect(tensionInput.value).toBe('15')

    await user.clear(affectionInput)
    await user.type(affectionInput, '108')
    await user.clear(tensionInput)
    await user.type(tensionInput, '65')
    await user.click(screen.getByRole('button', { name: /保存实时变量值/ }))

    await waitFor(() => {
      expect(backendApi.put).toHaveBeenCalledWith(
        '/api/webui/persona-v2/cards/maibot_xuyan/mvu-state',
        expect.objectContaining({
          body: {
            values: expect.objectContaining({
              affection: 108,
              intimacy_tension: 65,
            }),
          },
        })
      )
    })

    // Editing variable.key must NOT unmount the input or drop focus mid-typing
    const keyInput = screen.getByLabelText('变量键 3') as HTMLInputElement
    keyInput.focus()
    expect(document.activeElement).toBe(keyInput)
    await user.type(keyInput, '_v2')
    expect(document.activeElement).toBe(keyInput)
    expect(keyInput.value).toBe('intimacy_tension_v2')

    // Numeric default & guard delta inputs accept negative / decimal typing smoothly
    const minDeltaInput = screen.getByDisplayValue('-1') as HTMLInputElement
    await user.clear(minDeltaInput)
    await user.type(minDeltaInput, '-1.5')
    fireEvent.blur(minDeltaInput)
    expect(minDeltaInput.value).toBe('-1.5')
  })
})
