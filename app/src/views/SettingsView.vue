<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import {
  Connection,
  Delete,
  DocumentAdd,
  Monitor,
  Refresh,
  Setting,
  Stopwatch,
} from '@element-plus/icons-vue'
import {
  ElMessage,
  ElMessageBox,
  type FormInstance,
  type FormRules,
  type TableInstance,
} from 'element-plus'
import { useAppStore } from '@/stores/app'
import { proxyKey } from '@/services/parsers'
import { COMMON_REGISTRATION_COUNTRIES, countryLabel, normalizeCountryCode } from '@/services/countries'
import ImportDialog from '@/components/ImportDialog.vue'
import SecretCell from '@/components/SecretCell.vue'
import StatCard from '@/components/StatCard.vue'
import type { ExecutionSettingsInput, ImportResult, ProxyGroupSummary, ProxyRecord, ResourceQuery } from '@/types'

const store = useAppStore()
const formRef = ref<FormInstance>()
const saving = ref(false)
const registrationSecuritySaving = ref(false)
const importOpen = ref(false)
const proxyLoading = ref(false)
const proxyTableRef = ref<TableInstance>()
const proxyPage = ref(1)
const proxyPageSize = ref<ResourceQuery['pageSize']>(10)
const proxyPageSizes = [10, 20, 50, 100]
const selectedProxyIds = ref<string[]>([])
const proxyCountryFilter = ref('')
const importCountry = ref('')
const importGroup = ref('默认组')
const proxyMutation = ref<'selected' | 'all' | `single:${string}` | null>(null)
const form = reactive<ExecutionSettingsInput>({
  browserExecutablePath: store.settings.browserExecutablePath,
  roxyApiKey: store.settings.roxyApiKey,
  roxyApiPort: store.settings.roxyApiPort,
  headless: store.settings.headless,
  requireRegistrationPassword: store.settings.requireRegistrationPassword,
  enableRegistrationTotp: store.settings.enableRegistrationTotp,
  proxyRetryCount: store.settings.proxyRetryCount,
  concurrency: store.settings.concurrency,
  taskTimeoutSeconds: store.settings.taskTimeoutSeconds,
})

watch(
  () => store.settings,
  (settings) => {
    if (!registrationSecuritySaving.value) {
      form.browserExecutablePath = settings.browserExecutablePath
      form.roxyApiKey = settings.roxyApiKey
      form.roxyApiPort = settings.roxyApiPort
      form.headless = settings.headless
      form.proxyRetryCount = settings.proxyRetryCount
      form.concurrency = settings.concurrency
      form.taskTimeoutSeconds = settings.taskTimeoutSeconds
    }
    form.requireRegistrationPassword = settings.requireRegistrationPassword
    form.enableRegistrationTotp = settings.enableRegistrationTotp
  },
  { deep: true },
)

const rules: FormRules<ExecutionSettingsInput> = {
  browserExecutablePath: [
    { required: true, whitespace: true, message: '浏览器路径不能为空', trigger: 'blur' },
  ],
  roxyApiPort: [
    { required: true, type: 'integer', min: 1, max: 65535, message: 'API 端口必须为 1–65535 的整数' },
  ],
  proxyRetryCount: [
    { required: true, type: 'integer', min: 0, max: 5, message: '代理重试次数必须为 0–5 的整数' },
  ],
  concurrency: [
    { required: true, type: 'integer', min: 1, max: 12, message: '并发数必须为 1–12 的整数' },
  ],
  taskTimeoutSeconds: [
    { required: true, type: 'integer', min: 0, message: '任务超时必须为非负整数' },
  ],
}

const existingProxyKeys = computed(() => store.proxies.map(proxyKey))
const pageProxies = computed(() => store.proxies)
const proxyMutationBusy = computed(() => proxyMutation.value !== null)
const proxyCountryOptions = computed(() => {
  const codes = new Set<string>(COMMON_REGISTRATION_COUNTRIES.map((item) => item.value))
  store.proxyCountries.forEach((item) => codes.add(item.country))
  return [...codes]
    .filter((code) => code !== 'ZZ')
    .sort()
    .map((code) => ({ value: code, label: countryLabel(code) }))
})
const visibleProxyGroups = computed(() =>
  store.proxyGroups.filter((item) => !proxyCountryFilter.value || item.country === proxyCountryFilter.value),
)
function proxyGroupKey(group: ProxyGroupSummary) {
  return `${group.country}:${group.group}`
}

function formatDate(value: string | null) {
  if (!value) return '尚未检测'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

async function saveSettings() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  saving.value = true
  try {
    const payload: ExecutionSettingsInput = {
      browserExecutablePath: form.browserExecutablePath,
      roxyApiKey: form.roxyApiKey,
      roxyApiPort: form.roxyApiPort,
      headless: form.headless,
      requireRegistrationPassword: form.requireRegistrationPassword,
      enableRegistrationTotp: form.enableRegistrationTotp,
      proxyRetryCount: form.proxyRetryCount,
      concurrency: form.concurrency,
      taskTimeoutSeconds: form.taskTimeoutSeconds,
    }
    await store.saveSettings(payload)
    ElMessage.success('执行配置已保存到本机配置文件')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '配置保存失败')
  } finally {
    saving.value = false
  }
}

async function saveRegistrationSecuritySettings() {
  if (registrationSecuritySaving.value || saving.value) return

  registrationSecuritySaving.value = true
  try {
    const settings = store.settings
    await store.saveSettings({
      browserExecutablePath: settings.browserExecutablePath,
      roxyApiKey: settings.roxyApiKey,
      roxyApiPort: settings.roxyApiPort,
      headless: settings.headless,
      requireRegistrationPassword: form.requireRegistrationPassword,
      enableRegistrationTotp: form.enableRegistrationTotp,
      proxyRetryCount: settings.proxyRetryCount,
      concurrency: settings.concurrency,
      taskTimeoutSeconds: settings.taskTimeoutSeconds,
    })
    ElMessage.success(
      `注册安全设置已保存：密码${form.requireRegistrationPassword ? '开启' : '关闭'}，2FA${form.enableRegistrationTotp ? '开启' : '关闭'}`,
    )
  } catch (error) {
    form.requireRegistrationPassword = store.settings.requireRegistrationPassword
    form.enableRegistrationTotp = store.settings.enableRegistrationTotp
    ElMessage.error(error instanceof Error ? error.message : '注册安全设置保存失败')
  } finally {
    registrationSecuritySaving.value = false
  }
}

async function submitProxyImport(rawText: string) {
  const value = importCountry.value.trim().toUpperCase()
  const country = /^[A-Z]{2}$/.test(value) ? value : undefined
  return store.importProxies(rawText, country, importGroup.value.trim() || '默认组')
}

async function afterProxyImport(_result: ImportResult) {
  proxyPage.value = 1
  await loadProxies()
}

async function toggleProxyGroup(group: ProxyGroupSummary, value: string | number | boolean) {
  proxyMutation.value = 'selected'
  try {
    await store.updateProxyGroup({
      country: group.country,
      group: group.group,
      enabled: Boolean(value),
    })
    ElMessage.success(`分组“${group.group}”已${value ? '启用' : '暂停'}`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '分组状态更新失败')
  } finally {
    proxyMutation.value = null
  }
}

async function renameProxyGroup(group: ProxyGroupSummary) {
  try {
    const result = await ElMessageBox.prompt('输入新的分组名称', '重命名代理分组', {
      inputValue: group.group,
      inputPattern: /\S/,
      inputErrorMessage: '分组名称不能为空',
      confirmButtonText: '保存',
      cancelButtonText: '取消',
    })
    const newGroup = result.value.trim()
    if (!newGroup || newGroup === group.group) return
    await store.updateProxyGroup({ country: group.country, group: group.group, newGroup })
    ElMessage.success(`分组已重命名为“${newGroup}”`)
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(error instanceof Error ? error.message : '分组重命名失败')
  }
}

async function changeProxyGroupCountry(group: ProxyGroupSummary, country: string) {
  const normalized = normalizeCountryCode(country)
  if (normalized === 'ZZ' || normalized === group.country) return
  await store.updateProxyGroup({
    country: group.country,
    group: group.group,
    newCountry: normalized,
  })
  ElMessage.success(`分组“${group.group}”已移动到 ${countryLabel(normalized)}`)
}

async function removeProxyGroup(group: ProxyGroupSummary) {
  try {
    await ElMessageBox.confirm(
      `确认删除 ${countryLabel(group.country)} / ${group.group} 中的 ${group.total} 条代理？`,
      '删除代理分组',
      { type: 'warning', confirmButtonText: '删除分组', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  const deleted = await store.deleteProxyGroup(group.country, group.group)
  ElMessage.success(`已删除分组及其中 ${deleted} 条代理`)
}

async function toggleProxy(proxy: ProxyRecord, value: string | number | boolean) {
  await store.setProxyEnabled(proxy.id, Boolean(value))
}

async function changeProxyCountry(proxy: ProxyRecord, value: string) {
  const country = normalizeCountryCode(value)
  if (country === 'ZZ') {
    ElMessage.warning('请输入两位国家码')
    return
  }
  await store.setProxyCountry(proxy.id, country)
}

function setProxySelected(id: string, selected: boolean) {
  const next = new Set(selectedProxyIds.value)
  if (selected) next.add(id)
  else next.delete(id)
  selectedProxyIds.value = [...next]
}

function handleProxySelect(selection: ProxyRecord[], row: ProxyRecord) {
  setProxySelected(row.id, selection.some((item) => item.id === row.id))
}

function handleProxySelectAll(selection: ProxyRecord[]) {
  const selectedOnPage = new Set(selection.map((item) => item.id))
  pageProxies.value.forEach((row) => setProxySelected(row.id, selectedOnPage.has(row.id)))
}

async function syncProxySelection() {
  await nextTick()
  proxyTableRef.value?.clearSelection()
  const selected = new Set(selectedProxyIds.value)
  pageProxies.value.forEach((row) => {
    if (selected.has(row.id)) proxyTableRef.value?.toggleRowSelection(row, true)
  })
}

function clearProxySelection() {
  selectedProxyIds.value = []
  proxyTableRef.value?.clearSelection()
}

function clampProxyPage() {
  const lastPage = Math.max(1, Math.ceil(store.proxyTotal / proxyPageSize.value))
  proxyPage.value = Math.min(proxyPage.value, lastPage)
}

async function removeProxy(proxy: ProxyRecord) {
  try {
    await ElMessageBox.confirm(`确认删除代理 ${proxy.host}:${proxy.port}？`, '删除代理', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }

  proxyMutation.value = `single:${proxy.id}`
  try {
    const deleted = await store.deleteProxy(proxy.id)
    setProxySelected(proxy.id, false)
    clampProxyPage()
    if (store.proxyQuery.page !== proxyPage.value) await loadProxies()
    await syncProxySelection()
    ElMessage.success(`已删除 ${deleted} 条代理`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '代理删除失败')
  } finally {
    proxyMutation.value = null
  }
}

async function deleteSelectedProxies() {
  if (!selectedProxyIds.value.length || proxyMutationBusy.value) return
  const ids = [...selectedProxyIds.value]
  try {
    await ElMessageBox.confirm(
      `确认永久删除选中的 ${ids.length} 条代理？此操作不可恢复。`,
      '批量删除代理',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  proxyMutation.value = 'selected'
  try {
    const deleted = await store.deleteProxies(ids)
    clearProxySelection()
    clampProxyPage()
    if (store.proxyQuery.page !== proxyPage.value) await loadProxies()
    await syncProxySelection()
    ElMessage.success(`已删除 ${deleted} 条代理`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '批量删除代理失败')
  } finally {
    proxyMutation.value = null
  }
}

async function clearProxyPool() {
  if (!store.proxyTotal || proxyMutationBusy.value) return
  const total = store.proxyTotal
  try {
    await ElMessageBox.confirm(
      `确认清空代理池中的全部 ${total} 条代理？此操作不可恢复。`,
      '清空代理池',
      {
        type: 'error',
        confirmButtonText: '清空全部',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  proxyMutation.value = 'all'
  try {
    const deleted = await store.clearProxies()
    clearProxySelection()
    proxyPage.value = 1
    if (store.proxyQuery.page !== proxyPage.value) await loadProxies()
    await syncProxySelection()
    ElMessage.success(`代理池已清空，共删除 ${deleted} 条代理`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '清空代理池失败')
  } finally {
    proxyMutation.value = null
  }
}

async function loadProxies() {
  proxyLoading.value = true
  try {
    await Promise.all([
      store.refreshProxies({
        page: proxyPage.value,
        pageSize: proxyPageSize.value,
        q: '',
        country: proxyCountryFilter.value,
      }),
      store.refreshProxyCountries(),
      store.refreshProxyGroups(),
    ])
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '代理池读取失败')
  } finally {
    proxyLoading.value = false
  }
}

function statusType(status: ProxyRecord['status']) {
  if (status === 'available') return 'success'
  if (status === 'quarantined') return 'danger'
  return 'info'
}

function statusLabel(status: ProxyRecord['status']) {
  if (status === 'available') return '可用'
  if (status === 'quarantined') return '隔离'
  return '未知'
}

watch(pageProxies, () => void syncProxySelection(), { flush: 'post', immediate: true })
watch([proxyPage, proxyPageSize], () => void loadProxies())
watch(proxyCountryFilter, () => {
  proxyPage.value = 1
  void loadProxies()
})
onMounted(() => void loadProxies())
</script>

<template>
  <section>
    <div class="page-heading">
      <div>
        <h2>执行环境</h2>
        <p>执行参数保存到本机 JSON；代理池保存到本机 MongoDB。</p>
      </div>
      <div class="heading-actions">
        <div class="heading-save-time">
          <span>最后保存</span>
          <strong>{{ store.settings.updatedAt ? formatDate(store.settings.updatedAt) : '尚未保存到磁盘' }}</strong>
        </div>
        <el-button
          type="primary"
          :icon="Setting"
          :loading="saving"
          :disabled="registrationSecuritySaving"
          @click="saveSettings"
        >
          保存执行配置
        </el-button>
      </div>
    </div>

    <div class="settings-layout">
      <div class="panel execution-panel">
        <div class="panel-heading">
          <div class="panel-icon"><el-icon><Monitor /></el-icon></div>
          <div><h3>浏览器与任务参数</h3><p>D:\AutoRegister\data\settings.json</p></div>
          <el-tag :type="store.configServiceOnline ? 'success' : 'warning'" effect="dark" round>
            {{ store.configServiceOnline ? '配置服务在线' : '使用默认值' }}
          </el-tag>
        </div>

        <el-alert
          v-if="!store.configServiceOnline"
          class="config-alert"
          type="warning"
          :closable="false"
          show-icon
          :title="store.configError || '配置服务离线，请先启动 FastAPI 服务'"
        />

        <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
          <div class="api-settings-card">
            <div class="api-settings-title">
              <span>指纹浏览器本地API</span>
              <el-tag :type="form.roxyApiKey.trim() ? 'success' : 'warning'" size="small" round>
                {{ form.roxyApiKey.trim() ? '密钥已配置' : '等待配置' }}
              </el-tag>
            </div>
            <div class="api-input-grid">
              <el-form-item class="browser-path-field" label="指纹浏览器地址" prop="browserExecutablePath">
                <el-input v-model="form.browserExecutablePath" class="mono" placeholder="D:\RoxyBrowser\RoxyBrowser.exe" />
                <small>本地 API 不在线时将从此地址启动客户端</small>
              </el-form-item>
              <el-form-item label="Roxy API Key" prop="roxyApiKey">
                <el-input
                  v-model="form.roxyApiKey"
                  class="mono"
                  type="text"
                  autocomplete="off"
                  :spellcheck="false"
                  placeholder="请输入 Roxy API Key"
                />
              </el-form-item>
              <el-form-item label="API 端口" prop="roxyApiPort">
                <el-input-number v-model="form.roxyApiPort" :min="1" :max="65535" :step="1" controls-position="right" />
              </el-form-item>
            </div>
            <div class="headless-row">
              <div>
                <strong>浏览器启动模式</strong>
                <small>接口固定使用 127.0.0.1；首次调试建议使用有头模式</small>
              </div>
              <div class="headless-control">
                <span class="headless-mode-label">{{ form.headless ? '无头' : '有头' }}</span>
                <el-switch
                  v-model="form.headless"
                  aria-label="切换浏览器启动模式"
                />
              </div>
            </div>
          </div>
          <div class="form-grid task-settings-grid">
            <el-form-item label="代理额外重试次数" prop="proxyRetryCount">
              <el-input-number v-model="form.proxyRetryCount" :min="0" :max="5" :step="1" controls-position="right" />
              <small>同一代理初次失败后的额外重试，默认 1</small>
            </el-form-item>
            <el-form-item label="注册时设置密码">
              <div class="registration-setting-control">
                <el-switch
                  v-model="form.requireRegistrationPassword"
                  aria-label="注册时设置密码"
                  :disabled="saving || registrationSecuritySaving"
                  :loading="registrationSecuritySaving"
                  @change="saveRegistrationSecuritySettings"
                />
                <strong :class="{ enabled: form.requireRegistrationPassword }">
                  {{ form.requireRegistrationPassword ? '已开启' : '已关闭' }}
                </strong>
              </div>
              <small>切换后立即保存；开启后强制选择密码注册，关闭后优先使用邮箱验证码</small>
            </el-form-item>
            <el-form-item label="注册时设置 2FA">
              <div class="registration-setting-control">
                <el-switch
                  v-model="form.enableRegistrationTotp"
                  aria-label="注册时设置 2FA"
                  :disabled="saving || registrationSecuritySaving"
                  :loading="registrationSecuritySaving"
                  @change="saveRegistrationSecuritySettings"
                />
                <strong :class="{ enabled: form.enableRegistrationTotp }">
                  {{ form.enableRegistrationTotp ? '已开启' : '已关闭' }}
                </strong>
              </div>
              <small>切换后立即保存；关闭时跳过认证器配置并保留空 TOTP 密钥</small>
            </el-form-item>
            <el-form-item label="最大并发任务" prop="concurrency">
              <el-input-number v-model="form.concurrency" :min="1" :max="12" :step="1" controls-position="right" />
              <small>可调范围 1–12，默认 2</small>
            </el-form-item>
            <el-form-item label="整批任务超时（秒）" prop="taskTimeoutSeconds">
              <el-input-number v-model="form.taskTimeoutSeconds" :min="0" :step="30" controls-position="right" />
              <small>0 表示不限时；正整数为整批任务硬上限</small>
            </el-form-item>
          </div>
        </el-form>

      </div>

      <aside class="config-side">
        <StatCard label="并发上限" :value="form.concurrency" note="浏览器任务 Slot" :icon="Connection" />
        <StatCard
          label="整批超时"
          :value="form.taskTimeoutSeconds === 0 ? '无限制' : `${form.taskTimeoutSeconds}s`"
          note="0 = 无整批硬上限"
          :icon="Stopwatch"
          tone="amber"
        />
      </aside>
    </div>

    <div class="proxy-heading">
      <div><h2>按国家和分组管理代理</h2><p>同一国家可建立多个代理公式分组；注册、提链和支付都可以选择具体分组。</p></div>
      <div class="toolbar-group">
        <el-select v-model="proxyCountryFilter" clearable filterable placeholder="筛选国家" style="width: 150px">
          <el-option v-for="option in proxyCountryOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
        <el-select v-model="importCountry" filterable allow-create default-first-option placeholder="导入归类国家" style="width: 170px">
          <el-option v-for="option in proxyCountryOptions" :key="`import-${option.value}`" :label="option.label" :value="option.value" />
        </el-select>
        <el-input v-model="importGroup" maxlength="64" placeholder="导入分组名称" style="width: 180px" />
        <el-button
          type="danger"
          :icon="Delete"
          :loading="proxyMutation === 'all'"
          :disabled="store.proxyTotal === 0 || proxyMutationBusy"
          @click="clearProxyPool"
        >
          清空代理池
        </el-button>
        <el-tooltip content="等待 Playwright 执行器接入" placement="top">
          <span><el-button :icon="Refresh" disabled>检测全部</el-button></span>
        </el-tooltip>
        <el-button type="primary" :icon="DocumentAdd" @click="importOpen = true">导入代理</el-button>
      </div>
    </div>

    <div class="stats-grid proxy-stats">
      <StatCard label="代理总数" :value="store.stats.proxies.total" note="MongoDB 全部资源" :icon="Connection" />
      <StatCard label="已启用" :value="store.stats.proxies.enabled" note="可参与后续任务" :icon="Setting" tone="green" />
      <StatCard label="健康可用" :value="store.stats.proxies.available" note="最近检测通过" :icon="Refresh" tone="green" />
      <StatCard label="隔离数量" :value="store.stats.proxies.quarantined" note="暂不参与任务" :icon="Stopwatch" tone="amber" />
    </div>

    <div class="panel table-panel">
      <el-table
        v-loading="proxyLoading"
        :data="visibleProxyGroups"
        :row-key="proxyGroupKey"
        empty-text="暂无代理分组，请先选择国家和分组名称后导入"
      >
        <el-table-column label="启用" width="76">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled > 0"
              :loading="proxyMutation === 'selected'"
              @change="toggleProxyGroup(row, $event)"
            />
          </template>
        </el-table-column>
        <el-table-column label="国家" width="180">
          <template #default="{ row }">
            <el-select :model-value="row.country" size="small" filterable @change="changeProxyGroupCountry(row, String($event))">
              <el-option v-for="option in proxyCountryOptions" :key="`group-country-${option.value}`" :label="option.label" :value="option.value" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="代理分组" min-width="220">
          <template #default="{ row }">
            <strong>{{ row.group }}</strong>
            <el-button text type="primary" @click="renameProxyGroup(row)">重命名</el-button>
          </template>
        </el-table-column>
        <el-table-column label="协议" width="180">
          <template #default="{ row }">
            <el-tag v-for="scheme in row.schemes" :key="scheme" size="small" effect="plain" class="scheme-tag">{{ scheme.toUpperCase() }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="total" label="代理数量" width="110" />
        <el-table-column prop="enabled" label="已启用" width="100" />
        <el-table-column prop="available" label="健康可用" width="110" />
        <el-table-column prop="quarantined" label="隔离" width="90" />
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button text type="danger" :icon="Delete" aria-label="删除代理分组" @click="removeProxyGroup(row)" />
          </template>
        </el-table-column>
      </el-table>
      <div class="table-footer">
        <span>共 {{ visibleProxyGroups.length }} 个分组 · {{ store.stats.proxies.total }} 条代理 · 明细保存在 MongoDB</span>
      </div>
    </div>

    <ImportDialog
      v-model="importOpen"
      kind="proxy"
      :existing-keys="existingProxyKeys"
      :submit-handler="submitProxyImport"
      @imported="afterProxyImport"
    />
  </section>
</template>

<style scoped>
.settings-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 280px;
  gap: 16px;
  margin-bottom: 38px;
}

.execution-panel {
  padding: 22px;
}

.panel-heading {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
}

.panel-heading > div:nth-child(2) {
  flex: 1;
}

.panel-heading h3,
.panel-heading p {
  margin: 0;
}

.panel-heading h3 {
  font-size: 14px;
}

.panel-heading p {
  margin-top: 4px;
  color: var(--text-muted);
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 10px;
}

.panel-icon {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  border: 1px solid rgb(50 197 255 / 20%);
  border-radius: 10px;
  color: var(--accent);
  background: rgb(50 197 255 / 7%);
}

.config-alert {
  margin-bottom: 18px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}

.api-settings-card {
  padding: 16px 16px 14px;
  border: 1px solid rgb(50 197 255 / 18%);
  border-radius: 12px;
  background: rgb(50 197 255 / 4%);
}

.api-settings-title,
.headless-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.api-settings-title {
  margin-bottom: 12px;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 700;
}

.api-input-grid {
  display: grid;
  grid-template-columns: minmax(320px, 1.4fr) minmax(260px, 1fr) 150px;
  gap: 14px;
  align-items: start;
}

.api-input-grid :deep(.el-form-item) {
  margin-bottom: 14px;
}

.headless-row {
  padding-top: 2px;
}

.headless-row strong,
.headless-row small {
  display: block;
}

.headless-row strong {
  color: var(--text-secondary);
  font-size: 12px;
}

.headless-control {
  display: flex;
  align-items: center;
  gap: 10px;
}

.headless-mode-label {
  min-width: 28px;
  color: var(--accent);
  font-size: 12px;
  font-weight: 700;
  text-align: right;
}

.task-settings-grid {
  margin-top: 20px;
  padding-top: 18px;
  border-top: 1px solid rgb(255 255 255 / 7%);
}

.registration-setting-control {
  display: flex;
  min-height: 32px;
  align-items: center;
  gap: 10px;
}

.registration-setting-control strong {
  min-width: 42px;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 700;
}

.registration-setting-control strong.enabled {
  color: var(--success);
}

.el-input-number {
  width: 100%;
}

.el-form-item small {
  display: block;
  margin-top: 6px;
  color: var(--text-muted);
  font-size: 10px;
}

.heading-actions {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.heading-save-time {
  text-align: right;
}

.heading-save-time span,
.heading-save-time strong {
  display: block;
}

.heading-save-time span {
  color: var(--text-muted);
  font-size: 10px;
}

.heading-save-time strong {
  margin-top: 3px;
  color: var(--text-secondary);
  font-size: 11px;
}

.config-side {
  display: grid;
  grid-template-rows: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.proxy-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 18px;
}

.proxy-heading h2,
.proxy-heading p {
  margin: 0;
}

.proxy-heading h2 {
  font-size: 18px;
}

.proxy-heading p {
  margin-top: 6px;
  color: var(--text-secondary);
  font-size: 12px;
}

.proxy-stats {
  grid-template-columns: repeat(4, 1fr);
}

.proxy-host {
  color: #c7eafa;
}

@media (max-width: 1080px) {
  .settings-layout {
    grid-template-columns: 1fr;
  }

  .config-side {
    grid-template-columns: repeat(2, 1fr);
    grid-template-rows: 1fr;
  }
}

@media (max-width: 900px) {
  .api-input-grid {
    grid-template-columns: minmax(0, 1fr) 150px;
  }

  .browser-path-field {
    grid-column: 1 / -1;
  }
}

@media (max-width: 720px) {
  .heading-actions {
    width: 100%;
    justify-content: space-between;
  }

  .heading-save-time {
    text-align: left;
  }

  .form-grid,
  .api-input-grid,
  .config-side {
    grid-template-columns: 1fr;
  }

  .browser-path-field {
    grid-column: auto;
  }

  .config-side {
    grid-template-rows: repeat(2, minmax(0, 1fr));
  }

  .proxy-heading {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
