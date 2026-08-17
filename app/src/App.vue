<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { CircleCheck, Collection, Connection, CreditCard, Message, Operation, Setting, Fold, Expand, Monitor, VideoPlay } from '@element-plus/icons-vue'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const store = useAppStore()
const collapsed = ref(false)
let healthTimer: ReturnType<typeof setInterval> | undefined

const pageTitle = computed(() => String(route.meta.title ?? '启动界面'))
const mongoStatusLabel = computed(() => {
  if (store.mongoHealth.status === 'online') return 'MongoDB 在线'
  if (store.mongoHealth.status === 'reconnecting') return 'MongoDB 正在重连'
  return 'MongoDB 不可用'
})
const mongoStatusClass = computed(() => {
  if (store.mongoHealth.status === 'online') return 'status-pill--online'
  if (store.mongoHealth.status === 'reconnecting') return 'status-pill--reconnecting'
  return 'status-pill--offline'
})

function syncSidebar() {
  collapsed.value = window.innerWidth < 920
}

onMounted(() => {
  syncSidebar()
  window.addEventListener('resize', syncSidebar)
  void store.bootstrap()
  healthTimer = setInterval(() => void store.refreshHealth(), 5000)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', syncSidebar)
  if (healthTimer) clearInterval(healthTimer)
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ collapsed }">
      <div class="brand">
        <div class="brand-mark">AR</div>
        <div v-if="!collapsed" class="brand-copy">
          <strong>AutoRegister</strong>
          <span>本机控制台</span>
        </div>
      </div>

      <el-menu class="nav-menu" router :default-active="route.path" :collapse="collapsed">
        <el-menu-item index="/launch">
          <el-icon><VideoPlay /></el-icon>
          <template #title>启动界面</template>
        </el-menu-item>
        <el-menu-item index="/settings">
          <el-icon><Setting /></el-icon>
          <template #title>配置栏</template>
        </el-menu-item>
        <el-menu-item index="/accounts">
          <el-icon><Collection /></el-icon>
          <template #title>账号池</template>
        </el-menu-item>
        <el-menu-item index="/emails">
          <el-icon><Message /></el-icon>
          <template #title>邮箱池</template>
        </el-menu-item>
        <el-menu-item index="/payment-tools">
          <el-icon><CreditCard /></el-icon>
          <template #title>提炼</template>
        </el-menu-item>
        <el-menu-item index="/pipeline">
          <el-icon><Operation /></el-icon>
          <template #title>自动流水线</template>
        </el-menu-item>
        <el-menu-item index="/hero-sms">
          <el-icon><Message /></el-icon>
          <template #title>HeroSMS 接码</template>
        </el-menu-item>
        <el-menu-item index="/paid-accounts">
          <el-icon><CircleCheck /></el-icon>
          <template #title>成品管理</template>
        </el-menu-item>
        <el-menu-item index="/agreement-tools">
          <el-icon><Connection /></el-icon>
          <template #title>协议授权</template>
        </el-menu-item>
      </el-menu>

      <div class="sidebar-footer">
        <button class="collapse-button" type="button" @click="collapsed = !collapsed">
          <el-icon><component :is="collapsed ? Expand : Fold" /></el-icon>
          <span v-if="!collapsed">收起导航</span>
        </button>
      </div>
    </aside>

    <div class="workspace">
      <header class="topbar">
        <div>
          <p class="eyebrow">AUTOREGISTER / LOCAL</p>
          <h1>{{ pageTitle }}</h1>
        </div>
        <div class="service-status">
          <el-tooltip
            :content="store.mongoHealth.error || `数据库：${store.mongoHealth.database}`"
            placement="bottom"
          >
            <span class="status-pill" :class="mongoStatusClass"><i />{{ mongoStatusLabel }}</span>
          </el-tooltip>
          <span
            class="status-pill"
            :class="store.configServiceOnline ? 'status-pill--online' : 'status-pill--offline'"
          >
            <i />配置服务{{ store.configServiceOnline ? '在线' : '离线' }}
          </span>
          <span class="mode-badge"><el-icon><Monitor /></el-icon>本机模式</span>
        </div>
      </header>

      <main class="content-area">
        <router-view />
      </main>
    </div>
  </div>
</template>
