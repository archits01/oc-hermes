import { describe, expect, it } from 'vitest'

import { patchLmiInboxJs, shouldInterceptLmiInboxChunk } from './lmi-inbox-js-patch'

describe('patchLmiInboxJs', () => {
  it('keeps WhatsApp thread chrome from reading a missing emailViewOnly binding', () => {
    const source = 'ex&&!!s.memberId&&!emailViewOnly&&booked'
    const patched = patchLmiInboxJs(source)
    const s = { platform: 'whatsapp', memberId: '1' }
    const result = new Function('s', 'ex', 'booked', `return (${patched})`)(s, true, true)
    expect(result).toBe(true)
  })

  it('treats unbound Gmail rows without a CRM lead as view-only', () => {
    const source = 'emailViewOnly&&banner'
    const patched = patchLmiInboxJs(source)
    const s = { platform: 'gmail', memberId: null }
    const result = new Function('s', 'banner', `return (${patched})`)(s, true)
    expect(result).toBe(true)
  })

  it('does not rewrite bundles that already guard the binding', () => {
    const source = 'typeof emailViewOnly!=="undefined"?emailViewOnly:!1'
    expect(patchLmiInboxJs(source)).toBe(source)
  })

  it('leaves unrelated javascript alone', () => {
    expect(patchLmiInboxJs('function S(){return 1}')).toBe('function S(){return 1}')
  })
})

describe('shouldInterceptLmiInboxChunk', () => {
  it('matches only LMI dashboard next chunks', () => {
    expect(
      shouldInterceptLmiInboxChunk(
        'https://lmi-dashboard-one.vercel.app/_next/static/chunks/2jp83xwp6jtro.js'
      )
    ).toBe(true)
    expect(shouldInterceptLmiInboxChunk('https://lmi-dashboard-one.vercel.app/dashboard/inbox')).toBe(false)
    expect(shouldInterceptLmiInboxChunk('https://example.com/_next/static/chunks/app.js')).toBe(false)
  })
})
