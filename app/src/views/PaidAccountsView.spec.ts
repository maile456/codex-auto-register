import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessage } from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import PaidAccountsView from './PaidAccountsView.vue'
import { dataGateway } from '@/services/dataGateway'
import { copyText, downloadEncodedFile } from '@/services/exporter'
import type { PipelineItem } from '@/types'

vi.mock('@/services/dataGateway', () => ({
  dataGateway: {
    listPipeline: vi.fn(),
    paidPipelineStats: vi.fn(),
    exportPaidPipeline: vi.fn(),
    markPaidPipelineExport: vi.fn(),
    checkPaidPipelineMail: vi.fn(),
    smsReceiverSettings: vi.fn(),
    updateSmsReceiverSettings: vi.fn(),
    testSmsReceiver: vi.fn(),
    smsReceiverHeroSmsSettings: vi.fn(),
    updateSmsReceiverHeroSmsSettings: vi.fn(),
    smsReceiverHeroSmsCatalog: vi.fn(),
    submitPaidToSmsReceiver: vi.fn(),
    refreshSmsReceiverStatus: vi.fn(),
    deletePipeline: vi.fn(),
  },
}))

vi.mock('@/services/exporter', () => ({
  copyText: vi.fn(),
  downloadTextFile: vi.fn(),
  downloadEncodedFile: vi.fn(),
}))

const paidItem: PipelineItem = {
  id: 'paid-1',
  accountId: 'account-1',
  email: 'paid@example.test',
  chatgptPassword: 'FIXTURE_PASSWORD',
  totpSecret: 'FIXTURE_TOTP',
  emailAccessUrl: 'https://mail.example.test/paid',
  accountCreatedAt: '2026-08-14T00:00:00Z',
  accountType: 'free',
  promotionEligible: true,
  accessTokenConfigured: true,
  accessTokenExpiresAt: '2026-08-16T00:00:00Z',
  stage: 'paid',
  extractionStatus: 'succeeded',
  extractionRetryCount: 0,
  checkoutType: 'oaics',
  paymentLinkConfigured: true,
  paymentStatus: 'completed',
  paymentRetryCount: 0,
  paymentPhonePreview: '***5678',
  heroSmsManaged: true,
  heroSmsStatus: 'code_submitted',
  heroSmsAttempt: 1,
  heroSmsPrice: 0.42,
  paidAt: '2026-08-15T01:00:00Z',
  paymentSummary: { status: 'approved', settlementStatus: 'settled', billingCountry: 'JP' },
  exportCount: 0,
  mailConfirmationStatus: 'unchecked',
  createdAt: '2026-08-14T00:00:00Z',
  updatedAt: '2026-08-15T01:00:00Z',
}

describe('PaidAccountsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(dataGateway.listPipeline).mockResolvedValue({
      items: [paidItem], total: 1, page: 1, pageSize: 20, counts: { paid: 1 },
    })
    vi.mocked(dataGateway.paidPipelineStats).mockResolvedValue({
      total: 1, today: 1, last7Days: 1, terminalTotal: 1, failed: 0,
      successRate: 100, averageHeroSmsPrice: 0.42,
      exported: 0, unexported: 1, mailConfirmed: 0,
      daily: [{ date: '2026-08-15', count: 1 }],
    })
    vi.mocked(dataGateway.exportPaidPipeline).mockResolvedValue({
      content: 'paid@example.test----https://mail.example.test/paid',
      filename: 'paid-accounts.txt', count: 1, skippedMissingUrlCount: 0,
      format: 'original', mimeType: 'text/plain', encoding: 'utf-8',
    })
    vi.mocked(dataGateway.markPaidPipelineExport).mockResolvedValue({ updated: 1 })
    vi.mocked(dataGateway.checkPaidPipelineMail).mockResolvedValue({
      requested: 1, checked: 1, confirmed: 1, notFound: 0, failed: 0,
      items: [{ id: 'paid-1', status: 'confirmed' }],
    })
    vi.mocked(dataGateway.smsReceiverSettings).mockResolvedValue({
      enabled: true,
      autoSubmit: false,
      baseUrl: 'http://127.0.0.1:5015',
      mailboxPublicBaseUrl: '',
      concurrency: 3,
      failureRetries: 1,
      retryBackoffSeconds: 30,
      updatedAt: null,
    })
    vi.mocked(dataGateway.smsReceiverHeroSmsSettings).mockResolvedValue({
      apiKey: '',
      countryIds: [16],
      minPrice: null,
      maxPrice: 1,
      preferredPrice: null,
      acquirePriority: 'country',
      maxRetries: 3,
      codeWaitSeconds: 180,
      emailOtpWaitSeconds: 90,
      emailOtpPollIntervalSeconds: 3,
      emailOtpAttempts: 1,
      reuseEnabled: true,
      credentialConfigured: true,
    })
    vi.mocked(dataGateway.smsReceiverHeroSmsCatalog).mockResolvedValue({
      countries: [{ id: 16, name: '英国' }, { id: 36, name: '日本' }],
    })
    vi.mocked(dataGateway.updateSmsReceiverSettings).mockResolvedValue({
      enabled: true,
      autoSubmit: false,
      baseUrl: 'http://127.0.0.1:5015',
      mailboxPublicBaseUrl: '',
      concurrency: 3,
      failureRetries: 1,
      retryBackoffSeconds: 30,
      updatedAt: null,
    })
    vi.mocked(dataGateway.updateSmsReceiverHeroSmsSettings).mockResolvedValue({
      apiKey: '',
      countryIds: [16],
      minPrice: null,
      maxPrice: 1,
      preferredPrice: null,
      acquirePriority: 'country',
      maxRetries: 3,
      codeWaitSeconds: 180,
      emailOtpWaitSeconds: 90,
      emailOtpPollIntervalSeconds: 3,
      emailOtpAttempts: 1,
      reuseEnabled: true,
      credentialConfigured: true,
    })
    vi.mocked(dataGateway.submitPaidToSmsReceiver).mockResolvedValue({
      requested: 1,
      processed: 1,
      submitted: 1,
      skipped: 0,
      failed: 0,
      items: [{ id: 'paid-1', ok: true, state: 'queued' }],
    })
    vi.mocked(dataGateway.deletePipeline).mockResolvedValue(1)
  })

  it('shows paid account metrics and exports every filtered result', async () => {
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('成品管理')
    expect(wrapper.text()).toContain('paid@example.test')
    expect(wrapper.text()).toContain('导入格式：邮箱|密码|2FA')
    expect(wrapper.text()).not.toContain('近 14 天完成趋势')
    expect(wrapper.text()).toContain('到账状态')
    expect(wrapper.text()).toContain('OAICS')

    const exportAll = wrapper.findAll('button').find((button) => button.text().includes('导出全部'))
    await exportAll!.trigger('click')
    await flushPromises()

    expect(dataGateway.exportPaidPipeline).toHaveBeenCalledWith([], '', 'all', 'original')
    expect(downloadEncodedFile).toHaveBeenCalledWith(
      'paid@example.test----https://mail.example.test/paid',
      'paid-accounts.txt',
      'text/plain',
      'utf-8',
    )
    wrapper.unmount()
  })

  it('shows 2FA and supports quick selection, mail checking, and export marking', async () => {
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('2FA')
    await wrapper.findAll('button').find((button) => button.text().includes('前 10'))!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('已选 1')

    await wrapper.findAll('button').find((button) => button.text().includes('重新检查到账'))!.trigger('click')
    await flushPromises()
    expect(dataGateway.checkPaidPipelineMail).toHaveBeenCalledWith(['paid-1'])

    await wrapper.findAll('button').find((button) => button.text().includes('标记已导出'))!.trigger('click')
    await flushPromises()
    expect(dataGateway.markPaidPipelineExport).toHaveBeenCalledWith(['paid-1'], true)
    wrapper.unmount()
  })

  it('opens the mailbox URL from the finished-product actions', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await wrapper.get('button[aria-label="打开接码 URL"]').trigger('click')
    expect(open).toHaveBeenCalledWith(
      'https://mail.example.test/paid',
      '_blank',
      'noopener,noreferrer',
    )
    wrapper.unmount()
  })

  it('deletes selected finished products without touching account data', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await wrapper.findAll('button').find((button) => button.text().includes('前 10'))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('删除选中'))!.trigger('click')
    await flushPromises()

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('成品管理删除'))
    expect(dataGateway.deletePipeline).toHaveBeenCalledWith('paid-1')
    confirm.mockRestore()
    wrapper.unmount()
  })

  it('shows a local MailCom mailbox inside the page without running the Plus check', async () => {
    const localItem = {
      ...paidItem,
      emailAccessUrl: 'http://127.0.0.1:3211/api/mail/latest?email=paid%40example.test',
    }
    vi.mocked(dataGateway.listPipeline).mockResolvedValue({
      items: [localItem], total: 1, page: 1, pageSize: 20, counts: { paid: 1 },
    })
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await wrapper.get('button[aria-label="查看邮箱"]').trigger('click')
    await flushPromises()

    expect(document.body.textContent).toContain('查看邮箱')
    expect(document.body.textContent).toContain('paid@example.test')
    expect(document.body.querySelector('iframe')?.getAttribute('src')).toBe(
      'http://127.0.0.1:3211/static/mailbox-viewer.html?email=paid%40example.test',
    )
    expect(dataGateway.checkPaidPipelineMail).not.toHaveBeenCalled()
    expect(open).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps HeroSMS settings in a compact dialog', async () => {
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await wrapper.findAll('button').find((button) => button.text().includes('HeroSMS 配置'))!.trigger('click')
    await flushPromises()

    expect(document.body.textContent).toContain('HeroSMS 接码配置')
    expect(document.body.textContent).toContain('API Key')
    expect(document.body.textContent).toContain('接码国家')
    expect(document.body.textContent).toContain('国家优先队列')
    expect(document.body.textContent).toContain('单号最高价')
    expect(document.body.textContent).toContain('单号等待秒数')
    expect(document.body.textContent).toContain('同时提交数量')
    expect(document.body.textContent).toContain('接口失败重试')
    expect(document.body.textContent).toContain('邮箱验证码')
    wrapper.unmount()
  })

  it('reorders the HeroSMS country priority queue before saving', async () => {
    vi.mocked(dataGateway.smsReceiverHeroSmsSettings).mockResolvedValue({
      apiKey: '',
      countryIds: [16, 36],
      minPrice: null,
      maxPrice: 1,
      preferredPrice: null,
      acquirePriority: 'country',
      maxRetries: 3,
      codeWaitSeconds: 180,
      emailOtpWaitSeconds: 90,
      emailOtpPollIntervalSeconds: 3,
      emailOtpAttempts: 1,
      reuseEnabled: true,
      credentialConfigured: true,
    })
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await wrapper.findAll('button').find((button) => button.text().includes('HeroSMS 配置'))!.trigger('click')
    await flushPromises()
    const down = document.body.querySelector<HTMLButtonElement>('button[aria-label="下移 16"]')
    down?.click()
    const save = Array.from(document.body.querySelectorAll('button'))
      .find((button) => button.textContent?.trim() === '保存')
    save?.click()
    await flushPromises()

    expect(dataGateway.updateSmsReceiverHeroSmsSettings).toHaveBeenCalledWith(
      expect.objectContaining({ countryIds: [36, 16] }),
    )
    wrapper.unmount()
  })

  it('saves the receiver address before forwarding HeroSMS settings', async () => {
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await wrapper.findAll('button').find((button) => button.text().includes('HeroSMS 配置'))!.trigger('click')
    await flushPromises()
    const save = Array.from(document.body.querySelectorAll('button')).find((button) => button.textContent?.trim() === '保存')
    save?.click()
    await flushPromises()

    expect(dataGateway.updateSmsReceiverSettings).toHaveBeenCalled()
    expect(dataGateway.updateSmsReceiverHeroSmsSettings).toHaveBeenCalled()
    expect(dataGateway.updateSmsReceiverSettings).toHaveBeenCalledWith(expect.objectContaining({
      concurrency: 3,
      failureRetries: 1,
      retryBackoffSeconds: 30,
    }))
    expect(dataGateway.updateSmsReceiverHeroSmsSettings).toHaveBeenCalledWith(expect.objectContaining({
      countryIds: [16],
      acquirePriority: 'country',
      emailOtpWaitSeconds: 90,
      emailOtpPollIntervalSeconds: 3,
      emailOtpAttempts: 1,
    }))
    const receiverSaveOrder = vi.mocked(dataGateway.updateSmsReceiverSettings).mock.invocationCallOrder[0]!
    const heroSmsSaveOrder = vi.mocked(dataGateway.updateSmsReceiverHeroSmsSettings).mock.invocationCallOrder[0]!
    expect(receiverSaveOrder).toBeLessThan(heroSmsSaveOrder)
    wrapper.unmount()
  })

  it('sends only selected accounts that have both password and 2FA', async () => {
    const incompleteItem: PipelineItem = {
      ...paidItem,
      id: 'paid-incomplete',
      accountId: 'account-incomplete',
      email: 'incomplete@example.test',
      chatgptPassword: '',
      totpSecret: '',
      emailAccessUrl: '',
    }
    vi.mocked(dataGateway.listPipeline).mockResolvedValue({
      items: [paidItem, incompleteItem],
      total: 2,
      page: 1,
      pageSize: 20,
      counts: { paid: 2 },
    })
    const warning = vi.spyOn(ElMessage, 'warning')
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    const rowButtons = wrapper.findAll('button[aria-label="HeroSMS 接码"]')
    expect(rowButtons).toHaveLength(2)
    expect(rowButtons[0]!.attributes('disabled')).toBeUndefined()
    expect(rowButtons[1]!.attributes('disabled')).toBeDefined()

    await wrapper.findAll('button').find((button) => button.text().includes('前 10'))!.trigger('click')
    await flushPromises()
    const submit = wrapper.findAll('button').find((button) => button.text().includes('接码选中'))!
    expect(submit.text()).toContain('接码选中 1')
    await submit.trigger('click')
    await flushPromises()

    expect(dataGateway.submitPaidToSmsReceiver).toHaveBeenCalledWith(['paid-1'])
    expect(warning).toHaveBeenCalledWith('已跳过 1 个缺少密码或 2FA 的账号')
    wrapper.unmount()
  })

  it('copies a base64-transported Sub2 export as readable text', async () => {
    vi.mocked(dataGateway.exportPaidPipeline).mockResolvedValue({
      content: '',
      contentBase64: btoa('{"accounts":[{"email":"paid@example.test"}]}'),
      filename: 'paid-sub2.json',
      count: 1,
      skippedMissingUrlCount: 0,
      format: 'sub2api',
      mimeType: 'application/json',
      encoding: 'base64',
    })
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await wrapper.findAll('button').find((button) => button.text().includes('前 10'))!.trigger('click')
    await flushPromises()
    const sub2FormatButtons = wrapper.findAll('.export-format-option')
    await sub2FormatButtons.find((item) => item.text().includes('Sub2'))!.trigger('click')
    await sub2FormatButtons[0]!.trigger('click')
    await wrapper.findAll('button').find((button) => button.text().includes('复制选中'))!.trigger('click')
    await flushPromises()

    expect(dataGateway.exportPaidPipeline).toHaveBeenCalledWith(['paid-1'], '', 'all', 'sub2api')
    expect(copyText).toHaveBeenCalledWith('{"accounts":[{"email":"paid@example.test"}]}')
    wrapper.unmount()
  })

  it('copies and downloads password plus 2FA exports and reports incomplete accounts', async () => {
    const securityExport = 'paid@example.test----FIXTURE_PASSWORD----FIXTURE_TOTP'
    vi.mocked(dataGateway.exportPaidPipeline).mockResolvedValue({
      content: securityExport,
      filename: 'paid-password-totp.txt',
      count: 1,
      skippedMissingUrlCount: 0,
      skippedMissingSecurityCount: 2,
      format: 'password_totp',
      mimeType: 'text/plain',
      encoding: 'utf-8',
    })
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await wrapper.findAll('button').find((button) => button.text().includes('前 10'))!.trigger('click')
    await flushPromises()
    const securityFormatButtons = wrapper.findAll('.export-format-option')
    await securityFormatButtons.find((item) => item.text().includes('2FA'))!.trigger('click')
    await securityFormatButtons[0]!.trigger('click')

    expect(wrapper.text()).toContain('邮箱----密码----2FA')
    await wrapper.findAll('button').find((button) => button.text().includes('复制选中'))!.trigger('click')
    await flushPromises()

    expect(dataGateway.exportPaidPipeline).toHaveBeenCalledWith(['paid-1'], '', 'all', 'password_totp')
    expect(copyText).toHaveBeenCalledWith(securityExport)
    expect(document.body.textContent).toContain('另有 2 条缺少密码或 2FA')

    await wrapper.findAll('button').find((button) => button.text().includes('前 10'))!.trigger('click')
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('导出选中'))!.trigger('click')
    await flushPromises()

    expect(downloadEncodedFile).toHaveBeenCalledWith(
      securityExport,
      'paid-password-totp.txt',
      'text/plain',
      'utf-8',
    )
    wrapper.unmount()
  })
})
