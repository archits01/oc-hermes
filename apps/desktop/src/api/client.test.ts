import { describe, expect, test } from 'vitest'

import { GATEWAY_ERROR_MESSAGES } from './client'

describe('OpenComputer gateway error surface', () => {
  test('uses OpenComputer wording for every reachable gateway transport error', () => {
    expect(GATEWAY_ERROR_MESSAGES).toEqual({
      closed: 'OpenComputer gateway connection closed',
      connect: 'Could not connect to OpenComputer gateway',
      notConnected: 'OpenComputer gateway is not connected'
    })
  })
})
