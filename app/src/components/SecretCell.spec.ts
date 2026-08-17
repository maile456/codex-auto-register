import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it, vi } from 'vitest'
import SecretCell from './SecretCell.vue'

describe('SecretCell', () => {
  it('masks the secret by default and reveals only after explicit click', async () => {
    const wrapper = mount(SecretCell, {
      props: { value: 'VerySecretValue123' },
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.text()).not.toContain('VerySecretValue123')
    await wrapper.get('[aria-label="显示敏感信息"]').trigger('click')
    expect(wrapper.text()).toContain('VerySecretValue123')
    await wrapper.get('[aria-label="隐藏敏感信息"]').trigger('click')
    expect(wrapper.text()).not.toContain('VerySecretValue123')
  })

  it('copies the raw secret without writing browser storage', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })
    const localStorageSpy = vi.spyOn(Storage.prototype, 'setItem')
    const wrapper = mount(SecretCell, {
      props: { value: 'copy-me' },
      global: { plugins: [ElementPlus] },
    })

    await wrapper.get('[aria-label="复制敏感信息"]').trigger('click')

    expect(writeText).toHaveBeenCalledWith('copy-me')
    expect(localStorageSpy).not.toHaveBeenCalled()
  })
})
