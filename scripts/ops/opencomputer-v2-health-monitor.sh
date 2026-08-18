#!/usr/bin/env bash
set -u

# Lightweight LMI OpenComputer v2 health monitor. No model calls and no
# credentials are printed. Intended for the existing two-minute root cron.

[ -f /etc/opencomputer/telegram-monitor.env ] && . /etc/opencomputer/telegram-monitor.env

readonly SERVICE="opencomputer-v2-gateway.service"
readonly SERVE_SERVICE="opencomputer-v2-serve.service"
readonly TUNNEL_SERVICE="opencomputer-v2-fixed-tunnel.service"
readonly HOME_DIR="/opt/opencomputer-v2-data"
readonly STATE_FILE="${HOME_DIR}/.health-monitor-state"
readonly LOG_FILE="${HOME_DIR}/logs/health-monitor.log"
readonly MAX_LOG_LINES=5000

mkdir -p "${HOME_DIR}/logs"

send_alert() {
  [ -n "${BOT_TOKEN:-}" ] && [ -n "${CHAT_ID:-}" ] || return 0
  curl -fsS --max-time 10 -X POST \
    "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" -d text="$1" -d parse_mode="Markdown" \
    >/dev/null 2>&1 || true
}

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >>"${LOG_FILE}"
}

if [ -f "${LOG_FILE}" ] && [ "$(wc -l <"${LOG_FILE}")" -gt "${MAX_LOG_LINES}" ]; then
  tail -n 2000 "${LOG_FILE}" >"${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "${LOG_FILE}"
fi

prev_state="ok"
[ -f "${STATE_FILE}" ] && prev_state="$(head -1 "${STATE_FILE}")"
issues=""
warnings=""

if ! systemctl is-active --quiet "${SERVICE}"; then
  issues="${issues}• OpenComputer v2 gateway is DOWN\n"
fi
if ! systemctl is-active --quiet "${SERVE_SERVICE}"; then
  issues="${issues}• OpenComputer v2 Desktop backend is DOWN\n"
fi
if ! systemctl is-active --quiet "${TUNNEL_SERVICE}"; then
  issues="${issues}• OpenComputer v2 Desktop tunnel is DOWN\n"
fi

for protected in lmi-chrome.service lmi-query-api.service oc-panel-proxy.service; do
  if ! systemctl is-active --quiet "${protected}"; then
    issues="${issues}• Protected service ${protected} is DOWN\n"
  fi
done

for port in 8642 8643 8645 8646 8650 8651 29129; do
  if ! ss -ltn 2>/dev/null | grep -qE ":${port}[[:space:]]"; then
    issues="${issues}• Required port ${port} is not listening\n"
  fi
done

gateway_health="$(curl -fsS --max-time 3 http://127.0.0.1:8642/health 2>/dev/null || true)"
[ -n "${gateway_health}" ] || warnings="${warnings}• Gateway health check failed\n"

serve_health="$(curl -fsS --max-time 3 http://127.0.0.1:29129/api/status 2>/dev/null || true)"
if ! printf '%s' "${serve_health}" | grep -q '"gateway_running":true'; then
  warnings="${warnings}• Desktop backend does not report gateway_running=true\n"
fi

shadow_health="$(curl -fsS --max-time 3 http://127.0.0.1:18650/healthz 2>/dev/null || true)"
if ! printf '%s' "${shadow_health}" | grep -q '"ok":true'; then
  warnings="${warnings}• LMI shadow reader health check failed\n"
fi

if ! grep -qE '^[[:space:]]*enabled:[[:space:]]*true' "${HOME_DIR}/config.yaml"; then
  warnings="${warnings}• No enabled integration found in v2 config\n"
fi

free_kb="$(df / --output=avail | tail -1 | tr -d ' ')"
free_gb=$((free_kb / 1048576))
[ "${free_gb}" -ge 2 ] || warnings="${warnings}• Disk space low: ${free_gb}GB free\n"

avail_mb="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)"
[ "${avail_mb}" -ge 200 ] || warnings="${warnings}• Memory low: ${avail_mb}MB available\n"

if [ -n "${issues}" ]; then
  current_state="down"
elif [ -n "${warnings}" ]; then
  current_state="degraded"
else
  current_state="ok"
fi

if [ "${current_state}" = "down" ] && [ "${prev_state}" != "down" ]; then
  log "ALERT: ${prev_state} -> down"
  send_alert "🚨 *OpenComputer v2 DOWN*\n\n${issues}${warnings}"
  if ! systemctl is-active --quiet "${SERVICE}"; then
    log "AUTO-RECOVERY: restarting ${SERVICE}"
    systemctl restart "${SERVICE}" || true
  fi
elif [ "${current_state}" = "degraded" ] && [ "${prev_state}" = "ok" ]; then
  log "WARN: ok -> degraded"
  send_alert "⚠️ *OpenComputer v2 degraded*\n\n${warnings}"
elif [ "${current_state}" = "ok" ] && [ "${prev_state}" != "ok" ]; then
  log "RECOVERED: ${prev_state} -> ok"
  send_alert "✅ *OpenComputer v2 recovered*\nAll checks passing."
fi

printf '%s\n' "${current_state}" >"${STATE_FILE}"
log "${current_state^^}: gateway=${SERVICE} adapters=8643,8645,8646 disk=${free_gb}GB mem=${avail_mb}MB"

exit 0
