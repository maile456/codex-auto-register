<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Connection, Refresh, TopRight, Setting } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { dataGateway } from '@/services/dataGateway'
import type { HeroSmsSettings, PaypalAgreementServiceState } from '@/types'

const serviceState = ref<PaypalAgreementServiceState | null>(null)
const loading = ref(false)
const frameLoaded = ref(false)
const errorMessage = ref('')
const frameKey = ref(0)
const heroSettings = ref<HeroSmsSettings | null>(null)
const heroSaving = ref(false)
let statusTimer: ReturnType<typeof setInterval> | undefined

const online = computed(() => serviceState.value?.status === 'online')
const frameUrl = computed(() => {
  const base = serviceState.value?.uiPath || '/paypal-pay/'
  return heroSettings.value?.agreementAutoSmsEnabled
    ? `${base}${base.includes('?') ? '&' : '?'}auto_sms=1`
    : base
})
const statusLabel = computed(() => {
  const state = serviceState.value?.status
  if (state === 'online') return '协议服务在线'
  if (state === 'starting') return '协议服务启动中'
  if (state === 'conflict') return '端口被占用'
  if (state === 'failed') return '协议服务异常'
  return '协议服务未启动'
})

async function startService(showMessage = false) {
  loading.value = true
  errorMessage.value = ''
  frameLoaded.value = false
  try {
    serviceState.value = await dataGateway.startPaypalAgreement()
    if (serviceState.value.status === 'online') {
      frameKey.value += 1
      if (showMessage) ElMessage.success('协议授权服务已就绪')
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '协议授权服务启动失败'
  } finally {
    loading.value = false
  }
}

async function refreshStatus() {
  try {
    const next = await dataGateway.paypalAgreementStatus()
    const becameOnline = next.status === 'online' && serviceState.value?.status !== 'online'
    serviceState.value = next
    if (becameOnline) frameKey.value += 1
  } catch {
    // The explicit retry button reports actionable errors; polling stays quiet.
  }
}

async function loadHeroSettings() {
  try {
    heroSettings.value = await dataGateway.heroSmsSettings()
  } catch {
    // The dedicated HeroSMS page reports the detailed configuration error.
  }
}

async function toggleAgreementSms(value: string | number | boolean) {
  if (!heroSettings.value) return
  heroSaving.value = true
  try {
    heroSettings.value = await dataGateway.updateHeroSmsSettings({
      enabled: heroSettings.value.enabled,
      countryId: heroSettings.value.countryId,
      maxPrice: heroSettings.value.maxPrice,
      changeNumberRetries: heroSettings.value.changeNumberRetries,
      numberWaitSeconds: heroSettings.value.numberWaitSeconds,
      agreementAutoSmsEnabled: Boolean(value),
    })
    frameLoaded.value = false
    frameKey.value += 1
    ElMessage.success(Boolean(value) ? '协议授权自动接码已开启' : '协议授权自动接码已关闭')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '协议自动接码配置失败')
  } finally {
    heroSaving.value = false
  }
}

function openStandalone() {
  window.open(frameUrl.value, '_blank', 'noopener,noreferrer')
}

onMounted(() => {
  void startService()
  void loadHeroSettings()
  statusTimer = setInterval(() => void refreshStatus(), 5000)
})

onBeforeUnmount(() => {
  if (statusTimer) clearInterval(statusTimer)
})
</script>

<template>
  <section class="agreement-page">
    <div class="page-heading agreement-heading">
      <div>
        <h2>协议授权</h2>
        <p>独立协议任务、代理池、短信验证码与临时浏览器验证。</p>
      </div>
      <div class="heading-actions">
        <span class="agreement-status" :class="{ online }"><i />{{ statusLabel }}</span>
        <el-button :icon="Refresh" :loading="loading" @click="startService(true)">重试服务</el-button>
        <el-button type="primary" plain :icon="TopRight" :disabled="!online" @click="openStandalone">
          新窗口打开
        </el-button>
      </div>
    </div>

    <div class="integration-note">
      <el-icon><Connection /></el-icon>
      <div>
        <strong>隔离运行，不占用注册与现有提链任务</strong>
        <p>协议核心运行在独立本机进程，通过同源代理嵌入；原账号、代理池和提炼路由保持不变。</p>
      </div>
    </div>

    <div class="agreement-sms-config">
      <div>
        <strong>协议授权自动 PP 接码</strong>
        <p v-if="heroSettings">
          {{ heroSettings.enabled ? 'HeroSMS 已启用' : '请先在 HeroSMS 页面启用' }} · 国家 ID {{ heroSettings.countryId }} · 最高价格 {{ heroSettings.maxPrice }}
        </p>
        <p v-else>正在读取 HeroSMS 配置…</p>
      </div>
      <div class="agreement-sms-actions">
        <el-button text :icon="Setting" @click="$router.push('/hero-sms')">接码配置</el-button>
        <el-switch
          v-if="heroSettings"
          :model-value="heroSettings.agreementAutoSmsEnabled"
          :disabled="!heroSettings.enabled || heroSaving"
          active-text="自动接码"
          @change="toggleAgreementSms"
        />
      </div>
    </div>

    <div v-if="loading && !serviceState" class="panel service-placeholder">
      <el-skeleton :rows="6" animated />
    </div>
    <div v-else-if="errorMessage || !online" class="panel service-error">
      <el-result icon="warning" title="协议服务尚未就绪" :sub-title="errorMessage || serviceState?.error || '点击重试服务继续'">
        <template #extra>
          <el-button type="primary" :loading="loading" @click="startService(true)">重新启动</el-button>
        </template>
      </el-result>
    </div>
    <div v-else class="agreement-frame-shell" :class="{ loaded: frameLoaded }">
      <div v-if="!frameLoaded" class="frame-loading"><el-icon class="is-loading"><Refresh /></el-icon>正在加载协议工作台…</div>
      <iframe
        :key="frameKey"
        :src="frameUrl"
        title="协议授权工作台"
        referrerpolicy="no-referrer"
        @load="frameLoaded = true"
      />
    </div>
  </section>
</template>

<style scoped>
.agreement-page {
  min-width: 0;
}

.agreement-heading {
  align-items: center;
}

.heading-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.agreement-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 30px;
  padding: 0 11px;
  border: 1px solid rgb(255 189 74 / 22%);
  border-radius: 999px;
  color: var(--warning);
  background: rgb(255 189 74 / 7%);
  font-size: 10px;
  font-weight: 700;
}

.agreement-status i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentcolor;
  box-shadow: 0 0 9px currentcolor;
}

.agreement-status.online {
  border-color: rgb(62 211 156 / 22%);
  color: var(--success);
  background: rgb(62 211 156 / 7%);
}

.integration-note {
  display: flex;
  gap: 12px;
  margin-bottom: 14px;
  padding: 12px 14px;
  border: 1px solid rgb(50 197 255 / 18%);
  border-radius: 12px;
  background: rgb(50 197 255 / 5%);
}

.integration-note .el-icon {
  flex: 0 0 auto;
  margin-top: 2px;
  color: var(--primary);
  font-size: 18px;
}

.integration-note strong {
  color: #dcebf7;
  font-size: 12px;
}

.integration-note p {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 10px;
}

.agreement-sms-config {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
  padding: 12px 14px;
  border: 1px solid rgb(169 112 255 / 25%);
  border-radius: 12px;
  background: rgb(112 64 190 / 8%);
}

.agreement-sms-config strong { color: #eadfff; font-size: 12px; }
.agreement-sms-config p { margin: 4px 0 0; color: var(--text-muted); font-size: 10px; }
.agreement-sms-actions { display: flex; align-items: center; gap: 12px; }

.service-placeholder,
.service-error {
  min-height: 520px;
  padding: 28px;
}

.agreement-frame-shell {
  position: relative;
  min-height: calc(100vh - 238px);
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  background: #080d14;
  box-shadow: var(--shadow-panel);
}

.agreement-frame-shell iframe {
  display: block;
  width: 100%;
  height: calc(100vh - 238px);
  min-height: 720px;
  border: 0;
  opacity: 0;
  transition: opacity 160ms ease;
}

.agreement-frame-shell.loaded iframe {
  opacity: 1;
}

.frame-loading {
  position: absolute;
  inset: 0;
  display: grid;
  place-content: center;
  gap: 10px;
  color: var(--text-muted);
  font-size: 11px;
}

@media (max-width: 760px) {
  .agreement-heading {
    align-items: flex-start;
  }

  .heading-actions {
    justify-content: flex-start;
  }

  .agreement-frame-shell,
  .agreement-frame-shell iframe {
    min-height: 900px;
    height: 900px;
  }

  .agreement-sms-config { align-items: flex-start; flex-direction: column; }
}
</style>
