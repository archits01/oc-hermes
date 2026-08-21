#!/usr/bin/env python3
"""Install the reviewed LMI media overlay into the external data tree.

The Hermes checkout is Git-controlled, while the LMI adapter copies live under
``/opt/opencomputer-v2-data`` so they can retain their deployment-owned
configuration.  This command is the narrow bridge between those trees.  It
copies only the manifest-listed overlay and adapter inputs, verifies every
source and destination hash, writes recoverable backups first, and never
restarts a service or calls a provider.

The normal updater invokes this command after a successful fast-forward and
before restarting Hermes.  Any failed preflight or post-write verification
exits non-zero, so the updater must leave the running services untouched.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = Path("/opt/opencomputer-v2-data")
DEFAULT_BACKUP_ROOT = Path("/opt/opencomputer-v2-backups")
LOCK_PATH = Path("/run/lmi-media-overlay-sync.lock")

# These are the exact reviewed hashes recorded by the overlay manifest.  The
# duplicate map makes a stale/mutated manifest or an accidental source swap a
# hard failure rather than a deployment decision.
EXPECTED_FILES = {
    "plugins/platforms/lmi_unipile_overlay/ADAPTER_HARDENING.md": "6da752854abfc932ec8588ca0422fe1c59ac5a22a685a2e3678f283c4fb88ab2",
    "plugins/platforms/lmi_unipile_overlay/README.md": "b8b4c62b67342b518b044ac6d7db542d206e6103bd3c390b5645a4c3346db5ed",
    "plugins/platforms/lmi_unipile_overlay/__init__.py": "cc7aef1f05acd0c310ebdffa1659f343508c971dd49a43422b1f9ee377b94b73",
    "plugins/platforms/lmi_unipile_overlay/_lmi_media_runtime.py": "371096adbf0af11eac1e84e7ddc3e792e80c07bfe530d300ae16fb33b57f60c9",
    "plugins/platforms/lmi_unipile_overlay/_lmi_media_bootstrap.py": "26bd4b5039236f11cd095fff413b49ec0c56a379e2f29caa228ac7bdd4cad854",
    "plugins/platforms/lmi_unipile_overlay/bridge.py": "0d210ad33761062c6fd9c0936b39c1c3eeab1af175123e4799ea153615fc3bf3",
    "plugins/platforms/lmi_unipile_overlay/deployment.py": "ee96f39c57c671357fead092eadd3708fbbeffa912d1e7c82f4599e4081a657a",
    "plugins/platforms/lmi_unipile_overlay/manifest.yaml": "7eed997fd0d10b5a5f2dd1c3495f18fab61d051fde7c175b31d68bc5d3bad6ad",
    "plugins/platforms/lmi_unipile_overlay/live_inputs/_unipile_common.py": "65af0cb2c98e1c11f348034dbf7a4fef1fc312cd7aedd5fc4395504339d6eb28",
    "plugins/platforms/lmi_unipile_overlay/live_inputs/whatsapp_unipile/adapter.py": "83f183bcffa483f4839414e33bc47008a29dfbce9b23abb8e0eb3619bf50ba6d",
    "plugins/platforms/lmi_unipile_overlay/live_inputs/instagram/adapter.py": "e7429a174336c28801c07d4f2a3bf287e84f5c48fe616905371f031e3aebd63e",
}

# The runtime module is supplied by the reviewed dashboard commit and is
# referenced by LMI_MEDIA_FOLLOWUP_MODULE_PATH in runtime.env.  It is checked
# here even though it is not copied by this script.
MEDIA_MODULE_SHA256 = "411efe57a621183bef06b64478fe8defdaa5602999e834397eaea77aeb8d86bb"

TARGETS = {
    "plugins/platforms/lmi_unipile_overlay/ADAPTER_HARDENING.md": "plugins/platforms/lmi_unipile_overlay/ADAPTER_HARDENING.md",
    "plugins/platforms/lmi_unipile_overlay/README.md": "plugins/platforms/lmi_unipile_overlay/README.md",
    "plugins/platforms/lmi_unipile_overlay/__init__.py": "plugins/platforms/lmi_unipile_overlay/__init__.py",
    "plugins/platforms/lmi_unipile_overlay/_lmi_media_runtime.py": "plugins/platforms/lmi_unipile_overlay/_lmi_media_runtime.py",
    "plugins/platforms/lmi_unipile_overlay/_lmi_media_bootstrap.py": "plugins/platforms/lmi_unipile_overlay/_lmi_media_bootstrap.py",
    "plugins/platforms/lmi_unipile_overlay/bridge.py": "plugins/platforms/lmi_unipile_overlay/bridge.py",
    "plugins/platforms/lmi_unipile_overlay/deployment.py": "plugins/platforms/lmi_unipile_overlay/deployment.py",
    "plugins/platforms/lmi_unipile_overlay/manifest.yaml": "plugins/platforms/lmi_unipile_overlay/manifest.yaml",
    "plugins/platforms/lmi_unipile_overlay/live_inputs/_unipile_common.py": "plugins/platforms/_unipile_common.py",
    "plugins/platforms/lmi_unipile_overlay/live_inputs/whatsapp_unipile/adapter.py": "plugins/platforms/whatsapp_unipile/adapter.py",
    "plugins/platforms/lmi_unipile_overlay/live_inputs/instagram/adapter.py": "plugins/platforms/instagram/adapter.py",
}


class SyncBlocked(RuntimeError):
    """The deployment preflight or verification contract failed."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_path(repo_root: Path, relative: str) -> Path:
    path = repo_root / relative
    if not path.is_file() or path.is_symlink():
        raise SyncBlocked(f"managed source is not a regular file: {relative}")
    return path


def verify_sources(repo_root: Path) -> dict[str, str]:
    observed = {}
    for relative, expected in EXPECTED_FILES.items():
        path = source_path(repo_root, relative)
        actual = sha256(path)
        if actual != expected:
            raise SyncBlocked(f"managed source hash mismatch: {relative}")
        observed[relative] = actual
    return observed


def runtime_env_value(data_root: Path, name: str) -> str:
    env_path = data_root / "runtime.env"
    if not env_path.is_file() or env_path.is_symlink():
        raise SyncBlocked("runtime.env is unavailable")
    prefix = name + "="
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                return value
    raise SyncBlocked(f"runtime.env is missing {name}")


def verify_media_module(data_root: Path) -> str:
    path = Path(runtime_env_value(data_root, "LMI_MEDIA_FOLLOWUP_MODULE_PATH"))
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise SyncBlocked("configured LMI media module path is not a regular file")
    actual = sha256(path)
    if actual != MEDIA_MODULE_SHA256:
        raise SyncBlocked("configured LMI media module hash is not the reviewed hash")
    return actual


def compile_sources(repo_root: Path, relatives: Iterable[str]) -> None:
    for relative in relatives:
        path = source_path(repo_root, relative)
        if path.suffix == ".py":
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except (OSError, SyntaxError) as exc:
                raise SyncBlocked(f"managed Python source failed syntax validation: {relative}") from exc


def target_path(data_root: Path, relative: str) -> Path:
    path = data_root / TARGETS[relative]
    if path.exists() and path.is_symlink():
        raise SyncBlocked(f"managed destination is a symlink: {TARGETS[relative]}")
    return path


def backup_targets(data_root: Path, backup_root: Path, relatives: Iterable[str]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root / f"lmi-media-sync-{stamp}-{os.getpid()}"
    if backup.exists():
        raise SyncBlocked("backup path already exists")
    backup.mkdir(mode=0o700, parents=True)
    manifest: dict[str, object] = {"files": {}}
    for relative in relatives:
        destination = target_path(data_root, relative)
        entry: dict[str, object] = {"target": TARGETS[relative], "present": destination.is_file()}
        if destination.is_file():
            backup_file = backup / TARGETS[relative]
            backup_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(destination, backup_file)
            entry["sha256"] = sha256(destination)
        manifest["files"][relative] = entry
    (backup / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.chmod(backup / "MANIFEST.json", 0o600)
    return backup


def install_files(repo_root: Path, data_root: Path, relatives: Iterable[str]) -> None:
    stage = Path(tempfile.mkdtemp(prefix=".lmi-media-sync-", dir=str(data_root)))
    try:
        for relative in relatives:
            source = source_path(repo_root, relative)
            staged = stage / TARGETS[relative]
            staged.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            shutil.copy2(source, staged)
            if sha256(staged) != EXPECTED_FILES[relative]:
                raise SyncBlocked(f"staged managed file hash mismatch: {relative}")
        for relative in relatives:
            destination = target_path(data_root, relative)
            staged = stage / TARGETS[relative]
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            os.replace(staged, destination)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def verify_targets(data_root: Path, relatives: Iterable[str]) -> dict[str, str]:
    observed = {}
    for relative in relatives:
        destination = target_path(data_root, relative)
        if not destination.is_file():
            raise SyncBlocked(f"managed destination is missing: {TARGETS[relative]}")
        actual = sha256(destination)
        if actual != EXPECTED_FILES[relative]:
            raise SyncBlocked(f"managed destination hash mismatch: {TARGETS[relative]}")
        observed[TARGETS[relative]] = actual
    return observed


def synchronize(
    *,
    repo_root: Path = REPO_ROOT,
    data_root: Path = DEFAULT_DATA_ROOT,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    check_only: bool = False,
    verify_module: bool = True,
) -> dict[str, object]:
    """Verify or atomically install the reviewed managed files."""
    repo_root = Path(repo_root)
    data_root = Path(data_root)
    backup_root = Path(backup_root)
    source_hashes = verify_sources(repo_root)
    compile_sources(repo_root, EXPECTED_FILES)
    module_hash = verify_media_module(data_root) if verify_module else None
    if check_only:
        # A check is a readiness gate, not a partial inventory. Every
        # managed destination must exist and match its reviewed source before
        # the updater can claim the deployment is healthy.
        before = verify_targets(data_root, EXPECTED_FILES)
        return {"mode": "check", "source_hashes": source_hashes, "target_hashes": before, "media_module_sha256": module_hash}

    backup = backup_targets(data_root, backup_root, EXPECTED_FILES)
    try:
        install_files(repo_root, data_root, EXPECTED_FILES)
        after = verify_targets(data_root, EXPECTED_FILES)
    except Exception:
        # The backup is complete before the first replace.  Leave it available
        # for the operator; do not guess at rollback while another service may
        # be reading a file.  The updater will refuse to restart on this error.
        raise
    return {
        "mode": "apply",
        "backup": str(backup),
        "source_hashes": source_hashes,
        "target_hashes": after,
        "media_module_sha256": module_hash,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    args = parser.parse_args(argv)
    try:
        LOCK_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with LOCK_PATH.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = synchronize(
                data_root=args.data_root,
                backup_root=args.backup_root,
                check_only=args.check,
            )
    except (OSError, SyncBlocked) as exc:
        print(f"LMI_MEDIA_SYNC_BLOCKED: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
