import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const electronDir = path.dirname(fileURLToPath(import.meta.url))
const mainSource = fs.readFileSync(path.join(electronDir, 'main.ts'), 'utf8')
const hudSource = fs.readFileSync(path.join(electronDir, 'hud-ipc.ts'), 'utf8')

const HUD_CHANNELS = [
  'hermes:hud:open',
  'hermes:hud:frost',
  'hermes:hud:ignore-mouse',
  'hermes:hud:move-by',
  'hermes:hud:set-bounds',
  'hermes:hud:session',
  'hermes:hud:close'
] as const

function registrationCount(source: string, channel: string) {
  const escaped = channel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

  return [...source.matchAll(new RegExp(`ipcMain\\.(?:handle|on)\\(['\"]${escaped}['\"]`, 'g'))].length
}

describe('HUD IPC registration ownership', () => {
  it('registers every HUD channel exactly once in hud-ipc.ts and never again in main.ts', () => {
    for (const channel of HUD_CHANNELS) {
      expect(registrationCount(hudSource, channel), channel).toBe(1)
      expect(registrationCount(mainSource, channel), channel).toBe(0)
    }
  })
})
