import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox, type MessageBoxData } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { useAppStore } from '@/stores/app'
import type { PageResponse, ProxyRecord, ResourceQuery } from '@/types'
import SettingsView from './SettingsView.vue'

const TEST_API_KEY = 'TEST_ROXY_KEY_DO_NOT_LOG'

function proxy(id: string): ProxyRecord {
  return {
    id,
    host: `${id}.example.com`,
    port: 10000,
    username: `user-${id}`,
    password: `password-${id}`,
    enabled: true,
    status: 'unknown',
    latencyMs: null,
  lastCheckedAt: null,
    country: 'TR',
    group: '默认组',
    scheme: 'http',
  }
}

function proxyPage(items: ProxyRecord[], total: number, query: ResourceQuery): PageResponse<ProxyRecord> {
  return { items, total, page: query.page, pageSize: query.pageSize }
}

function buttonByText(wrapper: ReturnType<typeof mount>, text: string) {
  const button = wrapper.findAllComponents({ name: 'ElButton' }).find((item) => item.text().includes(text))
  if (!button) throw new Error(`Button not found: ${text}`)
  return button
}

function apiKeyInput(wrapper: ReturnType<typeof mount>) {
  return wrapper.get('input[placeholder="请输入 Roxy API Key"]')
}

describe('SettingsView Roxy execution settings', () => {
  it('groups the browser path, API key, port and headed mode in one local API card', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.settings.roxyApiKey = TEST_API_KEY
    vi.spyOn(store, 'refreshProxies').mockResolvedValue(proxyPage([], 0, store.proxyQuery))

    const wrapper = mount(SettingsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const apiCard = wrapper.get('.api-settings-card')
    expect(apiCard.text()).toContain('指纹浏览器本地API')
    expect(apiCard.text()).toContain('指纹浏览器地址')
    expect(apiCard.text()).toContain('Roxy API Key')
    expect(apiCard.text()).toContain('API 端口')
    expect(apiCard.find('.api-input-grid').exists()).toBe(true)
    expect(apiCard.find('.browser-path-field input').exists()).toBe(true)
    expect(apiCard.get('.headless-mode-label').text()).toBe('有头')
    expect(wrapper.text()).not.toContain('Roxy 本地 API')
    expect(wrapper.text()).not.toContain('ImRun Browser')
    expect(apiKeyInput(wrapper).attributes('type')).toBe('text')
    expect((apiKeyInput(wrapper).element as HTMLInputElement).value).toBe(TEST_API_KEY)
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('密钥已配置')
    const sideCards = wrapper.get('.config-side')
    expect(sideCards.findAllComponents({ name: 'StatCard' })).toHaveLength(2)
    expect(sideCards.text()).not.toContain('浏览器模式')
  })

  it('shows headless beside the switch and saves the headless flag', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    vi.spyOn(store, 'refreshProxies').mockResolvedValue(proxyPage([], 0, store.proxyQuery))
    const save = vi.spyOn(store, 'saveSettings').mockImplementation(async (input) => {
      store.settings = {
        schemaVersion: 2,
        browserExecutablePath: input.browserExecutablePath,
        roxyApiKey: input.roxyApiKey,
        roxyApiPort: input.roxyApiPort,
        headless: input.headless,
        requireRegistrationPassword: input.requireRegistrationPassword,
        enableRegistrationTotp: input.enableRegistrationTotp,
        proxyRetryCount: input.proxyRetryCount,
        concurrency: input.concurrency,
        taskTimeoutSeconds: input.taskTimeoutSeconds,
        updatedAt: new Date().toISOString(),
      }
      return store.settings
    })

    const wrapper = mount(SettingsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    await apiKeyInput(wrapper).setValue(TEST_API_KEY)
    const headless = wrapper.findAllComponents({ name: 'ElSwitch' })[0]
    expect(headless).toBeDefined()
    expect(headless!.props('inlinePrompt')).toBe(false)
    headless!.vm.$emit('update:modelValue', true)
    await nextTick()
    expect(wrapper.get('.headless-mode-label').text()).toBe('无头')
    await buttonByText(wrapper, '保存执行配置').trigger('click')
    await flushPromises()

    expect(save).toHaveBeenCalledWith(expect.objectContaining({
      roxyApiKey: TEST_API_KEY,
      roxyApiPort: 50000,
      headless: true,
      requireRegistrationPassword: false,
      enableRegistrationTotp: true,
      proxyRetryCount: 1,
    }))
    expect((apiKeyInput(wrapper).element as HTMLInputElement).value).toBe(TEST_API_KEY)
    expect(wrapper.text()).toContain('密钥已配置')
  })

  it('immediately saves password and 2FA switches independently and shows their state', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    vi.spyOn(store, 'refreshProxies').mockResolvedValue(proxyPage([], 0, store.proxyQuery))
    const save = vi.spyOn(store, 'saveSettings').mockImplementation(async (input) => {
      store.settings = {
        ...store.settings,
        ...input,
        updatedAt: new Date().toISOString(),
      }
      return store.settings
    })
    const wrapper = mount(SettingsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const passwordSwitch = wrapper.get('[aria-label="注册时设置密码"]')
    const totpSwitch = wrapper.get('[aria-label="注册时设置 2FA"]')
    const passwordItem = passwordSwitch.element.closest('.el-form-item')
    const totpItem = totpSwitch.element.closest('.el-form-item')
    expect(passwordItem?.textContent).toContain('已关闭')
    expect(totpItem?.textContent).toContain('已开启')
    await apiKeyInput(wrapper).setValue('UNSAVED_DRAFT_KEY')

    await passwordSwitch.setValue(true)
    await flushPromises()

    expect(save).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenLastCalledWith(expect.objectContaining({
      requireRegistrationPassword: true,
      enableRegistrationTotp: true,
    }))
    expect(passwordItem?.textContent).toContain('已开启')
    expect((apiKeyInput(wrapper).element as HTMLInputElement).value).toBe('UNSAVED_DRAFT_KEY')

    await totpSwitch.setValue(false)
    await flushPromises()

    expect(save).toHaveBeenCalledTimes(2)
    expect(save).toHaveBeenLastCalledWith(expect.objectContaining({
      requireRegistrationPassword: true,
      enableRegistrationTotp: false,
    }))
    expect(totpItem?.textContent).toContain('已关闭')
  })

  it('submits an empty API key when the user explicitly clears the field', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.settings.roxyApiKey = TEST_API_KEY
    vi.spyOn(store, 'refreshProxies').mockResolvedValue(proxyPage([], 0, store.proxyQuery))
    const save = vi.spyOn(store, 'saveSettings').mockResolvedValue(store.settings)
    const wrapper = mount(SettingsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    await apiKeyInput(wrapper).setValue('')

    await buttonByText(wrapper, '保存执行配置').trigger('click')
    await flushPromises()

    expect(save).toHaveBeenCalledOnce()
    expect(save.mock.calls[0]?.[0]).toHaveProperty('roxyApiKey', '')
    expect(wrapper.text()).toContain('等待配置')
  })

  it('shows and saves zero as an unlimited batch timeout', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.settings.taskTimeoutSeconds = 0
    vi.spyOn(store, 'refreshProxies').mockResolvedValue(proxyPage([], 0, store.proxyQuery))
    const save = vi.spyOn(store, 'saveSettings').mockResolvedValue(store.settings)

    const wrapper = mount(SettingsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const timeoutItem = wrapper
      .findAllComponents({ name: 'ElFormItem' })
      .find((item) => item.props('label') === '整批任务超时（秒）')
    expect(timeoutItem).toBeDefined()
    const timeoutInput = timeoutItem!.getComponent({ name: 'ElInputNumber' })
    expect(timeoutInput.props('min')).toBe(0)
    expect(wrapper.text()).toContain('0 表示不限时')
    expect(wrapper.get('.config-side').text()).toContain('无限制')
    timeoutInput.vm.$emit('update:modelValue', 600)
    await nextTick()
    expect(wrapper.get('.config-side').text()).toContain('600s')
    timeoutInput.vm.$emit('update:modelValue', 0)
    await nextTick()

    await buttonByText(wrapper, '保存执行配置').trigger('click')
    await flushPromises()

    expect(save).toHaveBeenCalledWith(expect.objectContaining({
      taskTimeoutSeconds: 0,
    }))
  })
})

describe('SettingsView proxy pool deletion', () => {
  it('shows group summaries instead of individual proxy credentials and toggles a whole group', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.proxyTotal = 11
    store.stats.proxies = { total: 11, enabled: 11, available: 0, quarantined: 0 }
    store.proxyGroups = [
      { country: 'TR', group: '住宅公式 A', total: 11, enabled: 11, available: 0, quarantined: 0, schemes: ['socks5'] },
    ]
    vi.spyOn(store, 'refreshProxies').mockResolvedValue(proxyPage([], 11, store.proxyQuery))
    vi.spyOn(store, 'refreshProxyCountries').mockResolvedValue([])
    vi.spyOn(store, 'refreshProxyGroups').mockResolvedValue(store.proxyGroups)
    const updateGroup = vi.spyOn(store, 'updateProxyGroup').mockResolvedValue({ matched: 11, modified: 11 })

    const wrapper = mount(SettingsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const table = wrapper.getComponent({ name: 'ElTable' })
    expect(table.text()).toContain('住宅公式 A')
    expect(table.text()).not.toContain('password-proxy')
    table.getComponent({ name: 'ElSwitch' }).vm.$emit('change', false)
    await flushPromises()
    expect(updateGroup).toHaveBeenCalledWith({ country: 'TR', group: '住宅公式 A', enabled: false })
  })

  it('requires confirmation before clearing the complete proxy pool', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    const item = proxy('proxy-1')
    store.proxies = [item]
    store.proxyTotal = 100
    store.stats.proxies = { total: 100, enabled: 100, available: 0, quarantined: 0 }
    vi.spyOn(store, 'refreshProxies').mockResolvedValue(proxyPage([item], 100, store.proxyQuery))
    const clearProxies = vi.spyOn(store, 'clearProxies').mockImplementation(async () => {
      store.proxies = []
      store.proxyTotal = 0
      store.stats.proxies = { total: 0, enabled: 0, available: 0, quarantined: 0 }
      return 100
    })
    const confirm = vi
      .spyOn(ElMessageBox, 'confirm')
      .mockResolvedValue('confirm' as MessageBoxData)

    const wrapper = mount(SettingsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    await buttonByText(wrapper, '清空代理池').trigger('click')
    await flushPromises()

    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining('全部 100 条代理'),
      '清空代理池',
      expect.objectContaining({ confirmButtonText: '清空全部', type: 'error' }),
    )
    expect(clearProxies).toHaveBeenCalledOnce()
    expect(buttonByText(wrapper, '清空代理池').attributes('disabled')).toBeDefined()
  })

  it('does not delete when the confirmation is cancelled', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    const item = proxy('proxy-1')
    store.proxies = [item]
    store.proxyTotal = 1
    store.stats.proxies = { total: 1, enabled: 1, available: 0, quarantined: 0 }
    vi.spyOn(store, 'refreshProxies').mockResolvedValue(proxyPage([item], 1, store.proxyQuery))
    const clearProxies = vi.spyOn(store, 'clearProxies')
    vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')

    const wrapper = mount(SettingsView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    await buttonByText(wrapper, '清空代理池').trigger('click')
    await flushPromises()

    expect(clearProxies).not.toHaveBeenCalled()
  })
})
