import assert from 'node:assert/strict'
import { test } from 'vitest'

import { seedDefaultConnectionIfMissing } from './default-connection'

function makeFs(files: Record<string, string>) {
  const store = { ...files }
  return {
    store,
    existsSync: (p: string) => Object.prototype.hasOwnProperty.call(store, p),
    readFileSync: (p: string) => {
      if (!Object.prototype.hasOwnProperty.call(store, p)) throw new Error(`ENOENT ${p}`)
      return store[p]
    },
    mkdirSync: () => undefined,
    writeFileSync: (p: string, data: string) => {
      store[p] = data
    }
  }
}

const validDefault = JSON.stringify({
  mode: 'remote',
  remote: {
    url: 'https://agent-e0bf40f99435625e.tryopencomputer.com',
    authMode: 'token',
    token: { encoding: 'plain', value: 'test-token' }
  }
})

test('does not overwrite an existing connection.json', () => {
  const fs = makeFs({
    '/user/connection.json': '{"mode":"local"}',
    '/res/default-connection.json': validDefault
  })
  const result = seedDefaultConnectionIfMissing({
    connectionPath: '/user/connection.json',
    defaultCandidates: ['/res/default-connection.json'],
    ...fs
  })
  assert.equal(result.seeded, false)
  assert.equal(result.reason, 'exists')
  assert.equal(fs.store['/user/connection.json'], '{"mode":"local"}')
})

test('seeds first launch from the first valid packaged default', () => {
  const fs = makeFs({
    '/res/default-connection.json': validDefault
  })
  const result = seedDefaultConnectionIfMissing({
    connectionPath: '/user/connection.json',
    defaultCandidates: [null, '/missing.json', '/res/default-connection.json'],
    ...fs
  })
  assert.equal(result.seeded, true)
  assert.equal(result.reason, 'seeded')
  assert.equal(result.from, '/res/default-connection.json')
  const written = JSON.parse(fs.store['/user/connection.json'])
  assert.equal(written.mode, 'remote')
  assert.equal(written.remote.url, 'https://agent-e0bf40f99435625e.tryopencomputer.com')
})

test('skips a malformed default and reports invalid-default', () => {
  const fs = makeFs({
    '/res/default-connection.json': '{"mode":"local"}'
  })
  const result = seedDefaultConnectionIfMissing({
    connectionPath: '/user/connection.json',
    defaultCandidates: ['/res/default-connection.json'],
    ...fs
  })
  assert.equal(result.seeded, false)
  assert.equal(result.reason, 'invalid-default')
  assert.equal(Object.prototype.hasOwnProperty.call(fs.store, '/user/connection.json'), false)
})

test('returns no-default when nothing is packaged', () => {
  const fs = makeFs({})
  const result = seedDefaultConnectionIfMissing({
    connectionPath: '/user/connection.json',
    defaultCandidates: ['/res/default-connection.json'],
    ...fs
  })
  assert.equal(result.seeded, false)
  assert.equal(result.reason, 'no-default')
})
