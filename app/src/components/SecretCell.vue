<script setup lang="ts">
import { computed, ref } from 'vue'
import { CopyDocument, Hide, View } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { copyText } from '@/services/exporter'

const props = withDefaults(
  defineProps<{
    value: string
    visibleChars?: number
    allowReveal?: boolean
    monospace?: boolean
  }>(),
  {
    visibleChars: 4,
    allowReveal: true,
    monospace: true,
  },
)

const revealed = ref(false)
const displayValue = computed(() => {
  if (revealed.value) return props.value
  const visible = props.value.slice(0, props.visibleChars)
  return `${visible}${'•'.repeat(Math.max(6, Math.min(12, props.value.length - props.visibleChars)))}`
})

async function handleCopy() {
  try {
    await copyText(props.value)
    ElMessage.success('已复制到剪贴板')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '复制失败')
  }
}
</script>

<template>
  <div class="secret-cell">
    <span :class="{ mono: monospace }" :title="revealed ? value : '敏感信息已遮罩'">
      {{ displayValue }}
    </span>
    <div class="secret-actions">
      <el-tooltip :content="revealed ? '隐藏' : '显示'">
        <el-button
          v-if="allowReveal"
          text
          circle
          size="small"
          :aria-label="revealed ? '隐藏敏感信息' : '显示敏感信息'"
          @click="revealed = !revealed"
        >
          <el-icon><component :is="revealed ? Hide : View" /></el-icon>
        </el-button>
      </el-tooltip>
      <el-tooltip content="复制">
        <el-button text circle size="small" aria-label="复制敏感信息" @click="handleCopy">
          <el-icon><CopyDocument /></el-icon>
        </el-button>
      </el-tooltip>
    </div>
  </div>
</template>

<style scoped>
.secret-cell {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 6px;
}

.secret-cell > span {
  overflow: hidden;
  max-width: 210px;
  color: #c9d4e3;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.secret-actions {
  display: flex;
  flex: 0 0 auto;
  opacity: 0.72;
}

.secret-cell:hover .secret-actions {
  opacity: 1;
}
</style>
