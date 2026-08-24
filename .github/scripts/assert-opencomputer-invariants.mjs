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
  'node .github/scripts/assert-opencomputer-invariants.mjs',
  'scheduled sync no longer runs the executable invariant checker'
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

const desktopMain = read('apps/desktop/electron/main.ts')
expectRegex(
  desktopMain,
  /seedDefaultConnectionIfMissing\(\{[\s\S]*?default-connection\.json/,
  'friend-DMG default connection seed lost'
)

try {
  const desktopPackage = JSON.parse(read('apps/desktop/package.json'))
  const extraResources = JSON.stringify(desktopPackage.build?.extraResources ?? [])

  if (!extraResources.includes('default-connection.json')) {
    fail('friend-DMG default connection packaging lost')
  }
} catch (error) {
  fail(`apps/desktop/package.json is not valid JSON: ${error instanceof Error ? error.message : String(error)}`)
}

expectIncludes(
  'apps/desktop/electron/packaged-app-updater.ts',
  'archits01/oc-hermes',
  'packaged updater no longer targets the fork'
)
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
const recordGroupMainTab = bots.match(/function recordGroupMainTab\(group, close\) \{([\s\S]*?)\n\}/)?.[1] ?? ''
const dropGroupMainTab = bots.match(/function dropGroupMainTab\(group\) \{([\s\S]*?)\n\}/)?.[1] ?? ''
const paneBody = bots.match(/function BotsPane\(\) \{([\s\S]*?)\n\}\n\n\/\/ ── plugin/)?.[1] ?? ''

expectRegex(recordGroupMainTab, /groupChatMainTabs\.set\(group, close\)/, 'Bots main-tab record mutator lost its map write')
expectRegex(recordGroupMainTab, /\$groupMainTabsRev\.set\(\$groupMainTabsRev\.get\(\) \+ 1\)/, 'Bots main-tab record mutator lost its revision bump')
expectRegex(dropGroupMainTab, /groupChatMainTabs\.delete\(group\)/, 'Bots main-tab drop mutator lost its map delete')
expectRegex(dropGroupMainTab, /\$groupMainTabsRev\.set\(\$groupMainTabsRev\.get\(\) \+ 1\)/, 'Bots main-tab drop mutator lost its revision bump')
expectRegex(
  bots,
  /function shouldRenderGroupChatInPane\(group\) \{\n  return Boolean\(group && !groupChatMainTabs\.has\(group\)\)\n\}/,
  'Bots fallback-pane ownership gate lost'
)
expectRegex(paneBody, /useValue\(\$groupMainTabsRev\)/, 'BotsPane no longer subscribes to main-tab ownership changes')
expectRegex(
  paneBody,
  /if \(shouldRenderGroupChatInPane\(groupChatName\) && groupChatMembers\.length\)/,
  'BotsPane no longer invokes the fallback-pane ownership gate'
)
expectRegex(bots, /recordGroupMainTab\(group, close\)/, 'openGroupChat no longer records main-tab ownership')
expectRegex(bots, /dropGroupMainTab\(group\)/, 'main-tab ownership is never released')

if (failures.length > 0) {
  for (const message of failures) {
    console.error(`OpenComputer invariant failed: ${message}`)
  }

  process.exitCode = 1
} else {
  console.log('OpenComputer invariants verified')
}
