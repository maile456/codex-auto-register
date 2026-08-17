import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import ExportDialog from '@/components/ExportDialog.vue'
import AccessTokenGroupsDialog from '@/components/AccessTokenGroupsDialog.vue'
import { dataGateway } from '@/services/dataGateway'
import * as exporter from '@/services/exporter'
import { useAppStore } from '@/stores/app'
import type { AccountRecord } from '@/types'
import AccountsView from './AccountsView.vue'

function account(overrides: Partial<AccountRecord> = {}): AccountRecord {
  return {
    id: 'account-valid',
    email: 'valid@example.test',
    chatgptPassword: '',
    totpSecret: '',
    emailAccessUrl: 'https://mail.example.test/inbox',
    createdAt: '2026-08-11T00:00:00.000Z',
    accountType: 'free',
    phoneBound: null,
    promotionEligible: null,
    accessTokenConfigured: true,
    accessTokenExpiresAt: '2099-08-11T01:00:00.000Z',
    accessTokenUpdatedAt: '2026-08-11T00:10:00.000Z',
    ...overrides,
  }
}

describe('AccountsView AccessToken controls', () => {
  it('labels the registration country and keeps legacy accounts identifiable', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [
      account({ registrationCountry: 'TR' }),
      account({ id: 'legacy-account', email: 'legacy@example.test', registrationCountry: null }),
    ]
    store.accountTotal = 2
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 2,
      page: 1,
      pageSize: 10,
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('土耳其 · TR')
    expect(wrapper.text()).toContain('历史账号')
  })

  it('opens the mailbox URL from the account actions', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account({ checkoutType: 'oaics' })]
    store.accountTotal = 1
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('OAICS')
    await wrapper.get('button[aria-label="打开接码 URL"]').trigger('click')
    expect(open).toHaveBeenCalledWith(
      'https://mail.example.test/inbox',
      '_blank',
      'noopener,noreferrer',
    )
    wrapper.unmount()
  })

  it('shows AT status and opens a locked single-token export without exposing AT', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [
      account(),
      account({
        id: 'account-missing',
        email: 'missing@example.test',
        accessTokenConfigured: false,
        accessTokenExpiresAt: null,
        accessTokenUpdatedAt: null,
      }),
    ]
    store.accountTotal = 2
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 2,
      page: 1,
      pageSize: 10,
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('AT 状态')
    expect(wrapper.text()).toContain('已提取')
    expect(wrapper.text()).toContain('未提取')
    expect(wrapper.text()).toContain('提取选中 AT')
    expect(wrapper.text()).not.toContain('accessToken')

    const extractButtons = wrapper
      .findAll('button')
      .filter((button) => button.text().trim() === '提取AT')
    expect(extractButtons).toHaveLength(2)
    expect(extractButtons[0]!.attributes('disabled')).toBeUndefined()
    expect(extractButtons[1]!.attributes('disabled')).toBeDefined()
    await extractButtons[0]!.trigger('click')

    const dialog = wrapper.getComponent(ExportDialog)
    expect(dialog.props('scope')).toBe('single')
    expect(dialog.props('ids')).toEqual(['account-valid'])
    expect(dialog.props('initialFormat')).toBe('access-tokens')
    expect(dialog.props('formatLocked')).toBe(true)
  })

  it('queries a single account and distinguishes unknown promotion status', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account()]
    store.accountTotal = 1
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })
    const query = vi.spyOn(store, 'checkAccountPromotions').mockResolvedValue({
      requested: 1,
      succeeded: 1,
      failed: 0,
      skipped: 0,
      items: [{ id: 'account-valid', status: 'success', errorCode: null }],
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('未查询')
    expect(wrapper.text()).not.toContain('不可试用')
    const button = wrapper.get('button[aria-label="查询优惠资格"]')
    await button.trigger('click')
    await flushPromises()

    expect(query).toHaveBeenCalledWith(['account-valid'])
  })

  it('queries selected accounts through the shared batch action', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account()]
    store.accountTotal = 1
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })
    const query = vi.spyOn(store, 'checkAccountPromotions').mockResolvedValue({
      requested: 1,
      succeeded: 1,
      failed: 0,
      skipped: 0,
      items: [{ id: 'account-valid', status: 'success', errorCode: null }],
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    wrapper.findComponent({ name: 'ElTable' }).vm.$emit(
      'select',
      [store.accounts[0]],
      store.accounts[0],
    )
    await flushPromises()
    const batchButton = wrapper
      .findAll('button')
      .find((candidate) => candidate.text().includes('查询选中优惠'))
    expect(batchButton).toBeDefined()
    await batchButton!.trigger('click')
    await flushPromises()

    expect(query).toHaveBeenCalledWith(['account-valid'])
  })

  it('filters the complete account pool by the untried Plus label', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account({ promotionEligible: true })]
    store.accountTotal = 1
    const refresh = vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const filter = wrapper
      .findAllComponents({ name: 'ElSelect' })
      .find((item) => item.props('placeholder') === '筛选 Plus 标签')
    expect(filter).toBeDefined()
    filter!.vm.$emit('change', 'untried_plus')
    await flushPromises()

    expect(refresh).toHaveBeenLastCalledWith(expect.objectContaining({
      promotion: 'untried_plus',
    }))
  })

  it('opens the ten-per-group AT copier from the account toolbar', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account()]
    store.accountTotal = 1
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 1,
      page: 1,
      pageSize: 10,
    })
    const exportAll = vi.spyOn(dataGateway, 'exportAccounts').mockResolvedValue({
      content: 'AT_ONE\nAT_TWO',
      filename: 'accounts-2-access-tokens.txt',
      count: 2,
      format: 'access-tokens',
      skippedMissingCount: 0,
      skippedExpiredCount: 0,
    })

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const groupButton = wrapper
      .findAll('button')
      .find((candidate) => candidate.text().includes('AT 分组复制'))
    expect(groupButton).toBeDefined()
    await groupButton!.trigger('click')
    await flushPromises()

    expect(wrapper.getComponent(AccessTokenGroupsDialog).props('modelValue')).toBe(true)
    expect(exportAll).toHaveBeenCalledWith('access-tokens', 'all', [])
  })

  it('copies the configured number of valid ATs with one click', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.accounts = [account()]
    store.accountTotal = 120
    vi.spyOn(store, 'refreshAccounts').mockResolvedValue({
      items: store.accounts,
      total: 120,
      page: 1,
      pageSize: 10,
    })
    const tokens = Array.from({ length: 120 }, (_, index) => `AT_${index + 1}`)
    vi.spyOn(dataGateway, 'exportAccounts').mockResolvedValue({
      content: tokens.join('\n'),
      filename: 'accounts-120-access-tokens.txt',
      count: 120,
      format: 'access-tokens',
      skippedMissingCount: 0,
      skippedExpiredCount: 0,
    })
    const copy = vi.spyOn(exporter, 'copyText').mockResolvedValue(undefined)

    const wrapper = mount(AccountsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const limitInput = wrapper.findAllComponents({ name: 'ElInputNumber' }).at(-1)
    expect(limitInput).toBeDefined()
    limitInput!.vm.$emit('update:modelValue', 37)
    await flushPromises()

    const button = wrapper
      .findAll('button')
      .find((candidate) => candidate.text().includes('一键复制前 37 个 AT'))
    expect(button).toBeDefined()
    await button!.trigger('click')
    await flushPromises()

    expect(dataGateway.exportAccounts).toHaveBeenCalledWith('access-tokens', 'all', [])
    expect(copy).toHaveBeenCalledWith(tokens.slice(0, 37).join('\n'))
  })
})
