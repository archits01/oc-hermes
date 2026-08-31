#!/usr/bin/env bash
# oc-update-app.sh — update the OpenComputer Mac app from source, no DMG.
#
# This is the Hermes update model, minus the parts we do not need: pull this
# checkout from our own fork, rebuild the Electron app, swap it into
# /Applications. No Python venv and no local engine, because the agent runs on
# the VM — connection.json keeps this app a thin client into it.
#
#   ~/hermes-agent/scripts/oc-update-app.sh
#   ~/hermes-agent/scripts/oc-update-app.sh --force-rebuild   # rebuild without pulling
#   ~/hermes-agent/scripts/oc-update-app.sh --unattended      # nightly launchd job
#
# --unattended NEVER interrupts a working session: if OpenComputer is running it
# exits immediately, before pulling or building, so an open app is a true no-op
# rather than a wasted 4am build. It also skips the relaunch — a scheduled job
# should not pop the app open overnight.
#
# What it deliberately REFUSES to do: touch a dirty or wrong-branch checkout.
# This tree is shared with other agent sessions; their uncommitted work is not
# ours to stash. It also builds BEFORE touching /Applications, so a failed build
# leaves the installed app exactly as it was.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH=oc-branding
APP=/Applications/OpenComputer.app
BUILT="$REPO/apps/desktop/release/mac-arm64/OpenComputer.app"
FORCE=0
UNATTENDED=0
for arg in "$@"; do
    case "$arg" in
        --force-rebuild) FORCE=1 ;;
        --unattended)    UNATTENDED=1 ;;
    esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mABORT: %s\033[0m\n' "$*" >&2; exit 1; }

cd "$REPO" || die "cannot cd $REPO"

# --- 0. unattended: never interrupt a working session -----------------------
# Checked BEFORE pull/build so an open app costs nothing at all. If he keeps the
# app open permanently the nightly job simply never swaps, which the log records
# and the manual invocation overrides.
if [ "$UNATTENDED" -eq 1 ] && pgrep -x OpenComputer >/dev/null 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S')  OpenComputer is running — skipping (run it manually to update now)."
    exit 0
fi

# --- 1. never clobber a shared tree ----------------------------------------
say "Checking the checkout is safe to update"
[ "$(git rev-parse --abbrev-ref HEAD)" = "$BRANCH" ] \
    || die "on branch '$(git rev-parse --abbrev-ref HEAD)', expected $BRANCH. Not switching branches for you."
DIRTY=$(git status --porcelain --untracked-files=no)
if [ -n "$DIRTY" ]; then
    printf '%s\n' "$DIRTY" | sed 's/^/    /'
    die "modified tracked files above. Another session may be working here — commit or revert them first."
fi
git diff --cached --quiet || die "staged changes present. Commit or reset them first."

OLD=$(git rev-parse --short HEAD)

# --- 2. fast-forward only ---------------------------------------------------
if [ "$FORCE" -eq 0 ]; then
    say "Pulling $BRANCH"
    git fetch --quiet origin "$BRANCH" || die "git fetch failed (network?)"
    if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ]; then
        echo "    Already up to date at $OLD."
        echo "    (Use --force-rebuild to rebuild the app anyway.)"
        exit 0
    fi
    git merge --ff-only "origin/$BRANCH" \
        || die "not a fast-forward — this checkout has diverged from origin. Needs a human."
fi
NEW=$(git rev-parse --short HEAD)
echo "    $OLD -> $NEW"

# --- 3. build BEFORE touching the installed app -----------------------------
say "Building the app (this takes several minutes)"
( cd apps/desktop && npm run pack ) || die "build failed. Your installed app was NOT touched."
[ -x "$BUILT/Contents/MacOS/OpenComputer" ] || die "build produced no app at $BUILT"

# --- 4. swap it in ----------------------------------------------------------
if pgrep -x OpenComputer >/dev/null 2>&1; then
    say "Quitting OpenComputer"
    osascript -e 'quit app "OpenComputer"' >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do pgrep -x OpenComputer >/dev/null 2>&1 || break; sleep 1; done
    pgrep -x OpenComputer >/dev/null 2>&1 \
        && die "OpenComputer is still running. Quit it manually and re-run — refusing to force-kill."
fi

if [ -d "$APP" ]; then
    say "Backing up the current app"
    rm -rf "$APP".backup-* 2>/dev/null || true      # keep exactly one backup, not a pile
    ditto "$APP" "$APP.backup-$OLD" && echo "    $APP.backup-$OLD"
fi

say "Installing"
rm -rf "$APP" && ditto "$BUILT" "$APP" || die "install failed — restore from $APP.backup-$OLD"
xattr -cr "$APP" 2>/dev/null || true                # unsigned build; clear quarantine

# --- 5. prove it, don't assume ---------------------------------------------
say "Verifying"
echo "    version:     $(defaults read "$APP/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null)"
STAMP="$APP/Contents/Resources/app.asar.unpacked/dist/build-stamp"
[ -f "$STAMP" ] && echo "    build stamp: $(cat "$STAMP")"
echo "    commit:      $OLD -> $NEW"

CONN="$HOME/Library/Application Support/OpenComputer/connection.json"
if [ -f "$CONN" ] && grep -q '"mode"[[:space:]]*:[[:space:]]*"remote"' "$CONN"; then
    echo "    connection:  intact, still remote (thin client into the VM)"
else
    printf '\033[31m    connection:  MISSING OR NOT REMOTE — the app may ask for API keys.\033[0m\n'
    printf '    Expected %s\n' "$CONN"
fi

if [ "$UNATTENDED" -eq 1 ]; then
    echo "Updated to $NEW. Not relaunching (unattended); it will start updated next time you open it."
else
    say "Relaunching"
    open -a "$APP"
fi
echo "Done. Agent updates still arrive from the VM automatically; this only refreshed the Mac app."
