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

const requiredFiles = [
  'scripts/fork-sync.sh',
  'optional-mcps/sarvam-voice/server/main.py',
  'optional-mcps/unipile/server/src/mcp_server_unipile_extended/server.py',
  'tools/media_store.py',
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

for (const relative of requiredFiles) {
  if (!fs.existsSync(filePath(relative))) {
    fail(`missing required file: ${relative}`)
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
  'FORK_CONTROL_PATHS=(.github/workflows .github/scripts/assert-opencomputer-invariants.mjs)',
  'scheduled sync does not preserve its workflow and invariant-checker control plane'
)
expectTextIncludes(
  forkSyncWorkflow,
  'git checkout HEAD^1 -- "${FORK_CONTROL_PATHS[@]}"',
  'clean upstream control-plane changes are not preserved before candidate push'
)

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
