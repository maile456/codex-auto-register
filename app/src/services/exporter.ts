import type {
  AccountExport,
  AccountRecord,
  EmailExport,
  EmailRecord,
  ExportFormat,
} from '@/types'

function two(value: number) {
  return String(value).padStart(2, '0')
}

export function formatExportTimestamp(date: Date) {
  return `${date.getFullYear()}${two(date.getMonth() + 1)}${two(date.getDate())}-${two(date.getHours())}${two(date.getMinutes())}${two(date.getSeconds())}`
}

export function buildAccountExport(
  accounts: AccountRecord[],
  format: Exclude<ExportFormat, 'access-tokens'>,
  now = new Date(),
): AccountExport {
  const content = accounts
    .map((account) =>
      format === 'credentials'
        ? `${account.email}----${account.chatgptPassword}----${account.totpSecret}`
        : `${account.email}----${account.emailAccessUrl}`,
    )
    .join('\n')
  const suffix = format === 'credentials' ? 'credentials' : 'mail-links'

  return {
    content,
    filename: `accounts-${accounts.length}-${suffix}-${formatExportTimestamp(now)}.txt`,
    count: accounts.length,
    format,
    skippedMissingCount: 0,
    skippedExpiredCount: 0,
  }
}

export function buildEmailExport(emails: EmailRecord[], now = new Date()): EmailExport {
  return {
    content: emails.map((email) => `${email.email}----${email.accessUrl}`).join('\n'),
    filename: `emails-${emails.length}-mail-links-${formatExportTimestamp(now)}.txt`,
    count: emails.length,
  }
}

export function downloadTextFile(content: string, filename: string) {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

export function downloadEncodedFile(
  content: string,
  filename: string,
  mimeType = 'application/octet-stream',
  encoding: 'utf-8' | 'base64' = 'utf-8',
) {
  let blob: Blob
  if (encoding === 'base64') {
    const binary = window.atob(content)
    const bytes = new Uint8Array(binary.length)
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
    blob = new Blob([bytes], { type: mimeType })
  } else {
    blob = new Blob([content], { type: `${mimeType};charset=utf-8` })
  }
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

export async function copyText(content: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(content)
    return
  }

  const textarea = document.createElement('textarea')
  textarea.value = content
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  const copied = document.execCommand('copy')
  document.body.removeChild(textarea)
  if (!copied) throw new Error('浏览器拒绝了剪贴板操作')
}
