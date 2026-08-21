#!/usr/bin/env bash
# Nightly safety net for work done directly in a live OpenComputer checkout.
#
# This creates and pushes a dated autosave branch, then returns the checkout to
# the same file contents. The reset is deliberately mixed: the autosaved
# changes remain in the working tree but the Git index is clean, so the next
# updater cannot mistake the autosave commit for staged operator work.
set -uo pipefail

REPO=${REPO:-/opt/opencomputer-v2}
LOG=${LOG:-/opt/opencomputer-v2-data/logs/oc-autosave.log}
TOKEN_FILE=${TOKEN_FILE:-/opt/opencomputer-v2-data/.gh-token}
mkdir -p "$(dirname "$LOG")"
log(){ printf '%s %s\n' "$(date -u +%FT%TZ)" "$1" >> "$LOG"; }
git_repo(){ git -c "safe.directory=${REPO}" -C "$REPO" "$@"; }

cd "$REPO" || { log "ERROR repo missing"; exit 1; }
if [ -z "$(git_repo status --porcelain)" ]; then log "clean - nothing to save"; exit 0; fi

BR="autosave/vm-$(date -u +%Y%m%d-%H%M)"
CUR="$(git_repo rev-parse --abbrev-ref HEAD)"
N="$(git_repo status --porcelain | wc -l | tr -d ' ')"

git_repo stash push -u -m "autosave-$BR" >/dev/null 2>&1 || { log "ERROR stash failed"; exit 1; }
git_repo branch "$BR" HEAD 2>/dev/null
git_repo stash pop >/dev/null 2>&1 || { log "ERROR stash pop failed - work is in the stash"; exit 1; }

TREE_MSG="autosave: $N uncommitted file(s) from the live VM tree

Captured automatically off $CUR. Not reviewed, not for release - this exists so
work done directly on the box cannot be lost to a branch switch."
git_repo add -A >/dev/null 2>&1
if git_repo -c user.name='oc-autosave' -c user.email='autosave@opencomputer.local' \
     commit -q -m "$TREE_MSG" 2>>"$LOG"; then
  git_repo branch -f "$BR" HEAD >/dev/null 2>&1
  # Mixed reset keeps the autosaved files in place but clears the index.
  git_repo reset -q HEAD~1
  log "saved $N file(s) -> $BR"
else
  # `git add -A` must never strand the live checkout with a dirty index when
  # a hook or filesystem error rejects the autosave commit.
  git_repo reset -q HEAD >/dev/null 2>&1 || true
  log "ERROR commit failed"; exit 1
fi

if [ -r "$TOKEN_FILE" ]; then
  ASKPASS="$(mktemp "${TMPDIR:-/tmp}/oc-autosave-askpass.XXXXXX")" || {
    log "WARN could not create askpass helper - branch exists locally"
    exit 0
  }
  trap 'rm -f -- "$ASKPASS"' EXIT
  printf '%s\n' \
    '#!/bin/sh' \
    'case "$1" in' \
    '  Username*) printf "%s\\n" x-access-token ;;' \
    '  Password*) head -n 1 -- "$OC_AUTOSAVE_TOKEN_FILE" ;;' \
    '  *) exit 1 ;;' \
    'esac' >"$ASKPASS"
  chmod 700 "$ASKPASS"
  if OC_AUTOSAVE_TOKEN_FILE="$TOKEN_FILE" GIT_ASKPASS="$ASKPASS" \
       GIT_ASKPASS_REQUIRE=force GIT_TERMINAL_PROMPT=0 \
       git_repo push -q origin "$BR:$BR" 2>>"$LOG"; then
    log "pushed $BR"
  else
    log "WARN push failed - branch exists locally"
  fi
else
  log "no token at $TOKEN_FILE - saved locally only"
fi
