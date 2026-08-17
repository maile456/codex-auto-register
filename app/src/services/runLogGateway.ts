import type { RunLogFile, RunLogSummary } from '@/types'

async function parseResponse<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>
  let message = `日志服务返回 HTTP ${response.status}`
  try {
    const body = await response.json()
    if (typeof body.detail === 'string') message = body.detail
    if (body.detail?.message) message = body.detail.message
  } catch {
    // Keep the stable fallback above.
  }
  throw new Error(message)
}

export const runLogGateway = {
  async listRuns(): Promise<RunLogSummary[]> {
    const response = await fetch('/api/run-logs/runs', {
      headers: { Accept: 'application/json' },
    })
    return parseResponse<RunLogSummary[]>(response)
  },

  async getRun(runId: string): Promise<RunLogFile> {
    const response = await fetch(`/api/run-logs/runs/${encodeURIComponent(runId)}`, {
      headers: { Accept: 'application/json' },
    })
    return parseResponse<RunLogFile>(response)
  },
}
