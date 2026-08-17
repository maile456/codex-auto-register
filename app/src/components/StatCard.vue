<script setup lang="ts">
import type { Component } from 'vue'

withDefaults(
  defineProps<{
    label: string
    value: string | number
    note: string
    icon?: Component
    tone?: 'blue' | 'green' | 'amber' | 'red'
  }>(),
  { tone: 'blue' },
)
</script>

<template>
  <article class="stat-card panel" :class="`stat-card--${tone}`">
    <div class="stat-card__topline">
      <span>{{ label }}</span>
      <el-icon v-if="icon"><component :is="icon" /></el-icon>
    </div>
    <strong>{{ value }}</strong>
    <p>{{ note }}</p>
  </article>
</template>

<style scoped>
.stat-card {
  position: relative;
  overflow: hidden;
  min-height: 126px;
  padding: 18px 19px;
}

.stat-card::after {
  position: absolute;
  top: -36px;
  right: -28px;
  width: 110px;
  height: 110px;
  border-radius: 50%;
  background: currentColor;
  content: '';
  filter: blur(48px);
  opacity: 0.11;
}

.stat-card--blue {
  color: var(--accent);
}

.stat-card--green {
  color: var(--success);
}

.stat-card--amber {
  color: var(--warning);
}

.stat-card--red {
  color: var(--danger);
}

.stat-card__topline {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--text-secondary);
  font-size: 12px;
}

.stat-card__topline .el-icon {
  color: currentColor;
  font-size: 18px;
}

.stat-card strong {
  display: block;
  margin-top: 12px;
  color: var(--text-primary);
  font-size: 28px;
  line-height: 1;
  letter-spacing: -0.04em;
}

.stat-card p {
  margin: 10px 0 0;
  color: var(--text-muted);
  font-size: 11px;
}
</style>
