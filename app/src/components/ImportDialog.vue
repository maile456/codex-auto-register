<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { DocumentAdd, UploadFilled } from '@element-plus/icons-vue'
import type { UploadFile } from 'element-plus'
import { ElMessage } from 'element-plus'
import { parseEmailImport, parseProxyImport } from '@/services/parsers'
import type { ImportResult } from '@/types'

const props = defineProps<{
  modelValue: boolean
  kind: 'email' | 'proxy'
  existingKeys: string[]
  submitHandler: (rawText: string) => Promise<ImportResult>
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  imported: [result: ImportResult]
}>()

const rawText = ref('')
const submitting = ref(false)

const isEmail = computed(() => props.kind === 'email')
const title = computed(() => (isEmail.value ? '导入邮箱' : '导入代理'))
const formatHint = computed(() =>
  isEmail.value ? '邮箱----接码地址 或 邮箱----mail.com 密码' : 'socks5://用户名:密码@主机:端口 或 host:port:username:password',
)
const placeholder = computed(() =>
  isEmail.value
    ? 'demo@example.com----https://example.invalid/inbox/demo\nuser@gardener.com----mail.com-password'
    : 'socks5://demo-user:demo-password@proxy.example.com:10000',
)
const preview = computed(() =>
  isEmail.value
    ? parseEmailImport(rawText.value, props.existingKeys)
    : parseProxyImport(rawText.value, props.existingKeys),
)

watch(
  () => props.modelValue,
  (open) => {
    if (open) rawText.value = ''
  },
)

async function readUpload(file: UploadFile) {
  if (!file.raw) return
  if (file.raw.size > 2 * 1024 * 1024) {
    ElMessage.warning('TXT 文件不能超过 2 MB')
    return
  }
  rawText.value = await file.raw.text()
}

async function submit() {
  if (!preview.value.accepted.length || submitting.value) return
  submitting.value = true
  try {
    const result = await props.submitHandler(rawText.value)
    emit('imported', result)
    emit('update:modelValue', false)
    ElMessage.success(`成功导入 ${result.imported} 条数据`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '导入失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="min(720px, calc(100vw - 28px))"
    destroy-on-close
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="import-intro">
      <el-icon><DocumentAdd /></el-icon>
      <div>
        <strong>一行一条，格式：<code>{{ formatHint }}</code></strong>
        <p>支持直接粘贴或选择 UTF-8 TXT；重复行将跳过，其他有效行继续导入。</p>
      </div>
    </div>

    <el-input
      v-model="rawText"
      type="textarea"
      :rows="10"
      resize="vertical"
      :placeholder="placeholder"
      aria-label="批量导入文本"
    />

    <div class="import-controls">
      <el-upload
        accept=".txt,text/plain"
        :auto-upload="false"
        :show-file-list="false"
        :on-change="readUpload"
      >
        <el-button :icon="UploadFilled">选择 TXT 文件</el-button>
      </el-upload>
      <span>空行、BOM 与两端空格会被自动清理</span>
    </div>

    <div v-if="preview.total" class="import-preview">
      <div class="preview-count preview-count--ok">
        <strong>{{ preview.accepted.length }}</strong><span>有效</span>
      </div>
      <div class="preview-count preview-count--duplicate">
        <strong>{{ preview.duplicates.length }}</strong><span>重复</span>
      </div>
      <div class="preview-count preview-count--error">
        <strong>{{ preview.errors.length }}</strong><span>错误</span>
      </div>
      <div class="preview-count">
        <strong>{{ preview.total }}</strong><span>总行数</span>
      </div>
    </div>

    <div v-if="preview.duplicates.length || preview.errors.length" class="issue-list">
      <div v-for="issue in [...preview.errors, ...preview.duplicates].slice(0, 6)" :key="`${issue.line}-${issue.reason}`">
        <span>第 {{ issue.line }} 行</span>
        <strong>{{ issue.reason }}</strong>
        <code>{{ issue.preview }}</code>
      </div>
      <p v-if="preview.errors.length + preview.duplicates.length > 6">
        另有 {{ preview.errors.length + preview.duplicates.length - 6 }} 条未展开
      </p>
    </div>

    <template #footer>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button
        type="primary"
        :disabled="preview.accepted.length === 0"
        :loading="submitting"
        @click="submit"
      >
        确认导入 {{ preview.accepted.length }} 条
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.import-intro {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 16px;
  padding: 13px 14px;
  border: 1px solid rgb(50 197 255 / 18%);
  border-radius: 10px;
  background: rgb(50 197 255 / 5%);
}

.import-intro .el-icon {
  margin-top: 2px;
  color: var(--accent);
  font-size: 20px;
}

.import-intro strong {
  color: #dce9f5;
  font-size: 12px;
}

.import-intro p {
  margin: 5px 0 0;
  color: var(--text-muted);
  font-size: 11px;
}

.import-intro code {
  color: #aeeaff;
}

.import-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 12px;
  color: var(--text-muted);
  font-size: 11px;
}

.import-preview {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin-top: 18px;
}

.preview-count {
  padding: 11px;
  border: 1px solid var(--border-subtle);
  border-radius: 9px;
  text-align: center;
  background: #0c121c;
}

.preview-count strong,
.preview-count span {
  display: block;
}

.preview-count strong {
  font-size: 18px;
}

.preview-count span {
  margin-top: 3px;
  color: var(--text-muted);
  font-size: 10px;
}

.preview-count--ok strong {
  color: var(--success);
}

.preview-count--duplicate strong {
  color: var(--warning);
}

.preview-count--error strong {
  color: var(--danger);
}

.issue-list {
  max-height: 170px;
  overflow: auto;
  margin-top: 12px;
  border: 1px solid var(--border-subtle);
  border-radius: 9px;
}

.issue-list > div {
  display: grid;
  grid-template-columns: 64px minmax(150px, 1fr) minmax(130px, 1fr);
  gap: 10px;
  padding: 9px 11px;
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-muted);
  font-size: 10px;
}

.issue-list strong {
  color: #d2dce9;
  font-weight: 500;
}

.issue-list code {
  overflow: hidden;
  color: var(--text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.issue-list > p {
  margin: 0;
  padding: 9px 11px;
  color: var(--text-muted);
  font-size: 10px;
}

@media (max-width: 560px) {
  .import-preview {
    grid-template-columns: repeat(2, 1fr);
  }

  .import-controls {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
