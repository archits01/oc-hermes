#!/usr/bin/env bash
set -Eeuo pipefail

# Finalize the LMI OpenComputer v2 cutover without changing the protected
# scraper, sender, query API, dashboard proxy, database, or active engagement
# schedules. Every changed legacy artifact is copied to a root-only backup.

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

readonly HOME_DIR="/opt/opencomputer-v2-data"
readonly BACKUP_ROOT="/opt/opencomputer-v2-backups"
readonly TEST_CRON="/etc/cron.d/opencomputer-test-profile-cron"
readonly LINKEDIN_DIR="${HOME_DIR}/plugins/platforms/linkedin"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="${BACKUP_ROOT}/finalize-cleanup-${timestamp}"
install -d -m 0700 "${backup}"

crontab -l >"${backup}/root.crontab.before"
chmod 0600 "${backup}/root.crontab.before"

# The retired Bolna synchronization worker is incompatible with the Sarvam
# cutover. Comment only that exact active line; preserve every LMI pipeline,
# reply, sender, scraper, maintenance, and reporting schedule byte-for-byte.
python3 - "${backup}/root.crontab.before" "${backup}/root.crontab.after" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text().splitlines()
out = []
changed = 0
for line in source:
    if line.lstrip().startswith("#") or "bolna_sync_agents.py" not in line:
        out.append(line)
        continue
    out.append("# [RETIRED-SARVAM-CUTOVER] " + line)
    changed += 1
if changed != 1:
    raise SystemExit(f"expected exactly one active Bolna sync schedule, found {changed}")
Path(sys.argv[2]).write_text("\n".join(out) + "\n")
PY
chmod 0600 "${backup}/root.crontab.after"
crontab "${backup}/root.crontab.after"

# The old multiplex test stack is parked. Its separate profile ticker must not
# keep launching old binaries every two minutes after the services are gone.
if [[ -f "${TEST_CRON}" ]]; then
  cp -a "${TEST_CRON}" "${backup}/opencomputer-test-profile-cron"
  rm -f "${TEST_CRON}"
fi

# Messenger is explicitly disabled in the v2 config and was never part of the
# active three-adapter cutover. Remove its retired Bolna implementation from
# the runtime while retaining the complete copy in this rollback directory.
if [[ -d "${HOME_DIR}/plugins/platforms/messenger_unipile" ]]; then
  cp -a "${HOME_DIR}/plugins/platforms/messenger_unipile" "${backup}/"
  rm -rf "${HOME_DIR}/plugins/platforms/messenger_unipile"
fi

# The active LinkedIn call tool already uses Sarvam. Remove only the retired
# external Bolna callback route and neutralize the shared persistence helper's
# provider-specific name/log text. The old query API endpoint remains an
# internal compatibility sink until the protected dashboard API is migrated.
python3 - "${LINKEDIN_DIR}/adapter.py" "${LINKEDIN_DIR}/plugin.yaml" <<'PY'
import sys
from pathlib import Path

adapter = Path(sys.argv[1])
manifest = Path(sys.argv[2])
text = adapter.read_text()
text = text.replace(
    '        app.router.add_post("/bolna/call-complete", self._handle_bolna_webhook)\n',
    '',
)
text = text.replace('self._handle_bolna_webhook(_NormalizedRequest())', 'self._handle_voice_webhook(_NormalizedRequest())')
text = text.replace('async def _handle_bolna_webhook(self, request)', 'async def _handle_voice_webhook(self, request)')
text = text.replace('# ------------------------------------------------------------------ bolna webhook', '# ------------------------------------------------------------------ voice webhook')
text = text.replace('Receive post-call data from Bolna voice agent.', 'Persist normalized post-call data from the configured voice provider.')
text = text.replace('logger.warning("[bolna] forward to query API failed for %s", execution_id)', 'logger.warning("[voice] forward to query API failed for %s", execution_id)')
text = text.replace('"[bolna] call logged: id=%s status=%s to=%s duration=%.1fs cost=%.2f",', '"[voice] call logged: id=%s status=%s to=%s duration=%.1fs cost=%.2f",')
text = text.replace('"bolna_webhook":"active","sarvam_webhook":"active"', '"sarvam_webhook":"active"')
adapter.write_text(text)

mtext = manifest.read_text()
mtext = mtext.replace('prompt: "Bolna API key"', 'prompt: "Sarvam Voice Agents API key"')
manifest.write_text(mtext)
PY

python3 -m py_compile "${LINKEDIN_DIR}/adapter.py"
systemctl restart opencomputer-v2-gateway.service

deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
  if systemctl is-active --quiet opencomputer-v2-gateway.service && \
     curl -fsS --max-time 3 http://127.0.0.1:8642/health >/dev/null && \
     ss -ltn | grep -qE ':8643[[:space:]]' && \
     ss -ltn | grep -qE ':8645[[:space:]]' && \
     ss -ltn | grep -qE ':8646[[:space:]]'; then
    break
  fi
  sleep 2
done

systemctl is-active --quiet opencomputer-v2-gateway.service
curl -fsS --max-time 5 http://127.0.0.1:8642/health >/dev/null
for port in 8643 8645 8646; do
  ss -ltn | grep -qE ":${port}[[:space:]]"
done

printf '%s\n' \
  "timestamp_utc=${timestamp}" \
  "retired_cron=bolna_sync_agents.py" \
  "removed_test_ticker=${TEST_CRON}" \
  "removed_disabled_plugin=messenger_unipile" \
  "active_voice_provider=sarvam" \
  "protected_jobs=unchanged_except_exact_retired_cron" \
  "gateway=opencomputer-v2-gateway.service:active" \
  "engagement_ports=8643,8645,8646" \
  >"${backup}/MANIFEST.txt"
chmod 0600 "${backup}/MANIFEST.txt"

echo "FINALIZE_OK backup=${backup}"
