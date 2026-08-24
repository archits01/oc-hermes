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

let paths

try {
  paths = execFileSync('git', ['diff', '--name-only', `${base}...${target}`], {
    encoding: 'utf8'
  })
    .split('\n')
    .filter(Boolean)
} catch (error) {
  const detail = error instanceof Error ? error.message : String(error)
  console.error(`OpenComputer boundary check could not compare ${base} to ${target}: ${detail}`)
  process.exit(2)
}

const violations = paths.filter(path => !allowed.some(pattern => pattern.test(path)))

if (violations.length > 0) {
  console.error('OpenComputer boundary rejected non-client fork changes:')
  for (const path of violations) {
    console.error(`  ${path}`)
  }
  process.exit(1)
}

console.log(`OpenComputer boundary verified: ${paths.length} fork-owned client/release paths`)
