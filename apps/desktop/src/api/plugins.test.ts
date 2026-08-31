import { afterEach, describe, expect, test } from 'vitest'

import { pluginRest } from './plugins'

const originalDesktop = window.hermesDesktop

afterEach(() => {
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: originalDesktop })
})

describe('plugin REST bridge errors', () => {
  test('uses OpenComputer wording when the desktop bridge is unavailable', async () => {
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: undefined })

    await expect(pluginRest('example', '/status')).rejects.toThrow('OpenComputer desktop bridge unavailable')
  })
})
