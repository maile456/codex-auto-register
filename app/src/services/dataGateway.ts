import type {
  AccountPlanCheckResult,
  AccountExport,
  AccountRecord,
  AccessTokenExtractResult,
  EmailExport,
  EmailRecord,
  EmailSource,
  ExportFormat,
  ExportScope,
  HealthResponse,
  HeroSmsTestResult,
  HeroSmsCountry,
  HeroSmsSettings,
  ImportResult,
  OverviewStats,
  PageResponse,
  ProxyRecord,
  ProxyCountrySummary,
  ProxyGroupSummary,
  PaymentExtractorBulkDeleteResult,
  PaymentExtractorConcurrency,
  PaymentExtractorDefaults,
  PaymentExtractorDeleteResult,
  PaymentExtractorOption,
  PaymentExtractorProxyTestResult,
  PaymentExtractorProxySourceResult,
  PaymentExtractorRetryInput,
  PaymentExtractorTask,
  PaymentExtractorTaskInput,
  PaymentExtractorTaskList,
  PaypalAgreementServiceState,
  PipelineItem,
  PipelineList,
  PipelinePaidExport,
  PipelinePaidMailCheck,
  PipelinePaidStats,
  PipelineSettings,
  SmsReceiverBatchResult,
  SmsReceiverSettings,
  ResourceQuery,
  RunExecutionState,
  RunWorkerSnapshot,
} from '@/types'

async function parseResponse<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>
  let message = `数据服务返回 HTTP ${response.status}`
  try {
    const body = await response.json()
    if (typeof body.detail === 'string') message = body.detail
    if (body.detail?.message) message = body.detail.message
    if (typeof body.error === 'string') message = body.error
  } catch {
    // Keep the stable fallback above.
  }
  throw new Error(message)
}

const PAYMENT_WORKBENCH_PASSWORD_KEY = 'payment_link_extractor.workbench_password'

function paymentExtractorHeaders(json = false): Record<string, string> {
  const headers: Record<string, string> = {}
  if (json) headers['Content-Type'] = 'application/json'
  try {
    const password = localStorage.getItem(PAYMENT_WORKBENCH_PASSWORD_KEY) || ''
    if (password) headers['X-Workbench-Password'] = password
  } catch {
    // Local-only deployments with an empty password remain usable without storage.
  }
  return headers
}

function queryString(query: ResourceQuery) {
  const params = new URLSearchParams({
    page: String(query.page),
    pageSize: String(query.pageSize),
  })
  if (query.q?.trim()) params.set('q', query.q.trim())
  if (query.country?.trim()) params.set('country', query.country.trim().toUpperCase())
  if (query.promotion) params.set('promotion', query.promotion)
  if (query.source && query.source !== 'all') params.set('source', query.source)
  return params.toString()
}

export const dataGateway = {
  async pipelineSettings(): Promise<PipelineSettings> {
    return parseResponse(await fetch('/api/pipeline/settings', {
      headers: paymentExtractorHeaders(),
    }))
  },

  async updatePipelineSettings(input: Omit<PipelineSettings,
    | 'updatedAt'
    | 'heroSmsApiKeyConfigured'
    | 'heroSmsEnabled'
    | 'heroSmsMaxPrice'
    | 'heroSmsChangeNumberRetries'
    | 'heroSmsNumberWaitSeconds'
    | 'heroSmsCountryId'
    | 'agreementAutoSmsEnabled'
  >): Promise<PipelineSettings> {
    return parseResponse(await fetch('/api/pipeline/settings', {
      method: 'PUT',
      headers: paymentExtractorHeaders(true),
      body: JSON.stringify(input),
    }))
  },

  async heroSmsSettings(): Promise<HeroSmsSettings> {
    return parseResponse(await fetch('/api/herosms/settings'))
  },

  async updateHeroSmsSettings(input: Omit<HeroSmsSettings, 'pipelineAutoPaymentEnabled' | 'apiKeyConfigured' | 'updatedAt'>): Promise<HeroSmsSettings> {
    return parseResponse(await fetch('/api/herosms/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    }))
  },

  async heroSmsCountries(): Promise<HeroSmsCountry[]> {
    return parseResponse(await fetch('/api/herosms/countries'))
  },

  async listPipeline(input: { page: number; pageSize: number; stage?: string; q?: string; exportState?: 'all' | 'exported' | 'unexported'; settlementState?: 'all' | 'waiting' | 'confirmed' | 'review' | 'failed' }): Promise<PipelineList> {
    const params = new URLSearchParams({ page: String(input.page), pageSize: String(input.pageSize) })
    if (input.stage) params.set('stage', input.stage)
    if (input.q?.trim()) params.set('q', input.q.trim())
    if (input.exportState && input.exportState !== 'all') params.set('exportState', input.exportState)
    if (input.settlementState && input.settlementState !== 'all') params.set('settlementState', input.settlementState)
    return parseResponse(await fetch(`/api/pipeline?${params}`, {
      headers: paymentExtractorHeaders(),
    }))
  },

  async paidPipelineStats(days = 14): Promise<PipelinePaidStats> {
    return parseResponse(await fetch(`/api/pipeline/paid/stats?days=${days}`, {
      headers: paymentExtractorHeaders(),
    }))
  },

  async exportPaidPipeline(ids: string[], query = '', exportState: 'all' | 'exported' | 'unexported' = 'all'): Promise<PipelinePaidExport> {
    return parseResponse(await fetch('/api/pipeline/paid/export', {
      method: 'POST',
      headers: paymentExtractorHeaders(true),
      body: JSON.stringify({ ids, query, exportState }),
    }))
  },

  async markPaidPipelineExport(ids: string[], exported: boolean): Promise<{ updated: number }> {
    return parseResponse(await fetch('/api/pipeline/paid/export-status', {
      method: 'POST',
      headers: paymentExtractorHeaders(true),
      body: JSON.stringify({ ids, exported }),
    }))
  },

  async checkPaidPipelineMail(ids: string[]): Promise<PipelinePaidMailCheck> {
    return parseResponse(await fetch('/api/pipeline/paid/mail-check', {
      method: 'POST',
      headers: paymentExtractorHeaders(true),
      body: JSON.stringify({ ids }),
    }))
  },

  async smsReceiverSettings(): Promise<SmsReceiverSettings> {
    return parseResponse(await fetch('/api/pipeline/sms-receiver/settings', {
      headers: paymentExtractorHeaders(),
    }))
  },

  async updateSmsReceiverSettings(input: Omit<SmsReceiverSettings, 'updatedAt'>): Promise<SmsReceiverSettings> {
    return parseResponse(await fetch('/api/pipeline/sms-receiver/settings', {
      method: 'PUT',
      headers: paymentExtractorHeaders(true),
      body: JSON.stringify(input),
    }))
  },

  async testSmsReceiver(): Promise<{ ok: boolean; service: string; baseUrl: string }> {
    return parseResponse(await fetch('/api/pipeline/sms-receiver/test', {
      method: 'POST',
      headers: paymentExtractorHeaders(),
    }))
  },

  async submitPaidToSmsReceiver(ids: string[]): Promise<SmsReceiverBatchResult> {
    return parseResponse(await fetch('/api/pipeline/paid/sms-receiver/submit', {
      method: 'POST',
      headers: paymentExtractorHeaders(true),
      body: JSON.stringify({ ids }),
    }))
  },

  async refreshSmsReceiverStatus(ids: string[]): Promise<SmsReceiverBatchResult> {
    return parseResponse(await fetch('/api/pipeline/paid/sms-receiver/status', {
      method: 'POST',
      headers: paymentExtractorHeaders(true),
      body: JSON.stringify({ ids }),
    }))
  },

  async syncPipeline(): Promise<{ eligible: number; inserted: number }> {
    return parseResponse(await fetch('/api/pipeline/sync', {
      method: 'POST',
      headers: paymentExtractorHeaders(),
    }))
  },

  async extractPipeline(ids: string[]): Promise<{ requested: number; started: number; deferred?: number }> {
    return parseResponse(await fetch('/api/pipeline/extract', {
      method: 'POST',
      headers: paymentExtractorHeaders(true),
      body: JSON.stringify({ ids }),
    }))
  },

  async testHeroSms(): Promise<HeroSmsTestResult> {
    return parseResponse(await fetch('/api/pipeline/herosms/test', {
      method: 'POST',
      headers: paymentExtractorHeaders(),
    }))
  },

  async heroSmsTest(): Promise<HeroSmsTestResult> {
    return parseResponse(await fetch('/api/herosms/test', {
      method: 'POST',
      headers: paymentExtractorHeaders(),
    }))
  },

  async startPipelinePayment(id: string, phone = '', protocolProxy = ''): Promise<PipelineItem> {
    return parseResponse(await fetch(`/api/pipeline/${encodeURIComponent(id)}/payment`, {
      method: 'POST',
      headers: paymentExtractorHeaders(true),
      body: JSON.stringify({ phone, protocolProxy }),
    }))
  },

  async submitPipelineOtp(id: string, value: string): Promise<PipelineItem> {
    return parseResponse(await fetch(`/api/pipeline/${encodeURIComponent(id)}/otp`, {
      method: 'POST',
      headers: paymentExtractorHeaders(true),
      body: JSON.stringify({ value }),
    }))
  },

  async retryPipeline(id: string, stage: 'extraction' | 'payment'): Promise<PipelineItem> {
    return parseResponse(await fetch(`/api/pipeline/${encodeURIComponent(id)}/retry-${stage}`, {
      method: 'POST',
      headers: paymentExtractorHeaders(),
    }))
  },

  async deletePipeline(id: string): Promise<number> {
    const result = await parseResponse<{ deleted: number }>(
      await fetch(`/api/pipeline/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        headers: paymentExtractorHeaders(),
      }),
    )
    return result.deleted
  },

  async paypalAgreementStatus(): Promise<PaypalAgreementServiceState> {
    return parseResponse(await fetch('/api/paypal-agreement/status'))
  },

  async startPaypalAgreement(): Promise<PaypalAgreementServiceState> {
    return parseResponse(
      await fetch('/api/paypal-agreement/start', { method: 'POST' }),
    )
  },

  async stopPaypalAgreement(): Promise<PaypalAgreementServiceState> {
    return parseResponse(
      await fetch('/api/paypal-agreement/stop', { method: 'POST' }),
    )
  },

  async extractAccessTokens(rawText: string): Promise<AccessTokenExtractResult> {
    return parseResponse(
      await fetch('/api/tools/access-tokens/extract', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rawText }),
      }),
    )
  },

  async paymentExtractorDefaults(): Promise<PaymentExtractorDefaults> {
    const defaults = await parseResponse<{
      country?: string
      forceCountry?: string | null
      paymentMethod?: string
      checkoutProxy?: string
      updateProxy?: string
      applyCheckoutUpdate?: boolean
      concurrency?: number
      maxConcurrency?: number
      countries?: Array<
        | string
        | {
            value?: string
            code?: string
            label?: string
            currency?: string | null
          }
      >
      paymentMethods?: Array<string | { value?: string; label?: string }>
    }>(await fetch('/api/payment-extractor/defaults', {
      headers: paymentExtractorHeaders(),
    }))

    const normalizeOption = (
      option: string | { value?: string; code?: string; label?: string; currency?: string | null },
      includeCurrency: boolean,
    ): PaymentExtractorOption | null => {
      if (typeof option === 'string') return { value: option, label: option }
      const value = String(option.value || option.code || '').trim()
      if (!value) return null
      const suffix = includeCurrency && option.currency ? ` · ${option.currency}` : ''
      return {
        value,
        label: option.label || `${value}${suffix}`,
        currency: option.currency,
      }
    }
    const countries = (defaults.countries || [])
      .map((option) => normalizeOption(option, true))
      .filter((option): option is PaymentExtractorOption => option !== null)
    const paymentMethods = (defaults.paymentMethods || [])
      .map((option) => normalizeOption(option, false))
      .filter((option): option is PaymentExtractorOption => option !== null)

    return {
      country: defaults.country || countries[0]?.value || '',
      forceCountry: defaults.forceCountry || '',
      paymentMethod: defaults.paymentMethod || paymentMethods[0]?.value || '',
      checkoutProxy: defaults.checkoutProxy || '',
      updateProxy: defaults.updateProxy || '',
      applyCheckoutUpdate: defaults.applyCheckoutUpdate !== false,
      concurrency: Math.max(1, Number(defaults.concurrency) || 4),
      maxConcurrency: Math.max(1, Number(defaults.maxConcurrency) || 10),
      countries,
      paymentMethods,
    }
  },

  async setPaymentExtractorConcurrency(
    concurrency: number,
  ): Promise<PaymentExtractorConcurrency> {
    return parseResponse(
      await fetch('/api/payment-extractor/concurrency', {
        method: 'PUT',
        headers: paymentExtractorHeaders(true),
        body: JSON.stringify({ concurrency }),
      }),
    )
  },

  async testPaymentExtractorProxy(
    checkoutProxy: string,
  ): Promise<PaymentExtractorProxyTestResult> {
    return parseResponse(
      await fetch('/api/payment-extractor/proxy-test', {
        method: 'POST',
        headers: paymentExtractorHeaders(true),
        body: JSON.stringify({ checkoutProxy }),
      }),
    )
  },

  async loadPaymentExtractorProxySource(
    url: string,
  ): Promise<PaymentExtractorProxySourceResult> {
    return parseResponse(
      await fetch('/api/payment-extractor/proxy-source', {
        method: 'POST',
        headers: paymentExtractorHeaders(true),
        body: JSON.stringify({ url }),
      }),
    )
  },

  async createPaymentExtractorTask(
    input: PaymentExtractorTaskInput,
  ): Promise<PaymentExtractorTask> {
    return parseResponse(
      await fetch('/api/payment-extractor/tasks', {
        method: 'POST',
        headers: paymentExtractorHeaders(true),
        body: JSON.stringify(input),
      }),
    )
  },

  async listPaymentExtractorTasks(): Promise<PaymentExtractorTaskList> {
    return parseResponse(await fetch('/api/payment-extractor/tasks', {
      headers: paymentExtractorHeaders(),
    }))
  },

  async getPaymentExtractorTask(taskId: string): Promise<PaymentExtractorTask> {
    return parseResponse(
      await fetch(`/api/payment-extractor/tasks/${encodeURIComponent(taskId)}`, {
        headers: paymentExtractorHeaders(),
      }),
    )
  },

  async cancelPaymentExtractorTask(taskId: string): Promise<PaymentExtractorTask> {
    return parseResponse(
      await fetch(`/api/payment-extractor/tasks/${encodeURIComponent(taskId)}/cancel`, {
        method: 'POST',
        headers: paymentExtractorHeaders(),
      }),
    )
  },

  async retryPaymentExtractorTask(
    taskId: string,
    input: PaymentExtractorRetryInput,
  ): Promise<PaymentExtractorTask> {
    return parseResponse(
      await fetch(`/api/payment-extractor/tasks/${encodeURIComponent(taskId)}/retry`, {
        method: 'POST',
        headers: paymentExtractorHeaders(true),
        body: JSON.stringify(input),
      }),
    )
  },

  async resolvePaymentExtractorPaypal(taskId: string): Promise<PaymentExtractorTask> {
    return parseResponse(
      await fetch(`/api/payment-extractor/tasks/${encodeURIComponent(taskId)}/resolve-paypal`, {
        method: 'POST',
        headers: paymentExtractorHeaders(),
      }),
    )
  },

  async deletePaymentExtractorTask(taskId: string): Promise<PaymentExtractorDeleteResult> {
    return parseResponse(
      await fetch(`/api/payment-extractor/tasks/${encodeURIComponent(taskId)}`, {
        method: 'DELETE',
        headers: paymentExtractorHeaders(),
      }),
    )
  },

  async bulkDeletePaymentExtractorTasks(
    target: 'failed' | 'succeeded',
  ): Promise<PaymentExtractorBulkDeleteResult> {
    return parseResponse(
      await fetch('/api/payment-extractor/tasks/bulk-delete', {
        method: 'POST',
        headers: paymentExtractorHeaders(true),
        body: JSON.stringify({ target }),
      }),
    )
  },

  async listAccounts(query: ResourceQuery): Promise<PageResponse<AccountRecord>> {
    return parseResponse(await fetch(`/api/accounts?${queryString(query)}`))
  },

  async listEmails(query: ResourceQuery): Promise<PageResponse<EmailRecord>> {
    return parseResponse(await fetch(`/api/emails?${queryString(query)}`))
  },

  async listProxies(query: ResourceQuery): Promise<PageResponse<ProxyRecord>> {
    return parseResponse(await fetch(`/api/proxies?${queryString(query)}`))
  },

  async importEmails(rawText: string): Promise<ImportResult> {
    return parseResponse(
      await fetch('/api/emails/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rawText }),
      }),
    )
  },

  async syncMailcomAliases(): Promise<ImportResult> {
    return parseResponse(
      await fetch('/api/emails/sync-mailcom-aliases', { method: 'POST' }),
    )
  },

  async importProxies(rawText: string, country?: string, group?: string): Promise<ImportResult> {
    return parseResponse(
      await fetch('/api/proxies/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          rawText,
          country: country?.trim().toUpperCase() || null,
          group: group?.trim() || null,
        }),
      }),
    )
  },

  async deleteAccounts(ids: string[]): Promise<number> {
    const result = await parseResponse<{ deleted: number }>(
      await fetch('/api/accounts/bulk-delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      }),
    )
    return result.deleted
  },

  async checkAccountPromotions(ids: string[]): Promise<AccountPlanCheckResult> {
    return parseResponse(
      await fetch('/api/accounts/check-promotion', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      }),
    )
  },

  async deleteEmails(ids: string[]): Promise<number> {
    const result = await parseResponse<{ deleted: number }>(
      await fetch('/api/emails/bulk-delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      }),
    )
    return result.deleted
  },

  async exportAccounts(
    format: ExportFormat,
    scope: ExportScope,
    ids: string[],
  ): Promise<AccountExport> {
    return parseResponse(
      await fetch('/api/accounts/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ format, scope, ids }),
      }),
    )
  },

  async exportEmails(scope: ExportScope, ids: string[]): Promise<EmailExport> {
    return parseResponse(
      await fetch('/api/emails/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope, ids }),
      }),
    )
  },

  async setProxyEnabled(id: string, enabled: boolean): Promise<ProxyRecord> {
    return parseResponse(
      await fetch(`/api/proxies/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      }),
    )
  },

  async setProxyCountry(id: string, country: string): Promise<ProxyRecord> {
    return parseResponse(
      await fetch(`/api/proxies/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ country: country.trim().toUpperCase() }),
      }),
    )
  },

  async listProxyCountries(): Promise<ProxyCountrySummary[]> {
    return parseResponse(await fetch('/api/proxies/countries'))
  },

  async listProxyGroups(): Promise<ProxyGroupSummary[]> {
    return parseResponse(await fetch('/api/proxies/groups'))
  },

  async updateProxyGroup(input: {
    country: string
    group: string
    newCountry?: string
    newGroup?: string
    enabled?: boolean
  }): Promise<{ matched: number; modified: number }> {
    return parseResponse(await fetch('/api/proxies/groups', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    }))
  },

  async deleteProxyGroup(country: string, group: string): Promise<number> {
    const params = new URLSearchParams({ country, group })
    const result = await parseResponse<{ deleted: number }>(
      await fetch(`/api/proxies/groups?${params}`, { method: 'DELETE' }),
    )
    return result.deleted
  },

  async deleteProxy(id: string): Promise<number> {
    const result = await parseResponse<{ deleted: number }>(
      await fetch(`/api/proxies/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    )
    return result.deleted
  },

  async deleteProxies(ids: string[]): Promise<number> {
    const result = await parseResponse<{ deleted: number }>(
      await fetch('/api/proxies/bulk-delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      }),
    )
    return result.deleted
  },

  async clearProxies(): Promise<number> {
    const result = await parseResponse<{ deleted: number }>(
      await fetch('/api/proxies', { method: 'DELETE' }),
    )
    return result.deleted
  },

  async overviewStats(): Promise<OverviewStats> {
    return parseResponse(await fetch('/api/stats/overview'))
  },

  async health(): Promise<HealthResponse> {
    return parseResponse(await fetch('/api/health'))
  },

  async startMockRun(count: number): Promise<RunExecutionState> {
    return parseResponse(
      await fetch('/api/runs/mock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ count }),
      }),
    )
  },

  async startBrowserProbeRun(
    count: number,
    country: string,
    group: string,
    emailSource: EmailSource = 'all',
  ): Promise<RunExecutionState> {
    return parseResponse(
      await fetch('/api/runs/browser-probe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          count,
          country: country.trim().toUpperCase(),
          group: group.trim(),
          emailSource,
        }),
      }),
    )
  },

  async activeRun(): Promise<RunExecutionState | null> {
    return parseResponse(await fetch('/api/runs/active'))
  },

  async getRun(runId: string): Promise<RunExecutionState> {
    return parseResponse(await fetch(`/api/runs/${encodeURIComponent(runId)}`))
  },

  async listRunWorkers(runId: string): Promise<RunWorkerSnapshot[]> {
    return parseResponse(await fetch(`/api/runs/${encodeURIComponent(runId)}/workers`))
  },

  async cancelRun(runId: string): Promise<RunExecutionState> {
    return parseResponse(
      await fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' }),
    )
  },
}
