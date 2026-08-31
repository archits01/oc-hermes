import { describe, expect, it } from 'vitest'

import { wakeWordAllowedForConnection } from './wake-word-policy'

describe('wakeWordAllowedForConnection', () => {
  it('removes wake-word UI and auto-arm from remote thin clients', () => {
    expect(wakeWordAllowedForConnection('remote')).toBe(false)
  })

  it('preserves wake-word support for local Desktop installs', () => {
    expect(wakeWordAllowedForConnection('local')).toBe(true)
    expect(wakeWordAllowedForConnection(undefined)).toBe(true)
  })
})
