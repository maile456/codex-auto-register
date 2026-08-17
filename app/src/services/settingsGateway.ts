import type { ExecutionSettings, ExecutionSettingsInput } from '@/types'

const DEFAULT_SETTINGS: ExecutionSettings = {
  schemaVersion: 2,
  browserExecutablePath: 'D:\\RoxyBrowser\\RoxyBrowser.exe',
  roxyApiKey: '',
  roxyApiPort: 50000,
  headless: false,
  requireRegistrationPassword: false,
  enableRegistrationTotp: true,
  proxyRetryCount: 1,
  concurrency: 2,
  taskTimeoutSeconds: 0,
  updatedAt: null,
}

async function parseResponse(response: Response) {
  if (response.ok) return response.json()
  let message = `配置服务返回 HTTP ${response.status}`
  try {
    const body = await response.json()
    if (typeof body.detail === 'string') message = body.detail
    if (body.detail?.message) message = body.detail.message
  } catch {
    // Keep the stable fallback above.
  }
  throw new Error(message)
}

export function defaultExecutionSettings() {
  return structuredClone(DEFAULT_SETTINGS)
}

export const settingsGateway = {
  async getExecutionSettings(): Promise<ExecutionSettings> {
    const response = await fetch('/api/settings/execution', {
      headers: { Accept: 'application/json' },
    })
    return parseResponse(response)
  },

  async updateExecutionSettings(input: ExecutionSettingsInput): Promise<ExecutionSettings> {
    const response = await fetch('/api/settings/execution', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(input),
    })
    return parseResponse(response)
  },
}
