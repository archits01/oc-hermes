/**
 * First-launch remote connection seeder.
 *
 * A packaged friend DMG can ship `default-connection.json` next to
 * `install-stamp.json` (electron-builder extraResources). On first launch,
 * if the user has no connection.json yet, copy that default so the app
 * auto-connects to the baked-in VM. Never overwrite an existing file.
 *
 * Pure enough to unit-test with injected fs. No electron import.
 */

export interface SeedDefaultConnectionOptions {
  connectionPath: string
  defaultCandidates: Array<string | null | undefined>
  existsSync: (path: string) => boolean
  readFileSync: (path: string, encoding: 'utf8') => string
  mkdirSync: (path: string, opts: { recursive: true }) => unknown
  writeFileSync: (path: string, data: string) => void
}

export interface SeedDefaultConnectionResult {
  seeded: boolean
  reason: 'exists' | 'no-default' | 'invalid-default' | 'seeded'
  from?: string
}

function isHttpUrl(value: unknown): value is string {
  if (typeof value !== 'string' || !value.trim()) {return false}

  try {
    const parsed = new URL(value.trim())

    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
  } catch {
    return false
  }
}

export function seedDefaultConnectionIfMissing(
  opts: SeedDefaultConnectionOptions
): SeedDefaultConnectionResult {
  if (opts.existsSync(opts.connectionPath)) {
    return { seeded: false, reason: 'exists' }
  }

  let sawInvalid = false

  for (const candidate of opts.defaultCandidates) {
    if (!candidate || !opts.existsSync(candidate)) {continue}

    try {
      const parsed = JSON.parse(opts.readFileSync(candidate, 'utf8'))

      if (!parsed || typeof parsed !== 'object') {
        sawInvalid = true

        continue
      }

      const mode = (parsed as { mode?: unknown }).mode

      if (mode !== 'remote' && mode !== 'cloud') {
        sawInvalid = true

        continue
      }

      const remote = (parsed as { remote?: { url?: unknown } }).remote

      if (!isHttpUrl(remote?.url)) {
        sawInvalid = true

        continue
      }

      const dir = opts.connectionPath.replace(/[/\\][^/\\]+$/, '')

      if (dir && dir !== opts.connectionPath) {
        opts.mkdirSync(dir, { recursive: true })
      }

      opts.writeFileSync(opts.connectionPath, `${JSON.stringify(parsed, null, 2)}\n`)

      return { seeded: true, reason: 'seeded', from: candidate }
    } catch {
      sawInvalid = true
    }
  }

  return { seeded: false, reason: sawInvalid ? 'invalid-default' : 'no-default' }
}
