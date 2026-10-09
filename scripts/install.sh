<<<<<<< HEAD
#!/bin/bash
# ============================================================================
# OpenComputer Installer
# ============================================================================
# Installation script for Linux, macOS, and Android/Termux.
# Uses uv for desktop/server installs and Python's stdlib venv + pip on Termux.
=======
#!/usr/bin/env bash
# Hermes Agent bootstrap: clone, acquire uv/Python, then hand the checkout to
# the same completion an update runs -- command publication, product builds and
# post-build maintenance -- so a fresh install and a finished update land in one
# state. Heavy dependencies (tool binaries, browsers, node) are pm's job:
# `hermes pm install`.
>>>>>>> upstream/main
#
# Stage protocol kept for Hermes-Setup:
#   --manifest            print the stage list as JSON
#   --stage NAME [--json] run one stage
#   --non-interactive     skip stages that need input
#   --include-desktop     build the desktop app too (products stage)
#   --verbose             stream every child command's output (the default
#                         off a terminal and in CI)
set -u

# Prevent uv from discovering config files (uv.toml, pyproject.toml) from the
# wrong user's home directory when running under sudo -u <user>.  See #21269.
# pm's own venv sync re-isolates (pm/environment.py), so this bootstrap
# hygiene can't break the locked sync the way it used to before pm owned it.
export UV_NO_CONFIG=1

<<<<<<< HEAD
# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# Configuration
REPO_URL_SSH="git@github.com:archits01/hermes-agent.git"
REPO_URL_HTTPS="https://github.com/archits01/hermes-agent.git"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# INSTALL_DIR is resolved AFTER arg parsing and OS detection so we can pick an
# FHS-style layout for root installs.  Track whether the user gave us an
# explicit directory — if so we never override it.
if [ -n "${HERMES_INSTALL_DIR:-}" ]; then
    INSTALL_DIR="$HERMES_INSTALL_DIR"
    INSTALL_DIR_EXPLICIT=true
else
    INSTALL_DIR=""
    INSTALL_DIR_EXPLICIT=false
fi
PYTHON_VERSION="3.11"
NODE_VERSION="26"

# FHS-style root install layout (set by resolve_install_layout when applicable):
#   code at /usr/local/lib/hermes-agent, command at /usr/local/bin/hermes,
#   data still at /root/.hermes (HERMES_HOME).  Matches Claude Code / Codex CLI
#   and keeps Docker bind-mounted /root/ volumes lean.
ROOT_FHS_LAYOUT=false
DETECTED_BROWSER_EXECUTABLE=""

# Options
USE_VENV=true
RUN_SETUP=true
SKIP_BROWSER=false
SKIP_COMPUTER_USE=false
NO_SKILLS=false
BRANCH="oc-branding"
=======
REPO_URL="${HERMES_REPO_URL:-https://github.com/NousResearch/hermes-agent.git}"
BRANCH="main"
>>>>>>> upstream/main
INSTALL_COMMIT=""
INSTALL_DIR="${HERMES_INSTALL_DIR:-}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
STAGE=""
WANT_MANIFEST=false
JSON=false
NON_INTERACTIVE=false
INCLUDE_DESKTOP=false
VERBOSE=false
SKIP_BROWSER=false
SKIP_COMPUTER_USE=false
INSTALL_LADDER=false
INSTALL_STARTED=0
INSTALL_RUN_ID=""
FAILURE_CLASS=""
INSTALL_SIGNAL=""
INSTALL_IN_STAGE=false

while [ $# -gt 0 ]; do
    case "$1" in
        --branch|-Branch|--commit|-Commit|--dir|--hermes-home|-HermesHome|--stage|-Stage)
            option="$1"
            if [ $# -lt 2 ] || [ -z "$2" ] || [[ "$2" == -* ]]; then
                printf '%s needs a value\n' "$option" >&2
                exit 2
            fi
            case "$option" in
                --branch|-Branch) BRANCH="$2" ;;
                --commit|-Commit) INSTALL_COMMIT="$2" ;;
                --dir) INSTALL_DIR="$2" ;;
                --hermes-home|-HermesHome) HERMES_HOME="$2" ;;
                --stage|-Stage) STAGE="$2" ;;
            esac
            shift 2 ;;
        --manifest|-Manifest) WANT_MANIFEST=true; shift ;;
        --json|-Json) JSON=true; shift ;;
        --non-interactive|-NonInteractive) NON_INTERACTIVE=true; shift ;;
        --skip-setup) NON_INTERACTIVE=true; shift ;;
        --skip-browser|--no-playwright|-SkipBrowser) SKIP_BROWSER=true; shift ;;
        --skip-computer-use|-SkipComputerUse) SKIP_COMPUTER_USE=true; shift ;;
        --include-desktop|-IncludeDesktop) INCLUDE_DESKTOP=true; shift ;;
        --verbose|-Verbose) VERBOSE=true; shift ;;
        -h|--help)
<<<<<<< HEAD
            echo "OpenComputer Installer"
            echo ""
            echo "Usage: install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --no-venv      Don't create virtual environment"
            echo "  --skip-setup   Skip interactive setup wizard"
            echo "  --skip-browser Skip Playwright/Chromium install (browser tools won't work)"
            echo "  --skip-computer-use  Skip the cua-driver (Computer Use) install"
            echo "  --no-skills    Start with a blank slate — seed no bundled skills, and"
            echo "                   write \$HERMES_HOME/.no-bundled-skills so future"
            echo "                   'hermes update' runs never inject bundled skills either"
            echo "  --branch NAME  Git branch to install (default: main)"
            echo "  --commit SHA   Pin checkout to a specific commit after clone/update"
            echo "                   (ignored when it would roll an existing install back)"
            echo "  --force-commit Apply --commit even if it rolls the install backwards"
            echo "  --manifest     Print desktop bootstrap stage manifest as JSON"
            echo "  --stage NAME   Run one desktop bootstrap stage"
            echo "  --json         Print a JSON result frame for --stage"
            echo "  --non-interactive  Skip stages that require user input"
            echo "  --include-desktop  Also build the desktop app (apps/desktop -> OpenComputer.app)"
            echo "  --dir PATH     Installation directory"
            echo "                   default (non-root):  ~/.hermes/hermes-agent"
            echo "                   default (root, Linux): /usr/local/lib/hermes-agent"
            echo "  --hermes-home PATH  Data directory (default: ~/.hermes, or \$HERMES_HOME)"
            echo "  -h, --help     Show this help"
            echo ""
            echo "Notes:"
            echo "  When running as root on Linux, Hermes installs the code under"
            echo "  /usr/local/lib/hermes-agent and links the command into"
            echo "  /usr/local/bin/hermes (FHS layout — matches Claude Code / Codex CLI)."
            echo "  Data, config, sessions, and logs still live in \$HERMES_HOME"
            echo "  (default /root/.hermes).  This keeps Docker bind-mounted volumes"
            echo "  small and ensures the command is on PATH for all shells."
            echo "  Existing installs at \$HERMES_HOME/hermes-agent are preserved in-place."
            echo "  --ensure DEPS  Install only specified deps (comma-separated)"
            echo "                   Supported: node, browser, ripgrep, ffmpeg"
            echo "                   Does NOT clone repo or create venv"

            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
=======
            echo "Usage: install.sh [--branch NAME] [--commit SHA] [--dir PATH]"
            echo "                  [--hermes-home PATH]"
            echo "                  [--manifest] [--stage NAME] [--json]"
            echo "                  [--non-interactive] [--include-desktop] [--verbose]"
            echo "                  [--skip-browser] [--skip-computer-use]"
            echo
            echo "  --skip-browser  Do not install the browser tools (agent-browser + Chromium)."
            echo "                  Alias: --no-playwright. Remembered by later"
            echo "                  installs and 'hermes update'; undo with 'hermes pm install agent-browser'."
            echo "  --skip-computer-use"
            echo "                  Do not install the computer-use driver (cua-driver). Remembered"
            echo "                  the same way; undo with 'hermes pm install cua-driver'."
            exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
>>>>>>> upstream/main
    esac
done

INSTALL_DIR="${INSTALL_DIR:-$HERMES_HOME/hermes-agent}"
export HERMES_HOME

INSTALL_LOG="$HERMES_HOME/logs/install.log"

# Same glyphs as the pre-pm installer. Colour only on a terminal, so CI
# transcripts and the Hermes-Setup driver read plain text.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[0;31m' C_GREEN=$'\033[0;32m' C_YELLOW=$'\033[0;33m'
    C_CYAN=$'\033[0;36m' C_MAGENTA=$'\033[0;35m' C_BOLD=$'\033[1m'
    C_DIM=$'\033[2m' C_NC=$'\033[0m'
else
    C_RED="" C_GREEN="" C_YELLOW="" C_CYAN="" C_MAGENTA="" C_BOLD="" C_DIM="" C_NC=""
fi

log() { printf '%s→%s %s\n' "$C_CYAN" "$C_NC" "$1"; }
log_success() { printf '%s✓%s %s\n' "$C_GREEN" "$C_NC" "$1"; }
log_warn() { printf '%s⚠%s %s\n' "$C_YELLOW" "$C_NC" "$1"; }
log_error() { printf '%s✗%s %s\n' "$C_RED" "$C_NC" "$1" >&2; }
# $2: closed failure class for the local install receipt (write_install_receipt).
fail() { STAGE_REASON="$1"; FAILURE_CLASS="${2:-other}"; log_error "$1"; exit 1; }

# One local receipt per full-ladder run, under the profile's shared-metrics dir.
# Only closed tokens and two timestamps (never STAGE_REASON, paths or URLs); the
# installer never sends anything. Hermes reports it as hermes.install.run on a
# later start only while shared metrics collection is on, and deletes it
# unreported when collection is off. Best effort: never fails the install.
write_install_receipt() {
    # $1 outcome, $2 failed stage, $3 failure class. One file per run: the first writer wins.
    # A failed write (full disk) removes its partial temp file: the reader never sees dotfiles.
    {
        local dir="$HERMES_HOME/telemetry/shared_metrics/pending_installs" id="$INSTALL_RUN_ID"
        [ -n "$id" ] && [ ! -e "$dir/$id.json" ] && mkdir -p "$dir" && {
            printf '{"id":"%s","installer":"install_sh","outcome":"%s","failed_stage":"%s","failure_class":"%s","started_at":%s,"finished_at":%s}\n' \
                "$id" "$1" "$2" "$3" "$INSTALL_STARTED" "$(date +%s)" > "$dir/.$id.tmp" \
                && mv -f "$dir/.$id.tmp" "$dir/$id.json" \
                || rm -f "$dir/.$id.tmp"
        }
    } >/dev/null 2>&1 || :
}

# Ladder signal handling. The traps only note the signal while a stage owns the
# terminal: bash runs a trap once the foreground stage returns, and that stage's
# result decides. A stage whose child caught Ctrl-C and succeeded (setup's
# provider picker: "Provider setup skipped.") lets the ladder carry on, as bash's
# own wait-and-cooperative-exit did before these traps existed. A stage that
# failed after the signal is recorded as `interrupted`, then the signal is
# re-raised so the exit status is the signal's. A TERM or HUP sent to the
# installer alone (not its process group) takes effect when the running stage
# returns, and the receipt names the stage the run stopped before. The stage
# stays in the foreground because a background stage's children ignore Ctrl-C
# under bash 3.2 (macOS /bin/bash), even after `trap -`.
install_signal() {
    # $1 signal name. Outside a stage (platform check, banner, between stages) stop now.
    INSTALL_SIGNAL="$1"
    [ "$INSTALL_IN_STAGE" = true ] || install_interrupted
}

install_interrupted() {
    # Record the stage the signal cut off, then die by that signal (exit code if it is blocked).
    local stage="${s:-prerequisites}" sig="${INSTALL_SIGNAL:-INT}" code=130
    case "$sig" in HUP) code=129 ;; TERM) code=143 ;; esac
    write_install_receipt failed "${stage//-/_}" interrupted
    trap - INT TERM HUP EXIT
    kill -s "$sig" "$$" 2>/dev/null
    exit "$code"
}

# Inside a ladder stage: a child the signal killed ends the stage like bash's own
# cooperative exit (its status is the signal's, 128+N); a child that caught the
# signal keeps its own result, and the stage's receipt reads `interrupted`.
stage_signal() {
    # $1 status of the command the signal arrived during, $2 this signal's 128+N.
    STAGE_INTERRUPTED=true
    [ "$1" -ne "$2" ] || exit "$2"
}

print_banner() {
<<<<<<< HEAD
    echo ""
    echo -e "${MAGENTA}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│              ⚕ OpenComputer Installer                   │"
    echo "├─────────────────────────────────────────────────────────┤"
    echo "│  Your always-on AI workspace.                           │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
=======
    printf '\n%s%s' "$C_MAGENTA" "$C_BOLD"
    printf '%s\n' "┌─────────────────────────────────────────────────────────┐"
    printf '%s\n' "│             ☤ Hermes Agent Installer                    │"
    printf '%s\n' "├─────────────────────────────────────────────────────────┤"
    printf '%s\n' "│  An open source AI agent by Nous Research.              │"
    printf '%s\n' "└─────────────────────────────────────────────────────────┘"
    printf '%s\n' "$C_NC"
>>>>>>> upstream/main
}

# Interactive runs collapse child-process output (git, uv, pm, the builds)
# into one status line. CI, --verbose and a non-terminal stdout -- the
# Hermes-Setup --json driver, E2E transcripts -- keep the full stream those
# readers parse.
quiet_output() {
    [ "$VERBOSE" = true ] && return 1
    if [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ] || [ -n "${HERMES_INSTALL_VERBOSE:-}" ]; then
        return 1
    fi
    [ -t 1 ]
}

status_line() {
    local text="  $1" width=$(( $2 - 1 ))
    [ "${#text}" -le "$width" ] || text="${text:0:width}"
    printf '\r\033[K%s%s%s' "$C_DIM" "$text" "$C_NC"
}

# run_logged [--may-fail] LABEL CMD...: run CMD. Quiet mode shows LABEL with
# CMD's latest output line rewritten in place, appends everything to
# $INSTALL_LOG and, on failure, prints the tail and the log path; --may-fail
# is for probes whose failure the caller handles (no report). Otherwise LABEL
# is logged and the output streams untouched. Returns CMD's exit status.
run_logged() {
    local may_fail=false
    if [ "$1" = --may-fail ]; then may_fail=true; shift; fi
    local label="$1"; shift
    if ! quiet_output || ! { mkdir -p "${INSTALL_LOG%/*}" && : >> "$INSTALL_LOG"; } 2>/dev/null; then
        log "$label"
        "$@"
        return
    fi
    local start cols rc line shown
    start=$(( $(wc -l < "$INSTALL_LOG") + 1 ))
    cols="$(tput cols 2>/dev/null)" || cols=80
    [ "${cols:-0}" -gt 20 ] 2>/dev/null || cols=80
    printf '==> %s (%s)\n' "$label" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$INSTALL_LOG"
    status_line "$label" "$cols"
    # stdin closed: under `curl | bash` it is the script itself, and nothing
    # run here may prompt behind a status line.
    "$@" </dev/null 2>&1 | {
        while IFS= read -r line || [ -n "$line" ]; do
            line="${line%$'\r'}"
            printf '%s\n' "$line" >&3
            # git and uv redraw progress with bare CRs; show the newest.
            shown="${line##*$'\r'}"
            [ -z "$shown" ] || status_line "$label: $shown" "$cols"
        done
    } 3>>"$INSTALL_LOG"
    rc=${PIPESTATUS[0]}
    printf '\r\033[K'
    if [ "$rc" -ne 0 ] && [ "$may_fail" = false ]; then
        log_error "$label failed (exit $rc). Last output:"
        tail -n +"$(( start + 1 ))" "$INSTALL_LOG" | tail -n 20 | sed 's/^/    /' >&2
        printf '    full log: %s\n' "$INSTALL_LOG" >&2
    fi
    return "$rc"
}

# --- BEGIN GENERATED: bootstrap pins (scripts/gen-bootstrap-pins.py) ---
# Derived from pm/lock.json. DO NOT EDIT BY HAND:
# run scripts/gen-bootstrap-pins.py after a pin bump.
UV_PIN_VERSION="0.12.3"

# Sets UV_PIN_URL + UV_PIN_SHA256 for a <os>-<arch> target key.
uv_bootstrap_pin() {
    case "$1" in
        linux-x64)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-x86_64-unknown-linux-gnu.tar.gz"
            UV_PIN_MIRROR="https://hermes-assets.nousresearch.com/upstream/sha256/600cf9a742aca00d292673b16b5acffaa7b8c269a364ad0c2e79498dcb1fe101"
            UV_PIN_SHA256="600cf9a742aca00d292673b16b5acffaa7b8c269a364ad0c2e79498dcb1fe101"
            ;;
        linux-arm64)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-unknown-linux-gnu.tar.gz"
            UV_PIN_MIRROR="https://hermes-assets.nousresearch.com/upstream/sha256/bb66cb52e7b1823aed1183630d8d8e5c958840d584a4c55ec10a4cfc168dcca2"
            UV_PIN_SHA256="bb66cb52e7b1823aed1183630d8d8e5c958840d584a4c55ec10a4cfc168dcca2"
            ;;
        linux-x64-musl)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-x86_64-unknown-linux-musl.tar.gz"
            UV_PIN_MIRROR="https://hermes-assets.nousresearch.com/upstream/sha256/0643b9fb8c9fb27458e709ce6ff939695013c41975ff7b02d3f3b138d8d4bdb3"
            UV_PIN_SHA256="0643b9fb8c9fb27458e709ce6ff939695013c41975ff7b02d3f3b138d8d4bdb3"
            ;;
        linux-arm64-musl)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-unknown-linux-musl.tar.gz"
            UV_PIN_MIRROR="https://hermes-assets.nousresearch.com/upstream/sha256/fa513fca1eb2913334c944fe9adbdd410274a1cbe8dd05d03699a9eb85311d4e"
            UV_PIN_SHA256="fa513fca1eb2913334c944fe9adbdd410274a1cbe8dd05d03699a9eb85311d4e"
            ;;
        darwin-x64)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-x86_64-apple-darwin.tar.gz"
            UV_PIN_MIRROR="https://hermes-assets.nousresearch.com/upstream/sha256/4c9f52262a14da336e4a42ed24992d12d0c956acde87619e4611d321dffa602b"
            UV_PIN_SHA256="4c9f52262a14da336e4a42ed24992d12d0c956acde87619e4611d321dffa602b"
            ;;
        darwin-arm64)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-apple-darwin.tar.gz"
            UV_PIN_MIRROR="https://hermes-assets.nousresearch.com/upstream/sha256/546f7f8a6c70ff13a3a9d2bc958db3427298cebf3e0cb756f9177133b7068843"
            UV_PIN_SHA256="546f7f8a6c70ff13a3a9d2bc958db3427298cebf3e0cb756f9177133b7068843"
            ;;
        *)
            UV_PIN_URL=""
            UV_PIN_SHA256=""
            return 1
            ;;
    esac
}
# --- END GENERATED: bootstrap pins ---

uv_bootstrap_target() {
    # Map this host to a pm/lock.json target key (<os>-<arch>).
    local _arch
    case "$(uname -m)" in
        arm64|aarch64) _arch="arm64" ;;
        x86_64|amd64)  _arch="x64" ;;
        *) return 1 ;;
    esac
    case "$(uname -s)" in
        Linux)
            # Same precedence as pm/store.py::_is_musl_libc: the native
            # userland's ELF interpreter decides; ldd and a musl loader on
            # disk are fallbacks only (a glibc host may carry musl as a
            # secondary toolchain, and minimal musl roots may lack ldd).
            local _libc="" _probe _head
            for _probe in /bin/sh /bin/ls; do
                _head="$(head -c 8192 "$_probe" 2>/dev/null | LC_ALL=C tr -d '\000')" || continue
                [[ "$_head" == $'\x7f'ELF* ]] || continue
                case "$_head" in
                    *ld-musl-*) _libc="musl"; break ;;
                    *ld-linux*) _libc="glibc"; break ;;
                esac
            done
            if [[ -z "$_libc" ]]; then
                _libc="$(ldd --version 2>&1 || true)"
                _libc="${_libc,,}"
            fi
            if [[ "$_libc" == *musl* ]]; then
                echo "linux-$_arch-musl"
            elif [[ "$_libc" == *glibc* || "$_libc" == *"gnu libc"* || "$_libc" == *"gnu c library"* ]]; then
                echo "linux-$_arch"
            elif compgen -G '/lib/ld-musl-*.so.1' >/dev/null; then
                echo "linux-$_arch-musl"
            else
                echo "linux-$_arch"
            fi
            ;;
        Darwin) echo "darwin-$_arch" ;;
        *) return 1 ;;
    esac
}

# Provision uv for this host from the pinned pm/lock.json artifact. Stages
# the EXACT artifact pm itself uses into the same store slot
# (<store>/uv-<version>-<target>/, the store pm's store_root() resolves),
# sha256-verified, so the byte authority is pm/lock.json - no astral-latest,
# no curl|sh.
UV_CMD=""
ensure_uv() {
    [ -n "$UV_CMD" ] && return 0
    # Always the pinned artifact, never a uv already on PATH: Hermes runs only
    # its own packaged toolchain.
    local _target
    if ! _target="$(uv_bootstrap_target)"; then
        fail "no pinned uv build for this platform ($(uname -s) $(uname -m)); Hermes does not support this host" unsupported_platform
    fi
    if ! uv_bootstrap_pin "$_target"; then
        fail "no pinned uv artifact for $_target; Hermes does not support this host" unsupported_platform
    fi
    local _store="${HERMES_RUNTIME_DIR:-$HERMES_HOME/tools}"
    local _entry="$_store/uv-$UV_PIN_VERSION-$_target"
    UV_CMD="$_entry/uv"
    if [ ! -x "$UV_CMD" ]; then
        log "Downloading uv $UV_PIN_VERSION ($_target)"
        local _tmp
        # no-tmp: ok — last-resort fallback when mktemp itself is missing
        _tmp="$(mktemp -d 2>/dev/null || echo "/tmp/hermes-uv-bootstrap.$$")"
        mkdir -p "$_tmp"
        local _fetched_from="$UV_PIN_URL"
        # Only network availability failures permit trying identical mirrored bytes.
        if curl -LsSf "$UV_PIN_URL" -o "$_tmp/uv.tar.gz"; then
            :
        else
            local _curl_status=$?
            case "$_curl_status" in
                5|6|7|18|22|28|52|55|56) ;;
                *) rm -rf "$_tmp"; fail "failed to download pinned uv from $UV_PIN_URL (curl $_curl_status)" download_failed ;;
            esac
            if [ -n "${UV_PIN_MIRROR:-}" ] && curl -LsSf "$UV_PIN_MIRROR" -o "$_tmp/uv.tar.gz"; then
                _fetched_from="$UV_PIN_MIRROR"
            else
                rm -rf "$_tmp"
                fail "failed to download pinned uv from $UV_PIN_URL or ${UV_PIN_MIRROR:-no mirror}" download_failed
            fi
        fi
        local _digest
        if command -v sha256sum >/dev/null 2>&1; then
            _digest="$(sha256sum "$_tmp/uv.tar.gz" | cut -d' ' -f1)"
        else
            _digest="$(shasum -a 256 "$_tmp/uv.tar.gz" | cut -d' ' -f1)"
        fi
        if [ "$_digest" != "$UV_PIN_SHA256" ]; then
            rm -rf "$_tmp"
            fail "uv download digest mismatch from $_fetched_from (expected $UV_PIN_SHA256, got $_digest)" download_digest_mismatch
        fi
        if ! tar -xzf "$_tmp/uv.tar.gz" -C "$_tmp"; then
            rm -rf "$_tmp"
            fail "failed to extract pinned uv archive" uv_unusable
        fi
        local _unpacked
        _unpacked="$(find "$_tmp" -mindepth 1 -maxdepth 2 -name uv -type f | head -n1)"
        if [ -z "$_unpacked" ]; then
            rm -rf "$_tmp"
            fail "uv binary not found in the downloaded archive" uv_unusable
        fi
        mkdir -p "$_entry"
        mv "$_unpacked" "$UV_CMD"
        [ -f "$(dirname "$_unpacked")/uvx" ] && mv "$(dirname "$_unpacked")/uvx" "$_entry/uvx"
        chmod +x "$UV_CMD"
        chmod +x "$_entry/uvx" 2>/dev/null || true
        rm -rf "$_tmp"
    fi
    # Bootstrap keeps the installer private; only UV_CMD invokes it.
    if ! "$UV_CMD" --version >/dev/null 2>&1; then
        fail "pinned uv staged but does not run on this host" uv_unusable
    fi
    log_success "uv ready ($("$UV_CMD" --version 2>/dev/null))"
}

check_platform() {
    # Termux is Linux by uname, but this installer builds a glibc source
    # install the phone cannot run (no Android wheels in the lock). The
    # signed APT package is the only supported shape there.
    if [ -n "${TERMUX_VERSION:-}" ] || case "${PREFIX:-}" in *com.termux/files/usr*) true ;; *) false ;; esac; then
        fail "Termux is installed from its APT repository, not install.sh: pkg install hermes-agent (setup: https://hermes-agent.nousresearch.com/docs/getting-started/termux)" unsupported_platform
    fi
    case "$(uname -s 2>/dev/null)" in
        Linux*) : ;;
        Darwin*) : ;;
        *) fail "unsupported platform: $(uname -s). On Windows use install.ps1." unsupported_platform ;;
    esac
}

json_string() {
    local value="$1" code char escaped
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    for ((code = 1; code < 32; code++)); do
        printf -v char '\\%03o' "$code"
        printf -v char '%b' "$char"
        printf -v escaped '\\u%04x' "$code"
        value="${value//"$char"/$escaped}"
    done
    printf '"%s"' "$value"
}

json_frame() {
    # $1 ok, $2 stage, $3 skipped, $4 reason
    if [ -n "${4:-}" ]; then
        printf '{"ok":%s,"stage":%s,"skipped":%s,"reason":%s}\n' "$1" "$(json_string "$2")" "$3" "$(json_string "$4")"
    else
        printf '{"ok":%s,"stage":%s,"skipped":%s}\n' "$1" "$(json_string "$2")" "$3"
    fi
}

stage_result() {
    local code="$1" ok=false reason="${STAGE_REASON:-}"
    if [ "$code" -eq 0 ]; then
        ok=true
    else
        reason="${reason:-stage failed (exit $code)}"
    fi
    if [ "$JSON" = true ]; then
        json_frame "$ok" "$STAGE" "${STAGE_SKIPPED:-false}" "$reason"
    fi
    if [ "$ok" = false ] && [ "${INSTALL_LADDER:-false}" = true ]; then
        local class="${FAILURE_CLASS:-other}"
        # A user abort beats the stage's own class: a child that caught Ctrl-C and exited 1
        # (pm, source_completion, `hermes setup`) still reaches the stage's `|| fail ... <class>`.
        case "$code" in 129|130|143) class=interrupted ;; esac
        [ "${STAGE_INTERRUPTED:-false}" = false ] || class=interrupted
        write_install_receipt failed "${STAGE//-/_}" "$class"
    fi
}

# The single authoritative stage list: emit_manifest prints it AND the
# no-flag ladder runs it. `products` is the shared completion tail -- the same
# call `hermes update` makes -- so the manifest and the run cannot disagree.
# `desktop` stays directly dispatchable via --stage for external callers, but
# is never listed: --include-desktop selects the desktop product inside
# `products` instead of adding a second build stage.
stage_names() {
    printf '%s\n' prerequisites repository venv python-deps config products setup gateway complete
}

# "title|category|needs_user_input".
products_record() {
    if [ "$INCLUDE_DESKTOP" = true ]; then
        echo "Install command and app + desktop|runtime|false"
    else
        echo "Install command and app|runtime|false"
    fi
}

# "$1" stage name -> its manifest record fields (title|category|needs_user_input).
stage_record() {
    case "$1" in
        prerequisites) echo "System prerequisites|runtime|false" ;;
        repository)    echo "Download Hermes Agent|runtime|false" ;;
        venv)          echo "Create Python environment|runtime|false" ;;
        python-deps)   echo "Install Python dependencies|runtime|false" ;;
        config)        echo "Prepare config and skills|configuration|false" ;;
        products)      products_record ;;
        setup)         echo "Configure API keys and settings|configuration|true" ;;
        gateway)       echo "Configure gateway service|configuration|true" ;;
        desktop)       echo "Build desktop app|runtime|false" ;;
        complete)      echo "Finish install|runtime|false" ;;
    esac
}

emit_manifest() {
    printf '%s' '{"protocol_version":1,"stages":['
    _sep=""
    for _s in $(stage_names); do
        IFS='|' read -r _title _category _needs <<< "$(stage_record "$_s")"
        printf '%s{"name":"%s","title":"%s","category":"%s","needs_user_input":%s}' \
            "$_sep" "$_s" "$_title" "$_category" "$_needs"
        _sep=","
    done
    printf '%s\n' ']}'
}

stage_prerequisites() {
    command -v git >/dev/null 2>&1 || fail "git is required. Install it with your system package manager." git_missing
    command -v curl >/dev/null 2>&1 || fail "curl is required. Install it with your system package manager." curl_missing
    # PM's Node on musl is the unofficial-builds musl archive, which links the
    # system libstdc++; without it every node/npm stage fails verification.
    local _target
    _target="$(uv_bootstrap_target 2>/dev/null)" || _target=""
    if [[ "$_target" == *-musl ]]; then
        local _libdir _stdcxx=""
        for _libdir in /lib /usr/lib /usr/local/lib; do
            compgen -G "$_libdir/libstdc++.so.6*" >/dev/null && { _stdcxx=yes; break; }
        done
        [ -n "$_stdcxx" ] || fail "musl host: the Node.js runtime needs the system libstdc++. Install it (Alpine: apk add libstdc++, Void: xbps-install libstdc++) and re-run." libstdcxx_missing
    fi
    # glibc Node links libatomic.so.1, absent on minimal Debian/RHEL hosts. PM
    # installs the distro package with `sudo -n` under its install lock, so
    # cache sudo credentials now, while the terminal can answer the prompt.
    if [ "$NON_INTERACTIVE" != true ] && [[ "$_target" == linux-* && "$_target" != *-musl ]] \
        && [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && has_terminal \
        && ! { ldconfig -p 2>/dev/null || /sbin/ldconfig -p 2>/dev/null; } | grep -q 'libatomic\.so\.1'; then
        log "Node.js needs libatomic.so.1; sudo may ask for your password to install it"
        sudo -v </dev/tty || log_warn "sudo failed; if Node.js fails, install libatomic (Debian/Ubuntu: libatomic1) and re-run"
    fi
    log_success "prerequisites ok (git, curl)"
}

stage_repository() {
    local pinned=false
    # An interrupted clone from an older installer can leave a .git with no
    # initial commit, where stash/checkout abort ("You do not have the
    # initial commit yet", #40998). Move it aside -- never delete it, it may
    # hold something the user wants -- and clone fresh below.
    if [ -d "$INSTALL_DIR/.git" ] && ! git -C "$INSTALL_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
        local broken
        broken="${INSTALL_DIR}.broken-$(date -u +%Y%m%d-%H%M%S)"
        log_warn "$INSTALL_DIR has no commits (interrupted clone); moving it aside to $broken"
        mv "$INSTALL_DIR" "$broken" || fail "cannot move $INSTALL_DIR aside" filesystem_error
    fi
    if [ -d "$INSTALL_DIR/.git" ]; then
        log "Updating $INSTALL_DIR ($BRANCH)"
        # An explicit HERMES_REPO_URL names the source for reruns too, not
        # just the first clone.
        if [ -n "${HERMES_REPO_URL:-}" ]; then
            git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL" || fail "cannot point origin at $REPO_URL" git_fetch_failed
        fi
        # Explicit refspec: a tag-pinned --single-branch checkout from an older
        # installer maps only the tag, so a by-name fetch writes FETCH_HEAD and
        # never the origin/$BRANCH everything below resolves (#125112).
        # git 2.53+ aborts fetches into a partial clone whose packs lack a .promisor
        # marker (#124272), and an install stuck there never fetches the updater that
        # heals it. Marking is idempotent and never rewrites objects.
        if [ "$(git -C "$INSTALL_DIR" config --bool --get remote.origin.promisor)" = true ]; then
            local pack
            for pack in "$INSTALL_DIR"/.git/objects/pack/pack-*.pack; do
                if [ -f "$pack" ] && [ ! -e "${pack%.pack}.promisor" ]; then
                    : > "${pack%.pack}.promisor" || log_warn "could not mark $pack as a partial-clone pack"
                fi
            done
            # Existing treeless checkout: same commit-graph lazy-fetch loop guard (#127711).
            git -C "$INSTALL_DIR" config maintenance.commit-graph.enabled false \
                || log_warn "could not disable maintenance.commit-graph.enabled in $INSTALL_DIR"
            git -C "$INSTALL_DIR" config gc.writeCommitGraph false \
                || log_warn "could not disable gc.writeCommitGraph in $INSTALL_DIR"
            git -C "$INSTALL_DIR" config fetch.writeCommitGraph false \
                || log_warn "could not disable fetch.writeCommitGraph in $INSTALL_DIR"
            # A treeless (tree:0) checkout from a late-September installer downloads whole
            # directory snapshots again on every history walk (#129514). Fetch its trees once; the
            # new filter is recorded only after that succeeds, so `hermes update` retries otherwise.
            if [ "$(git -C "$INSTALL_DIR" config --get remote.origin.partialclonefilter)" = tree:0 ]; then
                if run_logged --may-fail "Fetching directory history once (treeless checkout)" \
                    git -C "$INSTALL_DIR" -c gc.auto=0 -c maintenance.auto=false fetch --refetch \
                    --filter=blob:none origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"; then
                    git -C "$INSTALL_DIR" config remote.origin.partialclonefilter blob:none \
                        || log_warn "could not record the blobless filter in $INSTALL_DIR"
                else
                    log_warn "could not fetch the directory history; the next hermes update retries it"
                fi
            fi
        fi
        run_logged "Fetching origin/$BRANCH" git -C "$INSTALL_DIR" fetch origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" \
            || fail "git fetch failed" git_fetch_failed
        local stamp
        stamp="$(date -u +%Y%m%d-%H%M%S)"
        # Park local work BEFORE switching branches: checkout refuses a dirty
        # tree that conflicts, and the reset below would discard it. Work
        # that cannot be parked stops the install -- never overwrite it.
        if [ -n "$(git -C "$INSTALL_DIR" status --porcelain)" ]; then
            # An interrupted update can leave unmerged index entries, where
            # stash aborts ("could not write index"). Dropping only the
            # index-level conflict state keeps the working-tree changes for
            # the stash below (#4735).
            if [ -n "$(git -C "$INSTALL_DIR" ls-files --unmerged)" ]; then
                log_warn "clearing unmerged index entries from a previous conflict"
                git -C "$INSTALL_DIR" reset -q || fail "cannot clear the unmerged index in $INSTALL_DIR" local_changes_blocked
            fi
            run_logged "Stashing local changes" \
                git -C "$INSTALL_DIR" stash push --include-untracked -m "hermes-install-autostash-$stamp" \
                || fail "could not stash local changes in $INSTALL_DIR; commit or move them aside, then rerun" local_changes_blocked
            log_warn "local changes stashed as hermes-install-autostash-$stamp"
        fi
        # checkout's branch guess only sees remote refs the refspec maps, so a
        # narrow checkout (detached at its tag, no local branch) gets the branch
        # created at the fetched tip.
        if git -C "$INSTALL_DIR" show-ref --verify --quiet "refs/heads/$BRANCH"; then
            run_logged "Checking out $BRANCH" git -C "$INSTALL_DIR" checkout "$BRANCH" || fail "git checkout failed" git_checkout_failed
        else
            run_logged "Checking out $BRANCH" git -C "$INSTALL_DIR" checkout -b "$BRANCH" "origin/$BRANCH" \
                || fail "git checkout failed" git_checkout_failed
        fi
        if ! run_logged --may-fail "Fast-forwarding to origin/$BRANCH" \
            git -C "$INSTALL_DIR" merge --ff-only "origin/$BRANCH"; then
            # A release cut off the main line, a force-pushed remote, or the
            # user's own commits cannot fast-forward. Every stage below reads
            # files only the new tree has (pm/), so an install left on the old
            # tree cannot finish -- match the remote the way `hermes update`
            # does, after parking the old tip.
            # Only commits absent from origin need a rescue ref. Keep the same
            # namespace as `hermes update` so its pruning and recovery work.
            local dropped rescue_kind rescue_ref prior
            dropped="$(git -C "$INSTALL_DIR" rev-list --count "origin/$BRANCH..HEAD")" \
                || fail "cannot count commits before reset" git_reset_failed
            if [ "$dropped" -gt 0 ]; then
                rescue_kind="diverged"
                git -C "$INSTALL_DIR" merge-base HEAD "origin/$BRANCH" >/dev/null 2>&1 \
                    || rescue_kind="orphan"
                prior="$(git -C "$INSTALL_DIR" rev-parse --short=12 HEAD)" \
                    || fail "cannot identify commits before reset" git_reset_failed
                rescue_ref="refs/hermes-update-backups/$rescue_kind-$BRANCH-$stamp-$prior"
                git -C "$INSTALL_DIR" update-ref "$rescue_ref" HEAD \
                    || fail "cannot back up $dropped local commit(s); refusing to reset" git_reset_failed
                log_warn "$dropped commit(s) not on origin/$BRANCH backed up to $rescue_ref"
                log "List them with: git -C \"$INSTALL_DIR\" log origin/$BRANCH..$rescue_ref"
            fi
            run_logged "Resetting to origin/$BRANCH" git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH" \
                || fail "git reset failed" git_reset_failed
            log_warn "not fast-forwardable; reset to origin/$BRANCH"
        fi
    else
        # `mv <clone> <existing dir>` nests the checkout INSIDE it as
        # <dir>/tree, so a pre-existing destination must be empty (we take
        # the empty dir over) or we refuse: whatever lives there is not ours.
        if [ -e "$INSTALL_DIR" ] || [ -L "$INSTALL_DIR" ]; then
            if [ -d "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ] && [ -z "$(ls -A "$INSTALL_DIR")" ]; then
                rmdir "$INSTALL_DIR" || fail "cannot replace empty $INSTALL_DIR" filesystem_error
            else
                fail "$INSTALL_DIR exists and is not a Hermes git checkout. Move it aside, or install elsewhere with --dir <path>." dir_not_checkout
            fi
        fi
        mkdir -p "$(dirname "$INSTALL_DIR")"
        local staged attempt label cloned=false progress=() materialize=(reset --hard HEAD)
        # Phase lines ("Receiving objects: 42%") feed the status line; git
        # prints none to a pipe unless asked.
        if quiet_output; then progress=(--progress); fi
        staged="$(mktemp -d "$(dirname "$INSTALL_DIR")/.hermes-clone-XXXXXX")" || fail "cannot stage clone" filesystem_error
        for attempt in 1 2 3; do
            # Blobless: every commit, tree and release tag (runtime identity is
            # the nearest reachable release; --commit pins and branch switches
            # still resolve), file contents fetched on demand. Not treeless:
            # a long-lived treeless checkout re-downloads whole trees on every
            # checkout and history walk (#129712).
            label="Cloning $REPO_URL ($BRANCH) into $INSTALL_DIR"
            [ "$attempt" = 1 ] || label="$label (attempt $attempt of 3)"
            # A pinned install materializes only the pin (below): checking out the
            # branch tip first fetches and rewrites every file the two differ in.
            if run_logged "$label" git clone ${progress[@]+"${progress[@]}"} \
                --filter=blob:none ${INSTALL_COMMIT:+--no-checkout} --branch "$BRANCH" "$REPO_URL" "$staged/tree"; then
                cloned=true
                break
            fi
            rm -rf "$staged/tree"
            [ "$attempt" = 3 ] || sleep "$((attempt * 5))"
        done
        if [ "$cloned" = false ]; then
            # The checkout step is where throttled downloads die: clone the
            # graph alone, then retry materializing the tree separately.
            log_warn "direct clone failed; trying deferred checkout"
            if run_logged "Cloning history" git clone ${progress[@]+"${progress[@]}"} \
                --filter=blob:none --no-checkout --branch "$BRANCH" "$REPO_URL" "$staged/tree"; then
                cloned=deferred
            fi
        fi
        if [ "$cloned" = false ]; then
            rm -rf "$staged"
            fail "git clone failed; no checkout published" git_clone_failed
        fi
        if [ -n "$INSTALL_COMMIT" ]; then
            # Pin before publishing: a pinned or deferred clone has no files yet,
            # and an unpublished tree never leaves an empty checkout behind.
            git -C "$staged/tree" merge-base --is-ancestor "$INSTALL_COMMIT" "origin/$BRANCH" 2>/dev/null \
                || { rm -rf "$staged"; fail "commit $INSTALL_COMMIT is not on branch $BRANCH" commit_not_on_branch; }
        fi
        if [ -n "$INSTALL_COMMIT" ] || [ "$cloned" = deferred ]; then
            # The checkout step is where throttled downloads die: retry it alone.
            # A pin detaches HEAD and leaves the branch at its tip, as the published pin did.
            [ -z "$INSTALL_COMMIT" ] || materialize=(checkout --force --detach "$INSTALL_COMMIT")
            cloned=false
            for attempt in 1 2; do
                if run_logged "Checking out ${INSTALL_COMMIT:-files} (attempt $attempt of 2)" \
                    git -C "$staged/tree" "${materialize[@]}"; then
                    cloned=true
                    [ -z "$INSTALL_COMMIT" ] || pinned=true
                    break
                fi
                [ "$attempt" = 2 ] || sleep 5
            done
            if [ "$cloned" = false ]; then
                rm -rf "$staged"
                fail "could not check out ${INSTALL_COMMIT:-the cloned tree}; no checkout published" git_checkout_failed
            fi
        fi
        if ! mv "$staged/tree" "$INSTALL_DIR"; then
            rm -rf "$staged"
            fail "cannot publish cloned checkout" filesystem_error
        fi
        rmdir "$staged"
        # A treeless checkout must never write a commit-graph: over a graph with
        # changed-path data that lazy-fetches the trees of every unseen commit, in a
        # loop (#127711). gc.auto stays on: `hermes update` folds lazy-fetch packs with
        # `gc --auto`.
        git -C "$INSTALL_DIR" config maintenance.commit-graph.enabled false \
            || log_warn "could not disable maintenance.commit-graph.enabled in $INSTALL_DIR"
        git -C "$INSTALL_DIR" config gc.writeCommitGraph false \
            || log_warn "could not disable gc.writeCommitGraph in $INSTALL_DIR"
        git -C "$INSTALL_DIR" config fetch.writeCommitGraph false \
            || log_warn "could not disable fetch.writeCommitGraph in $INSTALL_DIR"
        log_success "Hermes Agent cloned"
    fi
<<<<<<< HEAD

    # Export for current session so hermes works immediately
    export PATH="$command_link_dir:$PATH"

    log_success "hermes command ready"
}

copy_config_templates() {
    log_info "Setting up configuration files..."

    # Create ~/.hermes directory structure (config at top level, code in subdir)
    mkdir -p "$HERMES_HOME"/{cron,sessions,logs,pairing,hooks,image_cache,audio_cache,memories,skills}

    # Create .env at ~/.hermes/.env (top level, easy to find)
    if [ ! -f "$HERMES_HOME/.env" ]; then
        if [ -f "$INSTALL_DIR/.env.example" ]; then
            cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env"
            log_success "Created ~/.hermes/.env from template"
        else
            touch "$HERMES_HOME/.env"
            log_success "Created ~/.hermes/.env"
        fi
    else
        log_info "~/.hermes/.env already exists, keeping it"
    fi
    # Restrict .env permissions — this file holds API keys and tokens.
    # 0600 ensures only the file owner can read/write, matching standard
    # practice for credential files (.netrc, .aws/credentials, .ssh/config).
    chmod 600 "$HERMES_HOME/.env"
    configure_browser_env_from_system_browser

    # Create config.yaml at ~/.hermes/config.yaml (top level, easy to find)
    if [ ! -f "$HERMES_HOME/config.yaml" ]; then
        if [ -f "$INSTALL_DIR/cli-config.yaml.example" ]; then
            cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
            log_success "Created ~/.hermes/config.yaml from template"
        fi
    else
        log_info "~/.hermes/config.yaml already exists, keeping it"
    fi

    # Create SOUL.md if it doesn't exist (global persona file).
    # This MUST match DEFAULT_SOUL_MD in hermes_cli/default_soul.py — the
    # runtime (_ensure_default_soul_md) treats the old comment-only scaffold as
    # "never customized" and upgrades it to this text on next run, so any drift
    # here is self-healing, but keep them in sync to avoid a churn on first run.
    if [ ! -f "$HERMES_HOME/SOUL.md" ]; then
        cat > "$HERMES_HOME/SOUL.md" << 'SOUL_EOF'
You are OpenComputer, an intelligent AI assistant. You are helpful, knowledgeable, and direct. You assist users with a wide range of tasks including answering questions, writing and editing code, analyzing information, creative work, and executing actions via your tools. You communicate clearly, admit uncertainty when appropriate, and prioritize being genuinely useful over being verbose unless otherwise directed below. Be targeted and efficient in your exploration and investigations.
SOUL_EOF
        log_success "Created ~/.hermes/SOUL.md (edit to customize personality)"
    fi

    log_success "Configuration directory ready: ~/.hermes/"

    # Seed bundled skills into ~/.hermes/skills/ (manifest-based, one-time per skill)
    if [ "$NO_SKILLS" = true ]; then
        # Blank-slate install: write the opt-out marker and skip seeding.
        # skills_sync.py and `hermes update` both honor this marker, so the
        # default profile stays empty across future updates too.
        printf '%s\n' \
            "This profile opted out of bundled-skill seeding (installed with --no-skills)." \
            "Delete this file to re-enable sync on the next 'hermes update'." \
            > "$HERMES_HOME/.no-bundled-skills" 2>/dev/null || true
        log_info "Skipping bundled skills (--no-skills). Wrote $HERMES_HOME/.no-bundled-skills"
        log_info "  Future 'hermes update' runs will not inject bundled skills. Delete the marker to opt back in."
    else
        log_info "Syncing bundled skills to ~/.hermes/skills/ ..."
        if "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/tools/skills_sync.py" 2>/dev/null; then
            log_success "Skills synced to ~/.hermes/skills/"
        else
            # Fallback: simple directory copy if Python sync fails
            if [ -d "$INSTALL_DIR/skills" ] && [ ! "$(ls -A "$HERMES_HOME/skills/" 2>/dev/null | grep -v '.bundled_manifest')" ]; then
                cp -r "$INSTALL_DIR/skills/"* "$HERMES_HOME/skills/" 2>/dev/null || true
                log_success "Skills copied to ~/.hermes/skills/"
            fi
        fi
=======
    if [ -n "$INSTALL_COMMIT" ] && [ "$pinned" = false ]; then
        # A rerun over an existing checkout pins here (a fresh clone pinned
        # before publishing). A pin must come from the branch being installed:
        # the complete marker records both, and a commit off that branch would
        # make the next plain rerun "update" onto a different line.
        git -C "$INSTALL_DIR" merge-base --is-ancestor "$INSTALL_COMMIT" "origin/$BRANCH" 2>/dev/null \
            || fail "commit $INSTALL_COMMIT is not on branch $BRANCH" commit_not_on_branch
        run_logged "Pinning $INSTALL_COMMIT" git -C "$INSTALL_DIR" checkout "$INSTALL_COMMIT" \
            || fail "could not pin commit $INSTALL_COMMIT" git_checkout_failed
>>>>>>> upstream/main
    fi
}

stage_venv() {
    # Keep the installer stage protocol; PM alone creates dependency environments.
    local boot_py
    bootstrap_python
    log_success "bootstrap Python ready; PM prepares the dependency environment"
}

# Tool-only bootstrap: acquire uv and Python before PM's own dependencies exist.
# The application dependency graph is never installed in this interpreter.
bootstrap_python() {
    ensure_uv
    local _py
    # Read packages.python.version by following object names and braces, not
    # indentation — same pre-Python reader contract as setup-hermes.sh's pin().
    _py="$(awk -F '"' '
        /^[[:space:]]*("[^"]+"[[:space:]]*:[[:space:]]*)?\{/ { path[++depth] = $2; next }
        /^[[:space:]]*\}[[:space:]]*,?[[:space:]]*$/ { delete path[depth--]; next }
        path[2] == "packages" && path[3] == "python" && $2 == "version" && depth == 3 { print $4; exit }
    ' "$INSTALL_DIR/pm/lock.json" | cut -d+ -f1 | cut -d. -f1,2)"
    [ -n "$_py" ] || _py="3.14"
    # Only base interpreters qualify: an activated app venv must not become
    # PM's bootstrap parent. Prefer the existing managed Python, then a host
    # Python of the same supported minor before attempting a download (#10778).
    # This interpreter only boots PM; PM still owns the exact runtime pin.
    if ! boot_py="$(UV_SYSTEM_PYTHON=1 UV_NO_PROJECT=1 "$UV_CMD" python find --managed-python "$_py" 2>/dev/null)" \
        && ! boot_py="$("$UV_CMD" python find --system --no-project "$_py" 2>/dev/null)"; then
        run_logged "Downloading Python $_py" "$UV_CMD" python install --no-bin --no-registry "$_py" \
            || fail "bootstrap Python installation failed" python_install_failed
        boot_py="$(UV_SYSTEM_PYTHON=1 UV_NO_PROJECT=1 "$UV_CMD" python find --managed-python "$_py")" || fail "bootstrap Python lookup failed" python_install_failed
    fi
    boot_py="${boot_py%$'\r'}"
    [ -x "$boot_py" ] && "$boot_py" --version >/dev/null 2>&1 || fail "bootstrap Python is not executable: $boot_py" python_install_failed
}

# uv exits before PM can replace its tool entry. pm.cli then prepares and
# enters its independently locked runtime before mutating application deps.
bootstrap_pm() {
    local boot_py
    local pm_args=(install)
    # PM records the opt-outs, so later installs and `hermes update` keep the
    # tools off until `hermes pm install <name>` opts back in.
    [ "$SKIP_BROWSER" = true ] && pm_args+=(--without agent-browser)
    [ "$SKIP_COMPUTER_USE" = true ] && pm_args+=(--without cua-driver)
    bootstrap_python
    # Refresh the sudo ticket cached in prerequisites (never prompts): the
    # clone and downloads can outlast sudo's timestamp_timeout.
    if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then sudo -n -v 2>/dev/null || :; fi
    (cd "$INSTALL_DIR" && run_logged "Installing dependencies (hash-verified via uv.lock)" \
        "$boot_py" -m pm.cli "${pm_args[@]}") \
        || fail "pm install failed" deps_install_failed
    log_success "dependencies installed"
}

stage_python_deps() {
    bootstrap_pm
}

desktop_product_present() {
    # Does this checkout already carry a built desktop app? A plain repair or
    # upgrade rerun on a desktop install must REBUILD it rather than leave a
    # bundle built from the previous code: the app is part of that install, and
    # the artifacts live inside the tree (gitignored), so an update makes them
    # stale instead of removing them.
    # electron-builder suffixes the output dir with the arch on every non-x64
    # target (linux-arm64-unpacked, mac-arm64, win-arm64-unpacked), so the x64
    # names alone miss a desktop build on ARM64 Linux/Windows (#94703).
    local release="$INSTALL_DIR/apps/desktop/release" dir
    for dir in linux-unpacked linux-arm64-unpacked mac mac-arm64 \
               win-unpacked win-ia32-unpacked win-arm64-unpacked; do
        [ -d "$release/$dir" ] && return 0
    done
    return 1
}

# Guarded: a login shell whose ~/.bash_profile sources ~/.bashrc (Fedora, RHEL)
# reads both files, so an unconditional prepend would put ~/.local/bin on PATH twice.
SHELL_PATH_LINE='case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac'
SHELL_PATH_SETUP_RE='^[[:space:]]*([^#[:space:]].*)?PATH=.*\.local/bin'

append_shell_path() {
    local rc="$1" line="$2" pattern="$3"
    # Existing user PATH setup wins; never append another line on a repair run.
    if [ -f "$rc" ] && grep -E "$pattern" "$rc" >/dev/null 2>&1; then
        return 0
    fi
    mkdir -p "$(dirname "$rc")"
    printf '\n# Hermes Agent command\n%s\n' "$line" >> "$rc" || fail "cannot update PATH in $rc" filesystem_error
    log_success "added ~/.local/bin to PATH in $rc"
}

wire_shell_path() {
    # The launcher is published by source_completion; shell rc files belong to
    # the installer, not to PM (updates must not modify a user's shell setup).
    # Never use the installer's inherited PATH as a proxy for a *new* shell.
    local login_shell="${SHELL:-/bin/bash}"
    case "${login_shell##*/}" in
        zsh)
            append_shell_path "$HOME/.zshrc" "$SHELL_PATH_LINE" "$SHELL_PATH_SETUP_RE"
            append_shell_path "$HOME/.zprofile" "$SHELL_PATH_LINE" "$SHELL_PATH_SETUP_RE"
            ;;
<<<<<<< HEAD
    esac
    if command -v cua-driver >/dev/null 2>&1; then
        if cua_driver_runtime_compatible; then
            log_success "Computer Use driver (cua-driver) already installed and compatible"
            return 0
        fi
        log_warn "Existing cua-driver is old or incomplete; repairing it"
    fi
    # Non-admin macOS accounts can't receive the CuaDriver.app bundle in
    # /Applications; skip cleanly instead of failing loudly (#47865 class).
    if [ "$(uname -s)" = "Darwin" ] && [ -d /Applications ] && [ ! -w /Applications ]; then
        log_info "Skipping Computer Use driver (cua-driver): /Applications is not writable"
        return 0
    fi

    log_info "Installing Computer Use driver (cua-driver)..."
    # Same upstream installer `hermes computer-use install` runs; time-boxed
    # so a stalled GitHub download can't hang the Hermes install. The
    # upstream installer serializes with its own lock (600s stale window),
    # so give it a ceiling above that — matching Hermes'
    # _CUA_INSTALLER_TIMEOUT (660s).
    local cua_log
    cua_log="$(mktemp)"
    if run_with_timeout 660 /bin/bash -c \
        'curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh | /bin/bash' \
        >"$cua_log" 2>&1; then
        log_success "Computer Use driver installed (enable via 'hermes tools' → Computer Use)"
    else
        log_warn "Computer Use driver install failed — it will install on demand when you enable the tool."
        log_info "Install later with: hermes computer-use install"
        tail -n 5 "$cua_log" >&2 || true
    fi
    rm -f "$cua_log"
}

run_setup_wizard() {
    if [ "$RUN_SETUP" = false ]; then
        log_info "Skipping setup wizard (--skip-setup)"
        return 0
    fi

    # The setup wizard reads from /dev/tty, so it works even when the
    # install script itself is piped (curl | bash). Only skip if no
    # terminal is available at all (e.g. Docker build, CI).
    #
    # Probe by actually opening /dev/tty: a bare existence test passes
    # in Docker builds where the device node is in the mount namespace
    # but opening fails with ENXIO, so the wizard would proceed and
    # then crash on `< /dev/tty` below.
    if ! (: </dev/tty) 2>/dev/null; then
        log_info "Setup wizard skipped (no terminal available). Run 'hermes setup' after install."
        return 0
    fi

    echo ""
    log_info "Starting setup wizard..."
    echo ""

    cd "$INSTALL_DIR"

    # Run hermes setup using the venv Python directly (no activation needed).
    # Redirect stdin from /dev/tty so interactive prompts work when piped from curl.
    if [ "$USE_VENV" = true ]; then
        "$INSTALL_DIR/venv/bin/python" -m hermes_cli.main setup < /dev/tty
    else
        python -m hermes_cli.main setup < /dev/tty
    fi
}

maybe_start_gateway() {
    # Check if any messaging platform tokens were configured
    ENV_FILE="$HERMES_HOME/.env"
    if [ ! -f "$ENV_FILE" ]; then
        return 0
    fi

    HAS_MESSAGING=false
    for VAR in TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN WHATSAPP_ENABLED; do
        VAL=$(grep "^${VAR}=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
        if [ -n "$VAL" ] && [ "$VAL" != "your-token-here" ]; then
            HAS_MESSAGING=true
            break
        fi
    done

    if [ "$HAS_MESSAGING" = false ]; then
        return 0
    fi

    echo ""
    log_info "Messaging platform token detected!"
    log_info "The gateway needs to be running for Hermes to send/receive messages."

    # If WhatsApp is enabled and no session exists yet, run foreground first for QR scan
    WHATSAPP_VAL=$(grep "^WHATSAPP_ENABLED=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
    WHATSAPP_SESSION="$HERMES_HOME/whatsapp/session/creds.json"
    if [ "$WHATSAPP_VAL" = "true" ] && [ ! -f "$WHATSAPP_SESSION" ]; then
        if [ "$IS_INTERACTIVE" = true ]; then
            echo ""
            log_info "WhatsApp is enabled but not yet paired."
            log_info "Running 'hermes whatsapp' to pair via QR code..."
            echo ""
            if prompt_yes_no "Pair WhatsApp now?" "yes"; then
                HERMES_CMD="$(get_hermes_command_path)"
                $HERMES_CMD whatsapp || true
            fi
        else
            log_info "WhatsApp pairing skipped (non-interactive). Run 'hermes whatsapp' to pair."
        fi
    fi

    # Probe by actually opening /dev/tty: a bare existence test passes
    # in Docker builds where the device node is in the mount namespace
    # but opening fails with ENXIO. See #16746.
    if ! (: </dev/tty) 2>/dev/null; then
        log_info "Gateway setup skipped (no terminal available). Run 'hermes gateway install' later."
        return 0
    fi

    echo ""
    local should_install_gateway=false
    if [ "$DISTRO" = "termux" ]; then
        if prompt_yes_no "Would you like to start the gateway in the background?" "yes"; then
            should_install_gateway=true
        fi
    else
        if prompt_yes_no "Would you like to install the gateway as a background service?" "yes"; then
            should_install_gateway=true
        fi
    fi

    if [ "$should_install_gateway" = true ]; then
        HERMES_CMD="$(get_hermes_command_path)"

        if [ "$DISTRO" != "termux" ] && command -v systemctl &> /dev/null; then
            log_info "Installing systemd service..."
            if $HERMES_CMD gateway install 2>/dev/null; then
                log_success "Gateway service installed"
                if $HERMES_CMD gateway start 2>/dev/null; then
                    log_success "Gateway started! Your bot is now online."
                else
                    log_warn "Service installed but failed to start. Try: hermes gateway start"
                fi
            else
                log_warn "Systemd install failed. You can start manually: hermes gateway"
            fi
        else
            if [ "$DISTRO" = "termux" ]; then
                log_info "Termux detected — starting gateway in best-effort background mode..."
            else
                log_info "systemd not available — starting gateway in background..."
            fi
            nohup $HERMES_CMD gateway > "$HERMES_HOME/logs/gateway.log" 2>&1 &
            GATEWAY_PID=$!
            log_success "Gateway started (PID $GATEWAY_PID). Logs: ~/.hermes/logs/gateway.log"
            log_info "To stop: kill $GATEWAY_PID"
            log_info "To restart later: hermes gateway"
            if [ "$DISTRO" = "termux" ]; then
                log_warn "Android may stop background processes when Termux is suspended or the system reclaims resources."
            fi
        fi
    else
        log_info "Skipped. Start the gateway later with: hermes gateway"
    fi
}

write_bootstrap_marker() {
    # Writes $INSTALL_DIR/.hermes-bootstrap-complete, which tells the Hermes
    # desktop app (apps/desktop/electron/main.ts) and the macOS launcher fast
    # path (apps/bootstrap-installer) "a real install finished here -- don't
    # re-run first-run bootstrap."
    #
    # Schema mirrors install.ps1's Write-BootstrapMarker and main.ts's
    # writeBootstrapMarker(). Keep the three in lockstep:
    #   schemaVersion 1 + pinnedCommit (length >= 7) are what the desktop
    #   validator requires; desktopVersion is omitted because only the desktop
    #   app knows its own version.
    if [ ! -d "$INSTALL_DIR" ]; then
        log_warn "Skipping bootstrap marker: $INSTALL_DIR doesn't exist"
        return 0
    fi

    # Explicit --commit wins; otherwise read HEAD from the checkout we just
    # installed. If neither resolves, skip the marker entirely rather than
    # write one the desktop will reject -- an absent marker is a clean
    # "bootstrap needed", a malformed one is a confusing half-state.
    local pinned_commit="$INSTALL_COMMIT"
    if [ -z "$pinned_commit" ]; then
        pinned_commit=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null) || pinned_commit=""
    fi

    if [ -z "$pinned_commit" ]; then
        log_warn "Skipping bootstrap marker: could not resolve HEAD in $INSTALL_DIR"
        return 0
    fi

    local marker_path="$INSTALL_DIR/.hermes-bootstrap-complete"
    local tmp_path="$marker_path.tmp"

    # Atomic publish: the macOS launcher predicate only checks existence, so a
    # torn write would arm the fast path against a half-written marker.
    printf '{\n  "schemaVersion": 1,\n  "pinnedCommit": "%s",\n  "pinnedBranch": "%s",\n  "completedAt": "%s"\n}\n' \
        "$pinned_commit" \
        "$BRANCH" \
        "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" > "$tmp_path"
    mv -f "$tmp_path" "$marker_path"
}

print_success() {
    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│              ✓ Installation Complete!                   │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
    echo ""

    # Show file locations
    echo -e "${CYAN}${BOLD}📁 Your files:${NC}"
    echo ""
    echo -e "   ${YELLOW}Config:${NC}    $HERMES_HOME/config.yaml"
    echo -e "   ${YELLOW}API Keys:${NC}  $HERMES_HOME/.env"
    echo -e "   ${YELLOW}Data:${NC}      $HERMES_HOME/cron/, sessions/, logs/"
    echo -e "   ${YELLOW}Code:${NC}      $INSTALL_DIR"
    echo ""

    echo -e "${CYAN}─────────────────────────────────────────────────────────${NC}"
    echo ""
    echo -e "${CYAN}${BOLD}🚀 Commands:${NC}"
    echo ""
    echo -e "   ${GREEN}hermes${NC}              Start chatting"
    echo -e "   ${GREEN}hermes setup${NC}        Configure API keys & settings"
    echo -e "   ${GREEN}hermes config${NC}       View/edit configuration"
    echo -e "   ${GREEN}hermes config edit${NC}  Open config in editor"
    echo -e "   ${GREEN}hermes gateway install${NC} Install gateway service (messaging + cron)"
    echo -e "   ${GREEN}hermes update${NC}       Update to latest version"
    echo ""

    echo -e "${CYAN}─────────────────────────────────────────────────────────${NC}"
    echo ""
    if [ "$DISTRO" = "termux" ]; then
        echo -e "${YELLOW}⚡ 'hermes' was linked into $(get_command_link_display_dir), which is already on PATH in Termux.${NC}"
        echo ""
    elif [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo -e "${YELLOW}⚡ 'hermes' was linked into /usr/local/bin and is ready to use — no shell reload needed.${NC}"
        echo ""
    else
        echo -e "${YELLOW}⚡ Reload your shell to use 'hermes' command:${NC}"
        echo ""
        LOGIN_SHELL="$(basename "${SHELL:-/bin/bash}")"
        if [ "$LOGIN_SHELL" = "zsh" ]; then
            echo "   source ~/.zshrc"
        elif [ "$LOGIN_SHELL" = "bash" ]; then
            echo "   source ~/.bashrc"
        elif [ "$LOGIN_SHELL" = "fish" ]; then
            echo "   source ~/.config/fish/config.fish"
        else
            echo "   source ~/.bashrc   # or ~/.zshrc"
        fi
        echo ""
    fi

    # Show Node.js warning if auto-install failed
    if [ "$HAS_NODE" = false ]; then
        echo -e "${YELLOW}"
        echo "Note: Node.js could not be installed automatically."
        echo "Browser tools need Node.js. Install manually:"
        if [ "$DISTRO" = "termux" ]; then
            echo "  pkg install nodejs"
        else
            echo "  https://nodejs.org/en/download/"
        fi
        echo -e "${NC}"
    fi

    # Show ripgrep note if not installed
    if [ "$HAS_RIPGREP" = false ]; then
        echo -e "${YELLOW}"
        echo "Note: ripgrep (rg) was not found. File search will use"
        echo "grep as a fallback. For faster search in large codebases,"
        if [ "$DISTRO" = "termux" ]; then
            echo "install ripgrep: pkg install ripgrep"
        else
            echo "install ripgrep: sudo apt install ripgrep (or brew install ripgrep)"
        fi
        echo -e "${NC}"
    fi
}

ensure_browser() {
    if ! command -v node >/dev/null 2>&1; then
        local node_bin="$HERMES_HOME/node/bin/node"
        if [ -x "$node_bin" ]; then
            export PATH="$HERMES_HOME/node/bin:$PATH"
        else
            log_error "Node.js not found. Run with --ensure node first."
            return 1
        fi
    fi

    local npm_bin
    npm_bin="$(command -v npm 2>/dev/null || echo "$HERMES_HOME/node/bin/npm")"
    if [ ! -x "$npm_bin" ]; then
        log_error "npm not found"
        return 1
    fi

    # agent-browser itself is intentionally NOT installed here (#43564 /
    # PR #44772 review): it resolves lazily via `npx agent-browser` instead,
    # which every consumer (tools/browser_tool.py, `hermes update`'s npx
    # cache warm) already goes through. Eagerly npm-installing a second,
    # separately version-pinned copy here -- only reachable via this
    # explicit --ensure browser fallback in the first place -- was redundant
    # complexity and an extra credential/supply-chain surface for a path
    # npx already covers.
    log_info "Installing camofox browser server..."
    local log_file
    log_file="$(mktemp)"
    # Time-boxed (#39219): a stalled npm registry fetch here would otherwise
    # hang the installer with no progress, same class as the desktop build.
    if ! run_with_timeout "$NODE_DEPS_TIMEOUT" "$npm_bin" install -g --prefix "$HERMES_HOME/node" --silent --ignore-scripts \
        "@askjo/camofox-browser@^1.5.2" \
        >"$log_file" 2>&1; then
        log_error "npm install failed or timed out:"
        cat "$log_file" >&2
        rm -f "$log_file"
        return 1
    fi
    rm -f "$log_file"
    export PATH="$HERMES_HOME/node/bin:$PATH"

    strip_snap_browser_override
    local sys_browser
    sys_browser="$(find_system_browser 2>/dev/null || true)"
    if [ -n "$sys_browser" ]; then
        configure_browser_env_from_system_browser "$sys_browser"
        log_info "Explicit browser override set -- Chromium download will be skipped when agent-browser installs on demand"
    fi

    return 0
}

ensure_mode() {
    detect_os

    IFS=',' read -ra DEPS <<< "$ENSURE_DEPS"
    for dep in "${DEPS[@]}"; do
        dep="$(echo "$dep" | tr -d '[:space:]')"
        case "$dep" in
            node)
                check_node
                ;;
            browser)
                check_node
                if [ "$HAS_NODE" = true ]; then
                    ensure_browser
                fi
                ;;
            ripgrep)
                if ! command -v rg &>/dev/null; then
                    HAS_RIPGREP=false
                    HAS_FFMPEG=true
                    install_system_packages
                fi
                ;;
            ffmpeg)
                if ! command -v ffmpeg &>/dev/null; then
                    HAS_FFMPEG=false
                    HAS_RIPGREP=true
                    install_system_packages
                fi
                ;;
            *)
                log_warn "Unknown dependency: $dep"
                ;;
        esac
    done
}


# Clear the cached Electron download + any half-written unpacked output so the
# next `npm run pack` re-downloads and re-stages from scratch. A corrupt zip in
# the per-user Electron download cache - most often a partial/resumed download
# that leaves concatenated junk - makes electron-builder's `unpack-electron`
# extract a tree MISSING the electron binary, so the `electron`->`Hermes` rename
# dies with ENOENT and every re-run repeats the broken extraction forever. This
# is the bash sibling of install.ps1's Clear-ElectronBuildCache and the Python
# _purge_electron_build_cache() used by `hermes desktop`; install.sh was the only
# build path lacking it. Echoes the removed paths (one per line); best-effort.
clear_electron_build_cache() {
    local desktop_dir="$1"
    local removed=""

    # Per-user Electron download cache dirs, honoring the overrides @electron/get
    # respects, then the platform defaults (macOS: ~/Library/Caches/electron,
    # Linux: $XDG_CACHE_HOME/electron or ~/.cache/electron).
    local cache_dirs=()
    [ -n "${electron_config_cache:-}" ] && cache_dirs+=("$electron_config_cache")
    [ -n "${ELECTRON_CACHE:-}" ] && cache_dirs+=("$ELECTRON_CACHE")
    if [ "$OS" = "macos" ]; then
        cache_dirs+=("$HOME/Library/Caches/electron")
    else
        [ -n "${XDG_CACHE_HOME:-}" ] && cache_dirs+=("$XDG_CACHE_HOME/electron")
        cache_dirs+=("$HOME/.cache/electron")
    fi

    local dir zip
    for dir in "${cache_dirs[@]}"; do
        [ -d "$dir" ] || continue
        # Recurse: the bad copy may be the top-level zip OR a copy inside an
        # @electron/get hash subdir.
        while IFS= read -r zip; do
            [ -n "$zip" ] || continue
            if rm -f "$zip" 2>/dev/null; then
                removed="$removed$zip
"
            fi
        done <<EOF
$(find "$dir" -type f -name 'electron-*.zip' 2>/dev/null)
EOF
    done

    # A half-written unpacked dir from an interrupted prior pack poisons the
    # rename even after the zip is fixed (mac-arm64-unpacked / linux-unpacked).
    local release_dir="$desktop_dir/release"
    if [ -d "$release_dir" ]; then
        local unpacked
        while IFS= read -r unpacked; do
            [ -n "$unpacked" ] || continue
            if rm -rf "$unpacked" 2>/dev/null; then
                removed="$removed$unpacked
"
            fi
        done <<EOF
$(find "$release_dir" -maxdepth 1 -type d -name '*-unpacked' 2>/dev/null)
EOF
    fi

    printf '%s' "$removed"
}

# Run the desktop pack in $1 (the apps/desktop dir). `npm run pack` = tsc +
# vite build + electron-builder --dir, producing an unpacked app for the
# current OS. Signing auto-discovery is disabled so electron-builder falls back
# to an ad-hoc signature instead of grabbing an unrelated Developer ID from the
# keychain (a real signed/notarized .dmg needs Apple credentials — a separate
# release concern). Optional $2 = an ELECTRON_MIRROR base URL for this attempt,
# used as a fallback when the default GitHub release download is blocked.
_desktop_pack() {
    local desktop_dir="$1"
    local mirror="${2:-}"
    if [ -n "$mirror" ]; then
        ( cd "$desktop_dir" && ELECTRON_MIRROR="$mirror" CSC_IDENTITY_AUTO_DISCOVERY=false npm run pack )
    else
        ( cd "$desktop_dir" && CSC_IDENTITY_AUTO_DISCOVERY=false npm run pack )
    fi
}

# Last-resort Electron mirror after GitHub download fails (#47266).
DESKTOP_ELECTRON_FALLBACK_MIRROR="https://npmmirror.com/mirrors/electron/"

# Per-attempt wall-clock cap for the desktop npm install / electron-builder pack
# (#39219). A stalled (not failed) Electron download on a throttled/blocked link
# never returns, so without this the installer hangs forever on "Build desktop
# app". 900s is generous enough for a slow-but-progressing ~150MB fetch + build;
# override with DESKTOP_BUILD_TIMEOUT for very slow links.
DESKTOP_BUILD_TIMEOUT="${DESKTOP_BUILD_TIMEOUT:-900}"

# Wall-clock cap for the plain registry `npm install`s (browser-tools + TUI
# deps). Same #39219 stall class but no ~150MB Electron binary, so a shorter
# default; override with NODE_DEPS_TIMEOUT for very slow links.
NODE_DEPS_TIMEOUT="${NODE_DEPS_TIMEOUT:-600}"

# Electron package dir — workspace-local nest first, then root hoist.
_electron_dir() {
    local install_dir="$1"
    if [ -d "$install_dir/apps/desktop/node_modules/electron" ]; then
        printf '%s\n' "$install_dir/apps/desktop/node_modules/electron"
    else
        printf '%s\n' "$install_dir/node_modules/electron"
    fi
}

# True when dist/ holds a usable Electron binary (#38673 / run-electron-builder.mjs).
_electron_dist_ok() {
    local install_dir="$1"
    local electron_dir
    electron_dir="$(_electron_dir "$install_dir")"
    if [ "$OS" = "macos" ]; then
        [ -e "$electron_dir/dist/Electron.app/Contents/MacOS/Electron" ]
    else
        [ -e "$electron_dir/dist/electron" ]
    fi
}

# Best-effort: run electron/install.js to populate dist/ (optional mirror).
_restore_electron_dist() {
    local install_dir="$1"
    local mirror="${2:-}"
    local electron_dir
    electron_dir="$(_electron_dir "$install_dir")"
    _electron_dist_ok "$install_dir" && return 0

    [ -f "$electron_dir/install.js" ] || return 1
    command -v node >/dev/null 2>&1 || return 1

    rm -rf "$electron_dir/dist" 2>/dev/null || true
    rm -f "$electron_dir/path.txt" 2>/dev/null || true

    if [ -n "$mirror" ]; then
        ( cd "$electron_dir" && ELECTRON_MIRROR="$mirror" node install.js ) || true
    else
        ( cd "$electron_dir" && node install.js ) || true
    fi
    _electron_dist_ok "$install_dir"
}

_electron_pkg_staged_missing_dist() {
    local install_dir="$1"
    local electron_dir
    electron_dir="$(_electron_dir "$install_dir")"
    [ -f "$electron_dir/package.json" ] && [ -f "$electron_dir/install.js" ] && ! _electron_dist_ok "$install_dir"
}

_restore_electron_dist_with_fallback() {
    local install_dir="$1"
    _restore_electron_dist "$install_dir" \
        || { [ -z "${ELECTRON_MIRROR:-}" ] && _restore_electron_dist "$install_dir" "$DESKTOP_ELECTRON_FALLBACK_MIRROR"; }
}

# Build apps/desktop into a launchable native app. Mirrors install.ps1's
# Install-Desktop: a root-level npm install so the apps/* workspace resolves
# the desktop's own deps (Electron ~150MB), then `npm run pack`
# (electron-builder --dir) which emits an unpacked app for the current OS. Only invoked
# via the 'desktop' stage / --include-desktop, which the Electron app's own
# first-launch bootstrap never requests (it must not rebuild itself).
install_desktop_voice_deps() {
    # Desktop ships with working voice out of the box: eagerly install the
    # wake-word + local-STT stacks ([wake] + [voice] extras) instead of
    # leaving them to lazy first-use install. Policy change (Teknium, July
    # 2026, #70509 testing): the first ear-click used to trigger a
    # multi-minute onnxruntime pip install that froze the UI and blew RPC
    # timeouts. Lazy install remains the fallback for CLI-only installs and
    # for anything this best-effort step fails to fetch.
    local _prev_venv="${VIRTUAL_ENV:-}"
    if [ "$USE_VENV" = true ]; then
        export VIRTUAL_ENV="$INSTALL_DIR/venv"
    fi
    if [ -z "${UV_CMD:-}" ]; then
        install_uv || true
    fi
    if [ -z "${UV_CMD:-}" ]; then
        log_warn "uv unavailable — voice/wake deps will lazy-install at first use instead"
        return 0
    fi
    log_info "Installing voice + wake-word dependencies (onnxruntime, faster-whisper — 1-3min)..."
    if (cd "$INSTALL_DIR" && $UV_CMD pip install -e ".[wake,voice]") ; then
        log_success "Voice + wake-word dependencies installed"
    else
        log_warn "Voice/wake dependency install failed — they will lazy-install at first use"
    fi
    if [ "$USE_VENV" = true ] && [ -z "$_prev_venv" ]; then
        unset VIRTUAL_ENV
    fi
    return 0
}

install_desktop() {
    local desktop_dir="$INSTALL_DIR/apps/desktop"

    # The desktop stage only runs when a build is explicitly requested
    # (--include-desktop / 'desktop' stage), so a missing toolchain is a hard
    # failure, not a silent skip — a silent skip yields a "complete" install
    # with no app and a confusing "couldn't find a built desktop" at launch.
    # Always re-resolve Node here. Stages run in separate processes, so we can't
    # trust an earlier check; more importantly check_node now enforces the build
    # floor (Node >=26) and prepends the Hermes-managed Node to PATH, so
    # the build never runs on a too-old system Node — the cause of the opaque
    # "Build desktop app … exit code 1" failure (Vite crashes on old Node).
    check_node
    if ! command -v npm >/dev/null 2>&1; then
        log_error "Cannot build desktop app: Node.js / npm unavailable"
        log_info "Install Node.js and retry: cd $desktop_dir && npm run pack"
        return 1
    fi
    if [ ! -f "$desktop_dir/package.json" ]; then
        log_warn "Skipping desktop build (apps/desktop not present in checkout)"
        return 0
    fi

    # 1. Root workspace install so apps/desktop's deps (Electron, Vite,
    #    node-pty prebuilds) resolve. The browser-tools install runs in the
    #    repo-root package workspace, which does not pull apps/* deps.
    #
    #    Prefer `npm ci`: it deletes node_modules and reinstalls from the
    #    lockfile, so it always produces a complete tree. Bare `npm install`
    #    can report "up to date" against a stale node_modules/.package-lock.json
    #    marker while node_modules is actually empty (Windows workspace-hoisting
    #    flake) — leaving tsc/typescript unresolved and `npm run pack`'s
    #    `tsc -b` failing with no obvious cause. Fall back to `npm install`
    #    only if `npm ci` is unavailable or the lockfile is out of sync.
    #
    #    Both the install and the build below are wrapped in a hard wall-clock
    #    timeout (#39219): the Electron binary (~150MB) is fetched from GitHub,
    #    and on a throttled/blocked connection that download can *stall* — npm
    #    neither errors nor exits, so the installer sits on "Build desktop app"
    #    forever with only `npm warn deprecated` lines visible. A stall now
    #    converts to a non-zero exit, which feeds the existing self-heal /
    #    mirror-fallback escalation instead of hanging the whole install.
    #
    #    The `npm ci` and its `npm install` fallback SHARE one budget: a stalled
    #    link wedges both identically, so giving each a full DESKTOP_BUILD_TIMEOUT
    #    would double the worst-case hang. We compute a single deadline and pass
    #    the remaining seconds to the fallback (min 30s so it still gets a real
    #    attempt if `npm ci` failed fast rather than stalling).
    log_info "Installing desktop workspace dependencies (includes Electron ~150MB, 1-3min)..."
    local _deps_start _deps_remaining
    _deps_start=$(date +%s)
    if run_with_timeout "$DESKTOP_BUILD_TIMEOUT" bash -c 'cd "$1" && npm ci' _ "$INSTALL_DIR"; then
        log_success "Desktop workspace dependencies installed"
    elif _deps_remaining=$(( DESKTOP_BUILD_TIMEOUT - ($(date +%s) - _deps_start) )); \
         [ "$_deps_remaining" -lt 30 ] && _deps_remaining=30; \
         run_with_timeout "$_deps_remaining" bash -c 'cd "$1" && npm install' _ "$INSTALL_DIR"; then
        log_success "Desktop workspace dependencies installed"
    elif _electron_pkg_staged_missing_dist "$INSTALL_DIR"; then
        log_warn "Desktop dependency install failed with a missing Electron dist; attempting self-heal..."
        _restore_electron_dist_with_fallback "$INSTALL_DIR" || true
    else
        log_error "Desktop workspace npm install failed"
        # Common cause: a previous 'sudo npm'/'sudo npx' left root-owned files in
        # ~/.npm, so this non-root install can't write the shared cache. npm hides
        # it behind a confusing EEXIST / "File exists" message while the real errno
        # is EACCES (-13). Point the user at the fix instead of a raw npm trace.
        log_info "If the errors above mention EACCES / 'permission denied' / EEXIST while"
        log_info "writing the npm cache, your ~/.npm likely holds root-owned files from an"
        log_info "earlier 'sudo npm' or 'sudo npx'. Reclaim ownership and retry:"
        log_info "  sudo chown -R \"\$(id -un)\" ~/.npm && npm cache verify"
        log_info "Then re-run this installer, or build manually:"
        log_info "  cd \"$INSTALL_DIR\" && npm ci && cd apps/desktop && npm run pack"
        return 1
    fi

    # 2. Build, with up to three escalating attempts so a transient/blocked
    #    Electron download self-heals instead of failing the whole install:
    #      a) plain `npm run pack` (downloads Electron from GitHub),
    #      b) on failure, purge a corrupt cached zip + stale unpacked dir and
    #         retry (matches install.ps1 / `hermes desktop`),
    #      c) on still-failing, fall back to a public Electron mirror — this is
    #         the GitHub-blocked/throttled case (the repeating "retrying" log).
    log_info "Building desktop app (this takes 1-3 minutes)..."
    local pack_ok=false
    if run_with_timeout "$DESKTOP_BUILD_TIMEOUT" _desktop_pack "$desktop_dir"; then
        pack_ok=true
    else
        local purged=""
        local restored=false
        if ! _electron_dist_ok "$INSTALL_DIR"; then
            purged="$(clear_electron_build_cache "$desktop_dir")"
            if _restore_electron_dist "$INSTALL_DIR"; then restored=true; fi
        fi
        if [ "$restored" = true ]; then
            log_warn "Desktop build failed; refreshed the Electron download and retrying once..."
            if run_with_timeout "$DESKTOP_BUILD_TIMEOUT" _desktop_pack "$desktop_dir"; then
                pack_ok=true
            fi
        fi
    fi

    # (c) GitHub blocked → mirror fallback (#47266).
    if [ "$pack_ok" = false ] && [ -z "${ELECTRON_MIRROR:-}" ]; then
        log_warn "Desktop build still failing — the Electron download from GitHub looks blocked."
        log_warn "Re-downloading Electron via a public mirror ($DESKTOP_ELECTRON_FALLBACK_MIRROR), then rebuilding..."
        log_warn "  (set ELECTRON_MIRROR yourself to use a different/trusted mirror)"
        _electron_dist_ok "$INSTALL_DIR" || _restore_electron_dist "$INSTALL_DIR" "$DESKTOP_ELECTRON_FALLBACK_MIRROR" || true
        if run_with_timeout "$DESKTOP_BUILD_TIMEOUT" _desktop_pack "$desktop_dir" "$DESKTOP_ELECTRON_FALLBACK_MIRROR"; then
            pack_ok=true
        fi
    fi

    if [ "$pack_ok" = false ]; then
        log_error "Desktop app build failed"
        # If the log shows repeated "retrying" lines fetching the Electron zip,
        # the binary download is blocked/throttled (firewall, proxy, region) and
        # the mirror fallback above also couldn't reach a host. Try a mirror you
        # trust and rebuild (@electron/get honors ELECTRON_MIRROR):
        log_info "If the log shows Electron download retries, rebuild via a reachable mirror:"
        log_info "  ELECTRON_MIRROR=<mirror-base-url> \\"
        log_info "    bash -c 'cd \"$desktop_dir\" && CSC_IDENTITY_AUTO_DISCOVERY=false npm run pack'"
        log_info "Otherwise build manually: cd $desktop_dir && npm run pack"
        return 1
    fi

    local app=""
    if [ "$OS" = "linux" ]; then
        if [ -x "$desktop_dir/release/linux-unpacked/OpenComputer" ]; then
            app="$desktop_dir/release/linux-unpacked/OpenComputer"
        elif [ -x "$desktop_dir/release/linux-unpacked/Hermes" ]; then
            app="$desktop_dir/release/linux-unpacked/Hermes"
        elif [ -x "$desktop_dir/release/linux-unpacked/hermes" ]; then
            app="$desktop_dir/release/linux-unpacked/hermes"
        fi
    else
        local cand
        for cand in \
            "$desktop_dir/release/mac-arm64/OpenComputer.app" \
            "$desktop_dir/release/mac/OpenComputer.app" \
            "$desktop_dir/release/mac-arm64/Hermes.app" \
            "$desktop_dir/release/mac/Hermes.app"; do
            if [ -d "$cand" ]; then
                app="$cand"
                break
            fi
        done
    fi
    if [ -z "$app" ]; then
        log_error "Desktop build completed but no app was found under $desktop_dir/release/"
        return 1
    fi
    log_success "Desktop app built: $app"

    # Linux: Electron's chrome-sandbox helper needs root:root 4755 or the
    # sandboxed renderer will abort on startup.  Check the file is a regular
    # file (not a symlink) before chown/chmod so we don't follow an
    # attacker-controlled link to an arbitrary path.
    if [ "$OS" = "linux" ]; then
        local sandbox="$desktop_dir/release/linux-unpacked/chrome-sandbox"
        if [ -f "$sandbox" ] && [ ! -L "$sandbox" ]; then
            if [ "$(id -u)" -eq 0 ]; then
                chown root:root "$sandbox" && chmod 4755 "$sandbox" || {
                    log_error "Cannot configure Electron sandbox helper: $sandbox"
                    return 1
                }
            elif command -v sudo >/dev/null 2>&1; then
                sudo chown root:root "$sandbox" && sudo chmod 4755 "$sandbox" || {
                    log_error "Cannot configure Electron sandbox helper (sudo failed): $sandbox"
                    return 1
                }
            else
                log_error "Cannot configure Electron sandbox helper without sudo: $sandbox"
                return 1
            fi
        fi
    fi

    # macOS: route through the same config-aware signing fixup as
    # `hermes desktop`, so install/repair and self-update agree about the app's
    # identity. The fixup preserves the Electron entitlement plists and signs
    # with a stable Designated Requirement (configured keychain identity, else
    # identifier-pinned ad-hoc), so macOS TCC grants — Full Disk Access,
    # Desktop/Downloads/Documents, Accessibility, microphone — survive the
    # rebuild instead of resetting on every update. The shell's
    # publisher-signing decision governed the build and is passed explicitly so
    # importing Python cannot reverse it by loading HERMES_HOME/.env. If the
    # helper is unavailable or fails, branch into the historical quarantine
    # strip + deep ad-hoc repair so a broken venv never leaves the bundle
    # unsigned/unlaunchable.
    if [ "$OS" = "macos" ] && [ -z "${CSC_LINK:-}" ] && [ -z "${APPLE_SIGNING_IDENTITY:-}" ] && command -v codesign >/dev/null 2>&1; then
        local config_python="$INSTALL_DIR/venv/bin/python"
        local fixup_ok=""
        if [ -x "$config_python" ]; then
            if HERMES_HOME="$HERMES_HOME" "$config_python" - "$desktop_dir" <<'PYEOF'
import sys
from pathlib import Path
from hermes_cli.main import _desktop_macos_relaunchable_fixup
ok = _desktop_macos_relaunchable_fixup(
    Path(sys.argv[1]), publisher_signing_configured=False
)
sys.exit(0 if ok else 1)
PYEOF
            then
                fixup_ok=1
            else
                log_warn "Config-aware macOS signing fixup failed; applying the historical ad-hoc fallback."
            fi
        fi
        if [ -z "$fixup_ok" ]; then
            xattr -cr "$app" 2>/dev/null || true
            codesign --force --deep --sign - "$app" >/dev/null 2>&1 || true
        fi
    fi

    # `npm install` + `npm run pack` rewrite lockfiles; restore them so the
    # checkout stays clean for the next `hermes update`.
    restore_dirty_lockfiles "$INSTALL_DIR"
}

# Each --stage runs in its own process, so (unlike the monolithic main() where
# clone_repo cd's once and later steps inherit it) a stage that operates on the
# checkout must cd into it explicitly. Without this, install_deps/setup_path run
# from the desktop app's cwd and resolve `.` / the venv against the wrong tree.
require_install_dir() {
    if [ -z "$INSTALL_DIR" ] || [ ! -d "$INSTALL_DIR" ]; then
        log_error "Install directory not found: ${INSTALL_DIR:-<unset>}"
        log_info "The 'repository' stage must run before this one."
        return 1
    fi
    cd "$INSTALL_DIR"
}

# Desktop bootstrap stage protocol. Mirrors the Windows install.ps1 surface
# closely enough for the Electron bootstrap runner to show structured progress.
run_stage_body() {
    local stage="$1"

    case "$stage" in
        prerequisites)
            print_banner
            detect_os
            resolve_install_layout
            install_uv
            check_python
            check_git
            check_node
            check_cxx_compiler
            check_network_prerequisites
            install_system_packages
            ;;
        repository)
            detect_os
            resolve_install_layout
            check_git
            clone_repo
            ;;
        venv)
            detect_os
            resolve_install_layout
            require_install_dir
            install_uv
            check_python
            setup_venv
            ;;
        python-deps)
            detect_os
            resolve_install_layout
            require_install_dir
            install_uv
            check_python
            install_deps
            ;;
        node-deps)
            detect_os
            resolve_install_layout
            require_install_dir
            check_node
            install_node_deps || return
            install_uv
            install_browser_use_cli
            install_computer_use_driver
            ;;
        path)
            detect_os
            resolve_install_layout
            require_install_dir
            setup_path
            ;;
        config)
            detect_os
            resolve_install_layout
            require_install_dir
            copy_config_templates
            ;;
        setup)
            detect_os
            resolve_install_layout
            require_install_dir
            run_setup_wizard
            ;;
        gateway)
            detect_os
            resolve_install_layout
            require_install_dir
            maybe_start_gateway
            ;;
        desktop)
            detect_os
            resolve_install_layout
            require_install_dir
            # Each stage runs in its own process, so the Hermes-managed Node
            # provisioned during prerequisites/node-deps (at $HERMES_HOME/node/bin)
            # isn't on PATH here. check_node re-adds it (or installs if missing)
            # so install_desktop can find npm instead of silently skipping.
            check_node
            install_desktop_voice_deps
            install_desktop
            ;;
        complete)
            detect_os
            resolve_install_layout
            print_success
            write_bootstrap_marker
            # Code-scoped stamp: write next to the install tree, not into
            # $HERMES_HOME. $HERMES_HOME is a shared data dir (it can be
            # bind-mounted into a Docker gateway too), so a stamp there gets
            # clobbered by the container's 'docker' stamp and wrongly blocks
            # 'hermes update' on this host install. See detect_install_method().
            echo "git" > "$INSTALL_DIR/.install_method"
=======
        fish)
            append_shell_path "$HOME/.config/fish/config.fish" 'fish_add_path "$HOME/.local/bin"' '^[[:space:]]*fish_add_path.*\.local/bin'
>>>>>>> upstream/main
            ;;
        *)
            append_shell_path "$HOME/.bashrc" "$SHELL_PATH_LINE" "$SHELL_PATH_SETUP_RE"
            append_shell_path "$HOME/.profile" "$SHELL_PATH_LINE" "$SHELL_PATH_SETUP_RE"
            # A login bash reads only the first of .bash_profile, .bash_login, .profile,
            # so an existing earlier file hides the .profile line above.
            local rc
            for rc in "$HOME/.bash_profile" "$HOME/.bash_login"; do
                if [ -f "$rc" ]; then
                    append_shell_path "$rc" "$SHELL_PATH_LINE" "$SHELL_PATH_SETUP_RE"
                fi
            done
            ;;
    esac
}

stage_products() {
    # The whole tail in one place, by calling the completion an update calls:
    # publish the commands, build the products (tui/web, plus the desktop app),
    # then run the post-build maintenance that syncs bundled skills and migrates
    # config. Node, browsers and the frontend build tools arrive through pm as
    # the build asks for them; the bootstrap interpreter itself only re-enters
    # the tree on PM's selected Python.
    local boot_py
    local args=(--source "$INSTALL_DIR")
    bootstrap_python
    if [ "$INCLUDE_DESKTOP" = true ] || desktop_product_present; then
        args+=(--desktop)
    fi
    (cd "$INSTALL_DIR" && run_logged "Building the hermes command and apps" \
        "$boot_py" -I -B -X utf8 hermes_cli/source_completion.py "${args[@]}") \
        || fail "app products or command publication failed" products_build_failed
    wire_shell_path
    log_success "app products and hermes command ready"
}

stage_desktop() {
    # External-caller contract: `--stage desktop` stays dispatchable on its own
    # (the manifest never lists it now -- --include-desktop selects the desktop
    # product inside `products`). Same completion call, desktop selected.
    INCLUDE_DESKTOP=true
    stage_products
}

stage_config() {
    mkdir -p "$HERMES_HOME"/cron "$HERMES_HOME"/sessions "$HERMES_HOME"/logs \
        "$HERMES_HOME"/pairing "$HERMES_HOME"/hooks "$HERMES_HOME"/image_cache \
        "$HERMES_HOME"/audio_cache "$HERMES_HOME"/memories "$HERMES_HOME"/skills
    if [ ! -f "$HERMES_HOME/.env" ]; then
        cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env" 2>/dev/null || touch "$HERMES_HOME/.env"
    fi
    chmod 600 "$HERMES_HOME/.env"
    if [ ! -f "$HERMES_HOME/config.yaml" ] && [ -f "$INSTALL_DIR/cli-config.yaml.example" ]; then
        cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
    fi
    log_success "config prepared in $HERMES_HOME"
}

# Interactive stages read the terminal, not stdin: under `curl | bash` stdin
# IS the script. Probe by opening /dev/tty -- a Docker build has the device
# node in its mount namespace but opening it fails (ENXIO).
has_terminal() { (: </dev/tty) 2>/dev/null; }

stage_setup() {
    if [ "$NON_INTERACTIVE" = true ]; then return 0; fi
    if ! has_terminal; then
        log "setup skipped (no terminal); run 'hermes setup' after install"
        return 0
    fi
    "$INSTALL_DIR/.hermes/bin/hermes" setup </dev/tty || fail "setup failed" setup_failed
}

stage_gateway() {
    if [ "$NON_INTERACTIVE" = true ]; then return 0; fi
    if ! has_terminal; then
        log "gateway setup skipped (no terminal); run 'hermes gateway install' after install"
        return 0
    fi
    # Setup installs the service when it handles the gateway; ask only if it did not.
    "$INSTALL_DIR/.hermes/bin/hermes" gateway install --if-missing </dev/tty || fail "gateway installation failed" gateway_failed
}

stage_complete() {
    local commit
    commit="$INSTALL_COMMIT"
    [ -n "$commit" ] || commit=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null) || commit=""
    if [ -n "$commit" ]; then
        printf '{\n  "schemaVersion": 1,\n  "pinnedCommit": "%s",\n  "pinnedBranch": "%s",\n  "completedAt": "%s"\n}\n' \
            "$commit" "$BRANCH" "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" > "$INSTALL_DIR/.hermes-bootstrap-complete.tmp"
        mv -f "$INSTALL_DIR/.hermes-bootstrap-complete.tmp" "$INSTALL_DIR/.hermes-bootstrap-complete"
    fi
    log_success "Hermes Agent install complete. Run: hermes"
}

print_path_reload_hint() {
    # The rc files only reach shells started later, and this installer is
    # always a child (`curl | bash`, `bash install.sh`) that cannot change its
    # parent's PATH. The inherited PATH is the parent's, so it says whether
    # the user can run `hermes` right away.
    case ":$PATH:" in *":$HOME/.local/bin:"*|*":$HOME/.local/bin/:"*) return 0 ;; esac
    local rc
    local login_shell="${SHELL:-}"
    case "${login_shell##*/}" in
        zsh) rc="source ~/.zshrc" ;;
        fish) rc="source ~/.config/fish/config.fish" ;;
        bash|"") rc="source ~/.bashrc" ;;
        *) rc=". ~/.profile" ;;
    esac
    log "Reload your shell to use hermes: open a new terminal, or run: $rc"
}

run_stage() (
    # Keep failure handling out of conditional calls, which disable errexit.
    set -e
    STAGE="$1"
    STAGE_REASON=""
    FAILURE_CLASS=""
    STAGE_INTERRUPTED=false
    STAGE_SKIPPED=false
    trap 'stage_result "$?"' EXIT
    if [ "$INSTALL_LADDER" = true ]; then
        trap 'stage_signal "$?" 130' INT
        trap 'stage_signal "$?" 143' TERM
        trap 'stage_signal "$?" 129' HUP
    fi
    if [ "$NON_INTERACTIVE" = true ] && { [ "$STAGE" = setup ] || [ "$STAGE" = gateway ]; }; then
        STAGE_SKIPPED=true
        STAGE_REASON="needs user input"
        exit 0
    fi
    case "$1" in
        prerequisites) stage_prerequisites ;;
        repository) stage_repository ;;
        venv) stage_venv ;;
        python-deps) stage_python_deps ;;
        config) stage_config ;;
        products) stage_products ;;
        setup) stage_setup ;;
        gateway) stage_gateway ;;
        desktop) stage_desktop ;;
        complete) stage_complete ;;
        *) STAGE_REASON="unknown stage: $1"; printf '%s\n' "$STAGE_REASON" >&2; exit 2 ;;
    esac
)

# Main. Guarded so the script can be SOURCED for its functions (the
# installer-test harness sources it with --manifest, which must define
# the functions and stop before main). Under `curl | bash` BASH_SOURCE is
# empty, and `set -u` would abort on the bare expansion.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
    if [ "$WANT_MANIFEST" = true ]; then
        emit_manifest
        exit 0
    fi

    if [ -n "$STAGE" ] && [ "$JSON" = true ]; then
        trap 'stage_result "$?"' EXIT
    elif [ -z "$STAGE" ]; then
        INSTALL_LADDER=true
        INSTALL_STARTED="$(date +%s 2>/dev/null)"
        INSTALL_RUN_ID="$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')" || INSTALL_RUN_ID=""
        [ "${#INSTALL_RUN_ID}" -eq 32 ] || INSTALL_RUN_ID="$(printf '%08x%08x%08x%08x' "$(( RANDOM * 32768 + RANDOM ))" "$$" "${INSTALL_STARTED:-0}" "$RANDOM")"
        # No clock, no receipt: the reader drops a receipt without both timestamps anyway.
        case "$INSTALL_STARTED" in ''|*[!0-9]*) INSTALL_RUN_ID="" ;; esac
        trap '[ "$?" -eq 0 ] || write_install_receipt failed prerequisites "${FAILURE_CLASS:-other}"' EXIT
        # Ctrl-C, SIGTERM or a closed terminal (SIGHUP) anywhere in the run: one `interrupted` receipt.
        trap 'install_signal INT' INT
        trap 'install_signal TERM' TERM
        trap 'install_signal HUP' HUP
    fi
    check_platform
    trap - EXIT

    if [ -n "$STAGE" ]; then
        run_stage "$STAGE"
        exit "$?"
    fi

    # No --stage: run the whole ladder — the same authoritative list the
    # manifest prints, so --include-desktop inserts desktop here too.
    print_banner
    for s in $(stage_names); do
        # A TERM/HUP noted while the previous stage ran stops the run before this one.
        [ -z "$INSTALL_SIGNAL" ] || install_interrupted
        INSTALL_IN_STAGE=true
        run_stage "$s"
        rc=$?
        INSTALL_IN_STAGE=false
        if [ "$rc" -eq 0 ]; then
            # The stage succeeded, so a Ctrl-C it saw was handled there: carry on.
            [ "$INSTALL_SIGNAL" != INT ] || INSTALL_SIGNAL=""
            continue
        fi
        case "$rc" in
            129) INSTALL_SIGNAL="${INSTALL_SIGNAL:-HUP}" ;;
            130) INSTALL_SIGNAL="${INSTALL_SIGNAL:-INT}" ;;
            143) INSTALL_SIGNAL="${INSTALL_SIGNAL:-TERM}" ;;
        esac
        [ -z "$INSTALL_SIGNAL" ] || install_interrupted
        # The stage normally wrote its receipt; one killed outright (SIGKILL, OOM) did not.
        write_install_receipt failed "${s//-/_}" other
        exit "$rc"
    done
    write_install_receipt success none none
    # Every stage finished: the receipt says success, and a pending TERM/HUP still ends the run.
    [ -z "$INSTALL_SIGNAL" ] || install_interrupted
    trap - INT TERM HUP
    print_path_reload_hint
fi
