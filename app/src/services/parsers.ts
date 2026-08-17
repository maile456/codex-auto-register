import type { ImportIssue, ImportPreview, ParsedEmail, ParsedProxy, ProxyScheme } from '@/types'

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

function linesOf(rawText: string) {
  return rawText.replace(/^\uFEFF/, '').split(/\r?\n/)
}

function maskEmail(value: string) {
  const [name = '', domain] = value.split('@')
  if (!domain) return '格式不可识别'
  const visible = name.slice(0, Math.min(2, name.length))
  return `${visible}${'•'.repeat(Math.max(2, Math.min(8, name.length - visible.length)))}@${domain}`
}

function redactLine(value: string) {
  const trimmed = value.trim()
  if (!trimmed) return '空行'
  const emailSeparator = trimmed.indexOf('----')
  if (emailSeparator >= 0) {
    const emailCandidate = trimmed.slice(0, emailSeparator).trim()
    return emailCandidate.includes('@')
      ? maskEmail(emailCandidate)
      : `${emailCandidate.slice(0, 8) || '无邮箱'}----••••`
  }
  const proxyParts = trimmed.split(':')
  if (/^(?:https?|socks5h?):\/\//i.test(trimmed)) {
    try {
      const url = new URL(trimmed)
      return `${url.protocol}//ACCOUNT:TOKEN@${url.hostname}:${url.port}`
    } catch {
      return '代理 URL 格式不可识别'
    }
  }
  if (proxyParts.length >= 2) return `${proxyParts[0]}:${proxyParts[1]}:••••:••••`
  return `${trimmed.slice(0, 14)}${trimmed.length > 14 ? '…' : ''}`
}

export function emailKey(email: string) {
  return email.trim().toLowerCase()
}

export function proxyKey(proxy: ParsedProxy) {
  return `${proxy.host.toLowerCase()}:${proxy.port}:${proxy.username}:${proxy.password}`
}

export function parseEmailImport(
  rawText: string,
  existingEmails: Iterable<string> = [],
): ImportPreview<ParsedEmail> {
  const accepted: ParsedEmail[] = []
  const duplicates: ImportIssue[] = []
  const errors: ImportIssue[] = []
  const seen = new Set(Array.from(existingEmails, emailKey))
  let total = 0

  linesOf(rawText).forEach((source, index) => {
    const value = source.trim()
    if (!value) return
    total += 1
    const line = index + 1
    const separatorIndex = value.indexOf('----')

    if (separatorIndex < 1) {
      errors.push({ line, reason: '缺少 ---- 分隔符', preview: redactLine(value) })
      return
    }

    const email = value.slice(0, separatorIndex).trim()
    const credential = value.slice(separatorIndex + 4).trim()

    if (!EMAIL_PATTERN.test(email)) {
      errors.push({ line, reason: '邮箱格式无效', preview: redactLine(value) })
      return
    }

    let isHttpUrl = false
    try {
      const url = new URL(credential)
      isHttpUrl = ['http:', 'https:'].includes(url.protocol)
    } catch {
      isHttpUrl = false
    }
    const isMailcomPassword = credential.length > 0 && credential.length <= 1024 && !credential.includes('://')
    if (!isHttpUrl && !isMailcomPassword) {
      errors.push({ line, reason: '第二段必须是接码 URL 或 mail.com 密码', preview: maskEmail(email) })
      return
    }

    const key = emailKey(email)
    if (seen.has(key)) {
      duplicates.push({ line, reason: '邮箱已存在或在本批次重复', preview: maskEmail(email) })
      return
    }

    seen.add(key)
    accepted.push({ email, accessUrl: credential })
  })

  return { total, accepted, duplicates, errors }
}

export function parseProxyImport(
  rawText: string,
  existingKeys: Iterable<string> = [],
): ImportPreview<ParsedProxy> {
  const accepted: ParsedProxy[] = []
  const duplicates: ImportIssue[] = []
  const errors: ImportIssue[] = []
  const seen = new Set(existingKeys)
  let total = 0

  rawText.replace(/^\uFEFF/, '').trim().split(/\s+/).forEach((source, index) => {
    const value = source.trim()
    if (!value) return
    total += 1
    const line = index + 1
    let host = ''
    let username = ''
    let password = ''
    let port = 0
    let scheme: ProxyScheme = 'http'
    if (value.includes('://')) {
      try {
        const url = new URL(value)
        const parsedScheme = url.protocol.replace(':', '').toLowerCase()
        if (!['http', 'https', 'socks5', 'socks5h'].includes(parsedScheme)) throw new Error('unsupported scheme')
        scheme = parsedScheme as ProxyScheme
        host = url.hostname.trim()
        port = Number(url.port)
        username = decodeURIComponent(url.username).trim()
        password = decodeURIComponent(url.password).trim()
      } catch {
        errors.push({ line, reason: '代理 URL 格式无效', preview: redactLine(value) })
        return
      }
    } else {
      const parts = value.split(':')
      if (parts.length < 4) {
        errors.push({ line, reason: '代理必须是 URL 或 host:port:username:password', preview: redactLine(value) })
        return
      }
      const [hostRaw, portRaw, usernameRaw, ...passwordParts] = parts
      host = hostRaw?.trim() ?? ''
      username = usernameRaw?.trim() ?? ''
      password = passwordParts.join(':').trim()
      port = Number(portRaw)
    }

    if (!host || !username || !password) {
      errors.push({ line, reason: '主机、用户名和密码不能为空', preview: redactLine(value) })
      return
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      errors.push({ line, reason: '端口必须是 1–65535 的整数', preview: redactLine(value) })
      return
    }

    const parsed = { host, port, username, password, scheme }
    const key = proxyKey(parsed)
    if (seen.has(key)) {
      duplicates.push({ line, reason: '代理已存在或在本批次重复', preview: redactLine(value) })
      return
    }

    seen.add(key)
    accepted.push(parsed)
  })

  return { total, accepted, duplicates, errors }
}
