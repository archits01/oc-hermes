import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { LinuxPackager, Packager } from 'app-builder-lib'
import { afterEach, test, vi } from 'vitest'

const require = createRequire(import.meta.url)
const lib = path.dirname(path.dirname(require.resolve('app-builder-lib')))
const { getPublishConfigs } = require(path.join(lib, 'dist/publish/PublishManager.js'))
afterEach(() => vi.unstubAllEnvs())

test.each([['repository', true, true], ['missing repository', false, true], ['no token', true, false]])(
  'real supplier publish resolution: %s', async (_name, repository, token) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'publish-resolution-'))
    for (const key of ['GH_TOKEN', 'GITHUB_TOKEN', 'GITLAB_TOKEN', 'KEYGEN_TOKEN', 'BITBUCKET_TOKEN']) vi.stubEnv(key, '')
    vi.stubEnv('GITHUB_TOKEN', token ? 'fixture-not-a-real-token' : '')
    for (const key of ['TRAVIS_REPO_SLUG', 'APPVEYOR_REPO_NAME', 'CIRCLE_PROJECT_USERNAME', 'CIRCLE_PROJECT_REPONAME']) vi.stubEnv(key, undefined)
    // No .git parent and no production metadata resolver replaced. Only resolution, never publish.
    try {
      /** @type {import('app-builder-lib').Metadata} */
      const metadata = { name: 'fixture', version: '1.0.0', description: 'fixture', author: 'Fixture' }
      if (repository) metadata.repository = 'https://github.com/NousResearch/hermes-agent'
      fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify(metadata))
      const info = new Packager({ projectDir: root, config: {} })
      await info.validateConfig()
      const packager = new LinuxPackager(info)
      if (!token) {
        assert.deepEqual(await getPublishConfigs(packager, null, null, true), [])
      } else if (!repository) {
        await assert.rejects(getPublishConfigs(packager, null, null, true), /Cannot detect repository/)
      } else {
        const repositoryInfo = await packager.repositoryInfo
        assert.equal(repositoryInfo.user, 'NousResearch')
        assert.equal(repositoryInfo.project, 'hermes-agent')
        assert.deepEqual(await getPublishConfigs(packager, null, null, true), [{ owner: 'NousResearch', repo: 'hermes-agent', provider: 'github' }])
      }
    } finally { fs.rmSync(root, { recursive: true, force: true }) }
  }
)

<<<<<<< HEAD
const TOKEN_VARS = ['GH_TOKEN', 'GITHUB_TOKEN', 'GITLAB_TOKEN', 'KEYGEN_TOKEN', 'BITBUCKET_TOKEN']
let savedEnv

beforeEach(() => {
  savedEnv = {}
  for (const key of TOKEN_VARS) {
    savedEnv[key] = process.env[key]
    delete process.env[key]
  }
})

afterEach(() => {
  for (const key of TOKEN_VARS) {
    if (savedEnv[key] === undefined) delete process.env[key]
    else process.env[key] = savedEnv[key]
  }
})

describe('local desktop pack stays out of the publish path', () => {
  test('the pack script pins an explicit publish policy', () => {
    // electron-builder 26 infers `onTagOrDraft` from CI when --publish is
    // absent, and `hermes desktop` runs the pack with CI=1 (_npm_lifecycle_env).
    // v27 drops the implicit behavior, so being explicit is also forward-safe.
    const pack = desktopPkg.scripts.pack
    assert.match(pack, /--dir\b/)
    assert.match(pack, /--publish\s+never\b/)
  })

  test('publish resolution succeeds with a GitHub token present', async () => {
    // The #87758 failure mode: CI=1 makes isPublish true, a GITHUB_TOKEN in the
    // environment auto-selects the github provider, and the provider needs a
    // repository it cannot find — apps/desktop has no .git/config of its own
    // and app-builder-lib does not walk up to the workspace root.
    process.env.GITHUB_TOKEN = 'x'

    const configs = await getPublishConfigs(
      fakePackager(desktopPkg),
      null,
      null,
      /* errorIfCannot */ true
    )

    assert.ok(Array.isArray(configs) && configs.length > 0)
    assert.equal(configs[0].provider, 'github')
    assert.equal(configs[0].owner, 'archits01')
    assert.equal(configs[0].repo, 'oc-hermes')
  })

  test('a package without the repository field is what breaks resolution', async () => {
    // Guards the fix itself: proves the assertion above passes because of the
    // repository field, not because the throw is unreachable.
    process.env.GITHUB_TOKEN = 'x'
    const { repository, ...withoutRepository } = desktopPkg
    assert.ok(repository, 'apps/desktop/package.json must declare a repository')

    await assert.rejects(
      () => getPublishConfigs(fakePackager(withoutRepository), null, null, true),
      /Cannot detect repository by \.git\/config/
    )
  })

  test('resolution is quiet when no publish token is configured', async () => {
    const configs = await getPublishConfigs(fakePackager(desktopPkg), null, null, true)
    assert.deepEqual(configs, [])
  })

  test('packaged release updates are pinned to the canonical OpenComputer GitHub repository', () => {
    assert.equal(desktopPkg.repository.url, 'git+https://github.com/archits01/oc-hermes.git')
    assert.deepEqual(desktopPkg.build.publish, [
      {
        provider: 'github',
        owner: 'archits01',
        repo: 'oc-hermes',
        releaseType: 'release'
      }
    ])
    assert.ok(desktopPkg.build.mac.target.includes('zip'), 'macOS updater requires the zip target')
  })
=======
test('the pack script pins an explicit publish policy', () => {
  const pack = require('../package.json').scripts.pack
  assert.match(pack, /--dir\b/)
  assert.match(pack, /--publish\s+never\b/)
>>>>>>> upstream/main
})
