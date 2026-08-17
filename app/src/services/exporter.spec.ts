import { describe, expect, it } from 'vitest'
import { buildAccountExport, buildEmailExport, formatExportTimestamp } from './exporter'
import type { AccountRecord, EmailRecord } from '@/types'

const account = (id: number): AccountRecord => ({
  id: String(id),
  email: `demo${id}@example.com`,
  chatgptPassword: `password-${id}`,
  totpSecret: `TOTP-${id}`,
  emailAccessUrl: `https://example.com/inbox/${id}`,
  createdAt: '2026-08-08T00:00:00.000Z',
  accountType: 'free',
  phoneBound: null,
  promotionEligible: true,
  accessTokenConfigured: false,
  accessTokenExpiresAt: null,
  accessTokenUpdatedAt: null,
})

describe('buildAccountExport', () => {
  const now = new Date(2026, 7, 8, 15, 30, 0)

  it('exports exact credential lines and puts count after accounts', () => {
    const result = buildAccountExport([account(1), account(2)], 'credentials', now)
    expect(result.content).toBe(
      'demo1@example.com----password-1----TOTP-1\ndemo2@example.com----password-2----TOTP-2',
    )
    expect(result.filename).toBe('accounts-2-credentials-20260808-153000.txt')
    expect(result.count).toBe(2)
  })

  it('exports exact email access link lines', () => {
    const result = buildAccountExport([account(1)], 'mail-links', now)
    expect(result.content).toBe('demo1@example.com----https://example.com/inbox/1')
    expect(result.filename).toBe('accounts-1-mail-links-20260808-153000.txt')
  })

  it('formats local timestamps deterministically', () => {
    expect(formatExportTimestamp(now)).toBe('20260808-153000')
  })
})

describe('buildEmailExport', () => {
  const now = new Date(2026, 7, 8, 15, 30, 0)
  const emails: EmailRecord[] = [
    {
      id: 'mail-1',
      email: 'one@example.com',
      accessUrl: 'https://example.com/inbox/one',
      importedAt: '2026-08-08T00:00:00.000Z',
    },
    {
      id: 'mail-2',
      email: 'two@example.com',
      accessUrl: 'https://example.com/inbox/two',
      importedAt: '2026-08-08T00:00:00.000Z',
    },
  ]

  it('exports exact mail-link lines and includes the count in the filename', () => {
    const result = buildEmailExport(emails, now)

    expect(result.content).toBe(
      'one@example.com----https://example.com/inbox/one\ntwo@example.com----https://example.com/inbox/two',
    )
    expect(result.filename).toBe('emails-2-mail-links-20260808-153000.txt')
    expect(result.count).toBe(2)
  })

  it('uses count one for a single email', () => {
    expect(buildEmailExport([emails[0]!], now).filename).toBe(
      'emails-1-mail-links-20260808-153000.txt',
    )
  })
})
