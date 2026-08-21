#!/usr/bin/env python3
"""Guarded migration from the deployment-owned updater to the Git updater."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


DATA_ROOT = Path("/opt/opencomputer-v2-data")
REPO_ROOT = Path("/opt/opencomputer-v2")
BACKUP_ROOT = Path("/opt/opencomputer-v2-backups")
OLD_WRAPPER = DATA_ROOT / "scripts/oc-auto-update.sh"
NEW_UPDATER = REPO_ROOT / "scripts/ops/lmi_opencomputer_v2_auto_update.sh"
OLD_CRON_LINE = (
    "23 * * * * /usr/bin/flock -n /tmp/lmi-oc-auto-update.lock "
    "/opt/opencomputer-v2-data/scripts/oc-auto-update.sh"
)
NEW_CRON_LINE = (
    "23 * * * * /usr/bin/flock -n /tmp/lmi-oc-auto-update.lock "
    "/opt/opencomputer-v2/scripts/ops/lmi_opencomputer_v2_auto_update.sh"
)


class CronMigrationBlocked(RuntimeError):
    """The expected single-line migration contract was not met."""


def migrate_crontab(text: str) -> str:
    lines = text.splitlines()
    old_matches = [index for index, line in enumerate(lines) if line == OLD_CRON_LINE]
    new_matches = [index for index, line in enumerate(lines) if line == NEW_CRON_LINE]
    if new_matches:
        if old_matches or len(new_matches) != 1:
            raise CronMigrationBlocked("cron contains conflicting updater lines")
        return text if text.endswith("\n") else text + "\n"
    if len(old_matches) != 1:
        raise CronMigrationBlocked("expected exactly one legacy updater cron line")
    lines[old_matches[0]] = NEW_CRON_LINE
    return "\n".join(lines) + "\n"


def _crontab() -> str:
    result = subprocess.run(
        ["crontab", "-l"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode not in (0, 1) or (result.returncode == 1 and result.stdout):
        raise CronMigrationBlocked("could not read root crontab")
    return result.stdout


def install() -> Path | None:
    if os.geteuid() != 0:
        raise CronMigrationBlocked("run as root")
    if not NEW_UPDATER.is_file() or not os.access(NEW_UPDATER, os.X_OK):
        raise CronMigrationBlocked("Git-controlled updater is unavailable or not executable")
    before = _crontab()
    after = migrate_crontab(before)
    if after == before:
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_ROOT / f"lmi-auto-update-cron-{stamp}-{os.getpid()}"
    backup.mkdir(mode=0o700, parents=True)
    (backup / "crontab.before").write_text(before, encoding="utf-8")
    os.chmod(backup / "crontab.before", 0o600)
    if OLD_WRAPPER.is_symlink():
        raise CronMigrationBlocked("legacy updater wrapper is a symlink")
    if OLD_WRAPPER.is_file():
        shutil.copy2(OLD_WRAPPER, backup / "oc-auto-update.sh")

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=backup, prefix="crontab.", delete=False
    ) as handle:
        handle.write(after)
        candidate = Path(handle.name)
    try:
        subprocess.run(["crontab", str(candidate)], check=True)
        installed = _crontab()
        if migrate_crontab(installed) != after or NEW_CRON_LINE not in installed.splitlines():
            raise CronMigrationBlocked("crontab verification failed after migration")
    except Exception:
        # Restore the exact pre-migration crontab if installation or
        # verification fails. The backup remains available for audit.
        restore = backup / "crontab.restore"
        restore.write_text(before, encoding="utf-8")
        try:
            subprocess.run(["crontab", str(restore)], check=True)
        finally:
            restore.unlink(missing_ok=True)
        raise
    finally:
        candidate.unlink(missing_ok=True)
    (backup / "MANIFEST.txt").write_text(
        f"old_line={OLD_CRON_LINE}\nnew_line={NEW_CRON_LINE}\n", encoding="utf-8"
    )
    os.chmod(backup / "MANIFEST.txt", 0o600)
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        backup = install()
    except (CronMigrationBlocked, OSError, subprocess.SubprocessError) as exc:
        print(f"CRON_MIGRATION_BLOCKED: {type(exc).__name__}")
        return 1
    print("CRON_ALREADY_MIGRATED" if backup is None else f"CRON_MIGRATION_OK backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
