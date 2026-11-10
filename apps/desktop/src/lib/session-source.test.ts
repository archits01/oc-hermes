import { describe, expect, it } from 'vitest'

import {
  isMessagingSource,
  LMI_CONFIGURED_MESSAGING_SOURCE_IDS,
  MESSAGING_SESSION_SOURCE_IDS,
  mergeMessagingSourceIds,
  sessionSourceLabel,
  sessionSourceSearchTerms
} from './session-source'

// Regression guard for #46761 / PR #47395: Photon (iMessage) must keep its own
// sidebar section. refreshMessagingSessions() filters rows through
// isMessagingSource(), so this entry is the sole condition that keeps Photon
// sessions out of generic recents. A silent removal would regress the feature
// with no test failure — these asserts pin the contract.
describe('photon messaging source registration', () => {
  it('treats photon as a messaging source (own sidebar section)', () => {
    expect(isMessagingSource('photon')).toBe(true)
  })

  it('is case/space insensitive on the source id', () => {
    expect(isMessagingSource('PHOTON')).toBe(true)
    expect(isMessagingSource('  photon ')).toBe(true)
  })

  it('exposes the iMessage/messages search aliases so Photon sessions are findable', () => {
    const terms = sessionSourceSearchTerms('photon')
    expect(terms).toContain('imessage')
    expect(terms).toContain('messages')
  })

  it('is registered in the messaging source id list', () => {
    expect(MESSAGING_SESSION_SOURCE_IDS).toContain('photon')
  })

  it('does not flag local/CLI-ish sources as messaging (guard sanity)', () => {
    expect(isMessagingSource('cli')).toBe(false)
    expect(isMessagingSource(null)).toBe(false)
    expect(isMessagingSource(undefined)).toBe(false)
  })
})

describe('whatsapp_unipile display label', () => {
  it('shows WhatsApp, not the Unipile adapter name', () => {
    expect(sessionSourceLabel('whatsapp_unipile')).toBe('WhatsApp')
    expect(sessionSourceLabel('whatsapp_unipile')).not.toMatch(/unipile/i)
  })

  it('still groups the Unipile adapter as its own messaging source', () => {
    expect(isMessagingSource('whatsapp_unipile')).toBe(true)
    expect(MESSAGING_SESSION_SOURCE_IDS).toContain('whatsapp_unipile')
  })
})

describe('configured LMI messaging sections', () => {
  it('keeps the configured channels visible without requiring a thread row', () => {
    expect(mergeMessagingSourceIds([], LMI_CONFIGURED_MESSAGING_SOURCE_IDS)).toEqual([
      'instagram',
      'linkedin',
      'telegram'
    ])
  })

  it('merges loaded sessions without admitting local or unknown sources', () => {
    expect(mergeMessagingSourceIds(['whatsapp_unipile', 'desktop', 'not-a-platform'], ['telegram'])).toEqual([
      'whatsapp_unipile',
      'telegram'
    ])
  })

  it('uses human labels for the LMI channels', () => {
    expect(sessionSourceLabel('instagram')).toBe('Instagram')
    expect(sessionSourceLabel('linkedin')).toBe('LinkedIn')
  })
})
