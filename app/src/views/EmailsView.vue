<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  CircleCheck,
  Delete,
  Download,
  Message,
  Plus,
  Refresh,
  Search,
  UploadFilled,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox, type TableInstance } from 'element-plus'
import { useAppStore } from '@/stores/app'
import EmailExportDialog from '@/components/EmailExportDialog.vue'
import ImportDialog from '@/components/ImportDialog.vue'
import SecretCell from '@/components/SecretCell.vue'
import StatCard from '@/components/StatCard.vue'
import type { EmailRecord, EmailSource, ExportScope, ImportResult, ResourceQuery } from '@/types'

const store = useAppStore()
const tableRef = ref<TableInstance>()
const search = ref('')
const importOpen = ref(false)
const currentPage = ref(1)
const pageSize = ref<ResourceQuery['pageSize']>(10)
const pageSizeOptions = [10, 20, 50, 100]
const loading = ref(false)
const sourceFilter = ref<EmailSource>('all')
const syncingAliases = ref(false)
const selectedIds = ref<string[]>([])
const exportOpen = ref(false)
const exportScope = ref<ExportScope>('selected')
const exportIds = ref<string[]>([])
const exportCount = ref(0)
let searchTimer: ReturnType<typeof setTimeout> | undefined

const pageEmails = computed(() => store.emails)
const existingKeys = computed(() => store.emails.map((item) => item.email.toLowerCase()))

function formatDate(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function setSelected(id: string, selected: boolean) {
  const next = new Set(selectedIds.value)
  if (selected) next.add(id)
  else next.delete(id)
  selectedIds.value = [...next]
}

function handleSelect(selection: EmailRecord[], row: EmailRecord) {
  setSelected(row.id, selection.some((item) => item.id === row.id))
}

function handleSelectAll(selection: EmailRecord[]) {
  const selectedOnPage = new Set(selection.map((item) => item.id))
  pageEmails.value.forEach((row) => setSelected(row.id, selectedOnPage.has(row.id)))
}

async function syncPageSelection() {
  await nextTick()
  tableRef.value?.clearSelection()
  const selected = new Set(selectedIds.value)
  pageEmails.value.forEach((row) => {
    if (selected.has(row.id)) tableRef.value?.toggleRowSelection(row, true)
  })
}

function clearSelection() {
  selectedIds.value = []
  tableRef.value?.clearSelection()
}

function clampCurrentPage() {
  const lastPage = Math.max(1, Math.ceil(store.emailTotal / pageSize.value))
  currentPage.value = Math.min(currentPage.value, lastPage)
}

function handlePageSizeChange(value: number) {
  pageSize.value = value as ResourceQuery['pageSize']
  currentPage.value = 1
}

async function loadPage() {
  loading.value = true
  try {
    await store.refreshEmails({
      page: currentPage.value,
      pageSize: pageSize.value,
      q: search.value,
      source: sourceFilter.value,
    })
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '邮箱池读取失败')
  } finally {
    loading.value = false
  }
}

async function syncAliases() {
  syncingAliases.value = true
  try {
    const result = await store.syncMailcomAliases()
    ElMessage.success(
      `分裂邮箱同步完成：新增 ${result.imported}，已存在 ${result.duplicateCount}，错误 ${result.errorCount}`,
    )
    await loadPage()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '分裂邮箱同步失败')
  } finally {
    syncingAliases.value = false
  }
}

function handleSearch() {
  currentPage.value = 1
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => void loadPage(), 250)
}

async function submitImport(rawText: string) {
  return store.importEmails(rawText)
}

async function afterImport(_result: ImportResult) {
  clampCurrentPage()
  if (store.emailQuery.page !== currentPage.value) await loadPage()
  await syncPageSelection()
}

function openExport(scope: ExportScope, email?: EmailRecord) {
  exportScope.value = scope
  if (scope === 'single' && email) {
    exportIds.value = [email.id]
    exportCount.value = 1
  }
  if (scope === 'selected') {
    exportIds.value = [...selectedIds.value]
    exportCount.value = selectedIds.value.length
  }
  if (scope === 'all') {
    exportIds.value = []
    exportCount.value = store.emailTotal
  }
  exportOpen.value = true
}

async function deleteSelected() {
  if (!selectedIds.value.length) return
  const count = selectedIds.value.length
  try {
    await ElMessageBox.confirm(
      `确认永久删除选中的 ${count} 个邮箱？此操作会写入 MongoDB。`,
      '删除选中邮箱',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  const deleted = await store.deleteEmails([...selectedIds.value])
  clearSelection()
  clampCurrentPage()
  if (store.emailQuery.page !== currentPage.value) await loadPage()
  await syncPageSelection()
  ElMessage.success(`已删除 ${deleted} 个邮箱`)
}

watch(pageEmails, () => void syncPageSelection(), { flush: 'post', immediate: true })
watch([currentPage, pageSize], () => void loadPage())
watch(sourceFilter, () => {
  currentPage.value = 1
  void loadPage()
})
onMounted(() => void loadPage())
onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
})
</script>

<template>
  <section>
    <div class="page-heading">
      <div>
        <h2>待使用邮箱</h2>
        <p>注册成功后邮箱会移出此列表，接码地址快照保留在账号记录中。</p>
      </div>
    </div>

    <div class="stats-grid">
      <StatCard label="可用邮箱" :value="store.stats.emails.available" note="MongoDB 当前待分配总数" :icon="Message" />
      <StatCard label="分裂邮箱" :value="store.stats.emails.aliases" note="来自 MailCom Hub" :icon="CircleCheck" tone="green" />
      <StatCard label="本次选中" :value="selectedIds.length" note="支持跨页选择" :icon="UploadFilled" tone="amber" />
      <StatCard label="存储模式" value="MongoDB" note="本机数据库持久化" :icon="Message" tone="blue" />
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
          <el-select v-model="sourceFilter" style="width: 150px" aria-label="邮箱来源筛选">
            <el-option label="全部来源" value="all" />
            <el-option label="普通邮箱" value="standard" />
            <el-option label="分裂邮箱" value="mailcom_alias" />
          </el-select>
          <span class="muted">已选择 {{ selectedIds.length }} 条</span>
        </div>
        <div class="toolbar-group">
          <el-button :icon="Refresh" :loading="syncingAliases" @click="syncAliases">同步分裂邮箱</el-button>
          <el-button :icon="Plus" @click="importOpen = true">批量导入</el-button>
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
          <el-button type="primary" plain :icon="Download" :disabled="store.emailTotal === 0" @click="openExport('all')">
            导出全部
          </el-button>
        </div>
      </div>

      <el-table
        ref="tableRef"
        v-loading="loading"
        :data="pageEmails"
        row-key="id"
        empty-text="邮箱池暂无数据"
        @select="handleSelect"
        @select-all="handleSelectAll"
      >
        <el-table-column type="selection" width="48" />
        <el-table-column label="邮箱" min-width="260">
          <template #default="{ row }">
            <div class="email-entry">
              <span class="availability-dot" />
              <div>
                <strong>{{ row.email }}</strong>
                <small>
                  {{ row.sourceType === 'mailcom_alias' ? `分裂邮箱 · 主邮箱 ${row.parentEmail || '未知'}` : '普通邮箱 · 等待分配' }}
                </small>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="110">
          <template #default="{ row }">
            <el-tag :type="row.sourceType === 'mailcom_alias' ? 'success' : 'info'" effect="plain">
              {{ row.sourceType === 'mailcom_alias' ? '分裂邮箱' : '普通邮箱' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="接码地址" min-width="390">
          <template #default="{ row }">
            <SecretCell :value="row.accessUrl" :visible-chars="18" /><!-- gitleaks:allow -->
          </template>
        </el-table-column>
        <el-table-column label="导入时间" width="160">
          <template #default="{ row }"><span class="muted">{{ formatDate(row.importedAt) }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="105">
          <template #default><el-tag type="success" effect="dark" round>可用</el-tag></template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button text type="primary" @click="openExport('single', row)">导出</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="table-footer">
        <span>共 {{ store.emailTotal }} 条 · 已选 {{ selectedIds.length }} 条 · 重复邮箱导入时自动跳过</span>
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          background
          layout="total, sizes, prev, pager, next"
          :page-sizes="pageSizeOptions"
          :total="store.emailTotal"
          @size-change="handlePageSizeChange"
        />
      </div>
    </div>

    <ImportDialog
      v-model="importOpen"
      kind="email"
      :existing-keys="existingKeys"
      :submit-handler="submitImport"
      @imported="afterImport"
    />
    <EmailExportDialog
      v-model="exportOpen"
      :scope="exportScope"
      :ids="exportIds"
      :count="exportCount"
    />
  </section>
</template>

<style scoped>
.email-entry {
  display: flex;
  align-items: center;
  gap: 11px;
}

.availability-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--success);
  box-shadow: 0 0 12px rgb(69 214 138 / 54%);
}

.email-entry strong,
.email-entry small {
  display: block;
}

.email-entry strong {
  color: #e2ebf5;
  font-size: 12px;
}

.email-entry small {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 9px;
}
</style>
