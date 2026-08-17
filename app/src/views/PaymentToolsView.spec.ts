import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { dataGateway } from '@/services/dataGateway'
import type {
  ExtractedAccessToken,
  PaymentExtractorDefaults,
  PaymentExtractorTask,
} from '@/types'
import PaymentToolsView from './PaymentToolsView.vue'

const defaults: PaymentExtractorDefaults = {
  country: 'BR',
  forceCountry: '',
  paymentMethod: 'paypal',
  checkoutProxy: '',
  updateProxy: '',
  applyCheckoutUpdate: true,
  concurrency: 4,
  maxConcurrency: 10,
  countries: [
    { value: 'BR', label: '巴西 · USD' },
    { value: 'JP', label: '日本 · JPY' },
  ],
  paymentMethods: [
    { value: 'paypal', label: 'PayPal' },
    { value: 'gopay', label: 'GoPay' },
    { value: 'gcash', label: 'GCash' },
  ],
}

function makeTask(overrides: Partial<PaymentExtractorTask> = {}): PaymentExtractorTask {
  return {
    taskId: 'task-fixture',
    status: 'queued',
    stage: 'queued',
    progress: 0,
    createdAt: '2026-08-14T09:00:00.000Z',
    accountEmail: 'account@example.test',
    paymentMethod: 'paypal',
    billingCountry: 'BR',
    ...overrides,
  }
}

function tokenItem(token: string, index: number): ExtractedAccessToken {
  return {
    token,
    preview: `token-${index}...hidden`,
    email: `user-${index}@example.test`,
    accountId: `account-${index}`,
    planType: 'free',
    expiresAt: '2099-08-14T00:00:00.000Z',
    expired: false,
  }
}

function mountView(tasks: PaymentExtractorTask[] = []) {
  vi.spyOn(dataGateway, 'paymentExtractorDefaults').mockResolvedValue(defaults)
  vi.spyOn(dataGateway, 'listPaymentExtractorTasks').mockResolvedValue({ tasks })
  return mount(PaymentToolsView, { global: { plugins: [ElementPlus] } })
}

function field(wrapper: VueWrapper, testId: string, tag: 'textarea' | 'input' = 'textarea') {
  const root = wrapper.get(`[data-testid="${testId}"]`)
  return root.element.tagName.toLowerCase() === tag ? root : root.get(tag)
}

describe('PaymentToolsView', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.spyOn(dataGateway, 'setPaymentExtractorConcurrency').mockResolvedValue({
      concurrency: 4,
      maxConcurrency: 10,
    })
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    })
  })

  it('updates and persists the extraction concurrency', async () => {
    const update = vi.mocked(dataGateway.setPaymentExtractorConcurrency)
    update.mockResolvedValue({ concurrency: 3, maxConcurrency: 10 })
    const wrapper = mountView()
    await flushPromises()

    const control = wrapper.findComponent({ name: 'ElInputNumber' })
    control.vm.$emit('update:modelValue', 3)
    await wrapper.vm.$nextTick()
    control.vm.$emit('change', 3, 4)
    await flushPromises()

    expect(update).toHaveBeenLastCalledWith(3)
    expect(JSON.parse(
      localStorage.getItem('autoregister.payment-extractor.preferences') || '{}',
    ).concurrency).toBe(3)
    wrapper.unmount()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('extracts a batch without rendering or persisting full ATs and round-robins both proxy pools', async () => {
    const accessTokens = ['ACCESS_TOKEN_ONE_PRIVATE', 'ACCESS_TOKEN_TWO_PRIVATE']
    vi.spyOn(dataGateway, 'extractAccessTokens').mockResolvedValue({
      count: 2,
      items: accessTokens.map(tokenItem),
    })
    const create = vi
      .spyOn(dataGateway, 'createPaymentExtractorTask')
      .mockImplementation(async (input) =>
        makeTask({
          taskId: input.accessToken === accessTokens[0] ? 'task-one' : 'task-two',
          accountEmail: input.accessToken === accessTokens[0]
            ? 'user-0@example.test'
            : 'user-1@example.test',
        }),
      )
    const wrapper = mountView()
    await flushPromises()

    await field(wrapper, 'credential-input').setValue(
      JSON.stringify({ accessToken: accessTokens[0] }),
    )
    await wrapper
      .findAll('button')
      .find((button) => button.text().includes('提炼 Access Token'))!
      .trigger('click')
    await flushPromises()

    await field(wrapper, 'checkout-proxy-pool').setValue(
      [
        'http://user-sid-AAAAAAAAAA:pass@checkout-1.test:8000',
        'http://user-sid-BBBBBBBBBB:pass@checkout-2.test:8000',
      ].join('\n'),
    )
    await field(wrapper, 'update-proxy-pool').setValue(
      [
        'http://user-sid-CCCCCCCCCC:pass@update-1.test:8000',
        'http://user-sid-DDDDDDDDDD:pass@update-2.test:8000',
      ].join('\n'),
    )
    await wrapper.get('[data-testid="submit-all"]').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(create).toHaveBeenCalledTimes(2)
    expect(create.mock.calls[0]![0]).toMatchObject({
      accessToken: accessTokens[0],
      country: 'BR',
      paymentMethod: 'paypal',
      applyCheckoutUpdate: true,
    })
    expect(create.mock.calls[0]![0].checkoutProxy).toContain('checkout-1.test')
    expect(create.mock.calls[1]![0].checkoutProxy).toContain('checkout-2.test')
    expect(create.mock.calls[0]![0].updateProxy).toContain('update-1.test')
    expect(create.mock.calls[1]![0].updateProxy).toContain('update-2.test')
    expect(create.mock.calls[0]![0].checkoutProxy).not.toContain('sid-AAAAAAAAAA')

    expect(wrapper.text()).not.toContain(accessTokens[0])
    expect(wrapper.text()).not.toContain(accessTokens[1])
    const persisted = localStorage.getItem('autoregister.payment-extractor.preferences') || ''
    expect(persisted).toContain('checkout-1.test')
    expect(persisted).not.toContain('ACCESS_TOKEN')
    wrapper.unmount()
  })

  it('imports an IPRocket proxy subscription into both pools and removes duplicates', async () => {
    const loadProxySource = vi
      .spyOn(dataGateway, 'loadPaymentExtractorProxySource')
      .mockResolvedValue({
        ok: true,
        proxies: [
          'http://existing:pass@checkout.test:8000',
          'http://imported:pass@iprocket.test:9000',
          'http://imported:pass@iprocket.test:9000',
        ],
        count: 3,
        uniqueCount: 2,
      })
    const wrapper = mountView()
    await flushPromises()

    await field(wrapper, 'checkout-proxy-pool').setValue(
      'http://existing:pass@checkout.test:8000',
    )
    await field(wrapper, 'update-proxy-pool').setValue(
      'http://update:pass@update.test:8000',
    )
    await field(wrapper, 'proxy-source-url', 'input').setValue(
      'https://app.iprocket.io/subscription/fixture',
    )
    await wrapper.get('[data-testid="import-proxy-source"]').trigger('click')
    await flushPromises()

    expect(loadProxySource).toHaveBeenCalledWith(
      'https://app.iprocket.io/subscription/fixture',
    )
    expect((field(wrapper, 'checkout-proxy-pool').element as HTMLTextAreaElement).value)
      .toBe([
        'http://existing:pass@checkout.test:8000',
        'http://imported:pass@iprocket.test:9000',
      ].join('\n'))
    expect((field(wrapper, 'update-proxy-pool').element as HTMLTextAreaElement).value)
      .toBe([
        'http://update:pass@update.test:8000',
        'http://existing:pass@checkout.test:8000',
        'http://imported:pass@iprocket.test:9000',
      ].join('\n'))
    wrapper.unmount()
  })

  it('tests proxies and handles cancel, retry, PayPal resolution, opening, deletion, and bulk clear', async () => {
    const running = makeTask({ taskId: 'running-task', status: 'running', stage: 'checkout', progress: 15 })
    const retryable = makeTask({ taskId: 'retry-task', status: 'failed', stage: 'failed', error: 'network timeout' })
    const bulkFailed = makeTask({ taskId: 'bulk-failed', status: 'cancelled', stage: 'cancelled' })
    const succeeded = makeTask({
      taskId: 'success-task',
      status: 'succeeded',
      stage: 'completed',
      progress: 100,
      finishedAt: '2026-08-14T09:01:00.000Z',
      result: {
        providerUrl: 'https://www.paypal.com/agreements/approve?fixture=1',
        amountDue: 0,
        amountDueMinor: 0,
        currency: 'USD',
        checkoutSessionId: 'oaics_fixture',
        sessionKind: 'openai_custom_checkout',
      },
    })
    const configuredDefaults = {
      ...defaults,
      checkoutProxy: 'http://checkout:pass@proxy.test:8000',
      updateProxy: 'http://update:pass@proxy.test:8000',
    }
    vi.spyOn(dataGateway, 'paymentExtractorDefaults').mockResolvedValue(configuredDefaults)
    vi.spyOn(dataGateway, 'listPaymentExtractorTasks').mockResolvedValue({
      tasks: [running, retryable, bulkFailed, succeeded],
    })
    const proxyTest = vi.spyOn(dataGateway, 'testPaymentExtractorProxy').mockResolvedValue({
      ok: true,
      ip: '203.0.113.8',
      countryCode: 'BR',
      region: 'Sao Paulo',
    })
    const cancel = vi.spyOn(dataGateway, 'cancelPaymentExtractorTask').mockResolvedValue({
      ...running,
      status: 'cancel_requested',
    })
    const retry = vi.spyOn(dataGateway, 'retryPaymentExtractorTask').mockResolvedValue(
      makeTask({ taskId: 'retried-task' }),
    )
    const resolve = vi.spyOn(dataGateway, 'resolvePaymentExtractorPaypal').mockResolvedValue({
      ...succeeded,
      result: {
        ...succeeded.result,
        providerUrl: 'https://www.paypal.com/billing/agreements/resolved',
      },
    })
    const remove = vi.spyOn(dataGateway, 'deletePaymentExtractorTask').mockResolvedValue({
      ok: true,
      taskId: 'success-task',
      status: 'deleted',
    })
    const bulk = vi.spyOn(dataGateway, 'bulkDeletePaymentExtractorTasks').mockResolvedValue({
      ok: true,
      deletedCount: 1,
      taskIds: ['bulk-failed'],
    })
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = mount(PaymentToolsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    await wrapper.get('[data-testid="test-checkout-proxy"]').trigger('click')
    await flushPromises()
    expect(proxyTest).toHaveBeenCalledWith('http://checkout:pass@proxy.test:8000')
    expect(wrapper.text()).toContain('203.0.113.8')

    await wrapper.get('[data-testid="cancel-running-task"]').trigger('click')
    await flushPromises()
    expect(cancel).toHaveBeenCalledWith('running-task')

    await wrapper.get('[data-testid="retry-retry-task"]').trigger('click')
    await flushPromises()
    expect(retry).toHaveBeenCalledWith(
      'retry-task',
      expect.objectContaining({
        checkoutProxy: 'http://checkout:pass@proxy.test:8000',
        updateProxy: 'http://update:pass@proxy.test:8000',
      }),
    )

    await wrapper.get('[data-testid="open-result-success-task"]').trigger('click')
    expect(open).toHaveBeenCalledWith(
      'https://www.paypal.com/agreements/approve?fixture=1',
      '_blank',
      'noopener,noreferrer',
    )

    await wrapper.get('[data-testid="resolve-success-task"]').trigger('click')
    await flushPromises()
    expect(resolve).toHaveBeenCalledWith('success-task')
    expect(wrapper.text()).toContain('https://www.paypal.com/billing/agreements/resolved')

    await wrapper.get('[data-testid="clear-failed"]').trigger('click')
    await flushPromises()
    expect(bulk).toHaveBeenCalledWith('failed')

    await wrapper.get('[data-testid="delete-success-task"]').trigger('click')
    await flushPromises()
    expect(remove).toHaveBeenCalledWith('success-task')
    wrapper.unmount()
  })

  it('retries every network failure and continues after an individual retry fails', async () => {
    const firstFailure = makeTask({
      taskId: 'network-first',
      status: 'failed',
      stage: 'failed',
      networkError: true,
      error: 'checkout timeout',
      accountEmail: 'first@example.test',
    })
    const secondFailure = makeTask({
      taskId: 'network-second',
      status: 'failed',
      stage: 'failed',
      networkError: true,
      error: 'proxy disconnected',
      accountEmail: 'second@example.test',
    })
    const nonNetworkFailure = makeTask({
      taskId: 'validation-failure',
      status: 'failed',
      stage: 'failed',
      networkError: false,
      error: 'invalid fixture',
    })
    vi.spyOn(dataGateway, 'paymentExtractorDefaults').mockResolvedValue({
      ...defaults,
      checkoutProxy: [
        'http://user:pass@checkout-1.test:8000',
        'http://user:pass@checkout-2.test:8000',
      ].join('\n'),
      updateProxy: [
        'http://user:pass@update-1.test:8000',
        'http://user:pass@update-2.test:8000',
      ].join('\n'),
    })
    vi.spyOn(dataGateway, 'listPaymentExtractorTasks').mockResolvedValue({
      tasks: [firstFailure, secondFailure, nonNetworkFailure],
    })
    const retry = vi
      .spyOn(dataGateway, 'retryPaymentExtractorTask')
      .mockImplementation(async (taskId) => {
        if (taskId === firstFailure.taskId) throw new Error('fixture retry failure')
        return makeTask({
          taskId: `retried-${taskId}`,
          accountEmail: 'retried@example.test',
        })
      })
    const wrapper = mount(PaymentToolsView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    await wrapper.get('[data-testid="retry-network-failed"]').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(retry.mock.calls.map(([taskId]) => taskId)).toEqual([
      'network-first',
      'network-second',
    ])
    expect(retry).not.toHaveBeenCalledWith('validation-failure', expect.anything())
    expect(new Set(retry.mock.calls.map(([, input]) => input.checkoutProxy))).toEqual(
      new Set([
        'http://user:pass@checkout-1.test:8000',
        'http://user:pass@checkout-2.test:8000',
      ]),
    )
    expect(new Set(retry.mock.calls.map(([, input]) => input.updateProxy))).toEqual(
      new Set([
        'http://user:pass@update-1.test:8000',
        'http://user:pass@update-2.test:8000',
      ]),
    )
    expect(wrapper.find('[data-task-id="network-first"]').exists()).toBe(true)
    expect(wrapper.find('[data-task-id="network-second"]').exists()).toBe(false)
    expect(wrapper.find('[data-task-id="retried-network-second"]').exists()).toBe(true)
    expect(wrapper.find('[data-task-id="validation-failure"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('exports successful payment links as a quoted CSV download', async () => {
    const succeeded = makeTask({
      taskId: 'csv-success',
      status: 'succeeded',
      stage: 'completed',
      progress: 100,
      accountEmail: 'quoted,"account"@example.test',
      result: {
        providerUrl: 'https://pay.example.test/provider?fixture=one,two',
        checkoutSessionId: 'oaics_csv_fixture',
        amountDue: 0,
        amountDueMinor: 0,
        currency: 'USD',
        paymentMethodId: 'pm_csv_fixture',
      },
    })
    let exportedBlob: Blob | undefined
    const createObjectUrl = vi
      .spyOn(URL, 'createObjectURL')
      .mockImplementation((blob) => {
        exportedBlob = blob as Blob
        return 'blob:payment-links-fixture'
      })
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    let clickedAnchor: HTMLAnchorElement | undefined
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        clickedAnchor = this
      })
    const wrapper = mountView([succeeded])
    await flushPromises()

    await wrapper.get('[data-testid="export-success-csv"]').trigger('click')

    expect(createObjectUrl).toHaveBeenCalledTimes(1)
    expect(anchorClick).toHaveBeenCalledTimes(1)
    expect(clickedAnchor?.href).toBe('blob:payment-links-fixture')
    expect(clickedAnchor?.download).toMatch(/^payment-links-.*\.csv$/)
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:payment-links-fixture')
    expect(exportedBlob).toBeDefined()

    const csv = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.addEventListener('load', () => resolve(String(reader.result)))
      reader.addEventListener('error', () => reject(reader.error))
      reader.readAsText(exportedBlob as Blob)
    })
    expect(csv).toContain(
      '"account_email","provider_url","checkout_session_id","payment_method","billing_country","amount_due","currency","payment_method_id"',
    )
    expect(csv).toContain(
      '"quoted,""account""@example.test","https://pay.example.test/provider?fixture=one,two","oaics_csv_fixture","paypal","BR","0","USD","pm_csv_fixture"',
    )
    wrapper.unmount()
  })

  it('polls an active task to completion and keeps manual opening HTTPS-only', async () => {
    vi.useFakeTimers()
    const running = makeTask({ taskId: 'poll-task', status: 'running', stage: 'stripe_init', progress: 35 })
    const completed = makeTask({
      ...running,
      status: 'succeeded',
      stage: 'completed',
      progress: 100,
      result: {
        providerUrl: 'https://pay.example.test/provider/fixture',
        amountDue: 1.25,
        amountDueMinor: 125,
        currency: 'USD',
      },
    })
    const getTask = vi.spyOn(dataGateway, 'getPaymentExtractorTask').mockResolvedValue(completed)
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = mountView([running])
    await flushPromises()

    await vi.advanceTimersByTimeAsync(1200)
    await flushPromises()
    expect(getTask).toHaveBeenCalledWith('poll-task')
    expect(wrapper.text()).toContain('https://pay.example.test/provider/fixture')
    expect(wrapper.text()).toContain('1.25 USD')

    const existing = wrapper.find('input[placeholder*="已有 HTTPS 支付链接"]')
    await existing.setValue('http://insecure.example.test/pay')
    const manualOpen = wrapper
      .findAll('button')
      .find((button) => button.text().trim() === '打开')
    await manualOpen!.trigger('click')
    expect(open).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
