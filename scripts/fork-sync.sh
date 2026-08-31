#!/usr/bin/env bash
# Retired upstream-sync entrypoint.
#
# This script used to merge and push oc-branding directly. That bypassed the
# candidate branch, structural OpenComputer/LMI invariants, and conflict
# preservation in .github/workflows/fork-sync.yml. It must not be scheduled or
# run as a production update path. The GitHub workflow is the single supported
# scheduled upstream-sync mechanism.
set -euo pipefail

printf '%s\n' \
  '[fork-sync] retired: direct local merge-and-push is unsafe.' \
  '[fork-sync] use the Fork Sync GitHub Action; it creates a guarded candidate branch and preserves conflict evidence.' >&2
exit 2
