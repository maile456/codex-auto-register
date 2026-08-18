<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  CircleCheck,
  CopyDocument,
  CreditCard,
  Delete,
  Document,
  Key,
  Operation,
  Refresh,
  Setting,
  Link,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'
import StatCard from '@/components/StatCard.vue'
import SecretCell from '@/components/SecretCell.vue'
import { dataGateway } from '@/services/dataGateway'
import { copyText } from '@/services/exporter'
import { countryLabel } from '@/services/countries'
import type { PaymentExtractorOption, PipelineItem, PipelineLogEntry, PipelineSettings, PipelineStage, ProxyCountrySummary, ProxyGroupSummary } from '@/types'

const router = useRouter()

const items = ref<PipelineItem[]>([])
const total = ref(0)
const counts = ref<Record<string, number>>({})
const loading = ref(false)
const actionLoading = ref(false)
const settingsOpen = ref(false)
const paymentOpen = ref(false)
const otpOpen = ref(false)
const logOpen = ref(false)
const logLoading = ref(false)
const currentPage = ref(1)
const pageSize = ref(20)
const stage = ref('')
const search = ref('')
const selectedIds = ref<string[]>([])
const paymentTarget = ref<PipelineItem | null>(null)
const otpTarget = ref<PipelineItem | null>(null)
const logTarget = ref<PipelineItem | null>(null)
const accountLogs = ref<PipelineLogEntry[]>([])
const paymentPhone = ref('')
const otpValue = ref('')
const proxyCountries = ref<ProxyCountrySummary[]>([])
const proxyGroups = ref<ProxyGroupSummary[]>([])
const proxyGroupsReady = ref(false)
const billingCountries = ref<PaymentExtractorOption[]>([])
let pollTimer: ReturnType<typeof setInterval> | undefined

const settings = reactive<PipelineSettings>({
  enabled: false,
  extractionConcurrency: 1,
  paymentConcurrency: 1,
  extractionFailureRetries: 0,
  paymentFailureRetries: 0,
  country: 'JP',
  checkoutProxy: '',
  updateProxy: '',
  protocolProxy: '',
  checkoutProxyCountry: '',
  updateProxyCountry: '',
  protocolProxyCountry: '',
  checkoutProxyGroup: '',
  updateProxyGroup: '',
  protocolProxyGroup: '',
  applyCheckoutUpdate: true,
  heroSmsEnabled: false,
  autoPaymentEnabled: false,
  heroSmsMaxPrice: 1,
  heroSmsChangeNumberRetries: 2,
  heroSmsNumberWaitSeconds: 120,
  heroSmsCountryId: 182,
  agreementAutoSmsEnabled: false,
  heroSmsApiKeyConfigured: false,
})

watch(() => settings.heroSmsEnabled, (enabled) => {
  if (!enabled) settings.autoPaymentEnabled = false
})

const pipelineTotal = computed(() => Object.values(counts.value).reduce((sum, value) => sum + value, 0))
const pipelineCountryName = computed(() => countryLabel(settings.country))
const extractedTotal = computed(() =>
  (counts.value.payment_ready || 0)
  + (counts.value.paying || 0)
  + (counts.value.payment_waiting_otp || 0)
  + (counts.value.payment_waiting_manual || 0)
  + (counts.value.payment_failed || 0)
  + (counts.value.paid || 0),
)
const proxyCountryOptions = computed(() =>
  proxyCountries.value
    .filter((item) => item.country !== 'ZZ' && item.enabled > 0)
    .map((item) => ({
      value: item.country,
      label: `${countryLabel(item.country)} · ${item.enabled} 条启用代理`,
    })),
)
function proxyGroupOptions(country: string) {
  return proxyGroups.value
    .filter((item) => item.country === country && item.enabled > 0)
    .map((item) => ({
      value: item.group,
      label: `${item.group} · ${item.enabled} 条启用代理`,
    }))
}
for (const [countryKey, groupKey] of [
  ['checkoutProxyCountry', 'checkoutProxyGroup'],
  ['updateProxyCountry', 'updateProxyGroup'],
  ['protocolProxyCountry', 'protocolProxyGroup'],
] as const) {
  watch(() => settings[countryKey], (country) => {
    if (!proxyGroupsReady.value) return
    if (!proxyGroupOptions(country).some((item) => item.value === settings[groupKey])) {
      settings[groupKey] = ''
    }
  })
}

const stageOptions = [
  { value: '', label: '全部' },
  { value: 'eligible', label: '待提链' },
  { value: 'extracting', label: '提链中' },
  { value: 'payment_ready', label: '待支付' },
  { value: 'payment_waiting_otp', label: '待 PP 验证码' },
  { value: 'paid', label: '支付成功' },
  { value: 'extraction_failed', label: '提链失败' },
  { value: 'payment_failed', label: '支付失败' },
]

function formatDate(value?: string | null) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(value))
}

function formatLogDate(value?: string | null) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(new Date(value))
}

function logTagType(level: PipelineLogEntry['level']) {
  if (level === 'success') return 'success'
  if (level === 'warning') return 'warning'
  if (level === 'error') return 'danger'
  return 'info'
}

function paymentLinkCountdown(item: PipelineItem) {
  if (!item.paymentLink || !item.paymentLinkExpiresAt) return ''
  const seconds = Math.max(0, Math.ceil((new Date(item.paymentLinkExpiresAt).getTime() - Date.now()) / 1000))
  if (!seconds) return '链接已过期，请手动重新提炼'
  const minutes = Math.floor(seconds / 60)
  return `链接剩余 ${minutes}分${String(seconds % 60).padStart(2, '0')}秒`
}

function paymentLinkIsExpired(item: PipelineItem) {
  if (item.paymentLinkExpired) return true
  if (!item.paymentLinkExpiresAt) return false
  const expiresAt = Date.parse(item.paymentLinkExpiresAt)
  return Number.isFinite(expiresAt) && expiresAt <= Date.now()
}

function canManualReextract(item: PipelineItem) {
  const stalePaymentStage = ['paying', 'payment_waiting_otp', 'payment_waiting_manual'].includes(item.stage)
  return item.stage === 'payment_failed'
    || ((item.stage === 'payment_ready' || stalePaymentStage) && paymentLinkIsExpired(item))
}

async function openLogs(item: PipelineItem) {
  logTarget.value = item
  accountLogs.value = []
  logOpen.value = true
  logLoading.value = true
  try {
    const response = await dataGateway.pipelineLogs(item.id)
    accountLogs.value = response.logs
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '账号日志读取失败')
  } finally {
    logLoading.value = false
  }
}

function stageLabel(value: string) {
  const labels: Record<string, string> = {
    eligible: '待提链', extracting: '提链中', extraction_failed: '提链失败',
    payment_ready: '待支付', paying: '支付中', payment_waiting_otp: '等待 PP 验证码',
    payment_waiting_manual: '等待人工验证', payment_failed: '支付失败', paid: '支付成功',
  }
  return labels[value] || value
}

function stageType(value: string) {
  if (value === 'paid' || value === 'payment_ready') return 'success'
  if (value.includes('failed')) return 'danger'
  if (value === 'eligible') return 'info'
  return 'warning'
}

function checkoutTypeLabel(item: PipelineItem) {
  if (item.checkoutType === 'oaics') return 'OAICS'
  if (item.checkoutType === 'cs') return 'CS'
  return '待判断'
}

async function loadSettings() {
  Object.assign(settings, await dataGateway.pipelineSettings())
}

async function loadBillingCountries() {
  const defaults = await dataGateway.paymentExtractorDefaults()
  billingCountries.value = defaults.countries || []
}

async function loadItems(quiet = false) {
  if (!quiet) loading.value = true
  try {
    const result = await dataGateway.listPipeline({
      page: currentPage.value,
      pageSize: pageSize.value,
      stage: stage.value,
      q: search.value,
    })
    items.value = result.items
    total.value = result.total
    counts.value = result.counts
  } catch (error) {
    if (!quiet) ElMessage.error(error instanceof Error ? error.message : '流水线读取失败')
  } finally {
    if (!quiet) loading.value = false
  }
}

async function syncEligible() {
  actionLoading.value = true
  try {
    const result = await dataGateway.syncPipeline()
    ElMessage.success(`符合资格 ${result.eligible} 个，新纳入 ${result.inserted} 个`)
    await loadItems(true)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '同步失败')
  } finally {
    actionLoading.value = false
  }
}

async function saveSettings() {
  actionLoading.value = true
  try {
    Object.assign(settings, await dataGateway.updatePipelineSettings({
      enabled: settings.enabled,
      extractionConcurrency: settings.extractionConcurrency,
      paymentConcurrency: settings.paymentConcurrency,
      extractionFailureRetries: settings.extractionFailureRetries,
      paymentFailureRetries: settings.paymentFailureRetries,
      country: settings.country,
      checkoutProxy: settings.checkoutProxy,
      updateProxy: settings.updateProxy,
      protocolProxy: settings.protocolProxy,
      checkoutProxyCountry: settings.checkoutProxyCountry,
      updateProxyCountry: settings.updateProxyCountry,
      protocolProxyCountry: settings.protocolProxyCountry,
      checkoutProxyGroup: settings.checkoutProxyGroup,
      updateProxyGroup: settings.updateProxyGroup,
      protocolProxyGroup: settings.protocolProxyGroup,
      applyCheckoutUpdate: settings.applyCheckoutUpdate,
      autoPaymentEnabled: settings.autoPaymentEnabled,
    }))
    settingsOpen.value = false
    ElMessage.success(settings.enabled ? '自动流水线已启用' : '自动流水线已暂停')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '配置保存失败')
  } finally {
    actionLoading.value = false
  }
}

async function extractSelected(ids = selectedIds.value) {
  if (!ids.length) return
  actionLoading.value = true
  try {
    const result = await dataGateway.extractPipeline(ids)
    const deferred = result.deferred || 0
    ElMessage.success(`已提交 ${result.started} 个提链任务${deferred ? `，${deferred} 个等待并发空位` : ''}`)
    selectedIds.value = []
    await loadItems(true)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '提链提交失败')
  } finally {
    actionLoading.value = false
  }
}

function openPayment(item: PipelineItem) {
  paymentTarget.value = item
  paymentPhone.value = ''
  paymentOpen.value = true
}

async function startPayment() {
  if (!paymentTarget.value) return
  actionLoading.value = true
  try {
    await dataGateway.startPipelinePayment(
      paymentTarget.value.id,
      settings.heroSmsEnabled ? '' : paymentPhone.value,
      settings.protocolProxyCountry ? '' : settings.protocolProxy,
    )
    paymentOpen.value = false
    ElMessage.success(`${pipelineCountryName.value}协议支付任务已启动`)
    await loadItems(true)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '支付任务启动失败')
  } finally {
    actionLoading.value = false
  }
}

function openOtp(item: PipelineItem) {
  otpTarget.value = item
  otpValue.value = ''
  otpOpen.value = true
}

async function submitOtp() {
  if (!otpTarget.value || !otpValue.value.trim()) return
  actionLoading.value = true
  try {
    await dataGateway.submitPipelineOtp(otpTarget.value.id, otpValue.value)
    otpOpen.value = false
    ElMessage.success('验证码已提交')
    await loadItems(true)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '验证码提交失败')
  } finally {
    actionLoading.value = false
  }
}

async function retry(item: PipelineItem, target: 'extraction' | 'payment') {
  actionLoading.value = true
  try {
    await dataGateway.retryPipeline(item.id, target)
    if (target === 'extraction') await dataGateway.extractPipeline([item.id])
    ElMessage.success(target === 'extraction' ? '已重新提交提链' : '已恢复到待支付')
    await loadItems(true)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '重试失败')
  } finally {
    actionLoading.value = false
  }
}

async function remove(item: PipelineItem) {
  try {
    await ElMessageBox.confirm(`从流水线移除 ${item.email}？账号池记录不会删除。`, '移除记录', {
      type: 'warning', confirmButtonText: '移除', cancelButtonText: '取消',
    })
  } catch { return }
  await dataGateway.deletePipeline(item.id)
  ElMessage.success('已从流水线移除')
  await loadItems(true)
}

function handleSelection(rows: PipelineItem[]) {
  selectedIds.value = rows.map((item) => item.id)
}

function canExtract(item: PipelineItem) {
  return item.stage === 'eligible' && item.extractionStatus === 'pending'
}

function openMailbox(item: PipelineItem) {
  if (!item.emailAccessUrl) return
  window.open(item.emailAccessUrl, '_blank', 'noopener,noreferrer')
}

function paymentLinkHref(item: PipelineItem) {
  if (!item.paymentLink) return ''
  try {
    const parsed = new URL(item.paymentLink)
    return ['http:', 'https:'].includes(parsed.protocol) ? parsed.toString() : ''
  } catch {
    return ''
  }
}

async function copyPaymentLink(item: PipelineItem) {
  if (!item.paymentLink) return
  try {
    await copyText(item.paymentLink)
    ElMessage.success('提炼链接已复制')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '复制失败')
  }
}

function changeStage(value: string | number | boolean | undefined) {
  stage.value = String(value || '')
  currentPage.value = 1
  void loadItems()
}

onMounted(async () => {
  await Promise.all([
    loadSettings(),
    loadBillingCountries(),
    loadItems(),
    dataGateway.listProxyCountries().then((items) => { proxyCountries.value = items }),
    dataGateway.listProxyGroups().then((items) => {
      proxyGroups.value = items
      proxyGroupsReady.value = true
    }),
  ])
  pollTimer = setInterval(() => void loadItems(true), 2500)
})

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <section class="pipeline-page">
    <div class="page-heading pipeline-heading">
      <div>
        <h2>{{ pipelineCountryName }} Plus 流水线</h2>
        <p>只处理已确认 Plus 试用资格且 AT 有效的账号。</p>
      </div>
      <div class="heading-actions">
        <el-tag :type="settings.enabled ? 'success' : 'info'" effect="plain">
          {{ settings.enabled ? '自动流水线运行中' : '自动流水线已暂停' }}
        </el-tag>
        <el-button :icon="Refresh" :loading="actionLoading" @click="syncEligible">同步合资格账号</el-button>
        <el-button type="primary" plain :icon="Setting" @click="settingsOpen = true">流水线配置</el-button>
      </div>
    </div>

    <div class="stats-grid pipeline-stats">
      <StatCard label="合资格队列" :value="pipelineTotal" note="资格通过且 AT 有效" :icon="Operation" />
      <StatCard label="提链成功" :value="extractedTotal" note="已获得 PayPal BA 链" :icon="Key" tone="green" />
      <StatCard label="等待 PP 验证" :value="counts.payment_waiting_otp || 0" note="HeroSMS 自动轮询" :icon="CreditCard" tone="amber" />
      <StatCard label="支付成功" :value="counts.paid || 0" note="协议支付已完成" :icon="CircleCheck" tone="green" />
    </div>

    <div class="pipeline-filter-band">
      <el-segmented
        :model-value="stage"
        :options="stageOptions"
        @change="changeStage"
      />
      <div class="filter-actions">
        <el-input
          v-model="search"
          clearable
          placeholder="搜索账号邮箱"
          @keyup.enter="loadItems()"
          @clear="loadItems()"
        />
        <el-button
          type="success"
          plain
          :icon="Key"
          :disabled="selectedIds.length === 0"
          :loading="actionLoading"
          @click="extractSelected()"
        >批量提链 {{ selectedIds.length || '' }}</el-button>
      </div>
    </div>

    <div class="panel pipeline-table-panel">
      <el-table
        v-loading="loading"
        :data="items"
        row-key="id"
        @selection-change="handleSelection"
      >
        <el-table-column type="selection" width="44" :selectable="canExtract" />
        <el-table-column label="账号" min-width="220">
          <template #default="{ row }">
            <div class="account-cell">
              <div class="account-title-row">
                <strong>{{ row.email }}</strong>
                <el-button
                  text
                  type="primary"
                  size="small"
                  :icon="Document"
                  aria-label="查看账号日志"
                  @click.stop="openLogs(row)"
                >日志</el-button>
              </div>
              <span>注册 {{ formatDate(row.accountCreatedAt) }} · AT {{ formatDate(row.accessTokenExpiresAt) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="CHATGPT 密码" min-width="190">
          <template #default="{ row }"><SecretCell :value="row.chatgptPassword" /></template>
        </el-table-column>
        <el-table-column label="TOTP 密钥" min-width="185">
          <template #default="{ row }"><SecretCell :value="row.totpSecret" /></template>
        </el-table-column>
        <el-table-column label="资格" width="130">
          <template #default="{ row }">
            <el-tag type="success" effect="plain">Plus 试用可用</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="结账类型" width="105">
          <template #default="{ row }">
            <el-tag
              :type="row.checkoutType === 'oaics' ? 'success' : row.checkoutType === 'cs' ? 'warning' : 'info'"
              effect="plain"
            >{{ checkoutTypeLabel(row) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="流水线阶段" width="150">
          <template #default="{ row }">
            <el-tag :type="stageType(row.stage)" effect="dark">{{ stageLabel(row.stage) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="提链" min-width="290">
          <template #default="{ row }">
            <div class="status-cell">
              <b>{{ row.extractionStatus }}</b>
              <small v-if="row.extractionRetryCount">已自动重试 {{ row.extractionRetryCount }} 次</small>
              <small v-if="row.extractedAt">{{ formatDate(row.extractedAt) }}</small>
              <small v-else-if="row.extractionError" class="error-text">{{ row.extractionError.code }}</small>
              <div v-if="paymentLinkHref(row)" class="payment-link-row">
                <el-tooltip :content="row.paymentLink" placement="top">
                  <a
                    class="payment-link-text"
                    :href="paymentLinkHref(row)"
                    target="_blank"
                    rel="noopener noreferrer"
                  >{{ row.paymentLink }}</a>
                </el-tooltip>
                <el-tooltip content="复制提炼链接">
                  <el-button
                    text
                    type="primary"
                    :icon="CopyDocument"
                    aria-label="复制提炼链接"
                    @click="copyPaymentLink(row)"
                  />
                </el-tooltip>
              </div>
              <small
                v-if="paymentLinkCountdown(row)"
                :class="{ 'error-text': row.paymentLinkExpired || paymentLinkCountdown(row).includes('已过期') }"
              >{{ paymentLinkCountdown(row) }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="支付" min-width="170">
          <template #default="{ row }">
            <div class="status-cell">
              <b>{{ row.paymentStatus }}</b>
              <small v-if="row.paymentRetryCount">已自动重试 {{ row.paymentRetryCount }} 次</small>
              <small v-if="row.paidAt">成功 {{ formatDate(row.paidAt) }}</small>
              <small v-else-if="row.paymentPhonePreview">PP {{ row.paymentPhonePreview }}</small>
              <small v-if="row.heroSmsManaged">HeroSMS 第 {{ row.heroSmsAttempt }} 个号 · {{ row.heroSmsStatus }}</small>
              <small v-if="row.heroSmsPrice != null">价格 {{ row.heroSmsPrice }}</small>
              <small v-if="row.paymentError" class="error-text">{{ row.paymentError.code }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="340" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button
                v-if="row.stage === 'eligible'"
                text type="success" :icon="Key"
                @click="extractSelected([row.id])"
              >提链</el-button>
              <el-button
                v-if="row.stage === 'payment_ready'"
                text type="primary" :icon="CreditCard"
                @click="openPayment(row)"
              >支付</el-button>
              <el-button
                v-if="row.stage === 'payment_waiting_otp'"
                text type="warning" :icon="Key"
                @click="openOtp(row)"
              >验证码</el-button>
              <el-button
                v-if="row.stage === 'extraction_failed'"
                text type="warning" @click="retry(row, 'extraction')"
              >重试提链</el-button>
              <el-button
                v-if="row.stage === 'payment_failed'"
                text type="warning" @click="retry(row, 'payment')"
              >重试支付</el-button>
              <el-button
                v-if="canManualReextract(row)"
                text type="primary" :icon="Key"
                @click="retry(row, 'extraction')"
              >重新提炼</el-button>
              <el-tooltip content="打开邮箱入口">
                <el-button
                  text
                  type="primary"
                  :icon="Link"
                  :disabled="!row.emailAccessUrl"
                  aria-label="打开邮箱入口"
                  @click="openMailbox(row)"
                />
              </el-tooltip>
              <el-button text type="danger" :icon="Delete" @click="remove(row)" />
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination-row">
        <span>共 {{ total }} 条</span>
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="sizes, prev, pager, next"
          @change="loadItems()"
        />
      </div>
    </div>

    <el-drawer
      v-model="logOpen"
      :title="`账号日志 · ${logTarget?.email || ''}`"
      size="620px"
    >
      <div v-loading="logLoading" class="account-log-panel">
        <el-alert
          type="info"
          :closable="false"
          title="日志只记录脱敏后的阶段、错误和重试信息，不保存密码、令牌、代理凭据或验证码。"
        />
        <el-empty v-if="!logLoading && accountLogs.length === 0" description="暂无流水线日志" />
        <el-timeline v-else class="account-log-timeline">
          <el-timeline-item
            v-for="entry in accountLogs"
            :key="entry.id"
            :timestamp="formatLogDate(entry.timestamp)"
            placement="top"
            :type="logTagType(entry.level)"
          >
            <div class="account-log-entry">
              <div class="account-log-heading">
                <strong>{{ entry.message }}</strong>
                <el-tag :type="logTagType(entry.level)" effect="plain" size="small">{{ entry.event }}</el-tag>
              </div>
              <code v-if="entry.code">{{ entry.code }}</code>
              <div v-if="Object.keys(entry.details || {}).length" class="account-log-details">
                <span v-for="(value, key) in entry.details" :key="key">{{ key }}：{{ value }}</span>
              </div>
            </div>
          </el-timeline-item>
        </el-timeline>
      </div>
    </el-drawer>

    <el-drawer v-model="settingsOpen" title="流水线配置" size="520px">
      <el-form label-position="top">
        <el-form-item label="自动流水线总开关">
          <el-switch v-model="settings.enabled" active-text="运行" inactive-text="暂停" />
        </el-form-item>
        <div class="pipeline-concurrency-grid">
          <el-form-item label="自动提链并发">
            <el-input-number v-model="settings.extractionConcurrency" :min="1" :max="10" :step="1" controls-position="right" />
          </el-form-item>
          <el-form-item label="自动支付并发">
            <el-input-number v-model="settings.paymentConcurrency" :min="1" :max="5" :step="1" controls-position="right" />
          </el-form-item>
        </div>
        <div class="pipeline-concurrency-grid">
          <el-form-item label="提链失败自动重试（0 = 仅手动）">
            <el-input-number v-model="settings.extractionFailureRetries" :min="0" :max="10" :step="1" controls-position="right" />
          </el-form-item>
          <el-form-item label="支付失败自动重试（0 = 仅手动）">
            <el-input-number v-model="settings.paymentFailureRetries" :min="0" :max="10" :step="1" controls-position="right" />
          </el-form-item>
        </div>
        <el-form-item label="账单国家 / 支付方式">
          <el-select v-model="settings.country" filterable placeholder="选择账单国家" style="width:100%">
            <el-option
              v-for="option in billingCountries"
              :key="`billing-${option.value}`"
              :label="`${option.label} · PayPal`"
              :value="option.value"
            />
          </el-select>
          <small class="legacy-proxy-note">账单国家与下面的代理出口国家互相独立。</small>
        </el-form-item>
        <el-form-item label="Checkout 代理池">
          <el-select v-model="settings.checkoutProxyCountry" clearable filterable placeholder="选择配置栏中的国家代理池" style="width:100%">
            <el-option v-for="option in proxyCountryOptions" :key="`checkout-${option.value}`" :label="option.label" :value="option.value" />
          </el-select>
          <small v-if="!settings.checkoutProxyCountry && settings.checkoutProxy" class="legacy-proxy-note">当前继续使用原独立 Checkout 代理池；选择国家后改用配置栏代理。</small>
        </el-form-item>
        <el-form-item label="Checkout 代理分组">
          <el-select v-model="settings.checkoutProxyGroup" clearable filterable placeholder="全部分组" style="width:100%" :disabled="!settings.checkoutProxyCountry">
            <el-option v-for="option in proxyGroupOptions(settings.checkoutProxyCountry)" :key="`checkout-group-${option.value}`" :label="option.label" :value="option.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="Update 代理池">
          <el-select v-model="settings.updateProxyCountry" clearable filterable placeholder="选择配置栏中的国家代理池" style="width:100%">
            <el-option v-for="option in proxyCountryOptions" :key="`update-${option.value}`" :label="option.label" :value="option.value" />
          </el-select>
          <small v-if="!settings.updateProxyCountry && settings.updateProxy" class="legacy-proxy-note">当前继续使用原独立 Update 代理池；选择国家后改用配置栏代理。</small>
        </el-form-item>
        <el-form-item label="Update 代理分组">
          <el-select v-model="settings.updateProxyGroup" clearable filterable placeholder="全部分组" style="width:100%" :disabled="!settings.updateProxyCountry">
            <el-option v-for="option in proxyGroupOptions(settings.updateProxyCountry)" :key="`update-group-${option.value}`" :label="option.label" :value="option.value" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="settings.applyCheckoutUpdate">执行 Checkout Update</el-checkbox>
        </el-form-item>
        <el-form-item label="协议支付代理池">
          <el-select v-model="settings.protocolProxyCountry" clearable filterable placeholder="选择配置栏中的国家代理池" style="width:100%">
            <el-option v-for="option in proxyCountryOptions" :key="`protocol-${option.value}`" :label="option.label" :value="option.value" />
          </el-select>
          <small v-if="!settings.protocolProxyCountry && settings.protocolProxy" class="legacy-proxy-note">当前继续使用原独立协议代理池；选择国家后改用配置栏代理。</small>
        </el-form-item>
        <el-form-item label="协议支付代理分组">
          <el-select v-model="settings.protocolProxyGroup" clearable filterable placeholder="全部分组" style="width:100%" :disabled="!settings.protocolProxyCountry">
            <el-option v-for="option in proxyGroupOptions(settings.protocolProxyCountry)" :key="`protocol-group-${option.value}`" :label="option.label" :value="option.value" />
          </el-select>
        </el-form-item>
        <el-divider content-position="left">HeroSMS 自动接码</el-divider>
        <el-form-item label="共享接码配置">
          <div class="hero-key-row">
            <el-tag :type="settings.heroSmsApiKeyConfigured ? 'success' : 'danger'" effect="plain">
              {{ settings.heroSmsApiKeyConfigured ? '后端已配置' : '未配置' }}
            </el-tag>
            <el-tag :type="settings.heroSmsEnabled ? 'success' : 'info'" effect="plain">
              {{ settings.heroSmsEnabled ? 'HeroSMS 已启用' : 'HeroSMS 已暂停' }}
            </el-tag>
            <el-button @click="router.push('/hero-sms')">打开接码配置</el-button>
          </div>
        </el-form-item>
        <el-form-item label="提链成功后自动支付">
          <el-switch v-model="settings.autoPaymentEnabled" :disabled="!settings.heroSmsEnabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="settingsOpen = false">取消</el-button>
        <el-button type="primary" :loading="actionLoading" @click="saveSettings">保存配置</el-button>
      </template>
    </el-drawer>

    <el-dialog v-model="paymentOpen" :title="`启动${pipelineCountryName}协议支付`" width="480px">
      <el-form label-position="top">
        <el-form-item label="账号"><el-input :model-value="paymentTarget?.email" disabled /></el-form-item>
        <el-alert
          v-if="settings.heroSmsEnabled"
          type="success"
          :closable="false"
          :title="`将从 HeroSMS 自动购买 PayPal 号码并完成${pipelineCountryName}账单支付验证。`"
        />
        <el-form-item v-else label="PP 手机号">
          <el-input v-model="paymentPhone" placeholder="输入含国家码的手机号" inputmode="tel" />
        </el-form-item>
        <el-alert type="info" :closable="false" title="支付代理使用流水线配置；自动接码失败时仍可手工重试。" />
      </el-form>
      <template #footer>
        <el-button @click="paymentOpen = false">取消</el-button>
        <el-button type="primary" :loading="actionLoading" @click="startPayment">启动支付</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="otpOpen" title="提交 PP 验证码" width="420px">
      <el-input v-model="otpValue" maxlength="32" placeholder="6 位验证码" inputmode="numeric" @keyup.enter="submitOtp" />
      <template #footer>
        <el-button @click="otpOpen = false">取消</el-button>
        <el-button type="primary" :loading="actionLoading" @click="submitOtp">提交验证码</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.pipeline-page{min-width:0}.pipeline-heading{align-items:center}.heading-actions,.filter-actions,.row-actions,.hero-key-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.pipeline-stats{margin-bottom:14px}.pipeline-filter-band{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:12px}.filter-actions .el-input{width:220px}.pipeline-table-panel{overflow:hidden}.account-cell,.status-cell{display:flex;flex-direction:column;gap:4px;min-width:0}.account-cell strong{overflow:hidden;text-overflow:ellipsis;color:var(--text-primary);font-size:12px}.account-cell span,.status-cell small,.hero-key-row span,.legacy-proxy-note{color:var(--text-muted);font-size:10px}.legacy-proxy-note{display:block;margin-top:6px}.status-cell b{font-size:11px}.status-cell .error-text{color:var(--danger)}.payment-link-row{display:flex;align-items:center;gap:4px;min-width:0}.payment-link-text{display:block;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--accent);font-size:10px;text-decoration:none}.payment-link-text:hover{text-decoration:underline}.payment-link-row .el-button{flex:0 0 24px;width:24px;height:24px;padding:0}.row-actions{flex-wrap:nowrap}.pagination-row{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;color:var(--text-muted);font-size:10px;border-top:1px solid var(--border-subtle)}.pipeline-concurrency-grid,.hero-number-grid{display:grid;gap:12px}.pipeline-concurrency-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.hero-number-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.pipeline-concurrency-grid .el-input-number,.hero-number-grid .el-input-number{width:100%}@media(max-width:900px){.pipeline-heading{align-items:flex-start}.pipeline-filter-band{align-items:stretch;flex-direction:column}.filter-actions .el-input{width:100%}.pagination-row{align-items:flex-start;flex-direction:column;gap:10px}.pipeline-concurrency-grid,.hero-number-grid{grid-template-columns:1fr}}
.account-title-row,.account-log-heading{display:flex;align-items:center;justify-content:space-between;gap:8px;min-width:0}.account-title-row strong{flex:1;min-width:0}.account-title-row .el-button{flex:0 0 auto;padding:2px 4px;font-size:10px}.account-log-panel{min-height:180px}.account-log-timeline{margin-top:22px;padding-left:4px}.account-log-entry{display:flex;flex-direction:column;gap:7px;padding:10px 12px;border:1px solid var(--border-subtle);border-radius:8px;background:var(--surface-raised)}.account-log-heading{align-items:flex-start}.account-log-heading strong{font-size:12px;line-height:1.55}.account-log-entry code{color:var(--danger);font-size:10px}.account-log-details{display:flex;gap:6px;flex-wrap:wrap}.account-log-details span{padding:2px 6px;border-radius:4px;background:var(--surface-soft);color:var(--text-muted);font-size:10px}
</style>
