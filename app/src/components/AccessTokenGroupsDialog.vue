<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { CopyDocument, Lock, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { dataGateway } from '@/services/dataGateway'
import { copyText } from '@/services/exporter'

const GROUP_SIZE = 10

interface TokenGroup {
  index: number
  tokens: string[]
}

const props = defineProps<{
  modelValue: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()

const loading = ref(false)
const groups = ref<TokenGroup[]>([])
const totalValid = ref(0)
const skippedMissing = ref(0)
const skippedExpired = ref(0)
const errorMessage = ref('')
const copiedGroup = ref<number | null>(null)
let copiedResetTimer: ReturnType<typeof setTimeout> | undefined
let loadRequestId = 0

const groupCount = computed(() => groups.value.length)

function clearCopiedState() {
  if (copiedResetTimer) clearTimeout(copiedResetTimer)
  copiedResetTimer = undefined
  copiedGroup.value = null
}

function clearSensitiveState() {
  loadRequestId += 1
  clearCopiedState()
  groups.value = []
  totalValid.value = 0
  skippedMissing.value = 0
  skippedExpired.value = 0
  errorMessage.value = ''
  loading.value = false
}

function buildGroups(tokens: string[]) {
  const next: TokenGroup[] = []
  for (let offset = 0; offset < tokens.length; offset += GROUP_SIZE) {
    next.push({
      index: next.length,
      tokens: tokens.slice(offset, offset + GROUP_SIZE),
    })
  }
  return next
}

async function loadGroups() {
  if (!props.modelValue) return
  const requestId = ++loadRequestId
  loading.value = true
  groups.value = []
  totalValid.value = 0
  skippedMissing.value = 0
  skippedExpired.value = 0
  errorMessage.value = ''
  clearCopiedState()
  try {
    const payload = await dataGateway.exportAccounts('access-tokens', 'all', [])
    if (requestId !== loadRequestId || !props.modelValue) return
    const tokens = payload.content
      ? payload.content.split(/\r?\n/).filter((token) => token.length > 0)
      : []
    groups.value = buildGroups(tokens)
    totalValid.value = tokens.length
    skippedMissing.value = payload.skippedMissingCount
    skippedExpired.value = payload.skippedExpiredCount
  } catch (error) {
    if (requestId !== loadRequestId || !props.modelValue) return
    groups.value = []
    totalValid.value = 0
    errorMessage.value = error instanceof Error ? error.message : '读取 Access Token 失败'
  } finally {
    if (requestId === loadRequestId) loading.value = false
  }
}

async function copyGroup(group: TokenGroup) {
  try {
    await copyText(group.tokens.join('\n'))
    copiedGroup.value = group.index
    if (copiedResetTimer) clearTimeout(copiedResetTimer)
    copiedResetTimer = setTimeout(() => {
      copiedGroup.value = null
      copiedResetTimer = undefined
    }, 1800)
    ElMessage.success(`已复制第 ${group.index + 1} 组（${group.tokens.length} 个 AT）`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '复制失败')
  }
}

function close() {
  emit('update:modelValue', false)
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) void loadGroups()
    else clearSensitiveState()
  },
  { immediate: true },
)

onBeforeUnmount(clearSensitiveState)
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="Access Token 分组复制"
    width="min(640px, calc(100vw - 28px))"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="security-notice">
      <el-icon><Lock /></el-icon>
      <div>
        <strong>只在点击复制时写入剪贴板</strong>
        <p>页面不展示明文 AT；每组最多 10 个，按账号池最新创建顺序排列。</p>
      </div>
    </div>

    <div v-if="!loading && !errorMessage" class="group-summary">
      <span>有效 AT：{{ totalValid }} 个 · 分成 {{ groupCount }} 组</span>
      <span v-if="skippedMissing || skippedExpired" class="muted">
        跳过缺失 {{ skippedMissing }} 个、过期 {{ skippedExpired }} 个
      </span>
    </div>

    <el-skeleton v-if="loading" :rows="4" animated />
    <el-alert
      v-else-if="errorMessage"
      :title="errorMessage"
      type="error"
      :closable="false"
      show-icon
    />
    <el-empty v-else-if="!groups.length" description="没有可复制的有效 AT" />
    <div v-else class="token-groups">
      <div v-for="group in groups" :key="group.index" class="token-group">
        <div>
          <strong>第 {{ group.index + 1 }} 组</strong>
          <span>第 {{ group.index * GROUP_SIZE + 1 }}–{{ group.index * GROUP_SIZE + group.tokens.length }} 个 · {{ group.tokens.length }} 个 AT</span>
        </div>
        <el-button
          type="primary"
          plain
          size="small"
          :icon="CopyDocument"
          @click="copyGroup(group)"
        >
          {{ copiedGroup === group.index ? '已复制' : '复制本组' }}
        </el-button>
      </div>
    </div>

    <template #footer>
      <el-button :icon="Refresh" :loading="loading" @click="loadGroups">刷新分组</el-button>
      <el-button @click="close">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.security-notice {
  display: flex;
  gap: 12px;
  margin-bottom: 18px;
  padding: 13px;
  border: 1px solid rgb(255 189 74 / 24%);
  border-radius: 10px;
  color: var(--warning);
  background: rgb(255 189 74 / 6%);
}

.security-notice .el-icon {
  flex: 0 0 auto;
  margin-top: 2px;
  font-size: 18px;
}

.security-notice strong {
  color: #f7dfb1;
  font-size: 12px;
}

.security-notice p {
  margin: 4px 0 0;
  color: #a99a7b;
  font-size: 11px;
}

.group-summary {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 12px;
  color: var(--text-secondary);
  font-size: 11px;
}

.token-groups {
  display: grid;
  gap: 8px;
  max-height: min(52vh, 480px);
  overflow-y: auto;
  padding-right: 3px;
}

.token-group {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  background: rgb(255 255 255 / 2%);
}

.token-group strong,
.token-group span {
  display: block;
}

.token-group strong {
  color: #e3ebf5;
  font-size: 12px;
}

.token-group span {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 10px;
}
</style>
