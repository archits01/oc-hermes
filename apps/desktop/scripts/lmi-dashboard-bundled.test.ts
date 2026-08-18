import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const pluginPath = path.join(root, 'src/plugins/lmi-dashboard/plugin.tsx')
const pluginsTs = path.join(root, 'src/contrib/plugins.ts')

describe('lmi-dashboard packaged sidebar page', () => {
  it('ships as a bundled src/plugins plugin, not a local HERMES_HOME extra', () => {
    expect(existsSync(pluginPath)).toBe(true)
    const source = readFileSync(pluginPath, 'utf8')
    expect(source).toMatch(/defaultEnabled:\s*true/)
    expect(source).toMatch(/path:\s*'\/lmi-dashboard'/)
    expect(source).toMatch(/label:\s*'LMI Dashboard'/)
    expect(source).toMatch(/document\.createElement\('webview'\)/)
    expect(source).not.toMatch(/openExternal/)
  })

  it('is covered by the auto-discovery glob used in packaged builds', () => {
    const source = readFileSync(pluginsTs, 'utf8')
    expect(source).toMatch("../plugins/*/plugin.{ts,tsx}")
  })
})
