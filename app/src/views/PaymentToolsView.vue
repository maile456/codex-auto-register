<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  CopyDocument,
  CreditCard,
  Delete,
  Download,
  Key,
  Link,
  Refresh,
  VideoPause,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { dataGateway } from '@/services/dataGateway'
import type {
  ExtractedAccessToken,
  PaymentExtractorOption,
  PaymentExtractorProxyTestResult,
  PaymentExtractorResult,
  PaymentExtractorTask,
  PaymentExtractorTaskStatus,
} from '@/types'

type TaskFilter = 'all' | 'running' | 'succeeded' | 'failed'
type ProxyKind = 'checkout' | 'update'

interface ProxyTestState {
  loading: boolean
  result: PaymentExtractorProxyTestResult | null
  error: string
}

interface SavedPreferences {
  checkoutProxyPool?: string
  updateProxyPool?: string
  country?: string
  paymentMethod?: string
  applyCheckoutUpdate?: boolean
  rotateCheckoutSession?: boolean
  rotateUpdateSession?: boolean
  proxySourceUrl?: string
  concurrency?: number
}

const PREFERENCES_KEY = 'autoregister.payment-extractor.preferences'
const WORKBENCH_PASSWORD_KEY = 'payment_link_extractor.workbench_password'
const TERMINAL_STATES = new Set<PaymentExtractorTaskStatus>([
  'succeeded',
  'failed',
  'cancelled',
])
const FALLBACK_COUNTRIES: PaymentExtractorOption[] = [
  { value: 'GB', label: '英国 · GBP' },
  { value: 'US', label: '美国 · USD' },
  { value: 'BR', label: '巴西 · USD' },
  { value: 'DE', label: '德国 · EUR' },
  { value: 'TH', label: '泰国 · USD' },
  { value: 'BA', label: '波黑 · USD' },
  { value: 'PH', label: '菲律宾 · PHP' },
  { value: 'ID', label: '印度尼西亚 · IDR' },
  { value: 'NL', label: '荷兰 · EUR' },
  { value: 'AE', label: '阿联酋 · AED' },
  { value: 'DK', label: '丹麦 · DKK' },
  { value: 'JP', label: '日本 · JPY' },
  { value: 'ES', label: '西班牙 · EUR' },
  { value: 'FI', label: '芬兰 · EUR' },
  { value: 'FR', label: '法国 · EUR' },
]
const FALLBACK_METHODS: PaymentExtractorOption[] = [
  { value: 'paypal', label: 'PayPal' },
  { value: 'gopay', label: 'GoPay' },
  { value: 'gcash', label: 'GCash' },
]

const rawText = ref('')
const extracting = ref(false)
const tokens = ref<ExtractedAccessToken[]>([])
const selectedIndex = ref(0)
const submitting = ref(false)
const defaultsLoading = ref(false)
const tasksLoading = ref(false)
const countries = ref<PaymentExtractorOption[]>(FALLBACK_COUNTRIES)
const paymentMethods = ref<PaymentExtractorOption[]>(FALLBACK_METHODS)
const country = ref('DE')
const forceCountry = ref('')
const paymentMethod = ref('paypal')
const checkoutProxyPool = ref('')
const updateProxyPool = ref('')
const proxySourceUrl = ref('')
const proxySourceLoading = ref(false)
const stripeHcaptchaToken = ref('')
const workbenchPassword = ref('')
const applyCheckoutUpdate = ref(true)
const concurrency = ref(4)
const maxConcurrency = ref(10)
const concurrencyUpdating = ref(false)
const rotateCheckoutSession = ref(true)
const rotateUpdateSession = ref(true)
const checkoutProxyTest = ref<ProxyTestState>({ loading: false, result: null, error: '' })
const updateProxyTest = ref<ProxyTestState>({ loading: false, result: null, error: '' })
const tasks = ref<PaymentExtractorTask[]>([])
const taskFilter = ref<TaskFilter>('all')
const pendingActions = ref<Record<string, string>>({})
const bulkAction = ref<'' | 'failed' | 'succeeded' | 'retry-network'>('')
const existingLink = ref('')
const preferencesReady = ref(false)
const hadSavedPreferences = ref(false)

let checkoutProxyCursor = 0
let updateProxyCursor = 0
let pollTimer: number | null = null

const selectedToken = computed(() => tokens.value[selectedIndex.value] ?? null)
const usableTokens = computed(() => tokens.value.filter((item) => item.expired !== true))
const checkoutProxyCount = computed(() => proxyLines(checkoutProxyPool.value).length)
const updateProxyCount = computed(() => proxyLines(updateProxyPool.value).length)
const taskCounts = computed(() => {
  const result = { running: 0, succeeded: 0, failed: 0 }
  for (const task of tasks.value) {
    if (task.status === 'succeeded') result.succeeded += 1
    else if (task.status === 'failed' || task.status === 'cancelled') result.failed += 1
    else result.running += 1
  }
  return result
})
const filteredTasks = computed(() => {
  if (taskFilter.value === 'all') return tasks.value
  if (taskFilter.value === 'running') {
    return tasks.value.filter((task) => !TERMINAL_STATES.has(task.status))
  }
  if (taskFilter.value === 'failed') {
    return tasks.value.filter((task) => task.status === 'failed' || task.status === 'cancelled')
  }
  return tasks.value.filter((task) => task.status === 'succeeded')
})
const successfulTasks = computed(() => tasks.value.filter((task) => task.status === 'succeeded'))
const canSubmit = computed(() => {
  if (!selectedToken.value || selectedToken.value.expired === true || submitting.value) return false
  if (!checkoutProxyPool.value.trim()) return false
  return !applyCheckoutUpdate.value || Boolean(updateProxyPool.value.trim())
})

watch(
  [
    checkoutProxyPool,
    updateProxyPool,
    country,
    paymentMethod,
    applyCheckoutUpdate,
    rotateCheckoutSession,
    rotateUpdateSession,
    proxySourceUrl,
    concurrency,
  ],
  savePreferences,
)

function proxyLines(value: string) {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
}

function normalizeProxyPool(value: string) {
  return proxyLines(value).join('\n')
}

function restorePreferences() {
  try {
    workbenchPassword.value = localStorage.getItem(WORKBENCH_PASSWORD_KEY) || ''
    const raw = localStorage.getItem(PREFERENCES_KEY)
    hadSavedPreferences.value = Boolean(raw)
    const saved = JSON.parse(raw || '{}') as SavedPreferences
    if (typeof saved.checkoutProxyPool === 'string') checkoutProxyPool.value = saved.checkoutProxyPool
    if (typeof saved.updateProxyPool === 'string') updateProxyPool.value = saved.updateProxyPool
    if (typeof saved.country === 'string') country.value = saved.country
    if (typeof saved.paymentMethod === 'string') paymentMethod.value = saved.paymentMethod
    if (typeof saved.applyCheckoutUpdate === 'boolean') {
      applyCheckoutUpdate.value = saved.applyCheckoutUpdate
    }
    if (typeof saved.rotateCheckoutSession === 'boolean') {
      rotateCheckoutSession.value = saved.rotateCheckoutSession
    }
    if (typeof saved.rotateUpdateSession === 'boolean') {
      rotateUpdateSession.value = saved.rotateUpdateSession
    }
    if (typeof saved.proxySourceUrl === 'string') proxySourceUrl.value = saved.proxySourceUrl
    if (Number.isInteger(saved.concurrency)) {
      concurrency.value = Math.max(1, Math.min(10, Number(saved.concurrency)))
    }
  } catch {
    // Keep service defaults when browser storage is unavailable or malformed.
  }
  preferencesReady.value = true
}

function saveWorkbenchPassword(value: string) {
  try {
    if (value) localStorage.setItem(WORKBENCH_PASSWORD_KEY, value)
    else localStorage.removeItem(WORKBENCH_PASSWORD_KEY)
  } catch {
    // The default empty-password local service remains usable without storage.
  }
}

watch(workbenchPassword, saveWorkbenchPassword)

function savePreferences() {
  if (!preferencesReady.value) return
  const preferences: SavedPreferences = {
    checkoutProxyPool: normalizeProxyPool(checkoutProxyPool.value),
    updateProxyPool: normalizeProxyPool(updateProxyPool.value),
    country: country.value,
    paymentMethod: paymentMethod.value,
    applyCheckoutUpdate: applyCheckoutUpdate.value,
    rotateCheckoutSession: rotateCheckoutSession.value,
    rotateUpdateSession: rotateUpdateSession.value,
    proxySourceUrl: proxySourceUrl.value,
    concurrency: concurrency.value,
  }
  try {
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify(preferences))
  } catch {
    // The form remains usable if persistent browser storage is unavailable.
  }
}

function selectProxy(value: string, kind: ProxyKind) {
  const lines = proxyLines(value)
  if (!lines.length) return ''
  if (kind === 'update') {
    const selected = lines[updateProxyCursor % lines.length] ?? ''
    updateProxyCursor += 1
    return selected
  }
  const selected = lines[checkoutProxyCursor % lines.length] ?? ''
  checkoutProxyCursor += 1
  return selected
}

function randomAlphaNumeric(length: number) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  const bytes = new Uint8Array(length)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (byte) => alphabet[byte % alphabet.length]).join('')
}

function randomDigits(length: number) {
  if (length <= 0) return ''
  const bytes = new Uint8Array(length)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (byte, index) => String(index === 0 ? (byte % 9) + 1 : byte % 10)).join('')
}

function rotateProxySessionMarker(proxy: string) {
  const value = proxy.trim()
  const match = value.match(/^([a-z][a-z\d+.-]*:\/\/)([^/@]+)(@.*)$/i)
  if (!match) return value
  const prefix = match[1] ?? ''
  const userInfo = match[2] ?? ''
  const destination = match[3] ?? ''
  const sid = userInfo.match(/(^|-)sid-([A-Za-z0-9]+)(?=-|:)/i)
  if (sid?.[0] && sid[2]) {
    return `${prefix}${userInfo.replace(sid[0], `${sid[1]}sid-${randomAlphaNumeric(sid[2].length)}`)}${destination}`
  }
  const numeric = userInfo.match(/-(\d+)$/)
  if (!numeric?.[1]) return value
  return `${prefix}${userInfo.slice(0, -numeric[1].length)}${randomDigits(numeric[1].length)}${destination}`
}

function formatDate(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

function stageLabel(stage: string) {
  const labels: Record<string, string> = {
    queued: '等待执行',
    running: '开始执行',
    eligibility_check: '检查优惠资格',
    checkout: '创建 Checkout',
    checkout_update: '更新 Checkout',
    stripe_init: '初始化支付',
    elements_session: '准备支付方式',
    taxes: '同步税费',
    payment_confirmation: '确认支付方式',
    redirect_resolution: '解析跳转链接',
    completed: '任务完成',
    cancelled: '任务已取消',
    failed: '任务失败',
  }
  return labels[stage] ?? stage ?? '处理中'
}

function statusLabel(status: PaymentExtractorTaskStatus) {
  return {
    queued: '排队中',
    running: '运行中',
    cancel_requested: '取消中',
    succeeded: '成功',
    failed: '失败',
    cancelled: '已取消',
  }[status]
}

function statusTagType(status: PaymentExtractorTaskStatus): 'success' | 'danger' | 'warning' | 'info' {
  if (status === 'succeeded') return 'success'
  if (status === 'failed' || status === 'cancelled') return 'danger'
  if (status === 'cancel_requested') return 'warning'
  return 'info'
}

function checkoutKindLabel(value?: string | null) {
  if (value === 'stripe_checkout') return 'CS'
  if (value === 'openai_custom_checkout') return 'OAICS'
  return value || '—'
}

function readResultString(result: PaymentExtractorResult | null | undefined, ...keys: string[]) {
  if (!result) return ''
  for (const key of keys) {
    const value = result[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function resultUrl(task: PaymentExtractorTask) {
  return readResultString(
    task.result,
    'providerUrl',
    'paypalUrl',
    'gopayUrl',
    'gcashUrl',
    'provider_url',
    'paypal_url',
    'gopay_url',
    'gcash_url',
  )
}

function formatAmount(result?: PaymentExtractorResult | null) {
  if (!result || result.amountDue === undefined || result.amountDue === null) return '—'
  return `${result.amountDue} ${result.currency || ''}`.trim()
}

function hasNonZeroAmount(task: PaymentExtractorTask) {
  const value = task.result?.amountDueMinor ?? task.result?.amountDue
  return value !== undefined && value !== null && Number.isFinite(Number(value)) && Number(value) !== 0
}

function canRetry(task: PaymentExtractorTask) {
  return task.status === 'failed' || task.status === 'cancelled' || (task.status === 'succeeded' && hasNonZeroAmount(task))
}

function redactSensitiveText(value?: string | null) {
  let output = String(value || '')
  for (const item of tokens.value) {
    if (item.token) output = output.replaceAll(item.token, '[Access Token 已隐藏]')
  }
  return output.replace(/eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}(?:\.[A-Za-z0-9_-]+)?/g, '[Access Token 已隐藏]')
}

function normalizedHttpsUrl(value: string) {
  try {
    const parsed = new URL(value.trim())
    return parsed.protocol === 'https:' ? parsed.toString() : ''
  } catch {
    return ''
  }
}

function openPaymentUrl(value: string) {
  const url = normalizedHttpsUrl(value)
  if (!url) {
    ElMessage.error('请输入有效的 HTTPS 支付链接')
    return
  }
  window.open(url, '_blank', 'noopener,noreferrer')
}

async function copyText(value: string, label: string) {
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success(`${label}已复制`)
  } catch {
    ElMessage.error(`${label}复制失败`)
  }
}

async function extractTokens() {
  if (!rawText.value.trim() || extracting.value) return
  extracting.value = true
  try {
    const result = await dataGateway.extractAccessTokens(rawText.value)
    tokens.value = result.items
    selectedIndex.value = 0
    if (result.count) ElMessage.success(`已提炼 ${result.count} 个 Access Token`)
    else ElMessage.warning('输入中没有识别到 Access Token')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : 'Access Token 提炼失败')
  } finally {
    extracting.value = false
  }
}

function clearCredentials() {
  rawText.value = ''
  tokens.value = []
  selectedIndex.value = 0
}

async function loadDefaults() {
  defaultsLoading.value = true
  try {
    const defaults = await dataGateway.paymentExtractorDefaults()
    maxConcurrency.value = Math.max(1, defaults.maxConcurrency || 10)
    if (!hadSavedPreferences.value) concurrency.value = defaults.concurrency || 4
    concurrency.value = Math.max(1, Math.min(maxConcurrency.value, concurrency.value))
    if (defaults.countries?.length) countries.value = defaults.countries
    if (defaults.paymentMethods?.length) paymentMethods.value = defaults.paymentMethods
    forceCountry.value = defaults.forceCountry || ''
    if (forceCountry.value) country.value = forceCountry.value
    else if (!hadSavedPreferences.value && defaults.country) country.value = defaults.country
    else if (!countries.value.some((item) => item.value === country.value)) {
      country.value = defaults.country || countries.value[0]?.value || 'BR'
    }
    if (!hadSavedPreferences.value && defaults.paymentMethod) {
      paymentMethod.value = defaults.paymentMethod
    } else if (!paymentMethods.value.some((item) => item.value === paymentMethod.value)) {
      paymentMethod.value = defaults.paymentMethod || paymentMethods.value[0]?.value || 'paypal'
    }
    if (!checkoutProxyPool.value.trim()) checkoutProxyPool.value = defaults.checkoutProxy || ''
    if (!updateProxyPool.value.trim()) updateProxyPool.value = defaults.updateProxy || ''
    if (typeof defaults.applyCheckoutUpdate === 'boolean' && !hadSavedPreferences.value) {
      applyCheckoutUpdate.value = defaults.applyCheckoutUpdate
    }
  } catch (error) {
    ElMessage.warning(error instanceof Error ? error.message : '提链默认配置加载失败')
  } finally {
    defaultsLoading.value = false
  }
}

async function updateConcurrency(quiet = false) {
  if (concurrencyUpdating.value) return
  concurrency.value = Math.max(1, Math.min(maxConcurrency.value, Number(concurrency.value) || 1))
  concurrencyUpdating.value = true
  try {
    const result = await dataGateway.setPaymentExtractorConcurrency(concurrency.value)
    maxConcurrency.value = Math.max(1, result.maxConcurrency || 10)
    concurrency.value = Math.max(1, Math.min(maxConcurrency.value, result.concurrency))
    if (!quiet) ElMessage.success(`提炼并发已调整为 ${concurrency.value}`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '提炼并发调整失败')
  } finally {
    concurrencyUpdating.value = false
  }
}

function upsertTask(task: PaymentExtractorTask) {
  const index = tasks.value.findIndex((item) => item.taskId === task.taskId)
  if (index >= 0) {
    tasks.value[index] = task
    tasks.value = [...tasks.value]
  } else {
    tasks.value = [task, ...tasks.value]
  }
}

async function loadTasks() {
  tasksLoading.value = true
  try {
    const result = await dataGateway.listPaymentExtractorTasks()
    tasks.value = result.tasks ?? []
    ensurePolling()
  } catch (error) {
    ElMessage.warning(error instanceof Error ? error.message : '提链任务加载失败')
  } finally {
    tasksLoading.value = false
  }
}

async function pollActiveTasks() {
  const activeIds = tasks.value
    .filter((task) => !TERMINAL_STATES.has(task.status))
    .map((task) => task.taskId)
  if (!activeIds.length) {
    stopPolling()
    return
  }
  const results = await Promise.allSettled(
    activeIds.map((taskId) => dataGateway.getPaymentExtractorTask(taskId)),
  )
  for (const result of results) {
    if (result.status === 'fulfilled') upsertTask(result.value)
  }
  if (!tasks.value.some((task) => !TERMINAL_STATES.has(task.status))) stopPolling()
}

function ensurePolling() {
  if (pollTimer !== null || !tasks.value.some((task) => !TERMINAL_STATES.has(task.status))) return
  pollTimer = window.setInterval(() => void pollActiveTasks(), 1200)
}

function stopPolling() {
  if (pollTimer === null) return
  window.clearInterval(pollTimer)
  pollTimer = null
}

function validateSubmission() {
  if (!checkoutProxyPool.value.trim()) throw new Error('请先填写 Checkout Proxy 池')
  if (applyCheckoutUpdate.value && !updateProxyPool.value.trim()) {
    throw new Error('执行 Checkout Update 时必须填写 Update Proxy 池')
  }
  if (!country.value) throw new Error('请选择账单国家')
  if (!paymentMethod.value) throw new Error('请选择支付方式')
}

async function submitTokenItems(items: ExtractedAccessToken[]) {
  if (!items.length || submitting.value) return
  try {
    validateSubmission()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '提链配置不完整')
    return
  }
  submitting.value = true
  let succeeded = 0
  const failures: string[] = []
  for (const item of items) {
    const checkoutSelected = selectProxy(checkoutProxyPool.value, 'checkout')
    const updateSelected = applyCheckoutUpdate.value
      ? selectProxy(updateProxyPool.value, 'update')
      : ''
    try {
      const task = await dataGateway.createPaymentExtractorTask({
        accessToken: item.token,
        checkoutProxy: rotateCheckoutSession.value
          ? rotateProxySessionMarker(checkoutSelected)
          : checkoutSelected,
        updateProxy: rotateUpdateSession.value
          ? rotateProxySessionMarker(updateSelected)
          : updateSelected,
        stripeHcaptchaToken: stripeHcaptchaToken.value.trim(),
        country: country.value,
        paymentMethod: paymentMethod.value,
        applyCheckoutUpdate: applyCheckoutUpdate.value,
      })
      upsertTask(task)
      succeeded += 1
    } catch (error) {
      failures.push(error instanceof Error ? error.message : '任务提交失败')
    }
  }
  submitting.value = false
  ensurePolling()
  if (succeeded) ElMessage.success(`已提交 ${succeeded} 个提链任务`)
  if (failures.length) ElMessage.error(`${failures.length} 个任务提交失败：${failures[0]}`)
}

async function testProxyPool(kind: ProxyKind) {
  const pool = kind === 'checkout' ? checkoutProxyPool.value : updateProxyPool.value
  const first = proxyLines(pool)[0] || ''
  if (!first) {
    ElMessage.error(`请先填写 ${kind === 'checkout' ? 'Checkout' : 'Update'} Proxy 池`)
    return
  }
  const target = kind === 'checkout' ? checkoutProxyTest : updateProxyTest
  target.value = { loading: true, result: null, error: '' }
  try {
    target.value = {
      loading: false,
      result: await dataGateway.testPaymentExtractorProxy(first),
      error: '',
    }
  } catch (error) {
    target.value = {
      loading: false,
      result: null,
      error: error instanceof Error ? error.message : '代理检测失败',
    }
  }
}

async function importProxySource() {
  if (!proxySourceUrl.value.trim() || proxySourceLoading.value) return
  proxySourceLoading.value = true
  try {
    const result = await dataGateway.loadPaymentExtractorProxySource(proxySourceUrl.value.trim())
    const incoming = result.proxies.map((item) => item.trim()).filter(Boolean)
    const merge = (current: string) => [...new Set([...proxyLines(current), ...incoming])].join('\n')
    checkoutProxyPool.value = merge(checkoutProxyPool.value)
    updateProxyPool.value = merge(updateProxyPool.value)
    ElMessage.success(`已导入 ${result.count} 条代理，去重后 ${result.uniqueCount} 条`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '代理订阅读取失败')
  } finally {
    proxySourceLoading.value = false
  }
}

function setPending(taskId: string, action: string) {
  pendingActions.value = { ...pendingActions.value, [taskId]: action }
}

function clearPending(taskId: string) {
  const next = { ...pendingActions.value }
  delete next[taskId]
  pendingActions.value = next
}

async function cancelTask(task: PaymentExtractorTask) {
  setPending(task.taskId, 'cancel')
  try {
    upsertTask(await dataGateway.cancelPaymentExtractorTask(task.taskId))
    ensurePolling()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '取消任务失败')
  } finally {
    clearPending(task.taskId)
  }
}

async function retryTask(task: PaymentExtractorTask) {
  setPending(task.taskId, 'retry')
  try {
    const checkout = checkoutProxyPool.value.trim()
      ? selectProxy(checkoutProxyPool.value, 'checkout')
      : ''
    const update = applyCheckoutUpdate.value && updateProxyPool.value.trim()
      ? selectProxy(updateProxyPool.value, 'update')
      : ''
    const retried = await dataGateway.retryPaymentExtractorTask(task.taskId, {
      ...(checkout
        ? {
            checkoutProxy: rotateCheckoutSession.value
              ? rotateProxySessionMarker(checkout)
              : checkout,
          }
        : {}),
      ...(update
        ? {
            updateProxy: rotateUpdateSession.value
              ? rotateProxySessionMarker(update)
              : update,
          }
        : {}),
    })
    tasks.value = tasks.value.filter((item) => item.taskId !== task.taskId)
    upsertTask(retried)
    ensurePolling()
    ElMessage.success('重试任务已创建')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '任务重试失败')
  } finally {
    clearPending(task.taskId)
  }
}

async function resolvePaypal(task: PaymentExtractorTask) {
  setPending(task.taskId, 'resolve')
  try {
    upsertTask(await dataGateway.resolvePaymentExtractorPaypal(task.taskId))
    ElMessage.success('PayPal 跳转链接已重新解析')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : 'PayPal 链接解析失败')
  } finally {
    clearPending(task.taskId)
  }
}

async function deleteTask(task: PaymentExtractorTask) {
  setPending(task.taskId, 'delete')
  try {
    await dataGateway.deletePaymentExtractorTask(task.taskId)
    tasks.value = tasks.value.filter((item) => item.taskId !== task.taskId)
    ElMessage.success('任务已删除')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '任务删除失败')
  } finally {
    clearPending(task.taskId)
  }
}

async function bulkDelete(target: 'failed' | 'succeeded') {
  if (bulkAction.value) return
  bulkAction.value = target
  try {
    const result = await dataGateway.bulkDeletePaymentExtractorTasks(target)
    const removed = new Set(result.taskIds)
    tasks.value = tasks.value.filter((task) => !removed.has(task.taskId))
    ElMessage.success(`已清理 ${result.deletedCount} 个任务`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '批量清理失败')
  } finally {
    bulkAction.value = ''
  }
}

async function retryNetworkFailures() {
  if (bulkAction.value) return
  const targets = tasks.value.filter((task) => task.status === 'failed' && task.networkError)
  if (!targets.length) return
  bulkAction.value = 'retry-network'
  let submitted = 0
  for (const task of targets) {
    try {
      const checkout = selectProxy(checkoutProxyPool.value, 'checkout')
      const update = applyCheckoutUpdate.value ? selectProxy(updateProxyPool.value, 'update') : ''
      const retried = await dataGateway.retryPaymentExtractorTask(task.taskId, {
        ...(checkout ? { checkoutProxy: rotateCheckoutSession.value ? rotateProxySessionMarker(checkout) : checkout } : {}),
        ...(update ? { updateProxy: rotateUpdateSession.value ? rotateProxySessionMarker(update) : update } : {}),
      })
      tasks.value = tasks.value.filter((item) => item.taskId !== task.taskId)
      upsertTask(retried)
      submitted += 1
    } catch {
      // Continue the remaining network failures; individual failures stay visible.
    }
  }
  bulkAction.value = ''
  ensurePolling()
  if (submitted) ElMessage.success(`已重试 ${submitted} 个网络失败任务`)
}

function csvCell(value: unknown) {
  const text = String(value ?? '')
  return `"${text.replaceAll('"', '""')}"`
}

function exportSuccessfulCsv() {
  const rows = successfulTasks.value.map((task) => [
    task.accountEmail || task.result?.accountEmail || '',
    resultUrl(task),
    task.result?.checkoutSessionId || '',
    task.paymentMethod,
    task.billingCountry,
    task.result?.amountDue ?? '',
    task.result?.currency || '',
    task.result?.paymentMethodId || '',
  ])
  if (!rows.length) return
  const header = ['account_email', 'provider_url', 'checkout_session_id', 'payment_method', 'billing_country', 'amount_due', 'currency', 'payment_method_id']
  const csv = `\ufeff${[header, ...rows].map((row) => row.map(csvCell).join(',')).join('\r\n')}\r\n`
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `payment-links-${new Date().toISOString().replaceAll(':', '-').slice(0, 19)}.csv`
  anchor.click()
  URL.revokeObjectURL(url)
}

onMounted(async () => {
  restorePreferences()
  await Promise.allSettled([loadDefaults(), loadTasks()])
  await updateConcurrency(true)
})

onBeforeUnmount(() => {
  stopPolling()
  rawText.value = ''
  tokens.value = []
  stripeHcaptchaToken.value = ''
})
</script>

<template>
  <section class="payment-tools">
    <div class="page-heading">
      <div>
        <h2>Access Token 提链控制台</h2>
        <p>完整任务流独立于注册 worker；Access Token 只保存在当前页面内存，不写入浏览器存储。</p>
      </div>
      <div class="heading-stats">
        <span>运行 <strong>{{ taskCounts.running }}</strong></span>
        <span>成功 <strong>{{ taskCounts.succeeded }}</strong></span>
        <span>失败 <strong>{{ taskCounts.failed }}</strong></span>
      </div>
    </div>

    <div class="console-grid">
      <div class="setup-stack">
        <article class="panel tool-panel">
          <div class="tool-title">
            <span class="tool-icon"><el-icon><Key /></el-icon></span>
            <div>
              <h3>1. Session / Access Token</h3>
              <p>支持裸 Token、Bearer、Session JSON 和批量分隔内容。</p>
            </div>
          </div>
          <el-input
            v-model="rawText"
            data-testid="credential-input"
            type="textarea"
            :rows="6"
            resize="vertical"
            autocomplete="off"
            placeholder="粘贴 Session JSON / Access Token；多段 JSON 可用单独一行 --- 分隔"
          />
          <div class="panel-actions">
            <el-button
              type="primary"
              :icon="Key"
              :loading="extracting"
              :disabled="!rawText.trim()"
              @click="extractTokens"
            >
              提炼 Access Token
            </el-button>
            <el-button @click="clearCredentials">清空敏感输入</el-button>
            <span class="muted">可用 {{ usableTokens.length }} / 已识别 {{ tokens.length }}</span>
          </div>

          <div v-if="tokens.length" class="token-list">
            <button
              v-for="(item, index) in tokens"
              :key="`${item.preview}-${index}`"
              type="button"
              class="token-item"
              :class="{ active: selectedIndex === index }"
              :data-testid="`token-${index}`"
              @click="selectedIndex = index"
            >
              <span>
                <strong>{{ item.email || `账号 ${index + 1}` }}</strong>
                <small>{{ item.preview }}</small>
              </span>
              <el-tag :type="item.expired ? 'danger' : 'success'" effect="plain" round>
                {{ item.expired ? '已过期' : '可用' }}
              </el-tag>
            </button>
            <div v-if="selectedToken" class="selected-token-actions">
              <span>当前：{{ selectedToken.preview }}</span>
              <el-button size="small" :icon="CopyDocument" @click="copyText(selectedToken.token, 'Access Token')">
                复制当前 AT
              </el-button>
            </div>
          </div>
        </article>

        <article class="panel tool-panel proxy-panel">
          <div class="tool-title">
            <span class="tool-icon"><el-icon><Refresh /></el-icon></span>
            <div>
              <h3>2. 双代理池</h3>
              <p>每次提交按行轮询；可为支持会话标记的代理生成新会话。</p>
            </div>
          </div>
          <div class="proxy-source-row">
            <el-input
              v-model="proxySourceUrl"
              data-testid="proxy-source-url"
              autocomplete="off"
              placeholder="可选：粘贴 app.iprocket.io HTTPS 代理订阅链接"
            />
            <el-button
              :loading="proxySourceLoading"
              :disabled="!proxySourceUrl.trim()"
              data-testid="import-proxy-source"
              @click="importProxySource"
            >
              导入订阅
            </el-button>
          </div>
          <div class="proxy-grid">
            <div class="proxy-field">
              <div class="field-heading">
                <label for="checkout-proxy-pool">Checkout Proxy 池</label>
                <span>{{ checkoutProxyCount }} 条</span>
              </div>
              <el-input
                id="checkout-proxy-pool"
                v-model="checkoutProxyPool"
                data-testid="checkout-proxy-pool"
                type="textarea"
                :rows="5"
                autocomplete="off"
                placeholder="每行一条：http(s)://user:pass@host:port 或 socks5://..."
              />
              <div class="proxy-controls">
                <el-checkbox v-model="rotateCheckoutSession">轮换 Checkout 会话标记</el-checkbox>
                <el-button
                  size="small"
                  data-testid="test-checkout-proxy"
                  :loading="checkoutProxyTest.loading"
                  @click="testProxyPool('checkout')"
                >
                  测试首条代理
                </el-button>
              </div>
              <div v-if="checkoutProxyTest.result" class="proxy-result success">
                IP {{ checkoutProxyTest.result.ip || '未知' }} ·
                {{ checkoutProxyTest.result.countryCode || checkoutProxyTest.result.country || '未知地区' }}
                {{ checkoutProxyTest.result.region || '' }}
              </div>
              <div v-else-if="checkoutProxyTest.error" class="proxy-result error">
                {{ checkoutProxyTest.error }}
              </div>
            </div>

            <div class="proxy-field">
              <div class="field-heading">
                <label for="update-proxy-pool">Update Proxy 池</label>
                <span>{{ updateProxyCount }} 条</span>
              </div>
              <el-input
                id="update-proxy-pool"
                v-model="updateProxyPool"
                data-testid="update-proxy-pool"
                type="textarea"
                :rows="5"
                autocomplete="off"
                placeholder="每行一条；用于 Checkout Update 阶段"
              />
              <div class="proxy-controls">
                <el-checkbox v-model="rotateUpdateSession">
                  轮换 Update 会话标记
                </el-checkbox>
                <el-button
                  size="small"
                  data-testid="test-update-proxy"
                  :loading="updateProxyTest.loading"
                  @click="testProxyPool('update')"
                >
                  测试首条代理
                </el-button>
              </div>
              <div v-if="updateProxyTest.result" class="proxy-result success">
                IP {{ updateProxyTest.result.ip || '未知' }} ·
                {{ updateProxyTest.result.countryCode || updateProxyTest.result.country || '未知地区' }}
                {{ updateProxyTest.result.region || '' }}
              </div>
              <div v-else-if="updateProxyTest.error" class="proxy-result error">
                {{ updateProxyTest.error }}
              </div>
            </div>
          </div>
          <p class="storage-note">代理池、订阅地址及可选工作台密码保存在本机 localStorage；Session、AT 与 hCaptcha Token 不会写入。</p>
        </article>

        <article class="panel tool-panel">
          <div class="tool-title">
            <span class="tool-icon"><el-icon><CreditCard /></el-icon></span>
            <div>
              <h3>3. 提链参数</h3>
              <p>国家和支付方式由后端能力动态加载。</p>
            </div>
          </div>
          <div class="parameter-grid">
            <div>
              <label class="field-label" for="extractor-country">账单国家</label>
              <el-select
                id="extractor-country"
                v-model="country"
                data-testid="country-select"
                :disabled="Boolean(forceCountry)"
                :loading="defaultsLoading"
              >
                <el-option
                  v-for="option in countries"
                  :key="option.value"
                  :label="option.label"
                  :value="option.value"
                />
              </el-select>
            </div>
            <div>
              <label class="field-label" for="extractor-method">支付方式</label>
              <el-select id="extractor-method" v-model="paymentMethod" data-testid="method-select" :loading="defaultsLoading">
                <el-option
                  v-for="option in paymentMethods"
                  :key="option.value"
                  :label="option.label"
                  :value="option.value"
                />
              </el-select>
            </div>
            <div>
              <label class="field-label" for="extractor-workbench-password">工作台密码（可选）</label>
              <el-input
                id="extractor-workbench-password"
                v-model="workbenchPassword"
                data-testid="workbench-password"
                type="password"
                show-password
                autocomplete="current-password"
                placeholder="对应 OPLL_WEB_PASSWORD"
              />
            </div>
            <div class="captcha-field">
              <label class="field-label" for="stripe-hcaptcha-token">Stripe hCaptcha Token（可选）</label>
              <el-input
                id="stripe-hcaptcha-token"
                v-model="stripeHcaptchaToken"
                data-testid="hcaptcha-token"
                type="password"
                show-password
                autocomplete="off"
                placeholder="仅随本次任务发送"
              />
            </div>
          </div>
          <div class="submit-row">
            <div class="submission-settings">
              <div class="concurrency-control">
                <span>并发</span>
                <el-input-number
                  v-model="concurrency"
                  data-testid="extractor-concurrency"
                  :min="1"
                  :max="maxConcurrency"
                  :disabled="concurrencyUpdating"
                  controls-position="right"
                  @change="updateConcurrency(false)"
                />
              </div>
              <el-checkbox v-model="applyCheckoutUpdate" data-testid="apply-update">
                执行 Checkout Update
              </el-checkbox>
            </div>
            <div class="panel-actions no-margin">
              <el-button
                type="primary"
                :icon="Link"
                :loading="submitting"
                :disabled="!canSubmit"
                data-testid="submit-selected"
                @click="selectedToken && submitTokenItems([selectedToken])"
              >
                提交当前账号
              </el-button>
              <el-button
                :loading="submitting"
                :disabled="!usableTokens.length || !checkoutProxyPool.trim()"
                data-testid="submit-all"
                @click="submitTokenItems(usableTokens)"
              >
                批量提交 {{ usableTokens.length }} 个
              </el-button>
            </div>
          </div>

          <div class="existing-link">
            <label class="field-label" for="existing-payment-link">已有支付链接</label>
            <el-input id="existing-payment-link" v-model="existingLink" placeholder="粘贴已有 HTTPS 支付链接后直接打开">
              <template #append>
                <el-button :icon="Link" @click="openPaymentUrl(existingLink)">打开</el-button>
              </template>
            </el-input>
          </div>
        </article>
      </div>

      <article class="panel tasks-panel">
        <div class="tasks-heading">
          <div>
            <h3>提链任务</h3>
            <p>运行中的任务每 1.2 秒读取一次最新阶段。</p>
          </div>
          <el-button :icon="Refresh" :loading="tasksLoading" @click="loadTasks">刷新</el-button>
        </div>

        <div class="task-toolbar">
          <el-radio-group v-model="taskFilter" size="small">
            <el-radio-button value="all">全部 {{ tasks.length }}</el-radio-button>
            <el-radio-button value="running">运行 {{ taskCounts.running }}</el-radio-button>
            <el-radio-button value="succeeded">成功 {{ taskCounts.succeeded }}</el-radio-button>
            <el-radio-button value="failed">失败 {{ taskCounts.failed }}</el-radio-button>
          </el-radio-group>
          <div class="bulk-actions">
            <el-button
              size="small"
              :disabled="!tasks.some((task) => task.status === 'failed' && task.networkError)"
              :loading="bulkAction === 'retry-network'"
              data-testid="retry-network-failed"
              @click="retryNetworkFailures"
            >
              重试网络失败
            </el-button>
            <el-button
              size="small"
              :icon="Download"
              :disabled="!successfulTasks.length"
              data-testid="export-success-csv"
              @click="exportSuccessfulCsv"
            >
              导出成功 CSV
            </el-button>
            <el-button
              size="small"
              :disabled="!taskCounts.failed"
              :loading="bulkAction === 'failed'"
              data-testid="clear-failed"
              @click="bulkDelete('failed')"
            >
              清空失败
            </el-button>
            <el-button
              size="small"
              :disabled="!taskCounts.succeeded"
              :loading="bulkAction === 'succeeded'"
              data-testid="clear-succeeded"
              @click="bulkDelete('succeeded')"
            >
              清空成功
            </el-button>
          </div>
        </div>

        <el-empty v-if="!filteredTasks.length && !tasksLoading" description="当前没有提链任务" :image-size="76" />
        <div v-else class="task-list">
          <article v-for="task in filteredTasks" :key="task.taskId" class="task-card" :data-task-id="task.taskId">
            <div class="task-card-head">
              <div>
                <div class="task-title-row">
                  <strong>{{ task.accountEmail || '账号未解析' }}</strong>
                  <el-tag :type="statusTagType(task.status)" effect="plain" round>
                    {{ statusLabel(task.status) }}
                  </el-tag>
                  <el-tag v-if="task.networkError" type="warning" effect="plain" round>网络错误</el-tag>
                </div>
                <p>
                  {{ task.paymentMethod.toUpperCase() }} · {{ task.billingCountry }} ·
                  {{ checkoutKindLabel(task.sessionKind || task.result?.sessionKind) }} ·
                  {{ task.taskId.slice(0, 12) }}
                </p>
              </div>
              <time>{{ formatDate(task.finishedAt || task.startedAt || task.createdAt) }}</time>
            </div>

            <div v-if="!TERMINAL_STATES.has(task.status)" class="task-progress">
              <div><span>{{ stageLabel(task.stage) }}</span><strong>{{ Math.max(0, Math.min(100, task.progress || 0)) }}%</strong></div>
              <el-progress :percentage="Math.max(0, Math.min(100, task.progress || 0))" :show-text="false" />
            </div>

            <div v-if="task.error" class="task-error">{{ redactSensitiveText(task.error) }}</div>
            <div v-if="task.logs?.length" class="task-logs">
              <code v-for="(line, index) in task.logs.slice(-8)" :key="index">{{ redactSensitiveText(line) }}</code>
            </div>

            <div v-if="task.status === 'succeeded'" class="result-box">
              <div v-if="resultUrl(task)" class="result-url">
                <code>{{ resultUrl(task) }}</code>
                <div>
                  <el-button size="small" :icon="CopyDocument" @click="copyText(resultUrl(task), '支付链接')">复制</el-button>
                  <el-button
                    size="small"
                    type="primary"
                    :icon="CreditCard"
                    :data-testid="`open-result-${task.taskId}`"
                    @click="openPaymentUrl(resultUrl(task))"
                  >
                    打开支付页
                  </el-button>
                </div>
              </div>
              <div v-else class="muted">任务成功，但响应中没有可打开的 Provider URL。</div>
              <dl class="result-meta">
                <div><dt>金额 / 币种</dt><dd>{{ formatAmount(task.result) }}</dd></div>
                <div><dt>Checkout 会话</dt><dd>{{ task.result?.checkoutSessionId || '—' }}</dd></div>
                <div><dt>支付方式 ID</dt><dd>{{ task.result?.paymentMethodId || '—' }}</dd></div>
                <div><dt>Stripe 跳转</dt><dd>{{ task.result?.stripeRedirectUrl || '—' }}</dd></div>
              </dl>
            </div>

            <div class="task-actions">
              <el-button
                v-if="!TERMINAL_STATES.has(task.status)"
                size="small"
                :icon="VideoPause"
                :data-testid="`cancel-${task.taskId}`"
                :loading="pendingActions[task.taskId] === 'cancel'"
                @click="cancelTask(task)"
              >
                取消
              </el-button>
              <el-button
                v-if="canRetry(task)"
                size="small"
                :icon="Refresh"
                :data-testid="`retry-${task.taskId}`"
                :loading="pendingActions[task.taskId] === 'retry'"
                @click="retryTask(task)"
              >
                换代理重试
              </el-button>
              <el-button
                v-if="task.status === 'succeeded' && task.paymentMethod === 'paypal'"
                size="small"
                :data-testid="`resolve-${task.taskId}`"
                :loading="pendingActions[task.taskId] === 'resolve'"
                @click="resolvePaypal(task)"
              >
                重新解析 PayPal
              </el-button>
              <el-button
                v-if="TERMINAL_STATES.has(task.status)"
                size="small"
                type="danger"
                plain
                :icon="Delete"
                :data-testid="`delete-${task.taskId}`"
                :loading="pendingActions[task.taskId] === 'delete'"
                @click="deleteTask(task)"
              >
                删除
              </el-button>
            </div>
          </article>
        </div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.heading-stats,
.task-title-row,
.panel-actions,
.proxy-controls,
.submit-row,
.tasks-heading,
.task-toolbar,
.bulk-actions,
.task-actions,
.result-url,
.selected-token-actions,
.submission-settings,
.concurrency-control {
  display: flex;
  align-items: center;
  gap: 10px;
}

.proxy-source-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  margin-bottom: 14px;
}

.heading-stats {
  flex-wrap: wrap;
}

.heading-stats span {
  padding: 7px 11px;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  color: var(--text-secondary);
  background: rgb(16 23 34 / 78%);
  font-size: 12px;
}

.heading-stats strong { color: var(--text-primary); }

.console-grid {
  display: grid;
  grid-template-columns: minmax(420px, 0.9fr) minmax(500px, 1.1fr);
  gap: 18px;
  align-items: start;
}

.setup-stack {
  display: grid;
  gap: 18px;
}

.tool-panel,
.tasks-panel {
  min-width: 0;
  padding: 22px;
}

.tool-title {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 18px;
}

.tool-title h3,
.tool-title p,
.tasks-heading h3,
.tasks-heading p {
  margin: 0;
}

.tool-title p,
.tasks-heading p {
  margin-top: 5px;
  color: var(--text-muted);
  font-size: 13px;
}

.tool-icon {
  display: grid;
  width: 38px;
  height: 38px;
  flex: 0 0 38px;
  place-items: center;
  border: 1px solid rgb(50 197 255 / 24%);
  border-radius: 11px;
  color: #7edcff;
  background: rgb(50 197 255 / 8%);
}

.panel-actions {
  flex-wrap: wrap;
  margin-top: 14px;
}

.panel-actions.no-margin { margin-top: 0; }

.token-list {
  display: grid;
  gap: 8px;
  max-height: 310px;
  margin-top: 17px;
  overflow: auto;
}

.token-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  padding: 11px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  color: var(--text-primary);
  background: rgb(255 255 255 / 2%);
  text-align: left;
  cursor: pointer;
}

.token-item.active {
  border-color: rgb(50 197 255 / 55%);
  background: rgb(50 197 255 / 9%);
}

.token-item span,
.token-item small { display: block; min-width: 0; }
.token-item small {
  margin-top: 4px;
  overflow: hidden;
  color: var(--text-muted);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  text-overflow: ellipsis;
}

.selected-token-actions {
  justify-content: space-between;
  padding: 9px 2px 0;
  color: var(--text-muted);
  font-size: 12px;
}

.proxy-grid,
.parameter-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.proxy-field {
  min-width: 0;
  padding: 13px;
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  background: rgb(255 255 255 / 2%);
}

.field-heading { display: flex; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
.field-heading label { color: var(--text-secondary); font-size: 13px; font-weight: 700; }
.field-heading span { color: var(--text-muted); font-size: 12px; }
.proxy-controls { justify-content: space-between; margin-top: 9px; }
.proxy-result { margin-top: 9px; font-size: 12px; overflow-wrap: anywhere; }
.proxy-result.success { color: var(--success); }
.proxy-result.error,
.task-error { color: var(--danger); }
.storage-note { margin: 12px 0 0; color: var(--text-muted); font-size: 12px; }

.parameter-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.parameter-grid .el-select { width: 100%; }
.captcha-field { grid-column: 1 / -1; }
.field-label { display: block; margin: 0 0 7px; color: var(--text-secondary); font-size: 13px; }
.submit-row { justify-content: space-between; flex-wrap: wrap; margin-top: 18px; }
.submission-settings { flex-wrap: wrap; }
.concurrency-control > span { color: var(--text-secondary); font-size: 13px; font-weight: 700; }
.concurrency-control :deep(.el-input-number) { width: 112px; }
.existing-link { padding-top: 16px; margin-top: 18px; border-top: 1px solid var(--border-subtle); }

.tasks-panel { position: sticky; top: 104px; max-height: calc(100vh - 126px); overflow: hidden; }
.tasks-heading,
.task-toolbar { justify-content: space-between; }
.tasks-heading { padding-bottom: 15px; border-bottom: 1px solid var(--border-subtle); }
.task-toolbar { flex-wrap: wrap; padding: 13px 0; }
.task-list { display: grid; gap: 12px; max-height: calc(100vh - 270px); padding-right: 4px; overflow: auto; }
.task-card { padding: 14px; border: 1px solid var(--border-subtle); border-radius: 12px; background: rgb(255 255 255 / 2%); }
.task-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.task-card-head p { margin: 6px 0 0; color: var(--text-muted); font-size: 12px; }
.task-card-head time { color: var(--text-muted); font-size: 11px; white-space: nowrap; }
.task-title-row { flex-wrap: wrap; }
.task-progress { margin-top: 13px; }
.task-progress > div { display: flex; justify-content: space-between; margin-bottom: 6px; color: var(--text-secondary); font-size: 12px; }
.task-error { padding: 9px 10px; margin-top: 12px; border: 1px solid rgb(255 100 124 / 23%); border-radius: 8px; background: rgb(255 100 124 / 7%); font-size: 12px; overflow-wrap: anywhere; }
.task-logs { display: grid; gap: 3px; max-height: 120px; padding: 9px; margin-top: 10px; overflow: auto; border-radius: 8px; background: rgb(0 0 0 / 20%); }
.task-logs code { color: var(--text-muted); font-size: 11px; overflow-wrap: anywhere; }
.result-box { padding: 11px; margin-top: 12px; border: 1px solid rgb(69 214 138 / 25%); border-radius: 10px; background: rgb(69 214 138 / 6%); }
.result-url { align-items: flex-start; justify-content: space-between; }
.result-url code { min-width: 0; color: #bfffe0; font-size: 12px; overflow-wrap: anywhere; }
.result-url > div { display: flex; flex: 0 0 auto; gap: 6px; }
.result-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 12px 0 0; }
.result-meta div { min-width: 0; padding-top: 8px; border-top: 1px solid rgb(255 255 255 / 6%); }
.result-meta dt { color: var(--text-muted); font-size: 11px; }
.result-meta dd { margin: 4px 0 0; color: var(--text-secondary); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 11px; overflow-wrap: anywhere; }
.task-actions { justify-content: flex-end; flex-wrap: wrap; margin-top: 12px; }

@media (max-width: 1280px) {
  .console-grid { grid-template-columns: 1fr; }
  .tasks-panel { position: static; max-height: none; }
  .task-list { max-height: none; }
}

@media (max-width: 720px) {
  .proxy-grid,
  .parameter-grid,
  .result-meta { grid-template-columns: 1fr; }
  .captcha-field { grid-column: auto; }
  .task-card-head,
  .result-url { flex-direction: column; }
  .result-url > div { flex-wrap: wrap; }
  .tasks-heading,
  .task-toolbar,
  .submit-row { align-items: stretch; flex-direction: column; }
  .submission-settings { justify-content: space-between; }
  .proxy-source-row { grid-template-columns: 1fr; }
}
</style>
