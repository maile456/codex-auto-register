import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it, vi } from 'vitest'
import { dataGateway } from '@/services/dataGateway'
import type { HeroSmsSettings, PaypalAgreementServiceState } from '@/types'
import AgreementToolsView from './AgreementToolsView.vue'

function onlineState(): PaypalAgreementServiceState {
  return {
    ok: true,
    status: 'online',
    service: 'paypal-agreement-protocol',
    sourceCommit: 'fixture-commit',
    host: '127.0.0.1',
    port: 18098,
    uiPath: '/paypal-pay/',
    uiUrl: '/paypal-pay/',
    managed: true,
    pid: 1234,
  }
}

function heroSettings(overrides: Partial<HeroSmsSettings> = {}): HeroSmsSettings {
  return {
    enabled: true,
    countryId: 182,
    maxPrice: 0.5,
    changeNumberRetries: 2,
    numberWaitSeconds: 120,
    agreementAutoSmsEnabled: false,
    pipelineAutoPaymentEnabled: false,
    apiKeyConfigured: true,
    updatedAt: null,
    ...overrides,
  }
}

describe('AgreementToolsView', () => {
  it('starts the isolated service and embeds the same-origin workbench', async () => {
    const start = vi.spyOn(dataGateway, 'startPaypalAgreement').mockResolvedValue(onlineState())
    vi.spyOn(dataGateway, 'paypalAgreementStatus').mockResolvedValue(onlineState())
    vi.spyOn(dataGateway, 'heroSmsSettings').mockResolvedValue(heroSettings())

    const wrapper = mount(AgreementToolsView, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(start).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('协议服务在线')
    expect(wrapper.text()).toContain('隔离运行，不占用注册与现有提链任务')
    expect(wrapper.text()).toContain('协议授权自动 PP 接码')
    expect(wrapper.get('iframe[title="协议授权工作台"]').attributes('src')).toBe('/paypal-pay/')
    wrapper.unmount()
  })

  it('keeps the retry action visible when startup fails', async () => {
    vi.spyOn(dataGateway, 'startPaypalAgreement').mockRejectedValue(new Error('fixture startup failed'))
    vi.spyOn(dataGateway, 'heroSmsSettings').mockResolvedValue(heroSettings())

    const wrapper = mount(AgreementToolsView, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('协议服务尚未就绪')
    expect(wrapper.text()).toContain('fixture startup failed')
    expect(wrapper.find('iframe').exists()).toBe(false)
    wrapper.unmount()
  })

  it('saves the agreement auto-sms switch through shared HeroSMS settings', async () => {
    vi.spyOn(dataGateway, 'startPaypalAgreement').mockResolvedValue(onlineState())
    vi.spyOn(dataGateway, 'paypalAgreementStatus').mockResolvedValue(onlineState())
    vi.spyOn(dataGateway, 'heroSmsSettings').mockResolvedValue(heroSettings())
    const update = vi.spyOn(dataGateway, 'updateHeroSmsSettings').mockResolvedValue(
      heroSettings({ agreementAutoSmsEnabled: true }),
    )
    const wrapper = mount(AgreementToolsView, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    const agreementSwitch = wrapper.findAllComponents({ name: 'ElSwitch' })[0]
    agreementSwitch!.vm.$emit('change', true)
    await flushPromises()

    expect(update).toHaveBeenCalledWith(expect.objectContaining({
      countryId: 182,
      agreementAutoSmsEnabled: true,
    }))
    expect(wrapper.get('iframe[title="协议授权工作台"]').attributes('src')).toBe('/paypal-pay/?auto_sms=1')
    wrapper.unmount()
  })
})
