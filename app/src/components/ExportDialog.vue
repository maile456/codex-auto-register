<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { CopyDocument, Download, Lock } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { copyText, downloadTextFile } from '@/services/exporter'
import { dataGateway } from '@/services/dataGateway'
import type { ExportFormat, ExportScope } from '@/types'

const props = defineProps<{
  modelValue: boolean
  scope: ExportScope
  ids: string[]
  count: number
  initialFormat?: ExportFormat
  formatLocked?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()

const format = ref<ExportFormat>('credentials')
const delivery = ref<'download' | 'copy'>('download')
const exporting = ref(false)

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      format.value = props.initialFormat ?? 'credentials'
      delivery.value = 'download'
    }
  },
  { immediate: true },
)

const scopeLabel = computed(() => {
  if (props.scope === 'single') return '单个账号'
  if (props.scope === 'selected') return '已选账号'
  return '全部账号'
})

async function confirmExport() {
  if (!props.count) return
  exporting.value = true
  try {
    const exportPayload = await dataGateway.exportAccounts(format.value, props.scope, props.ids)
    if (exportPayload.count === 0) {
      ElMessage.warning(
        `没有可导出的有效 AT：缺失 ${exportPayload.skippedMissingCount} 条，过期 ${exportPayload.skippedExpiredCount} 条`,
      )
      return
    }
    if (delivery.value === 'download') {
      downloadTextFile(exportPayload.content, exportPayload.filename)
      ElMessage.success(`已生成 ${exportPayload.filename}`)
    } else {
      await copyText(exportPayload.content)
      ElMessage.success(`已复制 ${exportPayload.count} 条账号数据`)
    }
    if (exportPayload.skippedMissingCount || exportPayload.skippedExpiredCount) {
      ElMessage.warning(
        `已跳过缺失 ${exportPayload.skippedMissingCount} 条、过期 ${exportPayload.skippedExpiredCount} 条`,
      )
    }
    emit('update:modelValue', false)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '导出失败')
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="导出账号"
    width="min(580px, calc(100vw - 28px))"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="security-notice">
      <el-icon><Lock /></el-icon>
      <div>
        <strong>导出内容包含敏感凭据</strong>
        <p>请确认保存位置与剪贴板环境安全。页面不会把导出内容写入浏览器存储。</p>
      </div>
    </div>

    <div class="export-section">
      <label>数据格式</label>
      <el-radio-group v-model="format" class="format-options" :disabled="formatLocked">
        <el-radio value="credentials" border>
          <strong>凭据格式</strong>
          <span>邮箱----密码----TOTP</span>
        </el-radio>
        <el-radio value="mail-links" border>
          <strong>接码格式</strong>
          <span>邮箱----接码地址</span>
        </el-radio>
        <el-radio value="access-tokens" border>
          <strong>AccessToken</strong>
          <span>每行仅一个完整 AT</span>
        </el-radio>
      </el-radio-group>
    </div>

    <div class="export-section">
      <label>交付方式</label>
      <el-radio-group v-model="delivery">
        <el-radio-button value="download"><el-icon><Download /></el-icon>下载 TXT</el-radio-button>
        <el-radio-button value="copy"><el-icon><CopyDocument /></el-icon>复制文本</el-radio-button>
      </el-radio-group>
    </div>

    <dl class="export-summary">
      <div><dt>范围</dt><dd>{{ scopeLabel }}</dd></div>
      <div><dt>数量</dt><dd>{{ count }} 条</dd></div>
      <div><dt>编码</dt><dd>UTF-8 / 无表头</dd></div>
      <div><dt>文件名</dt><dd class="mono">由服务端生成（包含实际数量与时间）</dd></div>
    </dl>

    <template #footer>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :loading="exporting" :disabled="count === 0" @click="confirmExport">
        {{ delivery === 'download' ? '确认下载' : '确认复制' }} {{ count }} 条
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.security-notice {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
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

.export-section {
  margin: 18px 0;
}

.export-section > label {
  display: block;
  margin-bottom: 9px;
  color: var(--text-secondary);
  font-size: 11px;
}

.format-options {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.format-options .el-radio {
  width: 100%;
  height: auto;
  min-height: 72px;
  margin-right: 0;
  padding: 12px;
}

.format-options strong,
.format-options span {
  display: block;
}

.format-options span {
  margin-top: 5px;
  color: var(--text-muted);
  font-size: 10px;
}

.export-summary {
  margin: 20px 0 0;
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
}

.export-summary div {
  display: grid;
  grid-template-columns: 90px 1fr;
  padding: 9px 12px;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 11px;
}

.export-summary div:last-child {
  border-bottom: 0;
}

.export-summary dt {
  color: var(--text-muted);
}

.export-summary dd {
  overflow: hidden;
  margin: 0;
  color: #d6e1ed;
  text-overflow: ellipsis;
}

@media (max-width: 520px) {
  .format-options {
    grid-template-columns: 1fr;
  }
}
</style>
