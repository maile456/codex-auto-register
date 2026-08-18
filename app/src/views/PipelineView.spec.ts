import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import PipelineView from './PipelineView.vue'
import { dataGateway } from '@/services/dataGateway'
import router from '@/router'
import type { PipelineItem } from '@/types'

vi.mock('@/services/dataGateway', () => ({
  dataGateway: {
    pipelineSettings: vi.fn(),
    listPipeline: vi.fn(),
    pipelineLogs: vi.fn(),
    syncPipeline: vi.fn(),
    updatePipelineSettings: vi.fn(),
    extractPipeline: vi.fn(),
    testHeroSms: vi.fn(),
    startPipelinePayment: vi.fn(),
    submitPipelineOtp: vi.fn(),
    retryPipeline: vi.fn(),
    deletePipeline: vi.fn(),
    listProxyCountries: vi.fn(),
    listProxyGroups: vi.fn(),
    paymentExtractorDefaults: vi.fn(),
  },
}))

function item(overrides: Partial<PipelineItem> = {}): PipelineItem {
  return {
    id: 'pipeline-1',
    accountId: 'account-1',
    email: 'pipeline@example.test',
    chatgptPassword: 'FIXTURE_PASSWORD',
    totpSecret: 'FIXTURE_TOTP',
    emailAccessUrl: 'https://mail.example.test/inbox',
    accountCreatedAt: '2026-08-15T00:00:00Z',
    accountType: 'free',
    promotionEligible: true,
    accessTokenConfigured: true,
    accessTokenExpiresAt: '2026-08-16T00:00:00Z',
    stage: 'eligible',
    extractionStatus: 'pending',
    extractionRetryCount: 0,
    paymentLink: null,
    paymentLinkConfigured: false,
    paymentStatus: 'pending',
    paymentRetryCount: 0,
    heroSmsManaged: false,
    heroSmsAttempt: 0,
    exportCount: 0,
    mailConfirmationStatus: 'unchecked',
    createdAt: '2026-08-15T00:00:00Z',
    updatedAt: '2026-08-15T00:00:00Z',
    ...overrides,
  }
}

const settings = {
  enabled: false,
  extractionConcurrency: 1,
  paymentConcurrency: 1,
  extractionFailureRetries: 0,
  paymentFailureRetries: 0,
  country: 'JP' as const,
  checkoutProxy: 'http://checkout.example:8080',
  updateProxy: 'http://update.example:8080',
  protocolProxy: 'http://protocol.example:8080',
  checkoutProxyCountry: '',
  updateProxyCountry: '',
  protocolProxyCountry: '',
  checkoutProxyGroup: '',
  updateProxyGroup: '',
  protocolProxyGroup: '',
  applyCheckoutUpdate: true,
  heroSmsEnabled: false,
  autoPaymentEnabled: false,
  heroSmsMaxPrice: 1,
  heroSmsChangeNumberRetries: 2,
  heroSmsNumberWaitSeconds: 120,
  heroSmsCountryId: 182,
  agreementAutoSmsEnabled: false,
  heroSmsApiKeyConfigured: true,
}

describe('PipelineView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(dataGateway.pipelineSettings).mockResolvedValue(settings)
    vi.mocked(dataGateway.listPipeline).mockResolvedValue({
      items: [item(), item({ id: 'pipeline-paid', stage: 'paid', extractionStatus: 'succeeded', paymentStatus: 'completed', checkoutType: 'cs' })],
      total: 2,
      page: 1,
      pageSize: 20,
      counts: { eligible: 1, paid: 1 },
    })
    vi.mocked(dataGateway.extractPipeline).mockResolvedValue({ requested: 1, started: 1 })
    vi.mocked(dataGateway.pipelineLogs).mockResolvedValue({
      itemId: 'pipeline-1',
      email: 'pipeline@example.test',
      stage: 'payment_failed',
      logs: [{
        id: 'log-1',
        timestamp: '2026-08-18T00:00:00Z',
        level: 'error',
        event: 'payment.failed',
        message: '协议代理池超过 500 条上限',
        code: 'protocol_request_failed',
        details: { proxyCount: 1100 },
      }],
    })
    vi.mocked(dataGateway.listProxyCountries).mockResolvedValue([
      { country: 'TR', total: 10, enabled: 8 },
      { country: 'JP', total: 5, enabled: 5 },
    ])
    vi.mocked(dataGateway.listProxyGroups).mockResolvedValue([
      { country: 'TR', group: 'TR-A', total: 10, enabled: 8, available: 8, quarantined: 0, schemes: ['http'] },
      { country: 'JP', group: 'JP-A', total: 5, enabled: 5, available: 5, quarantined: 0, schemes: ['http'] },
    ])
    vi.mocked(dataGateway.paymentExtractorDefaults).mockResolvedValue({
      country: 'JP',
      forceCountry: '',
      paymentMethod: 'paypal',
      checkoutProxy: '',
      updateProxy: '',
      applyCheckoutUpdate: true,
      concurrency: 1,
      maxConcurrency: 10,
      countries: [
        { value: 'JP', label: '日本 · JPY', currency: 'JPY' },
        { value: 'DE', label: '德国 · EUR', currency: 'EUR' },
      ],
      paymentMethods: [{ value: 'paypal', label: 'PayPal' }],
    })
    vi.mocked(dataGateway.submitPipelineOtp).mockResolvedValue(item({ stage: 'paying' }))
    vi.mocked(dataGateway.testHeroSms).mockResolvedValue({
      ok: true, configured: true, countryId: 182, service: 'PayPal', balance: 12.34,
    })
  })

  it('shows account details and submits only an eligible row for extraction', async () => {
    const wrapper = mount(PipelineView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus, router] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('pipeline@example.test')
    expect(wrapper.text()).toContain('Plus 试用可用')
    expect(wrapper.text()).toContain('CS')
    const extractButton = wrapper.findAll('button').find((button) => button.text() === '提链')
    expect(extractButton).toBeDefined()
    await extractButton!.trigger('click')
    await flushPromises()

    expect(dataGateway.extractPipeline).toHaveBeenCalledWith(['pipeline-1'])
    wrapper.unmount()
  })

  it('loads a redacted timeline only when the row log button is clicked', async () => {
    const wrapper = mount(PipelineView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus, router] },
    })
    await flushPromises()

    expect(dataGateway.pipelineLogs).not.toHaveBeenCalled()
    const logButton = wrapper.findAll('button').find((button) => button.text() === '日志')
    await logButton!.trigger('click')
    await flushPromises()

    expect(dataGateway.pipelineLogs).toHaveBeenCalledWith('pipeline-1')
    expect(document.body.textContent).toContain('协议代理池超过 500 条上限')
    expect(document.body.textContent).toContain('protocol_request_failed')
    expect(document.body.textContent).toContain('proxyCount：1100')
    wrapper.unmount()
  })

  it('offers a manual re-extract action for payment failures', async () => {
    vi.mocked(dataGateway.listPipeline).mockResolvedValue({
      items: [item({
        stage: 'payment_failed',
        extractionStatus: 'succeeded',
        paymentStatus: 'failed',
        paymentError: { code: 'protocol_request_failed', message: 'stale link' },
        paymentLink: 'https://checkout.example.test/pay/stale',
        paymentLinkConfigured: true,
      })],
      total: 1,
      page: 1,
      pageSize: 20,
      counts: { payment_failed: 1 },
    })
    const wrapper = mount(PipelineView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus, router] },
    })
    await flushPromises()

    const reextractButton = wrapper.findAll('button').find((button) => button.text() === '重新提炼')
    expect(reextractButton).toBeDefined()
    await reextractButton!.trigger('click')
    await flushPromises()

    expect(dataGateway.retryPipeline).toHaveBeenCalledWith('pipeline-1', 'extraction')
    expect(dataGateway.extractPipeline).toHaveBeenCalledWith(['pipeline-1'])
    wrapper.unmount()
  })

  it('offers a manual re-extract action for an expired link in payment', async () => {
    vi.mocked(dataGateway.listPipeline).mockResolvedValue({
      items: [item({
        stage: 'payment_waiting_otp',
        extractionStatus: 'succeeded',
        paymentStatus: 'awaiting_otp',
        paymentLink: 'https://checkout.example.test/pay/expired',
        paymentLinkConfigured: true,
        paymentLinkExpiresAt: new Date(Date.now() - 60_000).toISOString(),
        paymentLinkExpired: true,
      })],
      total: 1,
      page: 1,
      pageSize: 20,
      counts: { payment_waiting_otp: 1 },
    })
    const wrapper = mount(PipelineView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus, router] },
    })
    await flushPromises()

    const reextractButton = wrapper.findAll('button').find((button) => button.text() === '重新提炼')
    expect(reextractButton).toBeDefined()
    await reextractButton!.trigger('click')
    await flushPromises()

    expect(dataGateway.retryPipeline).toHaveBeenCalledWith('pipeline-1', 'extraction')
    expect(dataGateway.extractPipeline).toHaveBeenCalledWith(['pipeline-1'])
    wrapper.unmount()
  })

  it('shows the remaining lifetime for a fresh PayPal link', async () => {
    vi.mocked(dataGateway.listPipeline).mockResolvedValue({
      items: [item({
        stage: 'payment_ready',
        extractionStatus: 'succeeded',
        paymentLink: 'https://checkout.example.test/pay/fixture',
        paymentLinkConfigured: true,
        paymentLinkExpiresAt: new Date(Date.now() + 10 * 60_000).toISOString(),
        paymentLinkExpired: false,
        paymentLinkRemainingSeconds: 600,
      })],
      total: 1,
      page: 1,
      pageSize: 20,
      counts: { payment_ready: 1 },
    })
    const wrapper = mount(PipelineView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus, router] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('链接剩余')
    wrapper.unmount()
  })

  it('submits a manual PP otp from a waiting record', async () => {
    vi.mocked(dataGateway.listPipeline).mockResolvedValue({
      items: [item({ stage: 'payment_waiting_otp', extractionStatus: 'succeeded', paymentStatus: 'awaiting_otp' })],
      total: 1,
      page: 1,
      pageSize: 20,
      counts: { payment_waiting_otp: 1 },
    })
    const wrapper = mount(PipelineView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus, router] },
    })
    await flushPromises()

    const otpButton = wrapper.findAll('button').find((button) => button.text() === '验证码')
    await otpButton!.trigger('click')
    await flushPromises()
    const input = document.body.querySelector<HTMLInputElement>('input[placeholder="6 位验证码"]')
    expect(input).not.toBeNull()
    input!.value = '123456'
    input!.dispatchEvent(new Event('input'))
    const submit = Array.from(document.body.querySelectorAll('button')).find((button) => button.textContent?.includes('提交验证码'))
    submit?.click()
    await flushPromises()

    expect(dataGateway.submitPipelineOtp).toHaveBeenCalledWith('pipeline-1', '123456')
    wrapper.unmount()
  })

  it('shows an extracted payment link that can be opened', async () => {
    const paymentLink = 'https://checkout.example.test/pay/fixture'
    vi.mocked(dataGateway.listPipeline).mockResolvedValue({
      items: [item({
        stage: 'payment_ready',
        extractionStatus: 'succeeded',
        paymentLink,
        paymentLinkConfigured: true,
      })],
      total: 1,
      page: 1,
      pageSize: 20,
      counts: { payment_ready: 1 },
    })
    const wrapper = mount(PipelineView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus, router] },
    })
    await flushPromises()

    const link = wrapper.get<HTMLAnchorElement>('.payment-link-text')
    expect(link.text()).toBe(paymentLink)
    expect(link.attributes('href')).toBe(paymentLink)
    expect(link.attributes('target')).toBe('_blank')
    wrapper.unmount()
  })

  it('does not render a non-http payment link as clickable', async () => {
    vi.mocked(dataGateway.listPipeline).mockResolvedValue({
      items: [item({
        stage: 'payment_ready',
        extractionStatus: 'succeeded',
        paymentLink: 'javascript:fixture',
        paymentLinkConfigured: true,
      })],
      total: 1,
      page: 1,
      pageSize: 20,
      counts: { payment_ready: 1 },
    })
    const wrapper = mount(PipelineView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus, router] },
    })
    await flushPromises()

    expect(wrapper.find('.payment-link-text').exists()).toBe(false)
    wrapper.unmount()
  })

  it('selects configured country proxy pools and links to shared HeroSMS settings', async () => {
    const wrapper = mount(PipelineView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus, router] },
    })
    await flushPromises()

    const configButton = wrapper.findAll('button').find((button) => button.text().includes('流水线配置'))
    await configButton!.trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('HeroSMS 自动接码')
    expect(document.body.textContent).toContain('自动流水线总开关')
    expect(document.body.textContent).toContain('自动提链并发')
    expect(document.body.textContent).toContain('自动支付并发')
    expect(document.body.textContent).toContain('提链失败自动重试（0 = 仅手动）')
    expect(document.body.textContent).toContain('支付失败自动重试（0 = 仅手动）')
    expect(document.body.textContent).toContain('账单国家 / 支付方式')
    expect(document.body.textContent).toContain('账单国家与下面的代理出口国家互相独立')
    expect(document.body.textContent).toContain('日本 · JPY · PayPal')
    expect(document.body.textContent).toContain('德国 · EUR · PayPal')
    expect(document.body.textContent).toContain('后端已配置')
    expect(document.body.textContent).toContain('选择配置栏中的国家代理池')
    expect(document.body.textContent).toContain('打开接码配置')
    wrapper.unmount()
  })

  it('keeps persisted GB proxy groups while proxy options load later', async () => {
    const gbSettings = {
      ...settings,
      enabled: true,
      country: 'GB',
      checkoutProxyCountry: 'GB',
      updateProxyCountry: 'GB',
      protocolProxyCountry: 'GB',
      checkoutProxyGroup: 'GB-A',
      updateProxyGroup: 'GB-A',
      protocolProxyGroup: 'GB-A',
    }
    let resolveGroups!: (groups: Awaited<ReturnType<typeof dataGateway.listProxyGroups>>) => void
    vi.mocked(dataGateway.pipelineSettings).mockResolvedValueOnce(gbSettings)
    vi.mocked(dataGateway.updatePipelineSettings).mockResolvedValueOnce(gbSettings)
    vi.mocked(dataGateway.listProxyCountries).mockResolvedValueOnce([
      { country: 'GB', total: 10, enabled: 10 },
    ])
    vi.mocked(dataGateway.listProxyGroups).mockReturnValueOnce(new Promise((resolve) => {
      resolveGroups = resolve
    }))

    const wrapper = mount(PipelineView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus, router] },
    })
    await flushPromises()
    resolveGroups([
      { country: 'GB', group: 'GB-A', total: 10, enabled: 10, available: 10, quarantined: 0, schemes: ['socks5'] },
    ])
    await flushPromises()

    expect(wrapper.text()).toContain('英国 · GB Plus 流水线')
    const configButton = wrapper.findAll('button').find((button) => button.text().includes('流水线配置'))
    await configButton!.trigger('click')
    await flushPromises()
    const saveButton = Array.from(document.body.querySelectorAll('button'))
      .find((button) => button.textContent?.includes('保存配置'))
    saveButton?.click()
    await flushPromises()

    expect(dataGateway.updatePipelineSettings).toHaveBeenCalledWith(expect.objectContaining({
      country: 'GB',
      checkoutProxyCountry: 'GB',
      updateProxyCountry: 'GB',
      protocolProxyCountry: 'GB',
      checkoutProxyGroup: 'GB-A',
      updateProxyGroup: 'GB-A',
      protocolProxyGroup: 'GB-A',
    }))
    wrapper.unmount()
  })
})
