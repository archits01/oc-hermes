"""Tests for the lightweight LMI updater and guarded cron migration."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


OPS = Path(__file__).resolve().parents[3] / "scripts/ops"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cron_migration_replaces_only_the_exact_legacy_line():
    migration = load("lmi_install_auto_update_cron_test", OPS / "lmi_install_auto_update_cron.py")
    before = "MAILTO=\"\"\n" + migration.OLD_CRON_LINE + "\n15 * * * * /bin/true\n"
    after = migration.migrate_crontab(before)
    assert migration.OLD_CRON_LINE not in after
    assert after.splitlines().count(migration.NEW_CRON_LINE) == 1
    assert "MAILTO=\"\"" in after
    assert "15 * * * * /bin/true" in after


def test_cron_migration_is_idempotent():
    migration = load("lmi_install_auto_update_cron_idempotent_test", OPS / "lmi_install_auto_update_cron.py")
    current = migration.NEW_CRON_LINE + "\n"
    assert migration.migrate_crontab(current) == current


@pytest.mark.parametrize(
    "crontab",
    [
        "MAILTO=\"\"\n",
        "\n".join(
            [
                "23 * * * * /usr/bin/flock -n /tmp/lmi-oc-auto-update.lock /opt/opencomputer-v2-data/scripts/oc-auto-update.sh",
                "23 * * * * /usr/bin/flock -n /tmp/lmi-oc-auto-update.lock /opt/opencomputer-v2-data/scripts/oc-auto-update.sh",
            ]
        )
        + "\n",
    ],
)
def test_cron_migration_blocks_missing_or_duplicate_legacy_line(crontab):
    migration = load("lmi_install_auto_update_cron_guard_test", OPS / "lmi_install_auto_update_cron.py")
    with pytest.raises(migration.CronMigrationBlocked):
        migration.migrate_crontab(crontab)


def test_lightweight_updater_contains_sync_and_safe_update_gates():
    source = (OPS / "lmi_opencomputer_v2_auto_update.sh").read_text(encoding="utf-8")
    assert "hermes update" not in source
    assert "--check" in source
    assert "lmi_media_overlay_sync.py" in source
    assert "lmi_enabled_platform_ports.py" in source
    assert "git_repo merge --ff-only" in source
    assert "in_restart_blackout" in source
    assert "services_ready" in source
    assert "8643 8645 8646" not in source
