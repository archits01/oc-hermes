#!/usr/bin/env bash
set -Eeuo pipefail

# Controlled LMI engagement ownership cutover.
#
# Keeps the production scraper, Chrome, query API, panel proxy, database, and
# sender in place. Only the gateway that owns the Unipile adapter ports moves
# from hermes-gateway.service to opencomputer-v2-gateway.service.

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

readonly OLD_SERVICE="hermes-gateway.service"
readonly NEW_SERVICE="opencomputer-v2-gateway.service"
readonly OLD_HOME="/root/.hermes"
readonly NEW_HOME="/opt/opencomputer-v2-data"
readonly NEW_REPO="/opt/opencomputer-v2"
readonly BACKUP_ROOT="/opt/opencomputer-v2-backups"
readonly LOCK_FILE="/run/opencomputer-v2-engagement-cutover.lock"

exec 9>"${LOCK_FILE}"
flock -n 9 || { echo "ERROR: another cutover is running" >&2; exit 1; }

for required in \
  "${OLD_HOME}/config.yaml" \
  "${OLD_HOME}/.env" \
  "${OLD_HOME}/plugins/platforms" \
  "${NEW_HOME}/config.yaml" \
  "${NEW_HOME}/runtime.env" \
  "${NEW_REPO}/venv/bin/hermes" \
  "/var/lib/lmi-dashboard/unipile_webhooks.db"; do
  [[ -e "${required}" ]] || { echo "ERROR: missing ${required}" >&2; exit 1; }
done

for protected_service in lmi-chrome.service lmi-query-api.service oc-panel-proxy.service; do
  systemctl is-active --quiet "${protected_service}" || {
    echo "ERROR: protected service is not active: ${protected_service}" >&2
    exit 1
  }
done

if [[ -z "${SARVAM_API_KEY:-}" ]]; then
  echo "ERROR: SARVAM_API_KEY must be supplied through the process environment" >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="${BACKUP_ROOT}/engagement-cutover-${timestamp}"
install -d -m 0700 "${backup}"
cp -a "${NEW_HOME}/config.yaml" "${backup}/config.yaml"
cp -a "${NEW_HOME}/runtime.env" "${backup}/runtime.env"
if [[ -d "${NEW_HOME}/plugins" ]]; then
  cp -a "${NEW_HOME}/plugins" "${backup}/plugins"
fi
if [[ -d /etc/systemd/system/opencomputer-v2-gateway.service.d ]]; then
  cp -a /etc/systemd/system/opencomputer-v2-gateway.service.d "${backup}/service.d"
fi
systemctl is-enabled "${OLD_SERVICE}" >"${backup}/old-enabled-state" 2>/dev/null || true
printf '%s\n' "${backup}" >"${NEW_HOME}/.engagement-cutover-backup"
chmod 0600 "${NEW_HOME}/.engagement-cutover-backup"

rollback() {
  local rc=$?
  trap - ERR INT TERM
  echo "ROLLBACK: restoring pre-cutover gateway state" >&2
  systemctl stop "${NEW_SERVICE}" >/dev/null 2>&1 || true
  cp -a "${backup}/config.yaml" "${NEW_HOME}/config.yaml"
  cp -a "${backup}/runtime.env" "${NEW_HOME}/runtime.env"
  rm -rf "${NEW_HOME}/plugins"
  [[ -d "${backup}/plugins" ]] && cp -a "${backup}/plugins" "${NEW_HOME}/plugins"
  rm -rf /etc/systemd/system/opencomputer-v2-gateway.service.d
  [[ -d "${backup}/service.d" ]] && cp -a "${backup}/service.d" /etc/systemd/system/opencomputer-v2-gateway.service.d
  systemctl daemon-reload
  systemctl enable "${OLD_SERVICE}" >/dev/null 2>&1 || true
  systemctl restart "${OLD_SERVICE}" >/dev/null 2>&1 || true
  systemctl restart "${NEW_SERVICE}" >/dev/null 2>&1 || true
  exit "${rc}"
}
trap rollback ERR INT TERM

# Copy only the active adapter sources. Historical .bak files are deliberately
# excluded, and the production originals remain untouched.
install -d -m 0700 "${NEW_HOME}/plugins/platforms"
cp -a "${OLD_HOME}/plugins/platforms/_unipile_common.py" "${NEW_HOME}/plugins/platforms/"
for platform in linkedin instagram whatsapp_unipile messenger_unipile; do
  rm -rf "${NEW_HOME}/plugins/platforms/${platform}"
  install -d -m 0700 "${NEW_HOME}/plugins/platforms/${platform}"
  for source in __init__.py adapter.py plugin.yaml; do
    cp -a "${OLD_HOME}/plugins/platforms/${platform}/${source}" \
      "${NEW_HOME}/plugins/platforms/${platform}/${source}"
  done
done

# Current OpenComputer loads user platform adapters in isolated plugin
# packages, so the production adapters' old gateway.platforms import is not
# resolvable. Keep the shared helper inside each copied plugin package.
for platform in instagram whatsapp_unipile; do
  cp -a "${OLD_HOME}/plugins/platforms/_unipile_common.py" \
    "${NEW_HOME}/plugins/platforms/${platform}/_unipile_common.py"
  sed -i 's/from gateway\.platforms\._unipile_common import (/from ._unipile_common import (/' \
    "${NEW_HOME}/plugins/platforms/${platform}/adapter.py"
done

# Re-scope copied adapters to the new agent home while retaining the existing
# lead database and read-only leadgen safety modules.
find "${NEW_HOME}/plugins/platforms" -type f \( -name '*.py' -o -name '*.yaml' \) -print0 |
  xargs -0 sed -i \
    -e 's#/root/unipile_webhooks.db#/var/lib/lmi-dashboard/unipile_webhooks.db#g' \
    -e 's#/root/.hermes/state.db#/opt/opencomputer-v2-data/state.db#g' \
    -e 's#/root/.hermes/.env#/opt/opencomputer-v2-data/runtime.env#g' \
    -e 's#/root/.hermes/bolna_call_logs.db#/opt/opencomputer-v2-data/legacy_call_logs.db#g' \
    -e 's#/root/leadgen/escalations.jsonl#/opt/opencomputer-v2-data/escalations.jsonl#g'

# The current LinkedIn adapter is Sarvam-backed but its legacy manifest still
# required the retired Bolna key. Correct only the copied v2 manifest.
sed -i \
  -e 's/BOLNA_API_KEY/SARVAM_API_KEY/g' \
  -e 's/Bolna API key for voice calls/Sarvam Voice Agents API key for calls/g' \
  "${NEW_HOME}/plugins/platforms/linkedin/plugin.yaml"

# The production Instagram adapter retains an owner-disabled Bolna tool block.
# Do not carry that dead provider surface into the new authoritative agent;
# voice calls are exposed through Sarvam instead.
python3 - "${NEW_HOME}/plugins/platforms/instagram/adapter.py" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text()
start_marker = "    # --- make_call (Bolna voice - in-conversation escalation) ---\n"
end_marker = "    # --- instagram_get_messages ---\n"
if start_marker in text and end_marker in text:
    before, remainder = text.split(start_marker, 1)
    _, after = remainder.split(end_marker, 1)
    text = before + "    # Voice calls are provided by the Sarvam MCP.\n\n" + end_marker + after
path.write_text(text)
PY

# Build a VM-only environment without printing values. Existing OpenComputer
# router/API settings are preserved; connector credentials are copied from the
# protected production environment and never enter the DMG.
python3 - "${OLD_HOME}/.env" "${NEW_HOME}/runtime.env" <<'PY'
import os
import re
import sys
from pathlib import Path

source_path = Path(sys.argv[1])
target_path = Path(sys.argv[2])
line_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

def read_raw(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        match = line_re.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    return values

old = read_raw(source_path)
new = read_raw(target_path)
copy_names = {
    "LINKEDIN_UNIPILE_DSN", "LINKEDIN_UNIPILE_API_KEY", "LINKEDIN_ACCOUNT_ID",
    "LINKEDIN_ALLOWED_USERS", "LINKEDIN_ALLOW_ALL_USERS", "LINKEDIN_HOME_CHANNEL",
    "LINKEDIN_WEBHOOK_PORT", "INSTAGRAM_UNIPILE_DSN", "INSTAGRAM_UNIPILE_API_KEY",
    "INSTAGRAM_ACCOUNT_ID", "INSTAGRAM_ALLOWED_USERS", "INSTAGRAM_ALLOW_ALL_USERS",
    "INSTAGRAM_WEBHOOK_PORT", "WHATSAPP_UNIPILE_DSN", "WHATSAPP_UNIPILE_API_KEY",
    "WHATSAPP_ACCOUNT_ID", "WHATSAPP_ALLOWED_USERS", "WHATSAPP_ALLOW_ALL_USERS",
    "WHATSAPP_WEBHOOK_PORT", "MESSENGER_UNIPILE_DSN", "MESSENGER_UNIPILE_API_KEY",
    "MESSENGER_ACCOUNT_ID", "MESSENGER_ALLOW_ALL_USERS", "MESSENGER_WEBHOOK_PORT",
    "WEBHOOK_SECRET", "OPENAI_API_KEY", "OPENAI_BASE_URL",
}
for name in copy_names:
    if name in old:
        new[name] = old[name]

unipile_key = old.get("LINKEDIN_UNIPILE_API_KEY") or old.get("UNIPILE_NEW_API_KEY")
unipile_dsn = old.get("LINKEDIN_UNIPILE_DSN")
if not unipile_key or not unipile_dsn:
    raise SystemExit("required Unipile credentials are missing")
new["UNIPILE_API_KEY"] = unipile_key
new["UNIPILE_DSN"] = unipile_dsn

new.update({
    "API_SERVER_ENABLED": "true",
    "API_SERVER_HOST": "127.0.0.1",
    "API_SERVER_PORT": "8642",
    "SARVAM_API_KEY": os.environ["SARVAM_API_KEY"],
    "SARVAM_ORG_ID": "019fdb7b-803d-7759-a858-5f5b218de1c7",
    "SARVAM_WORKSPACE_ID": "019fdb7b-8041-739a-8319-758202013953",
    "SARVAM_APP_ID": "Conversatio-6a703fde-e360",
    "SARVAM_AGENT_ID": "Conversatio-6a703fde-e360",
    "SARVAM_APP_VERSION": "3",
    "SARVAM_CONNECTION_ID": "930c47ab-3e-e89d8b8c-e66e",
    "SARVAM_AGENT_PHONE_NUMBER": "+918065353722",
    "SARVAM_FROM_NUMBER": "+918065353722",
})
target_path.write_text("".join(f"{key}={value}\n" for key, value in sorted(new.items())))
target_path.chmod(0o600)
PY

# Copy the platform enablement shape but never copy secret-bearing extra keys.
# MCPs are enabled only after their stdio transports pass a standalone test.
python3 - "${OLD_HOME}/config.yaml" "${NEW_HOME}/config.yaml" <<'PY'
import sys
from pathlib import Path
import yaml

old_path, new_path = map(Path, sys.argv[1:3])
old = yaml.safe_load(old_path.read_text()) or {}
new = yaml.safe_load(new_path.read_text()) or {}
safe_extra = {"account_id", "webhook_port"}
platforms = {}
for name in ("linkedin", "instagram", "whatsapp_unipile", "messenger_unipile"):
    src = (old.get("platforms") or {}).get(name) or {}
    dst = {
        "enabled": bool(src.get("enabled", False)),
        "gateway_restart_notification": bool(src.get("gateway_restart_notification", False)),
    }
    extra = src.get("extra") or {}
    cleaned = {key: value for key, value in extra.items() if key in safe_extra}
    if cleaned:
        dst["extra"] = cleaned
    platforms[name] = dst
new["platforms"] = platforms
plugins = new.setdefault("plugins", {})
enabled_plugins = set(plugins.get("enabled") or [])
enabled_plugins.update({
    "platforms/linkedin",
    "platforms/instagram",
    "platforms/whatsapp_unipile",
})
plugins["enabled"] = sorted(enabled_plugins)
gateway = new.setdefault("gateway", {})
gateway["enabled"] = [
    "platforms/linkedin",
    "platforms/instagram",
    "platforms/whatsapp_unipile",
]

unipile = new["mcp_servers"]["unipile"]
unipile["env"] = {
    "UNIPILE_API_KEY": "${UNIPILE_API_KEY}",
    "UNIPILE_DSN": "${UNIPILE_DSN}",
}
sarvam = new["mcp_servers"]["sarvam-voice"]
sarvam["env"] = {
    name: "${" + name + "}"
    for name in (
        "SARVAM_API_KEY",
        "SARVAM_ORG_ID",
        "SARVAM_WORKSPACE_ID",
        "SARVAM_AGENT_ID",
        "SARVAM_APP_VERSION",
        "SARVAM_CONNECTION_ID",
        "SARVAM_FROM_NUMBER",
    )
}
for name in ("sarvam-voice", "unipile"):
    if name in (new.get("mcp_servers") or {}):
        new["mcp_servers"][name]["enabled"] = False
new_path.write_text(yaml.safe_dump(new, sort_keys=False))
new_path.chmod(0o600)
PY

install -d -m 0755 /etc/systemd/system/opencomputer-v2-gateway.service.d
cat >/etc/systemd/system/opencomputer-v2-gateway.service.d/20-lmi-engagement.conf <<'EOF'
[Service]
ProtectHome=read-only
ReadWritePaths=/opt/opencomputer-v2-data /var/lib/lmi-dashboard
EOF

python3 -m compileall -q "${NEW_HOME}/plugins/platforms"
systemctl daemon-reload

# Validate the generic MCPs without making an outbound message or phone call.
set -a
# shellcheck disable=SC1091
. "${NEW_HOME}/runtime.env"
set +a
export HERMES_HOME="${NEW_HOME}"
"${NEW_REPO}/venv/bin/hermes" mcp test sarvam-voice >/tmp/opencomputer-sarvam-mcp-test.log 2>&1
"${NEW_REPO}/venv/bin/hermes" mcp test unipile >/tmp/opencomputer-unipile-mcp-test.log 2>&1
grep -q 'Tools discovered:' /tmp/opencomputer-sarvam-mcp-test.log
grep -q 'Tools discovered:' /tmp/opencomputer-unipile-mcp-test.log

python3 - "${NEW_HOME}/config.yaml" <<'PY'
import sys
from pathlib import Path
import yaml
path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text()) or {}
for name in ("sarvam-voice", "unipile"):
    data["mcp_servers"][name]["enabled"] = True
path.write_text(yaml.safe_dump(data, sort_keys=False))
path.chmod(0o600)
PY

# Single-owner handoff: old adapters release the ports before v2 starts.
systemctl stop "${NEW_SERVICE}" >/dev/null 2>&1 || true
systemctl stop "${OLD_SERVICE}"
systemctl disable "${OLD_SERVICE}" >/dev/null
systemctl start "${NEW_SERVICE}"

deadline=$((SECONDS + 180))
while (( SECONDS < deadline )); do
  if systemctl is-active --quiet "${NEW_SERVICE}" && \
     curl -fsS --max-time 3 http://127.0.0.1:8642/health >/dev/null && \
     ss -ltn | grep -qE ':8643[[:space:]]' && \
     ss -ltn | grep -qE ':8645[[:space:]]' && \
     ss -ltn | grep -qE ':8646[[:space:]]'; then
    break
  fi
  sleep 2
done

systemctl is-active --quiet "${NEW_SERVICE}"
curl -fsS --max-time 5 http://127.0.0.1:8642/health >/dev/null
for port in 8643 8645 8646; do
  ss -ltn | grep -qE ":${port}[[:space:]]"
done
systemctl is-active --quiet lmi-chrome.service
systemctl is-active --quiet lmi-query-api.service
systemctl is-active --quiet oc-panel-proxy.service
! systemctl is-active --quiet "${OLD_SERVICE}"

cat >"${backup}/CUTOVER-MANIFEST.txt" <<EOF
timestamp_utc=${timestamp}
old_gateway=${OLD_SERVICE}:disabled,inactive,preserved
new_gateway=${NEW_SERVICE}:enabled,active
protected_services=lmi-chrome.service,lmi-query-api.service,oc-panel-proxy.service
engagement_ports=8643,8645,8646
lead_database=/var/lib/lmi-dashboard/unipile_webhooks.db
new_repo_commit=$(git -C "${NEW_REPO}" rev-parse HEAD)
EOF
chmod 0600 "${backup}/CUTOVER-MANIFEST.txt"

trap - ERR INT TERM
echo "CUTOVER_OK backup=${backup}"
