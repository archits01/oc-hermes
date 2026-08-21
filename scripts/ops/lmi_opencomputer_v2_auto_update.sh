#!/usr/bin/env bash
set -Eeuo pipefail

# Efficient hourly updater for the LMI pilot. This intentionally performs a
# fast-forward-only Git update and the narrow LMI overlay sync; it does not
# invoke the heavyweight Hermes CLI update path.

readonly REPO="/opt/opencomputer-v2"
readonly HOME_DIR="/opt/opencomputer-v2-data"
readonly BRANCH="oc-branding"
readonly EXPECTED_REMOTE="https://github.com/archits01/oc-hermes.git"
readonly LOG="/var/log/oc-auto-update.log"
readonly SERVICES=(opencomputer-v2-gateway opencomputer-v2-serve)
readonly LOCK_FILE="/run/opencomputer-v2-update.lock"
readonly MEDIA_SYNC="${REPO}/scripts/ops/lmi_media_overlay_sync.py"
readonly PLATFORM_PORTS_HELPER="${REPO}/scripts/ops/lmi_enabled_platform_ports.py"
readonly LOCAL_DESKTOP_STATUS="http://127.0.0.1:29129/api/status"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S %Z') $*" >>"${LOG}"; }

git_repo() {
  git -c "safe.directory=${REPO}" -C "${REPO}" "$@"
}

engagement_ports_ready() {
  local required_ports port
  required_ports="$(${REPO}/venv/bin/python "${PLATFORM_PORTS_HELPER}" "${HOME_DIR}/config.yaml")" || return 1
  for port in ${required_ports}; do
    ss -ltn | grep -qE ":${port}[[:space:]]" || return 1
  done
}

desktop_status_ready() {
  curl -fsS --max-time 5 "${LOCAL_DESKTOP_STATUS}" |
    python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("gateway_running") is True else 1)'
}

services_ready() {
  local service
  for service in "${SERVICES[@]}"; do
    systemctl is-active --quiet "${service}" || return 1
  done
  desktop_status_ready && engagement_ports_ready
}

in_restart_blackout() {
  local hhmm dow
  hhmm="$(TZ=Asia/Kolkata date +%H%M)"
  dow="$(TZ=Asia/Kolkata date +%u)"
  if [[ ${hhmm} -ge 0600 && ${hhmm} -lt 1400 ]]; then
    return 0
  fi
  if [[ ${dow} == 7 && ${hhmm} -ge 1845 && ${hhmm} -lt 2015 ]]; then
    return 0
  fi
  return 1
}

sync_overlay_if_needed() {
  SYNC_REPAIRED=0
  if "${REPO}/venv/bin/python" "${MEDIA_SYNC}" --check >/dev/null 2>&1; then
    return 0
  fi
  log "OVERLAY DRIFT detected; attempting Git-controlled repair"
  if ! "${REPO}/venv/bin/python" "${MEDIA_SYNC}"; then
    log "CRITICAL overlay sync failed; services NOT restarted"
    return 1
  fi
  SYNC_REPAIRED=1
  log "OVERLAY repaired from ${BRANCH} checkout"
}

restart_services() {
  local service
  for service in "${SERVICES[@]}"; do
    systemctl restart "${service}"
    sleep 15
    systemctl is-active --quiet "${service}" || {
      log "CRITICAL ${service} did not become active; updater will not claim health"
      return 1
    }
  done
  services_ready || {
    log "CRITICAL post-restart health gate failed; enabled platform port or desktop status missing"
    return 1
  }
}

[[ -d "${REPO}/.git" && -x "${REPO}/venv/bin/python" && -f "${HOME_DIR}/config.yaml" &&
   -f "${MEDIA_SYNC}" && -f "${PLATFORM_PORTS_HELPER}" ]] || {
  log "FATAL required updater path missing"
  exit 1
}

origin="$(git_repo remote get-url origin)"
[[ ${origin} == "${EXPECTED_REMOTE}" ]] || {
  log "FATAL unexpected origin ${origin}"
  exit 1
}

exec 9>"${LOCK_FILE}"
flock -n 9 || exit 0

if ! timeout 120 git_repo fetch --quiet origin "${BRANCH}" 2>>"${LOG}"; then
  log "WARN git fetch failed; retrying next tick"
  exit 0
fi

local_head="$(git_repo rev-parse HEAD)"
remote_head="$(git_repo rev-parse "origin/${BRANCH}")"
branch="$(git_repo branch --show-current)"
[[ ${branch} == "${BRANCH}" ]] || { log "SKIP checkout is on ${branch}"; exit 0; }

if [[ -n "$(git_repo status --porcelain --untracked-files=no)" ]]; then
  log "SKIP tracked checkout changes present"
  exit 0
fi

if [[ ${local_head} == ${remote_head} ]]; then
  # A no-op tick still repairs a deleted/mutated external overlay. During a
  # send blackout the repair is safe on disk but its restart waits for later.
  sync_overlay_if_needed || exit 1
  if [[ ${SYNC_REPAIRED} -eq 1 ]]; then
    if in_restart_blackout; then
      log "OVERLAY repaired during restart blackout; restart deferred"
      exit 0
    fi
    restart_services || exit 1
    log "DONE overlay repair ${local_head:0:9}"
  elif ! services_ready; then
    log "WARN no Git update; current services failed health gate"
  else
    log "NOOP ${local_head:0:9} healthy"
  fi
  exit 0
fi

if in_restart_blackout; then
  log "DEFER update ${local_head:0:9} -> ${remote_head:0:9} inside restart blackout"
  exit 0
fi

if ! git_repo merge-base --is-ancestor HEAD "origin/${BRANCH}"; then
  log "SKIP branch diverged; fast-forward required"
  exit 0
fi

if ! git_repo merge --ff-only "origin/${BRANCH}" >>"${LOG}" 2>&1; then
  log "SKIP fast-forward failed"
  exit 0
fi
log "MERGED ${local_head:0:9} -> ${remote_head:0:9}"

if git_repo diff --name-only "${local_head}" "${remote_head}" |
   grep -qE '(^|/)(pyproject\.toml|requirements[^/]*\.txt|setup\.(py|cfg))$'; then
  log "DEPS manifest changed; installing editable checkout"
  if ! timeout 900 "${REPO}/venv/bin/pip" install -e "${REPO}" --quiet >>"${LOG}" 2>&1; then
    log "CRITICAL dependency installation failed; services NOT restarted"
    exit 1
  fi
fi

if ! "${REPO}/venv/bin/python" "${MEDIA_SYNC}"; then
  log "CRITICAL overlay sync failed; services NOT restarted"
  exit 1
fi

restart_services || exit 1
log "DONE ${local_head:0:9} -> ${remote_head:0:9}"
