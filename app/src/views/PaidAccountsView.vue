<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  CircleCheck,
  CopyDocument,
  CreditCard,
  Download,
  Link,
  Message,
  Refresh,
  Select,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import SecretCell from '@/components/SecretCell.vue'
import StatCard from '@/components/StatCard.vue'
import { dataGateway } from '@/services/dataGateway'
import { copyText, downloadTextFile } from '@/services/exporter'
import type { PipelineItem, PipelinePaidStats, SmsReceiverSettings } from '@/types'

const items = ref<PipelineItem[]>([])
const total = ref(0)
const loading = ref(false)
const exportLoading = ref(false)
const mailLoading = ref(false)
const markingLoading = ref(false)
const receiverSaving = ref(false)
const receiverTesting = ref(false)
const receiverActionLoading = ref(false)
const receiverSettings = ref<SmsReceiverSettings>({
  enabled: false,
  autoSubmit: false,
  baseUrl: '',
  mailboxPublicBaseUrl: '',
  updatedAt: null,
})
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
const selectedIds = ref<string[]>([])
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
      }),
      dataGateway.paidPipelineStats(14),
    ])
    items.value = list.items
    total.value = list.total
    stats.value = overview
    if (!quiet) selectedIds.value = []
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

function selectable(item: PipelineItem) {
  return Boolean(item.email && item.emailAccessUrl)
}

async function quickSelect(count?: number) {
  tableRef.value?.clearSelection()
  await nextTick()
  const rows = items.value.filter(selectable).slice(0, count || items.value.length)
  rows.forEach((item) => tableRef.value?.toggleRowSelection(item, true))
}

async function restoreSelection(ids: string[]) {
  await nextTick()
  const selected = new Set(ids)
  items.value.filter((item) => selected.has(item.id)).forEach((item) => {
    tableRef.value?.toggleRowSelection(item, true)
  })
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
  const labels: Record<string, string> = {
    idle: '未送出', queued: '已排队', running: '接码中', retry_wait: '等待重试',
    paused: '已暂停', completed: '归档中', ready: '凭证已就绪', failed: '送出失败', stopped: '已停止',
  }
  return labels[item.smsReceiverState || 'idle'] || item.smsReceiverState || '未送出'
}

function receiverStatusType(item: PipelineItem) {
  if (item.smsReceiverState === 'ready' && item.smsReceiverCredentialReady) return 'success'
  if (item.smsReceiverState === 'failed' || item.smsReceiverState === 'stopped') return 'danger'
  if (item.smsReceiverState && item.smsReceiverState !== 'idle') return 'warning'
  return 'info'
}

async function loadReceiverSettings() {
  try {
    receiverSettings.value = await dataGateway.smsReceiverSettings()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '接码机配置读取失败')
  }
}

async function saveReceiverSettings(showMessage = true) {
  receiverSaving.value = true
  try {
    receiverSettings.value = await dataGateway.updateSmsReceiverSettings({
      enabled: receiverSettings.value.enabled,
      autoSubmit: receiverSettings.value.autoSubmit,
      baseUrl: receiverSettings.value.baseUrl.trim(),
      mailboxPublicBaseUrl: receiverSettings.value.mailboxPublicBaseUrl.trim(),
    })
    if (showMessage) ElMessage.success('接码机服务器配置已保存')
    return true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '接码机配置保存失败')
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
    ElMessage.success(`接码机连接正常${result.service ? `：${result.service}` : ''}`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '接码机连接测试失败')
  } finally {
    receiverTesting.value = false
  }
}

async function submitToReceiver(ids = selectedIds.value) {
  if (!ids.length) return
  const previousSelection = [...selectedIds.value]
  receiverActionLoading.value = true
  try {
    const result = await dataGateway.submitPaidToSmsReceiver(ids)
    if (result.submitted) ElMessage.success(`已送出 ${result.submitted} 个成品到接码机`)
    if (result.failed) ElMessage.warning(`${result.failed} 个送出失败，请查看接码机状态列`)
    await loadData(true)
    await restoreSelection(previousSelection)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '成品送出失败')
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

async function exportRecords(delivery: 'copy' | 'download', selected: boolean) {
  exportLoading.value = true
  try {
    const result = await dataGateway.exportPaidPipeline(selected ? selectedIds.value : [], search.value, exportState.value)
    if (!result.count) {
      ElMessage.warning('没有包含接码 URL 的成品账号')
      return
    }
    if (delivery === 'copy') {
      await copyText(result.content)
      ElMessage.success(`已复制 ${result.count} 条`)
    } else {
      downloadTextFile(result.content, result.filename)
      ElMessage.success(`已生成 ${result.filename}`)
    }
    if (result.skippedMissingUrlCount) {
      ElMessage.warning(`另有 ${result.skippedMissingUrlCount} 条缺少接码 URL`)
    }
    await loadData()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '导出失败')
  } finally {
    exportLoading.value = false
  }
}

async function copyRecord(item: PipelineItem) {
  if (!item.emailAccessUrl) return
  const result = await dataGateway.exportPaidPipeline([item.id])
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
  pollTimer = setInterval(() => {
    if (!selectedIds.value.length) void loadData(true)
  }, 5000)
})

onBeforeUnmount(() => {
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
        <el-button
          :icon="CopyDocument"
          :loading="exportLoading"
          :disabled="selectedIds.length === 0"
          @click="exportRecords('copy', true)"
        >复制勾选 {{ selectedIds.length || '' }}</el-button>
        <el-button type="primary" plain :icon="CopyDocument" :loading="exportLoading" @click="exportRecords('copy', false)">
          复制全部
        </el-button>
        <el-button type="primary" :icon="Download" :loading="exportLoading" @click="exportRecords('download', false)">
          导出 TXT
        </el-button>
      </div>
    </div>

    <div class="stats-grid paid-stats">
      <StatCard label="成品总数" :value="stats.total" note="累计入库账号" :icon="CircleCheck" tone="green" />
      <StatCard label="今日入库" :value="stats.today" note="日本自然日" :icon="CreditCard" />
      <StatCard
        label="平均接码成本"
        :value="stats.averageHeroSmsPrice == null ? '—' : stats.averageHeroSmsPrice.toFixed(4)"
        note="HeroSMS 已记录订单"
        :icon="CreditCard"
        tone="amber"
      />
      <StatCard label="未导出" :value="stats.unexported" note="等待交付账号" :icon="Download" tone="amber" />
      <StatCard label="邮件已确认" :value="stats.mailConfirmed" note="已匹配到账确认邮件" :icon="Message" tone="green" />
    </div>

    <div class="panel receiver-panel">
      <div class="receiver-heading">
        <div>
          <strong>服务器接码机</strong>
          <span>先导入“邮箱----服务器接码 URL”，再提交邮箱任务；接码机只返回任务状态。</span>
        </div>
        <div class="receiver-switches">
          <el-switch v-model="receiverSettings.enabled" active-text="启用对接" />
          <el-switch v-model="receiverSettings.autoSubmit" :disabled="!receiverSettings.enabled" active-text="支付成功自动送出" />
        </div>
      </div>
      <div class="receiver-config-grid">
        <el-input v-model="receiverSettings.baseUrl" placeholder="接码机服务器 API，例如 https://sms.example.com">
          <template #prepend>接码机服务器</template>
        </el-input>
        <el-input v-model="receiverSettings.mailboxPublicBaseUrl" placeholder="邮箱服务公网地址，例如 https://mail.example.com">
          <template #prepend>服务器邮箱 URL</template>
        </el-input>
        <div class="receiver-config-actions">
          <el-button :loading="receiverSaving" @click="saveReceiverSettings()">保存配置</el-button>
          <el-button type="primary" plain :loading="receiverTesting" @click="testReceiver">测试连接</el-button>
        </div>
      </div>
      <p class="receiver-note">只有本地 URL 会替换域名和端口；api798、laimail 等现有公网接码 URL 保持原样。</p>
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
      </div>
      <div class="selection-toolbar">
        <strong>已选 {{ selectedIds.length }}</strong>
        <span>快速选择</span>
        <el-button size="small" @click="quickSelect(10)">前 10</el-button>
        <el-button size="small" @click="quickSelect(20)">前 20</el-button>
        <el-button size="small" @click="quickSelect(50)">前 50</el-button>
        <el-button size="small" @click="quickSelect()">本页</el-button>
        <el-button size="small" :disabled="!selectedIds.length" :loading="mailLoading" :icon="Refresh" @click="checkMail()">重新检查到账</el-button>
        <el-button size="small" type="primary" :disabled="!selectedIds.length || !receiverSettings.enabled" :loading="receiverActionLoading" @click="submitToReceiver()">送去接码机</el-button>
        <el-button size="small" :disabled="!selectedIds.length || !receiverSettings.enabled" :loading="receiverActionLoading" :icon="Refresh" @click="refreshReceiver()">刷新接码状态</el-button>
        <el-button size="small" :disabled="!selectedIds.length" :loading="markingLoading" :icon="Select" @click="markExported(true)">标记已导出</el-button>
        <el-button size="small" :disabled="!selectedIds.length" :loading="markingLoading" @click="markExported(false)">恢复未导出</el-button>
        <el-button size="small" :disabled="!selectedIds.length" @click="tableRef?.clearSelection()">取消选择</el-button>
      </div>
      <el-table ref="tableRef" v-loading="loading" :data="items" row-key="id" @selection-change="handleSelection">
        <el-table-column type="selection" width="44" :selectable="selectable" />
        <el-table-column label="账号邮箱" min-width="220">
          <template #default="{ row }">
            <div class="account-cell">
              <strong>{{ row.email }}</strong>
              <span>支付 {{ formatDate(row.paidAt) }}</span>
            </div>
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
        <el-table-column label="CHATGPT 密码" min-width="185">
          <template #default="{ row }"><SecretCell :value="row.chatgptPassword" /></template>
        </el-table-column>
        <el-table-column label="接码 URL" min-width="240">
          <template #default="{ row }"><SecretCell :value="row.emailAccessUrl" /></template>
        </el-table-column>
        <el-table-column label="到账状态" min-width="200">
          <template #default="{ row }">
            <div class="detail-cell">
              <el-tag :type="mailStatusType(row)" effect="plain">{{ mailStatus(row) }}</el-tag>
              <span v-if="row.mailConfirmationReceivedAt">{{ formatDate(row.mailConfirmationReceivedAt) }}</span>
              <span v-if="row.mailConfirmationOrderId" class="truncate" :title="row.mailConfirmationOrderId">订单 {{ row.mailConfirmationOrderId }}</span>
              <span v-if="row.mailConfirmationSubject" class="truncate" :title="row.mailConfirmationSubject">{{ row.mailConfirmationSubject }}</span>
              <span v-else-if="row.mailConfirmationError" class="error-text">{{ row.mailConfirmationError }}</span>
              <span v-if="row.mailConfirmationAttempt">已检查 {{ row.mailConfirmationAttempt }} 次</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="导出状态" min-width="140">
          <template #default="{ row }">
            <div class="detail-cell">
              <el-tag :type="row.exportCount ? 'success' : 'info'" effect="plain">{{ row.exportCount ? '已导出' : '未导出' }}</el-tag>
              <span v-if="row.exportCount">累计 {{ row.exportCount }} 次</span>
              <span v-if="row.lastExportedAt">最近 {{ formatDate(row.lastExportedAt) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="接码机" min-width="170">
          <template #default="{ row }">
            <div class="detail-cell">
              <el-tag :type="receiverStatusType(row)" effect="plain">{{ receiverStatusLabel(row) }}</el-tag>
              <span v-if="row.smsReceiverCredentialReady">凭证已归档</span>
              <span v-if="row.smsReceiverUpdatedAt">更新 {{ formatDate(row.smsReceiverUpdatedAt) }}</span>
              <span v-if="row.smsReceiverError" class="error-text">{{ row.smsReceiverError }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="接码记录" min-width="160">
          <template #default="{ row }">
            <div class="detail-cell">
              <strong>{{ row.paymentPhonePreview || '—' }}</strong>
              <span v-if="row.heroSmsManaged">第 {{ row.heroSmsAttempt }} 个号</span>
              <span v-if="row.heroSmsPrice != null">成本 {{ row.heroSmsPrice }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="支付结果" min-width="150">
          <template #default="{ row }">
            <div class="detail-cell">
              <el-tag type="success" effect="plain">已完成</el-tag>
              <span>{{ row.paymentSummary?.settlementStatus || row.paymentSummary?.status || 'completed' }}</span>
              <span>{{ row.paymentSummary?.billingCountry || 'JP' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="210" fixed="right">
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
              <el-tooltip content="送去服务器接码机">
                <el-button text type="primary" :icon="Select" :disabled="!receiverSettings.enabled || !row.emailAccessUrl" aria-label="送去接码机" @click="submitToReceiver([row.id])" />
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
</style>
