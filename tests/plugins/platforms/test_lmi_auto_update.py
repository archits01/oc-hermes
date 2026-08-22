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


@pytest.mark.parametrize("blackout_hhmm", ["0800", "0830", "0900", "0959"])
def test_noop_drift_defers_in_blackout_then_repairs_and_restarts(tmp_path, blackout_hhmm):
    """A no-op tick must not consume overlay drift during a restart blackout."""
    import os
    import subprocess
    import sys

    repo = tmp_path / "repo"
    home = tmp_path / "data"
    fake_bin = tmp_path / "bin"
    repo_git = repo / ".git"
    python_dir = repo / "venv" / "bin"
    ops_dir = repo / "scripts" / "ops"
    repo_git.mkdir(parents=True)
    python_dir.mkdir(parents=True)
    ops_dir.mkdir(parents=True)
    home.mkdir()
    (home / "config.yaml").write_text("platforms: {}\n", encoding="utf-8")
    fake_media = ops_dir / "lmi_media_overlay_sync.py"
    fake_ports = ops_dir / "lmi_enabled_platform_ports.py"
    fake_media.write_text("# fake media sync\n", encoding="utf-8")
    fake_ports.write_text("# fake platform helper\n", encoding="utf-8")

    marker = tmp_path / "overlay-repaired"
    restarts = tmp_path / "restarts"
    fake_python = python_dir / "python"
    fake_python.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "name = pathlib.Path(sys.argv[1]).name\n"
        "if name == 'lmi_media_overlay_sync.py':\n"
        "    if '--check' in sys.argv and not pathlib.Path(os.environ['SYNC_MARKER']).exists():\n"
        "        raise SystemExit(1)\n"
        "    pathlib.Path(os.environ['SYNC_MARKER']).write_text('repaired')\n"
        "elif name == 'lmi_enabled_platform_ports.py':\n"
        "    print('8645 8646')\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_bin.mkdir()

    (fake_bin / "git").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "a = sys.argv\n"
        "if 'remote' in a and 'get-url' in a: print('https://github.com/archits01/oc-hermes.git')\n"
        "elif 'rev-parse' in a and 'HEAD' in a: print('same-head')\n"
        "elif 'rev-parse' in a and 'origin/oc-branding' in a: print('same-head')\n"
        "elif 'branch' in a: print('oc-branding')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    (fake_bin / "systemctl").write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'restart':\n"
        "    with pathlib.Path(os.environ['RESTARTS']).open('a') as f: f.write(sys.argv[2] + '\\n')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    (fake_bin / "ss").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'LISTEN 0 0 127.0.0.1:8645 0.0.0.0:*' 'LISTEN 0 0 127.0.0.1:8646 0.0.0.0:*'\n",
        encoding="utf-8",
    )
    (fake_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "timeout").write_text(
        "#!/bin/sh\n"
        "shift\n"
        "\"$@\"\n",
        encoding="utf-8",
    )
    (fake_bin / "date").write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  +%u) echo 1 ;;\n"
        "  +%H%M) echo \"${FAKE_HHMM:-1500}\" ;;\n"
        "  *) /bin/date \"$@\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (fake_bin / "curl").write_text(
        "#!" + sys.executable + "\nprint('{\"gateway_running\":true}')\n",
        encoding="utf-8",
    )
    for command in ("git", "systemctl", "ss", "flock", "timeout", "date", "curl"):
        (fake_bin / command).chmod(0o755)

    script = OPS / "lmi_opencomputer_v2_auto_update.sh"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "LMI_AUTO_UPDATE_REPO": str(repo),
            "LMI_AUTO_UPDATE_HOME": str(home),
            "LMI_AUTO_UPDATE_LOG": str(tmp_path / "update.log"),
            "LMI_AUTO_UPDATE_LOCK": str(tmp_path / "update.lock"),
            "LMI_AUTO_UPDATE_MEDIA_SYNC": str(fake_media),
            "LMI_AUTO_UPDATE_PLATFORM_PORTS_HELPER": str(fake_ports),
            "LMI_AUTO_UPDATE_RESTART_SETTLE_SECONDS": "0",
            "SYNC_MARKER": str(marker),
            "RESTARTS": str(restarts),
            "FAKE_HHMM": blackout_hhmm,
        }
    )

    first = subprocess.run([str(script)], env=env, text=True, capture_output=True)
    assert first.returncode == 0, first.stderr
    assert "value too great for base" not in first.stderr
    assert not marker.exists()
    assert not restarts.exists()
    assert "OVERLAY DRIFT detected during restart blackout; repair deferred" in (
        tmp_path / "update.log"
    ).read_text(encoding="utf-8")

    env["FAKE_HHMM"] = "1500"
    second = subprocess.run([str(script)], env=env, text=True, capture_output=True)
    assert second.returncode == 0, second.stderr
    assert marker.exists(), second.stderr
    assert marker.read_text(encoding="utf-8") == "repaired"
    assert restarts.read_text(encoding="utf-8").splitlines() == [
        "opencomputer-v2-gateway",
        "opencomputer-v2-serve",
    ]
