#!/usr/bin/env bash
# Install one safe daily OpenCode Free catalog refresh job on LMI-PI-01.
set -euo pipefail

readonly checkout="${LMI_FREE_MODEL_CHECKOUT:-/opt/opencomputer-v2}"
readonly data_dir="${LMI_FREE_MODEL_DATA_DIR:-/opt/opencomputer-v2-data}"
readonly marker='# opencomputer-lmi-free-model-catalog-refresh'
readonly job="17 3 * * * HERMES_HOME=${data_dir} flock -n ${data_dir}/locks/lmi-free-model-catalog-refresh.lock ${checkout}/venv/bin/python ${checkout}/scripts/ops/lmi_free_model_catalog_refresh.py >> ${data_dir}/logs/lmi-free-model-catalog-refresh.log 2>&1 ${marker}"

render() {
  printf '%s\n' "$job"
}

current_crontab() {
  local error_file status
  error_file="$(mktemp)"
  if crontab -l 2>"$error_file"; then
    rm -f "$error_file"
    return 0
  else
    status=$?
  fi
  if grep -Eqi 'no crontab for|no crontab' "$error_file"; then
    rm -f "$error_file"
    return 0
  fi
  printf 'refused: could not read current crontab (status %s)\n' "$status" >&2
  rm -f "$error_file"
  return "$status"
}

check() {
  local current matches
  current="$(current_crontab)"
  matches="$(printf '%s\n' "$current" | grep -Fxc "$job" || true)"
  [[ "$matches" == 1 ]] && [[ "$(printf '%s\n' "$current" | grep -Fc "$marker" || true)" == 1 ]]
}

case "${1:---check}" in
  --render)
    render
    ;;
  --check)
    if check; then
      printf 'installed: exact daily LMI free-model refresh job present\n'
    else
      printf 'missing or non-canonical LMI free-model refresh job\n' >&2
      exit 1
    fi
    ;;
  --dry-run)
    printf 'would preserve unrelated crontab lines and install:\n'
    render
    ;;
  --install)
    install -d -m 700 "${data_dir}/locks" "${data_dir}/logs"
    current="$(current_crontab)"
    # Only replace our own marker line; no unrelated cron entries are changed.
    { printf '%s\n' "$current" | grep -Fv "$marker" || true; render; } | crontab -
    check
    printf 'installed: exact daily LMI free-model refresh job present\n'
    ;;
  *)
    printf 'usage: %s [--render|--check|--dry-run|--install]\n' "$0" >&2
    exit 64
    ;;
esac
