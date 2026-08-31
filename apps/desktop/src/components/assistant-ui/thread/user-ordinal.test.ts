import { describe, expect, it } from 'vitest'

import { type OrdinalThreadMessage, visibleUserOrdinalFromThread } from './user-message'

const user = (id: string): OrdinalThreadMessage => ({ id, role: 'user' })
const reply = (id: string): OrdinalThreadMessage => ({ id, role: 'assistant' })
const failedReply = (id: string): OrdinalThreadMessage => ({
  id,
  role: 'assistant',
  status: { reason: 'error', type: 'incomplete' }
})

/**
 * These ordinals are the ONE visible-user space the rewind path shares with the
 * gateway (use-prompt-actions/utils.ts). If this file and visibleUserOrdinal()
 * ever disagree, restore silently rewinds to the wrong message.
 */
describe('visibleUserOrdinalFromThread', () => {
  it('counts plain user turns in order', () => {
    const thread = [user('u0'), reply('a0'), user('u1'), reply('a1'), user('u2')]

    expect(visibleUserOrdinalFromThread(thread, 'u0')).toBe(0)
    expect(visibleUserOrdinalFromThread(thread, 'u1')).toBe(1)
    expect(visibleUserOrdinalFromThread(thread, 'u2')).toBe(2)
  })

  it('skips a failed turn, because the gateway holds no slot for it', () => {
    // u1 never reached the backend, so u2 is ordinal 1 there, not 2.
    const thread = [user('u0'), reply('a0'), user('u1'), failedReply('a1'), user('u2')]

    expect(visibleUserOrdinalFromThread(thread, 'u0')).toBe(0)
    expect(visibleUserOrdinalFromThread(thread, 'u2')).toBe(1)
  })

  it('reports no ordinal for a failed turn itself', () => {
    // Returning the running count would hand back u2's ordinal and rewind past
    // the message the user actually clicked.
    const thread = [user('u0'), reply('a0'), user('u1'), failedReply('a1'), user('u2')]

    expect(visibleUserOrdinalFromThread(thread, 'u1')).toBeNull()
  })

  it('stays null for an unknown or missing id', () => {
    const thread = [user('u0'), reply('a0')]

    expect(visibleUserOrdinalFromThread(thread, 'nope')).toBeNull()
    expect(visibleUserOrdinalFromThread(thread, undefined)).toBeNull()
  })

  it('does not treat a completed reply as a failure', () => {
    const thread = [user('u0'), { id: 'a0', role: 'assistant', status: { reason: 'stop', type: 'complete' } }, user('u1')]

    expect(visibleUserOrdinalFromThread(thread, 'u1')).toBe(1)
  })
})
