#!/usr/bin/env bash
# fork-sync: merge upstream NousResearch/hermes-agent into this fork's oc-branding branch,
# preserving the OpenComputer/Portal branding. Pushes if the merge is clean; stops with a
# clear message on conflict so a human can resolve while keeping the branding.
#
# Usage:  scripts/fork-sync.sh
# Schedule it however you like — a local cron, or the GitHub Actions wrapper (see docs at bottom).
set -euo pipefail

BRANCH="${FORK_SYNC_BRANCH:-oc-branding}"
UPSTREAM_URL="https://github.com/NousResearch/hermes-agent.git"

echo "[fork-sync] fetching origin/$BRANCH"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH" || true

echo "[fork-sync] fetching upstream main"
git remote add upstream "$UPSTREAM_URL" 2>/dev/null || git remote set-url upstream "$UPSTREAM_URL"
git fetch --no-tags upstream main

before="$(git rev-parse HEAD)"
echo "[fork-sync] merging upstream/main into $BRANCH"
if git merge --no-edit upstream/main; then
  after="$(git rev-parse HEAD)"
  if [ "$before" = "$after" ]; then
    echo "[fork-sync] already up to date with upstream — nothing to push."
  else
    git push origin "$BRANCH"
    echo "[fork-sync] merged upstream/main and pushed $BRANCH."
  fi
else
  git merge --abort || true
  echo "[fork-sync] CONFLICT merging upstream/main into $BRANCH." >&2
  echo "[fork-sync] Resolve manually, KEEPING the OpenComputer/Portal branding:" >&2
  echo "    git merge upstream/main   # fix conflicts" >&2
  echo "    git push origin $BRANCH" >&2
  exit 1
fi

# --- To run this weekly in the cloud (GitHub Actions), add a workflow that checks out
# --- oc-branding and runs this script. Adding files under .github/workflows/ needs a token
# --- with the `workflow` scope: `gh auth refresh -h github.com -s workflow`, or paste the
# --- workflow via the GitHub web UI (Actions -> New workflow).
