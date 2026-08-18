#!/usr/bin/env bash
set -Eeuo pipefail

# Controlled update for the LMI OpenComputer v2 pilot.
#
# The VM agent follows the same oc-branding fork as the Desktop build, but it
# updates only through this explicit owner action.  Customer credentials and
# LMI adapters remain in HERMES_HOME, outside the git checkout.

readonly REPO="/opt/opencomputer-v2"
readonly HOME_DIR="/opt/opencomputer-v2-data"
readonly SERVICE="opencomputer-v2-gateway.service"
readonly BRANCH="oc-branding"
readonly EXPECTED_REMOTE="https://github.com/archits01/hermes-agent.git"
readonly BACKUP_ROOT="/opt/opencomputer-v2-backups"
readonly LOCK_FILE="/run/opencomputer-v2-update.lock"
readonly QUEUE_HELPER="${REPO}/plugins/platforms/_lmi_live_reply_queue.py"
readonly LOCAL_DESKTOP_STATUS="http://127.0.0.1:29129/api/status"

engagement_ports_ready() {
  local port
  for port in 8643 8645 8646; do
    ss -ltn | grep -qE ":${port}[[:space:]]" || return 1
  done
}

desktop_status_ready() {
  curl -fsS --max-time 5 "${LOCAL_DESKTOP_STATUS}" |
    python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("gateway_running") is True else 1)'
}

mode="apply"
if [[ ${1:-} == "--check" ]]; then
  mode="check"
elif [[ $# -ne 0 ]]; then
  echo "usage: $0 [--check]" >&2
  exit 2
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

for required in \
  "${REPO}/.git" \
  "${REPO}/venv/bin/hermes" \
  "${HOME_DIR}/config.yaml" \
  "${HOME_DIR}/runtime.env" \
  "${HOME_DIR}/plugins/platforms"; do
  [[ -e ${required} ]] || { echo "ERROR: missing ${required}" >&2; exit 1; }
done

origin="$(git -C "${REPO}" remote get-url origin)"
if [[ ${origin} != "${EXPECTED_REMOTE}" ]]; then
  echo "ERROR: refusing unexpected origin: ${origin}" >&2
  exit 1
fi

exec 9>"${LOCK_FILE}"
flock -n 9 || { echo "ERROR: another VM update is running" >&2; exit 1; }

export HERMES_HOME="${HOME_DIR}"

if [[ ${mode} == "check" ]]; then
  "${REPO}/venv/bin/hermes" update --check --branch "${BRANCH}"
  systemctl is-active --quiet "${SERVICE}"
  desktop_status_ready
  engagement_ports_ready
  echo "CHECK_OK branch=${BRANCH} service=${SERVICE}"
  exit 0
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="${BACKUP_ROOT}/controlled-update-${timestamp}"
install -d -m 0700 "${backup}"
cp -a "${HOME_DIR}/config.yaml" "${backup}/config.yaml"
cp -a "${HOME_DIR}/runtime.env" "${backup}/runtime.env"
cp -a "${HOME_DIR}/plugins" "${backup}/plugins"
[[ -f ${HOME_DIR}/SOUL.md ]] && cp -a "${HOME_DIR}/SOUL.md" "${backup}/SOUL.md"
[[ -f ${QUEUE_HELPER} ]] && cp -a "${QUEUE_HELPER}" "${backup}/_lmi_live_reply_queue.py"
git -C "${REPO}" rev-parse HEAD >"${backup}/commit.before"
git -C "${REPO}" status --short --untracked-files=no >"${backup}/tracked-status.before"

if [[ -s ${backup}/tracked-status.before ]]; then
  echo "ERROR: tracked checkout changes present; refusing update" >&2
  exit 1
fi

"${REPO}/venv/bin/hermes" update --branch "${BRANCH}" --yes --backup

# The queue helper is an LMI overlay, not upstream core. Restore it if the
# updater removed untracked files; never restore over a tracked upstream file.
if [[ ! -f ${QUEUE_HELPER} && -f ${backup}/_lmi_live_reply_queue.py ]]; then
  install -m 0644 "${backup}/_lmi_live_reply_queue.py" "${QUEUE_HELPER}"
fi

"${REPO}/venv/bin/python" -m py_compile \
  "${QUEUE_HELPER}" \
  "${HOME_DIR}/plugins/platforms/linkedin/adapter.py" \
  "${HOME_DIR}/plugins/platforms/instagram/adapter.py" \
  "${HOME_DIR}/plugins/platforms/whatsapp_unipile/adapter.py"

systemctl restart "${SERVICE}"
deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
  if systemctl is-active --quiet "${SERVICE}" && \
     desktop_status_ready && engagement_ports_ready; then
    break
  fi
  sleep 2
done

systemctl is-active --quiet "${SERVICE}"
desktop_status_ready
engagement_ports_ready
git -C "${REPO}" rev-parse HEAD >"${backup}/commit.after"

printf '%s\n' \
  "branch=${BRANCH}" \
  "service=${SERVICE}" \
  "backup=${backup}" \
  "health=ok" \
  "lmi_overlay=preserved" \
  >"${backup}/MANIFEST.txt"
chmod 0600 "${backup}/MANIFEST.txt"

echo "UPDATE_OK branch=${BRANCH} backup=${backup}"
