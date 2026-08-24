import fs from 'node:fs'
import path from 'node:path'

const root = process.cwd()
const failures = []

function fail(message) {
  failures.push(message)
}

function filePath(relative) {
  return path.join(root, relative)
}

function read(relative) {
  const target = filePath(relative)

  if (!fs.existsSync(target)) {
    fail(`missing required file: ${relative}`)
    return ''
  }

  return fs.readFileSync(target, 'utf8')
}

function expectTextIncludes(source, text, message) {
  if (!source.includes(text)) {
    fail(message)
  }
}

function expectIncludes(relative, text, message) {
  expectTextIncludes(read(relative), text, message)
}

function expectRegex(source, expression, message) {
  if (!expression.test(source)) {
    fail(message)
  }
}

function functionBody(source, name) {
  const signature = source.indexOf(`function ${name}(`)
  const opening = signature === -1 ? -1 : source.indexOf('{', signature)

  if (opening === -1) {
    return ''
  }

  let depth = 0
  let quote = null
  let escaped = false

  for (let index = opening; index < source.length; index += 1) {
    const character = source[index]
    const next = source[index + 1]

    if (quote) {
      if (escaped) {
        escaped = false
      } else if (character === '\\') {
        escaped = true
      } else if (character === quote) {
        quote = null
      }

      continue
    }

    if (character === '"' || character === "'" || character === '`') {
      quote = character
      continue
    }

    if (character === '/' && next === '/') {
      const end = source.indexOf('\n', index + 2)

      if (end === -1) {
        return ''
      }

      index = end
      continue
    }

    if (character === '/' && next === '*') {
      const end = source.indexOf('*/', index + 2)

      if (end === -1) {
        return ''
      }

      index = end + 1
      continue
    }

    if (character === '{') {
      depth += 1
    } else if (character === '}') {
      depth -= 1

      if (depth === 0) {
        return source.slice(opening + 1, index)
      }
    }
  }

  return ''
}

function botsMainTabOwnershipFailures(source) {
  const issues = []
  const expect = (body, expression, message) => {
    if (!expression.test(body)) {
      issues.push(message)
    }
  }
  const recordGroupMainTab = functionBody(source, 'recordGroupMainTab')
  const dropGroupMainTab = functionBody(source, 'dropGroupMainTab')
  const shouldRenderGroupChatInPane = functionBody(source, 'shouldRenderGroupChatInPane')
  const botsPane = functionBody(source, 'BotsPane')
  const openGroupChat = functionBody(source, 'openGroupChat')
  const closeGroupChatMainTab = functionBody(source, 'closeGroupChatMainTab')

  expect(recordGroupMainTab, /groupChatMainTabs\.set\(group, close\)/, 'Bots main-tab record mutator lost its map write')
  expect(recordGroupMainTab, /\$groupMainTabsRev\.set\(\$groupMainTabsRev\.get\(\) \+ 1\)/, 'Bots main-tab record mutator lost its revision bump')
  expect(dropGroupMainTab, /groupChatMainTabs\.delete\(group\)/, 'Bots main-tab drop mutator lost its map delete')
  expect(dropGroupMainTab, /\$groupMainTabsRev\.set\(\$groupMainTabsRev\.get\(\) \+ 1\)/, 'Bots main-tab drop mutator lost its revision bump')
  expect(shouldRenderGroupChatInPane, /return Boolean\(group && !groupChatMainTabs\.has\(group\)\)/, 'Bots fallback-pane ownership gate lost')
  expect(botsPane, /useValue\(\$groupMainTabsRev\)/, 'BotsPane no longer subscribes to main-tab ownership changes')
  expect(
    botsPane,
    /if \(shouldRenderGroupChatInPane\(groupChatName\) && groupChatMembers\.length\)/,
    'BotsPane no longer invokes the fallback-pane ownership gate'
  )
  expect(openGroupChat, /recordGroupMainTab\(group, close\)/, 'openGroupChat no longer records main-tab ownership')
  expect(openGroupChat, /dropGroupMainTab\(group\)/, 'openGroupChat no longer releases ownership on tab close')
  expect(closeGroupChatMainTab, /dropGroupMainTab\(group\)/, 'closeGroupChatMainTab no longer releases ownership')

  return issues
}

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function workflowStepBody(source, name) {
  const pattern = new RegExp(
    `^\\s*- name: ${escapeRegex(name)}\\n([\\s\\S]*?)(?=^\\s*- name:|(?![\\s\\S]))`,
    'm'
  )

  return source.match(pattern)?.[1] ?? ''
}

function releaseWorkflowFailures(source) {
  const issues = []
  const checkout = workflowStepBody(source, 'Check out immutable release tag')
  const signing = workflowStepBody(source, 'Require signing credentials and version-matched tag')
  const releaseConfig = workflowStepBody(source, 'Verify OpenComputer release configuration')
  const updaterFeed = workflowStepBody(source, 'Verify signed updater feed and artifacts')
  const upload = workflowStepBody(source, 'Upload verified release assets')
  const publish = workflowStepBody(source, 'Publish release only when explicitly requested')
  const requireText = (body, text, message) => {
    if (!body.includes(text)) {
      issues.push(message)
    }
  }

  requireText(checkout, 'ref: refs/tags/${{ inputs.tag }}', 'release checkout is not pinned to refs/tags input')
  requireText(signing, 'tag_ref="refs/tags/$RELEASE_TAG"', 'release tag ref is not pinned to refs/tags')
  requireText(signing, 'git show-ref --verify --quiet "$tag_ref"', 'release tag existence is not verified')
  requireText(signing, 'git rev-parse "$tag_ref^{}"', 'release tag is not peeled to a commit')
  requireText(signing, '"$tag_commit" == "$head_commit"', 'release tag is not proved equal to HEAD')
  requireText(releaseConfig, 'node .github/scripts/assert-opencomputer-invariants.mjs', 'release build does not run invariants first')
  requireText(updaterFeed, 'app_update="$app_path/Contents/Resources/app-update.yml"', 'release verifies a decoy updater file instead of the consumed path')
  requireText(upload, 'gh release create "$RELEASE_TAG" --verify-tag --draft', 'release creation does not require an existing tag')

  for (const [label, body] of [['upload', upload], ['publish', publish]]) {
    requireText(body, 'git ls-remote --refs origin "refs/tags/$RELEASE_TAG"', `${label} does not recheck remote tag ref`)
    requireText(body, 'git ls-remote origin "refs/tags/$RELEASE_TAG^{}"', `${label} does not recheck remote peeled tag`)
    requireText(body, '"$remote_commit" == "$expected_commit"', `${label} does not bind remote tag to checked-out commit`)
  }

  return issues
}

function forkSyncWorkflowFailures(source) {
  const issues = []

  if (
    /^\s*git\s+push(?:\s+--[^\s]+)*\s+origin\s+(?:"|'|)?(?:oc-branding|refs\/heads\/oc-branding|HEAD:oc-branding|HEAD:refs\/heads\/oc-branding)(?:"|'|)?(?:\s|$)/m.test(
      source
    )
  ) {
    issues.push('scheduled sync has an executable direct push to oc-branding')
  }

  return issues
}

function replaceNth(source, needle, occurrence, replacement) {
  let start = -1

  for (let index = 0; index <= occurrence; index += 1) {
    start = source.indexOf(needle, start + 1)
    if (start === -1) {
      return source
    }
  }

  return source.slice(0, start) + replacement + source.slice(start + needle.length)
}

const requiredFiles = [
  'scripts/fork-sync.sh',
  '.github/scripts/assert-opencomputer-boundary.mjs',
  'apps/desktop/src/plugins/lmi-dashboard/plugin.tsx',
  'apps/desktop/src/contrib/plugins.ts',
  'apps/desktop/src/lib/session-source.ts',
  'apps/desktop/electron/default-connection.ts',
  'apps/desktop/electron/main.ts',
  'apps/desktop/electron/packaged-app-updater.ts',
  'apps/desktop/electron/update-remote.ts',
  'apps/desktop/package.json',
  'apps/desktop/public/flower.png'
]

const vmOwnedLmiSourcePaths = [
  'optional-mcps/unipile',
  'optional-mcps/sarvam-voice',
  'plugins/platforms/lmi_unipile_overlay',
  'plugins/platforms/_lmi_live_reply_queue.py',
  'plugins/platforms/_lmi_media_bootstrap.py',
  'plugins/platforms/_lmi_media_runtime.py',
  'plugins/platforms/_unipile_common.py',
  'plugins/platforms/instagram',
  'plugins/platforms/linkedin',
  'plugins/platforms/whatsapp_unipile',
  'scripts/ops/lmi_media_overlay_sync.py',
  'scripts/ops/lmi_opencomputer_v2_auto_update.sh',
  'scripts/ops/lmi_unipile_mcp_pin.py',
  'scripts/ops/oc-autosave.sh',
  'scripts/ops/opencomputer-v2-health-monitor.sh'
]

for (const relative of requiredFiles) {
  if (!fs.existsSync(filePath(relative))) {
    fail(`missing required file: ${relative}`)
  }
}

for (const relative of vmOwnedLmiSourcePaths) {
  if (fs.existsSync(filePath(relative))) {
    fail(`VM-owned LMI source returned to the OpenComputer base: ${relative}`)
  }
}

const localForkSync = read('scripts/fork-sync.sh')
expectTextIncludes(localForkSync, '[fork-sync] retired:', 'unsafe local fork-sync retirement notice lost')
expectRegex(
  localForkSync,
  /\bexit 2\b/,
  'unsafe local fork-sync must fail closed instead of merging or pushing directly'
)
if (/\bgit\s+(?:merge|push|checkout|pull)\b/.test(localForkSync)) {
  fail('unsafe local fork-sync still contains a direct git merge/push/checkout/pull command')
}

const forkSyncWorkflow = read('.github/workflows/fork-sync.yml')
expectTextIncludes(
  forkSyncWorkflow,
  'elif [[ "$br" == *-CONFLICTS ]]; then',
  'scheduled sync can still age-delete unresolved conflict evidence'
)
expectTextIncludes(
  forkSyncWorkflow,
  '${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}',
  'scheduled sync candidates are not unique per run attempt'
)
if (forkSyncWorkflow.includes('git push -f origin "$CB"')) {
  fail('scheduled sync can overwrite unresolved conflict evidence with a force push')
}
expectTextIncludes(
  forkSyncWorkflow,
  'node .github/scripts/assert-opencomputer-invariants.mjs',
  'scheduled sync no longer runs the executable invariant checker'
)
expectTextIncludes(
  forkSyncWorkflow,
  'FORK_CONTROL_PATHS=(.github/workflows .github/scripts/assert-opencomputer-invariants.mjs .github/scripts/assert-opencomputer-boundary.mjs)',
  'scheduled sync does not preserve its workflow and invariant-checker control plane'
)
expectTextIncludes(
  forkSyncWorkflow,
  'git checkout HEAD^1 -- "${FORK_CONTROL_PATHS[@]}"',
  'clean upstream control-plane changes are not preserved before candidate push'
)
expectTextIncludes(
  forkSyncWorkflow,
  'node .github/scripts/assert-opencomputer-boundary.mjs upstream/main HEAD',
  'scheduled sync does not enforce the GitHub/VM ownership boundary'
)
for (const issue of forkSyncWorkflowFailures(forkSyncWorkflow)) {
  fail(issue)
}

const directPushMutation = forkSyncWorkflow.replace(
  'git push origin "${{ steps.merge.outputs.branch }}"',
  'git push origin HEAD:oc-branding'
)
if (directPushMutation === forkSyncWorkflow) {
  fail('fork-sync direct-push checker self-test fixture was not found')
} else if (forkSyncWorkflowFailures(directPushMutation).length === 0) {
  fail('fork-sync direct-push checker failed to detect an executable oc-branding push')
}

expectIncludes('hermes_cli/default_soul.py', 'OpenComputer', 'branding lost: default_soul.py')
expectIncludes('scripts/install.sh', 'OpenComputer', 'branding lost: install.sh')
expectIncludes('scripts/install.sh', 'archits01', 'install.sh no longer points at the fork')
expectIncludes('scripts/install.ps1', 'archits01', 'install.ps1 no longer points at the fork')
expectIncludes('apps/desktop/src/i18n/en.ts', 'OpenComputer', 'branding lost: i18n/en.ts')
expectIncludes('apps/desktop/src/components/assistant-ui/thread/status.tsx', 'OpenComputer is working', 'desktop activity branding lost')
expectIncludes('apps/desktop/src/sdk/index.ts', 'Update OpenComputer Desktop', 'desktop upgrade branding lost')
expectIncludes('apps/desktop/src/store/gateway.ts', 'OpenComputer gateway unavailable', 'gateway error branding lost')
expectIncludes('apps/desktop/src/i18n/en.ts', 'remote OpenComputer', 'remote profile branding lost')
expectIncludes('apps/desktop/src/api/client.ts', 'OpenComputer gateway connection closed', 'gateway closed-error branding lost')
expectIncludes('apps/desktop/src/api/client.ts', 'Could not connect to OpenComputer gateway', 'gateway connect-error branding lost')
expectIncludes('apps/desktop/src/api/client.ts', 'OpenComputer gateway is not connected', 'gateway disconnected-error branding lost')
expectIncludes('apps/desktop/src/api/plugins.ts', 'OpenComputer desktop bridge unavailable', 'plugin bridge-error branding lost')

const apiClient = read('apps/desktop/src/api/client.ts')
expectRegex(
  apiClient,
  /super\(\{[\s\S]*?closedErrorMessage:\s*GATEWAY_ERROR_MESSAGES\.closed,[\s\S]*?connectErrorMessage:\s*GATEWAY_ERROR_MESSAGES\.connect,[\s\S]*?notConnectedErrorMessage:\s*GATEWAY_ERROR_MESSAGES\.notConnected,/,
  'OpenComputer gateway error messages are no longer wired into HermesGateway'
)

const desktopMain = read('apps/desktop/electron/main.ts')
expectRegex(
  desktopMain,
  /seedDefaultConnectionIfMissing\(\{[\s\S]*?default-connection\.json/,
  'friend-DMG default connection seed lost'
)

function asPublishers(value) {
  if (value == null) {
    return []
  }

  return Array.isArray(value) ? value : [value]
}

function effectiveMacPublishers(build, target) {
  const targetPublish = build?.[target]?.publish

  if (targetPublish !== undefined) {
    return asPublishers(targetPublish)
  }

  const macPublish = build?.mac?.publish

  if (macPublish !== undefined) {
    return asPublishers(macPublish)
  }

  return asPublishers(build?.publish)
}

function macPublishErrors(build, target) {
  const publishers = effectiveMacPublishers(build, target)

  if (
    publishers.length !== 1 ||
    publishers[0]?.provider !== 'github' ||
    publishers[0]?.owner !== 'archits01' ||
    publishers[0]?.repo !== 'oc-hermes'
  ) {
    return [`effective macOS ${target} publisher is not exactly github.com/archits01/oc-hermes`]
  }

  return []
}

try {
  const desktopPackage = JSON.parse(read('apps/desktop/package.json'))
  const build = desktopPackage.build
  const extraResources = JSON.stringify(build?.extraResources ?? [])

  if (!extraResources.includes('default-connection.json')) {
    fail('friend-DMG default connection packaging lost')
  }

  for (const target of ['mac', 'dmg']) {
    for (const issue of macPublishErrors(build, target)) {
      fail(issue)
    }
  }

  if (
    build?.protocols?.[0]?.name !== 'OpenComputer Protocol' ||
    build?.dmg?.title !== 'Install OpenComputer' ||
    build?.win?.legalTrademarks !== 'OpenComputer' ||
    build?.nsis?.shortcutName !== 'OpenComputer' ||
    build?.nsis?.uninstallDisplayName !== 'OpenComputer' ||
    build?.linux?.synopsis !== 'Native desktop shell for OpenComputer.'
  ) {
    fail('desktop package branding lost from a distributable platform target')
  }

  const macOverride = JSON.parse(JSON.stringify(build))
  macOverride.mac = { ...(macOverride.mac ?? {}), publish: { provider: 'generic', url: 'https://invalid.example' } }
  if (macPublishErrors(macOverride, 'dmg').length === 0) {
    fail('macOS publisher checker self-test missed a build.mac.publish override')
  }

  const dmgOverride = JSON.parse(JSON.stringify(build))
  dmgOverride.dmg = { ...(dmgOverride.dmg ?? {}), publish: { provider: 'generic', url: 'https://invalid.example' } }
  if (macPublishErrors(dmgOverride, 'dmg').length === 0) {
    fail('macOS publisher checker self-test missed a build.dmg.publish override')
  }

  const leadingGeneric = JSON.parse(JSON.stringify(build))
  leadingGeneric.publish = [{ provider: 'generic', url: 'https://invalid.example' }, ...asPublishers(build.publish)]
  if (macPublishErrors(leadingGeneric, 'dmg').length === 0) {
    fail('macOS publisher checker self-test missed a leading generic publisher')
  }
} catch (error) {
  fail(`apps/desktop/package.json is not valid JSON: ${error instanceof Error ? error.message : String(error)}`)
}

expectIncludes(
  'apps/desktop/electron/update-remote.ts',
  'archits01/oc-hermes',
  'desktop source updater no longer targets the fork'
)
const macReleaseWorkflow = read('.github/workflows/release-desktop-macos.yml')
for (const issue of releaseWorkflowFailures(macReleaseWorkflow)) {
  fail(issue)
}

const releaseNegativeCases = [
  [
    'tag refs/tags pin',
    source => source.replace('tag_ref="refs/tags/$RELEASE_TAG"', 'tag_ref="refs/heads/$RELEASE_TAG"')
  ],
  [
    'exact packaged updater path',
    source =>
      source.replace(
        'app_update="$app_path/Contents/Resources/app-update.yml"',
        'app_update="$(find "$app_path" -type f -name app-update.yml -print -quit)"'
      )
  ],
  [
    'upload remote peeled-tag recheck',
    source =>
      replaceNth(
        source,
        'remote_peeled="$(git ls-remote origin "refs/tags/$RELEASE_TAG^{}"',
        0,
        'remote_peeled=""'
      )
  ],
  [
    'publish remote peeled-tag recheck',
    source =>
      replaceNth(
        source,
        'remote_peeled="$(git ls-remote origin "refs/tags/$RELEASE_TAG^{}"',
        1,
        'remote_peeled=""'
      )
  ]
]

for (const [name, mutate] of releaseNegativeCases) {
  const mutated = mutate(macReleaseWorkflow)

  if (mutated === macReleaseWorkflow) {
    fail(`release workflow checker self-test fixture was not found: ${name}`)
  } else if (releaseWorkflowFailures(mutated).length === 0) {
    fail(`release workflow checker failed to detect a missing ${name}`)
  }
}

const bundledPlugins = read('apps/desktop/src/contrib/plugins.ts')
expectRegex(
  bundledPlugins,
  /import\.meta\.glob<\{ default: HermesPlugin \}>\('\.\.\/plugins\/\*\/plugin\.\{js,ts,tsx\}', \{ eager: true \}\)/,
  'bundled desktop-plugin discovery lost'
)
expectIncludes('apps/desktop/src/plugins/lmi-dashboard/plugin.tsx', "id: 'lmi-dashboard'", 'LMI dashboard plugin contract lost')

const sessionSource = read('apps/desktop/src/lib/session-source.ts')
const sourceArray = sessionSource.match(/export const MESSAGING_SESSION_SOURCE_IDS\s*=\s*\[([\s\S]*?)\n\]/)

if (!sourceArray) {
  fail('MESSAGING_SESSION_SOURCE_IDS array is missing')
} else {
  const arrayBody = sourceArray[1].replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
  const sourceIds = new Set([...arrayBody.matchAll(/'([^']+)'/g)].map(([, id]) => id))

  for (const id of ['linkedin', 'instagram', 'whatsapp_unipile', 'messenger_unipile']) {
    if (!sourceIds.has(id)) {
      fail(`LMI messaging source missing from MESSAGING_SESSION_SOURCE_IDS: ${id}`)
    }
  }
}

const bots = read('apps/desktop/src/plugins/hermes-bots/plugin.js')
for (const issue of botsMainTabOwnershipFailures(bots)) {
  fail(issue)
}

const ownershipNegativeCases = [
  ['openGroupChat record call', '      recordGroupMainTab(group, close)'],
  ['openGroupChat close cleanup', '          dropGroupMainTab(group)'],
  ['BotsPane revision subscription', '  useValue($groupMainTabsRev)'],
  ['BotsPane fallback gate', '  if (shouldRenderGroupChatInPane(groupChatName) && groupChatMembers.length)']
]

for (const [name, statement] of ownershipNegativeCases) {
  const mutated = bots.replace(statement, `  /* checker self-test removed: ${name} */`)

  if (mutated === bots) {
    fail(`Bots ownership checker self-test fixture was not found: ${name}`)
  } else if (botsMainTabOwnershipFailures(mutated).length === 0) {
    fail(`Bots ownership checker failed to detect a missing ${name}`)
  }
}

if (failures.length > 0) {
  for (const message of failures) {
    console.error(`OpenComputer invariant failed: ${message}`)
  }

  process.exitCode = 1
} else {
  console.log('OpenComputer invariants verified')
}
