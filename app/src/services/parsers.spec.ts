import { describe, expect, it } from 'vitest'
import { parseEmailImport, parseProxyImport, proxyKey } from './parsers'

describe('parseEmailImport', () => {
  it('cleans BOM and blank lines and accepts the documented format', () => {
    const result = parseEmailImport(
      '\uFEFFdemo@example.com----https://example.com/inbox/token\n\n  second@example.com----https://example.com/s/2  ',
    )

    expect(result.total).toBe(2)
    expect(result.accepted).toEqual([
      { email: 'demo@example.com', accessUrl: 'https://example.com/inbox/token' },
      { email: 'second@example.com', accessUrl: 'https://example.com/s/2' },
    ])
  })

  it('skips existing and in-batch duplicate emails case-insensitively', () => {
    const result = parseEmailImport(
      'DUP@example.com----https://example.com/1\nnew@example.com----https://example.com/2\nNEW@example.com----https://example.com/3',
      ['dup@example.com'],
    )

    expect(result.accepted).toHaveLength(1)
    expect(result.duplicates).toHaveLength(2)
  })

  it('accepts mail.com branded mailboxes with an IMAP password', () => {
    const result = parseEmailImport(
      'person@gardener.com----mail-password-1\nsecond@fireman.net----mail-password-2',
    )

    expect(result.errors).toHaveLength(0)
    expect(result.accepted).toEqual([
      { email: 'person@gardener.com', accessUrl: 'mail-password-1' },
      { email: 'second@fireman.net', accessUrl: 'mail-password-2' },
    ])
  })

  it('rejects invalid email and unsupported URL without exposing the URL token', () => {
    const result = parseEmailImport(
      'invalid----https://example.com/s/super-secret-token\nok@example.com----file:///private/token',
    )

    expect(result.errors).toHaveLength(2)
    expect(JSON.stringify(result.errors)).not.toContain('super-secret-token')
    expect(JSON.stringify(result.errors)).not.toContain('/private/token')
  })
})

describe('parseProxyImport', () => {
  it('accepts a password containing colons', () => {
    const result = parseProxyImport('proxy.example.com:10000:user:pass:with:colons')
    expect(result.accepted).toEqual([
      { host: 'proxy.example.com', port: 10000, username: 'user', password: 'pass:with:colons', scheme: 'http' },
    ])
  })

  it('accepts whitespace-separated SOCKS5 URLs and preserves the scheme', () => {
    const result = parseProxyImport(
      'socks5://user-region-GB-one:pass@proxy.example.com:3000 socks5://user-region-GB-two:pass@proxy.example.com:3000',
    )
    expect(result.total).toBe(2)
    expect(result.errors).toHaveLength(0)
    expect(result.accepted.map((item) => item.scheme)).toEqual(['socks5', 'socks5'])
  })

  it.each(['host:0:user:pass', 'host:65536:user:pass', 'host:nope:user:pass'])(
    'rejects invalid port in %s',
    (input) => {
      expect(parseProxyImport(input).errors).toHaveLength(1)
    },
  )

  it('skips exact existing proxy records', () => {
    const parsed = { host: 'proxy.example.com', port: 10000, username: 'user', password: 'pass', scheme: 'http' as const }
    const result = parseProxyImport(
      'proxy.example.com:10000:user:pass',
      [proxyKey(parsed)],
    )
    expect(result.accepted).toHaveLength(0)
    expect(result.duplicates).toHaveLength(1)
  })
})
