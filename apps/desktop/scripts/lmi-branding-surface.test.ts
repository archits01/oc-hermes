import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const surfaces = [
  'package.json',
  'scripts/set-exe-identity.mjs',
  'src/plugins/lmi-dashboard/plugin.tsx',
  'src/components/boot-failure-reauth.ts',
  'src/components/onboarding/providers.tsx',
  'src/lib/desktop-remote-auth.ts',
  'src/app/settings/constants.ts',
  'src/app/settings/billing/errors.ts',
  'src/app/skills/mcp-tab.tsx',
  'src/themes/presets.ts',
  'electron/backend-health.ts',
  'src/components/boot-failure-overlay.tsx'
]
const forbidden = /\bHermes Agent\b|\bNous Research\b|\bNous Cloud\b|\bNous Portal\b|Nous-(?:approved|trained)/i

describe('Open Computer DMG branding contract', () => {
  it('has no upstream product identity on public desktop surfaces', () => {
    const failures: string[] = []
    for (const relative of surfaces) {
      const source = readFileSync(path.join(root, relative), 'utf8')
      if (forbidden.test(source)) failures.push(relative)
    }
    expect(failures).toEqual([])
  })

  it('uses the production Vercel LMI dashboard in the sidebar plugin', () => {
    const source = readFileSync(path.join(root, 'src/plugins/lmi-dashboard/plugin.tsx'), 'utf8')
    expect(source).toContain('https://lmi-dashboard-one.vercel.app/dashboard')
    expect(source).toContain('<iframe')
    expect(source).toContain('Laser Magic India Dashboard')
  })
})
