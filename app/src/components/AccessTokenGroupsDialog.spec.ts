import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it, vi } from 'vitest'
import { dataGateway } from '@/services/dataGateway'
import * as exporter from '@/services/exporter'
import AccessTokenGroupsDialog from './AccessTokenGroupsDialog.vue'

function exportPayload(tokens: string[]) {
  return {
    content: tokens.join('\n'),
    filename: `accounts-${tokens.length}-access-tokens.txt`,
    count: tokens.length,
    format: 'access-tokens' as const,
    skippedMissingCount: 2,
    skippedExpiredCount: 1,
  }
}

describe('AccessTokenGroupsDialog', () => {
  it('loads all valid ATs and copies each group in batches of ten', async () => {
    const tokens = Array.from({ length: 23 }, (_, index) => `AT_${String(index + 1).padStart(2, '0')}`)
    vi.spyOn(dataGateway, 'exportAccounts').mockResolvedValue(exportPayload(tokens))
    const copy = vi.spyOn(exporter, 'copyText').mockResolvedValue(undefined)

    const wrapper = mount(AccessTokenGroupsDialog, {
      props: { modelValue: true },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(dataGateway.exportAccounts).toHaveBeenCalledWith('access-tokens', 'all', [])
    expect(wrapper.text()).toContain('有效 AT：23 个 · 分成 3 组')
    expect(wrapper.text()).toContain('跳过缺失 2 个、过期 1 个')
    expect(wrapper.findAll('button').filter((button) => button.text().trim() === '复制本组')).toHaveLength(3)
    expect(wrapper.text()).not.toContain('AT_01')

    const groupButtons = wrapper
      .findAll('button')
      .filter((button) => button.text().trim() === '复制本组')
    await groupButtons[0]!.trigger('click')
    expect(copy).toHaveBeenLastCalledWith(tokens.slice(0, 10).join('\n'))

    await groupButtons[2]!.trigger('click')
    expect(copy).toHaveBeenLastCalledWith(tokens.slice(20).join('\n'))
  })

  it('shows an empty state when every account lacks a valid AT', async () => {
    vi.spyOn(dataGateway, 'exportAccounts').mockResolvedValue({
      ...exportPayload([]),
      skippedMissingCount: 4,
      skippedExpiredCount: 2,
    })

    const wrapper = mount(AccessTokenGroupsDialog, {
      props: { modelValue: true },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('没有可复制的有效 AT')
    expect(wrapper.text()).toContain('跳过缺失 4 个、过期 2 个')
    expect(wrapper.findAll('button').filter((button) => button.text().trim() === '复制本组')).toHaveLength(0)
  })
})
