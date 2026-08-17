import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import PaidAccountsView from './PaidAccountsView.vue'
import { dataGateway } from '@/services/dataGateway'
import { copyText } from '@/services/exporter'
import type { PipelineItem } from '@/types'

vi.mock('@/services/dataGateway', () => ({
  dataGateway: {
    listPipeline: vi.fn(),
    paidPipelineStats: vi.fn(),
    exportPaidPipeline: vi.fn(),
    markPaidPipelineExport: vi.fn(),
    checkPaidPipelineMail: vi.fn(),
  },
}))

vi.mock('@/services/exporter', () => ({
  copyText: vi.fn(),
  downloadTextFile: vi.fn(),
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
    })
    vi.mocked(dataGateway.markPaidPipelineExport).mockResolvedValue({ updated: 1 })
    vi.mocked(dataGateway.checkPaidPipelineMail).mockResolvedValue({
      requested: 1, checked: 1, confirmed: 1, notFound: 0, failed: 0,
      items: [{ id: 'paid-1', status: 'confirmed' }],
    })
  })

  it('shows paid account metrics and exports every filtered result', async () => {
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('成品管理')
    expect(wrapper.text()).toContain('paid@example.test')
    expect(wrapper.text()).not.toContain('近 14 天完成趋势')
    expect(wrapper.text()).toContain('到账状态')
    expect(wrapper.text()).toContain('OAICS')

    const copyAll = wrapper.findAll('button').find((button) => button.text().includes('复制全部'))
    await copyAll!.trigger('click')
    await flushPromises()

    expect(dataGateway.exportPaidPipeline).toHaveBeenCalledWith([], '', 'all')
    expect(copyText).toHaveBeenCalledWith('paid@example.test----https://mail.example.test/paid')
    wrapper.unmount()
  })

  it('hides TOTP and supports quick selection, mail checking, and export marking', async () => {
    const wrapper = mount(PaidAccountsView, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).not.toContain('TOTP 密钥')
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
})
