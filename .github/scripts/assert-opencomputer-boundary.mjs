#!/usr/bin/env node
import { execFileSync } from 'node:child_process'

const [base = 'upstream/main', target = 'HEAD'] = process.argv.slice(2)
const allowed = [
  /^\.github\//,
  /^apps\/(?:bootstrap-installer|desktop)\//,
  /^web\//,
  /^website\//,
  /^ui-tui\//,
  /^hermes_cli\/default_soul\.py$/,
  // Upstream has trailing blank lines here; retain the pre-existing clean form
  // so a strict diff check cannot be defeated by an upstream whitespace defect.
  /^tests\/hermes_cli\/test_model_search\.py$/,
  /^scripts\/(?:desktop-update\/|fork-sync\.sh$|install\.ps1$|install\.sh$|oc-update-app\.sh$)/,
  /^package-lock\.json$/,
  /^docs\/upstream-reconciliation-2026-08-24\.md$/
]

function violationsFor(paths) {
  return paths.filter(path => !allowed.some(pattern => pattern.test(path)))
}

if (process.argv.includes('--self-test')) {
  // --no-renames below emits both sides of a move. This fixture represents a
  // server file renamed into an otherwise-allowed client directory: the old
  // gateway path must still fail the ownership check.
  const syntheticRename = ['gateway/run.py', 'apps/desktop/src/lib/moved-gateway.ts']
  const violations = violationsFor(syntheticRename)

  if (violations.length !== 1 || violations[0] !== 'gateway/run.py') {
    console.error('OpenComputer boundary self-test failed to reject a renamed server path')
    process.exit(1)
  }

  console.log('OpenComputer boundary self-test verified rename-source rejection')
  process.exit(0)
}

let paths

try {
  // Do not let Git collapse a rename into its destination path. A core file
  // moved under an allowed client prefix must expose both paths so the source
  // side is rejected by the boundary.
  paths = execFileSync('git', ['diff', '--no-renames', '--name-only', `${base}...${target}`], {
    encoding: 'utf8'
  })
    .split('\n')
    .filter(Boolean)
} catch (error) {
  const detail = error instanceof Error ? error.message : String(error)
  console.error(`OpenComputer boundary check could not compare ${base} to ${target}: ${detail}`)
  process.exit(2)
}

const violations = violationsFor(paths)

if (violations.length > 0) {
  console.error('OpenComputer boundary rejected non-client fork changes:')
  for (const path of violations) {
    console.error(`  ${path}`)
  }
  process.exit(1)
}

console.log(`OpenComputer boundary verified: ${paths.length} fork-owned client/release paths`)
