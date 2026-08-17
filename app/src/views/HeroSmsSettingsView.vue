<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Connection, Message, Refresh, Setting } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import StatCard from '@/components/StatCard.vue'
import { dataGateway } from '@/services/dataGateway'
import type { HeroSmsCountry, HeroSmsSettings } from '@/types'

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const balance = ref<number | null>(null)
const countries = ref<HeroSmsCountry[]>([])
const settings = reactive<HeroSmsSettings>({
  enabled: false,
  countryId: 182,
  maxPrice: 1,
  changeNumberRetries: 2,
  numberWaitSeconds: 120,
  agreementAutoSmsEnabled: false,
  pipelineAutoPaymentEnabled: false,
  apiKeyConfigured: false,
  updatedAt: null,
})

const selectedCountry = computed(() =>
  countries.value.find((item) => item.id === settings.countryId)?.name || `国家 ID ${settings.countryId}`,
)

async function load() {
  loading.value = true
  try {
    const [current, available] = await Promise.all([
      dataGateway.heroSmsSettings(),
      dataGateway.heroSmsCountries(),
    ])
    Object.assign(settings, current)
    countries.value = available
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : 'HeroSMS 配置读取失败')
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const result = await dataGateway.updateHeroSmsSettings({
      enabled: settings.enabled,
      countryId: settings.countryId,
      maxPrice: settings.maxPrice,
      changeNumberRetries: settings.changeNumberRetries,
      numberWaitSeconds: settings.numberWaitSeconds,
      agreementAutoSmsEnabled: settings.agreementAutoSmsEnabled,
    })
    Object.assign(settings, result)
    ElMessage.success('HeroSMS 接码配置已保存')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : 'HeroSMS 配置保存失败')
  } finally {
    saving.value = false
  }
}

async function testConnection() {
  testing.value = true
  try {
    const result = await dataGateway.heroSmsTest()
    balance.value = result.balance
    ElMessage.success(`HeroSMS 连接成功，余额 ${result.balance.toFixed(4)}`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : 'HeroSMS 连接失败')
  } finally {
    testing.value = false
  }
}

function toggleEnabled(value: string | number | boolean) {
  settings.enabled = Boolean(value)
  if (!settings.enabled) settings.agreementAutoSmsEnabled = false
}

onMounted(() => void load())
</script>

<template>
  <section class="hero-page" v-loading="loading">
    <div class="page-heading hero-heading">
      <div>
        <h2>HeroSMS PP 接码配置</h2>
        <p>流水线与协议授权共用这一套国家、价格、换号和等待时间。</p>
      </div>
      <div class="heading-actions">
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新国家</el-button>
        <el-button type="primary" :icon="Setting" :loading="saving" @click="save">保存配置</el-button>
      </div>
    </div>

    <div class="stats-grid hero-stats">
      <StatCard label="API Key" :value="settings.apiKeyConfigured ? '已配置' : '未配置'" note="仅保存在后端环境" :icon="Connection" :tone="settings.apiKeyConfigured ? 'green' : 'amber'" />
      <StatCard label="接码国家" :value="selectedCountry" :note="`HeroSMS ID ${settings.countryId}`" :icon="Message" tone="green" />
      <StatCard label="当前状态" :value="settings.enabled ? '运行' : '暂停'" note="流水线与协议授权共享" :icon="Setting" :tone="settings.enabled ? 'green' : 'amber'" />
      <StatCard label="账户余额" :value="balance == null ? '未测试' : balance.toFixed(4)" note="点击测试连接刷新" :icon="Refresh" />
    </div>

    <article class="panel hero-config-panel">
      <div class="section-heading">
        <div class="section-icon"><el-icon><Message /></el-icon></div>
        <div><h3>PayPal 接码参数</h3><p>国家目录实时从 HeroSMS 获取，API Key 不下发到浏览器。</p></div>
      </div>
      <el-form label-position="top">
        <el-form-item label="启用 HeroSMS">
          <el-switch :model-value="settings.enabled" active-text="自动购买 PayPal 号码" @change="toggleEnabled" />
        </el-form-item>
        <el-form-item label="接码国家">
          <el-select v-model="settings.countryId" filterable placeholder="选择 HeroSMS 国家" style="width:100%">
            <el-option v-for="country in countries" :key="country.id" :label="`${country.name} · ${country.id}`" :value="country.id" />
          </el-select>
        </el-form-item>
        <div class="hero-number-grid">
          <el-form-item label="单号最高价格">
            <el-input-number v-model="settings.maxPrice" :min="0.01" :max="100" :step="0.05" :precision="4" controls-position="right" />
          </el-form-item>
          <el-form-item label="最多换号次数">
            <el-input-number v-model="settings.changeNumberRetries" :min="0" :max="10" :step="1" controls-position="right" />
          </el-form-item>
          <el-form-item label="单号等待秒数">
            <el-input-number v-model="settings.numberWaitSeconds" :min="30" :max="1200" :step="10" controls-position="right" />
          </el-form-item>
        </div>
        <el-alert
          type="info"
          :closable="false"
          :title="settings.pipelineAutoPaymentEnabled ? '流水线自动支付已开启，将使用这里的接码配置。' : '流水线自动支付当前关闭，可在流水线配置中开启。'"
        />
        <div class="hero-actions">
          <el-button :loading="testing" :disabled="!settings.apiKeyConfigured" @click="testConnection">测试连接</el-button>
          <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
        </div>
      </el-form>
    </article>
  </section>
</template>

<style scoped>
.hero-page{min-width:0}.hero-heading{align-items:center}.heading-actions,.hero-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.hero-stats{margin-bottom:14px}.hero-config-panel{max-width:820px;padding:20px}.hero-number-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.hero-number-grid .el-input-number{width:100%}.hero-actions{justify-content:flex-end;margin-top:18px}@media(max-width:760px){.hero-heading{align-items:flex-start}.hero-number-grid{grid-template-columns:1fr}}
</style>
