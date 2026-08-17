import { beforeEach, describe, expect, it, vi } from 'vitest'
import { dataGateway } from './dataGateway'

describe('dataGateway proxy deletion', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('sends selected proxy ids to the bulk-delete endpoint', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ deleted: 2 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await expect(dataGateway.deleteProxies(['proxy-1', 'proxy-2'])).resolves.toBe(2)
    expect(fetch).toHaveBeenCalledWith('/api/proxies/bulk-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: ['proxy-1', 'proxy-2'] }),
    })
  })

  it('clears the complete proxy collection through DELETE /api/proxies', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ deleted: 100 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await expect(dataGateway.clearProxies()).resolves.toBe(100)
    expect(fetch).toHaveBeenCalledWith('/api/proxies', { method: 'DELETE' })
  })
})

describe('dataGateway access token export', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('requests selected access tokens only through the explicit export endpoint', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          content: 'TEST_AT',
          filename: 'accounts-1-access-tokens-20260811-010000.txt',
          count: 1,
          format: 'access-tokens',
          skippedMissingCount: 1,
          skippedExpiredCount: 0,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    const result = await dataGateway.exportAccounts('access-tokens', 'selected', [
      'account-1',
      'account-2',
    ])

    expect(result.content).toBe('TEST_AT')
    expect(result.skippedMissingCount).toBe(1)
    expect(fetch).toHaveBeenCalledWith('/api/accounts/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        format: 'access-tokens',
        scope: 'selected',
        ids: ['account-1', 'account-2'],
      }),
    })
  })
})

describe('dataGateway paid account export', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('sends multiple paid export formats in one request', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({ formats: ['original', 'password_totp'], exports: [], errors: [], artifactCount: 0, failedFormatCount: 0 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await dataGateway.exportPaidPipeline(
      ['paid-1', 'paid-2'],
      'example.test',
      'unexported',
      ['original', 'password_totp'],
    )

    expect(fetch).toHaveBeenCalledWith('/api/pipeline/paid/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ids: ['paid-1', 'paid-2'],
        query: 'example.test',
        exportState: 'unexported',
        formats: ['original', 'password_totp'],
      }),
    })
  })
})

describe('dataGateway promotion checks', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('sends account ids to the promotion check endpoint', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          requested: 2,
          succeeded: 1,
          failed: 0,
          skipped: 1,
          items: [],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await dataGateway.checkAccountPromotions(['account-1', 'account-2'])

    expect(fetch).toHaveBeenCalledWith('/api/accounts/check-promotion', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: ['account-1', 'account-2'] }),
    })
  })

  it('adds the untried Plus filter to the account query', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0, page: 1, pageSize: 10 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await dataGateway.listAccounts({
      page: 1,
      pageSize: 10,
      q: '',
      promotion: 'untried_plus',
    })

    expect(fetch).toHaveBeenCalledWith(
      '/api/accounts?page=1&pageSize=10&promotion=untried_plus',
    )
  })
})

describe('dataGateway split-mail registration source', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('syncs aliases from the local MailCom Hub endpoint', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({ total: 2, imported: 2, duplicateCount: 0, errorCount: 0 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await expect(dataGateway.syncMailcomAliases()).resolves.toMatchObject({ imported: 2 })
    expect(fetch).toHaveBeenCalledWith('/api/emails/sync-mailcom-aliases', {
      method: 'POST',
    })
  })

  it('passes the selected split-mail source to the browser run', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          status: 'queued',
          runId: 'run-alias',
          kind: 'browser_probe',
          requested: 1,
          pending: 1,
          processed: 0,
          succeeded: 0,
          failed: 0,
          workerCount: 1,
          activeWorkers: 0,
          startedAt: '2026-08-17T00:00:00Z',
          updatedAt: '2026-08-17T00:00:00Z',
          finishedAt: null,
          logPersisted: true,
          cancelRequested: false,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await dataGateway.startBrowserProbeRun(1, 'gb', '英国组', 'mailcom_alias')
    expect(fetch).toHaveBeenCalledWith('/api/runs/browser-probe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        count: 1,
        country: 'GB',
        group: '英国组',
        emailSource: 'mailcom_alias',
      }),
    })
  })
})

describe('dataGateway payment extractor authentication', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal('fetch', vi.fn())
  })

  it('adds the saved workbench password only to payment extractor requests', async () => {
    localStorage.setItem('payment_link_extractor.workbench_password', 'fixture-password')
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          country: 'DE',
          paymentMethod: 'paypal',
          countries: [],
          paymentMethods: [],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await dataGateway.paymentExtractorDefaults()

    expect(fetch).toHaveBeenCalledWith('/api/payment-extractor/defaults', {
      headers: { 'X-Workbench-Password': 'fixture-password' },
    })
  })
})

describe('dataGateway agreement sidecar', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('starts the isolated protocol service through its own namespace', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          ok: true,
          status: 'online',
          service: 'paypal-agreement-protocol',
          sourceCommit: 'fixture',
          host: '127.0.0.1',
          port: 18098,
          uiPath: '/paypal-pay/',
          uiUrl: '/paypal-pay/',
          managed: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    const result = await dataGateway.startPaypalAgreement()

    expect(result.status).toBe('online')
    expect(fetch).toHaveBeenCalledWith('/api/paypal-agreement/start', { method: 'POST' })
  })
})

describe('dataGateway HeroSMS pipeline', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal('fetch', vi.fn())
  })

  it('tests HeroSMS without sending its API key from the browser', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({ ok: true, configured: true, country: 'JP', service: 'PayPal', balance: 12.34 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    const result = await dataGateway.testHeroSms()

    expect(result.balance).toBe(12.34)
    expect(fetch).toHaveBeenCalledWith('/api/pipeline/herosms/test', {
      method: 'POST',
      headers: {},
    })
    expect(JSON.stringify(vi.mocked(fetch).mock.calls)).not.toContain('api_key')
  })
})
