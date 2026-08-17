import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import router from '@/router'
import { useAppStore } from '@/stores/app'
import LaunchView from './LaunchView.vue'

describe('LaunchView', () => {
  it('passes the selected registration country when starting a run', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.stats.emails.available = 2
    store.mongoHealth.status = 'online'
    store.settings.concurrency = 1
    store.proxyCountries = [
      { country: 'TR', total: 2, enabled: 2 },
      { country: 'JP', total: 1, enabled: 1 },
    ]
    store.proxyGroups = [
      { country: 'TR', group: 'TR-A', total: 2, enabled: 2, available: 2, quarantined: 0, schemes: ['http'] },
      { country: 'JP', group: 'JP-A', total: 1, enabled: 1, available: 1, quarantined: 0, schemes: ['http'] },
    ]
    const start = vi.spyOn(store, 'startBrowserProbeRun').mockResolvedValue({
      ...store.runState,
      status: 'completed',
      runId: 'run-country',
      requested: 1,
      pending: 0,
      processed: 1,
      succeeded: 1,
      failed: 0,
    })

    const wrapper = mount(LaunchView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await nextTick()
    const countrySelect = wrapper.findAllComponents({ name: 'ElSelect' })[1]
    countrySelect!.vm.$emit('update:modelValue', 'JP')
    await nextTick()
    await wrapper.get('.start-button').trigger('click')
    await flushPromises()

    expect(start).toHaveBeenCalledWith(1, 'JP', 'JP-A', 'all')
  })

  it('renders pool-wide Plus, Free and available-email statistics', () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.stats.emails.available = 3
    store.stats.accounts.plus = { total: 2, bound: 1, unbound: 1 }
    store.stats.accounts.free = { total: 2, eligible: 1, ineligible: 1 }

    const wrapper = mount(LaunchView, {
      global: { plugins: [pinia, ElementPlus] },
    })

    expect(wrapper.get('.metric-card--mail > strong').text()).toBe('3')
    expect(wrapper.get('.metric-card--plus > strong').text()).toBe('2')
    expect(wrapper.get('.metric-card--plus').text()).toContain('已接码 1')
    expect(wrapper.get('.metric-card--plus').text()).toContain('未接码 1')
    expect(wrapper.get('.metric-card--free > strong').text()).toBe('2')
    expect(wrapper.get('.metric-card--free').text()).toContain('有优惠资格 1')
    expect(wrapper.get('.metric-card--free').text()).toContain('无优惠资格 1')
    expect(wrapper.text()).not.toContain('masked-in-this-view')
  })

  it('disables starting when the email pool is empty', () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.stats.emails.available = 0

    const wrapper = mount(LaunchView, {
      global: { plugins: [pinia, ElementPlus] },
    })

    expect(wrapper.get('.start-button').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('邮箱池为空，请先导入邮箱')
    expect(wrapper.get('.summary-pending strong').text()).toBe('0')
  })

  it('shows the remaining pending count and never displays a negative value', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.stats.emails.available = 4
    store.mongoHealth.status = 'online'

    const wrapper = mount(LaunchView, {
      global: { plugins: [pinia, ElementPlus] },
    })

    expect(wrapper.get('.summary-pending strong').text()).toBe('1')
    store.runState = {
      status: 'running',
      runId: 'run-test',
      kind: 'browser_probe',
      requested: 4,
      pending: 3,
      processed: 1,
      succeeded: 1,
      failed: 0,
      workerCount: 2,
      activeWorkers: 1,
      startedAt: '2026-08-08T08:00:00.000Z',
      updatedAt: '2026-08-08T08:00:01.000Z',
      finishedAt: null,
      logPersisted: false,
      cancelRequested: false,
    }
    await nextTick()
    expect(wrapper.get('.summary-pending strong').text()).toBe('3')

    store.runState.status = 'completed'
    store.runState.processed = 4
    store.runState.pending = 0
    await nextTick()
    expect(wrapper.get('.summary-pending strong').text()).toBe('0')

    store.runState.processed = 5
    await nextTick()
    expect(wrapper.get('.summary-pending strong').text()).toBe('0')
  })

  it('shows success rate separately from task progress', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.stats.emails.available = 20
    store.mongoHealth.status = 'online'
    store.runState = {
      status: 'running',
      runId: 'run-success-rate',
      kind: 'browser_probe',
      requested: 20,
      pending: 10,
      processed: 10,
      succeeded: 8,
      failed: 2,
      workerCount: 2,
      activeWorkers: 2,
      startedAt: '2026-08-08T08:00:00.000Z',
      updatedAt: '2026-08-08T08:00:10.000Z',
      finishedAt: null,
      logPersisted: true,
      cancelRequested: false,
    }

    const wrapper = mount(LaunchView, {
      global: { plugins: [pinia, ElementPlus] },
    })

    const successRateTag = wrapper.get('.success-rate-tag')
    expect(successRateTag.text()).toBe('成功率 80%')
    expect(wrapper.getComponent({ name: 'ElProgress' }).props('percentage')).toBe(50)

    store.runState.processed = 0
    store.runState.succeeded = 0
    await nextTick()
    expect(successRateTag.text()).toBe('成功率 0%')
  })

  it('shows full worker email, egress IP, stage and one shared workspace', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.stats.emails.available = 4
    store.settings.concurrency = 2
    store.runState.workerCount = 2
    store.runState.activeWorkers = 1
    store.runWorkers = [
      {
        workerId: 'worker-1',
        sequence: 1,
        status: 'running',
        stage: 'verification',
        stageElapsedMs: 2500,
        email: 'full.worker@example.com',
        egressIp: '203.0.113.88',
        errorCode: null,
        startedAt: '2026-08-11T00:00:00.000Z',
        updatedAt: '2026-08-11T00:00:02.500Z',
        finishedAt: null,
      },
    ]

    const wrapper = mount(LaunchView, {
      global: { plugins: [pinia, ElementPlus] },
    })

    expect(wrapper.get('.worker-overview').text()).toContain('1 / 2')
    expect(wrapper.get('.worker-email').text()).toBe('full.worker@example.com')
    expect(wrapper.get('.worker-ip').text()).toBe('203.0.113.88')
    expect(wrapper.get('.worker-row').text()).toContain('验证码 · 2.5s')
    expect(wrapper.text()).toContain('需要 workspace 1')
    await wrapper.getComponent({ name: 'ElInputNumber' }).setValue(4)
    await nextTick()
    expect(wrapper.text()).toContain('并发 2')
    expect(wrapper.text()).toContain('需要 workspace 1')
  })

  it('shows the Roxy circuit alert and redacted worker diagnostics', () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.runState.status = 'failed'
    store.runState.terminalReasonCode = 'roxy_circuit_open'
    store.runWorkers = [
      {
        workerId: 'worker-circuit',
        sequence: 5,
        status: 'failed',
        stage: 'failed',
        stageElapsedMs: 0,
        email: 'worker@example.com',
        egressIp: null,
        errorCode: 'roxy_workspace_not_ready',
        errorStage: 'roxy_workspace',
        errorOperation: 'workspace_list',
        errorKind: 'transport',
        errorHttpStatus: null,
        errorApiCode: null,
        errorRetryCount: 12,
        errorElapsedMs: 30_000,
        startedAt: '2026-08-12T00:00:00.000Z',
        updatedAt: '2026-08-12T00:00:30.000Z',
        finishedAt: '2026-08-12T00:00:30.000Z',
      },
    ]

    const wrapper = mount(LaunchView, {
      global: { plugins: [pinia, ElementPlus] },
    })

    expect(wrapper.get('.circuit-alert').text()).toContain(
      'Roxy 连续异常，任务已安全终止；未处理邮箱已释放，请重启 Roxy 后重新发起。',
    )
    expect(wrapper.get('.worker-diagnostic').text()).toBe(
      'roxy_workspace_not_ready · roxy_workspace · workspace_list',
    )
  })

  it('registers launch as the default route', () => {
    const routes = router.getRoutes()
    expect(routes.find((route) => route.path === '/')?.redirect).toBe('/launch')
    expect(routes.find((route) => route.path === '/launch')?.name).toBe('launch')
    expect(routes.find((route) => route.path === '/:pathMatch(.*)*')?.redirect).toBe('/launch')
  })

  it('shows a cancel control for an active run', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useAppStore()
    store.stats.emails.available = 4
    store.mongoHealth.status = 'online'
    store.runState = {
      status: 'running',
      runId: '7b55cd2d-b835-4e02-8df0-57526e7ca734',
      kind: 'browser_probe',
      requested: 4,
      pending: 4,
      processed: 0,
      succeeded: 0,
      failed: 0,
      workerCount: 2,
      activeWorkers: 2,
      startedAt: '2026-08-08T08:00:00.000Z',
      updatedAt: '2026-08-08T08:00:00.000Z',
      finishedAt: null,
      logPersisted: true,
      cancelRequested: false,
    }
    const cancel = vi.spyOn(store, 'cancelRun').mockResolvedValue(store.runState)
    const wrapper = mount(LaunchView, {
      global: { plugins: [pinia, ElementPlus] },
    })

    await wrapper.get('.cancel-button').trigger('click')
    expect(cancel).toHaveBeenCalledOnce()
  })
})
