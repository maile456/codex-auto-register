import { defineStore } from 'pinia'
import { dataGateway } from '@/services/dataGateway'
import { runLogGateway } from '@/services/runLogGateway'
import { defaultExecutionSettings, settingsGateway } from '@/services/settingsGateway'
import type {
  AccountRecord,
  EmailRecord,
  EmailSource,
  ExecutionSettings,
  ExecutionSettingsInput,
  MongoHealth,
  OverviewStats,
  ProxyRecord,
  ProxyCountrySummary,
  ProxyGroupSummary,
  ResourceQuery,
  RunExecutionState,
  RunWorkerSnapshot,
  RunLogEntry,
  RunLogSummary,
} from '@/types'

const DEFAULT_QUERY: ResourceQuery = { page: 1, pageSize: 10, q: '' }

function emptyStats(): OverviewStats {
  return {
    accounts: {
      total: 0,
      today: 0,
      totpComplete: 0,
      plus: { total: 0, bound: 0, unbound: 0 },
      free: { total: 0, eligible: 0, ineligible: 0 },
    },
    emails: { available: 0, aliases: 0 },
    proxies: { total: 0, enabled: 0, available: 0, quarantined: 0 },
  }
}

function emptyRunState(): RunExecutionState {
  return {
    status: 'idle',
    runId: null,
    kind: 'browser_probe',
    requested: 0,
    pending: 0,
    processed: 0,
    succeeded: 0,
    failed: 0,
    workerCount: 0,
    activeWorkers: 0,
    startedAt: null,
    updatedAt: null,
    finishedAt: null,
    logPersisted: false,
    cancelRequested: false,
    terminalReasonCode: null,
  }
}

let monitorPromise: Promise<RunExecutionState> | null = null

export const useAppStore = defineStore('app', {
  state: () => ({
    accounts: [] as AccountRecord[],
    emails: [] as EmailRecord[],
    proxies: [] as ProxyRecord[],
    proxyCountries: [] as ProxyCountrySummary[],
    proxyGroups: [] as ProxyGroupSummary[],
    accountTotal: 0,
    emailTotal: 0,
    proxyTotal: 0,
    accountQuery: { ...DEFAULT_QUERY } as ResourceQuery,
    emailQuery: { ...DEFAULT_QUERY } as ResourceQuery,
    proxyQuery: { ...DEFAULT_QUERY } as ResourceQuery,
    stats: emptyStats(),
    settings: defaultExecutionSettings() as ExecutionSettings,
    initialized: false,
    configServiceOnline: false,
    configError: '' as string,
    mongoHealth: {
      status: 'offline',
      database: 'autoregister',
      error: null,
      nextRetrySeconds: null,
    } as MongoHealth,
    dataError: '' as string,
    logServiceOnline: false,
    logError: '' as string,
    runState: emptyRunState(),
    runWorkers: [] as RunWorkerSnapshot[],
    currentRunLogs: [] as RunLogEntry[],
    displayedLogs: [] as RunLogEntry[],
    selectedLogRunId: null as string | null,
    runHistory: [] as RunLogSummary[],
  }),

  actions: {
    async bootstrap() {
      if (this.initialized) return
      const [settings, runHistory, health] = await Promise.allSettled([
        settingsGateway.getExecutionSettings(),
        runLogGateway.listRuns(),
        dataGateway.health(),
      ])
      if (settings.status === 'fulfilled') {
        this.settings = settings.value
        this.configServiceOnline = true
      } else {
        this.settings = defaultExecutionSettings()
        this.configError = settings.reason instanceof Error ? settings.reason.message : '配置服务不可用'
      }
      if (runHistory.status === 'fulfilled') {
        this.runHistory = runHistory.value
        this.logServiceOnline = true
      } else {
        this.logError = runHistory.reason instanceof Error ? runHistory.reason.message : '日志服务不可用'
      }
      if (health.status === 'fulfilled') this.mongoHealth = health.value.mongodb
      else this.dataError = health.reason instanceof Error ? health.reason.message : 'FastAPI 不可用'
      this.initialized = true

      if (this.mongoHealth.status === 'online') {
        await Promise.all([
          this.refreshAccounts(),
          this.refreshEmails(),
          this.refreshProxies(),
          this.refreshProxyCountries(),
          this.refreshProxyGroups(),
          this.refreshStats(),
        ]).catch(() => undefined)
        const active = await dataGateway.activeRun().catch(() => null)
        if (active?.runId) {
          this.runState = active
          this.runWorkers = await dataGateway.listRunWorkers(active.runId).catch(() => [])
          void this.monitorRun(active.runId)
        }
      }
      const latest = this.runHistory[0]
      if (latest) await this.openRunLog(latest.runId).catch(() => undefined)
    },

    async refreshHealth() {
      const previous = this.mongoHealth.status
      try {
        const health = await dataGateway.health()
        this.mongoHealth = health.mongodb
        if (health.mongodb.status === 'online') {
          this.dataError = ''
          if (previous !== 'online') {
            await Promise.all([
              this.refreshAccounts(),
              this.refreshEmails(),
              this.refreshProxies(),
              this.refreshProxyCountries(),
              this.refreshProxyGroups(),
              this.refreshStats(),
            ])
          }
        }
      } catch (error) {
        this.mongoHealth.status = 'offline'
        this.dataError = error instanceof Error ? error.message : 'FastAPI 不可用'
      }
    },

    async refreshAccounts(query?: ResourceQuery) {
      if (query) this.accountQuery = { ...query }
      const result = await dataGateway.listAccounts(this.accountQuery)
      this.accounts = result.items
      this.accountTotal = result.total
      return result
    },

    async refreshEmails(query?: ResourceQuery) {
      if (query) this.emailQuery = { ...query }
      const result = await dataGateway.listEmails(this.emailQuery)
      this.emails = result.items
      this.emailTotal = result.total
      return result
    },

    async refreshProxies(query?: ResourceQuery) {
      if (query) this.proxyQuery = { ...query }
      const result = await dataGateway.listProxies(this.proxyQuery)
      this.proxies = result.items
      this.proxyTotal = result.total
      return result
    },

    async refreshProxyCountries() {
      this.proxyCountries = await dataGateway.listProxyCountries()
      return this.proxyCountries
    },

    async refreshProxyGroups() {
      this.proxyGroups = await dataGateway.listProxyGroups()
      return this.proxyGroups
    },

    async refreshStats() {
      this.stats = await dataGateway.overviewStats()
      return this.stats
    },

    async importEmails(rawText: string) {
      const result = await dataGateway.importEmails(rawText)
      await Promise.all([this.refreshEmails(), this.refreshStats()])
      return result
    },

    async syncMailcomAliases() {
      const result = await dataGateway.syncMailcomAliases()
      await Promise.all([this.refreshEmails(), this.refreshStats()])
      return result
    },

    async importProxies(rawText: string, country?: string, group?: string) {
      const result = await dataGateway.importProxies(rawText, country, group)
      await Promise.all([this.refreshProxies(), this.refreshProxyCountries(), this.refreshProxyGroups(), this.refreshStats()])
      return result
    },

    async updateProxyGroup(input: {
      country: string
      group: string
      newCountry?: string
      newGroup?: string
      enabled?: boolean
    }) {
      const result = await dataGateway.updateProxyGroup(input)
      await Promise.all([this.refreshProxyCountries(), this.refreshProxyGroups(), this.refreshStats()])
      return result
    },

    async deleteProxyGroup(country: string, group: string) {
      const deleted = await dataGateway.deleteProxyGroup(country, group)
      await Promise.all([this.refreshProxyCountries(), this.refreshProxyGroups(), this.refreshStats()])
      return deleted
    },

    async deleteAccounts(ids: string[]) {
      const deleted = await dataGateway.deleteAccounts(ids)
      await Promise.all([this.refreshAccounts(), this.refreshStats()])
      return deleted
    },

    async checkAccountPromotions(ids: string[]) {
      const result = await dataGateway.checkAccountPromotions(ids)
      await Promise.all([this.refreshAccounts(), this.refreshStats()])
      return result
    },

    async deleteEmails(ids: string[]) {
      const deleted = await dataGateway.deleteEmails(ids)
      await Promise.all([this.refreshEmails(), this.refreshStats()])
      return deleted
    },

    async saveSettings(input: ExecutionSettingsInput) {
      try {
        this.settings = await settingsGateway.updateExecutionSettings(input)
        this.configServiceOnline = true
        this.configError = ''
        return this.settings
      } catch (error) {
        this.configServiceOnline = false
        this.configError = error instanceof Error ? error.message : '配置保存失败'
        throw error
      }
    },

    async setProxyEnabled(id: string, enabled: boolean) {
      await dataGateway.setProxyEnabled(id, enabled)
      await Promise.all([this.refreshProxies(), this.refreshProxyCountries(), this.refreshStats()])
    },

    async setProxyCountry(id: string, country: string) {
      await dataGateway.setProxyCountry(id, country)
      await Promise.all([this.refreshProxies(), this.refreshProxyCountries()])
    },

    async deleteProxy(id: string) {
      const deleted = await dataGateway.deleteProxy(id)
      await Promise.all([this.refreshProxies(), this.refreshProxyCountries(), this.refreshStats()])
      return deleted
    },

    async deleteProxies(ids: string[]) {
      const deleted = await dataGateway.deleteProxies(ids)
      await Promise.all([this.refreshProxies(), this.refreshProxyCountries(), this.refreshStats()])
      return deleted
    },

    async clearProxies() {
      const deleted = await dataGateway.clearProxies()
      await Promise.all([this.refreshProxies(), this.refreshProxyCountries(), this.refreshStats()])
      return deleted
    },

    async loadRunHistory() {
      try {
        this.runHistory = await runLogGateway.listRuns()
        this.logServiceOnline = true
        this.logError = ''
        return this.runHistory
      } catch (error) {
        this.logServiceOnline = false
        this.logError = error instanceof Error ? error.message : '日志服务不可用'
        throw error
      }
    },

    async openRunLog(runId: string) {
      try {
        const result = await runLogGateway.getRun(runId)
        this.selectedLogRunId = runId
        this.displayedLogs = result.entries
        if (runId === this.runState.runId) this.currentRunLogs = result.entries
        this.logServiceOnline = true
        this.logError = ''
      } catch (error) {
        this.logServiceOnline = false
        this.logError = error instanceof Error ? error.message : '无法读取任务日志'
        throw error
      }
    },

    async startMockRun(count: number) {
      if (['queued', 'running', 'waiting_for_database'].includes(this.runState.status)) {
        throw new Error('已有任务正在运行')
      }
      this.runWorkers = []
      this.runState = await dataGateway.startMockRun(count)
      if (!this.runState.runId) throw new Error('任务服务未返回 runId')
      this.selectedLogRunId = this.runState.runId
      await this.openRunLog(this.runState.runId).catch(() => undefined)
      return this.monitorRun(this.runState.runId)
    },

    async startBrowserProbeRun(
      count: number,
      country: string,
      group: string,
      emailSource: EmailSource = 'all',
    ) {
      if (['queued', 'running', 'waiting_for_database'].includes(this.runState.status)) {
        throw new Error('已有任务正在运行')
      }
      this.runWorkers = []
      this.runState = await dataGateway.startBrowserProbeRun(count, country, group, emailSource)
      if (!this.runState.runId) throw new Error('任务服务未返回 runId')
      this.selectedLogRunId = this.runState.runId
      await Promise.all([
        this.openRunLog(this.runState.runId).catch(() => undefined),
        dataGateway
          .listRunWorkers(this.runState.runId)
          .then((workers) => {
            this.runWorkers = workers
          })
          .catch(() => undefined),
      ])
      return this.monitorRun(this.runState.runId)
    },

    async cancelRun() {
      if (!this.runState.runId) throw new Error('当前没有可取消的任务')
      this.runState = await dataGateway.cancelRun(this.runState.runId)
      return this.runState
    },

    async monitorRun(runId: string): Promise<RunExecutionState> {
      if (monitorPromise) return monitorPromise
      monitorPromise = (async () => {
        while (true) {
          const [state, workers] = await Promise.all([
            dataGateway.getRun(runId),
            dataGateway.listRunWorkers(runId).catch(() => this.runWorkers),
          ])
          this.runState = state
          this.runWorkers = workers
          await this.openRunLog(runId).catch(() => undefined)
          if (!['queued', 'running', 'waiting_for_database'].includes(state.status)) break
          await new Promise((resolve) => setTimeout(resolve, 500))
        }
        await Promise.all([
          this.refreshAccounts(),
          this.refreshEmails(),
          this.refreshStats(),
          this.loadRunHistory(),
        ]).catch(() => undefined)
        return { ...this.runState }
      })()
      try {
        return await monitorPromise
      } finally {
        monitorPromise = null
      }
    },
  },
})
