<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  CircleCheck,
  CopyDocument,
  CreditCard,
  Delete,
  Download,
  Link,
  Message,
  Refresh,
  Select,
  Setting,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import SecretCell from '@/components/SecretCell.vue'
import StatCard from '@/components/StatCard.vue'
import { dataGateway } from '@/services/dataGateway'
import { copyText, downloadEncodedFile } from '@/services/exporter'
import type {
  PipelineItem,
  PipelinePaidExport,
  PipelinePaidExportBatch,
  PipelinePaidExportFormat,
  PipelinePaidStats,
  SmsReceiverHeroSmsCountry,
  SmsReceiverHeroSmsSettings,
  SmsReceiverSettings,
} from '@/types'

const items = ref<PipelineItem[]>([])
const total = ref(0)
const loading = ref(false)
const exportLoading = ref(false)
const mailLoading = ref(false)
const markingLoading = ref(false)
const deleteLoading = ref(false)
const receiverSaving = ref(false)
const receiverTesting = ref(false)
const receiverActionLoading = ref(false)
const receiverRetryLoading = ref(false)
const receiverConfigVisible = ref(false)
const exportFormats = ref<PipelinePaidExportFormat[]>(['original'])
const exportFormatOptions: Array<{ label: string; value: PipelinePaidExportFormat; description: string }> = [
  { label: '原格式', value: 'original', description: '邮箱与接码 URL' },
  { label: '密码+2FA', value: 'password_totp', description: '邮箱----密码----2FA' },
  { label: 'Sub2 合并', value: 'sub2api', description: 'Sub2 合并 JSON' },
  { label: 'Sub2 分开 ZIP', value: 'sub2api_split', description: '每个账号一个 Sub2 JSON' },
  { label: 'Codex JSON', value: 'codex_json', description: 'Codex OAuth JSON' },
]
const exportPackaging = ref<'separate' | 'zip'>('separate')
const exportFormatSummary = computed(() => exportFormatOptions
  .filter((option) => exportFormats.value.includes(option.value))
  .map((option) => option.description)
  .join('、'))
const hasCopyableExportFormat = computed(() => exportFormats.value.some((format) => format !== 'codex_json'))
const receiverSettings = ref<SmsReceiverSettings>({
  enabled: false,
  autoSubmit: false,
  baseUrl: 'http://127.0.0.1:5015',
  mailboxPublicBaseUrl: '',
  concurrency: 3,
  failureRetries: 1,
  retryBackoffSeconds: 30,
  updatedAt: null,
})
const receiverHeroSmsSettings = ref<SmsReceiverHeroSmsSettings>({
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
  credentialConfigured: false,
})
const receiverHeroSmsCountries = ref<SmsReceiverHeroSmsCountry[]>([])
const receiverCountryMap = computed(() => new Map(
  receiverHeroSmsCountries.value.map((country) => [Number(country.id), country]),
))
const receiverPriorityCountries = computed(() => receiverHeroSmsSettings.value.countryIds.map((id, index) => ({
  id,
  index,
  country: receiverCountryMap.value.get(Number(id)),
})))
const receiverCountryIdsText = ref('16')
const mailboxDialogVisible = ref(false)
const mailboxFrameUrl = ref('')
const mailboxFrameKey = ref(0)
const mailboxEmail = ref('')
const tableRef = ref<{ clearSelection: () => void; toggleRowSelection: (row: PipelineItem, selected: boolean) => void } | null>(null)
const currentPage = ref(1)
const pageSize = ref(20)
const search = ref('')
const exportState = ref<'all' | 'exported' | 'unexported'>('all')
const settlementState = ref<'all' | 'waiting' | 'confirmed' | 'review' | 'failed'>('all')
const receiverState = ref<'all' | 'verified' | 'unverified' | 'failed' | 'pending'>('all')
const selectedIds = ref<string[]>([])
const selectedReceiverIds = computed(() => {
  const selected = new Set(selectedIds.value)
  return items.value
    .filter((item) => selected.has(item.id) && receiverEligible(item))
    .map((item) => item.id)
})
const selectedReceiverRetryIds = computed(() => {
  const selected = new Set(selectedIds.value)
  return items.value
    .filter((item) => selected.has(item.id)
      && receiverEligible(item)
      && ['failed', 'stopped'].includes(item.smsReceiverState || ''))
    .map((item) => item.id)
})
const selectionAnchorId = ref<string | null>(null)
const shiftPressed = ref(false)
const stats = ref<PipelinePaidStats>({
  total: 0,
  today: 0,
  last7Days: 0,
  terminalTotal: 0,
  failed: 0,
  successRate: 0,
  averageHeroSmsPrice: null,
  exported: 0,
  unexported: 0,
  mailConfirmed: 0,
  smsVerified: 0,
  smsUnverified: 0,
  daily: [],
})
let pollTimer: ReturnType<typeof setInterval> | undefined

function formatDate(value?: string | null) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(value))
}

async function loadData(quiet = false) {
  if (!quiet) loading.value = true
  try {
    const [list, overview] = await Promise.all([
      dataGateway.listPipeline({
        page: currentPage.value,
        pageSize: pageSize.value,
        stage: 'paid',
        q: search.value,
        exportState: exportState.value,
        settlementState: settlementState.value,
        receiverState: receiverState.value,
      }),
      dataGateway.paidPipelineStats(14),
    ])
    items.value = list.items
    total.value = list.total
    stats.value = overview
    if (!quiet) {
      selectedIds.value = []
      selectionAnchorId.value = null
    }
  } catch (error) {
    if (!quiet) ElMessage.error(error instanceof Error ? error.message : '成品账号读取失败')
  } finally {
    if (!quiet) loading.value = false
  }
}

function submitSearch() {
  currentPage.value = 1
  void loadData()
}

function handleSelection(rows: PipelineItem[]) {
  selectedIds.value = rows.map((item) => item.id)
}

function handleRowSelect(rows: PipelineItem[], row: PipelineItem) {
  const selected = rows.some((item) => item.id === row.id)
  if (shiftPressed.value && selectionAnchorId.value && selectionAnchorId.value !== row.id) {
    void selectRange(row, selected)
    return
  }
  selectionAnchorId.value = row.id
}

function isInteractiveTarget(target: EventTarget | null) {
  return target instanceof Element && Boolean(target.closest('button, a, input, label, [role="button"], .el-checkbox'))
}

async function selectRange(target: PipelineItem, selected = true) {
  const anchorIndex = items.value.findIndex((item) => item.id === selectionAnchorId.value)
  const targetIndex = items.value.findIndex((item) => item.id === target.id)
  if (anchorIndex < 0 || targetIndex < 0) {
    tableRef.value?.toggleRowSelection(target, selected)
    selectionAnchorId.value = target.id
    return
  }
  const start = Math.min(anchorIndex, targetIndex)
  const end = Math.max(anchorIndex, targetIndex)
  items.value.slice(start, end + 1).filter(selectable).forEach((item) => {
    tableRef.value?.toggleRowSelection(item, selected)
  })
  await nextTick()
}

function handleRowClick(row: PipelineItem, _column: unknown, event: MouseEvent) {
  if (!selectable(row) || isInteractiveTarget(event.target)) return
  if (event.shiftKey && selectionAnchorId.value) {
    void selectRange(row, true)
    return
  }
  const selected = selectedIds.value.includes(row.id)
  tableRef.value?.toggleRowSelection(row, !selected)
  selectionAnchorId.value = row.id
}

function handleKeyDown(event: KeyboardEvent) {
  if (event.key === 'Shift') shiftPressed.value = true
}

function handleKeyUp(event: KeyboardEvent) {
  if (event.key === 'Shift') shiftPressed.value = false
}

function clearSelection() {
  tableRef.value?.clearSelection()
  selectedIds.value = []
  selectionAnchorId.value = null
}

function selectable(item: PipelineItem) {
  return Boolean(item.email)
}

function receiverEligible(item: PipelineItem) {
  return Boolean(
    item.email
    && item.chatgptPassword?.trim()
    && item.totpSecret?.trim(),
  )
}

async function quickSelect(count?: number) {
  clearSelection()
  await nextTick()
  const rows = items.value.filter(selectable).slice(0, count || items.value.length)
  rows.forEach((item) => tableRef.value?.toggleRowSelection(item, true))
  selectionAnchorId.value = rows.at(-1)?.id || null
}

async function restoreSelection(ids: string[]) {
  await nextTick()
  const selected = new Set(ids)
  items.value.filter((item) => selected.has(item.id)).forEach((item) => {
    tableRef.value?.toggleRowSelection(item, true)
  })
  selectionAnchorId.value = ids.at(-1) || null
}

async function markExported(exported: boolean) {
  if (!selectedIds.value.length) return
  markingLoading.value = true
  try {
    const result = await dataGateway.markPaidPipelineExport(selectedIds.value, exported)
    ElMessage.success(exported ? `已标记 ${result.updated} 个账号为已导出` : `已恢复 ${result.updated} 个账号为未导出`)
    await loadData()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '导出状态更新失败')
  } finally {
    markingLoading.value = false
  }
}

async function deleteSelected() {
  const ids = [...selectedIds.value]
  if (!ids.length) return
  if (!window.confirm(`确定从成品管理删除选中的 ${ids.length} 条记录吗？账号池和接码机素材不会删除。`)) return
  deleteLoading.value = true
  try {
    let deleted = 0
    let failed = 0
    for (const id of ids) {
      try {
        deleted += await dataGateway.deletePipeline(id)
      } catch {
        failed += 1
      }
    }
    ElMessage.success(`已删除 ${deleted} 条成品记录`)
    if (failed) ElMessage.warning(`${failed} 条删除失败，请刷新后重试`)
    await loadData()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '成品删除失败')
  } finally {
    deleteLoading.value = false
  }
}

async function deleteOne(item: PipelineItem) {
  selectedIds.value = [item.id]
  await deleteSelected()
}

async function checkMail(ids = selectedIds.value) {
  if (!ids.length) return
  const previousSelection = [...selectedIds.value]
  mailLoading.value = true
  try {
    const result = await dataGateway.checkPaidPipelineMail(ids)
    ElMessage.success(`已检查 ${result.checked} 个，邮件确认 ${result.confirmed} 个`)
    if (result.waiting || result.review || result.failed) {
      ElMessage.warning(`等待到账 ${result.waiting || 0} 个，待复核 ${result.review || 0} 个，检查异常 ${result.failed} 个`)
    }
    await loadData(true)
    await restoreSelection(previousSelection)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '确认邮件检查失败')
  } finally {
    mailLoading.value = false
  }
}

function mailStatus(item: PipelineItem) {
  const labels = {
    unchecked: '等待到账', waiting: '等待到账', confirmed: '邮件已确认',
    not_found: '到账待复核', review: '到账待复核', failed: '邮箱检查异常',
  }
  return labels[item.mailConfirmationStatus || 'unchecked']
}

function mailStatusType(item: PipelineItem) {
  if (item.mailConfirmationStatus === 'confirmed') return 'success'
  if (item.mailConfirmationStatus === 'failed') return 'danger'
  if (item.mailConfirmationStatus === 'not_found' || item.mailConfirmationStatus === 'review') return 'warning'
  return 'info'
}

function checkoutTypeLabel(item: PipelineItem) {
  if (item.checkoutType === 'oaics') return 'OAICS'
  if (item.checkoutType === 'cs') return 'CS'
  return '待判断'
}

function receiverStatusLabel(item: PipelineItem) {
  if (item.smsReceiverPhoneVerified || item.smsReceiverCredentialReady) {
    return item.smsReceiverPhoneNumber
      ? `已接码 ${item.smsReceiverPhoneNumber}`
      : item.smsReceiverCredentialReady ? '已接码（凭证已就绪）' : '手机号已接码'
  }
  const labels: Record<string, string> = {
    idle: '未送出', waiting: '等待接码机空闲', queued: '已排队', running: '接码中', retry_wait: '等待重试',
    paused: '已暂停', completed: '归档中', ready: '凭证已就绪', failed: '送出失败', stopped: '已停止',
  }
  return labels[item.smsReceiverState || 'idle'] || item.smsReceiverState || '未送出'
}

function receiverStatusType(item: PipelineItem) {
  if (item.smsReceiverPhoneVerified || item.smsReceiverCredentialReady) return 'success'
  if (item.smsReceiverState === 'failed' || item.smsReceiverState === 'stopped') return 'danger'
  if (item.smsReceiverState && item.smsReceiverState !== 'idle') return 'warning'
  return 'info'
}

async function loadReceiverSettings(showHeroSmsError = false) {
  let receiver: SmsReceiverSettings
  try {
    receiver = await dataGateway.smsReceiverSettings()
    receiverSettings.value = {
      ...receiver,
      baseUrl: receiver.baseUrl || 'http://127.0.0.1:5015',
    }
  } catch (error) {
    if (showHeroSmsError) ElMessage.error(error instanceof Error ? error.message : 'HeroSMS 接码服务配置读取失败')
    return
  }
  if (!receiver.baseUrl) return
  try {
    const heroSms = await dataGateway.smsReceiverHeroSmsSettings()
    const countryIds = heroSms.countryIds.length ? heroSms.countryIds : [16]
    receiverHeroSmsSettings.value = {
      ...heroSms,
      apiKey: '',
      countryIds,
      minPrice: heroSms.minPrice && heroSms.minPrice > 0 ? heroSms.minPrice : null,
      maxPrice: heroSms.maxPrice > 0 ? heroSms.maxPrice : 1,
      preferredPrice: heroSms.preferredPrice && heroSms.preferredPrice > 0 ? heroSms.preferredPrice : null,
      acquirePriority: ['country', 'price', 'price_high'].includes(heroSms.acquirePriority)
        ? heroSms.acquirePriority
        : 'country',
      maxRetries: heroSms.maxRetries > 0 ? heroSms.maxRetries : 3,
      codeWaitSeconds: heroSms.codeWaitSeconds >= 30 ? heroSms.codeWaitSeconds : 180,
      emailOtpWaitSeconds: heroSms.emailOtpWaitSeconds >= 30 ? heroSms.emailOtpWaitSeconds : 90,
      emailOtpPollIntervalSeconds: heroSms.emailOtpPollIntervalSeconds >= 1
        ? heroSms.emailOtpPollIntervalSeconds
        : 3,
      emailOtpAttempts: heroSms.emailOtpAttempts >= 1 ? heroSms.emailOtpAttempts : 1,
    }
    receiverCountryIdsText.value = countryIds.join(', ')
  } catch (error) {
    if (showHeroSmsError) ElMessage.warning(error instanceof Error ? error.message : 'HeroSMS 服务尚未连接')
    return
  }
  try {
    const catalog = await dataGateway.smsReceiverHeroSmsCatalog()
    receiverHeroSmsCountries.value = catalog.countries || []
  } catch {
    receiverHeroSmsCountries.value = []
  }
}

function openReceiverConfig() {
  receiverConfigVisible.value = true
  void loadReceiverSettings(true)
}

function normalizedReceiverCountries() {
  if (receiverHeroSmsCountries.value.length) return receiverHeroSmsSettings.value.countryIds
  return receiverCountryIdsText.value
    .split(/[,，\s]+/)
    .map((value) => Number(value.trim()))
    .filter((value, index, values) => Number.isInteger(value) && value > 0 && values.indexOf(value) === index)
}

function moveReceiverCountry(index: number, direction: -1 | 1) {
  const target = index + direction
  const countries = [...receiverHeroSmsSettings.value.countryIds]
  if (target < 0 || target >= countries.length) return
  ;[countries[index], countries[target]] = [countries[target]!, countries[index]!]
  receiverHeroSmsSettings.value.countryIds = countries
}

function removeReceiverCountry(id: number) {
  receiverHeroSmsSettings.value.countryIds = receiverHeroSmsSettings.value.countryIds
    .filter((countryId) => Number(countryId) !== Number(id))
}

function decodeUtf8Base64(content: string) {
  const binary = window.atob(content)
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0))
  return new TextDecoder().decode(bytes)
}

async function saveReceiverSettings(showMessage = true) {
  receiverSaving.value = true
  try {
    const countryIds = normalizedReceiverCountries()
    if (!countryIds.length) {
      ElMessage.warning('请至少选择一个 HeroSMS 接码国家')
      return false
    }
    if (countryIds.length > 10) {
      ElMessage.warning('HeroSMS 国家优先队列最多 10 个')
      return false
    }
    const minPrice = receiverHeroSmsSettings.value.minPrice && receiverHeroSmsSettings.value.minPrice > 0
      ? receiverHeroSmsSettings.value.minPrice
      : null
    const preferredPrice = receiverHeroSmsSettings.value.preferredPrice && receiverHeroSmsSettings.value.preferredPrice > 0
      ? receiverHeroSmsSettings.value.preferredPrice
      : null
    if (minPrice !== null && minPrice > receiverHeroSmsSettings.value.maxPrice) {
      ElMessage.warning('最低价格不能高于最高单价')
      return false
    }
    if (preferredPrice !== null && (
      preferredPrice > receiverHeroSmsSettings.value.maxPrice
      || (minPrice !== null && preferredPrice < minPrice)
    )) {
      ElMessage.warning('指定价格必须位于最低价格与最高单价之间')
      return false
    }
    const receiver = await dataGateway.updateSmsReceiverSettings({
      enabled: receiverSettings.value.enabled,
      autoSubmit: receiverSettings.value.autoSubmit,
      baseUrl: receiverSettings.value.baseUrl.trim() || 'http://127.0.0.1:5015',
      mailboxPublicBaseUrl: receiverSettings.value.mailboxPublicBaseUrl.trim(),
      concurrency: receiverSettings.value.concurrency,
      failureRetries: receiverSettings.value.failureRetries,
      retryBackoffSeconds: receiverSettings.value.retryBackoffSeconds,
    })
    const heroSms = await dataGateway.updateSmsReceiverHeroSmsSettings({
      ...receiverHeroSmsSettings.value,
      apiKey: receiverHeroSmsSettings.value.apiKey.trim(),
      countryIds,
      minPrice,
      preferredPrice,
    })
    receiverSettings.value = receiver
    const savedCountryIds = heroSms.countryIds.length ? heroSms.countryIds : countryIds
    receiverHeroSmsSettings.value = { ...heroSms, apiKey: '', countryIds: savedCountryIds }
    receiverCountryIdsText.value = savedCountryIds.join(', ')
    if (showMessage) {
      ElMessage.success('HeroSMS 接码配置已保存')
      receiverConfigVisible.value = false
    }
    return true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : 'HeroSMS 配置保存失败')
    return false
  } finally {
    receiverSaving.value = false
  }
}

async function testReceiver() {
  receiverTesting.value = true
  try {
    if (!await saveReceiverSettings(false)) return
    const result = await dataGateway.testSmsReceiver()
    ElMessage.success(`HeroSMS 接码服务连接正常${result.service ? `：${result.service}` : ''}`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : 'HeroSMS 接码服务连接失败')
  } finally {
    receiverTesting.value = false
  }
}

async function submitToReceiver(ids = selectedIds.value) {
  if (!ids.length) return
  const rowsById = new Map(items.value.map((item) => [item.id, item]))
  const eligibleIds = ids.filter((id) => {
    const item = rowsById.get(id)
    return item ? receiverEligible(item) : true
  })
  const locallySkipped = ids.length - eligibleIds.length
  if (locallySkipped) {
    ElMessage.warning(`已跳过 ${locallySkipped} 个缺少密码或 2FA 的账号`)
  }
  if (!eligibleIds.length) return
  const previousSelection = [...selectedIds.value]
  receiverActionLoading.value = true
  try {
    const result = await dataGateway.submitPaidToSmsReceiver(eligibleIds)
    if (result.submitted) ElMessage.success(`已启动 ${result.submitted} 个 HeroSMS 接码任务`)
    if (result.skipped) ElMessage.warning(`接码机已跳过 ${result.skipped} 个素材不完整或 2FA 无效的账号`)
    if (result.failed) ElMessage.warning(`${result.failed} 个接码任务启动失败，请查看状态`)
    await loadData(true)
    await restoreSelection(previousSelection)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : 'HeroSMS 接码启动失败')
  } finally {
    receiverActionLoading.value = false
  }
}

async function refreshReceiver(ids = selectedIds.value) {
  if (!ids.length) return
  const previousSelection = [...selectedIds.value]
  receiverActionLoading.value = true
  try {
    const result = await dataGateway.refreshSmsReceiverStatus(ids)
    ElMessage.success(`已刷新 ${result.processed} 个，凭证就绪 ${result.ready || 0} 个`)
    if (result.failed) ElMessage.warning(`${result.failed} 个状态查询失败`)
    await loadData(true)
    await restoreSelection(previousSelection)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '接码机状态刷新失败')
  } finally {
    receiverActionLoading.value = false
  }
}

async function retryReceiver(ids = selectedReceiverRetryIds.value) {
  if (!ids.length) return
  const previousSelection = [...selectedIds.value]
  receiverRetryLoading.value = true
  try {
    const result = await dataGateway.retryPaidSmsReceiver(ids)
    if (result.queued) ElMessage.success(`已将 ${result.queued} 个失败任务加入接码队列`)
    if (result.skipped) ElMessage.warning(`${result.skipped} 个账号资料不完整或已经接码`)
    await loadData(true)
    await restoreSelection(previousSelection)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '接码重试入队失败')
  } finally {
    receiverRetryLoading.value = false
  }
}

function toggleExportFormat(format: PipelinePaidExportFormat) {
  if (exportFormats.value.includes(format)) {
    if (exportFormats.value.length === 1) {
      ElMessage.warning('请至少保留一种导出格式')
      return
    }
    exportFormats.value = exportFormats.value.filter((value) => value !== format)
    return
  }
  exportFormats.value = exportFormatOptions
    .map((option) => option.value)
    .filter((value) => value === format || exportFormats.value.includes(value))
}

function exportEmptyMessage(format: PipelinePaidExportFormat) {
  const messages: Record<PipelinePaidExportFormat, string> = {
    original: '没有包含接码 URL 的成品账号',
    password_totp: '没有同时包含密码和 2FA 的成品账号',
    sub2api: '没有可导出的 Sub2 凭证',
    sub2api_split: '没有可导出的 Sub2 凭证',
    codex_json: '没有可导出的 Codex OAuth 凭证',
  }
  return messages[format]
}

function showExportSkipMessages(
  result: PipelinePaidExport,
  format: PipelinePaidExportFormat,
  includeFormat: boolean,
) {
  const prefix = includeFormat ? `${exportFormatOptions.find((option) => option.value === format)?.label || format}：` : ''
  if (result.skippedMissingUrlCount) {
    ElMessage.warning(`${prefix}另有 ${result.skippedMissingUrlCount} 条缺少接码 URL`)
  }
  if (result.skippedMissingSecurityCount) {
    ElMessage.warning(`${prefix}另有 ${result.skippedMissingSecurityCount} 条缺少密码或 2FA`)
  }
  if (result.skippedMissingCredentialCount) {
    ElMessage.warning(`${prefix}另有 ${result.skippedMissingCredentialCount} 条缺少 OAuth 凭证`)
  }
  if (result.skippedReceiverAccountCount) {
    ElMessage.warning(`${prefix}另有 ${result.skippedReceiverAccountCount} 条尚未完成 HeroSMS 接码`)
  }
}

function isExportBatch(result: Awaited<ReturnType<typeof dataGateway.exportPaidPipeline>>): result is PipelinePaidExportBatch {
  return Array.isArray((result as PipelinePaidExportBatch).exports)
}

async function exportRecords(delivery: 'copy' | 'download', selected: boolean) {
  const formats = exportFormatOptions
    .map((option) => option.value)
    .filter((format) => exportFormats.value.includes(format))
  const requestedFormats = delivery === 'copy'
    ? formats.filter((format) => format !== 'codex_json')
    : formats
  if (!requestedFormats.length) {
    ElMessage.warning('Codex JSON 需要使用下载导出')
    return
  }
  exportLoading.value = true
  try {
    const copied: Array<{ format: PipelinePaidExportFormat; content: string; count: number }> = []
    let downloaded = 0
    let exportedCount = 0
    let response: Awaited<ReturnType<typeof dataGateway.exportPaidPipeline>>
    try {
      const exportArgs = [
        selected ? selectedIds.value : [],
        search.value,
        exportState.value,
        requestedFormats.length === 1 ? requestedFormats[0]! : requestedFormats,
      ] as const
      response = requestedFormats.length > 1
        ? await dataGateway.exportPaidPipeline(...exportArgs, exportPackaging.value)
        : await dataGateway.exportPaidPipeline(...exportArgs)
    } catch (error) {
      ElMessage.error(`导出失败：${error instanceof Error ? error.message : '未知错误'}`)
      return
    }

    const results = isExportBatch(response) ? response.exports : [response]
    if (delivery === 'download' && isExportBatch(response) && response.archive) {
      downloadEncodedFile(
        response.archive.contentBase64 || response.archive.content,
        response.archive.filename,
        response.archive.mimeType,
        response.archive.encoding,
      )
      response.exports.forEach((result) => {
        exportedCount += result.count
        showExportSkipMessages(result, result.format, true)
      })
      ElMessage.success(`已生成合并压缩包，包含 ${response.exports.length} 种格式`)
      if (exportedCount) await loadData()
      return
    }
    const resultByFormat = new Map(results.map((result) => [result.format, result]))
    if (isExportBatch(response)) {
      response.errors.forEach((error) => {
        const label = exportFormatOptions.find((option) => option.value === error.format)?.label || error.format
        ElMessage.error(`${label}导出失败：${error.message}`)
      })
    }

    for (const format of requestedFormats) {
      const result = resultByFormat.get(format)
      if (!result) continue
      if (!result.count) {
        const label = requestedFormats.length > 1
          ? `${exportFormatOptions.find((option) => option.value === format)?.label || format}：`
          : ''
        ElMessage.warning(`${label}${exportEmptyMessage(format)}`)
        continue
      }
      exportedCount += result.count
      if (delivery === 'copy') {
        copied.push({
          format,
          content: result.encoding === 'base64'
            ? decodeUtf8Base64(result.contentBase64 || result.content)
            : result.content,
          count: result.count,
        })
      } else {
        downloadEncodedFile(
          result.encoding === 'base64' ? (result.contentBase64 || result.content) : result.content,
          result.filename,
          result.mimeType,
          result.encoding,
        )
        downloaded += 1
      }
      showExportSkipMessages(result, format, requestedFormats.length > 1)
    }
    if (delivery === 'copy' && copied.length) {
      const content = copied.length === 1
        ? copied[0]!.content
        : copied.map((entry) => {
          const label = exportFormatOptions.find((option) => option.value === entry.format)?.label || entry.format
          return `【${label}】\n${entry.content}`
        }).join('\n\n')
      await copyText(content)
      ElMessage.success(copied.length === 1
        ? `已复制 ${copied[0]!.count} 条`
        : `已复制 ${copied.length} 种格式，共 ${exportedCount} 条`)
    }
    if (delivery === 'download' && downloaded) {
      ElMessage.success(downloaded === 1 ? '已生成 1 个导出文件' : `已分别生成 ${downloaded} 个导出文件`)
    }
    if (delivery === 'copy' && formats.includes('codex_json')) {
      ElMessage.warning('Codex JSON 未复制，请使用导出按钮下载')
    }
    if (exportedCount) await loadData()
  } finally {
    exportLoading.value = false
  }
}

async function copyRecord(item: PipelineItem) {
  if (!item.emailAccessUrl) return
  const result = await dataGateway.exportPaidPipeline([item.id], '', 'all', 'original') as PipelinePaidExport
  await copyText(result.content)
  ElMessage.success('已复制并标记为已导出')
  await loadData()
}

function parseMailboxUrl(item: PipelineItem) {
  if (!item.emailAccessUrl) return
  try {
    const url = new URL(item.emailAccessUrl)
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error('unsupported_protocol')
    return url
  } catch {
    ElMessage.error('接码 URL 格式无效')
  }
}

function isLocalMailComUrl(url: URL) {
  return ['127.0.0.1', 'localhost'].includes(url.hostname)
    && url.port === '3211'
    && url.pathname.replace(/\/$/, '') === '/api/mail/latest'
}

function localMailComViewerUrl(url: URL) {
  const email = url.searchParams.get('email') || ''
  return `${url.origin}/static/mailbox-viewer.html?${new URLSearchParams({ email }).toString()}`
}

function viewMailbox(item: PipelineItem) {
  const url = parseMailboxUrl(item)
  if (!url) return
  if (!isLocalMailComUrl(url)) {
    window.open(url.toString(), '_blank', 'noopener,noreferrer')
    return
  }
  mailboxEmail.value = item.email
  mailboxFrameUrl.value = localMailComViewerUrl(url)
  mailboxFrameKey.value += 1
  mailboxDialogVisible.value = true
}

function refreshMailbox() {
  mailboxFrameKey.value += 1
}

function openMailbox(item: PipelineItem) {
  const url = parseMailboxUrl(item)
  if (!url) return
  window.open(url.toString(), '_blank', 'noopener,noreferrer')
}

function openCurrentMailbox() {
  if (!mailboxFrameUrl.value) return
  window.open(mailboxFrameUrl.value, '_blank', 'noopener,noreferrer')
}

onMounted(() => {
  void loadData()
  void loadReceiverSettings()
  window.addEventListener('keydown', handleKeyDown)
  window.addEventListener('keyup', handleKeyUp)
  pollTimer = setInterval(() => {
    if (!selectedIds.value.length) void loadData(true)
  }, 5000)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleKeyDown)
  window.removeEventListener('keyup', handleKeyUp)
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <section class="paid-page">
    <div class="page-heading paid-heading">
      <div>
        <h2>成品管理</h2>
        <p>集中管理已支付账号；到账状态以付款后的订阅确认邮件为准。</p>
      </div>
      <div class="heading-actions">
        <el-button :icon="Refresh" :loading="loading" @click="loadData()">刷新</el-button>
      </div>
    </div>

    <div class="stats-grid paid-stats">
      <StatCard label="成品总数" :value="stats.total" note="累计入库账号" :icon="CircleCheck" tone="green" />
      <StatCard label="今日入库" :value="stats.today" note="日本自然日" :icon="CreditCard" />
      <StatCard label="未导出" :value="stats.unexported" note="等待交付账号" :icon="Download" tone="amber" />
      <StatCard label="邮件已确认" :value="stats.mailConfirmed" note="已匹配到账确认邮件" :icon="Message" tone="green" />
      <StatCard label="已接码" :value="stats.smsVerified || 0" note="手机号已验证或接码凭证已就绪" :icon="Message" tone="green" />
    </div>

    <div class="panel operation-panel">
      <div class="operation-group receiver-tools">
        <div class="tool-title">
          <strong>HeroSMS 接码</strong>
          <el-tag :type="receiverSettings.enabled ? 'success' : 'info'" effect="plain">
            {{ receiverSettings.enabled ? '已启用' : '未启用' }}
          </el-tag>
          <span>需邮箱 + 密码 + 2FA</span>
          <span>导入格式：邮箱|密码|2FA</span>
          <span v-if="receiverHeroSmsSettings.credentialConfigured">密钥已配置</span>
        </div>
        <el-button :icon="Setting" @click="openReceiverConfig">HeroSMS 配置</el-button>
        <el-button
          type="primary"
          :disabled="!selectedReceiverIds.length || !receiverSettings.enabled"
          :loading="receiverActionLoading"
          @click="submitToReceiver()"
        >接码选中 {{ selectedReceiverIds.length || '' }}</el-button>
        <el-button
          :icon="Refresh"
          :disabled="!selectedIds.length || !receiverSettings.enabled"
          :loading="receiverActionLoading"
          @click="refreshReceiver()"
        >刷新状态</el-button>
      </div>
      <div class="operation-divider" />
      <div class="operation-group export-tools">
        <div class="tool-title">
          <strong>导出</strong>
          <span>{{ exportFormatSummary }}</span>
          <el-tag type="primary" effect="plain">已选 {{ selectedIds.length }} 个账号</el-tag>
        </div>
        <div class="export-format-picker" role="group" aria-label="导出格式（可多选）">
          <el-button
            v-for="option in exportFormatOptions"
            :key="option.value"
            class="export-format-option"
            :type="exportFormats.includes(option.value) ? 'primary' : 'default'"
            :plain="!exportFormats.includes(option.value)"
            :aria-pressed="exportFormats.includes(option.value)"
            @click="toggleExportFormat(option.value)"
          >{{ option.label }}</el-button>
        </div>
        <el-select v-if="exportFormats.length > 1" v-model="exportPackaging" class="export-packaging" aria-label="多格式导出方式">
          <el-option label="分别下载" value="separate" />
          <el-option label="合并为压缩包" value="zip" />
        </el-select>
        <el-button
          :icon="CopyDocument"
          :loading="exportLoading"
          :disabled="selectedIds.length === 0 || !hasCopyableExportFormat"
          @click="exportRecords('copy', true)"
        >复制选中</el-button>
        <el-button
          type="primary"
          plain
          :icon="Download"
          :loading="exportLoading"
          :disabled="selectedIds.length === 0"
          @click="exportRecords('download', true)"
        >导出选中</el-button>
        <el-button type="primary" :icon="Download" :loading="exportLoading" @click="exportRecords('download', false)">
          导出全部
        </el-button>
      </div>
    </div>

    <div class="panel table-panel paid-table-panel">
      <div class="table-toolbar">
        <div class="toolbar-copy">
          <strong>成品账号明细</strong>
          <span>共 {{ total }} 条</span>
        </div>
        <el-input
          v-model="search"
          class="search-input"
          clearable
          placeholder="搜索账号邮箱"
          @keyup.enter="submitSearch"
          @clear="submitSearch"
        />
        <el-select v-model="exportState" class="export-filter" aria-label="导出状态" @change="submitSearch">
          <el-option label="全部导出状态" value="all" />
          <el-option label="未导出" value="unexported" />
          <el-option label="已导出" value="exported" />
        </el-select>
        <el-select v-model="settlementState" class="settlement-filter" aria-label="到账状态" @change="submitSearch">
          <el-option label="全部到账状态" value="all" />
          <el-option label="等待到账" value="waiting" />
          <el-option label="邮件已确认" value="confirmed" />
          <el-option label="到账待复核" value="review" />
          <el-option label="邮箱检查异常" value="failed" />
        </el-select>
        <el-select v-model="receiverState" class="receiver-filter" aria-label="接码状态" @change="submitSearch">
          <el-option label="全部接码状态" value="all" />
          <el-option label="已接码" value="verified" />
          <el-option label="未接码" value="unverified" />
          <el-option label="接码中/排队" value="pending" />
          <el-option label="接码失败" value="failed" />
        </el-select>
      </div>
      <div class="selection-toolbar">
        <el-tag class="selection-count" :type="selectedIds.length ? 'primary' : 'info'" effect="plain">
          已选 {{ selectedIds.length }} 个账号
        </el-tag>
        <span>快速选择：</span>
        <el-button size="small" @click="quickSelect(10)">前 10</el-button>
        <el-button size="small" @click="quickSelect(20)">前 20</el-button>
        <el-button size="small" @click="quickSelect(50)">前 50</el-button>
        <el-button size="small" @click="quickSelect()">本页</el-button>
        <span class="shift-selection-hint">先选一行，再按住 Shift 点击另一行，可连续选择整段</span>
        <el-button size="small" :disabled="!selectedIds.length" :loading="mailLoading" :icon="Refresh" @click="checkMail()">重新检查到账</el-button>
        <el-button size="small" type="warning" :disabled="!selectedReceiverRetryIds.length || !receiverSettings.enabled" :loading="receiverRetryLoading" :icon="Refresh" @click="retryReceiver()">失败重试入队 {{ selectedReceiverRetryIds.length || '' }}</el-button>
        <el-button size="small" :disabled="!selectedIds.length" :loading="markingLoading" :icon="Select" @click="markExported(true)">标记已导出</el-button>
        <el-button size="small" :disabled="!selectedIds.length" :loading="markingLoading" @click="markExported(false)">恢复未导出</el-button>
        <el-button size="small" type="danger" :disabled="!selectedIds.length" :loading="deleteLoading" :icon="Delete" @click="deleteSelected">删除选中</el-button>
        <el-button size="small" :disabled="!selectedIds.length" @click="clearSelection">取消选择</el-button>
      </div>
      <el-table
        ref="tableRef"
        v-loading="loading"
        :data="items"
        row-key="id"
        @select="handleRowSelect"
        @selection-change="handleSelection"
        @row-click="handleRowClick"
      >
        <el-table-column type="selection" width="44" :selectable="selectable" />
        <el-table-column label="账号邮箱" min-width="205">
          <template #default="{ row }">
            <div class="account-cell">
              <strong>{{ row.email }}</strong>
              <div class="account-meta">
                <el-tag
                  :type="row.checkoutType === 'oaics' ? 'success' : row.checkoutType === 'cs' ? 'warning' : 'info'"
                  effect="plain"
                  size="small"
                >{{ checkoutTypeLabel(row) }}</el-tag>
                <span>支付 {{ formatDate(row.paidAt) }}</span>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="账号资料" min-width="220">
          <template #default="{ row }">
            <div class="credential-cell">
              <div><span>密码</span><SecretCell :value="row.chatgptPassword" /></div>
              <div><span>2FA</span><SecretCell :value="row.totpSecret" /></div>
              <div><span>邮箱</span><SecretCell :value="row.emailAccessUrl" /></div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="交付状态" min-width="250">
          <template #default="{ row }">
            <div class="detail-cell">
              <div class="status-tags">
                <el-tag :type="mailStatusType(row)" effect="plain">{{ mailStatus(row) }}</el-tag>
                <el-tag :type="receiverStatusType(row)" effect="plain">{{ receiverStatusLabel(row) }}</el-tag>
              </div>
              <span v-if="row.mailConfirmationReceivedAt">{{ formatDate(row.mailConfirmationReceivedAt) }}</span>
              <span v-if="row.mailConfirmationOrderId" class="truncate" :title="row.mailConfirmationOrderId">订单 {{ row.mailConfirmationOrderId }}</span>
              <span v-if="row.mailConfirmationError" class="error-text">{{ row.mailConfirmationError }}</span>
              <span v-if="row.smsReceiverPhoneVerified">手机号已验证</span>
              <span v-if="row.smsReceiverPhoneNumber">手机号 {{ row.smsReceiverPhoneNumber }}</span>
              <span v-if="row.smsReceiverCredentialReady">OAuth 凭证已归档</span>
              <span v-if="row.smsReceiverError" class="error-text">{{ row.smsReceiverError }}</span>
              <span v-if="row.mailConfirmationAttempt">已检查 {{ row.mailConfirmationAttempt }} 次</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="导出" min-width="110">
          <template #default="{ row }">
            <div class="detail-cell">
              <el-tag :type="row.exportCount ? 'success' : 'info'" effect="plain">{{ row.exportCount ? '已导出' : '未导出' }}</el-tag>
              <span v-if="row.exportCount">累计 {{ row.exportCount }} 次</span>
              <span v-if="row.lastExportedAt">最近 {{ formatDate(row.lastExportedAt) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="170" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-tooltip content="复制邮箱与接码 URL">
                <el-button
                  text
                  type="primary"
                  :icon="CopyDocument"
                  :disabled="!row.emailAccessUrl"
                  aria-label="复制邮箱与接码 URL"
                  @click="copyRecord(row)"
                />
              </el-tooltip>
              <el-tooltip content="查看邮箱">
                <el-button text type="success" :icon="Message" :disabled="!row.emailAccessUrl" aria-label="查看邮箱" @click="viewMailbox(row)" />
              </el-tooltip>
              <el-tooltip content="重新检查到账">
                <el-button text type="warning" :icon="Refresh" :disabled="!row.emailAccessUrl" aria-label="重新检查到账" @click="checkMail([row.id])" />
              </el-tooltip>
              <el-tooltip content="在新窗口打开接码 URL">
                <el-button
                  text
                  type="primary"
                  :icon="Link"
                  :disabled="!row.emailAccessUrl"
                  aria-label="打开接码 URL"
                  @click="openMailbox(row)"
                />
              </el-tooltip>
              <el-tooltip :content="receiverEligible(row) ? '启动 HeroSMS 接码' : '需要邮箱、密码和 2FA'">
                <el-button text type="primary" :icon="Select" :disabled="!receiverSettings.enabled || !receiverEligible(row)" aria-label="HeroSMS 接码" @click="submitToReceiver([row.id])" />
              </el-tooltip>
              <el-tooltip v-if="['failed', 'stopped'].includes(row.smsReceiverState || '')" content="加入接码重试队列">
                <el-button text type="warning" :icon="Refresh" :disabled="!receiverEligible(row) || !receiverSettings.enabled" aria-label="加入接码重试队列" @click="retryReceiver([row.id])" />
              </el-tooltip>
              <el-tooltip content="从成品管理删除">
                <el-button text type="danger" :icon="Delete" aria-label="删除成品" :loading="deleteLoading" @click="deleteOne(row)" />
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination-row">
        <span>第 {{ currentPage }} 页</span>
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="sizes, prev, pager, next"
          @change="loadData()"
        />
      </div>
    </div>

    <el-dialog v-model="receiverConfigVisible" class="receiver-config-dialog" title="HeroSMS 接码配置" width="min(920px, 94vw)" destroy-on-close>
      <el-form label-position="top" class="receiver-form">
        <div class="form-section-title">
          <strong>接码服务</strong>
          <span>本机服务默认使用 5015 端口 · 配置保存后立即影响后续任务</span>
        </div>
        <div class="receiver-switches">
          <el-switch v-model="receiverSettings.enabled" active-text="启用 HeroSMS 接码" />
          <el-switch v-model="receiverSettings.autoSubmit" :disabled="!receiverSettings.enabled" active-text="邮箱已确认后自动接码" />
        </div>
        <div class="form-section-title section-gap">
          <strong>HeroSMS</strong>
          <el-tag :type="receiverHeroSmsSettings.credentialConfigured ? 'success' : 'warning'" effect="plain">
            {{ receiverHeroSmsSettings.credentialConfigured ? '密钥已保存' : '未配置密钥' }}
          </el-tag>
        </div>
        <el-form-item label="API Key">
          <el-input v-model="receiverHeroSmsSettings.apiKey" type="password" show-password placeholder="留空保持现有密钥" autocomplete="new-password" />
        </el-form-item>
        <el-form-item label="接码国家优先队列（最多 10 个）">
          <el-select
            v-model="receiverHeroSmsSettings.countryIds"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            :loading="!receiverHeroSmsCountries.length"
            no-data-text="国家目录暂不可用"
            placeholder="搜索并选择一个或多个国家"
          >
            <el-option
              v-for="country in receiverHeroSmsCountries"
              :key="country.id"
              :label="`${country.flag || ''} ${country.name}`.trim()"
              :value="Number(country.id)"
            />
          </el-select>
        </el-form-item>
        <div v-if="receiverPriorityCountries.length" class="receiver-priority-list">
          <div class="receiver-priority-caption">
            <span>严格按从上到下顺序拿号；当前国家无号时自动尝试下一个</span>
            <strong>{{ receiverPriorityCountries.length }} / 10</strong>
          </div>
          <div
            v-for="entry in receiverPriorityCountries"
            :key="entry.id"
            class="receiver-priority-item"
          >
            <span class="priority-index">优先 {{ entry.index + 1 }}</span>
            <strong>{{ entry.country?.flag || '🌐' }} {{ entry.country?.name || `国家 ${entry.id}` }}</strong>
            <code>ID {{ entry.id }}</code>
            <div>
              <el-button text :disabled="entry.index === 0" :aria-label="`上移 ${entry.id}`" @click="moveReceiverCountry(entry.index, -1)">↑</el-button>
              <el-button text :disabled="entry.index === receiverPriorityCountries.length - 1" :aria-label="`下移 ${entry.id}`" @click="moveReceiverCountry(entry.index, 1)">↓</el-button>
              <el-button text type="danger" :disabled="receiverPriorityCountries.length === 1" :aria-label="`移除 ${entry.id}`" @click="removeReceiverCountry(entry.id)">×</el-button>
            </div>
          </div>
        </div>
        <div class="form-section-title section-gap">
          <strong>HeroSMS 高级设置</strong>
          <a href="https://hero-sms.com/cn/api" target="_blank" rel="noopener noreferrer">官方 API 文档</a>
        </div>
        <div class="form-grid three-columns">
          <el-form-item label="拿号优先级">
            <el-select v-model="receiverHeroSmsSettings.acquirePriority">
              <el-option label="国家顺序优先" value="country" />
              <el-option label="低价优先" value="price" />
              <el-option label="高价优先" value="price_high" />
            </el-select>
          </el-form-item>
          <el-form-item label="单号最高价">
            <el-input-number v-model="receiverHeroSmsSettings.maxPrice" :min="0.0001" :precision="4" :step="0.01" controls-position="right" />
          </el-form-item>
          <el-form-item label="最低价格（可选）">
            <el-input-number v-model="receiverHeroSmsSettings.minPrice" :min="0" :precision="4" :step="0.01" controls-position="right" placeholder="不限制" />
          </el-form-item>
          <el-form-item label="指定价格（可选）">
            <el-input-number v-model="receiverHeroSmsSettings.preferredPrice" :min="0" :precision="4" :step="0.01" controls-position="right" placeholder="自动选择" />
          </el-form-item>
          <el-form-item label="最多换号次数">
            <el-input-number v-model="receiverHeroSmsSettings.maxRetries" :min="1" :max="20" controls-position="right" />
          </el-form-item>
          <el-form-item label="单号等待秒数">
            <el-input-number v-model="receiverHeroSmsSettings.codeWaitSeconds" :min="30" :max="600" :step="10" controls-position="right" />
          </el-form-item>
        </div>
        <el-switch class="advanced-switch" v-model="receiverHeroSmsSettings.reuseEnabled" active-text="优先复用已有可用号码" />
        <div class="form-section-title section-gap">
          <strong>自动任务调度</strong>
          <span>参考接码机流水线；仅对后续提交生效</span>
        </div>
        <div class="form-grid three-columns">
          <el-form-item label="同时提交数量">
            <el-input-number v-model="receiverSettings.concurrency" :min="1" :max="10" controls-position="right" />
            <small>建议 1–3，过高容易触发限流</small>
          </el-form-item>
          <el-form-item label="接口失败重试">
            <el-input-number v-model="receiverSettings.failureRetries" :min="0" :max="3" controls-position="right" />
            <small>仅重试 408/409/425/429、5xx 和网络错误</small>
          </el-form-item>
          <el-form-item label="重试间隔秒数">
            <el-input-number v-model="receiverSettings.retryBackoffSeconds" :min="5" :max="600" :step="5" controls-position="right" />
            <small>后续每轮按倍数退避</small>
          </el-form-item>
        </div>
        <div class="form-section-title section-gap">
          <strong>邮箱验证码</strong>
          <span>等待、轮询与重新触发参数</span>
        </div>
        <div class="form-grid three-columns">
          <el-form-item label="单轮等待秒数">
            <el-input-number v-model="receiverHeroSmsSettings.emailOtpWaitSeconds" :min="30" :max="300" :step="10" controls-position="right" />
          </el-form-item>
          <el-form-item label="取码轮询间隔">
            <el-input-number v-model="receiverHeroSmsSettings.emailOtpPollIntervalSeconds" :min="1" :max="30" controls-position="right" />
          </el-form-item>
          <el-form-item label="失败重试轮数">
            <el-input-number v-model="receiverHeroSmsSettings.emailOtpAttempts" :min="1" :max="5" controls-position="right" />
          </el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button :loading="receiverTesting" @click="testReceiver">测试连接</el-button>
        <el-button type="primary" :loading="receiverSaving" @click="saveReceiverSettings()">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="mailboxDialogVisible" class="mailbox-dialog" width="min(860px, 92vw)" destroy-on-close>
      <template #header>
        <div class="mailbox-dialog-heading">
          <strong>查看邮箱</strong>
          <span>{{ mailboxEmail }}</span>
        </div>
      </template>
      <iframe
        v-if="mailboxFrameUrl"
        :key="mailboxFrameKey"
        class="mailbox-frame"
        :src="mailboxFrameUrl"
        :title="`${mailboxEmail} 的最新邮件`"
      />
      <template #footer>
        <el-button :icon="Refresh" @click="refreshMailbox">刷新邮件</el-button>
        <el-button type="primary" plain :icon="Link" @click="openCurrentMailbox">新窗口打开</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.paid-page{min-width:0}.paid-heading{align-items:center}.heading-actions,.row-actions,.selection-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.paid-stats{margin-bottom:18px}.receiver-panel{margin-bottom:18px;padding:16px}.receiver-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:14px}.receiver-heading>div:first-child{display:flex;flex-direction:column;gap:5px}.receiver-heading strong{font-size:13px}.receiver-heading span,.receiver-note{color:var(--text-muted);font-size:10px}.receiver-switches,.receiver-config-actions{display:flex;align-items:center;gap:14px}.receiver-config-grid{display:grid;grid-template-columns:minmax(280px,1fr) minmax(280px,1fr) auto;gap:10px}.receiver-note{margin:10px 0 0}.visual-grid{display:grid;grid-template-columns:minmax(0,2.2fr) minmax(260px,.8fr);gap:14px;margin-bottom:18px}.trend-panel,.quality-panel{min-height:286px;padding:18px}.visual-header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.visual-header h3{margin:0 0 4px;font-size:14px}.visual-header span,.toolbar-copy span{color:var(--text-muted);font-size:11px}.bar-chart{display:flex;height:210px;align-items:stretch;gap:8px;padding-top:22px;overflow-x:auto}.bar-column{display:grid;min-width:34px;flex:1;grid-template-rows:18px 1fr 22px;align-items:end;text-align:center}.bar-value{color:var(--text-secondary);font-size:10px}.bar-track{display:flex;width:100%;height:142px;align-items:flex-end;justify-content:center;border-bottom:1px solid var(--border-strong)}.bar-track i{display:block;width:min(22px,70%);min-height:4px;border-radius:3px 3px 0 0;background:var(--success);box-shadow:0 0 14px rgb(69 214 138 / 18%);transition:height 180ms ease}.bar-track i.empty{background:var(--border-strong);box-shadow:none}.bar-date{padding-top:7px;color:var(--text-muted);font-size:9px}.quality-panel{display:flex;flex-direction:column;align-items:center;justify-content:space-between}.quality-panel .visual-header{width:100%}.quality-legend{width:100%;border-top:1px solid var(--border-subtle)}.quality-legend div{display:flex;align-items:center;justify-content:space-between;padding:9px 2px;border-bottom:1px solid var(--border-subtle);font-size:11px}.quality-legend span{display:flex;align-items:center;gap:7px;color:var(--text-secondary)}.quality-legend i{width:7px;height:7px;border-radius:50%}.success-dot{background:var(--success)}.failed-dot{background:var(--danger)}.paid-table-panel{overflow:hidden}.table-toolbar{display:flex;align-items:center;gap:10px;padding:14px 16px}.toolbar-copy{display:flex;align-items:baseline;gap:10px;margin-right:auto}.toolbar-copy strong{font-size:13px}.search-input{width:230px}.export-filter,.settlement-filter{width:150px}.selection-toolbar{min-height:46px;padding:8px 16px;border-top:1px solid var(--border-subtle);border-bottom:1px solid var(--border-subtle);background:var(--surface-raised)}.selection-toolbar strong{font-size:11px}.selection-toolbar span{color:var(--text-muted);font-size:10px}.account-cell,.detail-cell{display:flex;min-width:0;flex-direction:column;gap:4px}.account-cell strong,.truncate{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.account-cell strong{font-size:12px}.account-cell span,.detail-cell span{color:var(--text-muted);font-size:10px}.detail-cell strong{font-size:11px}.error-text{color:var(--danger)!important}.row-actions{flex-wrap:nowrap}.pagination-row{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;color:var(--text-muted);font-size:10px;border-top:1px solid var(--border-subtle)}.mailbox-dialog-heading{display:flex;min-width:0;flex-direction:column;gap:4px}.mailbox-dialog-heading strong{font-size:16px}.mailbox-dialog-heading span{overflow:hidden;color:var(--text-muted);font-size:11px;text-overflow:ellipsis;white-space:nowrap}.mailbox-frame{width:100%;height:min(60vh,520px);border:1px solid var(--border-subtle);border-radius:6px;background:#fff}@media(max-width:1080px){.receiver-config-grid{grid-template-columns:1fr}.receiver-config-actions{justify-content:flex-end}.visual-grid{grid-template-columns:1fr}.quality-panel{min-height:250px}}@media(max-width:900px){.paid-heading,.receiver-heading{align-items:flex-start;flex-direction:column}.receiver-switches{align-items:flex-start;flex-direction:column}.heading-actions{width:100%}.table-toolbar{align-items:stretch;flex-direction:column}.toolbar-copy{margin-right:0}.search-input,.export-filter,.settlement-filter{width:100%}.pagination-row{align-items:flex-start;flex-direction:column;gap:10px}.bar-column{min-width:42px}}
.operation-panel{display:flex;align-items:center;gap:16px;margin-bottom:18px;padding:12px 16px}.operation-group{display:flex;min-width:0;align-items:center;gap:8px;flex-wrap:wrap}.receiver-tools{flex:1}.export-tools{justify-content:flex-end}.tool-title{display:flex;min-width:0;align-items:center;gap:8px;margin-right:4px}.tool-title strong{font-size:12px;white-space:nowrap}.tool-title span{color:var(--text-muted);font-size:10px;white-space:nowrap}.operation-divider{width:1px;align-self:stretch;background:var(--border-subtle)}.account-meta,.status-tags{display:flex;align-items:center;gap:6px}.credential-cell{display:flex;min-width:0;flex-direction:column;gap:7px}.credential-cell>div{display:grid;min-width:0;grid-template-columns:34px minmax(0,1fr);align-items:center;gap:5px}.credential-cell>div>span{color:var(--text-muted);font-size:9px}.receiver-form{padding:0 2px}.form-section-title{display:flex;align-items:center;justify-content:space-between;gap:12px;padding-bottom:9px;border-bottom:1px solid var(--border-subtle)}.form-section-title strong{font-size:13px}.form-section-title span{color:var(--text-muted);font-size:10px}.section-gap{margin-top:4px}.form-grid{display:grid;gap:12px}.two-columns{grid-template-columns:1fr 1fr}.three-columns{grid-template-columns:repeat(3,1fr)}.receiver-form .receiver-switches{margin:12px 0 14px}.receiver-form :deep(.el-select),.receiver-form :deep(.el-input-number){width:100%}@media(max-width:1500px){.operation-panel{align-items:stretch;flex-direction:column}.operation-divider{width:auto;height:1px}.export-tools{justify-content:flex-start}}@media(max-width:720px){.two-columns,.three-columns{grid-template-columns:1fr}.tool-title{width:100%}.operation-group :deep(.el-segmented){max-width:100%;overflow-x:auto}}
.receiver-form .section-gap{margin-top:20px}.form-section-title a{color:var(--accent);font-size:10px;text-decoration:none}.form-section-title a:hover{text-decoration:underline}.receiver-priority-list{display:flex;flex-direction:column;gap:8px;margin:-4px 0 18px;padding:12px;border:1px solid var(--border-subtle);border-radius:8px;background:var(--surface-raised)}.receiver-priority-caption,.receiver-priority-item{display:flex;align-items:center;gap:10px}.receiver-priority-caption{justify-content:space-between;color:var(--text-muted);font-size:10px}.receiver-priority-caption strong{color:var(--success)}.receiver-priority-item{min-height:42px;padding:6px 8px;border:1px solid var(--border-subtle);border-radius:7px;background:var(--surface)}.receiver-priority-item>strong{min-width:0;flex:1;font-size:11px}.receiver-priority-item code{color:var(--text-muted);font-size:9px}.receiver-priority-item>div{display:flex;align-items:center}.priority-index{padding:4px 7px;border-radius:5px;background:rgb(69 214 138 / 12%);color:var(--success);font-size:9px;font-weight:700}.advanced-switch{margin:2px 0 4px}.receiver-form :deep(.el-form-item){margin-bottom:12px}.receiver-form :deep(.el-form-item__content)>small{display:block;width:100%;margin-top:5px;color:var(--text-muted);font-size:9px;line-height:1.4}@media(max-width:720px){.receiver-priority-item{align-items:flex-start;flex-wrap:wrap}.receiver-priority-item>strong{min-width:180px}.receiver-priority-item>div{margin-left:auto}}
</style>
