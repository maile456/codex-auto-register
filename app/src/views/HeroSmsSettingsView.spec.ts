import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { dataGateway } from '@/services/dataGateway'
import HeroSmsSettingsView from './HeroSmsSettingsView.vue'

vi.mock('@/services/dataGateway', () => ({
  dataGateway: {
    heroSmsSettings: vi.fn(),
    heroSmsCountries: vi.fn(),
    updateHeroSmsSettings: vi.fn(),
    heroSmsTest: vi.fn(),
  },
}))

const current = {
  enabled: true,
  countryId: 182,
  maxPrice: 0.5,
  changeNumberRetries: 2,
  numberWaitSeconds: 120,
  agreementAutoSmsEnabled: false,
  pipelineAutoPaymentEnabled: true,
  apiKeyConfigured: true,
  updatedAt: null,
}

describe('HeroSmsSettingsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(dataGateway.heroSmsSettings).mockResolvedValue(current)
    vi.mocked(dataGateway.heroSmsCountries).mockResolvedValue([
      { id: 182, name: 'Japan' },
      { id: 62, name: 'Turkey' },
    ])
    vi.mocked(dataGateway.updateHeroSmsSettings).mockResolvedValue(current)
    vi.mocked(dataGateway.heroSmsTest).mockResolvedValue({
      ok: true,
      configured: true,
      countryId: 182,
      service: 'PayPal',
      balance: 12.34,
    })
  })

  it('loads dynamic countries and saves the shared auto-sms settings', async () => {
    const wrapper = mount(HeroSmsSettingsView, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('HeroSMS PP 接码配置')
    expect(wrapper.text()).toContain('Japan')
    expect(wrapper.text()).toContain('流水线自动支付已开启')
    const countrySelect = wrapper.findAllComponents({ name: 'ElSelect' })[0]
    countrySelect!.vm.$emit('update:modelValue', 62)
    const saveButton = wrapper.findAll('button').find((button) => button.text().includes('保存配置'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(dataGateway.updateHeroSmsSettings).toHaveBeenCalledWith(
      expect.objectContaining({ countryId: 62, enabled: true }),
    )
    wrapper.unmount()
  })
})
