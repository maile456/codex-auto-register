<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import {
  CircleCheck,
  CircleClose,
  DocumentCopy,
  Download,
  Message,
  Refresh,
  UserFilled,
  VideoPlay,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { copyText, downloadTextFile } from '@/services/exporter'
import { useAppStore } from '@/stores/app'
import { countryLabel } from '@/services/countries'
import type { EmailSource } from '@/types'

const store = useAppStore()
const requestedCount = ref<number | undefined>(1)
const selectedCountry = ref((() => {
  try {
    return localStorage.getItem('autoregister.registrationCountry') || ''
  } catch {
    return ''
  }
})())
const selectedGroup = ref((() => {
  try {
    return localStorage.getItem('autoregister.registrationProxyGroup') || ''
  } catch {
    return ''
  }
})())
const selectedEmailSource = ref<EmailSource>((() => {
  try {
    const saved = localStorage.getItem('autoregister.emailSource')
    return saved === 'standard' || saved === 'mailcom_alias' ? saved : 'all'
  } catch {
    return 'all'
  }
})())
const syncingAliases = ref(false)
const logViewport = ref<HTMLElement>()

const availableForSource = computed(() => {
  if (selectedEmailSource.value === 'mailcom_alias') return store.stats.emails.aliases
  if (selectedEmailSource.value === 'standard') {
    return Math.max(0, store.stats.emails.available - store.stats.emails.aliases)
  }
  return store.stats.emails.available
})

const running = computed(() =>
  ['queued', 'running', 'waiting_for_database'].includes(store.runState.status),
)
const countValid = computed(
  () =>
    Number.isInteger(requestedCount.value) &&
    Number(requestedCount.value) >= 1 &&
    Number(requestedCount.value) <= availableForSource.value &&
    Boolean(selectedCountry.value) &&
    Boolean(selectedGroup.value) &&
    (store.proxyGroups.find(
      (item) => item.country === selectedCountry.value && item.group === selectedGroup.value,
    )?.enabled || 0) >=
      Math.min(Number(requestedCount.value) || 1, store.settings.concurrency) &&
    store.mongoHealth.status === 'online',
)
const groupOptions = computed(() =>
  store.proxyGroups
    .filter((item) => item.country === selectedCountry.value && item.enabled > 0)
    .map((item) => ({ ...item, label: `${item.group} · ${item.enabled} 条启用代理` })),
)
const countryOptions = computed(() =>
  store.proxyCountries
    .filter((item) => item.country !== 'ZZ' && item.enabled > 0)
    .map((item) => ({
      ...item,
      label: `${countryLabel(item.country)} · ${item.enabled} 条启用代理`,
    })),
)
const progress = computed(() =>
  store.runState.requested
    ? Math.round((store.runState.processed / store.runState.requested) * 100)
    : 0,
)
const successRate = computed(() =>
  store.runState.processed
    ? Math.round((store.runState.succeeded / store.runState.processed) * 100)
    : 0,
)
const workspaceRequired = computed(() =>
  Math.max(0, Number(requestedCount.value) || 0) > 0 ? 1 : 0,
)
const pendingCount = computed(() => {
  if (store.runState.status === 'idle') {
    return Math.max(0, Number(requestedCount.value) || 0)
  }
  return Math.max(0, store.runState.pending)
})
const logText = computed(() => store.displayedLogs.map((entry) => JSON.stringify(entry)).join('\n'))
const selectedHistory = computed(() =>
  store.runHistory.find((item) => item.runId === store.selectedLogRunId),
)
const historyOptions = computed(() => {
  if (
    store.runState.runId &&
    !store.runHistory.some((item) => item.runId === store.runState.runId)
  ) {
    return [
      {
        runId: store.runState.runId,
        filename: `run-${store.runState.runId}.jsonl`,
        startedAt: store.runState.startedAt ?? new Date().toISOString(),
        updatedAt: store.runState.finishedAt ?? store.runState.startedAt ?? new Date().toISOString(),
        entryCount: store.currentRunLogs.length,
        lastEvent: store.currentRunLogs.at(-1)?.event ?? 'run_created',
      },
      ...store.runHistory,
    ]
  }
  return store.runHistory
})

watch(
  availableForSource,
  (available) => {
    if (running.value) return
    if (available === 0) requestedCount.value = undefined
    else if (!requestedCount.value || requestedCount.value > available) requestedCount.value = 1
  },
  { immediate: true },
)

watch(
  countryOptions,
  (options) => {
    if (!options.some((item) => item.country === selectedCountry.value)) {
      selectedCountry.value = options[0]?.country || ''
    }
  },
  { immediate: true },
)

watch(selectedCountry, (value) => {
  try {
    if (value) localStorage.setItem('autoregister.registrationCountry', value)
  } catch {
    // Local storage is optional.
  }
})

watch(
  groupOptions,
  (options) => {
    if (!options.some((item) => item.group === selectedGroup.value)) {
      selectedGroup.value = options[0]?.group || ''
    }
  },
  { immediate: true },
)

watch(selectedGroup, (value) => {
  try {
    if (value) localStorage.setItem('autoregister.registrationProxyGroup', value)
  } catch {
    // Local storage is optional.
  }
})

watch(selectedEmailSource, (value) => {
  try {
    localStorage.setItem('autoregister.emailSource', value)
  } catch {
    // Local storage is optional.
  }
})

watch(
  () => store.displayedLogs.length,
  async () => {
    if (store.selectedLogRunId !== store.runState.runId) return
    await nextTick()
    if (logViewport.value) logViewport.value.scrollTop = logViewport.value.scrollHeight
  },
)

function formatTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function runStatusLabel() {
  if (store.runState.status === 'queued') return '排队中'
  if (store.runState.status === 'running') return '运行中'
  if (store.runState.status === 'waiting_for_database') return '等待数据库恢复'
  if (store.runState.status === 'completed') return '已完成'
  if (store.runState.status === 'failed') return '异常结束'
  if (store.runState.status === 'cancelled') return '已取消'
  if (store.runState.status === 'interrupted') return '服务中断'
  return '等待启动'
}

function logLevelLabel(level: string) {
  if (level === 'success') return '成功'
  if (level === 'warning') return '警告'
  if (level === 'error') return '错误'
  return '信息'
}

function workerStageLabel(stage: string) {
  const labels: Record<string, string> = {
    queued: '排队',
    roxy_starting: '启动 Roxy',
    proxy_check: '检查代理',
    login: '登录页',
    email: '提交邮箱',
    verification: '验证码',
    profile: '账号资料',
    access_token: '提取 AT',
    two_factor: '设置 2FA',
    password_setup: '添加密码',
    cleanup: '清理资源',
    success: '成功',
    partial_success: '部分成功',
    failed: '失败',
    cancelled: '已取消',
  }
  return labels[stage] ?? stage
}

function workerDiagnostic(worker: (typeof store.runWorkers)[number]) {
  const parts = [worker.errorCode, worker.errorStage, worker.errorOperation].filter(Boolean)
  return parts.join(' · ')
}

function formatElapsed(milliseconds: number) {
  if (milliseconds < 1000) return `${milliseconds}ms`
  return `${(milliseconds / 1000).toFixed(1)}s`
}

async function startRun() {
  if (!countValid.value || requestedCount.value === undefined) {
    ElMessage.warning('请输入不超过当前可用邮箱数的正整数')
    return
  }
  try {
    const result = await store.startBrowserProbeRun(
      requestedCount.value,
      selectedCountry.value,
      selectedGroup.value,
      selectedEmailSource.value,
    )
    if (result.status === 'completed') {
      ElMessage.success(`真实探测完成：成功 ${result.succeeded}，失败 ${result.failed}`)
    } else {
      ElMessage.warning(`真实探测结束：${runStatusLabel()}`)
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '真实探测启动失败')
  }
}

async function syncAliases() {
  syncingAliases.value = true
  try {
    const result = await store.syncMailcomAliases()
    ElMessage.success(`分裂邮箱同步完成：新增 ${result.imported}，已存在 ${result.duplicateCount}`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '分裂邮箱同步失败')
  } finally {
    syncingAliases.value = false
  }
}

async function cancelRun() {
  if (!running.value || store.runState.cancelRequested) return
  try {
    await store.cancelRun()
    ElMessage.warning('取消请求已提交，正在释放未处理邮箱')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '取消任务失败')
  }
}

async function selectHistory(runId: string) {
  try {
    await store.openRunLog(runId)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '日志读取失败')
  }
}

async function refreshHistory() {
  try {
    await store.loadRunHistory()
    ElMessage.success('日志列表已刷新')
  } catch (error) {
    ElMessage.warning(error instanceof Error ? error.message : '日志服务不可用')
  }
}

async function copyLogs() {
  if (!logText.value) return
  try {
    await copyText(logText.value)
    ElMessage.success(`已复制 ${store.displayedLogs.length} 条 JSONL 日志`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '复制失败')
  }
}

function downloadLogs() {
  if (!logText.value) return
  const filename = selectedHistory.value?.filename ?? `run-${store.selectedLogRunId ?? 'local'}.jsonl`
  downloadTextFile(logText.value, filename)
  ElMessage.success(`已下载 ${filename}`)
}
</script>

<template>
  <section>
    <div class="page-heading launch-heading">
      <div>
        <h2>任务启动台</h2>
        <p>FastAPI 使用 Windows 多进程调度真实 Roxy/Playwright 探测，每个邮箱使用全新 worker。</p>
      </div>
      <el-tag type="info" effect="dark" round>固定生成 Free 账号</el-tag>
    </div>

    <div class="launch-metrics">
      <article class="panel metric-card metric-card--mail">
        <div class="metric-title"><span>当前可用邮箱</span><el-icon><Message /></el-icon></div>
        <strong>{{ store.stats.emails.available }}</strong>
        <p>其中分裂邮箱 {{ store.stats.emails.aliases }} 个</p>
      </article>

      <article class="panel metric-card metric-card--plus">
        <div class="metric-title"><span>Plus 账号</span><el-icon><UserFilled /></el-icon></div>
        <strong>{{ store.stats.accounts.plus.total }}</strong>
        <div class="metric-breakdown">
          <span><i class="dot dot--green" />已接码 {{ store.stats.accounts.plus.bound }}</span>
          <span><i class="dot dot--amber" />未接码 {{ store.stats.accounts.plus.unbound }}</span>
        </div>
        <small>接码状态表示是否已绑定手机号</small>
      </article>

      <article class="panel metric-card metric-card--free">
        <div class="metric-title"><span>Free 账号</span><el-icon><UserFilled /></el-icon></div>
        <strong>{{ store.stats.accounts.free.total }}</strong>
        <div class="metric-breakdown">
          <span><i class="dot dot--green" />有优惠资格 {{ store.stats.accounts.free.eligible }}</span>
          <span><i class="dot dot--muted" />无优惠资格 {{ store.stats.accounts.free.ineligible }}</span>
        </div>
        <small>真实探测成功后写入账号池</small>
      </article>
    </div>

    <div class="launch-workspace">
      <article class="panel start-panel">
        <div class="section-heading">
          <div class="section-icon"><el-icon><VideoPlay /></el-icon></div>
          <div><h3>启动真实探测</h3><p>失败邮箱释放回池，成功邮箱自动进入账号池。</p></div>
        </div>

        <div class="run-form">
          <label>邮箱来源</label>
          <div class="source-row">
            <el-select v-model="selectedEmailSource" :disabled="running" aria-label="注册邮箱来源">
              <el-option label="全部邮箱" value="all" />
              <el-option label="仅普通邮箱" value="standard" />
              <el-option label="仅分裂邮箱" value="mailcom_alias" />
            </el-select>
            <el-button :icon="Refresh" :loading="syncingAliases" :disabled="running" @click="syncAliases">
              同步分裂邮箱
            </el-button>
          </div>
          <label>注册国家 / 代理池</label>
          <el-select
            v-model="selectedCountry"
            filterable
            placeholder="请选择注册国家"
            :disabled="running"
          >
            <el-option
              v-for="option in countryOptions"
              :key="option.country"
              :label="option.label"
              :value="option.country"
            />
          </el-select>
          <label>代理分组</label>
          <el-select
            v-model="selectedGroup"
            filterable
            placeholder="请选择代理分组"
            :disabled="running || !selectedCountry"
          >
            <el-option
              v-for="option in groupOptions"
              :key="`${option.country}-${option.group}`"
              :label="option.label"
              :value="option.group"
            />
          </el-select>
          <label>本次待处理数量</label>
          <el-input-number
            v-model="requestedCount"
            :min="1"
            :max="Math.max(1, availableForSource)"
            :step="1"
            :disabled="running || availableForSource === 0 || store.mongoHealth.status !== 'online'"
            controls-position="right"
          />
          <div class="run-hints">
            <span>当前来源可用 {{ availableForSource }}</span>
            <span>邮箱来源 {{ selectedEmailSource === 'mailcom_alias' ? '分裂邮箱' : selectedEmailSource === 'standard' ? '普通邮箱' : '全部' }}</span>
            <span>并发 {{ store.settings.concurrency }}</span>
            <span>注册国家 {{ selectedCountry ? countryLabel(selectedCountry) : '未选择' }}</span>
            <span>代理分组 {{ selectedGroup || '未选择' }}</span>
            <span>需要 workspace {{ workspaceRequired }}</span>
            <span>类型 Free</span>
          </div>
          <div class="run-actions">
            <el-button
              class="start-button"
              type="primary"
              size="large"
              :icon="VideoPlay"
              :loading="running && !store.runState.cancelRequested"
              :disabled="!countValid || running"
              @click="startRun"
            >
              {{ store.runState.status === 'waiting_for_database' ? '等待数据库恢复' : running ? '真实探测运行中' : '启动真实探测' }}
            </el-button>
            <el-button
              v-if="running"
              class="cancel-button"
              type="danger"
              size="large"
              plain
              :loading="store.runState.cancelRequested"
              :disabled="store.runState.cancelRequested"
              @click="cancelRun"
            >
              {{ store.runState.cancelRequested ? '正在取消' : '取消任务' }}
            </el-button>
          </div>
          <el-alert
            v-if="availableForSource === 0"
            type="warning"
            :closable="false"
            :title="selectedEmailSource === 'mailcom_alias' ? '分裂邮箱池为空，请先同步 MailCom Hub' : selectedEmailSource === 'standard' ? '普通邮箱池为空，请先导入邮箱' : '邮箱池为空，请先导入邮箱'"
            show-icon
          />
          <el-alert
            v-else-if="countryOptions.length === 0"
            type="warning"
            :closable="false"
            title="没有已分类且启用的注册代理，请先到配置栏设置代理国家"
            show-icon
          />
          <el-alert
            v-else-if="store.mongoHealth.status !== 'online'"
            type="error"
            :closable="false"
            title="MongoDB 当前不可用，恢复连接后可启动任务"
            show-icon
          />
        </div>
      </article>

      <article class="panel progress-panel">
        <div class="progress-heading">
          <div><span>本次任务</span><strong>{{ runStatusLabel() }}</strong></div>
          <el-tag class="success-rate-tag" :type="store.runState.status === 'failed' ? 'danger' : running ? 'warning' : 'success'" effect="dark" round>
            成功率 {{ successRate }}%
          </el-tag>
        </div>
        <el-progress
          :percentage="progress"
          :status="store.runState.status === 'failed' ? 'exception' : store.runState.status === 'completed' ? 'success' : undefined"
          :stroke-width="10"
        />
        <el-alert
          v-if="store.runState.terminalReasonCode === 'roxy_circuit_open'"
          class="circuit-alert"
          type="error"
          :closable="false"
          title="Roxy 连续异常，任务已安全终止；未处理邮箱已释放，请重启 Roxy 后重新发起。"
          show-icon
        />
        <div class="run-summary">
          <div class="summary-pending"><span>待处理</span><strong>{{ pendingCount }}</strong></div>
          <div><span>已处理</span><strong>{{ store.runState.processed }}</strong></div>
          <div class="summary-success"><span>成功</span><strong>{{ store.runState.succeeded }}</strong></div>
          <div class="summary-failed"><span>失败</span><strong>{{ store.runState.failed }}</strong></div>
        </div>
        <div class="worker-overview">
          <span>活动 worker</span>
          <strong>{{ store.runState.activeWorkers }} / {{ store.runState.workerCount }}</strong>
        </div>
        <div v-if="store.runWorkers.length" class="worker-list">
          <div
            v-for="worker in store.runWorkers"
            :key="worker.workerId"
            class="worker-row"
          >
            <div class="worker-main">
              <span class="worker-sequence">#{{ worker.sequence }}</span>
              <strong class="worker-email">{{ worker.email }}</strong>
              <code class="worker-ip">{{ worker.egressIp || '等待出口 IP' }}</code>
              <el-tag
                :type="worker.status === 'failed' ? 'danger' : worker.status === 'success' ? 'success' : worker.status === 'partial_success' ? 'warning' : 'info'"
                size="small"
              >
                {{ workerStageLabel(worker.stage) }} · {{ formatElapsed(worker.stageElapsedMs) }}
              </el-tag>
            </div>
            <code v-if="workerDiagnostic(worker)" class="worker-diagnostic">
              {{ workerDiagnostic(worker) }}
            </code>
          </div>
        </div>
        <div class="rule-note">
          <el-icon><CircleCheck /></el-icon>
          <span>所有 worker 共享一个 workspace；每个 worker 使用独立进程、代理和临时指纹窗口。</span>
        </div>
      </article>
    </div>

    <article class="panel log-panel">
      <div class="log-toolbar">
        <div>
          <h3>任务日志</h3>
          <p>UTF-8 JSONL · 最近保留 10 次 · 敏感凭据不写入日志</p>
        </div>
        <div class="log-actions">
          <el-tag :type="store.logServiceOnline ? 'success' : 'warning'" effect="dark" round>
            {{ store.logServiceOnline ? '日志已持久化' : '日志服务离线' }}
          </el-tag>
          <el-select
            :model-value="store.selectedLogRunId"
            class="history-select"
            placeholder="选择历史任务"
            @change="selectHistory"
          >
            <el-option
              v-for="item in historyOptions"
              :key="item.runId"
              :label="`${formatTime(item.startedAt)} · ${item.lastEvent}`"
              :value="item.runId"
            />
          </el-select>
          <el-button :icon="Refresh" circle aria-label="刷新日志列表" @click="refreshHistory" />
          <el-button :icon="DocumentCopy" :disabled="!logText" @click="copyLogs">复制</el-button>
          <el-button :icon="Download" :disabled="!logText" @click="downloadLogs">下载</el-button>
        </div>
      </div>

      <el-alert
        v-if="store.logError"
        class="log-alert"
        type="warning"
        :closable="false"
        :title="store.logError"
        show-icon
      />

      <div ref="logViewport" class="log-viewport">
        <div v-if="!store.displayedLogs.length" class="empty-log">
          <el-icon><CircleClose /></el-icon>
          <span>暂无任务日志，启动一次真实探测后将在这里实时显示。</span>
        </div>
        <div
          v-for="(entry, index) in store.displayedLogs"
          :key="`${entry.timestamp}-${entry.event}-${index}`"
          class="log-row"
          :class="`log-row--${entry.level}`"
        >
          <time>{{ formatTime(entry.timestamp) }}</time>
          <span class="log-level">{{ logLevelLabel(entry.level) }}</span>
          <code>{{ entry.event }}</code>
          <strong v-if="entry.email">{{ entry.email }}</strong>
          <p>{{ entry.message }}</p>
        </div>
      </div>
    </article>
  </section>
</template>

<style scoped>
.launch-heading {
  align-items: center;
}

.launch-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 18px;
}

.metric-card {
  min-height: 168px;
  padding: 20px;
}

.metric-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--text-secondary);
  font-size: 12px;
}

.metric-title .el-icon {
  color: var(--accent);
  font-size: 19px;
}

.metric-card > strong {
  display: block;
  margin: 14px 0 12px;
  font-size: 34px;
  line-height: 1;
}

.metric-card > p,
.metric-card > small {
  color: var(--text-muted);
  font-size: 11px;
}

.metric-breakdown {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.metric-breakdown span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 7px;
  color: var(--text-secondary);
  background: rgb(7 11 17 / 44%);
  font-size: 10px;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.dot--green { background: var(--success); }
.dot--amber { background: var(--warning); }
.dot--muted { background: var(--text-muted); }

.launch-workspace {
  display: grid;
  grid-template-columns: minmax(300px, 0.72fr) minmax(430px, 1.28fr);
  gap: 16px;
  margin-bottom: 18px;
}

.start-panel,
.progress-panel {
  padding: 21px;
}

.section-heading {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
}

.section-heading h3,
.section-heading p,
.log-toolbar h3,
.log-toolbar p {
  margin: 0;
}

.section-heading h3,
.log-toolbar h3 {
  font-size: 14px;
}

.section-heading p,
.log-toolbar p {
  margin-top: 5px;
  color: var(--text-muted);
  font-size: 11px;
}

.section-icon {
  display: grid;
  width: 38px;
  height: 38px;
  flex: 0 0 38px;
  place-items: center;
  border: 1px solid rgb(50 197 255 / 24%);
  border-radius: 10px;
  color: var(--accent);
  background: rgb(50 197 255 / 7%);
}

.run-form label {
  display: block;
  margin-bottom: 8px;
  color: var(--text-secondary);
  font-size: 11px;
}

.run-form .el-input-number {
  width: 100%;
}

.source-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  margin-bottom: 14px;
}

.source-row .el-select {
  width: 100%;
}

.run-hints {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin: 9px 0 16px;
  color: var(--text-muted);
  font-size: 10px;
}

.start-button {
  width: 100%;
}

.run-actions {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
}

.run-actions .cancel-button {
  min-width: 112px;
}

.progress-heading,
.log-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.progress-heading {
  margin-bottom: 20px;
}

.progress-heading > div span,
.progress-heading > div strong {
  display: block;
}

.progress-heading > div span {
  color: var(--text-muted);
  font-size: 10px;
}

.progress-heading > div strong {
  margin-top: 4px;
  font-size: 17px;
}

.success-rate-tag {
  display: inline-flex;
  min-width: 118px;
  height: 34px;
  align-items: center;
  justify-content: center;
  color: #07111a !important;
  font-size: 13px;
  font-weight: 900;
  letter-spacing: 0.02em;
  text-align: center;
}

.success-rate-tag :deep(.el-tag__content) {
  width: 100%;
  color: inherit;
  text-align: center;
}

.run-summary {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 9px;
  margin-top: 22px;
}

.run-summary div {
  padding: 12px;
  border: 1px solid var(--border-subtle);
  border-radius: 9px;
  background: rgb(7 11 17 / 44%);
}

.run-summary span,
.run-summary strong {
  display: block;
}

.run-summary span {
  color: var(--text-muted);
  font-size: 10px;
}

.run-summary strong {
  margin-top: 7px;
  font-size: 20px;
}

.summary-success strong { color: var(--success); }
.summary-failed strong { color: var(--danger); }

.worker-overview {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 16px;
  color: var(--text-muted);
  font-size: 11px;
}

.worker-overview strong { color: var(--accent); }

.worker-list {
  display: grid;
  gap: 8px;
  max-height: 240px;
  margin-top: 10px;
  overflow: auto;
}

.circuit-alert {
  margin-top: 14px;
}

.worker-row {
  display: block;
  padding: 9px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  background: rgb(7 11 17 / 44%);
  font-size: 11px;
}

.worker-main {
  display: grid;
  grid-template-columns: 34px minmax(160px, 1fr) minmax(120px, 0.7fr) auto;
  gap: 8px;
  align-items: center;
}

.worker-sequence { color: var(--text-muted); }
.worker-email { overflow: hidden; text-overflow: ellipsis; }
.worker-ip { color: var(--accent); }

.worker-diagnostic {
  display: block;
  margin: 7px 0 0 42px;
  color: var(--danger);
  font-size: 10px;
}

.rule-note {
  display: flex;
  gap: 8px;
  margin-top: 17px;
  color: var(--text-muted);
  font-size: 10px;
}

.rule-note .el-icon {
  flex: 0 0 auto;
  color: var(--accent);
}

.log-panel {
  overflow: hidden;
}

.log-toolbar {
  padding: 17px 19px;
  border-bottom: 1px solid var(--border-subtle);
}

.log-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.history-select {
  width: 240px;
}

.log-alert {
  margin: 12px 18px 0;
}

.log-viewport {
  overflow: auto;
  min-height: 260px;
  max-height: 430px;
  padding: 10px 0;
  background: #070b11;
}

.log-row {
  display: grid;
  grid-template-columns: 118px 44px 150px minmax(150px, 220px) minmax(260px, 1fr);
  gap: 10px;
  align-items: start;
  min-width: 900px;
  padding: 8px 18px;
  border-left: 2px solid transparent;
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 10px;
}

.log-row:hover { background: rgb(50 197 255 / 4%); }
.log-row--success { border-left-color: var(--success); }
.log-row--warning { border-left-color: var(--warning); }
.log-row--error { border-left-color: var(--danger); }

.log-row time,
.log-row code {
  color: var(--text-muted);
}

.log-row strong {
  overflow: hidden;
  color: #a9dff3;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.log-row p {
  margin: 0;
  color: var(--text-secondary);
}

.log-level {
  color: var(--accent);
}

.log-row--success .log-level { color: var(--success); }
.log-row--warning .log-level { color: var(--warning); }
.log-row--error .log-level { color: var(--danger); }

.empty-log {
  display: flex;
  min-height: 240px;
  align-items: center;
  justify-content: center;
  gap: 9px;
  color: var(--text-muted);
  font-size: 12px;
}

@media (max-width: 1040px) {
  .launch-metrics,
  .launch-workspace {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 720px) {
  .launch-heading,
  .log-toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .run-summary {
    grid-template-columns: repeat(2, 1fr);
  }

  .log-actions,
  .history-select {
    width: 100%;
  }
}
</style>
