"""Offline contract tests for the LMI OpenCode Free refresh scheduler."""
from __future__ import annotations

import subprocess
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts/ops/install_lmi_free_model_catalog_refresh_cron.sh"


def test_scheduler_render_is_one_exact_locked_daily_job_without_credentials():
    rendered = subprocess.run(
        ["bash", str(INSTALLER), "--render"], check=True, text=True,
        capture_output=True,
    ).stdout.strip()
    assert rendered.count("\n") == 0
    assert rendered.startswith("17 3 * * * HERMES_HOME=/opt/opencomputer-v2-data ")
    assert "flock -n /opt/opencomputer-v2-data/locks/lmi-free-model-catalog-refresh.lock" in rendered
    assert "/opt/opencomputer-v2/venv/bin/python /opt/opencomputer-v2/scripts/ops/lmi_free_model_catalog_refresh.py" in rendered
    assert ">> /opt/opencomputer-v2-data/logs/lmi-free-model-catalog-refresh.log 2>&1" in rendered
    assert "# opencomputer-lmi-free-model-catalog-refresh" in rendered
    assert "KEY=" not in rendered and "TOKEN=" not in rendered and "SECRET=" not in rendered


def test_scheduler_dry_run_is_non_mutating_and_renders_same_job():
    dry_run = subprocess.run(
        ["bash", str(INSTALLER), "--dry-run"], check=True, text=True,
        capture_output=True,
    ).stdout
    assert "would preserve unrelated crontab lines" in dry_run
    assert dry_run.count("# opencomputer-lmi-free-model-catalog-refresh") == 1


def _fake_crontab(tmp_path: Path, *, read_error: bool = False) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    state = tmp_path / "crontab.txt"
    state.write_text("9 9 * * * keep-this-job\n")
    script = bin_dir / "crontab"
    script.write_text(
        "#!/usr/bin/env bash\nset -eu\n"
        f"state={state!s}\n"
        + (
            "if [[ ${1:-} == -l ]]; then echo 'permission denied' >&2; exit 13; fi\n"
            if read_error else
            "if [[ ${1:-} == -l ]]; then cat \"$state\"; exit 0; fi\n"
        )
        + "if [[ ${1:-} == - ]]; then cat > \"$state\"; exit 0; fi\nexit 64\n"
    )
    script.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}:{env.get('PATH','')}",
        "LMI_FREE_MODEL_CHECKOUT": str(tmp_path / "checkout"),
        "LMI_FREE_MODEL_DATA_DIR": str(tmp_path / "data"),
    })
    return env, state


def test_scheduler_install_preserves_unrelated_jobs_and_is_idempotent(tmp_path):
    env, state = _fake_crontab(tmp_path)
    for _ in range(2):
        subprocess.run(["bash", str(INSTALLER), "--install"], env=env, check=True)
    installed = state.read_text()
    assert "keep-this-job" in installed
    assert installed.count("# opencomputer-lmi-free-model-catalog-refresh") == 1


def test_scheduler_read_failure_refuses_without_overwriting(tmp_path):
    env, state = _fake_crontab(tmp_path, read_error=True)
    before = state.read_bytes()
    result = subprocess.run(
        ["bash", str(INSTALLER), "--install"], env=env, text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "refused: could not read current crontab" in result.stderr
    assert state.read_bytes() == before
