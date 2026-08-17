<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CopyDocument, Delete, Download, Key, Link, Lock, MagicStick, Search, UserFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox, type TableInstance } from 'element-plus'
import type { AccountRecord, ExportFormat, ExportScope, ResourceQuery } from '@/types'
import { useAppStore } from '@/stores/app'
import { dataGateway } from '@/services/dataGateway'
import { countryLabel } from '@/services/countries'
import { copyText } from '@/services/exporter'
import ExportDialog from '@/components/ExportDialog.vue'
import AccessTokenGroupsDialog from '@/components/AccessTokenGroupsDialog.vue'
import SecretCell from '@/components/SecretCell.vue'
import StatCard from '@/components/StatCard.vue'

const store = useAppStore()
const tableRef = ref<TableInstance>()
const search = ref('')
const promotionFilter = ref<ResourceQuery['promotion']>('')
const currentPage = ref(1)
const pageSize = ref<ResourceQuery['pageSize']>(10)
const pageSizeOptions = [10, 20, 50, 100]
const loading = ref(false)
const promotionChecking = ref(false)
const checkingAccountIds = ref<string[]>([])
const selectedIds = ref<string[]>([])
const exportOpen = ref(false)
const exportScope = ref<ExportScope>('selected')
const exportIds = ref<string[]>([])
const exportCount = ref(0)
const exportInitialFormat = ref<ExportFormat>('credentials')
const exportFormatLocked = ref(false)
const accessTokenGroupsOpen = ref(false)
const copyingAt = ref(false)
const copyAtLimit = ref((() => {
  try {
    const saved = Number(localStorage.getItem('autoregister.copyAtLimit') || 100)
    return Number.isInteger(saved) ? Math.max(1, Math.min(10_000, saved)) : 100
  } catch {
    return 100
  }
})())
let searchTimer: ReturnType<typeof setTimeout> | undefined

const pageAccounts = computed(() => store.accounts)

function formatDate(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function hasValidAccessToken(account: AccountRecord) {
  if (!account.accessTokenConfigured || !account.accessTokenExpiresAt) return false
  return new Date(account.accessTokenExpiresAt).getTime() > Date.now()
}

function accessTokenStatus(account: AccountRecord) {
  if (!account.accessTokenConfigured) return 'missing'
  return hasValidAccessToken(account) ? 'valid' : 'expired'
}

function promotionLabel(account: AccountRecord) {
  if (account.planCheckStatus === 'running') return '查询中'
  if (account.planCheckStatus === 'failed') return '查询失败'
  if (account.promotionEligible === true) return '可 Plus 试用'
  if (account.promotionEligible === false) return '不可试用'
  return '未查询'
}

function promotionTagType(account: AccountRecord) {
  if (account.planCheckStatus === 'failed') return 'danger'
  if (account.planCheckStatus === 'running') return 'warning'
  if (account.promotionEligible === true) return 'success'
  return 'info'
}

function promotionTooltip(account: AccountRecord) {
  if (account.planCheckStatus === 'failed') {
    return `错误码：${account.planCheckErrorCode || 'plan_check_failed'}`
  }
  if (account.planCheckedAt) return `查询时间：${formatDate(account.planCheckedAt)}`
  return '尚未查询 Plus 试用资格'
}

function checkoutTypeLabel(account: AccountRecord) {
  if (account.checkoutType === 'oaics') return 'OAICS'
  if (account.checkoutType === 'cs') return 'CS'
  return '待判断'
}

async function checkPromotions(ids: string[]) {
  if (!ids.length || promotionChecking.value) return
  if (ids.length > 100) {
    ElMessage.warning('单次最多查询 100 个账号')
    return
  }
  promotionChecking.value = true
  checkingAccountIds.value = [...ids]
  try {
    const result = await store.checkAccountPromotions(ids)
    if (result.failed || result.skipped) {
      ElMessage.warning(
        `资格查询完成：成功 ${result.succeeded}，失败 ${result.failed}，跳过 ${result.skipped}`,
      )
    } else {
      ElMessage.success(`已查询 ${result.succeeded} 个账号的优惠资格`)
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '优惠资格查询失败')
  } finally {
    checkingAccountIds.value = []
    promotionChecking.value = false
  }
}

function setSelected(id: string, selected: boolean) {
  const next = new Set(selectedIds.value)
  if (selected) next.add(id)
  else next.delete(id)
  selectedIds.value = [...next]
}

function handleSelect(selection: AccountRecord[], row: AccountRecord) {
  setSelected(row.id, selection.some((item) => item.id === row.id))
}

function handleSelectAll(selection: AccountRecord[]) {
  const selectedOnPage = new Set(selection.map((item) => item.id))
  pageAccounts.value.forEach((row) => setSelected(row.id, selectedOnPage.has(row.id)))
}

async function syncPageSelection() {
  await nextTick()
  tableRef.value?.clearSelection()
  const selected = new Set(selectedIds.value)
  pageAccounts.value.forEach((row) => {
    if (selected.has(row.id)) tableRef.value?.toggleRowSelection(row, true)
  })
}

function clearSelection() {
  selectedIds.value = []
  tableRef.value?.clearSelection()
}

function clampCurrentPage() {
  const lastPage = Math.max(1, Math.ceil(store.accountTotal / pageSize.value))
  currentPage.value = Math.min(currentPage.value, lastPage)
}

function handlePageSizeChange(value: number) {
  pageSize.value = value as ResourceQuery['pageSize']
  currentPage.value = 1
}

async function loadPage() {
  loading.value = true
  try {
    await store.refreshAccounts({
      page: currentPage.value,
      pageSize: pageSize.value,
      q: search.value,
      promotion: promotionFilter.value,
    })
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '账号池读取失败')
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  currentPage.value = 1
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => void loadPage(), 250)
}

function openExport(
  scope: ExportScope,
  account?: AccountRecord,
  initialFormat: ExportFormat = 'credentials',
  formatLocked = false,
) {
  exportScope.value = scope
  exportInitialFormat.value = initialFormat
  exportFormatLocked.value = formatLocked
  if (scope === 'single' && account) {
    exportIds.value = [account.id]
    exportCount.value = 1
  }
  if (scope === 'selected') {
    exportIds.value = [...selectedIds.value]
    exportCount.value = selectedIds.value.length
  }
  if (scope === 'all') {
    exportIds.value = []
    exportCount.value = store.accountTotal
  }
  exportOpen.value = true
}

function openAccessTokenExport(scope: 'single' | 'selected', account?: AccountRecord) {
  openExport(scope, account, 'access-tokens', true)
}

function changePromotionFilter(value: string | number | boolean | undefined) {
  promotionFilter.value = String(value || '') as ResourceQuery['promotion']
  currentPage.value = 1
  void loadPage()
}

function openAccessTokenGroups() {
  accessTokenGroupsOpen.value = true
}

async function copyAccessTokens() {
  if (copyingAt.value) return
  copyingAt.value = true
  try {
    const payload = await dataGateway.exportAccounts('access-tokens', 'all', [])
    const tokens = payload.content
      .split(/\r?\n/)
      .map((token) => token.trim())
      .filter(Boolean)
      .slice(0, copyAtLimit.value)
    if (!tokens.length) {
      ElMessage.warning('账号池中没有有效 AT')
      return
    }
    await copyText(tokens.join('\n'))
    ElMessage.success(`已复制前 ${tokens.length} 个有效 AT`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '批量复制 AT 失败')
  } finally {
    copyingAt.value = false
  }
}

watch(copyAtLimit, (value) => {
  copyAtLimit.value = Math.max(1, Math.min(10_000, Number(value) || 100))
  try {
    localStorage.setItem('autoregister.copyAtLimit', String(copyAtLimit.value))
  } catch {
    // Local storage is optional.
  }
})

function openMailbox(account: AccountRecord) {
  if (!account.emailAccessUrl) return
  try {
    const url = new URL(account.emailAccessUrl)
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error('unsupported_protocol')
    window.open(url.toString(), '_blank', 'noopener,noreferrer')
  } catch {
    ElMessage.error('接码 URL 格式无效')
  }
}

async function deleteSelected() {
  if (!selectedIds.value.length) return
  const count = selectedIds.value.length
  try {
    await ElMessageBox.confirm(
      `确认永久删除选中的 ${count} 个账号？此操作会写入 MongoDB。`,
      '删除选中账号',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  const deleted = await store.deleteAccounts([...selectedIds.value])
  clearSelection()
  clampCurrentPage()
  if (store.accountQuery.page !== currentPage.value) await loadPage()
  await syncPageSelection()
  ElMessage.success(`已删除 ${deleted} 个账号`)
}

watch(pageAccounts, () => void syncPageSelection(), { flush: 'post', immediate: true })
watch([currentPage, pageSize], () => void loadPage())
onMounted(() => void loadPage())
onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
})
</script>

<template>
  <section>
    <div class="page-heading">
      <div>
        <h2>已完成账号</h2>
        <p>成功记录自动进入账号池；凭据只读并默认遮罩。</p>
      </div>
    </div>

    <div class="stats-grid">
      <StatCard label="账号总数" :value="store.stats.accounts.total" note="MongoDB 全部成功记录" :icon="UserFilled" />
      <StatCard label="今日新增" :value="store.stats.accounts.today" note="按 UTC 日期统计" :icon="Download" tone="green" />
      <StatCard label="TOTP 完整" :value="store.stats.accounts.totpComplete" note="密钥字段完整" :icon="Key" tone="amber" />
      <StatCard label="可导出" :value="store.stats.accounts.total" note="两种格式均已就绪" :icon="Lock" tone="green" />
    </div>

    <div class="panel table-panel">
      <div class="table-toolbar">
        <div class="toolbar-group">
          <el-input
            v-model="search"
            class="toolbar-search"
            :prefix-icon="Search"
            clearable
            placeholder="搜索邮箱"
            @input="handleSearch"
          />
          <span class="muted">已选择 {{ selectedIds.length }} 条</span>
        </div>
        <div class="toolbar-group">
          <el-button
            type="warning"
            plain
            :icon="MagicStick"
            :loading="promotionChecking"
            :disabled="selectedIds.length === 0 || promotionChecking"
            @click="checkPromotions([...selectedIds])"
          >
            查询选中优惠
          </el-button>
          <el-button
            type="danger"
            plain
            :icon="Delete"
            :disabled="selectedIds.length === 0"
            @click="deleteSelected"
          >
            删除选中
          </el-button>
          <el-button :disabled="selectedIds.length === 0" @click="openExport('selected')">
            导出选中
          </el-button>
          <el-button
            type="success"
            plain
            :icon="Key"
            :disabled="selectedIds.length === 0"
            @click="openAccessTokenExport('selected')"
          >
            提取选中 AT
          </el-button>
          <el-button
            type="success"
            plain
            :icon="CopyDocument"
            :disabled="store.accountTotal === 0"
            @click="openAccessTokenGroups"
          >
            AT 分组复制（每组 10 个）
          </el-button>
          <el-button
            type="success"
            :icon="CopyDocument"
            :loading="copyingAt"
            :disabled="store.accountTotal === 0"
            @click="copyAccessTokens"
          >
            一键复制前 {{ copyAtLimit }} 个 AT
          </el-button>
          <el-input-number
            v-model="copyAtLimit"
            :min="1"
            :max="10000"
            :step="10"
            controls-position="right"
            aria-label="一键复制 AT 数量"
            style="width: 128px"
          />
          <el-select
            :model-value="promotionFilter"
            clearable
            placeholder="筛选 Plus 标签"
            style="width: 180px"
            @change="changePromotionFilter"
          >
            <el-option label="未试用 Plus" value="untried_plus" />
            <el-option label="不可试用" value="ineligible" />
            <el-option label="未查询" value="unchecked" />
          </el-select>
          <el-button type="primary" plain :disabled="store.accountTotal === 0" @click="openExport('all')">导出全部</el-button>
        </div>
      </div>

      <el-table
        ref="tableRef"
        v-loading="loading"
        :data="pageAccounts"
        row-key="id"
        empty-text="账号池暂无数据"
        @select="handleSelect"
        @select-all="handleSelectAll"
      >
        <el-table-column type="selection" width="48" />
        <el-table-column label="邮箱" min-width="220">
          <template #default="{ row }">
            <div class="account-email">
              <span class="email-avatar">{{ row.email.slice(0, 1).toUpperCase() }}</span>
              <div>
                <div class="account-email-line">
                  <strong>{{ row.email }}</strong>
                  <el-tag size="small" effect="plain" :type="row.registrationCountry ? 'success' : 'info'">
                    {{ row.registrationCountry ? countryLabel(row.registrationCountry) : '历史账号' }}
                  </el-tag>
                </div>
                <small>ID {{ row.id }}</small>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="账号类型" width="96">
          <template #default="{ row }">
            <el-tag :type="row.accountType === 'plus' ? 'warning' : 'info'" effect="dark" round>
              {{ row.accountType === 'plus' ? 'Plus' : 'Free' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="结账类型" width="105">
          <template #default="{ row }">
            <el-tag
              :type="row.checkoutType === 'oaics' ? 'success' : row.checkoutType === 'cs' ? 'warning' : 'info'"
              effect="plain"
              round
            >
              {{ checkoutTypeLabel(row) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="手机号状态" width="128">
          <template #default="{ row }">
            <el-tag
              v-if="row.accountType === 'plus'"
              :type="row.phoneBound ? 'success' : 'warning'"
              effect="plain"
              round
            >
              {{ row.phoneBound ? '已接码' : '未接码' }}
            </el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="优惠资格" width="145">
          <template #default="{ row }">
            <el-tooltip :content="promotionTooltip(row)">
              <el-tag :type="promotionTagType(row)" effect="plain" round>
                {{ promotionLabel(row) }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="CHATGPT 密码" min-width="210">
          <template #default="{ row }"><SecretCell :value="row.chatgptPassword" /></template>
        </el-table-column>
        <el-table-column label="TOTP 密钥" min-width="205">
          <template #default="{ row }"><SecretCell :value="row.totpSecret" /></template>
        </el-table-column>
        <el-table-column label="AT 状态" width="158">
          <template #default="{ row }">
            <div class="at-status">
              <el-tag
                :type="accessTokenStatus(row) === 'valid' ? 'success' : accessTokenStatus(row) === 'expired' ? 'warning' : 'info'"
                effect="plain"
                round
              >
                {{ accessTokenStatus(row) === 'valid' ? '已提取' : accessTokenStatus(row) === 'expired' ? '已过期' : '未提取' }}
              </el-tag>
              <small v-if="row.accessTokenExpiresAt">{{ formatDate(row.accessTokenExpiresAt) }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="145">
          <template #default="{ row }"><span class="muted">{{ formatDate(row.createdAt) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="252" fixed="right">
          <template #default="{ row }">
            <el-tooltip content="查询 Plus 试用资格">
              <span>
                <el-button
                  text
                  type="warning"
                  :icon="MagicStick"
                  aria-label="查询优惠资格"
                  :loading="checkingAccountIds.includes(row.id)"
                  :disabled="!hasValidAccessToken(row) || promotionChecking"
                  @click="checkPromotions([row.id])"
                />
              </span>
            </el-tooltip>
            <el-tooltip content="打开接码 URL">
              <span>
                <el-button
                  text
                  type="primary"
                  :icon="Link"
                  :disabled="!row.emailAccessUrl"
                  aria-label="打开接码 URL"
                  @click="openMailbox(row)"
                />
              </span>
            </el-tooltip>
            <el-button text type="primary" @click="openExport('single', row)">导出</el-button>
            <el-tooltip :content="hasValidAccessToken(row) ? '复制或下载已保存的 AT' : '该账号没有有效 AT'">
              <span>
                <el-button
                  text
                  type="success"
                  :disabled="!hasValidAccessToken(row)"
                  @click="openAccessTokenExport('single', row)"
                >提取AT</el-button>
              </span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>

      <div class="table-footer">
        <span>共 {{ store.accountTotal }} 条 · 已选 {{ selectedIds.length }} 条 · 数据已持久化到 MongoDB</span>
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          background
          layout="total, sizes, prev, pager, next"
          :page-sizes="pageSizeOptions"
          :total="store.accountTotal"
          @size-change="handlePageSizeChange"
        />
      </div>
    </div>

    <ExportDialog
      v-model="exportOpen"
      :scope="exportScope"
      :ids="exportIds"
      :count="exportCount"
      :initial-format="exportInitialFormat"
      :format-locked="exportFormatLocked"
    />
    <AccessTokenGroupsDialog v-model="accessTokenGroupsOpen" />
  </section>
</template>

<style scoped>
.account-email {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 10px;
}

.account-email-line {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 6px;
}

.account-email-line :deep(.el-tag) {
  flex: 0 0 auto;
  font-size: 9px;
}

.at-status {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}

.at-status small {
  color: var(--text-muted);
  font-size: 10px;
}

.email-avatar {
  display: grid;
  width: 31px;
  height: 31px;
  flex: 0 0 31px;
  place-items: center;
  border: 1px solid rgb(50 197 255 / 24%);
  border-radius: 9px;
  color: #aeeaff;
  background: rgb(50 197 255 / 8%);
  font-size: 11px;
  font-weight: 800;
}

.account-email strong,
.account-email small {
  display: block;
  overflow: hidden;
  max-width: 190px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-email strong {
  color: #e3ebf5;
  font-size: 12px;
  font-weight: 600;
}

.account-email small {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 9px;
}
</style>
