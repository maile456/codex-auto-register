<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { CopyDocument, Download, Lock } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { copyText, downloadTextFile } from '@/services/exporter'
import { dataGateway } from '@/services/dataGateway'
import type { ExportScope } from '@/types'

const props = defineProps<{
  modelValue: boolean
  scope: ExportScope
  ids: string[]
  count: number
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()

const delivery = ref<'download' | 'copy'>('download')
const exporting = ref(false)

watch(
  () => props.modelValue,
  (open) => {
    if (open) delivery.value = 'download'
  },
)

const scopeLabel = computed(() => {
  if (props.scope === 'single') return '单个邮箱'
  if (props.scope === 'selected') return '已选邮箱'
  return '全部邮箱'
})

async function confirmExport() {
  if (!props.count) return
  exporting.value = true
  try {
    const exportPayload = await dataGateway.exportEmails(props.scope, props.ids)
    if (delivery.value === 'download') {
      downloadTextFile(exportPayload.content, exportPayload.filename)
      ElMessage.success(`已生成 ${exportPayload.filename}`)
    } else {
      await copyText(exportPayload.content)
      ElMessage.success(`已复制 ${exportPayload.count} 条邮箱数据`)
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
    title="导出邮箱"
    width="min(560px, calc(100vw - 28px))"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="email-export-notice">
      <el-icon><Lock /></el-icon>
      <div>
        <strong>导出内容包含接码地址</strong>
        <p>每行格式为“邮箱----接码地址”，请确认保存位置与剪贴板环境安全。</p>
      </div>
    </div>

    <div class="email-export-section">
      <label>交付方式</label>
      <el-radio-group v-model="delivery">
        <el-radio-button value="download"><el-icon><Download /></el-icon>下载 TXT</el-radio-button>
        <el-radio-button value="copy"><el-icon><CopyDocument /></el-icon>复制文本</el-radio-button>
      </el-radio-group>
    </div>

    <dl class="email-export-summary">
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
.email-export-notice {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
  padding: 13px;
  border: 1px solid rgb(255 189 74 / 24%);
  border-radius: 10px;
  color: var(--warning);
  background: rgb(255 189 74 / 6%);
}

.email-export-notice .el-icon {
  flex: 0 0 auto;
  margin-top: 2px;
  font-size: 18px;
}

.email-export-notice strong {
  color: #f7dfb1;
  font-size: 12px;
}

.email-export-notice p {
  margin: 4px 0 0;
  color: #a99a7b;
  font-size: 11px;
}

.email-export-section {
  margin: 18px 0;
}

.email-export-section > label {
  display: block;
  margin-bottom: 9px;
  color: var(--text-secondary);
  font-size: 11px;
}

.email-export-summary {
  margin: 20px 0 0;
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
}

.email-export-summary div {
  display: grid;
  grid-template-columns: 90px 1fr;
  padding: 9px 12px;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 11px;
}

.email-export-summary div:last-child {
  border-bottom: 0;
}

.email-export-summary dt {
  color: var(--text-muted);
}

.email-export-summary dd {
  overflow: hidden;
  margin: 0;
  color: #d6e1ed;
  text-overflow: ellipsis;
}
</style>
